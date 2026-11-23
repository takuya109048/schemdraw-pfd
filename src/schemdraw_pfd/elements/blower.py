"""Blower / fan (ブロワー・ファン)."""

from __future__ import annotations

import math

from ..geometry import Ellipse, Polyline, Port, Shape
from .base import ElementSpec, Geometry

R = 1.5  # radius of the casing


class BlowerSpec(ElementSpec):
    """Centrifugal blower or fan (4x4), drawn as the ISO 10628 fan: a round
    casing with two lines converging towards the discharge and a
    three-bladed impeller inside.

    Ports: ``in`` (suction, west) and ``out`` (discharge, east, where the
    converging lines meet the casing).  Rotating machinery like the pump.
    """

    name = "blower"

    def build(self, params):
        c = 2.0
        shapes: list[Shape] = [
            Ellipse(c, c, R, R),
            # the converging lines: from the top and bottom of the casing
            # to the discharge on its east side
            Polyline(((c, c + R), (c + R, c + 0.35)), boundary=False),
            Polyline(((c, c - R), (c + R, c - 0.35)), boundary=False),
        ]
        for k in range(3):  # impeller blades
            a = math.radians(90 + 120 * k)
            tip = (c + 0.75 * math.cos(a), c + 0.75 * math.sin(a))
            shapes.append(Polyline(((c, c), tip), boundary=False))
        ports = {"in": Port("W", 2), "out": Port("E", 2)}
        return Geometry(4, 4, shapes, ports, label_pos="below", rotating=True)
