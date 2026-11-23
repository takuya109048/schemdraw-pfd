"""Safety / relief valve (安全弁)."""

from __future__ import annotations

from ..geometry import Polyline, Port, Shape
from .base import ElementSpec, Geometry


class SafetyValveSpec(ElementSpec):
    """Spring-loaded safety (relief) valve (2x3): an angle body, its inlet
    from below and its outlet to the side, with the spring on top.

    Ports: ``in`` (bottom: from the protected equipment) and ``out`` (east:
    to the flare or the atmosphere).
    """

    name = "safety_valve"

    def build(self, params):
        spring: list[tuple[float, float]] = [(1.0, 1.0)]
        for k in range(4):
            spring.append((0.65 if k % 2 else 1.35, 1.35 + 0.3 * k))
        spring.append((1, 2.6))
        shapes: list[Shape] = [
            Polyline(((0.5, 0), (1.5, 0), (1, 1)), True),  # inlet half
            Polyline(((2, 0.5), (2, 1.5), (1, 1)), True),  # outlet half
            Polyline(tuple(spring), boundary=False),
            Polyline(((0.6, 2.6), (1.4, 2.6)), boundary=False),
        ]
        ports = {"in": Port("S", 1), "out": Port("E", 1)}
        return Geometry(2, 3, shapes, ports, label_pos="left")
