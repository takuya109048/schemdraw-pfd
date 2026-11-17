"""Shared building blocks for element definitions and the element registry.

An :class:`ElementSpec` turns user parameters into a :class:`Geometry`: a bounding
box on the grid, outline shapes and named ports.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any

from ..geometry import Polyline, Port, Shape

Text = tuple[float, float, str, float]  # x, y, text, size relative to the tag font
LABEL_POSITIONS = ("center", "above", "below", "left", "none")


@dataclass
class Geometry:
    width: int
    height: int
    shapes: list[Shape]
    ports: dict[str, Port]
    label_pos: str = "center"  # center | above | below | left | none
    texts: list[Text] = field(default_factory=list)  # fixed text inside the symbol
    nozzles: bool = False  # draw a flange tick where a pipe meets the outline
    rotating: bool = False  # rotating machinery (spec text goes to the bottom row)
    off_sheet: bool = False  # connector flag: lives in a shared edge column
    # groups of ports on one face that serve alike (head nozzles, header
    # branches): the layout may swap the lines among them to avoid crossings
    swappable: list[list[str]] = field(default_factory=list)

    def translated(self, dx: float, dy: float) -> list[Shape]:
        return [_shift(s, dx, dy) for s in self.shapes]


def _shift(s: Shape, dx: float, dy: float) -> Shape:
    if isinstance(s, Polyline):
        return replace(s, points=tuple((x + dx, y + dy) for x, y in s.points))
    return replace(s, cx=s.cx + dx, cy=s.cy + dy)


def rotate_ccw(geo: Geometry) -> Geometry:
    """Rotate a geometry by 90 degrees counter-clockwise (W->S, E->N, N->W, S->E)."""
    h = geo.height

    def pt(x: float, y: float) -> tuple[float, float]:
        return (h - y, x)

    shapes: list[Shape] = []
    for s in geo.shapes:
        if isinstance(s, Polyline):
            shapes.append(replace(s, points=tuple(pt(x, y) for x, y in s.points)))
        else:
            cx, cy = pt(s.cx, s.cy)
            shapes.append(
                replace(
                    s, cx=cx, cy=cy, rx=s.ry, ry=s.rx,
                    theta1=s.theta1 + 90, theta2=s.theta2 + 90,
                )
            )  # fmt: skip
    turn = {"W": "S", "E": "N", "N": "W", "S": "E"}
    ports = {
        n: Port(turn[p.side], h - p.offset if p.side in "WE" else p.offset)
        for n, p in geo.ports.items()
    }
    texts = [(*pt(x, y), t, s) for x, y, t, s in geo.texts]
    return replace(
        geo, width=geo.height, height=geo.width, shapes=shapes, ports=ports, texts=texts
    )


class ElementSpec:
    """Base class: subclass, set ``name`` and implement :meth:`build`."""

    name = "element"

    def build(self, params: dict[str, Any]) -> Geometry:  # pragma: no cover
        raise NotImplementedError


def even(value: Any, minimum: int, what: str) -> int:
    """Validate a size that must be an even number of grid cells."""
    v = int(value)
    if v < minimum or v % 2:
        raise ValueError(f"{what} must be an even number >= {minimum}, got {value}")
    return v


def parse_port(v: Any) -> Port:
    """Accept ``Port``, ``{"side": "N", "offset": 1}`` or ``("N", 1)``."""
    if isinstance(v, Port):
        return v
    side, offset = (v["side"], v["offset"]) if isinstance(v, dict) else v
    if side not in ("N", "S", "E", "W"):
        raise ValueError(f"port side must be N/S/E/W, got {side!r}")
    if int(offset) != offset:
        raise ValueError(f"port offset must be on the grid (integer), got {offset!r}")
    return Port(side, int(offset))


def mirror_x(geo: Geometry) -> Geometry:
    """Mirror a geometry left-right (W <-> E)."""
    w = geo.width
    shapes: list[Shape] = []
    for s in geo.shapes:
        if isinstance(s, Polyline):
            shapes.append(replace(s, points=tuple((w - x, y) for x, y in s.points)))
        else:
            shapes.append(
                replace(s, cx=w - s.cx, theta1=180 - s.theta2, theta2=180 - s.theta1)
            )
    flip = {"W": "E", "E": "W", "N": "N", "S": "S"}
    ports = {
        n: replace(
            p, side=flip[p.side], offset=w - p.offset if p.side in "NS" else p.offset
        )
        for n, p in geo.ports.items()
    }
    texts = [(w - x, y, t, s) for x, y, t, s in geo.texts]
    return replace(geo, shapes=shapes, ports=ports, texts=texts)


ROTATIONS = (0, 90, 180, 270)
TRANSFORM_PARAMS = ("rotation", "mirror")


def transform(geo: Geometry, rotation: Any = 0, mirror: Any = False) -> Geometry:
    """Common placement transform: mirror left-right first, then rotate CCW."""
    if int(rotation) not in ROTATIONS:
        raise ValueError(f"rotation must be one of {ROTATIONS}, got {rotation!r}")
    if mirror:
        geo = mirror_x(geo)
    for _ in range(int(rotation) // 90):
        geo = rotate_ccw(geo)
    return geo


# -- registry ------------------------------------------------------------------
class ElementLibrary:
    """A set of element types.

    Lookups fall back to ``parent``, so a flowsheet can own a library with its
    own (e.g. JSON-defined) elements on top of the standard ones without
    changing what other flowsheets see.
    """

    def __init__(self, parent: ElementLibrary | None = None) -> None:
        self.parent = parent
        self._specs: dict[str, ElementSpec] = {}

    def register(self, spec: ElementSpec) -> ElementSpec:
        """Add (or replace) an element type in this library."""
        self._specs[spec.name] = spec
        return spec

    def get(self, name: str) -> ElementSpec:
        lib: ElementLibrary | None = self
        while lib is not None:
            if name in lib._specs:
                return lib._specs[name]
            lib = lib.parent
        raise KeyError(f"unknown element type {name!r}; known: {self.names()}")

    def __contains__(self, name: str) -> bool:
        try:
            self.get(name)
        except KeyError:
            return False
        return True

    def names(self) -> list[str]:
        out = set(self._specs)
        if self.parent is not None:
            out |= set(self.parent.names())
        return sorted(out)

    def local(self) -> dict[str, ElementSpec]:
        """Element types defined in this library itself (not in a parent)."""
        return dict(self._specs)

    def build(self, name: str, params: dict[str, Any] | None = None) -> Geometry:
        """Build an element, then apply the common ``rotation`` / ``mirror``.

        Elements nested inside this one (e.g. valves on header branches) are
        looked up in this library too.
        """
        params = dict(params or {})
        common = {k: params.pop(k) for k in TRANSFORM_PARAMS if k in params}
        token = _ACTIVE.set(self)
        try:
            geo = self.get(name).build(params)
        finally:
            _ACTIVE.reset(token)
        return transform(geo, **common)


DEFAULT_LIBRARY = ElementLibrary()
_ACTIVE: ContextVar[ElementLibrary] = ContextVar(
    "active_library", default=DEFAULT_LIBRARY
)


def active_library() -> ElementLibrary:
    """The library an element is being built from (default: the standard one)."""
    return _ACTIVE.get()


def register_element(spec: ElementSpec) -> ElementSpec:
    """Add (or replace) an element type in the default (global) library."""
    return DEFAULT_LIBRARY.register(spec)


def get_element(name: str) -> ElementSpec:
    """Look up an element type in the active library (default: global)."""
    return active_library().get(name)


def element_names() -> list[str]:
    return active_library().names()
