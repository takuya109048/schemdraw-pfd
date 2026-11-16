"""Shell & tube heat exchanger (シェルアンドチューブ熱交換器)."""

from __future__ import annotations

from dataclasses import replace

from ..geometry import Ellipse, Polyline, Port, Shape
from .base import ElementSpec, Geometry, even, rotate_ccw


class HeatExchangerSpec(ElementSpec):
    """Shell & tube exchanger with rounded channel heads.

    params: ``length`` (even, default 12), ``diameter`` (even, default 4),
    ``orientation`` (``horizontal``, default, or ``vertical``).

    Ports (horizontal): ``tube_in`` (W), ``tube_out`` (E), ``shell_in`` /
    ``shell_out_n`` (top), ``shell_out`` / ``shell_in_s`` (bottom).
    Standing vertically the ports turn with it: ``tube_in`` at the bottom,
    ``tube_out`` at the top, ``shell_in`` / ``shell_out_n`` on the west face,
    ``shell_out`` / ``shell_in_s`` on the east face.
    """

    name = "heat_exchanger"

    def build(self, params):
        n = even(params.get("length", 12), 8, "heat_exchanger length")
        d = even(params.get("diameter", 4), 2, "heat_exchanger diameter")
        r = d / 2
        shapes: list[Shape] = [
            Polyline(((1, 0), (n - 1, 0))),
            Polyline(((1, d), (n - 1, d))),
            Ellipse(1, r, 1, r, 90, 270),
            Ellipse(n - 1, r, 1, r, -90, 90),
        ]
        for x in (1.8, 2.1, n - 2.1, n - 1.8):  # tube sheets (double lines)
            shapes.append(Polyline(((x, -0.25), (x, d + 0.25)), False, False))
        tubes = max(3, d)
        for i in range(1, tubes + 1):  # tube bundle
            y = d * i / (tubes + 1)
            shapes.append(Polyline(((2.1, y), (n - 2.1, y)), False, False))
        ports = {
            "tube_in": Port("W", d // 2),
            "tube_out": Port("E", d // 2),
            "shell_in": Port("N", 4),
            "shell_out": Port("S", n - 4),
            "shell_in_s": Port("S", 4),
            "shell_out_n": Port("N", n - 4),
        }
        geo = Geometry(n, d, shapes, ports, label_pos="above", nozzles=True)
        if params.get("orientation", "horizontal") == "vertical":
            geo = replace(rotate_ccw(geo), label_pos="left")
        return geo
