"""Ejector (エジェクター)."""

from __future__ import annotations

from ..geometry import Polyline, Port, Shape
from .base import ElementSpec, Geometry


class EjectorSpec(ElementSpec):
    """Steam / gas ejector (4x2): the motive nozzle, the throat and the
    diffuser widening towards the outlet.

    Ports: ``motive`` (motive fluid, west), ``suction`` (the suctioned gas,
    bottom) and ``out`` (discharge, east).
    """

    name = "ejector"

    def build(self, params):
        body = (
            (0, 0.6), (1.5, 0.6), (2.2, 0.85), (4, 0.3),
            (4, 1.7), (2.2, 1.15), (1.5, 1.4), (0, 1.4),
        )  # fmt: skip
        shapes: list[Shape] = [
            Polyline(body, True),
            Polyline(((0, 1), (1.2, 1)), boundary=False),  # motive nozzle
            Polyline(((1.0, 0.85), (1.4, 1), (1.0, 1.15)), boundary=False),
        ]
        ports = {"motive": Port("W", 1), "suction": Port("S", 1), "out": Port("E", 1)}
        return Geometry(4, 2, shapes, ports, label_pos="above")
