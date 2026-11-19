"""Orthogonal pipe routing on the grid.

Priority: (1) fewest bends, (2) shortest length.  Pipes leave and enter every
element perpendicular to the bounding-box face (I or L shape near the nozzle).

Strict mode additionally forbids any move opposite to a direction already travelled
(no U-turns, no I-turns / doubling back).  If a connection cannot be routed in strict
mode the caller may fall back to relaxed mode, which only forbids 180-degree
reversals in place.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache

from .geometry import NORMALS, ray_hit
from .layout import Placed

Node = tuple[int, int]
DIRS = [(0, 1), (1, 0), (0, -1), (-1, 0)]  # N E S W (clockwise)
TURN_COST = 1000
CROSS_COST = 20
# bit fields of a search heap entry (see Router._search); far beyond any sheet
STATE_BITS = 32
COUNT_BITS = 32
COST_BITS = 40
FLOOD_AFTER = 50  # strict search steps before the bends to the goal are counted
# a relaxed search (made when no strict route was found) counts them at once
RELAXED_FLOOD_AFTER = 1


class RoutingError(RuntimeError):
    pass


@dataclass
class Terminal:
    point: tuple[float, float]  # exact point on the outline (absolute)
    edge: Node  # where the pipe crosses the bounding-box face
    first: Node  # first free grid node outside the element
    out_dir: int  # index into DIRS: direction leaving the element


@dataclass
class Route:
    points: list[tuple[float, float]]  # corner points, outline to outline
    strict: bool
    nodes: list[Node] = field(default_factory=list)  # grid path, first to first

    @property
    def bends(self) -> int:
        return max(len(self.points) - 2, 0)


def resolve_port(p: Placed, port_name: str) -> Terminal:
    """Where a line meets ``p`` at its port (kept per geometry: the outline
    is searched once, not on every placement)."""
    near = p.geo.port_ends.get(port_name)
    if near is None:
        near = p.geo.port_ends[port_name] = _port_ends(p, port_name)
    (px, py), (ex, ey), (nx, ny), out = near
    e: Node = (ex + p.x, ey + p.y)
    return Terminal((px + p.x, py + p.y), e, (e[0] + nx, e[1] + ny), out)


def _port_ends(p: Placed, port_name: str) -> tuple:
    """:func:`resolve_port` relative to ``p``'s corner: (point, edge,
    normal, direction out)."""
    ports = p.geo.ports
    if port_name not in ports:
        raise KeyError(f"{p.id}: unknown port {port_name!r}; known: {sorted(ports)}")
    port = ports[port_name]
    w, h, off = p.geo.width, p.geo.height, port.offset
    span = h if port.side in "WE" else w
    if not 0 <= off <= span:
        raise ValueError(f"{p.id}.{port_name}: offset {off} outside 0..{span}")
    nx, ny = NORMALS[port.side]
    edge = {"W": (0, off), "E": (w, off), "S": (off, 0), "N": (off, h)}[port.side]
    origin = (edge[0] + nx, edge[1] + ny)
    t = ray_hit(p.geo.shapes, origin, (-nx, -ny))
    if t is None:
        raise ValueError(f"{p.id}.{port_name}: no outline found for this port")
    point = (origin[0] - nx * t, origin[1] - ny * t)
    return point, edge, (nx, ny), DIRS.index((nx, ny))


def _min_bends(p: Node, d: int, t: Node, td: int) -> int:
    """Bends certainly needed to reach ``t`` moving in ``td``, now moving ``d``."""
    if d == td:
        dx, dy = DIRS[d]
        rx, ry = t[0] - p[0], t[1] - p[1]
        on_line = rx * dy - ry * dx == 0 and rx * dx + ry * dy >= 0
        return 0 if on_line else 2
    if d == (td + 2) % 4:
        return 2
    return 1


def _strict_possible(s: Node, sd: int, t: Node, td: int) -> bool:
    """A strict route never moves against a direction it has used, i.e. it is
    monotone in x and in y.  It cannot exist when the start direction, the
    arrival direction and the way to the target already need both directions
    of one axis.

    Leaving and arriving in opposite directions (a branch rising from a header
    into a top nozzle) makes the one reversal they need allowed: an arch."""
    if sd == (td + 2) % 4:
        return True  # monotone across, the reversal itself is allowed
    need = {sd, td}
    dx, dy = t[0] - s[0], t[1] - s[1]
    if dx:
        need.add(1 if dx > 0 else 3)
    if dy:
        need.add(0 if dy > 0 else 2)
    return not ({0, 2} <= need or {1, 3} <= need)


def _estimate(p: Node, d: int, t: Node, td: int) -> int:
    """Admissible and consistent A* estimate of the remaining cost."""
    return abs(t[0] - p[0]) + abs(t[1] - p[1]) + TURN_COST * _min_bends(p, d, t, td)


def _table(test: Callable[[int], bool]) -> bytes:
    """Byte table: ``"1"`` for the cell values passing ``test``, else ``"0"``."""
    return bytes(ord("1") if test(v) else ord("0") for v in range(256))


_CLEAR = _table(lambda v: v == 0)  # not blocked / no pipe / no doorstep
_OFF_AXIS = (_table(lambda v: not v & 1), _table(lambda v: not v & 2))  # axes
_NOT_DOOR = (_table(lambda v: v != 1), _table(lambda v: v != 2))  # door


def _board(cells: bytes | bytearray, table: bytes) -> int:
    """Bit board: bit ``i`` set where ``table`` maps ``cells[i]`` to "1"."""
    return int(cells.translate(table)[::-1], 2)


_BYTE = bytes.maketrans(b"01", b"\0\1")


def _cell_bytes(board: int, n: int) -> int:
    """``board`` (of ``n`` cells) one byte per cell, 0 or 1, as an int: cell
    ``i`` is byte ``i`` from the low end, so a shift sets another bit of
    every byte and such ints add up cell by cell."""
    text = bin(board)[2:].zfill(n)[::-1].encode()
    return int.from_bytes(text.translate(_BYTE), "little")


def _shift(cells: int, d: int, h: int) -> int:
    """Every cell of a bit board one cell on in direction ``d``."""
    if d == 0:
        return cells << 1
    if d == 1:
        return cells << h
    if d == 2:
        return cells >> 1
    return cells >> h


def _straight(enter: tuple[int, int], h: int, w: int) -> Callable[[int, int], int]:
    """``fill(cells, d)``: ``cells`` and every cell they reach by moving
    straight on in direction ``d``, into cells ``enter[d % 2]`` lets in.

    North (+1 along a column) is a carry: ``open + start`` clears the run of
    open cells above each start cell and stops at its end.  The other ways
    spread by doubling shifts (Kogge-Stone): 1, 2, 4 ... cells at a time,
    each over the cells whose whole stretch is open.  The frame around the
    grid is blocked, so no run passes from one column (row) to the next."""
    north = enter[0]
    chains: dict[int, list[tuple[int, int]]] = {}

    def chain(d: int) -> list[tuple[int, int]]:
        if d not in chains:
            unit, span = (h, w) if d % 2 else (1, h)
            open_, k, reach, out = enter[d % 2], unit, 0, []
            while True:
                out.append((k, open_))
                reach += k // unit
                if reach >= span:
                    break
                open_ &= (open_ << k) if d == 1 else (open_ >> k)
                k *= 2
            chains[d] = out
        return chains[d]

    def fill(cells: int, d: int) -> int:
        if d == 0:
            start = (cells << 1) & north
            if start:
                cells |= (((north + start) ^ north) & north) | start
        elif d == 1:
            for k, open_ in chain(1):
                cells |= open_ & (cells << k)
        else:
            for k, open_ in chain(d):
                cells |= open_ & (cells >> k)
        return cells

    return fill


class _Levels(dict[int, bytes]):
    """``levels[board][cell]`` (see ``Router._bend_levels``): a board's
    bytes made when it is first looked at, so only the boards the search
    reaches are made."""

    def __init__(self, make: Callable[[int], bytes]) -> None:
        super().__init__()
        self.make = make

    def __missing__(self, board: int) -> bytes:
        made = self[board] = self.make(board)
        return made


@lru_cache(maxsize=32)
def _grid_xy(x0: int, y0: int, h: int, n: int) -> tuple[list[int], list[int]]:
    """x and y of every index of a grid (see Router); never changed."""
    return [i // h + x0 for i in range(n)], [i % h + y0 for i in range(n)]


class Router:
    """Routes lines one by one on the grid; a routed line becomes an obstacle.

    The grid is stored flat: node ``(x, y)`` has index ``(x - x0) * h + y - y0``
    inside a frame one cell wider than the routing area on every side, whose
    cells are blocked (so the search needs no bounds test).  ``axes[i]``: the
    axes routed lines already use at node ``i`` (bit 0 vertical, bit 1
    horizontal).
    """

    # boards of what equipment and doorsteps leave open (see _open)
    _walls: tuple[bytes, bytes, int, tuple[int, ...], int] | None = None

    def __init__(self, placed: list[Placed], margin: int = 3):
        boxes = [p.bbox for p in placed]
        xmin = min(b[0] for b in boxes) - margin
        xmax = max(b[2] for b in boxes) + margin
        ymin = min(b[1] for b in boxes) - margin
        ymax = max(b[3] for b in boxes) + margin
        self.x0, self.y0 = xmin - 1, ymin - 1
        self.h = h = ymax - ymin + 3
        w = xmax - xmin + 3
        blocked = bytearray(w * h)
        for i in range(h):  # the frame: left and right columns
            blocked[i] = blocked[(w - 1) * h + i] = 1
        for x in range(w):  # bottom and top rows
            blocked[x * h] = blocked[x * h + h - 1] = 1
        for x0, y0, x1, y1 in boxes:
            for x in range(x0, x1 + 1):
                i = (x - self.x0) * h - self.y0
                blocked[i + y0 : i + y1 + 1] = b"\1" * (y1 - y0 + 1)
        self.blocked = blocked
        self.axes = bytearray(w * h)
        # per cell: the axis bit of the port whose doorstep it is (or 0)
        self.door = bytearray(w * h)
        self.steps = (1, h, -1, -h)  # index offset of each of DIRS
        self._coords: tuple[list[int], list[int]] | None = None

    def _index(self, p: Node) -> int:
        return (p[0] - self.x0) * self.h + p[1] - self.y0

    def _xy(self) -> tuple[list[int], list[int]]:
        """x and y of every grid index (shared by routers on one grid)."""
        if self._coords is None:
            self._coords = _grid_xy(self.x0, self.y0, self.h, len(self.blocked))
        return self._coords

    def reserve(self, terminals: list[Terminal]) -> None:
        """Keep the first cell outside every port (its doorstep) for that
        port's own line: another line may cross it straight, but neither
        turn on it nor run through it along the port's axis.

        Without it a line routed early may turn on the cell right in front
        of a nozzle it does not serve, and the nozzle's own line can then no
        longer leave or arrive there.
        """
        for t in terminals:
            self.door[self._index(t.first)] = 1 << (t.out_dir % 2)

    def _staircase(self, s: Node, sd: int, t: Node, td: int) -> bool:
        """Can a strict search that is no arch get from ``s`` to ``t`` at all?

        Such a route never moves against a direction it has used (nor against
        ``sd``), so every move goes one of the directions it must go anyway:
        ``sd``, ``td`` and towards ``t`` -- one per axis (see
        :func:`_strict_possible`).  It is a staircase inside the box spanned
        by ``s`` and ``t``.  Over the cells of that box (no directions, no
        turn rules) this says whether a staircase gets through the cells the
        search may enter.  A necessary condition only: False means the search
        fails for certain, found out over a few cells instead of a whole
        search and flood.
        """
        blocked, axes, door, h = self.blocked, self.axes, self.door, self.h
        si, ti = self._index(s), self._index(t)
        if si == ti and sd == td:
            return True
        if axes[ti]:
            return False  # the search may not end on a pipe
        dx, dy = t[0] - s[0], t[1] - s[1]
        need = {sd, td}
        if dx:
            need.add(1 if dx > 0 else 3)
        if dy:
            need.add(0 if dy > 0 else 2)
        if {0, 2} <= need or {1, 3} <= need:
            return True  # not a staircase: say nothing
        xstep = h if 1 in need else -h if 3 in need else 0
        ystep = 1 if 0 in need else -1 if 2 in need else 0
        nx, ny = abs(dx) + 1, abs(dy) + 1
        # reach[a * ny + b]: the cell a steps along x and b along y from s
        reach = bytearray(nx * ny)
        reach[0] = 1
        for a in range(nx):
            col = si + a * xstep
            row = a * ny
            for b in range(1 if a == 0 else 0, ny):
                n = col + b * ystep
                if blocked[n]:
                    continue
                k = row + b
                if b and reach[k - 1] and not axes[n] & 1:
                    if door[n] != 1 or n == ti:  # a vertical step in
                        reach[k] = 1
                        continue
                if a and reach[k - ny] and not axes[n] & 2:
                    if door[n] != 2 or n == ti:  # a horizontal step in
                        reach[k] = 1
        # the last step goes along ``td`` into t (no pipe there, see above)
        if blocked[ti]:
            return False
        last = nx * ny - 1
        if td % 2 == 0:
            return ny > 1 and bool(reach[last - 1])
        return nx > 1 and bool(reach[last - ny])

    def _reachable(
        self,
        s: Node,
        sd: int,
        t: Node,
        td: int,
        strict: bool,
        axes: bytes | bytearray | None = None,
    ) -> bool:
        """Can the search get from ``s`` (moving ``sd``) to ``t`` (arriving
        moving ``td``) at all?  A flood over the search's own states --
        (cell, direction, and in strict mode the directions travelled) --
        with exactly its moves: no reversal in place (strict: none of a
        travelled direction, but the arrival of an arch), no turn on a pipe
        or another port's doorstep, no cell blocked, run along or entered on
        another port's axis.  So it fails exactly when the search does.

        The states of one (direction, travelled) pair are one bit board (see
        :func:`_board`), flooded a whole straight run at a time
        (:func:`_straight`); a turn moves the cells it may happen on to the
        board of the new direction.  Rounds: as many as turns, not cells.

        ``axes``: the pipes laid, if not this router's (see routes_alone).
        """
        axes = self.axes if axes is None else axes
        h = self.h
        si, ti = self._index(s), self._index(t)
        if axes[ti]:
            return False  # the search may not end on a pipe
        if si == ti and sd == td:
            return True  # it starts on its goal
        goal = 1 << ti
        enter, turn = self._open(si, ti, axes)
        fill = _straight(enter, h, len(self.blocked) // h)
        arch = sd == (td + 2) % 4

        def step(cells: int, d: int) -> int:
            """One cell on in direction ``d``, where it may enter."""
            return _shift(cells, d, h) & enter[d % 2]

        reached: dict[tuple[int, int], int] = {}  # (direction, travelled)
        todo: dict[tuple[int, int], int] = {}

        def add(d: int, mask: int, cells: int) -> None:
            cells &= ~reached.get((d, mask), 0)
            if cells:
                todo[d, mask] = todo.get((d, mask), 0) | cells

        def turns(cells: int, d: int, mask: int) -> None:
            for nd in ((d + 1) % 4, (d + 3) % 4):
                if not strict:
                    add(nd, 0, step(cells, nd))
                elif not mask & 1 << (nd + 2) % 4 or (arch and nd == td):
                    add(nd, mask | 1 << nd, step(cells, nd))

        start = 1 << si
        mask = (1 << sd) if strict else 0
        add(sd, mask, step(start, sd))
        if turn & start:
            turns(start, sd, mask)
        while todo:
            # a board reached by a move may always go straight on
            (d, mask), cells = todo.popitem()
            old = reached.get((d, mask), 0)
            new = fill(cells, d) & ~old
            if not new:
                continue
            reached[d, mask] = old | new
            if d == td and new & goal:
                return True
            if new & turn:
                turns(new & turn, d, mask)
        return False

    def _open(
        self, si: int, ti: int, axes: bytes | bytearray
    ) -> tuple[tuple[int, int], int]:
        """Bit boards of the search's moves (see :func:`_board`): the cells a
        move along each axis may enter (the goal ``ti`` past its doorstep
        rule), and the cells a line may turn on (the start ``si`` too)."""
        walls = self._walls
        if walls is None or walls[0] != self.blocked or walls[1] != self.door:
            # what equipment and doorsteps leave open: kept while they stay
            walls = self._walls = (
                bytes(self.blocked),
                bytes(self.door),
                _board(self.blocked, _CLEAR),
                tuple(_board(self.door, _NOT_DOOR[a]) for a in (0, 1)),
                _board(self.door, _CLEAR),
            )
        _, _, free, off_door, no_door = walls
        goal = 1 << ti
        vertical, horizontal = (
            free & _board(axes, _OFF_AXIS[a]) & (off_door[a] | goal) for a in (0, 1)
        )
        turn = _board(axes, _CLEAR) & (no_door | 1 << si)
        return (vertical, horizontal), turn

    def _bend_levels(
        self, s: Node, sd: int, t: Node, td: int, strict: bool
    ) -> tuple[_Levels, int] | None:
        """The fewest bends from every state of the search to its goal, and
        the cost of one route with the fewest: what lets a long search drop
        the states that can only cost more (see :meth:`_search`).  None: the
        goal cannot be reached at all (as :meth:`_reachable` would say).

        Counted back from the goal on bit boards, a level per bend: level 0
        holds the states that run straight on into the goal, level ``k + 1``
        those that run straight on to a cell where a turn reaches level
        ``k`` -- until the start is reached (at level ``b``: the fewest bends
        of any route) or nothing grows.  One board per direction and, in
        strict mode, set of directions travelled (always the start's, never
        two opposite ones but an arch's out and in).

        Returns ``levels[direction * 16 + travelled][cell]`` -- a state's
        level, ``b + 1`` for any above ``b``; a board's bytes are made when
        first looked at -- and the cost (as the search counts it) of a route
        of ``b`` bends walked from the start, down a level at every turn: no
        cheapest route costs more.
        """
        axes, blocked, door, h = self.axes, self.blocked, self.door, self.h
        n = len(blocked)
        si, ti = self._index(s), self._index(t)
        if axes[ti]:
            return None  # the search may not end on a pipe
        enter, turn = self._open(si, ti, axes)
        fill = _straight(enter, h, n // h)
        goal, start = 1 << ti, 1 << si
        arch = sd == (td + 2) % 4

        def ok(mask: int) -> bool:  # a set of directions a route may travel
            if not strict:
                return mask == 0
            if not mask >> sd & 1:
                return False
            for a in (0, 1):
                both = mask >> a & 1 and mask >> a + 2 & 1
                if both and not (arch and a == sd % 2):
                    return False
            return True

        def may(mask: int, nd: int) -> bool:  # may a move go ``nd``?
            return not mask & 1 << (nd + 2) % 4 or (arch and nd == td)

        def after(mask: int, nd: int) -> int:  # the travelled set after it
            return mask | 1 << nd if strict else 0

        first = sd * 16 + (1 << sd if strict else 0)
        boards = [
            d * 16 + m
            for m in range(16)
            if ok(m)
            for d in range(4)
            if not strict or m >> d & 1
        ]
        # where a state of each board may stand: entered moving its way
        host = {k: enter[k >> 4 & 1] | (start if k == first else 0) for k in boards}
        # per board: the boards its turns lead to, and whether it runs on
        turns = {
            k: [
                (nd, nd * 16 + after(k & 15, nd))
                for nd in ((k >> 4) + 1 & 3, (k >> 4) + 3 & 3)
                if may(k & 15, nd) and nd * 16 + after(k & 15, nd) in host
            ]
            for k in boards
        }
        runs = {k: may(k & 15, k >> 4) for k in boards}

        def back(k: int, cells: int) -> int:
            """``cells`` and every state of board ``k`` running straight on
            into them."""
            if not runs[k]:
                return cells
            d = k >> 4
            run = fill(cells & enter[d % 2], (d + 2) % 4)
            return cells | (_shift(run, (d + 2) % 4, h) & host[k])

        level = {k: back(k, goal & host[k]) if k >> 4 == td else 0 for k in boards}
        counts = [level]
        while not level[first] & start:
            grown = {}
            for k in boards:
                corner = 0  # cells where a turn reaches the level below
                for nd, to in turns[k]:
                    if level[to]:
                        corner |= _shift(level[to] & enter[nd % 2], (nd + 2) % 4, h)
                grown[k] = back(k, level[k] | (corner & turn & host[k]))
            if grown == level:
                return None  # the start never reaches the goal
            level = grown
            counts.append(level)
        b = len(counts) - 1
        if b >= 255:
            return _Levels(lambda k: bytes(n)), 1 << 60  # past a byte: drop none
        top = bytes([b + 1]) * n
        every = (1 << n) - 1  # level b + 1: every cell no level holds

        def made(k: int) -> bytes:
            if k not in host or not level[k]:
                return top
            # one board per bit of the level, each turned into bytes once
            planes = [0] * (b + 1).bit_length()
            below = 0
            for j, cells in enumerate([c[k] for c in counts] + [every]):
                new, below = cells & ~below, cells
                for bit in range(len(planes)):
                    if j >> bit & 1:
                        planes[bit] |= new
            value = sum(_cell_bytes(p, n) << bit for bit, p in enumerate(planes) if p)
            return value.to_bytes(n, "little")

        levels = _Levels(made)
        # walk a route of b bends: turn down a level wherever one can
        i, d, mask, cost = si, sd, first & 15, 0
        while not (i == ti and d == td):
            k = levels[d * 16 + mask][i]
            nxt = None
            if k and not axes[i] and not (door[i] and i != si):
                for nd in ((d + 1) % 4, (d + 3) % 4):
                    j, axis = i + self.steps[nd], 1 << nd % 2
                    if not may(mask, nd) or blocked[j] or axes[j] & axis:
                        continue
                    if door[j] == axis and j != ti:
                        continue
                    if levels[nd * 16 + after(mask, nd)][j] == k - 1:
                        nxt = j, nd, after(mask, nd), TURN_COST
                        break
            if nxt is None:  # straight on: the level holds
                nxt = i + self.steps[d], d, mask, 0
                if levels[d * 16 + mask][nxt[0]] > k:
                    return levels, 1 << 60  # (never) no bound: drop nothing
            i, d, mask, turned = nxt
            cost += 1 + turned + (CROSS_COST if axes[i] else 0)
        return levels, cost

    # -- search ---------------------------------------------------------------
    def _search(self, s: Node, sd: int, t: Node, td: int, strict: bool):
        """A* over (node, direction, travelled-directions mask).

        The estimate is the Manhattan distance plus TURN_COST for every bend
        that is certainly still needed (:func:`_estimate`, inlined here).  It
        never overestimates and is consistent, so the route found is still a
        cheapest one (fewest bends, then shortest); among equally cheap routes,
        ties go to the lower cost so far, then to the earlier insertion.

        A state is packed into one int: ``(node index * 4 + dir) * 16 + mask``.

        A search that drags on (``FLOOD_AFTER`` steps: the estimate counts
        too few bends, the way is blocked) gets the fewest bends from every
        state to the goal and the cost of one route (:meth:`_bend_levels`);
        a relaxed one, made because no strict route was found, gets them at
        once (``RELAXED_FLOOD_AFTER``).
        From then on a state is not taken up when its cost so far, its
        distance to the goal and the bends it still needs come to more than
        that route: it is on no cheapest route.  That changes nothing about
        the route found: every state on a cheapest route is still taken up
        (at its own cost, never more than the route), by the same state
        first, and in the same order -- the states dropped never lower the
        cost of one on a cheapest route, and entries that would lose to it
        are skipped as stale anyway.
        """
        blocked, axes, door = self.blocked, self.axes, self.door
        tx, ty = t
        ti = self._index(t)
        si = self._index(s)
        tdx, tdy = DIRS[td]
        back = (td + 2) % 4
        # out one way, in the opposite way: the arrival reversal is no U-turn
        arch = sd == back
        if strict and not arch and not self._staircase(s, sd, t, td):
            return None  # no staircase gets through: fails for certain
        # per direction d: the moves (straight on, then the two turns) with
        # the bends they certainly still need when not heading for the target
        moves = [
            [
                (
                    nd,
                    nd != d,
                    self.steps[nd],
                    DIRS[nd][0],
                    DIRS[nd][1],
                    1 << ((nd + 2) % 4),  # the opposite direction's bit
                    1 << (nd % 2),  # the axis bit
                    1 << nd,
                    # strict: may this move reverse a travelled direction?
                    arch and nd == td,
                    -1 if nd == td else (2 if nd == back else 1) * TURN_COST,
                )
                for nd in (d, (d + 1) % 4, (d + 3) % 4)
            ]
            for d in range(4)
        ]
        pop, push = heapq.heappop, heapq.heappush
        gx, gy = self._xy()

        # a heap entry is one int ordered like the tuple (f, cost so far,
        # insertion counter, state): bit fields, the counter unique -- so the
        # entries pop in exactly the tuple order, without building tuples
        sb, cb, gb = STATE_BITS, COUNT_BITS, COST_BITS
        sm, gm = (1 << sb) - 1, (1 << gb) - 1
        gs = cb + sb  # shift of the cost so far
        fs = gb + gs  # shift of f
        start = (self._index(s) * 4 + sd) * 16 + ((1 << sd) if strict else 0)
        dist = {start: 0}
        get = dist.get
        prev: dict[int, int] = {}
        heap = [_estimate(s, sd, t, td) << fs | start]
        counter = 1
        never = 1 << 60
        # bends still needed per state, and the cost no state may exceed
        # (unknown until the search drags on, see _bend_levels)
        none = bytes(len(blocked))
        levels, bound = _Levels(lambda k: none), never
        check = FLOOD_AFTER if strict else RELAXED_FLOOD_AFTER
        while heap:
            if check:
                check -= 1
                if not check:
                    found = self._bend_levels(s, sd, t, td, strict)
                    if found is None:
                        return None  # no route at all
                    levels, bound = found
            key = pop(heap)
            state = key & sm
            cost = (key >> gs) & gm
            if get(state, never) < cost:
                continue
            mask = state & 15
            i = state >> 6
            d = (state >> 4) & 3
            here = axes[i]
            if i == ti and d == td and not here:
                path = []
                while True:
                    j = state >> 6
                    path.append((gx[j], gy[j]))
                    if state not in prev:
                        return path[::-1]
                    state = prev[state]
            x = gx[i]
            y = gy[i]
            for nd, turn, step, dx, dy, rev, axis, bit, reverse_ok, bends in moves[d]:
                if turn and (here or (door[i] and i != si)):
                    continue  # never turn on a pipe, or at another's doorstep
                if strict and mask & rev and not reverse_ok:
                    continue
                n = i + step
                if blocked[n]:
                    continue
                if door[n] == axis and n != ti:
                    continue  # along the axis of another port, at its doorstep
                used = axes[n]
                if used & axis:
                    continue  # runs along an existing pipe
                nc = cost + 1
                if turn:
                    nc += TURN_COST
                if used:
                    nc += CROSS_COST
                nstate = (n * 4 + nd) * 16 + ((mask | bit) if strict else 0)
                if nc < get(nstate, never):
                    rx = tx - x - dx
                    ry = ty - y - dy
                    run = (rx if rx >= 0 else -rx) + (ry if ry >= 0 else -ry)
                    if nc + run + TURN_COST * levels[nstate & 63][n] > bound:
                        continue  # dearer than a route known: on no cheapest one
                    dist[nstate] = nc
                    prev[nstate] = state
                    if bends < 0:  # heading for the target: on its line?
                        on = rx * tdy - ry * tdx == 0 and rx * tdx + ry * tdy >= 0
                        bends = 0 if on else 2 * TURN_COST
                    push(
                        heap,
                        (nc + run + bends) << fs | nc << gs | counter << sb | nstate,
                    )
                    counter += 1
        return None

    def _commit(self, nodes: list[Node]) -> None:
        for a, b in zip(nodes, nodes[1:]):
            bit = 1 if a[0] == b[0] else 2  # vertical / horizontal piece
            self.axes[self._index(a)] |= bit
            self.axes[self._index(b)] |= bit

    # -- public ---------------------------------------------------------------
    def route(
        self, src: Terminal, dst: Terminal, allow_relaxed: bool, commit: bool = True
    ) -> Route | None:
        if src.first == dst.edge and dst.first == src.edge:
            # facing ports one cell apart: join them directly (an I shape)
            return Route(_simplify([src.point, src.edge, dst.edge, dst.point]), True)
        td = (dst.out_dir + 2) % 4  # arrive moving into the element
        modes = (True, False) if allow_relaxed else (True,)
        if not _strict_possible(src.first, src.out_dir, dst.first, td):
            modes = modes[1:]  # skip a search that is bound to fail
        for strict in modes:
            nodes = self._search(src.first, src.out_dir, dst.first, td, strict)
            if nodes is None:
                continue
            full = [src.point, src.edge, *nodes, dst.edge, dst.point]
            if commit:
                self._commit(nodes)
            return Route(_simplify(full), strict, nodes)
        return None

    def routes_alone(self, src: Terminal, dst: Terminal, allow_relaxed: bool) -> bool:
        """Would the line route with no other line laid?

        Laid lines only take moves away (no running along one, turning on
        one or ending on one), never add any: a line that does not route
        alone routes in no order, after no rip-up.  Decided like
        :meth:`route` decides it, the search replaced by its flood (which
        fails exactly when it does); a relaxed route exists whenever a strict
        one does."""
        if src.first == dst.edge and dst.first == src.edge:
            return True  # facing ports: joined directly
        s, sd, t = src.first, src.out_dir, dst.first
        td = (dst.out_dir + 2) % 4
        if not allow_relaxed and not _strict_possible(s, sd, t, td):
            return False
        none = bytes(len(self.axes))
        return self._reachable(s, sd, t, td, not allow_relaxed, axes=none)

    def route_together(
        self,
        ends: list[tuple[Terminal, Terminal, Node]],
        allow_relaxed: bool,
    ) -> list[Route] | None:
        """Route corresponding lines of repeated units with one shape.

        ``ends``: (src, dst, shift) per line; each line's terminals are the
        first line's, moved by its shift.  The shape is searched on a grid
        where a cell is free only if it is free at every shift, then laid
        down once per line.  None (nothing laid down) if there is no such
        shape; the caller then routes the lines one by one.
        """
        src, dst, _ = ends[0]
        if src.first == dst.edge and dst.first == src.edge:
            return None
        saved = self.blocked, self.axes, self.door
        self.blocked, self.axes, self.door = self._merged([sh for _, _, sh in ends])
        try:
            model = self.route(src, dst, allow_relaxed, commit=False)
        finally:
            self.blocked, self.axes, self.door = saved
        if model is None:
            return None
        before = bytes(self.axes)
        routes = []
        for s, d, sh in ends:
            r = self.replay(s, d, model, sh)
            if r is None:  # copies run into each other
                self.axes = bytearray(before)
                return None
            routes.append(r)
        return routes

    def _merged(self, shifts: list[Node]) -> tuple[bytearray, bytearray, bytearray]:
        """The grid as seen through every shift at once (cells OR-ed)."""
        h, n = self.h, len(self.blocked)
        w = n // h
        blocked = int.from_bytes(self.blocked, "big")
        axes = int.from_bytes(self.axes, "big")
        door = int.from_bytes(self.door, "big")
        for dx, dy in shifts:
            if (dx, dy) == (0, 0):
                continue
            off = dx * h + dy
            b, a = self._shifted(self.blocked, off, 1), self._shifted(self.axes, off, 0)
            door |= int.from_bytes(self._shifted(self.door, off, 0), "big")
            # a cell whose shifted row falls off the column is off the grid
            col = bytes(1 if not 0 <= y + dy < h else 0 for y in range(h))
            blocked |= int.from_bytes(b, "big") | int.from_bytes(col * w, "big")
            axes |= int.from_bytes(a, "big")
        return (
            bytearray(blocked.to_bytes(n, "big")),
            bytearray(axes.to_bytes(n, "big")),
            bytearray(door.to_bytes(n, "big")),
        )

    @staticmethod
    def _shifted(arr: bytearray, off: int, pad: int) -> bytes:
        """``out[i] = arr[i + off]`` (``pad`` beyond the ends)."""
        if off >= 0:
            return bytes(arr[off:]) + bytes([pad]) * off
        return bytes([pad]) * -off + bytes(arr[:off])

    def replay(
        self, src: Terminal, dst: Terminal, model: Route, shift: Node
    ) -> Route | None:
        """Route like ``model``, moved by ``shift``, if that path is free.

        The copy must obey the rules of the search: it starts at ``src`` and
        ends at ``dst`` heading in, never enters blocked cells, never runs
        along a routed pipe, never turns on one and never reverses in place.
        """
        if not model.nodes:
            return None  # facing ports: route() joins them directly anyway
        dx, dy = shift
        nodes = [(x + dx, y + dy) for x, y in model.nodes]
        if nodes[0] != src.first or nodes[-1] != dst.first:
            return None
        h, w = self.h, len(self.blocked) // self.h
        for x, y in nodes:
            if not (0 < x - self.x0 < w - 1 and 0 < y - self.y0 < h - 1):
                return None
        idx = [self._index(p) for p in nodes]
        blocked, axes, door = self.blocked, self.axes, self.door
        if blocked[idx[0]]:
            return None
        first, last = idx[0], idx[-1]
        d = src.out_dir
        for (a, b), i, j in zip(zip(nodes, nodes[1:]), idx, idx[1:]):
            nd = DIRS.index((b[0] - a[0], b[1] - a[1]))
            if nd == (d + 2) % 4 or blocked[j] or axes[j] & (1 << (nd % 2)):
                return None
            if door[j] == 1 << (nd % 2) and j != last:
                return None
            if nd != d and (axes[i] or (door[i] and i != first)):
                return None
            d = nd
        if d != (dst.out_dir + 2) % 4 or axes[idx[-1]]:
            return None
        full = [src.point, src.edge, *nodes, dst.edge, dst.point]
        self._commit(nodes)
        return Route(_simplify(full), model.strict, nodes)


def _simplify(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop duplicate and collinear points."""
    out: list[tuple[float, float]] = []
    for p in pts:
        if out and p == out[-1]:
            continue
        while len(out) >= 2:
            (ax, ay), (bx, by) = out[-2], out[-1]
            if (bx - ax) * (p[1] - by) - (by - ay) * (p[0] - bx) == 0:
                out.pop()
            else:
                break
        out.append(p)
    return out


Box = tuple[float, float, float, float]  # x0, y0, x1, y1


def needless_uturn(points: list[tuple[float, float]], boxes: Sequence[Box]) -> bool:
    """Does the route turn back on itself with nothing in the way?

    Turning back (a leg, then later one in the opposite direction) is fine
    when it goes around equipment.  The pocket of the U lies between the two
    legs, from the pieces joining them as deep as the longer leg; the U goes
    around an element that overlaps the pocket, or that touches it while
    lying wholly between the legs (a hop over a small item whose edge is on
    the line's own row).  A U around nothing is needless.
    """
    dirs = [
        (0 if q[1] > p[1] else 2) if p[0] == q[0] else (1 if q[0] > p[0] else 3)
        for p, q in zip(points, points[1:])
    ]
    for i, d in enumerate(dirs):
        j = next((j for j in range(i + 1, len(dirs)) if dirs[j] == (d + 2) % 4), None)
        if j is None:
            continue
        a = 1 if d in (0, 2) else 0  # the legs' axis (1: vertical legs)
        join = points[i + 1 : j + 1]
        across = [p[1 - a] for p in join]
        ends = (points[i][a], points[j + 1][a])  # the legs' far ends
        if d in (0, 1):  # first leg heads up / right: the pocket opens below
            depth = (min(ends), max(p[a] for p in join))
        else:
            depth = (min(p[a] for p in join), max(ends))
        lo = [0.0, 0.0]
        hi = [0.0, 0.0]
        lo[1 - a], hi[1 - a] = min(across), max(across)
        lo[a], hi[a] = depth

        def around(b: Box) -> bool:
            b0, b1 = (b[0], b[1]), (b[2], b[3])
            if all(b0[k] < hi[k] and b1[k] > lo[k] for k in (0, 1)):
                return True  # overlaps the pocket
            k = 1 - a  # wholly between the legs, and touching the pocket
            between = lo[k] <= b0[k] and b1[k] <= hi[k]
            return between and b0[a] <= hi[a] and b1[a] >= lo[a]

        if not any(around(b) for b in boxes):
            return True
    return False
