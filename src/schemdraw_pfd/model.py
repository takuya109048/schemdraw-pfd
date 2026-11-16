"""Flowsheet model: add equipment, define connections, build the diagram."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .elements import DEFAULT_LIBRARY, CustomSpec, ElementLibrary, Geometry
from .expand import GENERATED_PREFIX, Expanded, expand
from .flow import plan_flow
from .layout import (
    FlowLink,
    Item,
    Link,
    Placed,
    _bands,
    min_gap_x,
    place,
    place_y,
    port_dx,
)
from .netlist import (
    ANCHORS,
    Connection,
    Equipment,
    InlineItem,
    check_id,
    split_ref,
)
from .router import Route, Router, RoutingError, resolve_port
from .style import DEFAULT_LINE_STYLES, LineStyle

__all__ = [
    "FORMAT_VERSION",
    "Connection",
    "Diagram",
    "Equipment",
    "Flowsheet",
    "InlineItem",
]

FORMAT_VERSION = 1  # version of the JSON flowsheet format written by to_dict
MIN_GAP = 1  # cells always left between neighbouring elements / level bands
REROUTE_ROUNDS = 3  # rip-up and reroute attempts per placement
KINDS = tuple(DEFAULT_LINE_STYLES)  # built-in line kinds (kept for compatibility)

_ELEMENT_KEYS = {"id", "type", "level", "params", "ports", "desc", "elevation"}
_CONNECTION_KEYS = {"from", "to", "type", "inline", "anchor", "recycle"}
_TOP_KEYS = {
    "version",
    "layout",
    "custom_elements",
    "line_kinds",
    "elements",
    "connections",
}
LAYOUTS = ("order", "flow")


@dataclass
class Diagram:
    placed: list[Placed]
    routes: list[tuple[Connection, Route]]
    warnings: list[str]
    descs: dict[str, str] = field(default_factory=dict)
    line_styles: dict[str, LineStyle] = field(
        default_factory=lambda: dict(DEFAULT_LINE_STYLES)
    )

    @property
    def bends(self) -> int:
        return sum(r.bends for _, r in self.routes)

    @property
    def size(self) -> tuple[int, int]:
        """Overall (width, height) of the equipment area in grid cells."""
        return (
            max(p.bbox[2] for p in self.placed) - min(p.bbox[0] for p in self.placed),
            max(p.bbox[3] for p in self.placed) - min(p.bbox[1] for p in self.placed),
        )


def _score(d: Diagram) -> tuple[int, int]:
    """Lower is better: unavoidable U-turns first, then bends."""
    return len(d.warnings), d.bends


def _context(where: str, fn: Callable[[], Any]) -> Any:
    """Run ``fn`` and prefix any input error with where it happened."""
    try:
        return fn()
    except (KeyError, ValueError, TypeError) as e:
        msg = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
        try:
            wrapped = type(e)(f"{where}: {msg}")
        except Exception:  # exotic subclass with another signature
            raise e from None
        raise wrapped from e


class Flowsheet:
    """Equipment is placed left to right in the order it was added.

    ``library``: element types to use.  The flowsheet always gets its own
    library on top of it (default: the standard elements), so element types
    registered for one flowsheet (``fs.library.register`` or JSON
    ``custom_elements``) never leak into another.

    ``layout``: ``"order"`` -- left to right in the order items were added,
    each in a column of its own; ``"flow"`` -- the order follows the direction
    of flow (main flow left to right, see :mod:`.flow`) and items on different
    heights may share X.  Levels are kept in both.
    """

    def __init__(
        self, library: ElementLibrary | None = None, layout: str = "order"
    ) -> None:
        if layout not in LAYOUTS:
            raise ValueError(f"layout must be one of {LAYOUTS}, got {layout!r}")
        self.layout = layout
        self.library = ElementLibrary(parent=library or DEFAULT_LIBRARY)
        self.line_styles: dict[str, LineStyle] = dict(DEFAULT_LINE_STYLES)
        self.equipment: list[Equipment] = []
        self.connections: list[Connection] = []

    # -- editing --------------------------------------------------------------
    def ids(self) -> set[str]:
        """Ids of all equipment, including inline items that were given an id."""
        out = {e.id for e in self.equipment}
        out |= {i.id for c in self.connections for i in c.inline if i.id}
        return out

    def add(
        self,
        type: str,
        id: str | None = None,
        level: int = 0,
        ports: dict[str, Any] | None = None,
        desc: str = "",
        elevation: int | None = None,
        **params: Any,
    ) -> Equipment:
        """Add equipment to the right of everything added so far.

        ``level``: 0 is the bottom tier.  ``ports``: extra connection points for
        this item, ``{"name": ("N|S|E|W", grid_offset)}``; they are projected onto
        the element's outline like the built-in ones.  ``desc``: specification
        text (name, size, material ...) listed with the tag above the diagram, or
        below it for rotating machinery.  ``elevation``: cells above the floor of
        the level band (default: automatic, lined up with the connected port).
        ``params``: element parameters, plus the common ``rotation`` (0/90/180/270)
        and ``mirror`` (bool).
        """
        self.library.get(type)  # validate early
        if level < 0:
            raise ValueError("level must be >= 0")
        if elevation is not None and elevation < 0:
            raise ValueError("elevation must be >= 0")
        id = id or f"{type}{len(self.equipment) + 1}"
        check_id(id)
        if id.startswith(GENERATED_PREFIX):
            raise ValueError(f"id {id!r}: ids starting with '_' are reserved")
        if id in self.ids():
            raise ValueError(f"duplicate id {id!r}")
        eq = Equipment(id, type, level, params, dict(ports or {}), desc, elevation)
        self.equipment.append(eq)
        return eq

    def connect(
        self,
        src: str,
        dst: str,
        kind: str = "pipe",
        inline: Sequence[Any] | None = None,
        anchor: str = "auto",
        recycle: bool = False,
    ) -> Connection:
        """Line from ``"ID.port"`` to ``"ID.port"``.

        ``kind``: one of :attr:`line_styles` -- built in are ``pipe`` (process
        line with flow arrow), ``signal`` (dashed instrument signal) and
        ``instrument`` (thin line from a tapping point to an instrument); add
        more with :meth:`add_line_kind`.

        ``inline``: equipment sitting on the line, in flow order, e.g.
        ``["valve", {"type": "valve", "id": "FV-1", "params": {"kind":
        "control"}}]`` (see :class:`InlineItem`).  They are placed next to the
        ``dst`` end (``anchor="auto"``: next to ``src`` when ``dst`` is an
        off-sheet flag) on its level.

        A port may be used by up to three lines; a tee is then inserted for them
        automatically.

        ``recycle``: the line runs back against the main flow (reflux,
        recycle).  Only matters for ``layout="flow"``, which also finds loops on
        its own; marking the line says which one of a loop runs back.
        """
        if kind not in self.line_styles:
            raise ValueError(
                f"kind must be one of {sorted(self.line_styles)}, got {kind!r}"
            )
        if anchor not in ANCHORS:
            raise ValueError(f"anchor must be one of {ANCHORS}, got {anchor!r}")
        items = [InlineItem.parse(v) for v in inline or ()]
        known = self.ids()
        for item in items:
            self.library.get(item.type)
            if item.id is None:
                continue
            if item.id.startswith(GENERATED_PREFIX):
                raise ValueError(f"id {item.id!r}: ids starting with '_' are reserved")
            if item.id in known:
                raise ValueError(f"duplicate id {item.id!r}")
            known.add(item.id)
        for ref in (src, dst):
            i, _ = split_ref(ref)
            if i not in known:
                raise KeyError(f"unknown equipment {i!r}")
        c = Connection(src, dst, kind, items, anchor, bool(recycle))
        self.connections.append(c)
        return c

    def add_line_kind(self, name: str, style: LineStyle) -> None:
        """Add (or restyle) a kind of line, e.g. ``utility`` or ``electric``."""
        self.line_styles[name] = style

    def set_order(self, ids: list[str]) -> None:
        """Set the left-to-right order explicitly (inline items follow their line)."""
        by_id = {e.id: e for e in self.equipment}
        if sorted(ids) != sorted(by_id):
            raise ValueError("set_order needs every equipment id exactly once")
        self.equipment = [by_id[i] for i in ids]

    def geometry(self, eq: Equipment | str) -> Geometry:
        """Built geometry of one item (with this flowsheet's library).

        A string may also name an inline item or a generated tee.
        """
        if isinstance(eq, str):
            found = next((e for e in self.equipment if e.id == eq), None)
            if found is None:
                return self.expand().geos[eq]
            eq = found
        e = eq
        return _context(e.id, lambda: e.geometry(self.library))

    def expand(self) -> Expanded:
        """The flat netlist that is laid out: inline items and automatic tees
        become equipment, every port carries at most one line."""
        return expand(
            self.equipment,
            self.connections,
            self.library,
            lambda e, build: _context(e.id, build),
        )

    # -- build ----------------------------------------------------------------
    def build(self, max_expand: int = 4, compact: bool = True) -> Diagram:
        """Lay out and route.

        0. Expand inline items and shared ports (:meth:`expand`).
        1. Find the smallest uniform spacing that routes every pipe in strict mode
           (no U/I-turns); only if that is impossible is the relaxed mode used, and
           each affected pipe is reported in ``Diagram.warnings``.
        2. Compaction: every individual gap (between neighbouring elements and
           between level bands) is squeezed cell by cell as long as all pipes still
           route with no more warnings and no more bends than before.
        3. ``layout="flow"`` only: from left to right, each item is allowed to
           share X with items above / below it (it slides left under or over
           them) when that keeps the routing as good; then the gaps are
           squeezed once more.
        """
        net, items, links, flow = self._prepare()
        free = self.layout == "flow"
        levels = max(e.level for e in net.equipment) + 1
        # tightest gap each element may ever have (independent of the level gaps)
        floor_x = min_gap_x(items, place_y(items, [MIN_GAP] * levels, links, free))
        floor_y = [MIN_GAP] * levels
        overlap = [False] * len(items)

        # many gap changes do not move anything (a pipe corridor or the
        # neighbour's height decides); route each distinct placement only once
        seen: dict[tuple, tuple[Diagram | None, list[str]]] = {}

        def attempt(
            gx: list[int],
            gy: list[int],
            relaxed: bool,
            bound: tuple[int, int] | None = None,
        ) -> tuple[Diagram | None, list[str]]:
            placed = place(items, gx, gy, links, overlap, flow, free)
            key = (relaxed, tuple((p.x, p.y) for p in placed))
            if key not in seen:
                seen[key] = self._route_all(net, placed, relaxed, bound)
            return seen[key]

        def spread(k: int) -> tuple[list[int], list[int]]:
            return [f + k for f in floor_x], [f + k for f in floor_y]

        best = None
        failures: list[str] = []
        relaxed = False
        gx, gy = spread(0)
        for k in range(max_expand):  # strict: the tightest spacing that routes
            best, failures = attempt(*spread(k), False)
            if best:
                gx, gy = spread(k)
                break
        if best is None:  # relaxed: the spacing with the fewest U-turns, then bends
            relaxed = True
            for k in range(max_expand):
                trial, fails = attempt(*spread(k), True)
                if trial and (best is None or _score(trial) < _score(best)):
                    best, (gx, gy) = trial, spread(k)
                failures = failures or fails
        if best is None:
            raise RoutingError("could not route: " + "; ".join(failures))
        if not compact:
            return best

        def squeeze(best: Diagram) -> Diagram:
            for gaps, floor in ((gx, floor_x), (gy, floor_y)):
                for i in range(len(gaps)):
                    while gaps[i] > floor[i]:
                        gaps[i] -= 1
                        trial, _ = attempt(gx, gy, relaxed, _score(best))
                        if trial and _score(trial) <= _score(best):
                            best = trial
                        else:
                            gaps[i] += 1
                            break
            return best

        best = squeeze(best)
        if self.layout != "flow":
            return best
        for j, it in enumerate(items):
            if it.edge:
                continue
            overlap[j] = True
            trial, _ = attempt(gx, gy, relaxed, _score(best))
            if trial and _score(trial) <= _score(best):
                best = trial
            else:
                overlap[j] = False
        return squeeze(best)

    def _prepare(self) -> tuple[Expanded, list[Item], list[Link], list[FlowLink]]:
        """Layout input: the expanded netlist in left-to-right order, the
        items, facing-port links and (flow layout) main-flow arrows."""
        if not self.equipment:
            raise ValueError("flowsheet is empty")
        net = self.expand()
        _validate(net)
        flow: list[FlowLink] = []
        flags: dict[str, str] = {}
        if self.layout == "flow":
            # 1st pass on the lines as the user wrote them (inline items left
            # out: their mirroring depends on the declared order); then expand
            # in that order, so inline items face the way their line runs
            geos = {e.id: self.geometry(e) for e in self.equipment}
            first = plan_flow(
                self.equipment, self.connections, geos, self._process_kinds()
            )
            pos = {self.equipment[i].id: k for k, i in enumerate(first.order)}
            # copies: heights are settled on them, never on the user's items
            ordered = [
                replace(e) for e in sorted(self.equipment, key=lambda e: pos[e.id])
            ]
            net = expand(
                ordered,
                self.connections,
                self.library,
                lambda e, build: _context(e.id, build),
            )
            flow, flags = self._apply_flow(net)
        links = _links(net)
        _settle_tees(net, links)
        if self.layout == "flow":
            _settle_lifts(net, flags, links)
        return net, _items(net, flags), links, flow

    def _process_kinds(self) -> set[str]:
        """Line kinds that order the equipment in the flow layout."""
        return {k for k, s in self.line_styles.items() if s.process and s.main}

    def _apply_flow(self, net: Expanded) -> tuple[list[FlowLink], dict[str, str]]:
        """Reorder ``net`` by the direction of flow (see :mod:`.flow`).

        Returns the main-flow arrows for the layout and, for off-sheet flags,
        the sheet edge: left for a flag the flow starts from, right otherwise.
        """
        plan = plan_flow(
            net.equipment, net.connections, net.geos, self._process_kinds()
        )
        old = net.equipment
        net.equipment = [old[i] for i in plan.order]
        new = {e.id: j for j, e in enumerate(net.equipment)}
        flow: list[FlowLink] = []
        for arrow in plan.forward:
            a, b = old[arrow.a].id, old[arrow.b].id
            ga, gb = net.geos[a], net.geos[b]
            pa, pb = ga.ports[arrow.a_port], gb.ports[arrow.b_port]
            flow.append(
                FlowLink(
                    new[a],
                    port_dx(ga, pa.side, pa.offset),
                    new[b],
                    port_dx(gb, pb.side, pb.offset),
                )
            )
        # a flag sits on the edge its connecting port faces away from -- or,
        # when only utility lines reach it, right beside what it serves
        faces: dict[str, str] = {}
        kinds: dict[str, set[str]] = {}
        for c in net.connections:
            for eq, port in c.ends:
                if net.geos[eq].off_sheet:
                    faces.setdefault(eq, net.geos[eq].ports[port].side)
                    kinds.setdefault(eq, set()).add(c.kind)
        flags: dict[str, str] = {}
        for e in net.equipment:
            if not net.geos[e.id].off_sheet:
                continue
            local = e.params.get("local", "auto")
            if local == "auto":
                used = kinds.get(e.id, set())
                local = bool(used) and not any(self.line_styles[k].main for k in used)
            if local:
                flags[e.id] = "local"
            else:
                flags[e.id] = "right" if faces.get(e.id) == "W" else "left"
        return flow, flags

    def _route_all(
        self,
        net: Expanded,
        placed: list[Placed],
        relaxed: bool,
        bound: tuple[int, int] | None = None,
    ) -> tuple[Diagram | None, list[str]]:
        """Route every line on one placement, with rip-up and reroute.

        Lines are routed one by one, so an early line can take the only good
        corridor of a later one.  When lines fail or need a U-turn, everything
        is ripped up and routed again with those lines first (up to
        ``REROUTE_ROUNDS`` times); the best result counts.  The diagram lists
        the routes in connection order whatever order they were routed in.
        """
        order = list(range(len(net.connections)))
        best: Diagram | None = None
        first_failures: list[str] | None = None
        # compaction trials (bound given) get one reroute round: enough to
        # rescue most squeezes, without paying full rounds for hopeless ones
        rounds = REROUTE_ROUNDS if bound is None else 1
        for _ in range(rounds + 1):
            d, failures, problems = self._route_order(
                net, placed, relaxed, order, bound
            )
            if d is not None and (best is None or _score(d) < _score(best)):
                best = d
                if not d.warnings:
                    break
            if first_failures is None:
                first_failures = failures
            if not problems:
                break
            again = problems + [i for i in order if i not in problems]
            if again == order:
                break
            order = again
        if best is not None:
            return best, []
        return None, first_failures or []

    def _route_order(
        self,
        net: Expanded,
        placed: list[Placed],
        relaxed: bool,
        order: list[int],
        bound: tuple[int, int] | None,
    ) -> tuple[Diagram | None, list[str], list[int]]:
        """Route the lines in ``order``; also returns the problem lines.

        ``bound``: (warnings, bends) of the best diagram so far.  Both only grow
        line by line, so routing stops -- returning no diagram -- as soon as
        this placement can no longer beat it, and at its first failure.
        """
        by_id = {p.id: p for p in placed}
        router = Router(placed)
        routes: dict[int, Route] = {}
        warnings: dict[int, str] = {}
        failures: list[str] = []
        failed: list[int] = []  # problem lines: failures first, then U-turns
        bends = 0
        for ci in order:
            c = net.connections[ci]
            (si, sp), (di, dp) = c.ends
            r = router.route(
                resolve_port(by_id[si], sp), resolve_port(by_id[di], dp), relaxed
            )
            if r is None:
                failures.append(f"{c.src} -> {c.dst}")
                failed.append(ci)
                if bound is not None:
                    return None, failures, failed
                continue
            if not r.strict:
                warnings[ci] = (
                    f"{c.src} -> {c.dst}: U-turn/doubling back was unavoidable"
                )
            routes[ci] = r
            bends += r.bends
            if bound is not None and (len(warnings), bends) > bound:
                return None, ["worse than the best layout so far"], failed
        if failures:
            return None, failures, failed
        problems = list(warnings)
        descs = {e.id: e.desc for e in net.equipment if e.desc}
        diagram = Diagram(
            placed,
            [(net.connections[ci], routes[ci]) for ci in sorted(routes)],
            [warnings[ci] for ci in sorted(warnings)],
            descs,
            dict(self.line_styles),
        )
        return diagram, [], problems

    # -- io -------------------------------------------------------------------
    @classmethod
    def from_dict(cls, d: dict, library: ElementLibrary | None = None) -> Flowsheet:
        """Read the JSON flowsheet format (see README).

        Errors name the offending entry, e.g. ``elements[3] (FV-201): ...``.
        """
        version = d.get("version", FORMAT_VERSION)
        if version != FORMAT_VERSION:
            raise ValueError(
                f"unsupported flowsheet format version {version!r} "
                f"(this tool reads version {FORMAT_VERSION})"
            )
        unknown = set(d) - _TOP_KEYS
        if unknown:
            raise ValueError(f"unknown top-level keys {sorted(unknown)}")
        fs = cls(library, d.get("layout", "order"))
        for i, spec in enumerate(d.get("custom_elements", [])):
            where = f"custom_elements[{i}] ({spec.get('name', '?')})"
            _context(where, lambda: fs.library.register(CustomSpec.from_dict(spec)))
        for name, style in d.get("line_kinds", {}).items():
            _context(
                f"line_kinds.{name}",
                lambda: fs.add_line_kind(name, LineStyle.from_dict(style)),
            )
        for i, e in enumerate(d.get("elements", [])):
            _context(f"elements[{i}] ({e.get('id', '?')})", lambda: fs._add_dict(e))
        # lines may end on an inline item declared by a later line
        pending = list(enumerate(d.get("connections", [])))
        while pending:
            retry = []
            for i, c in pending:
                try:
                    fs._connect_dict(c)
                except KeyError:
                    retry.append((i, c))
            if len(retry) == len(pending):
                i, c = retry[0]
                where = f"connections[{i}] ({c.get('from')} -> {c.get('to')})"
                _context(where, lambda: fs._connect_dict(c))
            pending = retry
        fs.connections.sort(key=_original_order(d, fs))
        return fs

    def _add_dict(self, e: dict) -> None:
        unknown = set(e) - _ELEMENT_KEYS
        if unknown:
            raise ValueError(
                f"unknown keys {sorted(unknown)} (element parameters go in 'params')"
            )
        self.add(
            e["type"],
            e.get("id"),
            int(e.get("level", 0)),
            e.get("ports"),
            e.get("desc", ""),
            e.get("elevation"),
            **e.get("params", {}),
        )

    def _connect_dict(self, c: dict) -> None:
        unknown = set(c) - _CONNECTION_KEYS
        if unknown:
            raise ValueError(f"unknown keys {sorted(unknown)}")
        self.connect(
            c["from"],
            c["to"],
            c.get("type", "pipe"),
            c.get("inline"),
            c.get("anchor", "auto"),
            c.get("recycle", False),
        )

    def to_dict(self) -> dict[str, Any]:
        """The flowsheet in the JSON format read by :meth:`from_dict`.

        Element types defined in Python (not as :class:`CustomSpec`) cannot be
        written out; they must be registered again before reading the file.
        """
        d: dict[str, Any] = {"version": FORMAT_VERSION}
        if self.layout != "order":
            d["layout"] = self.layout
        customs = [
            s.to_dict()
            for s in self.library.local().values()
            if isinstance(s, CustomSpec)
        ]
        if customs:
            d["custom_elements"] = customs
        kinds = {
            k: s.to_dict()
            for k, s in self.line_styles.items()
            if DEFAULT_LINE_STYLES.get(k) != s
        }
        if kinds:
            d["line_kinds"] = kinds
        d["elements"] = [e.to_dict() for e in self.equipment]
        d["connections"] = [c.to_dict() for c in self.connections]
        return d

    @classmethod
    def from_json(
        cls, path: str | Path, library: ElementLibrary | None = None
    ) -> Flowsheet:
        return cls.from_dict(
            json.loads(Path(path).read_text(encoding="utf-8-sig")), library
        )

    def to_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", "utf-8"
        )

    def render(self, path: str | Path, **kw: Any) -> Diagram:
        """Build and draw; keyword arguments go to :func:`render.draw`."""
        from .render import draw

        diagram = self.build()
        draw(diagram, path, **kw)
        return diagram


def _original_order(d: dict, fs: Flowsheet) -> Callable[[Connection], int]:
    """Sort key restoring the file's line order after deferred connects."""
    pos = {(c["from"], c["to"]): i for i, c in enumerate(d.get("connections", []))}
    return lambda c: pos.get((c.src, c.dst), len(pos))


# -- layout input from the expanded netlist ------------------------------------------
def _items(net: Expanded, flags: dict[str, str] | None = None) -> list[Item]:
    """Off-sheet flags go to an edge of the sheet: ``flags[id]`` if given (flow
    layout: by direction), else the left one if they come before all equipment
    in the order, otherwise the right one (override with the ``edge``
    parameter: ``left`` / ``right``)."""
    geos = [net.geos[e.id] for e in net.equipment]
    first = next((i for i, g in enumerate(geos) if not g.off_sheet), len(geos))
    items = []
    for i, (e, g) in enumerate(zip(net.equipment, geos)):
        edge = None
        local = e.params.get("local") is True or (flags or {}).get(e.id) == "local"
        if g.off_sheet and not local:  # a local flag is placed like equipment
            auto = "left" if i < first else "right"
            edge = e.params.get("edge") or (flags or {}).get(e.id, auto)
            if edge not in ("left", "right"):
                raise ValueError(f"{e.id}: edge must be 'left' or 'right'")
        items.append(Item(e.id, e.type, e.level, g, e.elevation, edge))
    return items


def _validate(net: Expanded) -> None:
    for c in net.connections:
        for eq, port in c.ends:
            if eq not in net.geos:
                raise KeyError(f"{c.src} -> {c.dst}: unknown equipment {eq!r}")
            ports = net.geos[eq].ports
            if port not in ports:
                raise KeyError(f"{eq}: unknown port {port!r}; known: {sorted(ports)}")


def _links(net: Expanded) -> list[Link]:
    index = {e.id: i for i, e in enumerate(net.equipment)}
    links: list[Link] = []
    for c in net.connections:
        (a, pa), (b, pb) = sorted(
            ((index[eq], net.geos[eq].ports[port]) for eq, port in c.ends),
            key=lambda e: e[0],
        )
        if a != b:
            links.append(Link(a, pa.side, pa.offset, b, pb.side, pb.offset))
    return links


def _settle_lifts(net: Expanded, flags: dict[str, str], links: list[Link]) -> None:
    """Heights that a top or bottom nozzle needs (flow layout).

    On one level, a line between a side port (W/E) of ``p`` and a top / bottom
    nozzle of ``q`` is an L only if the side port is above the top nozzle or
    below the bottom one.  Above a top nozzle ``p`` is lifted; below a bottom
    nozzle the vessel ``q`` is lifted over ``p`` (``p`` cannot go below the
    floor).  Items with an elevation given by the user are never moved.
    Lifting one item can move what lines up with it, so repeat until stable.
    """
    fixed = {e.id for e in net.equipment if e.elevation is not None}
    eqs = net.equipment
    for _ in range(8):
        items = _items(net, flags)
        gap = [MIN_GAP] * (max(e.level for e in eqs) + 1)
        ys = place_y(items, gap, links, anchor_free=True)
        _, base = _bands(items, gap)
        moved = False
        for lk in links:
            if items[lk.a].level != items[lk.b].level:
                continue
            for q, q_side, p, p_side, p_off in (
                (lk.a, lk.a_side, lk.b, lk.b_side, lk.b_off),
                (lk.b, lk.b_side, lk.a, lk.a_side, lk.a_off),
            ):
                if q_side not in "NS" or p_side not in "WE":
                    continue
                top = ys[q] + items[q].geo.height
                port = ys[p] + p_off
                if q_side == "N" and port <= top:
                    j, y = p, top + 1 - p_off
                elif q_side == "S" and port >= ys[q]:
                    j, y = (q, port + 1) if eqs[q].id not in fixed else (p, -1)
                else:
                    continue
                if eqs[j].id in fixed or y < base[items[j].level]:
                    continue
                elevation = y - base[items[j].level]
                if eqs[j].elevation != elevation:
                    eqs[j].elevation = elevation
                    moved = True
        if not moved:
            return


def _settle_tees(net: Expanded, links: list[Link]) -> None:
    """Height of tees beside a vertical shared port on the same level.

    Above an N port the tee's trunk arm must be at least one cell above the
    port (up, then across: an L); below an S port at least one cell below.
    Band heights may grow with the tee, so repeat until nothing moves.
    """
    if not net.pending:
        return
    index = {e.id: i for i, e in enumerate(net.equipment)}
    for _ in range(4):
        items = _items(net)
        gap = [MIN_GAP] * (max(e.level for e in net.equipment) + 1)
        ys = place_y(items, gap, links)
        _, base = _bands(items, gap)
        moved = False
        for t in net.pending:
            o = index[t.owner]
            port_y = ys[o] + (net.geos[t.owner].height if t.side == "N" else 0)
            tee = net.equipment[index[t.tee]]
            want = max(port_y - base[tee.level] + (0 if t.side == "N" else -2), 0)
            if tee.elevation != want:
                tee.elevation = want
                moved = True
        if not moved:
            return
