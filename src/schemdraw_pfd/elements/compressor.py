"""Compressor (圧縮機)."""

from __future__ import annotations

from ..geometry import Polyline, Port, Shape
from .base import ElementSpec, Geometry


class CompressorSpec(ElementSpec):
    """Centrifugal compressor (4x4): a trapezoid, wide at the suction.

    Ports: ``in`` (suction, west, the wide end) and ``out`` (discharge, east,
    the narrow end).  Rotating machinery: its specification is listed with
    the pumps' at the bottom of the sheet.
    """

    name = "compressor"

    def build(self, params):
        shapes: list[Shape] = [Polyline(((0, 0), (4, 1), (4, 3), (0, 4)), True)]
        ports = {"in": Port("W", 2), "out": Port("E", 2)}
        return Geometry(4, 4, shapes, ports, label_pos="below", rotating=True)
