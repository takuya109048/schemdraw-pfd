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
from dataclasses import dataclass

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
    of one axis."""
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
    def __init__(self, placed: list[Placed], margin: int = 3):
        self.blocked: set[Node] = set()
        xs0, ys0, xs1, ys1 = [], [], [], []
        for p in placed:
            x0, y0, x1, y1 = p.bbox
            xs0.append(x0), ys0.append(y0), xs1.append(x1), ys1.append(y1)
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    self.blocked.add((x, y))
        self.xmin, self.xmax = min(xs0) - margin, max(xs1) + margin
        self.ymin, self.ymax = min(ys0) - margin, max(ys1) + margin
        self.edges: set[frozenset[Node]] = set()
        self.axes: dict[Node, set[int]] = {}  # node -> {0: vertical, 1: horizontal}

    # -- search ---------------------------------------------------------------
    def _search(self, s: Node, sd: int, t: Node, td: int, strict: bool):
        """A* over (node, direction, travelled-directions mask).

        The estimate is the Manhattan distance plus TURN_COST for every bend
        that is certainly still needed.  It never overestimates and is
        consistent, so the route found is still a cheapest one (fewest bends,
        then shortest); among equally cheap routes, ties go to the lower cost
        so far, then to the earlier insertion.
        """
        start = (s, sd, (1 << sd) if strict else 0)
        dist = {start: 0}
        prev: dict = {}
        heap = [(_estimate(s, sd, t, td), 0, 0, start)]
        counter = 1
        while heap:
            _, cost, _, state = heapq.heappop(heap)
            if dist.get(state, 1 << 60) < cost:
                continue
            (x, y), d, mask = state
            if (x, y) == t and d == td and not self.axes.get((x, y)):
                path = [(x, y)]
                while state in prev:
                    state = prev[state]
                    path.append(state[0])
                return path[::-1]
            occupied = bool(self.axes.get((x, y)))
            for nd in (d, (d + 1) % 4, (d + 3) % 4):
                if nd != d and occupied:
                    continue  # never turn on top of an existing pipe
                if strict and mask & (1 << ((nd + 2) % 4)):
                    continue
                dx, dy = DIRS[nd]
                n = (x + dx, y + dy)
                if not (
                    self.xmin <= n[0] <= self.xmax and self.ymin <= n[1] <= self.ymax
                ):
                    continue
                if n in self.blocked or frozenset((n, (x, y))) in self.edges:
                    continue
                axis = nd % 2
                used = self.axes.get(n, ())
                if axis in used:
                    continue
                step = 1 + (TURN_COST if nd != d else 0) + (CROSS_COST if used else 0)
                nstate = (n, nd, mask | (1 << nd) if strict else 0)
                nc = cost + step
                if nc < dist.get(nstate, 1 << 60):
                    dist[nstate] = nc
                    prev[nstate] = state
                    f = nc + _estimate(n, nd, t, td)
                    heapq.heappush(heap, (f, nc, counter, nstate))
                    counter += 1
        return None

    def _commit(self, nodes: list[Node]) -> None:
        for a, b in zip(nodes, nodes[1:]):
            self.edges.add(frozenset((a, b)))
            axis = 0 if a[0] == b[0] else 1
            self.axes.setdefault(a, set()).add(axis)
            self.axes.setdefault(b, set()).add(axis)

    # -- public ---------------------------------------------------------------
    def route(self, src: Terminal, dst: Terminal, allow_relaxed: bool) -> Route | None:
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
            self._commit(nodes)
            return Route(_simplify(full), strict)
        return None


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
