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
from dataclasses import dataclass, field

from .geometry import NORMALS, ray_hit
from .layout import Placed

Node = tuple[int, int]
DIRS = [(0, 1), (1, 0), (0, -1), (-1, 0)]  # N E S W (clockwise)
TURN_COST = 1000
CROSS_COST = 20


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
    pt = (origin[0] - nx * t + p.x, origin[1] - ny * t + p.y)
    e: Node = (edge[0] + p.x, edge[1] + p.y)
    return Terminal(pt, e, (e[0] + nx, e[1] + ny), DIRS.index((nx, ny)))


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


class Router:
    """Routes lines one by one on the grid; a routed line becomes an obstacle.

    The grid is stored flat: node ``(x, y)`` has index ``(x - x0) * h + y - y0``
    inside a frame one cell wider than the routing area on every side, whose
    cells are blocked (so the search needs no bounds test).  ``axes[i]``: the
    axes routed lines already use at node ``i`` (bit 0 vertical, bit 1
    horizontal).
    """

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
        self.steps = (1, h, -1, -h)  # index offset of each of DIRS

    def _index(self, p: Node) -> int:
        return (p[0] - self.x0) * self.h + p[1] - self.y0

    # -- search ---------------------------------------------------------------
    def _search(self, s: Node, sd: int, t: Node, td: int, strict: bool):
        """A* over (node, direction, travelled-directions mask).

        The estimate is the Manhattan distance plus TURN_COST for every bend
        that is certainly still needed (:func:`_estimate`, inlined here).  It
        never overestimates and is consistent, so the route found is still a
        cheapest one (fewest bends, then shortest); among equally cheap routes,
        ties go to the lower cost so far, then to the earlier insertion.

        A state is packed into one int: ``(node index * 4 + dir) * 16 + mask``.
        """
        blocked, axes, h = self.blocked, self.axes, self.h
        x0, y0 = self.x0, self.y0
        tx, ty = t
        ti = self._index(t)
        tdx, tdy = DIRS[td]
        back = (td + 2) % 4
        # out one way, in the opposite way: the arrival reversal is no U-turn
        arch = sd == back
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

        start = (self._index(s) * 4 + sd) * 16 + ((1 << sd) if strict else 0)
        dist = {start: 0}
        get = dist.get
        prev: dict[int, int] = {}
        heap = [(_estimate(s, sd, t, td), 0, 0, start)]
        counter = 1
        never = 1 << 60
        while heap:
            _, cost, _, state = pop(heap)
            if get(state, never) < cost:
                continue
            mask = state & 15
            i, d = divmod(state >> 4, 4)
            here = axes[i]
            if i == ti and d == td and not here:
                path = []
                while True:
                    xi, yi = divmod(state >> 6, h)
                    path.append((xi + x0, yi + y0))
                    if state not in prev:
                        return path[::-1]
                    state = prev[state]
            x, y = divmod(i, h)
            x += x0
            y += y0
            for nd, turn, step, dx, dy, rev, axis, bit, reverse_ok, bends in moves[d]:
                if turn and here:
                    continue  # never turn on top of an existing pipe
                if strict and mask & rev and not reverse_ok:
                    continue
                n = i + step
                if blocked[n]:
                    continue
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
                    dist[nstate] = nc
                    prev[nstate] = state
                    rx = tx - x - dx
                    ry = ty - y - dy
                    if bends < 0:  # heading for the target: on its line?
                        on = rx * tdy - ry * tdx == 0 and rx * tdx + ry * tdy >= 0
                        bends = 0 if on else 2 * TURN_COST
                    f = nc + abs(rx) + abs(ry) + bends
                    push(heap, (f, nc, counter, nstate))
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
        saved = self.blocked, self.axes
        self.blocked, self.axes = self._merged([sh for _, _, sh in ends])
        try:
            model = self.route(src, dst, allow_relaxed, commit=False)
        finally:
            self.blocked, self.axes = saved
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

    def _merged(self, shifts: list[Node]) -> tuple[bytearray, bytearray]:
        """The grid as seen through every shift at once (cells OR-ed)."""
        h, n = self.h, len(self.blocked)
        w = n // h
        blocked = int.from_bytes(self.blocked, "big")
        axes = int.from_bytes(self.axes, "big")
        for dx, dy in shifts:
            if (dx, dy) == (0, 0):
                continue
            off = dx * h + dy
            b, a = self._shifted(self.blocked, off, 1), self._shifted(self.axes, off, 0)
            # a cell whose shifted row falls off the column is off the grid
            col = bytes(1 if not 0 <= y + dy < h else 0 for y in range(h))
            blocked |= int.from_bytes(b, "big") | int.from_bytes(col * w, "big")
            axes |= int.from_bytes(a, "big")
        return bytearray(blocked.to_bytes(n, "big")), bytearray(axes.to_bytes(n, "big"))

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
        blocked, axes = self.blocked, self.axes
        if blocked[idx[0]]:
            return None
        d = src.out_dir
        for (a, b), i, j in zip(zip(nodes, nodes[1:]), idx, idx[1:]):
            nd = DIRS.index((b[0] - a[0], b[1] - a[1]))
            if nd == (d + 2) % 4 or blocked[j] or axes[j] & (1 << (nd % 2)):
                return None
            if nd != d and axes[i]:
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


def needless_uturn(points: list[tuple[float, float]], boxes: list[Box]) -> bool:
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
