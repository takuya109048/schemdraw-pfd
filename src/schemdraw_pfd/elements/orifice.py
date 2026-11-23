"""Orifice plate (オリフィス)."""

from __future__ import annotations

from ..geometry import Polyline, Port, Shape
from .base import ElementSpec, Geometry


class OrificeSpec(ElementSpec):
    """Flow orifice plate (2x2): two short bars across the pipe.  Put it on
    a line with ``inline``.

    Ports: ``in`` (west), ``out`` (east) and ``tap`` (top, on the pipe: the
    pressure taps, to draw an ``instrument`` line to a flow transmitter).
    """

    name = "orifice"

    def build(self, params):
        shapes: list[Shape] = [
            Polyline(((0, 1), (2, 1)), pipe=True),
            Polyline(((0.8, 0.4), (0.8, 1.6)), boundary=False),
            Polyline(((1.2, 0.4), (1.2, 1.6)), boundary=False),
        ]
        ports = {"in": Port("W", 1), "out": Port("E", 1), "tap": Port("N", 1)}
        return Geometry(2, 2, shapes, ports, label_pos="none")
