"""Flowsheet model: add equipment, define connections, build the diagram."""

from __future__ import annotations

import copy
import json
import multiprocessing as mp
import os
import pickle
import re
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .elements import DEFAULT_LIBRARY, CustomSpec, ElementLibrary, Geometry
from .expand import GENERATED_PREFIX, TAP_PREFIX, Expanded, expand
from .flow import plan_flow, split_rows
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
from .levels import Levels, moves, search_levels, start_levels
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
    "version layout rows custom_elements line_kinds elements connections".split()
)
LAYOUTS = ("order", "flow")
ROW_MODES = ("auto", "single")


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
    # a sheet folded into rows (flow layout): each row's (bottom, top) y
    rows: list[tuple[float, float]] = field(default_factory=list)

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


def _run_first(
    fs: Flowsheet, mode: tuple[bool, bool], max_expand: int, compact: bool
) -> Diagram | RoutingError:
    try:
        return fs._first(mode, max_expand, compact)
    except RoutingError as e:
        return e


def _run_swap(
    fs: Flowsheet,
    mode: tuple[bool, bool],
    best: Diagram,
    max_expand: int,
    compact: bool,
) -> Diagram:
    return fs._swap(mode, best, max_expand, compact)


def _pool_map(fn: Callable[..., Any], tasks: list[tuple], jobs: int | None) -> list:
    """``fn(*task)`` for every task, in worker processes when there are
    several tasks and CPUs; in order.  The builds are deterministic, so the
    results are the same either way -- only the wall time differs."""
    workers = min(jobs or os.cpu_count() or 1, len(tasks))
    if workers > 1:
        try:
            ctx = (
                mp.get_context("fork") if "fork" in mp.get_all_start_methods() else None
            )
            with ProcessPoolExecutor(workers, mp_context=ctx) as pool:
                return list(pool.map(fn, *zip(*tasks)))
        except (OSError, BrokenProcessPool, pickle.PicklingError):
            pass  # no processes here: build in this one
    return [fn(*t) for t in tasks]


def _run_level_cost(fs: Flowsheet, levels: Levels, max_expand: int) -> int | None:
    """Cost of one sheet on the given levels: placed and routed once, in
    the plain placement mode and without squeezing the gaps."""
    sheet = copy.copy(fs)
    sheet.equipment = fs._with_levels(levels)
    sheet._in_order = sheet._flag_room = False
    sheet._quick = True
    try:
        return sheet._level_cost(sheet._build(max_expand, compact=False))
    except RoutingError:
        return None


def _repeated(fs: Flowsheet) -> list[list[str]]:
    """Ids of the corresponding items of repeated units (see
    :mod:`.symmetry`), found on the levels the search starts from."""
    sheet = copy.copy(fs)
    sheet.equipment = fs._with_levels()
    net = sheet._prepare()[0]
    sym = find_units(net)
    classes: dict[int, list[str]] = {}
    for e, c in zip(net.equipment, sym.item_class):
        classes.setdefault(c, []).append(e.id)
    return [ids for ids in classes.values() if len(ids) > 1]


def _settle_levels(sheets: list[Flowsheet], max_expand: int, jobs: int | None) -> None:
    """Give every item without a level the level the search finds best
    (see :mod:`.levels`), sheet by sheet."""
    for fs in sheets:
        sets = moves(fs.equipment, fs.connections, _repeated(fs))
        lv = start_levels(fs.equipment)
        if sets:

            def evaluate(tasks: list[Levels], fs: Flowsheet = fs) -> list[int | None]:
                work = [(fs, t, max_expand) for t in tasks]
                return _pool_map(_run_level_cost, work, jobs)

            lv = search_levels(lv, sets, evaluate)
        fs.equipment = fs._with_levels(lv)


def _build_sheets(
    sheets: list[Flowsheet], max_expand: int, compact: bool, jobs: int | None
) -> list[Diagram]:
    """Build several sheets (the rows of a folded sheet, or one sheet): every
    placement mode of every sheet at once, the cheapest per sheet kept (the
    first on a tie), then each sheet's ports re-sorted."""
    _settle_levels(sheets, max_expand, jobs)
    modes = [fs._modes() for fs in sheets]
    tasks = [(fs, m, max_expand, compact) for fs, ms in zip(sheets, modes) for m in ms]
    results = iter(_pool_map(_run_first, tasks, jobs))
    chosen: list[tuple[tuple[bool, bool], Diagram]] = []
    for fs, ms in zip(sheets, modes):
        best: tuple[tuple[bool, bool], Diagram] | None = None
        error: RoutingError | None = None
        for m in ms:
            r = next(results)
            if isinstance(r, RoutingError):
                error = r
            elif best is None or fs._cost(r) < fs._cost(best[1]):
                best = (m, r)
        if best is None:
            assert error is not None
            raise error
        chosen.append(best)
    swaps = [(fs, m, d, max_expand, compact) for fs, (m, d) in zip(sheets, chosen)]
    return _pool_map(_run_swap, swaps, jobs)


ROW_GAP = 6  # cells between two rows, besides their specification texts


def _stack(diagrams: list[Diagram], styles: dict[str, LineStyle]) -> Diagram:
    """Rows stacked top first, each row's left edge at x = 0; the gap below
    a row leaves room for its rotating machines' specs and the specs of the
    row below (their lines of text, at the default theme's size)."""
    from .style import DEFAULT_THEME

    em = DEFAULT_THEME.fontsize / 36.0  # font height in cells (render.PT_PER_UNIT)
    pitch = DEFAULT_THEME.spec_line_height * em

    def extent(d: Diagram) -> tuple[float, float, float]:
        ys = [v for p in d.placed for v in (p.bbox[1], p.bbox[3])]
        ys += [y for _, r in d.routes for _, y in r.points]
        xs = [p.bbox[0] for p in d.placed] + [
            x for _, r in d.routes for x, _ in r.points
        ]
        return min(xs), min(ys), max(ys)

    def text_lines(d: Diagram, rotating: bool) -> int:
        return max(
            (
                d.descs[p.id].count("\n") + 2
                for p in d.placed
                if p.id in d.descs and p.geo.rotating == rotating
            ),
            default=0,
        )

    placed: list[Placed] = []
    routes: list[tuple[Connection, Route]] = []
    warnings: list[str] = []
    descs: dict[str, str] = {}
    bands: list[tuple[float, float]] = []
    top = 0.0  # the next row's top edge
    for k, d in enumerate(diagrams):
        x0, y0, y1 = extent(d)
        if k:
            gap = (text_lines(diagrams[k - 1], True) + text_lines(d, False)) * pitch
            top -= ROW_GAP + int(gap + 0.999)
        dx, dy = -int(x0), int(top - y1)
        placed += [replace(p, x=p.x + dx, y=p.y + dy) for p in d.placed]
        routes += [
            (
                c,
                Route(
                    [(x + dx, y + dy) for x, y in r.points],
                    r.strict,
                    [(x + dx, y + dy) for x, y in r.nodes],
                ),
            )
            for c, r in d.routes
        ]
        warnings += d.warnings
        descs.update(d.descs)
        bands.append((y0 + dy, y1 + dy))
        top = y0 + dy
    return Diagram(
        placed,
        routes,
        warnings,
        descs,
        dict(styles),
        sum(d.uturns for d in diagrams),
        bands,
    )


def _natural(text: str) -> tuple:
    """Sort key putting numbers in order: ``P-2`` before ``P-10``."""
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in re.split(r"(\d+)", text)
        if part
    )


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
        # flow layout: "auto" folds a main flow through several columns into
        # rows (see :meth:`_row_split`); "single" keeps one row
        self.rows = "auto"
        self._in_order = False  # placement mode of the build in progress
        self._flag_room = False  # see _settle_lifts
        self._quick = False  # level search: the first spacing that routes
        # worker processes for the builds of a sheet's rows and placement
        # modes (None: one per CPU; 1: everything in this process)
        self.jobs: int | None = None
        self._room_used = False
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
        level: int | None = None,
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

        ``level``: 0 is the bottom tier; None (the default) works it out from
        the lines (pumps on the ground, see :mod:`.levels`).  ``ports``: extra connection points for
        this item, ``{"name": ("N|S|E|W", grid_offset)}``; they are projected onto
        the element's outline like the built-in ones.  ``desc``: specification
        text (name, size, material ...) listed with the tag above the diagram, or
        below it for rotating machinery.  ``elevation``: cells above the floor of
        the level band (default: automatic, lined up with the connected port).
        ``params``: element parameters, plus the common ``rotation`` (0/90/180/270)
        and ``mirror`` (bool).
        """
        self.library.get(type)  # validate early
        if level is not None and level < 0:
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
        eqs = self._with_levels() if equipment is None else equipment
        edges = None
        if self.layout == "flow":  # flags stand on the sheet edges
            geos = {e.id: self.geometry(e) for e in eqs}
            edges = self._flag_edges(eqs, self.connections, geos)
        return expand(
            eqs,
            self.connections,
            self.library,
            lambda e, build: _context(e.id, build),
            edges,
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

        ``layout="flow"`` ignores the order the equipment and lines were
        written in: they are taken in the natural order of their ids
        (``P-2`` before ``P-10``), so the sheet follows from the lines, their
        direction and the levels alone -- the same whatever the order of the
        JSON lists.
        """
        user = self.equipment, self.connections
        try:
            if self.layout != "flow":
                return self._build_swapped(max_expand, compact)
            self.equipment = sorted(self.equipment, key=lambda e: _natural(e.id))
            self.connections = sorted(
                self.connections,
                key=lambda c: (_natural(c.src), _natural(c.dst), c.kind),
            )
            rows = self._row_split() if self.rows == "auto" else []
            if len(rows) > 1:
                return self._build_rows(rows, max_expand, compact)
            return self._build_swapped(max_expand, compact)
        finally:
            self.equipment, self.connections = user

    def _with_levels(self, levels: Levels | None = None) -> list[Equipment]:
        """The equipment with every level left out filled in (copies): from
        ``levels``, or where the search starts (see :mod:`.levels`; a build
        searches for the best levels first, see :func:`_settle_levels`)."""
        if all(e.level is not None for e in self.equipment):
            return self.equipment
        lv = start_levels(self.equipment) if levels is None else levels
        return [
            e if e.level is not None else replace(e, level=lv[e.id])
            for e in self.equipment
        ]

    def _level_cost(self, d: Diagram) -> int:
        """What the level search lowers: bends and crossings (``bend_cost``
        each) plus the length of all lines in cells."""
        length = sum(
            abs(b[0] - a[0]) + abs(b[1] - a[1])
            for _, r in d.routes
            for a, b in zip(r.points, r.points[1:])
        )
        return (d.bends + d.crossings) * self.bend_cost + round(length)

    def _branchy(self) -> bool:
        """Does any item send process lines to three or more items?"""
        kinds = self._process_kinds()
        out: dict[str, set[str]] = {}
        for c in self.connections:
            if c.kind in kinds:
                (s, _), (d, _) = c.ends
                out.setdefault(s, set()).add(d)
        return any(len(v) >= 3 for v in out.values())

    # -- rows -------------------------------------------------------------------
    def _row_split(self) -> list[set[str]]:
        """The ids of each row (top first) when the flow is folded.

        A main flow running through two or more major units (columns, tall
        items) is cut where it leaves one unit for the next, and before the
        first unit when a feed train comes before it (see
        :func:`.flow.split_rows`).  Items joined only to inline items (a
        controller on an inline valve) go with the line carrying them.
        """
        eqs, conns = self.equipment, self.connections
        geos = {e.id: self.geometry(e) for e in eqs}
        plan = plan_flow(eqs, conns, geos, self._process_kinds())
        majors = {i for i, e in enumerate(eqs) if e.type == "column" or e.span > 1}
        edge = {i for i, e in enumerate(eqs) if geos[e.id].off_sheet}
        rows = split_rows(plan, majors, edge)
        if len(rows) < 2:
            return []
        row = {eqs[i].id: k for k, r in enumerate(rows) for i in r}
        hosts = self._inline_rows(row)
        for e in eqs:  # every line on an inline item: go with that line
            others = [
                o
                for c in conns
                for (a, _), (o, _) in (c.ends, c.ends[::-1])
                if a == e.id
            ]
            if others and all(o in hosts for o in others):
                row[e.id] = hosts[others[0]]
        out: list[set[str]] = [set() for _ in rows]
        for i, k in row.items():
            out[k].add(i)
        return out

    def _inline_rows(self, row: dict[str, int]) -> dict[str, int]:
        """Row of every inline item that has an id: the row of the end its
        line's items stand next to (the destination, or the source when the
        line runs out to an off-sheet flag)."""
        out: dict[str, int] = {}
        for c in self.connections:
            (s, _), (d, _) = c.ends
            if s not in row or d not in row:
                continue
            off = self.geometry(next(e for e in self.equipment if e.id == d)).off_sheet
            side = s if c.anchor == "src" or (c.anchor == "auto" and off) else d
            for item in c.inline:
                if item.id:
                    out[item.id] = row[side]
        return out

    def _build_rows(
        self, rows: list[set[str]], max_expand: int, compact: bool
    ) -> Diagram:
        """Build each row as a sheet of its own and stack them, top first.

        A line between two rows is cut into a pair of continuation flags: one
        on the right edge of its source's row (``to <id>``) and one on the
        left edge of its destination's row (``from <id>``); inline items stay
        with the destination.
        """
        row = {i: k for k, r in enumerate(rows) for i in r}
        row.update(self._inline_rows(row))
        by_id = {e.id: e for e in self.equipment}
        subs: list[Flowsheet] = []
        for r in rows:
            sub = Flowsheet(layout="flow")
            sub.library = self.library
            sub.line_styles = self.line_styles
            sub.bend_cost = self.bend_cost
            sub.rows = "single"
            sub.equipment = [replace(e) for e in self.equipment if e.id in r]
            subs.append(sub)
        for n, c in enumerate(self.connections):
            (s, sp), (d, dp) = c.ends
            a, b = row[s], row[d]
            if a == b:
                subs[a].connections.append(c)
                continue
            out_id, in_id = f"{GENERATED_PREFIX}RO{n}", f"{GENERATED_PREFIX}RI{n}"
            src_eq, dst_eq = by_id.get(s), by_id.get(d)
            subs[a].equipment.append(
                Equipment(
                    out_id,
                    "connector",
                    src_eq.level if src_eq else 0,
                    {"text": f"to {d}", "point": "right"},
                )
            )
            subs[b].equipment.append(
                Equipment(
                    in_id,
                    "connector",
                    dst_eq.level if dst_eq else 0,
                    {"text": f"from {s}", "point": "right"},
                )
            )
            subs[a].connections.append(Connection(c.src, f"{out_id}.tail", c.kind))
            subs[b].connections.append(replace(c, src=f"{in_id}.tip", recycle=False))
        diagrams = _build_sheets(subs, max_expand, compact, self.jobs)
        return _stack(diagrams, self.line_styles)

    def _build_swapped(self, max_expand: int, compact: bool) -> Diagram:
        """:meth:`_build` in every placement mode (see :meth:`_modes`), the
        cheapest kept, then the swappable ports re-sorted (see build)."""
        return _build_sheets([self], max_expand, compact, self.jobs)[0]

    def _modes(self) -> list[tuple[bool, bool]]:
        """Placement modes worth a build: (in order, flag room).

        Flow layout with branching units (an item with three or more branches
        -- a column's side draws): the sheet is built both packed freely and
        with every item kept in order (see :func:`.layout.place`); with a
        flag one cell under a bottom nozzle, both with and without moving it
        (see :func:`_settle_lifts`).  The cheapest one is kept."""
        orders = [False]
        if self.layout == "flow" and self._branchy():
            orders.append(True)
        rooms = [False]
        if self.layout == "flow":
            self._flag_room = True
            try:
                self._prepare()
            finally:
                self._flag_room = False
            if self._room_used:  # a flag would move: try both ways
                rooms.append(True)
        return [(o, r) for o in orders for r in rooms]

    def _first(
        self, mode: tuple[bool, bool], max_expand: int, compact: bool
    ) -> Diagram:
        """:meth:`_build` in one placement mode."""
        self._in_order, self._flag_room = mode
        return self._build(max_expand, compact)

    def _swap(
        self, mode: tuple[bool, bool], best: Diagram, max_expand: int, compact: bool
    ) -> Diagram:
        """Re-sort the lines on swappable ports and rebuild while it costs no
        more (see build), in the placement mode ``best`` was built in."""
        self._in_order, self._flag_room = mode
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
        # placed in order, except where they belong elsewhere: a main pipe's
        # taps (where the main pipe needs them) and the items on a recycle
        # (a reflux valve stands by the column it returns to)
        order = None
        if self._in_order:
            back = {
                item.id or f"{GENERATED_PREFIX}L{ci}_{k}"
                for ci, c in enumerate(self.connections)
                if c.recycle
                for k, item in enumerate(c.inline)
            }
            order = [
                not (e.id.startswith(TAP_PREFIX) or e.id in back) for e in net.equipment
            ]
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
            placed = place(items, gx, gy, links, overlap, flow, free, order)
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
            if best and (not weight or self._quick):
                break  # order layout: the tightest one; flow: the cheapest one
        if best is None:  # relaxed: the spacing with the fewest U-turns, then bends
            relaxed = True
            for k in range(max_expand):
                trial, fails = attempt(*spread(k), True)
                if trial and (best is None or cost(trial) < cost(best)):
                    best, (gx, gy) = trial, spread(k)
                failures = failures or fails
        # a crowded sheet (many side draws on one column) may need more room
        # than ``max_expand`` steps: keep widening until it routes at all
        k = max_expand
        while best is None and k < 4 * max_expand:
            trial, _ = attempt(*spread(k), True)
            if trial:
                best, (gx, gy) = trial, spread(k)
            k += 1
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
        # each gap: straight to its floor if that holds, else the smallest
        # squeeze that holds, found by bisection (a wide sheet would
        # otherwise be squeezed one cell per routing pass)
        for gaps, floor, groups in steps:
            for group in groups:
                ok, bad = 0, min(gaps[i] - floor[i] for i in group) + 1
                if bad <= 1:
                    continue
                start = [gaps[i] for i in group]
                step = bad - 1
                while bad - ok > 1:
                    for i, g in zip(group, start):
                        gaps[i] = g - step
                    trial, _ = attempt(gx, gy, relaxed, best)
                    if trial and cost(trial) <= cost(best):
                        best, ok = trial, step
                    else:
                        bad = step
                    step = (ok + bad) // 2
                for i, g in zip(group, start):
                    gaps[i] = g - ok
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
            eqs = self._with_levels()
            geos = {e.id: self.geometry(e) for e in eqs}
            first = plan_flow(eqs, self.connections, geos, self._process_kinds())
            pos = {eqs[i].id: k for k, i in enumerate(first.order)}
            # copies: heights are settled on them, never on the user's items
            net = self.expand(
                [replace(e) for e in sorted(eqs, key=lambda e: pos[e.id])]
            )
            flow, flags = self._apply_flow(net)
        links = _links(net)
        _settle_tees(net, links)
        if self.layout == "flow":
            self._room_used = _settle_lifts(net, flags, links, self._flag_room)
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
        return flow, self._flag_edges(net.equipment, net.connections, net.geos)

    def _flag_edges(
        self,
        eqs: list[Equipment],
        conns: list[Connection],
        geos: dict[str, Geometry],
    ) -> dict[str, str]:
        """Where each off-sheet flag goes (flow layout): on the edge its
        connecting port faces away from (``"left"`` / ``"right"``) -- or,
        when only utility lines reach it, right beside what it serves
        (``"local"``)."""
        faces: dict[str, str] = {}
        kinds: dict[str, set[str]] = {}
        for c in conns:
            for eq, port in c.ends:
                g = geos.get(eq)
                if g is not None and g.off_sheet and port in g.ports:
                    faces.setdefault(eq, g.ports[port].side)
                    kinds.setdefault(eq, set()).add(c.kind)
        flags: dict[str, str] = {}
        for e in eqs:
            if not geos[e.id].off_sheet:
                continue
            local = e.params.get("local", "auto")
            if local == "auto":
                used = kinds.get(e.id, set())
                local = bool(used) and not any(self.line_styles[k].main for k in used)
            if local:
                flags[e.id] = "local"
            else:
                flags[e.id] = "right" if faces.get(e.id) == "W" else "left"
        return flags

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
        if self.layout == "flow":
            # by the flow, not by the order the lines were written: from the
            # downstream end back (lines whose ends stand furthest right
            # first), so the lines out of a unit are laid before the supply
            # lines that have to find their way in around them
            pos = {e.id: i for i, e in enumerate(net.equipment)}

            def upstream(ci: int) -> tuple[int, int, int]:
                a, b = (pos[eq] for eq, _ in net.connections[ci].ends)
                return -min(a, b), -max(a, b), ci

            order.sort(key=upstream)
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
        router.reserve(
            [
                resolve_port(by_id[eq], port)
                for c in net.connections
                for eq, port in c.ends
            ]
        )
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
        fs = cls(library, d.get("layout", "flow"))  # JSON: from the flow
        if d.get("rows", "auto") not in ROW_MODES:
            raise ValueError(f"rows must be one of {ROW_MODES}, got {d['rows']!r}")
        fs.rows = d.get("rows", "auto")
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
            None if e.get("level") is None else int(e["level"]),
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
        d: dict[str, Any] = {"version": FORMAT_VERSION, "layout": self.layout}
        if self.rows != "auto":
            d["rows"] = self.rows
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
        items.append(Item(e.id, e.type, e.tier, g, e.elevation, edge, e.span))
    return items


def _band_count(eqs: list[Equipment]) -> int:
    """Level bands needed: the highest level, or the top of a tall item."""
    return max(e.tier + max(e.span, 1) for e in eqs)


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


def _settle_lifts(
    net: Expanded, flags: dict[str, str], links: list[Link], flag_room: bool = False
) -> bool:
    """Heights that a top or bottom nozzle needs (flow layout).

    On one level, a line between a side port (W/E) of ``p`` and a top / bottom
    nozzle of ``q`` is an L only if the side port is above the top nozzle or
    below the bottom one.  Above a top nozzle ``p`` is lifted; below a bottom
    nozzle the vessel ``q`` is lifted over ``p`` (``p`` cannot go below the
    floor).  Items with an elevation given by the user are never moved.
    Lifting one item can move what lines up with it, so repeat until stable.

    ``flag_room``: a flag one cell below a bottom nozzle on its own level
    gets the two cells an L needs: it moves down a cell, or the vessel up.
    Returns whether any flag stood so.
    """
    fixed = {e.id for e in net.equipment if e.elevation is not None}
    used = False
    eqs = net.equipment
    index = {e.id: i for i, e in enumerate(eqs)}
    # the taps of one main pipe are lifted together: the main pipe stays straight
    group = {t: [index[u] for u in h] for h in net.headers for t in h}
    taps = {index[t] for h in net.headers for t in h}
    # corresponding items of repeated units are lifted together: they stay alike
    sym = find_units(net)
    classes: dict[int, list[int]] = {}
    for i, k in enumerate(sym.item_class):
        classes.setdefault(k, []).append(i)
    alike = {i: classes[k] for i, k in enumerate(sym.item_class)}
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
                room = 1 if flag_room and items[p].geo.off_sheet else 0
                if q_side == "N" and port <= top + 1:
                    # the pipe turns down one cell above the nozzle: the port
                    # must be two cells up for an L -- a tall item (a column
                    # taking back a side stripper's vapour) is not lifted for
                    # it: the vessel stands lower instead
                    if items[p].span > 1 and eqs[q].id not in fixed:
                        j, y = q, port - 2 - items[q].geo.height
                    else:
                        j, y = p, top + 2 - p_off
                elif q_side == "S" and port >= ys[q] - room:
                    # a flag one cell under a bottom nozzle (a cooling water
                    # supply under its exchanger) needs a cell more: it moves
                    # down, or (on its floor) the exchanger moves up
                    low = ys[q] - 2 - p_off
                    near = room and port == ys[q] - 1
                    used |= bool(room)
                    if near and eqs[p].id not in fixed and low >= base[items[p].level]:
                        j, y = p, low
                    elif eqs[q].id not in fixed:
                        j, y = q, port + 1 + room
                    else:
                        j, y = p, -1
                else:
                    continue
                if eqs[j].id in fixed or y < base[items[j].level]:
                    continue
                elevation = y - base[items[j].level]
                # a main pipe's taps, or the copies of an item in repeated
                # units (columns side by side), rise together
                members = group.get(eqs[j].id) or [
                    k for k in alike.get(j, [j]) if eqs[k].id not in fixed
                ]
                if len(members) > 1:
                    elevation = max(
                        [elevation] + [eqs[k].elevation or 0 for k in members]
                    )
                for k in members:
                    moved |= _lift(eqs[k], elevation)
        moved |= _clear_main_pipes(eqs, items, ys, base, links, taps, fixed, alike)
        if not moved:
            return used
    return used


def _clear_main_pipes(
    eqs: list[Equipment],
    items: list[Item],
    ys: list[int],
    base: list[int],
    links: list[Link],
    taps: set[int],
    fixed: set[str],
    alike: dict[int, list[int]],
) -> bool:
    """Lift vessels that stand across a main pipe on their own level.

    A main pipe runs straight along its band from tap to tap (and on to the
    port it leaves); a vessel between two of its taps whose body reaches the
    pipe's height (a column on the floor beside the pumps whose outlets
    merge into one bottoms line) would make the pipe dip under it.  The
    vessel stands on the pipe instead: a cell above it, so its bottom
    nozzle drops through the pipe (a crossing) to whatever it feeds.  The
    copies of the vessel in repeated units are lifted alike.  True if
    anything moved.
    """
    moved = False
    for lk in links:
        if (lk.a_side, lk.b_side) != ("E", "W") or not ({lk.a, lk.b} & taps):
            continue
        level = items[lk.a].level
        if items[lk.b].level != level:
            continue
        tap = lk.a if lk.a in taps else lk.b
        pipe = ys[tap] + (lk.a_off if tap == lk.a else lk.b_off)
        for j in range(lk.a + 1, lk.b):
            it = items[j]
            if j in taps or it.level != level or not it.geo.nozzles:
                continue
            if not ys[j] <= pipe <= ys[j] + it.geo.height:
                continue
            elevation = pipe + 1 - base[level]
            for k in alike.get(j, [j]):
                if eqs[k].id not in fixed and (eqs[k].elevation or 0) < elevation:
                    moved |= _lift(eqs[k], elevation)
    return moved


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
            want = max(port_y - floor + (1 if t.side == "N" else -3), 0)
            moved |= _lift(net.equipment[index[t.tee]], want)
        if not moved:
            return
