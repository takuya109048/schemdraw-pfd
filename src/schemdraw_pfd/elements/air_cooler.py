"""Air-cooled heat exchanger (空冷式熱交換器)."""

from __future__ import annotations

from ..geometry import Ellipse, Polyline, Port, Shape, rect
from .base import ElementSpec, Geometry, even

BUNDLE = 2  # the tube bundle: from this row to the top


class AirCoolerSpec(ElementSpec):
    """Fin-fan air cooler: the tube bundle on top of forced-draft fans.

    params: ``length`` (even, default 8), ``fans`` (default 1; at most one
    per 4 cells of length).  Ports: ``in`` (west) and ``out`` (east), at the
    ends of the tube bundle.
    """

    name = "air_cooler"

    def build(self, params):
        n = even(params.get("length", 8), 4, "air_cooler length")
        fans = int(params.get("fans", 1))
        if not 1 <= fans <= n // 4:
            raise ValueError(f"air_cooler fans must be 1 to {n // 4}, got {fans}")
        h = BUNDLE + 2
        shapes: list[Shape] = [rect(0, BUNDLE, n, h)]
        for y in (BUNDLE + 2 / 3, BUNDLE + 4 / 3):  # tubes
            shapes.append(Polyline(((0, y), (n, y)), False, False))
        bay = n / fans
        for k in range(fans):
            cx = bay * (k + 0.5)
            shapes += [  # plenum, fan blades, shaft and motor
                Polyline(((cx - bay / 2 + 0.3, BUNDLE), (cx - 1, 1.2)), False, False),
                Polyline(((cx + bay / 2 - 0.3, BUNDLE), (cx + 1, 1.2)), False, False),
                Ellipse(cx, 1.2, 1, 0.25, boundary=False),
                Polyline(((cx, 1.2), (cx, 0.6)), False, False),
                rect(cx - 0.4, 0, cx + 0.4, 0.6, boundary=False),
            ]
        ports = {"in": Port("W", 3), "out": Port("E", 3)}
        return Geometry(n, h, shapes, ports, label_pos="above", nozzles=True)
