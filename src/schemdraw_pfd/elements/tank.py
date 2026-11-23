"""Storage tank (貯槽)."""

from __future__ import annotations

from ..geometry import Polyline, Port, Shape
from .base import ElementSpec, Geometry, even, shell_ports

ROOF = 1  # rise of the cone roof


class TankSpec(ElementSpec):
    """Flat-bottomed storage tank with a cone roof.

    params: ``width`` (even, default 8), ``height`` (default 8, at least 4,
    the roof included).  Ports: ``top`` (roof apex), ``bottom`` (centre of
    the bottom), ``n<i>`` / ``s<i>`` on every inner grid column of the roof /
    bottom (left to right; the centre one is ``top`` / ``bottom``), and
    ``w<i>`` / ``e<i>`` on every grid row of the shell above the bottom
    (``w_low``, ``w_mid``, ``w_high``, ... aliases).  Lines on the roof or
    bottom nozzles may be swapped to avoid crossings.
    """

    name = "tank"

    def build(self, params):
        w = even(params.get("width", 8), 4, "tank width")
        h = int(params.get("height", 8))
        if h < 4:
            raise ValueError("tank height must be >= 4")
        eave = h - ROOF
        shapes: list[Shape] = [
            Polyline(((0, eave), (0, 0), (w, 0), (w, eave))),
            Polyline(((0, eave), (w / 2, h), (w, eave))),  # cone roof
        ]
        ports = {"top": Port("N", w // 2), "bottom": Port("S", w // 2)}
        for i, x in enumerate(range(1, w)):
            ports[f"n{i}"] = Port("N", x)
            ports[f"s{i}"] = Port("S", x)
        ports |= shell_ports(list(range(1, eave)))
        groups = [[f"n{i}" for i in range(w - 1)], [f"s{i}" for i in range(w - 1)]]
        return Geometry(w, h, shapes, ports, nozzles=True, swappable=groups)
