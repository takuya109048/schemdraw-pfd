"""Fired heater (加熱炉)."""

from __future__ import annotations

from ..geometry import Polyline, Port, Shape, rect
from .base import ElementSpec, Geometry, even

CONVECTION = 2  # height of the convection section over the radiant box
STACK = 3  # height of the stack over the convection section


class FurnaceSpec(ElementSpec):
    """Fired heater: a radiant box with burners on its floor, the convection
    section above it and the stack on top.  The process coil enters the
    convection section, runs up and down the radiant box and leaves near its
    floor.

    params: ``width`` (even, default 8, at least 6), ``height`` (default 12,
    at least 10).  Ports: ``in`` (west, convection section), ``out`` (east,
    low in the radiant box), ``fuel`` (bottom centre, the burners) and
    ``flue`` (top of the stack).
    """

    name = "furnace"

    def build(self, params):
        w = even(params.get("width", 8), 6, "furnace width")
        h = int(params.get("height", 12))
        if h < 10:
            raise ValueError("furnace height must be >= 10")
        box = h - CONVECTION - STACK  # top of the radiant box
        c = w / 2
        shapes: list[Shape] = [
            rect(0, 0, w, box),
            Polyline(((0, box), (1, box + CONVECTION), (w - 1, box + CONVECTION))),
            Polyline(((w - 1, box + CONVECTION), (w, box))),
            rect(c - 1, box + CONVECTION, c + 1, h),  # stack
        ]
        # the coil: across the convection section, along the radiant box's
        # ceiling, then up and down the box from left to right
        coil = [(0.5, box + 1), (w - 1.2, box + 1), (w - 1.2, box - 0.5)]
        coil += [(1, box - 0.5)]
        for x in range(1, w):
            top = box - 0.5 if x == 1 else box - 1.3
            down = x % 2 == 1
            ys = (top, 1.8) if down else (1.8, top)
            coil += [(x, ys[0]), (x, ys[1])]
        coil[-1] = (w - 1, 1)
        coil.append((w, 1))
        shapes.append(Polyline(tuple(coil), False, False))
        for x in (c - 1.5, c, c + 1.5):  # burner flames
            shapes.append(
                Polyline(((x - 0.3, 0), (x, 0.9), (x + 0.3, 0)), False, False)
            )
        ports = {
            "in": Port("W", box + 1),
            "out": Port("E", 1),
            "fuel": Port("S", w // 2),
            "flue": Port("N", w // 2),
        }
        return Geometry(w, h, shapes, ports, label_pos="left", nozzles=True)
