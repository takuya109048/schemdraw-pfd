"""Revisions: changes drawn onto a finished sheet.

A sheet that has been built (laid out and routed) is changed by a list of
:class:`~.netlist.Revision` objects, one after another.  Each one works on
the sheet as the ones before it left it and never builds it anew, so what
was there keeps its look -- where every item stands, how every line runs,
where it bends and what it crosses:

* **removed** items and lines are taken off the sheet (and kept as ghosts,
  which the drawing can show dashed in the revision's colour).  A line goes
  with the items that sit on it (inline valves); a main pipe's tee whose
  lines are all gone goes too.  Removing an inline item that has a line on
  each side joins the line up again, straight through where it stood;
* **added** items are placed one by one in free space next to what they
  connect to, where their lines route best (fewest U-turns, then bends,
  crossings and length); their lines are routed around everything that is
  there.  A line onto a port already in use taps the line that leaves (or
  enters) it: a junction on that line, which itself does not move;
* where there is no room, the sheet is **moved apart**: everything beyond a
  cut moves along by the room needed, the lines across the cut stretch.
  Nothing changes shape, so the sheet still looks the same;
* the inline items of an added line are put on the line once it is routed,
  on a straight run near the end they belong to.

What a revision added is recorded per item and line (``Diagram.added``), so
the drawing can show each revision in its colour.
"""

from __future__ import annotations

import copy
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from .elements import ElementLibrary, Geometry
from .expand import GENERATED_PREFIX, TAP_PREFIX
from .geometry import Port
from .layout import Placed
from .netlist import (
    REVISION_COLORS,
    Change,
    Connection,
    Equipment,
    InlineItem,
    Revision,
    split_ref,
)
from .router import (
    DIRS,
    Route,
    Router,
    RoutingError,
    Terminal,
    _min_bends,
    _simplify,
    resolve_port,
)
from .style import LineStyle

JUNCTION_PREFIX = f"{GENERATED_PREFIX}J"  # where an added line taps a line
ROW_FLAG_PREFIXES = (f"{GENERATED_PREFIX}RO", f"{GENERATED_PREFIX}RI")
LINE_ITEM_PREFIX = f"{GENERATED_PREFIX}L"  # unnamed inline items
REACH = 10  # cells around a neighbour's port an added item may stand at
CANDIDATES = 8  # pairs of line ends tried when routing a line
SPOTS = 24  # placements of an added item that are routed and compared
TAPS = 16  # nearest cells of a line in use tried as a junction
ROOM_TRIES = 3  # times the sheet is moved apart for one item
BEND_COST = 6  # cells of line length one bend (or crossing) is worth
MARGIN = 24  # cells around the sheet added lines may run through

Node = tuple[int, int]
Point = tuple[float, float]


@dataclass(frozen=True)
class RevisionInfo:
    """One revision applied to a diagram, in order."""

    id: str
    color: str
    note: str = ""


@dataclass
class Ghost:
    """An item or a line a revision took away (drawn dashed if shown)."""

    rev: str
    placed: Placed | None = None
    line: tuple[Connection, Route] | None = None


def line_key(c: Connection) -> str:
    """How a line is named in ``Diagram.added`` (and in the drawing)."""
    return f"{c.src} -> {c.dst}"


def revision_color(rev: Revision, index: int) -> str:
    return rev.color or REVISION_COLORS[index % len(REVISION_COLORS)]


@dataclass
class Context:
    """What a revision needs from its flowsheet."""

    library: ElementLibrary
    geometry: Callable[[Equipment], Geometry]
    styles: dict[str, LineStyle]
    # lines as the user wrote them so far (to tell a shared port, and which
    # piece of a shared main pipe belongs to which line)
    lines: list[Connection] = field(default_factory=list)
    # named inline items (they go with their line): id -> (in, out) ports
    inline: dict[str, tuple[str, str]] = field(default_factory=dict)
    # every named item as written (type, parameters, ports, description)
    specs: dict[str, Equipment] = field(default_factory=dict)


def _int(v: float) -> int | None:
    return int(v) if float(v).is_integer() else None


def _lattice(points: list[Point]) -> list[Node]:
    """Grid cells along a polyline, in order (no repeats)."""
    out: list[Node] = []
    for p, q in zip(points, points[1:]):
        if p[1] == q[1]:
            y = _int(p[1])
            if y is None:
                continue
            a, b = p[0], q[0]
            step = 1 if b >= a else -1
            lo = int(-(-a // 1)) if step > 0 else int(a // 1)
            hi = int(b // 1) if step > 0 else int(-(-b // 1))
            cells = [(x, y) for x in range(lo, hi + step, step)]
        elif p[0] == q[0]:
            x = _int(p[0])
            if x is None:
                continue
            a, b = p[1], q[1]
            step = 1 if b >= a else -1
            lo = int(-(-a // 1)) if step > 0 else int(a // 1)
            hi = int(b // 1) if step > 0 else int(-(-b // 1))
            cells = [(x, y) for y in range(lo, hi + step, step)]
        else:
            continue
        for c in cells:
            if not out or out[-1] != c:
                out.append(c)
    return out


def _pipe_cells(points: list[Point]) -> dict[Node, int]:
    """Cells a line runs through, with the axes it uses there (bit 0
    vertical, bit 1 horizontal)."""
    out: dict[Node, int] = {}
    for p, q in zip(points, points[1:]):
        bit = 1 if p[0] == q[0] else 2
        for c in _lattice([p, q]):
            out[c] = out.get(c, 0) | bit
    return out


def _run_needed(c: Connection, geometry: Callable[[Equipment], Geometry]) -> int:
    """Straight cells a line needs for its inline items: each item, a cell
    between them and two at each end."""
    if not c.inline:
        return 0
    sizes = [
        geometry(Equipment(i.id or "?", i.type, 0, dict(i.params))).width
        for i in c.inline
    ]
    return sum(sizes) + len(sizes) - 1 + 6


def _length(points: list[Point]) -> float:
    return sum(abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in zip(points, points[1:]))


class _Sheet:
    """The diagram being changed."""

    def __init__(self, d: Any, ctx: Context, rev: str, index: int) -> None:
        self.ctx = ctx
        self.rev = rev
        self.index = index
        self.placed: dict[str, Placed] = {p.id: p for p in d.placed}
        self.lines: dict[int, tuple[Connection, Route]] = dict(enumerate(d.routes))
        self.next_line = len(self.lines)
        self.added: dict[str, str] = dict(d.added)
        self.ghosts: list[Ghost] = list(d.ghosts)
        self.hosts: dict[str, str] = dict(d.hosts)  # junction -> line it taps
        self.descs: dict[str, str] = dict(d.descs)
        self.rows: list[tuple[float, float]] = list(d.rows)
        self.warnings: list[str] = list(d.warnings)
        self.uturns: int = d.uturns
        self.serial = 0
        self.orphans: set[str] = set()  # flags whose lines were all removed
        # replacements (see _pair): added item -> the removed item whose
        # place it may take; removed items whose place is taken or open
        self.reuse: dict[str, Placed] = {}
        self.open: set[str] = set()
        # (revision, new, old) / (revision, item): see Diagram
        self.replaced: list[tuple[str, str, str]] = list(d.replaced)
        self.changed: list[tuple[str, str]] = list(d.changed)

    # -- bookkeeping ----------------------------------------------------------
    def new_id(self, prefix: str) -> str:
        while True:
            self.serial += 1
            i = f"{prefix}{self.index + 1}_{self.serial}"
            if i not in self.placed:
                return i

    def add_line(self, c: Connection, r: Route) -> int:
        k = self.next_line
        self.next_line += 1
        self.lines[k] = (c, r)
        self.added[line_key(c)] = self.rev
        return k

    def add_item(self, p: Placed, desc: str = "") -> None:
        self.placed[p.id] = p
        self.added[p.id] = self.rev
        if desc:
            self.descs[p.id] = desc

    def lines_at(self, item: str) -> list[int]:
        return [
            k for k, (c, _) in self.lines.items() if any(e == item for e, _ in c.ends)
        ]

    def bound(self, item: str) -> bool:
        """Does ``item`` belong to the line it sits on (an inline item, a
        continuation flag of a folded sheet)?"""
        return (
            item.startswith(LINE_ITEM_PREFIX)
            or item.startswith(ROW_FLAG_PREFIXES)
            or item in self.ctx.inline
        )

    # -- removing -------------------------------------------------------------
    def drop(self, items: Iterable[str] = (), lines: Iterable[int] = ()) -> None:
        """Take items and lines off the sheet, and what goes with them."""
        todo: deque[tuple[str, Any]] = deque()
        todo.extend(("item", i) for i in items)
        todo.extend(("line", k) for k in lines)
        while todo:
            what, x = todo.popleft()
            if what == "item":
                p = self.placed.pop(x, None)
                if p is None:
                    continue
                self.descs.pop(x, None)
                self.hosts.pop(x, None)
                if not x.startswith(JUNCTION_PREFIX):
                    self.ghosts.append(Ghost(self.rev, placed=p))
                todo.extend(("line", k) for k in self.lines_at(x))
                for prefix, other in zip(ROW_FLAG_PREFIXES, ROW_FLAG_PREFIXES[::-1]):
                    if x.startswith(prefix):  # the flag's partner on another row
                        todo.append(("item", other + x[len(prefix) :]))
                continue
            entry = self.lines.pop(x, None)
            if entry is None:
                continue
            c, r = entry
            self.ghosts.append(Ghost(self.rev, line=(c, r)))
            key = line_key(c)
            todo.extend(("item", j) for j, h in self.hosts.items() if h == key)
            for e, _ in c.ends:
                if e not in self.placed:
                    continue
                left = self.lines_at(e)
                if self.bound(e):
                    todo.append(("item", e))
                elif e.startswith(TAP_PREFIX):
                    if len(left) <= 1:
                        todo.append(("item", e))
                    else:
                        self.trim_tee(e, left)
                elif e.startswith(JUNCTION_PREFIX) and not left:
                    todo.append(("item", e))
                elif self.placed[e].geo.off_sheet and not left:
                    self.orphans.add(e)  # a flag to nothing (if it stays so)

    def trim_tee(self, tee: str, left: list[int]) -> None:
        """Draw only the arms of a tee that still carry a line."""
        p = self.placed[tee]
        arms = "".join(
            port
            for k in left
            for e, port in self.lines[k][0].ends
            if e == tee and port in "nsew"
        )
        gone = "".join(a for a in p.geo.ports if a not in arms)
        if arms and gone:
            geo = self.ctx.library.build("tee", {"arms": arms})
            self.placed[tee] = replace(p, geo=geo)
            arm = self.ctx.library.build("tee", {"arms": gone})
            self.ghosts.append(Ghost(self.rev, placed=replace(p, geo=arm)))

    def unplug(self, item: str) -> bool:
        """Remove an inline item and join its line up again through where it
        stood (other lines to it, a control valve's signal, go).  False
        (nothing done) if it does not sit on one straight line."""
        pin, pout = self.ctx.inline[item]
        ks = self.lines_at(item)
        into = [k for k in ks if self.lines[k][0].ends[1] == (item, pin)]
        out = [k for k in ks if self.lines[k][0].ends[0] == (item, pout)]
        if len(into) != 1 or len(out) != 1:
            return False
        (ca, ra), (cb, rb) = self.lines[into[0]], self.lines[out[0]]
        if ca.kind != cb.kind:
            return False
        a, b = ra.points[:-1], rb.points[1:]
        if not a or not b or (a[-1][0] != b[0][0] and a[-1][1] != b[0][1]):
            return False
        others = [k for k in ks if k not in (into[0], out[0])]
        joined = Connection(ca.src, cb.dst, ca.kind)
        route = Route(_simplify(a + b), ra.strict and rb.strict, [])
        p = self.placed.pop(item)
        self.descs.pop(item, None)
        self.ghosts.append(Ghost(self.rev, placed=p))
        for k in into + out:
            del self.lines[k]
        rev = self.added.get(line_key(ca))
        k = self.next_line
        self.next_line += 1
        self.lines[k] = (joined, route)
        if rev is not None:
            self.added[line_key(joined)] = rev
        for j, h in self.hosts.items():
            if h in (line_key(ca), line_key(cb)):
                self.hosts[j] = line_key(joined)
        self.drop(lines=others)
        return True

    # -- the lines as a graph -------------------------------------------------
    def same_port(self, item: str, a: str, b: str) -> bool:
        if a == b:
            return True
        p = self.placed.get(item)
        return p is not None and any(a in g and b in g for g in p.geo.swappable)

    def path(self, src: str, dst: str, without: frozenset[int] = frozenset()):
        """The lines on the sheet that carry the written line ``src -> dst``
        (through inline items, tees, junctions and row flags), or None."""
        (s, sp), (d, dp) = split_ref(src), split_ref(dst)
        through = {
            i
            for i in self.placed
            if self.bound(i) or i.startswith((TAP_PREFIX, JUNCTION_PREFIX))
        }
        # edges: item -> [(other item, line or None, this end's port)]
        adj: dict[str, list[tuple[str, int | None, str]]] = {}

        def link(a: str, pa: str, b: str, pb: str, k: int | None) -> None:
            adj.setdefault(a, []).append((b, k, pa))
            adj.setdefault(b, []).append((a, k, pb))

        keys: dict[str, int] = {}
        for k, (c, _) in self.lines.items():
            keys[line_key(c)] = k
            if k in without:
                continue
            (a, pa), (b, pb) = c.ends
            link(a, pa, b, pb, k)
        for j, h in self.hosts.items():
            k = keys.get(h)
            if k is None or k in without or j not in self.placed:
                continue
            (a, pa), (b, pb) = self.lines[k][0].ends
            link(a, pa, j, "j", None)
            link(j, "j", b, pb, None)
        for i in self.placed:
            if i.startswith(ROW_FLAG_PREFIXES[0]):
                other = ROW_FLAG_PREFIXES[1] + i[len(ROW_FLAG_PREFIXES[0]) :]
                if other in self.placed:
                    link(i, "", other, "", None)
        start = [(o, k) for o, k, port in adj.get(s, []) if self.same_port(s, port, sp)]
        prev: dict[str, tuple[str, int | None]] = {}
        queue = deque()
        for o, k in start:
            if o == d:
                ends = [port for oo, kk, port in adj.get(d, []) if oo == s and kk == k]
                if any(self.same_port(d, port, dp) for port in ends):
                    return [k] if k is not None else []
            if o in through and o not in prev:
                prev[o] = (s, k)
                queue.append(o)
        while queue:
            n = queue.popleft()
            for o, k, _ in adj.get(n, []):
                if o == d:
                    ends = [port for oo, kk, port in adj[d] if oo == n and kk == k]
                    if any(self.same_port(d, port, dp) for port in ends):
                        out = [] if k is None else [k]
                        while n != s:
                            n, kk = prev[n]
                            if kk is not None:
                                out.append(kk)
                        return out[::-1]
                if o in through and o not in prev and o != s:
                    prev[o] = (n, k)
                    queue.append(o)
        return None

    def remove_line(self, src: str, dst: str) -> None:
        path = self.path(src, dst)
        if path is None:
            raise KeyError(f"remove: no line {src} -> {dst} on the sheet")
        others = [
            c
            for c in self.ctx.lines
            if (c.src, c.dst) != (src, dst) and self.path(c.src, c.dst) is not None
        ]
        # the piece of the line no other line needs (not a shared main pipe)
        for k in path[::-1]:
            gone = frozenset([k])
            if all(self.path(c.src, c.dst, gone) is not None for c in others):
                self.drop(lines=[k])
                return
        self.drop(lines=path[-1:])

    # -- moving apart ---------------------------------------------------------
    def cut(self, axis: int, at: int, by: int) -> int:
        """Where :meth:`make_room` really cuts: moved on past every item
        the cut would go through."""
        boxes = [p.bbox for p in self.placed.values()]
        boxes += [g.placed.bbox for g in self.ghosts if g.placed]
        moved = True
        while moved:  # never cut through an item
            moved = False
            for b in boxes:
                lo, hi = b[axis], b[axis + 2]
                if by > 0 and lo < at <= hi:
                    at, moved = hi + 1, True
                elif by < 0 and lo <= at < hi:
                    at, moved = lo - 1, True
        return at

    def snapshot(self) -> tuple:
        return (
            dict(self.placed),
            dict(self.lines),
            list(self.ghosts),
            list(self.rows),
            dict(self.added),
            dict(self.descs),
            dict(self.hosts),
            self.next_line,
            list(self.warnings),
            self.uturns,
            set(self.open),
            list(self.replaced),
            list(self.changed),
        )

    def restore(self, state: tuple) -> None:
        (
            self.placed,
            self.lines,
            self.ghosts,
            self.rows,
            self.added,
            self.descs,
            self.hosts,
            self.next_line,
            self.warnings,
            self.uturns,
            self.open,
            self.replaced,
            self.changed,
        ) = (copy.copy(x) for x in state)

    def make_room(self, axis: int, at: int, by: int) -> None:
        """Move everything at or beyond ``at`` (along ``axis``; ``by`` < 0:
        at or before it) by ``by`` cells; lines across the cut stretch."""
        at = self.cut(axis, at, by)

        def mv(v: float) -> float:
            return v + by if (v >= at if by > 0 else v <= at) else v

        def pt(p: Point) -> Point:
            return (mv(p[0]), p[1]) if axis == 0 else (p[0], mv(p[1]))

        def node(n: Node) -> Node:
            return (int(mv(n[0])), n[1]) if axis == 0 else (n[0], int(mv(n[1])))

        def item(p: Placed) -> Placed:
            if axis == 0:
                return replace(p, x=int(mv(p.x)))
            return replace(p, y=int(mv(p.y)))

        def route(r: Route) -> Route:
            return Route(
                [pt(q) for q in r.points], r.strict, [node(n) for n in r.nodes]
            )

        self.placed = {i: item(p) for i, p in self.placed.items()}
        self.lines = {k: (c, route(r)) for k, (c, r) in self.lines.items()}
        self.ghosts = [
            Ghost(
                g.rev,
                None if g.placed is None else item(g.placed),
                None if g.line is None else (g.line[0], route(g.line[1])),
            )
            for g in self.ghosts
        ]
        if axis == 1:
            self.rows = [(mv(lo), mv(hi)) for lo, hi in self.rows]

    # -- routing --------------------------------------------------------------
    def board(self, margin: int = MARGIN, also: str | None = None) -> Router:
        """A router over the sheet with every line laid."""
        placed = list(self.placed.values())
        boxes = [p.bbox for p in placed]
        x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
        x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
        over = 0
        for _, r in self.lines.values():
            for x, y in r.points:
                over = max(over, x0 - x, x - x1, y0 - y, y - y1)
        router = Router(placed, margin + int(over) + 1)
        for _, r in self.lines.values():
            self.lay(router, r.points)
        for g in self.cleared(also):  # items this revision takes away stay
            self.block(router, g.bbox)  # clear (lines may run where one ran)
        return router

    def cleared(self, also: str | None = None) -> list[Placed]:
        """Items this revision removed whose place stays clear: all but
        those an added item replaces in place (``also``: one more)."""
        return [
            g.placed
            for g in self.ghosts
            if g.rev == self.rev
            and g.placed is not None
            and g.placed.id not in self.open
            and g.placed.id != also
        ]

    @staticmethod
    def lay(router: Router, points: list[Point]) -> None:
        h, n = router.h, len(router.axes)
        for c, bits in _pipe_cells(points).items():
            i = (c[0] - router.x0) * h + c[1] - router.y0
            if 0 <= i < n:
                router.axes[i] |= bits

    @staticmethod
    def block(router: Router, box: tuple[int, int, int, int]) -> bool:
        """Block a box on ``router``; False if it leaves the grid."""
        h, w = router.h, len(router.blocked) // router.h
        x0, y0, x1, y1 = box
        if x0 - router.x0 < 1 or x1 - router.x0 > w - 2:
            return False
        if y0 - router.y0 < 1 or y1 - router.y0 > h - 2:
            return False
        for x in range(x0, x1 + 1):
            i = (x - router.x0) * h - router.y0
            router.blocked[i + y0 : i + y1 + 1] = b"\1" * (y1 - y0 + 1)
        return True

    @staticmethod
    def clone(router: Router) -> Router:
        out = copy.copy(router)
        out.blocked = bytearray(router.blocked)
        out.axes = bytearray(router.axes)
        out.door = bytearray(router.door)
        return out

    def used(self, item: str, port: str) -> list[int]:
        return [
            k
            for k, (c, _) in self.lines.items()
            if any(e == item and p == port for e, p in c.ends)
        ]

    def port_for(self, item: str, port: str) -> str:
        """The port a new line onto ``item.port`` takes: the port itself,
        or -- when the first build gave its line to another nozzle of the
        same group (see ``Flowsheet._reassign``) and the user's lines leave it
        free -- the nozzle of the group left free."""
        p = self.placed[item]
        if port not in p.geo.ports:
            raise KeyError(
                f"{item}: unknown port {port!r}; known: {sorted(p.geo.ports)}"
            )
        if not self.used(item, port):
            return port
        written = {(e, q) for c in self.ctx.lines for e, q in c.ends if e == item}
        if (item, port) in written:
            return port
        for g in p.geo.swappable:
            if port in g:
                for q in g:
                    if not self.used(item, q) and (item, q) not in written:
                        return q
        return port

    def terminals(
        self, router: Router, item: str, port: str
    ) -> list[tuple[Terminal, str | None]]:
        """Where a new line may meet ``item.port``: the port itself if no
        line uses it, else junctions on the lines leaving it (with the line
        each taps), nearest first."""
        p = self.placed[item]
        port = self.port_for(item, port)
        own = resolve_port(p, port)
        if not self.used(item, port):
            return [(own, None)]
        # the lines out of the port, on through a main pipe's tees -- and,
        # dearer, on past the inline items on them (when the piece next to
        # the port is too short to tap)
        keys: dict[int, bool] = {}  # line -> past an inline item
        seen = {item}
        frontier = [(item, port, False)]
        while frontier:
            e, q, far = frontier.pop(0)
            for k in self.used(e, q) if q else self.lines_at(e):
                if k in keys:
                    continue
                keys[k] = far
                for o, _ in self.lines[k][0].ends:
                    if o in seen:
                        continue
                    if o.startswith(TAP_PREFIX):
                        seen.add(o)
                        frontier.append((o, "", far))
                    elif self.bound(o) and not o.startswith(ROW_FLAG_PREFIXES):
                        seen.add(o)
                        frontier.append((o, "", True))
        h = router.h
        n = len(router.axes)
        doors = {i for i in range(n) if router.door[i]}
        # not right in front of another item (a tee before its nozzle)
        near: set[Node] = set()
        for o, q in self.placed.items():
            if o == item or o in seen:
                continue
            x0, y0, x1, y1 = q.bbox
            near.update(
                (x, y) for x in range(x0 - 1, x1 + 2) for y in range(y0 - 1, y1 + 2)
            )
        out: list[tuple[float, Terminal, str]] = []
        for k in keys:
            c, r = self.lines[k]
            cells = _lattice(r.points)
            corners = {(int(x), int(y)) for x, y in r.points if _int(x) is not None}
            for j, cell in enumerate(cells[1:-1], start=1):
                if cell in corners or cell in near:
                    continue
                i = (cell[0] - router.x0) * h + cell[1] - router.y0
                if not 0 <= i < n or router.blocked[i] or router.axes[i] == 3:
                    continue
                if i in doors:
                    continue
                prev, nxt = cells[j - 1], cells[j + 1]
                vertical = prev[0] == nxt[0]
                for d in (1, 3) if vertical else (0, 2):
                    dx, dy = DIRS[d]
                    f = (cell[0] + dx, cell[1] + dy)
                    fi = (f[0] - router.x0) * h + f[1] - router.y0
                    if not 0 <= fi < n:
                        continue
                    if router.blocked[fi] or router.axes[fi] or router.door[fi]:
                        continue
                    t = Terminal((float(cell[0]), float(cell[1])), cell, f, d)
                    dist = abs(cell[0] - own.point[0]) + abs(cell[1] - own.point[1])
                    out.append((dist + 1000 * keys[k], t, line_key(c)))
        out.sort(key=lambda e: e[0])
        return [(t, key) for _, t, key in out[:TAPS]]

    def route(
        self, router: Router, c: Connection
    ) -> tuple[Route, Terminal, Terminal, list[str | None], int] | None:
        """Route ``c`` (its ends placed) on ``router`` and lay it there; with
        its terminals, the lines its ends tap and the crossings it makes."""
        (s, sp), (d, dp) = c.ends
        srcs = self.terminals(router, s, sp)
        dsts = self.terminals(router, d, dp)
        for t, host in srcs + dsts:
            if host is None:  # a port: keep its doorstep for its own line
                router.reserve([t])

        def guess(a: Terminal, b: Terminal) -> int:
            return abs(a.first[0] - b.first[0]) + abs(a.first[1] - b.first[1])

        pairs = sorted(
            ((a, b) for a in srcs for b in dsts),
            key=lambda ab: guess(ab[0][0], ab[1][0]),
        )[:CANDIDATES]
        best = None
        for (a, ha), (b, hb) in pairs:
            r = router.route(a, b, True, commit=False)
            if r is None:
                continue
            crossings = sum(
                1
                for x, y in r.nodes
                if router.axes[(x - router.x0) * router.h + y - router.y0]
            )
            cost = (r.bends + crossings) * BEND_COST + _length(r.points)
            key = (not r.strict, cost)
            if best is None or key < best[0]:
                best = (key, r, a, b, [ha, hb], crossings)
        if best is None:
            return None
        _, r, a, b, hosts, crossings = best
        router._commit(r.nodes)
        return r, a, b, hosts, crossings

    def commit(
        self,
        router: Router,
        c: Connection,
        found: tuple[Route, Terminal, Terminal, list[str | None], int],
    ) -> tuple[Connection, int]:
        """Put a routed line on the sheet (junctions where it taps lines)."""
        r, a, b, hosts, _ = found
        ends = list(c.ends)
        for end, (t, host) in enumerate(zip((a, b), hosts)):
            if host is None:
                item, port = ends[end]
                ends[end] = (item, self.port_for(item, port))
                continue
            j = self.new_id(JUNCTION_PREFIX)
            geo = Geometry(0, 0, [], {"j": Port("N", 0)}, label_pos="none")
            self.add_item(Placed(j, "junction", 0, geo, *t.edge))
            self.hosts[j] = host
            self.block(router, (*t.edge, *t.edge))
            ends[end] = (j, "j")
        flat = replace(
            c,
            src=f"{ends[0][0]}.{ends[0][1]}",
            dst=f"{ends[1][0]}.{ends[1][1]}",
            inline=[],
        )
        if not r.strict:
            self.uturns += 1
        return flat, self.add_line(flat, r)

    # -- inline items ---------------------------------------------------------
    def inline_plan(
        self,
        user: Connection,
        r: Route,
        ends: tuple[Placed | None, Placed | None],
        skip: int | None = None,
        extra: Iterable[tuple[list[Point], tuple[int, int, int, int] | None]] = (),
    ) -> tuple[list[Node], list[tuple[Placed, int, int]]] | None:
        """Where the inline items of ``user`` go on its route ``r`` (``ends``:
        the items it runs between, None for a junction; ``skip``: the line
        itself, if on the sheet): on one straight run, next to the end they
        belong to.  ``extra``: lines (and items) about to be laid as well.
        The cells of the route and per item (placed, first cell, last
        cell); None if they fit nowhere."""
        cells = _lattice(r.points)
        sp, dp = ends

        def inside(cell: Node, p: Placed | None) -> bool:
            if p is None:
                return False
            x0, y0, x1, y1 = p.bbox
            return x0 <= cell[0] <= x1 and y0 <= cell[1] <= y1

        while len(cells) > 1 and inside(cells[0], sp) and inside(cells[1], sp):
            cells.pop(0)
        while len(cells) > 1 and inside(cells[-1], dp) and inside(cells[-2], dp):
            cells.pop()
        if len(cells) < 4:
            return None
        own = set(cells)
        others: dict[Node, int] = {}
        for kk, (_, rr) in self.lines.items():
            if kk != skip:
                for cell, bits in _pipe_cells(rr.points).items():
                    others[cell] = others.get(cell, 0) | bits
        boxes = [
            p.bbox for i, p in self.placed.items() if not i.startswith(JUNCTION_PREFIX)
        ]
        for pts, box in extra:
            if pts:
                for cell, bits in _pipe_cells(pts).items():
                    others[cell] = others.get(cell, 0) | bits
            if box is not None:
                boxes.append(box)
        # flow direction at every cell (the move into it)
        dirs = [0] + [
            DIRS.index((b[0] - a[0], b[1] - a[1])) for a, b in zip(cells, cells[1:])
        ]
        anchor = user.anchor
        if anchor == "auto":
            flag = bool(dp and dp.geo.off_sheet) and not (sp and sp.geo.off_sheet)
            anchor = "src" if flag else "dst"

        def oriented(item: InlineItem, way: int) -> Geometry | None:
            """The item turned so the line (moving ``way``) runs through it
            from its in-port to its out-port; None if it does not."""
            params = dict(item.params)
            if "rotation" not in params and "mirror" not in params:
                params.update(
                    ({"rotation": 90}, {}, {"rotation": 270}, {"mirror": True})[way]
                )
            geo = self.ctx.geometry(Equipment(item.id or "?", item.type, 0, params))
            pi, po = geo.ports.get(item.port_in), geo.ports.get(item.port_out)
            if pi is None or po is None:
                raise KeyError(
                    f"inline {item.type!r} has no port {item.port_in!r} / "
                    f"{item.port_out!r}; known: {sorted(geo.ports)} (set 'in' / 'out')"
                )
            if pi.side != "SWNE"[way] or po.side != "NESW"[way]:
                return None
            return geo if pi.offset == po.offset else None

        def fits(at: int, items: list[InlineItem]) -> list[tuple] | None:
            """The items one after another, the first one's in-port at
            cell ``at``, all on one straight run (a cell of it before,
            between and after them)."""
            out = []
            for item in items:
                if at < 2 or at >= len(cells) - 2:
                    return None
                way = dirs[at]
                geo = oriented(item, way)
                if geo is None:
                    return None
                tmp = Placed("?", item.type, 0, geo, 0, 0)
                ti = resolve_port(tmp, item.port_in)
                to = resolve_port(tmp, item.port_out)
                end = at + abs(to.edge[0] - ti.edge[0]) + abs(to.edge[1] - ti.edge[1])
                if end + 1 > len(cells) - 2:
                    return None
                if any(dirs[j] != way for j in range(at - 1, end + 2)):
                    return None
                p = Placed("?", item.type, 0, geo, *(
                    cells[at][0] - ti.edge[0], cells[at][1] - ti.edge[1]
                ))  # fmt: skip
                if resolve_port(p, item.port_out).edge != cells[end]:
                    return None
                bx0, by0, bx1, by1 = p.bbox
                mine = set(cells[at : end + 1])
                for cx in range(bx0, bx1 + 1):
                    for cy in range(by0, by1 + 1):
                        cell = (cx, cy)
                        if cell in others or (cell in own and cell not in mine):
                            return None
                for b in boxes:
                    if (
                        b[0] <= bx1 + 1
                        and b[2] >= bx0 - 1
                        and b[1] <= by1 + 1
                        and b[3] >= by0 - 1
                    ):
                        return None
                out.append((p, at, end))
                at = end + 2
            return out

        items = user.inline
        trials = []
        for at in range(2, len(cells) - 2):
            trial = fits(at, items)
            if not trial:
                continue
            first, last = trial[0][1], trial[-1][2]
            way = dirs[first]
            # a straight lead of a cell more than needed reads better
            lead = first - 2 >= 1 and dirs[first - 2] == way
            tail = last + 2 <= len(cells) - 1 and dirs[last + 2] == way
            near = len(cells) - 1 - last if anchor == "dst" else first
            trials.append(((not (lead and tail), near), trial))
        if not trials:
            return None
        return cells, min(trials, key=lambda t: t[0])[1]

    def put_inline(self, user: Connection, k: int, ids: list[str]) -> bool:
        """Put the inline items of ``user`` on its routed line ``k`` (see
        :meth:`inline_plan`).  False if they fit nowhere."""
        c, r = self.lines[k]
        (s, _), (d, _) = c.ends
        plan = self.inline_plan(user, r, (self.placed[s], self.placed[d]), k)
        if plan is None:
            return False
        cells, found = plan
        items = user.inline
        # cut the line at the items
        del self.lines[k]
        self.added.pop(line_key(c), None)
        refs = [c.src]
        placed_items = []
        for item, iid, (p, a, b) in zip(items, ids, found):
            p = replace(p, id=iid)
            self.add_item(p, item.desc)
            placed_items.append((p, item, a, b))
            refs += [f"{iid}.{item.port_in}", f"{iid}.{item.port_out}"]
        refs.append(c.dst)
        # where each piece starts and ends: (cell, point on the outline)
        cuts: list[tuple[int, Point]] = [(0, r.points[0])]
        for p, item, a, b in placed_items:
            cuts.append((a, resolve_port(p, item.port_in).point))
            cuts.append((b, resolve_port(p, item.port_out).point))
        cuts.append((len(cells) - 1, r.points[-1]))
        for j in range(0, len(refs), 2):
            (a, pa), (b, pb) = cuts[j], cuts[j + 1]
            pts: list[Point] = [pa]
            pts += [(float(x), float(y)) for x, y in cells[a : b + 1]]
            pts.append(pb)
            piece = Route(_simplify(pts), r.strict, list(cells[a + 1 : b]))
            self.add_line(Connection(refs[j], refs[j + 1], c.kind), piece)
        return True

    # -- placing --------------------------------------------------------------
    def free_cells(self, also: str | None = None) -> tuple[set[Node], set[Node]]:
        """Cells an added item may not cover: around items (a cell's gap
        kept), and on lines -- and on what this revision removed, except
        where an added item replaces a removed one (see :meth:`cleared`)."""
        near: set[Node] = set()
        kept = self.cleared(also)
        boxes = [p.bbox for p in self.placed.values()] + [g.bbox for g in kept]
        for x0, y0, x1, y1 in boxes:
            for x in range(x0 - 1, x1 + 2):
                for y in range(y0 - 1, y1 + 2):
                    near.add((x, y))
        pipes: set[Node] = set()
        for _, r in self.lines.values():
            pipes.update(_lattice(r.points))
        freed = {
            g.placed.id
            for g in self.ghosts
            if g.rev == self.rev and g.placed is not None
        } - {g.id for g in kept}
        for g in self.ghosts:
            if g.rev == self.rev and g.line is not None:
                if not any(e in freed for e, _ in g.line[0].ends):
                    pipes.update(_lattice(g.line[1].points))
        return near, pipes

    # -- several lines at once -------------------------------------------------
    def try_lines(self, router: Router, lines: list[Connection]) -> tuple[tuple, list]:
        """Route ``lines`` one after another on ``router`` (their inline
        items planned on them, see :meth:`inline_plan`), without changing the
        sheet.  Returns how good that is -- (lines that fail, lines whose
        items find no room, U-turns, cost) -- and per routed line what
        :meth:`lay` needs."""
        fails, short, turns, cost = 0, 0, 0, 0.0
        extra: list[tuple[list[Point], tuple[int, int, int, int] | None]] = []
        done = []
        for c in lines:
            found = self.route(router, c)
            if found is None:
                fails += 1
                continue
            r, _, _, hosts, crossings = found
            if c.inline:
                (s, _), (d, _) = c.ends
                ends = (
                    self.placed[s] if hosts[0] is None else None,
                    self.placed[d] if hosts[1] is None else None,
                )
                plan = self.inline_plan(c, r, ends, None, extra)
                if plan is None:
                    short += 1
                else:
                    for p, _, _ in plan[1]:
                        self.block(router, p.bbox)
                        extra.append(([], p.bbox))
            extra.append((r.points, None))
            turns += not r.strict
            cost += (r.bends + crossings) * BEND_COST + _length(r.points)
            done.append((c, found))
        return (fails, short, turns, cost), done


def apply_revision(d: Any, rev: Revision, index: int, ctx: Context) -> Any:
    """``d`` with ``rev`` drawn onto it (see the module documentation)."""
    sheet = _Sheet(d, ctx, rev.id, index)
    # -- what goes ------------------------------------------------------------
    for item in rev.remove:
        if item not in sheet.placed:
            raise KeyError(f"remove: no item {item!r} on the sheet")
        if not (item in ctx.inline and sheet.unplug(item)):
            sheet.drop(items=[item])
    for src, dst in rev.remove_lines:
        sheet.remove_line(src, dst)
    _forget(rev, ctx)

    # -- what changes ---------------------------------------------------------
    again: list[Equipment] = []  # changed items put anew, with their lines
    relines: list[Connection] = []
    for ch in rev.changes:
        moved = _change(sheet, ch, ctx)
        if moved is not None:
            again.append(moved[0])
            relines += [c for c in moved[1] if c not in relines]

    # -- what comes -----------------------------------------------------------
    for e in rev.equipment:
        if e.id in sheet.placed:
            raise ValueError(f"duplicate id {e.id!r}: it is on the sheet")
    adding = list(rev.equipment) + again
    new = {e.id: e for e in adding}
    for c in rev.connections:
        for i in c.inline:
            if i.id and i.id in sheet.placed:
                raise ValueError(f"duplicate id {i.id!r}: it is on the sheet")
    _learn(rev, ctx)  # its items and lines (routed below)
    _pair(sheet, rev, ctx, adding)
    pending = list(rev.connections) + relines
    inline_ids: dict[int, list[str]] = {
        id(c): [
            item.id or f"{LINE_ITEM_PREFIX}R{index + 1}_{n}_{k}"
            for k, item in enumerate(c.inline)
        ]
        for n, c in enumerate(rev.connections + relines)
    }
    known = set(new) | {i for ids in inline_ids.values() for i in ids}
    for c in pending:
        for e, _ in c.ends:
            if e not in sheet.placed and e not in known:
                raise KeyError(f"{c.src} -> {c.dst}: unknown equipment {e!r}")

    def lay(router: Router, done: list) -> None:
        """Put routed lines (see :meth:`_Sheet.try_lines`) on the sheet."""
        for c, found in done:
            pending.remove(c)
            _, k = sheet.commit(router, c, found)
            if c.inline:
                put(c, k)

    def put(c: Connection, k: int) -> None:
        if sheet.put_inline(c, k, inline_ids[id(c)]):
            return
        # stretch the line where it runs straight longest, then try again
        _, r = sheet.lines[k]
        pts = r.points
        runs = sorted(
            ((abs(b[0] - a[0]) + abs(b[1] - a[1]), a, b) for a, b in zip(pts, pts[1:])),
            key=lambda e: -e[0],
        )
        need = _run_needed(c, ctx.geometry)
        for size, a, b in runs:
            axis = 0 if a[1] == b[1] else 1
            lo, hi = sorted((int(a[axis]), int(b[axis])))
            by = max(need - int(size), 2)
            # a cut inside the run that goes through no item
            at = next(
                (v for v in range(lo + 1, hi + 1) if sheet.cut(axis, v, by) == v),
                None,
            )
            if at is None:
                continue
            state = sheet.snapshot()
            sheet.make_room(axis, at, by)
            if sheet.put_inline(c, k, inline_ids[id(c)]):
                return
            sheet.restore(state)

        sheet.warnings.append(
            f"{rev.id}: no room on {c.src} -> {c.dst} for its inline items"
        )

    def route_ready() -> None:
        """Route every pending line whose ends are both on the sheet."""
        while True:
            ready = [c for c in pending if all(e in sheet.placed for e, _ in c.ends)]
            if not ready:
                return
            c = ready[0]
            router = sheet.board()
            (fails, _, _, _), done = sheet.try_lines(router, [c])
            if fails:
                split(c)
            else:
                lay(router, done)

    def split(c: Connection) -> None:
        """A line that routes nowhere (to another row of a folded sheet):
        a pair of continuation flags instead, one by each end."""
        if any(e.startswith(ROW_FLAG_PREFIXES) for e, _ in c.ends):
            raise RoutingError(f"{rev.id}: could not route {c.src} -> {c.dst}")
        tag = sheet.new_id("")
        out, back = ROW_FLAG_PREFIXES[0] + tag, ROW_FLAG_PREFIXES[1] + tag
        (s, _), (d, _) = c.ends
        flags = [
            Equipment(out, "connector", 0, {"text": f"to {d}", "point": "right"}),
            Equipment(back, "connector", 0, {"text": f"from {s}", "point": "right"}),
        ]
        halves = [
            Connection(c.src, f"{out}.tail", c.kind),
            replace(c, src=f"{back}.tip", recycle=False),
        ]
        pending.remove(c)
        pending.extend(halves)
        inline_ids[id(halves[1])] = inline_ids[id(c)]
        for flag, half in zip(flags, halves):
            router, done = _place(sheet, flag, ctx.geometry(flag), [half], rev.id)
            lay(router, done)

    route_ready()
    todo = [e.id for e in adding]
    while todo:

        def links(i: str) -> list[Connection]:
            return [
                c
                for c in pending
                if any(e == i for e, _ in c.ends)
                and all(e == i or e in sheet.placed for e, _ in c.ends)
            ]

        pick = max(todo, key=lambda i: (len(links(i)), -todo.index(i)))
        todo.remove(pick)
        eq = new[pick]
        router, done = _place(sheet, eq, ctx.geometry(eq), links(pick), rev.id)
        lay(router, done)
        route_ready()
    if pending:
        c = pending[0]
        raise RoutingError(f"{rev.id}: could not route {c.src} -> {c.dst}")
    # flags left with no line at all go too
    sheet.drop(items=[i for i in sorted(sheet.orphans) if not sheet.lines_at(i)])

    info = list(d.revisions) + [
        RevisionInfo(rev.id, revision_color(rev, index), rev.note)
    ]
    return replace(
        d,
        placed=list(sheet.placed.values()),
        routes=list(sheet.lines.values()),
        warnings=sheet.warnings,
        descs=sheet.descs,
        uturns=sheet.uturns,
        rows=sheet.rows,
        added=sheet.added,
        ghosts=sheet.ghosts,
        hosts=sheet.hosts,
        revisions=info,
        replaced=sheet.replaced,
        changed=sheet.changed,
    )


def _pair(sheet: _Sheet, rev: Revision, ctx: Context, adding: list[Equipment]) -> None:
    """Added items that replace removed ones (``sheet.reuse``): of the same
    type and size, sharing an item they connect to.  Such an item may take
    the place of the one it replaces (and does, if its lines route as well
    from there): a pump changed for another stays where the old one stood."""
    gone = [g.placed for g in sheet.ghosts if g.rev == rev.id and g.placed]
    lines = [g.line[0] for g in sheet.ghosts if g.rev == rev.id and g.line]

    def neighbours(item: str, conns: list[Connection], hops: int = 2) -> set[str]:
        out: set[str] = set()
        todo, seen = [item], {item}
        for _ in range(hops):
            nxt = []
            for i in todo:
                for c in conns:
                    ids = [e for e, _ in c.ends]
                    if i in ids:
                        for o in ids:
                            if o not in seen:
                                seen.add(o)
                                # through generated items (tees, inline items)
                                if o.startswith(GENERATED_PREFIX) or sheet.bound(o):
                                    nxt.append(o)
                                else:
                                    out.add(o)
            todo = nxt
        return out

    taken: set[str] = set()
    for e in adding:
        geo = ctx.geometry(e)
        if e.id in sheet.reuse:  # a changed item: its own old place
            taken.add(sheet.reuse[e.id].id)
            continue
        mine = neighbours(e.id, rev.connections + lines)
        for g in gone:
            if g.id in taken or g.type != e.type:
                continue
            if (g.geo.width, g.geo.height) != (geo.width, geo.height):
                continue
            if mine & neighbours(g.id, lines):
                sheet.reuse[e.id] = g
                taken.add(g.id)
                break


def _place(
    sheet: _Sheet, eq: Equipment, geo: Geometry, lines: list[Connection], rev: str
) -> tuple[Router, list]:
    """Put an added item where its lines to what is on the sheet route best
    (moving the sheet apart if it fits nowhere); the router and the routed
    lines to lay (see ``_Sheet.try_lines``)."""
    w, h = geo.width, geo.height
    if not lines:  # joined to nothing yet: right of everything
        boxes = [p.bbox for p in sheet.placed.values()]
        x = max(b[2] for b in boxes) + 4
        y = min(b[1] for b in boxes)
        sheet.add_item(Placed(eq.id, eq.type, 0, geo, x, y), eq.desc)
        return sheet.board(), []
    state = sheet.snapshot()
    for attempt in range(ROOM_TRIES + 1):
        best = _best_spot(sheet, eq, geo, lines)
        if best is not None:
            (x, y), router, done = best
            sheet.add_item(Placed(eq.id, eq.type, 0, geo, x, y), eq.desc)
            home = sheet.reuse.get(eq.id)
            if home is not None and (home.x, home.y) == (x, y):
                sheet.open.add(home.id)  # it stands where the old one stood
                swap = (sheet.rev, eq.id, home.id)
                if swap not in sheet.replaced:
                    sheet.replaced.append(swap)
            return router, done
        if attempt == ROOM_TRIES:
            break
        # no room: move the sheet apart in front of the first line's port
        c = lines[0]
        (s, sp), (d, dp) = c.ends
        other, port = (d, dp) if s == eq.id else (s, sp)
        t = resolve_port(sheet.placed[other], sheet.port_for(other, port))
        axis = 0 if t.out_dir in (1, 3) else 1
        size = (w if axis == 0 else h) + 4 + 4 * attempt
        sign = 1 if t.out_dir in (0, 1) else -1
        sheet.make_room(axis, t.first[axis], sign * size)
    sheet.restore(state)
    # some of its lines route: the others are drawn otherwise (see split)
    best = _best_spot(sheet, eq, geo, lines, partial=True) if len(lines) > 1 else None
    if best is not None:
        (x, y), router, done = best
        sheet.add_item(Placed(eq.id, eq.type, 0, geo, x, y), eq.desc)
        return router, done
    raise RoutingError(f"{rev}: found no place for {eq.id}")


def _best_spot(
    sheet: _Sheet,
    eq: Equipment,
    geo: Geometry,
    lines: list[Connection],
    partial: bool = False,
) -> tuple[tuple[int, int], Router, list] | None:
    """The best free spot for ``eq`` (see :func:`_place`), with the router
    and the lines routed there; None if its lines route nowhere (all of
    them; ``partial``: any of them)."""
    home = sheet.reuse.get(eq.id)  # the place of the item it replaces
    also = home.id if home else None
    near, pipes = sheet.free_cells(also)
    w, h = geo.width, geo.height
    probe = Placed(eq.id, eq.type, 0, geo, 0, 0)
    base = sheet.board(also=also)
    # per line: own port, where it may meet the other end, src?, run needed
    ends: list[tuple[str, list[Terminal], bool, int]] = []
    for c in lines:
        (s, sp), (d, dp) = c.ends
        mine_src = s == eq.id
        other, oport = (d, dp) if mine_src else (s, sp)
        meets = [t for t, _ in sheet.terminals(base, other, oport)]
        if not meets:  # a line in use too short to tap: aim at the port
            meets = [resolve_port(sheet.placed[other], sheet.port_for(other, oport))]
        need = _run_needed(c, sheet.ctx.geometry)
        ends.append((sp if mine_src else dp, meets[:TAPS], mine_src, need))

    def free(x: int, y: int) -> bool:
        for cx in range(x, x + w + 1):
            for cy in range(y, y + h + 1):
                if (cx, cy) in near or (cx, cy) in pipes:
                    return False
        return True

    def estimate(x: int, y: int) -> float:
        total = 0.0
        for port, meets, mine_src, need in ends:
            own = resolve_port(replace(probe, x=x, y=y), port)
            best = None
            for t in meets:
                a, b = (own, t) if mine_src else (t, own)
                bends = _min_bends(a.first, a.out_dir, b.first, (b.out_dir + 2) % 4)
                dist = abs(a.first[0] - b.first[0]) + abs(a.first[1] - b.first[1])
                # too close for the line's inline items: it would need stretching
                cost = bends * BEND_COST + dist + max(need - dist, 0) * BEND_COST
                best = cost if best is None else min(best, cost)
            total += best or 0
        return total

    spots: dict[tuple[int, int], float] = {}
    for _, meets, _, _ in ends:
        for t in meets[:4]:
            fx, fy = t.first
            for x in range(fx - w - REACH, fx + REACH + 1):
                for y in range(fy - h - REACH, fy + REACH + 1):
                    if (x, y) not in spots and free(x, y):
                        spots[x, y] = estimate(x, y)

    def fits_home(p: Placed) -> bool:
        """The old item's place, which the layout chose: it only has to be
        clear (the gap the free spots keep may be less there)."""
        x0, y0, x1, y1 = p.bbox
        for q in sheet.placed.values():
            b = q.bbox
            if b[0] <= x1 and x0 <= b[2] and b[1] <= y1 and y0 <= b[3]:
                return False
        cells = {(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)}
        return not cells & pipes

    at_home = home is not None and fits_home(replace(home, geo=geo))
    if at_home:
        assert home is not None
        spots[home.x, home.y] = estimate(home.x, home.y)
    if not spots:
        return None
    ranked = sorted(spots, key=lambda s: (spots[s], abs(s[1]), s[0]))[:SPOTS]
    if at_home and home is not None and (home.x, home.y) not in ranked:
        ranked.insert(0, (home.x, home.y))
    best = None
    for x, y in ranked:
        router = sheet.clone(base)
        if not sheet.block(router, (x, y, x + w, y + h)):
            continue
        sheet.placed[eq.id] = Placed(eq.id, eq.type, 0, geo, x, y)
        try:
            score, done = sheet.try_lines(router, lines)
        finally:
            del sheet.placed[eq.id]
        moved = not (home is not None and (x, y) == (home.x, home.y))
        fails, short, turns, cost = score
        key = (fails, short, moved, turns, cost, spots[x, y])
        if best is None or key < best[0]:
            best = (key, (x, y), router, done)
    if best is None or best[0][0] >= (len(lines) if partial else 1):
        return None
    return best[1], best[2], best[3]


def _change(
    sheet: _Sheet, ch: Change, ctx: Context
) -> tuple[Equipment, list[Connection]] | None:
    """Change an item on the sheet (see :class:`~.netlist.Change`).

    Turned the way it stands now (the rotation / mirror the layout gave
    it), if the new item is as big and every port with a line stays where
    it was, it is changed in place: its lines stay, only where they meet
    its outline is found again.  Otherwise it is taken off with its lines
    and returned with them, to be put anew -- in its old place if it fits
    (see ``_Sheet.reuse``)."""
    if ch.id not in sheet.placed:
        raise KeyError(f"change: no item {ch.id!r} on the sheet")
    spec = ctx.specs.get(ch.id)
    if spec is None:
        raise KeyError(f"change: {ch.id!r} cannot be changed (no item written)")
    new = ctx.specs[ch.id] = _changed_spec(spec, ch)
    p = sheet.placed[ch.id]
    used = {
        port for k in sheet.lines_at(ch.id) for e, port in sheet.lines[k][0].ends
        if e == ch.id
    }  # fmt: skip
    turns = [(r, m) for m in (False, True) for r in (0, 90, 180, 270)]
    if "rotation" in ch.params or "mirror" in ch.params:
        turns = [(new.params.get("rotation", 0), new.params.get("mirror", False))]
    for rot, mir in turns:
        params = {**new.params, "rotation": rot, "mirror": mir}
        try:
            geo = ctx.geometry(replace(new, params=params))
        except (KeyError, ValueError):
            continue
        if (geo.width, geo.height) != (p.geo.width, p.geo.height):
            continue
        if not all(
            q in geo.ports
            and (geo.ports[q].side, geo.ports[q].offset)
            == (p.geo.ports[q].side, p.geo.ports[q].offset)
            for q in used
        ):
            continue
        # in place: the item redrawn, its lines met where its outline is now
        q = replace(p, type=new.type, geo=geo)
        sheet.placed[ch.id] = q
        for k in sheet.lines_at(ch.id):
            c, r = sheet.lines[k]
            pts = list(r.points)
            (a, pa), (b, pb) = c.ends
            if a == ch.id:
                pts[0] = resolve_port(q, pa).point
            if b == ch.id:
                pts[-1] = resolve_port(q, pb).point
            sheet.lines[k] = (c, Route(_simplify(pts), r.strict, r.nodes))
        _mark_changed(sheet, ch.id, new.desc)
        return None
    # put anew: off the sheet with its lines, back as an added item
    lines = [c for c in ctx.lines if any(e == ch.id for e, _ in c.ends)]
    ports = ctx.geometry(new).ports
    for c in lines:
        for e, port in c.ends:
            if e == ch.id and port not in ports:
                raise KeyError(
                    f"change: {ch.id} as changed has no port {port!r} for "
                    f"{c.src} -> {c.dst}; known: {sorted(ports)}"
                )
    if ch.id in ctx.inline:
        raise ValueError(
            f"change: {ch.id} does not fit its line any more; "
            "remove the line and add it again instead"
        )
    old = sheet.placed[ch.id]
    before = len(sheet.ghosts)
    sheet.drop(items=[ch.id])
    # what went with it comes back (in the revision's colour): no ghosts
    sheet.ghosts[before:] = [g for g in sheet.ghosts[before:] if g.placed == old]
    # grown: what lies beyond its old top / right side moves apart by the
    # growth first, so it grows where it stands
    geo = ctx.geometry(new)
    x0, y0, x1, y1 = old.bbox
    if geo.height > old.geo.height:
        sheet.make_room(1, y1 + 1, geo.height - old.geo.height)
    if geo.width > old.geo.width:
        sheet.make_room(0, x1 + 1, geo.width - old.geo.width)
    sheet.reuse[ch.id] = old
    sheet.changed.append((sheet.rev, ch.id))
    # the same item, changed: its old outline is not drawn under it
    sheet.replaced.append((sheet.rev, ch.id, ch.id))
    return new, lines


def _mark_changed(sheet: _Sheet, item: str, desc: str) -> None:
    sheet.added[item] = sheet.rev
    sheet.changed.append((sheet.rev, item))
    if desc:
        sheet.descs[item] = desc
    else:
        sheet.descs.pop(item, None)


# -- what a revision tells about the items and lines ---------------------------------
def _changed_spec(spec: Equipment, ch: Change) -> Equipment:
    return replace(
        spec,
        type=ch.type or spec.type,
        params={**spec.params, **ch.params},
        desc=spec.desc if ch.desc is None else ch.desc,
    )


def _forget(rev: Revision, ctx: Context) -> None:
    """The lines ``rev`` removes, or removes an end of, are gone."""
    gone = set(rev.remove)
    cut = set(rev.remove_lines)
    ctx.lines = [
        c
        for c in ctx.lines
        if (c.src, c.dst) not in cut and not any(e in gone for e, _ in c.ends)
    ]


def _learn(rev: Revision, ctx: Context) -> None:
    """The items and lines ``rev`` adds (named inline items included)."""
    ctx.specs.update((e.id, replace(e)) for e in rev.equipment)
    for c in rev.connections:
        for i in c.inline:
            if i.id:
                ctx.specs[i.id] = Equipment(
                    i.id, i.type, None, dict(i.params), {}, i.desc
                )
                ctx.inline[i.id] = (i.port_in, i.port_out)
    ctx.lines = ctx.lines + list(rev.connections)


def account_revision(rev: Revision, ctx: Context) -> None:
    """What :func:`apply_revision` learns from ``rev``, for a revision that
    is on the sheet already (a frozen sheet, see :mod:`.freeze`)."""
    _forget(rev, ctx)
    for ch in rev.changes:
        ctx.specs[ch.id] = _changed_spec(ctx.specs[ch.id], ch)
    _learn(rev, ctx)
