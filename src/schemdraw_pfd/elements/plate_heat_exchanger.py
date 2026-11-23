"""Plate heat exchanger (プレート式熱交換器)."""

from __future__ import annotations

from ..geometry import Polyline, Port, Shape, rect
from .base import ElementSpec, Geometry


class PlateHeatExchangerSpec(ElementSpec):
    """Plate heat exchanger (4x6), drawn as in ISO 10628: the frame with its
    pack of plates and a cross from corner to corner.

    Ports: ``in1`` (west, top) -> ``out1`` (east, bottom) for one fluid and
    ``in2`` (west, bottom) -> ``out2`` (east, top) for the other, so both
    enter from the left and the two passes cross inside.
    """

    name = "plate_heat_exchanger"

    def build(self, params):
        shapes: list[Shape] = [rect(0, 0, 4, 6)]
        for k in range(7):  # the plates
            x = 0.8 + 0.4 * k
            shapes.append(Polyline(((x, 0), (x, 6)), False, False))
        shapes += [
            Polyline(((0, 0), (4, 6)), False, False),
            Polyline(((0, 6), (4, 0)), False, False),
        ]
        ports = {
            "in1": Port("W", 5),
            "out1": Port("E", 1),
            "in2": Port("W", 1),
            "out2": Port("E", 5),
        }
        return Geometry(4, 6, shapes, ports, label_pos="above", nozzles=True)
