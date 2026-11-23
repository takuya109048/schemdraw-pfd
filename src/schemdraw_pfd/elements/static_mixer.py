"""Static mixer (スタティックミキサー)."""

from __future__ import annotations

from ..geometry import Polyline, Port, Shape, rect
from .base import ElementSpec, Geometry


class StaticMixerSpec(ElementSpec):
    """In-line static mixer (4x2): a pipe section with twisted elements.
    Put it on a line with ``inline``.

    Ports: ``in`` (west) and ``out`` (east).
    """

    name = "static_mixer"

    def build(self, params):
        shapes: list[Shape] = [rect(0, 0.4, 4, 1.6)]
        for k in range(4):  # the elements, turned alternately
            x = 0.4 + 0.8 * k
            y0, y1 = (0.5, 1.5) if k % 2 else (1.5, 0.5)
            shapes.append(Polyline(((x, y0), (x + 0.8, y1)), boundary=False))
        ports = {"in": Port("W", 1), "out": Port("E", 1)}
        return Geometry(4, 2, shapes, ports, label_pos="above")
