"""Y-type strainer (Y形ストレーナー)."""

from __future__ import annotations

from ..geometry import Polyline, Port, Shape
from .base import ElementSpec, Geometry


class StrainerSpec(ElementSpec):
    """Y-type strainer (2x2), drawn on the pipe: the screen leg slants down
    from the pipe.  Put it on a line with ``inline``.

    Ports: ``in`` (west) and ``out`` (east).
    """

    name = "strainer"

    def build(self, params):
        shapes: list[Shape] = [
            Polyline(((0, 1), (2, 1)), pipe=True),
            Polyline(((0.6, 1), (1.4, 0.2), (1.8, 0.6), (1.0, 1.4)), boundary=False),
        ]
        ports = {"in": Port("W", 1), "out": Port("E", 1)}
        return Geometry(2, 2, shapes, ports, label_pos="none")
