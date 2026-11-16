"""Pump (ポンプ)."""

from __future__ import annotations

from ..geometry import Ellipse, Polyline, Port
from .base import ElementSpec, Geometry


class PumpSpec(ElementSpec):
    """Centrifugal pump (2x3), the size of a valve.

    Round casing on a trapezoidal pedestal.  Suction (``in``) is horizontal: it
    enters from the west and runs to the eye of the casing.  Discharge (``out``) is
    vertical: it leaves tangentially from the east edge of the casing, upwards.
    """

    name = "pump"

    def build(self, params):
        shapes = [
            Ellipse(1, 2, 1, 1),
            Polyline(((2, 2), (2, 3))),  # tangential discharge
            Polyline(((0, 2), (1.2, 2)), False, False),  # suction past the eye
            Polyline(((0.8, 2.2), (1.2, 2), (0.8, 1.8)), False, False),  # arrowhead
            Polyline(((0.5, 1.13), (0.1, 0.3), (1.9, 0.3), (1.5, 1.13)), False, False),
        ]
        ports = {"in": Port("W", 2), "out": Port("N", 2)}
        return Geometry(2, 3, shapes, ports, label_pos="below", rotating=True)
