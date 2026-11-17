"""Horizontal drum (横型槽)."""

from __future__ import annotations

from ..geometry import Ellipse, Polyline, Port, Shape
from .base import ElementSpec, Geometry, even


class HorizontalDrumSpec(ElementSpec):
    """Horizontal vessel with rounded heads.

    params: ``length`` (even, default 8), ``diameter`` (even, default 4).
    Ports: ``w``, ``e`` (head apexes), ``n<i>`` / ``s<i>`` on every grid column of
    the shell (``n_mid`` / ``s_mid`` aliases); lines on one side may be swapped
    to avoid crossings.
    """

    name = "horizontal_drum"

    def build(self, params):
        n = even(params.get("length", 8), 4, "horizontal_drum length")
        d = even(params.get("diameter", 4), 2, "horizontal_drum diameter")
        r, head = d // 2, max(1, d // 4)
        if n < 2 * head + 2:
            raise ValueError("horizontal_drum is too short")
        shapes: list[Shape] = [
            Polyline(((head, 0), (n - head, 0))),
            Polyline(((head, d), (n - head, d))),
            Ellipse(head, r, head, r, 90, 270),
            Ellipse(n - head, r, head, r, -90, 90),
        ]
        ports = {"w": Port("W", r), "e": Port("E", r)}
        cols = list(range(head, n - head + 1))
        for i, x in enumerate(cols):
            ports[f"n{i}"] = Port("N", x)
            ports[f"s{i}"] = Port("S", x)
        ports["n_mid"] = ports[f"n{len(cols) // 2}"]
        ports["s_mid"] = ports[f"s{len(cols) // 2}"]
        groups = [
            [f"n{i}" for i in range(len(cols))],
            [f"s{i}" for i in range(len(cols))],
        ]
        return Geometry(n, d, shapes, ports, nozzles=True, swappable=groups)
