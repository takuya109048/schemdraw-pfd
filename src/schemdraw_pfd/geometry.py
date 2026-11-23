"""Outline primitives and ray casting used to find exact connection points.

All coordinates are in grid units, local to an element (origin = bottom-left of
the element's bounding box).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

Point = tuple[float, float]
EPS = 1e-9

# side -> outward unit normal
NORMALS: dict[str, tuple[int, int]] = {
    "N": (0, 1),
    "S": (0, -1),
    "E": (1, 0),
    "W": (-1, 0),
}


@dataclass(frozen=True)
class Polyline:
    """Open or closed polyline. ``boundary=False`` makes it a pure decoration;
    ``pipe=True`` marks a piece of piping drawn as part of an element (a
    header's pipes, a tee's arms), which outputs that keep lines editable
    (:mod:`.xlsx`) draw as a line rather than as part of the symbol.
    ``line`` (a line kind, e.g. ``utility``): drawn in that kind's style
    instead of the equipment line width, so it reads as part of the line it
    leads into (a utility circle's lead)."""

    points: tuple[Point, ...]
    closed: bool = False
    boundary: bool = True
    fill: str | None = None
    pipe: bool = False
    line: str | None = None

    def segments(self) -> list[tuple[Point, Point]]:
        pts = list(self.points)
        segs = list(zip(pts, pts[1:]))
        if self.closed and len(pts) > 2:
            segs.append((pts[-1], pts[0]))
        return segs


@dataclass(frozen=True)
class Ellipse:
    """Ellipse or elliptic arc (angles in degrees, counter-clockwise)."""

    cx: float
    cy: float
    rx: float
    ry: float
    theta1: float = 0.0
    theta2: float = 360.0
    boundary: bool = True
    fill: str | None = None

    @property
    def full(self) -> bool:
        return abs(self.theta2 - self.theta1) >= 360.0 - EPS

    def contains_angle(self, deg: float) -> bool:
        if self.full:
            return True
        span = (self.theta2 - self.theta1) % 360.0
        return (deg - self.theta1) % 360.0 <= span + 1e-7


Shape = Polyline | Ellipse


def rect(x0: float, y0: float, x1: float, y1: float, boundary: bool = True) -> Polyline:
    return Polyline(((x0, y0), (x1, y0), (x1, y1), (x0, y1)), True, boundary)


def _ray_segment(o: Point, d: Point, a: Point, b: Point) -> float | None:
    """Smallest t >= 0 where o + t*d touches segment ab (d is axis aligned)."""
    ex, ey = b[0] - a[0], b[1] - a[1]
    denom = d[0] * ey - d[1] * ex
    if abs(denom) < EPS:  # parallel
        # collinear?
        if abs((a[0] - o[0]) * d[1] - (a[1] - o[1]) * d[0]) > EPS:
            return None
        ts = [(p[0] - o[0]) * d[0] + (p[1] - o[1]) * d[1] for p in (a, b)]
        ts = [t for t in ts if t >= -EPS]
        return max(min(ts), 0.0) if ts else None
    t = ((a[0] - o[0]) * ey - (a[1] - o[1]) * ex) / denom
    u = ((a[0] - o[0]) * d[1] - (a[1] - o[1]) * d[0]) / denom
    if t >= -EPS and -EPS <= u <= 1 + EPS:
        return max(t, 0.0)
    return None


def _ray_ellipse(o: Point, d: Point, e: Ellipse) -> float | None:
    # transform to unit circle space
    ox, oy = (o[0] - e.cx) / e.rx, (o[1] - e.cy) / e.ry
    dx, dy = d[0] / e.rx, d[1] / e.ry
    a = dx * dx + dy * dy
    b = 2 * (ox * dx + oy * dy)
    c = ox * ox + oy * oy - 1
    disc = b * b - 4 * a * c
    if disc < -EPS:
        return None
    disc = math.sqrt(max(disc, 0.0))
    best = None
    for t in sorted(((-b - disc) / (2 * a), (-b + disc) / (2 * a))):
        if t < -EPS:
            continue
        px, py = o[0] + t * d[0], o[1] + t * d[1]
        ang = math.degrees(math.atan2((py - e.cy) / e.ry, (px - e.cx) / e.rx))
        if e.contains_angle(ang):
            best = max(t, 0.0)
            break
    return best


def ray_hit(shapes: list[Shape], origin: Point, direction: Point) -> float | None:
    """Distance from ``origin`` along ``direction`` to the first outline hit."""
    best: float | None = None
    for s in shapes:
        if not s.boundary:
            continue
        cands: list[float | None] = []
        if isinstance(s, Polyline):
            cands = [_ray_segment(origin, direction, a, b) for a, b in s.segments()]
        else:
            cands = [_ray_ellipse(origin, direction, s)]
        for t in cands:
            if t is not None and (best is None or t < best):
                best = t
    return best


def outline_distance(shapes: list[Shape], origin: Point, direction: Point) -> float:
    """Like :func:`ray_hit`, but an outline must be hit."""
    t = ray_hit(shapes, origin, direction)
    if t is None:
        raise ValueError(f"no outline found from {origin} towards {direction}")
    return t


@dataclass
class Port:
    """Connection point request: a side of the bounding box plus a grid offset.

    The actual point is found by casting a ray inward from that side, so it always
    lies on the outermost outline whatever the shape is.  The pipe leaves along the
    side's outward normal, i.e. perpendicular to the bounding-box face.
    """

    side: str
    offset: int
    meta: dict = field(default_factory=dict)


def find_crossings(
    paths: Sequence[Sequence[Point]],
) -> list[tuple[int, int, int, int, Point]]:
    """All line crossings as ``(h_route, h_seg, v_route, v_seg, point)``.

    ``h_*`` identify the horizontal segment, ``v_*`` the vertical one.
    """
    horiz, vert = [], []
    for ri, pts in enumerate(paths):
        for si, (p, q) in enumerate(zip(pts, pts[1:])):
            if p[1] == q[1]:
                horiz.append((ri, si, p[1], min(p[0], q[0]), max(p[0], q[0])))
            else:
                vert.append((ri, si, p[0], min(p[1], q[1]), max(p[1], q[1])))
    out = []
    for hr, hs, y, x0, x1 in horiz:
        for vr, vs, x, y0, y1 in vert:
            if x0 < x < x1 and y0 < y < y1:
                out.append((hr, hs, vr, vs, (x, y)))
    return out
