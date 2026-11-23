"""Steam trap (スチームトラップ)."""

from __future__ import annotations

import math

from ..geometry import Ellipse, Polyline, Port, Shape
from .base import ElementSpec, Geometry

R = 0.6  # radius of the symbol


class SteamTrapSpec(ElementSpec):
    """Steam trap (2x2), drawn as in ISO 10628: a circle on the condensate
    line with its lower right half solid.  Put it on a line with ``inline``.

    Ports: ``in`` (west) and ``out`` (east).
    """

    name = "steam_trap"

    def build(self, params):
        half = [  # the solid half, below the diagonal from top right
            (1 + R * math.cos(math.radians(a)), 1 + R * math.sin(math.radians(a)))
            for a in range(45, -136, -15)
        ]
        shapes: list[Shape] = [
            Polyline(((0, 1), (1 - R, 1)), pipe=True),
            Ellipse(1, 1, R, R),
            Polyline(tuple(half), True, False, fill="black"),
            Polyline(((1 + R, 1), (2, 1)), pipe=True),
        ]
        ports = {"in": Port("W", 1), "out": Port("E", 1)}
        return Geometry(2, 2, shapes, ports, label_pos="none")
