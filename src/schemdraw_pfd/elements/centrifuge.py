"""Centrifuge (遠心分離機)."""

from __future__ import annotations

from ..geometry import Polyline, Port, Shape, rect
from .base import ElementSpec, Geometry


class CentrifugeSpec(ElementSpec):
    """Basket centrifuge (4x4): a casing with the basket inside, standing on
    its drive motor.

    Ports: ``in`` (feed, top centre), ``out`` (filtrate, east) and
    ``solids`` (cake discharge, bottom, beside the motor).  Rotating
    machinery like the pump.
    """

    name = "centrifuge"

    def build(self, params):
        shapes: list[Shape] = [
            rect(0, 1, 4, 4),  # casing
            Polyline(((0.6, 3.6), (1, 1.5), (3, 1.5), (3.4, 3.6)), boundary=False),
            Polyline(((2, 0.6), (2, 1.5)), boundary=False),  # shaft
            rect(1.4, 0, 2.6, 0.6),  # drive motor
        ]
        ports = {"in": Port("N", 2), "out": Port("E", 2), "solids": Port("S", 1)}
        return Geometry(4, 4, shapes, ports, label_pos="below", rotating=True)
