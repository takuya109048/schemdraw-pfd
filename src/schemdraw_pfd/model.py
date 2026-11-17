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
from .geometry import find_crossings
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
from .router import Route, Router, RoutingError, needless_uturn, resolve_port
from .style import DEFAULT_LINE_STYLES, LineStyle
from .symmetry import Symmetry, find_units, gap_groups

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
SWAP_ROUNDS = 2  # rebuilds with swappable ports re-sorted (see Flowsheet.build)
BEND_COST = 6  # flow layout: sheet cells (width + height) one bend is worth
KINDS = tuple(DEFAULT_LINE_STYLES)  # built-in line kinds (kept for compatibility)

_ELEMENT_KEYS = {"id", "type", "level", "params", "ports", "desc", "elevation", "span"}
_CONNECTION_KEYS = {"from", "to", "type", "inline", "anchor", "recycle"}
_TOP_KEYS = set(
    "version layout custom_elements line_kinds elements connections".split()
)
LAYOUTS = ("order", "flow")


@dataclass
class Diagram:
    """A routed sheet.

    ``warnings``: lines that turn back with nothing in the way (needless
    U-turns).  ``uturns``: lines that turn back at all, around equipment
    included -- the layout keeps them as few as it can.
    """

    placed: list[Placed]
    routes: list[tuple[Connection, Route]]
    warnings: list[str]
    descs: dict[str, str] = field(default_factory=dict)
    line_styles: dict[str, LineStyle] = field(
        default_factory=lambda: dict(DEFAULT_LINE_STYLES)
    )
    uturns: int = 0

    @property
    def bends(self) -> int:
        return sum(r.bends for _, r in self.routes)

    @property
    def crossings(self) -> int:
        """Places where two lines cross."""
        return len(find_crossings([r.points for _, r in self.routes]))

    @property
    def size(self) -> tuple[int, int]:
        """Overall (width, height) of the equipment area in grid cells."""
        return _size(self.placed)


def _size(placed: list[Placed]) -> tuple[int, int]:
    """(width, height) of the bounding box of ``placed`` in grid cells."""
    boxes = [p.bbox for p in placed]
    return (
        max(b[2] for b in boxes) - min(b[0] for b in boxes),
        max(b[3] for b in boxes) - min(b[1] for b in boxes),
    )


def _score(d: Diagram) -> tuple[int, int]:
    """Lower is better: U-turns first, then bends."""
    return d.uturns, d.bends


def _offset(p: Placed, q: Placed) -> tuple[int, int]:
    return p.x - q.x, p.y - q.y


def _check_new_id(id: str, known: set[str]) -> None:
    if id.startswith(GENERATED_PREFIX):
        raise ValueError(f"id {id!r}: ids starting with '_' are reserved")
    if id in known:
        raise ValueError(f"duplicate id {id!r}")


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
        # flow layout: how many cells of sheet size one extra bend may cost
        self.bend_cost = BEND_COST
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
        span: int = 1,
        **params: Any,
    ) -> Equipment:
        """Add equipment to the right of everything added so far.

        ``span``: a tall item (a tower) stands through this many levels,
        starting at ``level``: it does not stretch those bands, the items on
        them stand beside it, and the first level above clears its top.

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
        if span < 1:
            raise ValueError("span must be >= 1")
        id = id or f"{type}{len(self.equipment) + 1}"
        check_id(id)
        _check_new_id(id, self.ids())
        eq = Equipment(
            id, type, level, params, dict(ports or {}), desc, elevation, span
        )
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

        A port may be used by any number of lines: a main pipe (header) then
        runs on from the port and the lines cut into it one after another.

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
            if item.id is not None:
                _check_new_id(item.id, known)
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
        e: Equipment = eq
        return _context(e.id, lambda: e.geometry(self.library))

    def expand(self, equipment: list[Equipment] | None = None) -> Expanded:
        """The flat netlist that is laid out: inline items and automatic tees
        become equipment, every port carries at most one line.

        ``equipment``: the items in another order (default: as added)."""
        return expand(
            self.equipment if equipment is None else equipment,
            self.connections,
            self.library,
            lambda e, build: _context(e.id, build),
        )

    # -- build ----------------------------------------------------------------
    def build(self, max_expand: int = 4, compact: bool = True) -> Diagram:
        """Lay out and route (:meth:`_build`), then let swappable ports trade
        lines (:meth:`_reassign`).

        Head nozzles of a vessel and plain header branches serve alike (see
        ``Geometry.swappable``): the port a line names only says which face
        it uses.  After a first build the lines on each such group are sorted
        along the face by where they head, so the outermost line takes the
        outermost nozzle and lines do not cross in front of it; the sheet is
        built again and kept if it costs no more.  The flowsheet itself is
        not changed: the diagram's connections name the ports used.
        """
        best = self._build(max_expand, compact)
        user = self.connections
        try:
            for _ in range(SWAP_ROUNDS):
                conns = self._reassign(best)  # from the current connections
                if conns is None:
                    break
                self.connections = conns
                try:
                    trial = self._build(max_expand, compact)
                except RoutingError:
                    break
                if self._cost(trial) > self._cost(best):
                    break
                best = trial
        finally:
            self.connections = user
        return best

    def _cost(self, d: Diagram) -> tuple[int, int]:
        """U-turns, then (flow layout) bends and crossings weighed against the
        sheet size, or (order layout) bends."""
        if self.layout != "flow":
            return _score(d)
        return d.uturns, (d.bends + d.crossings) * self.bend_cost + sum(d.size)

    def _reassign(self, d: Diagram) -> list[Connection] | None:
        """The connections with the lines on every swappable port group
        sorted along their face by where they come from in ``d`` (the way
        each turns after leaving its nozzle); None if that changes nothing.
        A port shared by several lines stays put."""
        placed = {p.id: p for p in d.placed}
        # where a line comes from, seen from its port: the end of its first
        # run along the face (after the leg out of the nozzle), or its far end
        far: dict[tuple[str, str], tuple[float, float]] = {}
        for c, r in d.routes:
            (a, pa), (b, pb) = c.ends
            pts = r.points
            far.setdefault((a, pa), pts[min(2, len(pts) - 1)])
            far.setdefault((b, pb), pts[max(len(pts) - 3, 0)])
        uses: dict[tuple[str, str], list[tuple[int, int]]] = {}
        for k, c in enumerate(self.connections):
            for end, ref in enumerate(c.ends):
                uses.setdefault(ref, []).append((k, end))
        new = list(self.connections)
        changed = False
        for e in self.equipment:
            p = placed.get(e.id)
            if p is None or not p.geo.swappable:
                continue
            ports = p.geo.ports
            for group in p.geo.swappable:
                slot_name: dict[tuple[str, int], str] = {}
                for name in group:
                    slot_name.setdefault((ports[name].side, ports[name].offset), name)
                lines = []  # (key, slot, connection, end)
                for (eq, name), us in uses.items():
                    port = ports.get(name) if eq == e.id else None
                    slot = None if port is None else (port.side, port.offset)
                    if slot not in slot_name or len(us) != 1 or (eq, name) not in far:
                        continue
                    lines.append((far[(eq, name)], slot, *us[0]))
                if len(lines) < 2:
                    continue
                side = lines[0][1][0]
                along = 0 if side in "NS" else 1  # coordinate along the face
                face = {"N": p.y + p.geo.height, "S": p.y, "E": p.x + p.geo.width}
                base = face.get(side, p.x)
                origin = p.x if along == 0 else p.y
                mid = origin + sum(s[1] for _, s, _, _ in lines) / len(lines)

                def key(line: tuple) -> tuple[float, float]:
                    pt = line[0]
                    dist = abs(pt[1 - along] - base)  # how far out it turns
                    # beyond the outermost nozzle, the line turning nearest
                    # the face must be outermost: it would cut the others
                    return pt[along], -dist if pt[along] > mid else dist

                slots = sorted((s for _, s, _, _ in lines), key=lambda s: s[1])
                for (_, slot, k, end), target in zip(sorted(lines, key=key), slots):
                    if target == slot:
                        continue
                    ref = f"{e.id}.{slot_name[target]}"
                    c = new[k]
                    new[k] = replace(c, src=ref) if end == 0 else replace(c, dst=ref)
                    changed = True
        return new if changed else None

    def _build(self, max_expand: int = 4, compact: bool = True) -> Diagram:
        """Lay out and route with the ports as named.

        0. Expand inline items and shared ports (:meth:`expand`).
        1. Find the smallest uniform spacing that routes every pipe in strict mode
           (no U/I-turns); only if that is impossible is the relaxed mode used.
           A pipe that then turns back with no equipment in the way is reported
           in ``Diagram.warnings``; turning back around equipment is fine.
        2. Compaction: every individual gap (between neighbouring elements and
           between level bands) is squeezed cell by cell as long as all pipes still
           route with no more U-turns and no more bends than before.
        ``layout="flow"`` packs densely: an item only keeps clear of the items
        it could touch (those on its own height), so items on different levels
        share X from the start.  A squeeze is then kept when it has no more
        U-turns and its *cost* -- ``bend_cost`` per bend and per line crossing
        plus the sheet's width and height in cells -- does not grow: pipes stay
        simple (fewest bends and crossings first), and an extra bend or
        crossing is only paid for by a clearly smaller sheet.
        """
        net, items, links, flow = self._prepare()
        free = self.layout == "flow"
        sym = find_units(net)
        levels = _band_count(net.equipment)
        # tightest gap each element may ever have (independent of the level gaps)
        floor_x = min_gap_x(items, place_y(items, [MIN_GAP] * levels, links, free))
        if free:  # dense: the gap is to whatever stands on the same height
            floor_x = [MIN_GAP] * len(items)
        floor_y = [MIN_GAP] * levels
        overlap = [free and not it.edge for it in items]
        weight = self.bend_cost if free else 0  # order layout: bends only

        # a crossing weighs like a bend: a squeeze may not buy a crossing
        cost = self._cost

        # many gap changes do not move anything (a pipe corridor or the
        # neighbour's height decides); route each distinct placement only once
        seen: dict[tuple, tuple[Diagram | None, list[str]]] = {}

        def attempt(
            gx: list[int],
            gy: list[int],
            relaxed: bool,
            best: Diagram | None = None,
        ) -> tuple[Diagram | None, list[str]]:
            placed = place(items, gx, gy, links, overlap, flow, free)
            key = (relaxed, tuple((p.x, p.y) for p in placed))
            if key not in seen:
                bound = None
                if best is not None:  # most bends this placement can afford
                    turns, total = cost(best)
                    size = sum(_size(placed)) if weight else 0
                    bound = (turns, (total - size) // max(weight, 1))
                seen[key] = self._route_all(net, placed, relaxed, bound, sym)
            return seen[key]

        def spread(k: int) -> tuple[list[int], list[int]]:
            return [f + k for f in floor_x], [f + k for f in floor_y]

        best = None
        failures: list[str] = []
        relaxed = False
        gx, gy = spread(0)
        for k in range(max_expand):  # strict: the tightest spacing that routes
            trial, fails = attempt(*spread(k), False)
            failures = failures or fails
            if trial and (best is None or cost(trial) < cost(best)):
                best, (gx, gy) = trial, spread(k)
            if best and not weight:
                break  # order layout: the tightest one; flow: the cheapest one
        if best is None:  # relaxed: the spacing with the fewest U-turns, then bends
            relaxed = True
            for k in range(max_expand):
                trial, fails = attempt(*spread(k), True)
                if trial and (best is None or cost(trial) < cost(best)):
                    best, (gx, gy) = trial, spread(k)
                failures = failures or fails
        if best is None:
            raise RoutingError("could not route: " + "; ".join(failures))
        if not compact:
            return best

        # gaps of corresponding items of repeated units move together, so the
        # copies stay alike (see :mod:`.symmetry`)
        steps = (
            (gx, floor_x, gap_groups(sym)),
            (gy, floor_y, [[i] for i in range(len(gy))]),
        )
        for gaps, floor, groups in steps:
            for group in groups:
                while all(gaps[i] > floor[i] for i in group):
                    for i in group:
                        gaps[i] -= 1
                    trial, _ = attempt(gx, gy, relaxed, best)
                    if trial and cost(trial) <= cost(best):
                        best = trial
                    else:
                        for i in group:
                            gaps[i] += 1
                        break
        return best

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
            net = self.expand(
                [replace(e) for e in sorted(self.equipment, key=lambda e: pos[e.id])]
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
            net.equipment,
            net.connections,
            net.geos,
            self._process_kinds(),
            net.owners,
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
        sym: Symmetry | None = None,
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
                net, placed, relaxed, order, bound, sym
            )
            if d is not None and (best is None or _score(d) < _score(best)):
                best = d
                if not d.uturns:
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
        sym: Symmetry | None = None,
    ) -> tuple[Diagram | None, list[str], list[int]]:
        """Route the lines in ``order``; also returns the problem lines.

        ``bound``: (U-turns, bends) of the best diagram so far.  Both only grow
        line by line, so routing stops -- returning no diagram -- as soon as
        this placement can no longer beat it, and at its first failure.

        ``sym``: lines of repeated units (see :mod:`.symmetry`).  The first line
        of a class routed sets the shape; a corresponding line whose ends are
        moved by the same offset first tries that shape, moved along, and is
        searched for only if the copy does not fit.
        """
        by_id = {p.id: p for p in placed}
        router = Router(placed)
        routes: dict[int, Route] = {}
        shapes: dict[int, tuple[int, Route]] = {}  # line class -> first routed
        ready: dict[int, Route] = {}  # copies laid down with their class
        members: dict[int, list[int]] = {}
        if sym and sym.repeated:
            for cj, k in enumerate(sym.line_class):
                members.setdefault(k, []).append(cj)
        turned: list[int] = []  # lines that turn back, in routing order
        failures: list[str] = []
        failed: list[int] = []  # problem lines: failures first, then U-turns
        bends = 0
        for ci in order:
            c = net.connections[ci]
            (si, sp), (di, dp) = c.ends
            src, dst = resolve_port(by_id[si], sp), resolve_port(by_id[di], dp)
            cls = sym.line_class[ci] if sym else ci
            r = ready.pop(ci, None)
            if r is None and cls not in shapes:
                # first of its class: one shape that fits every copy
                copies = [ci]
                for cj in members.get(cls, []):
                    if cj == ci or cj in routes:
                        continue
                    (sj, _), (dj, _) = net.connections[cj].ends
                    shift = _offset(by_id[sj], by_id[si])
                    if shift == _offset(by_id[dj], by_id[di]):
                        copies.append(cj)
                if len(copies) > 1:
                    ends = []
                    for cj in copies:
                        (sj, spj), (dj, dpj) = net.connections[cj].ends
                        ends.append(
                            (
                                resolve_port(by_id[sj], spj),
                                resolve_port(by_id[dj], dpj),
                                _offset(by_id[sj], by_id[si]),
                            )
                        )
                    together = router.route_together(ends, relaxed)
                    if together:
                        r = together[0]
                        ready.update(zip(copies[1:], together[1:]))
            elif r is None:  # its class's shape did not fit all: try it here
                cm, model = shapes[cls]
                (msi, _), (mdi, _) = net.connections[cm].ends
                shift = _offset(by_id[si], by_id[msi])
                if shift == _offset(by_id[di], by_id[mdi]):
                    r = router.replay(src, dst, model, shift)
            if r is None:
                r = router.route(src, dst, relaxed)
            if r is not None and cls not in shapes:
                shapes[cls] = (ci, r)
            if r is None:
                failures.append(f"{c.src} -> {c.dst}")
                failed.append(ci)
                if bound is not None:
                    return None, failures, failed
                continue
            if not r.strict:
                turned.append(ci)
            routes[ci] = r
            bends += r.bends
            if bound is not None and (len(turned), bends) > bound:
                return None, ["worse than the best layout so far"], failed
        if failures:
            return None, failures, failed
        boxes = [p.bbox for p in placed]
        warnings = [
            f"{c.src} -> {c.dst}: U-turn with nothing in the way"
            for ci in sorted(turned)
            if needless_uturn(routes[ci].points, boxes)
            for c in [net.connections[ci]]
        ]
        descs = {e.id: e.desc for e in net.equipment if e.desc}
        diagram = Diagram(
            placed,
            [(net.connections[ci], routes[ci]) for ci in sorted(routes)],
            warnings,
            descs,
            dict(self.line_styles),
            len(turned),
        )
        return diagram, [], turned

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
            int(e.get("span", 1)),
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
        items.append(Item(e.id, e.type, e.level, g, e.elevation, edge, e.span))
    return items


def _band_count(eqs: list[Equipment]) -> int:
    """Level bands needed: the highest level, or the top of a tall item."""
    return max(e.level + max(e.span, 1) for e in eqs)


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


def _heights(
    net: Expanded,
    links: list[Link],
    flags: dict[str, str] | None = None,
    anchor_free: bool = False,
) -> tuple[list[Item], list[int], list[int]]:
    """Items, their bottom y and the band floors on the tightest band gaps."""
    items = _items(net, flags)
    gap = [MIN_GAP] * _band_count(net.equipment)
    _, base = _bands(items, gap)
    return items, place_y(items, gap, links, anchor_free), base


def _lift(eq: Equipment, elevation: int) -> bool:
    """Set ``eq``'s elevation; True if it changed."""
    if eq.elevation == elevation:
        return False
    eq.elevation = elevation
    return True


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
    index = {e.id: i for i, e in enumerate(eqs)}
    # the taps of one main pipe are lifted together: the main pipe stays straight
    group = {t: [index[u] for u in h] for h in net.headers for t in h}
    for _ in range(8):
        items, ys, base = _heights(net, links, flags, anchor_free=True)
        moved = False
        for lk in links:
            if items[lk.a].level != items[lk.b].level:
                continue
            for q, q_side, p, p_side, p_off in (
                (lk.a, lk.a_side, lk.b, lk.b_side, lk.b_off),
                (lk.b, lk.b_side, lk.a, lk.a_side, lk.a_off),
            ):
                top = ys[q] + items[q].geo.height
                if (q_side, p_side) == ("N", "S"):
                    # facing nozzles (a controller over its valve): p goes
                    # above -- but a vessel stays on its floor (a tank whose
                    # bottom feeds another tank's top on the same level)
                    lift = eqs[p].id not in fixed and not items[p].geo.nozzles
                    if ys[p] < top + 2 and lift:
                        moved |= _lift(eqs[p], top + 2 - base[items[p].level])
                    continue
                if q_side not in "NS" or p_side not in "WE":
                    continue
                port = ys[p] + p_off
                if q_side == "N" and port <= top + 1:
                    # the pipe turns down one cell above the nozzle: the port
                    # must be two cells up for an L
                    j, y = p, top + 2 - p_off
                elif q_side == "S" and port >= ys[q]:
                    j, y = (q, port + 1) if eqs[q].id not in fixed else (p, -1)
                else:
                    continue
                if eqs[j].id in fixed or y < base[items[j].level]:
                    continue
                elevation = y - base[items[j].level]
                members = group.get(eqs[j].id, [j])
                if len(members) > 1:
                    elevation = max(
                        [elevation] + [eqs[k].elevation or 0 for k in members]
                    )
                for k in members:
                    moved |= _lift(eqs[k], elevation)
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
        items, ys, base = _heights(net, links)
        moved = False
        for t in net.pending:
            o = index[t.owner]
            port_y = ys[o] + (net.geos[t.owner].height if t.side == "N" else 0)
            floor = base[items[index[t.tee]].level]
            want = max(port_y - floor + (0 if t.side == "N" else -2), 0)
            moved |= _lift(net.equipment[index[t.tee]], want)
        if not moved:
            return
