"""Cyclone (サイクロン)."""

from __future__ import annotations

from ..geometry import Polyline, Port, Shape
from .base import ElementSpec, Geometry


class CycloneSpec(ElementSpec):
    """Cyclone separator (4x8): a cylinder over a cone, with the vortex
    finder (the gas outlet pipe) drawn inside the cylinder.

    Ports: ``in`` (tangential inlet, west, near the top), ``gas_out`` (top
    centre) and ``solids`` (bottom of the cone).
    """

    name = "cyclone"

    def build(self, params):
        body = ((0, 8), (4, 8), (4, 5), (2.5, 1), (2.5, 0), (1.5, 0), (1.5, 1), (0, 5))
        shapes: list[Shape] = [
            Polyline(body, True),
            Polyline(((1.4, 8), (1.4, 6.3), (2.6, 6.3), (2.6, 8)), False, False),
        ]
        ports = {"in": Port("W", 7), "gas_out": Port("N", 2), "solids": Port("S", 2)}
        return Geometry(4, 8, shapes, ports, label_pos="left", nozzles=True)
