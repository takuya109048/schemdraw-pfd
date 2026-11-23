"""Agitated vessel / reactor (撹拌槽・反応器)."""

from __future__ import annotations

from ..geometry import Ellipse, Polyline, Port, Shape
from .base import ElementSpec, Geometry, Text, even, shell_ports

DRIVE = 3  # cells above the top head: the motor on its shaft
MOTOR = 0.9  # radius of the motor circle
JACKET = 0.4  # gap between the shell and the jacket


class ReactorSpec(ElementSpec):
    """Vertical vessel with an agitator driven by a motor on the top head,
    optionally with a heating / cooling jacket.

    params: ``width`` (even, default 6), ``height`` (the vessel, default 10;
    the motor adds 3 cells above it), ``jacket`` (default false: a jacket
    round the bottom head and the lower two thirds of the shell).

    Ports: ``bottom`` (bottom head apex), ``n<i>`` / ``s<i>`` on the inner
    grid columns of the top / bottom head (left to right; the top head has
    none on the centre column, where the shaft is), ``w<i>`` / ``e<i>`` on
    every grid row of the shell (``w_low``, ``w_mid``, ``w_high``, ...
    aliases).  With a jacket: ``jacket_in`` (west, at the bottom of the
    shell) and ``jacket_out`` (east, at the top of the jacket); shell
    nozzles in the jacketed part are drawn through the jacket.
    """

    name = "reactor"

    def build(self, params):
        w = even(params.get("width", 6), 4, "reactor width")
        h = int(params.get("height", 10))
        jacket = bool(params.get("jacket", False))
        r = w // 2
        head = max(1, min(r, w // 3 if w > 4 else r))
        if h < 2 * head + 3:
            raise ValueError(f"reactor height must be >= {2 * head + 3}")
        lo, hi = head, h - head
        a = JACKET if jacket else 0.0
        shapes: list[Shape] = [
            Polyline(((a, lo), (a, hi))),
            Polyline(((w - a, lo), (w - a, hi))),
            Ellipse(r, hi, r - a, head, 0, 180),
            Ellipse(r, lo, r - a, head - a, 180, 360),
        ]
        ports = {"bottom": Port("S", r)}
        top = lo + max(2, (hi - lo) * 2 // 3)  # highest row in the jacket
        if jacket:
            j = top + 0.5
            shapes += [
                Polyline(((a, j), (0, j - a), (0, lo))),
                Polyline(((w - a, j), (w, j - a), (w, lo))),
                Ellipse(r, lo, r, head, 180, 360),
            ]
        # the agitator: motor, shaft and a two-bladed propeller low in the
        # vessel
        shapes += [
            Ellipse(r, h + DRIVE - MOTOR - 0.1, MOTOR, MOTOR),
            Polyline(((r, h + DRIVE - 2 * MOTOR - 0.1), (r, lo + 0.5)), False, False),
            Ellipse(r - 0.6, lo + 0.5, 0.6, 0.18, boundary=False),
            Ellipse(r + 0.6, lo + 0.5, 0.6, 0.18, boundary=False),
        ]
        texts: list[Text] = [(r, h + DRIVE - MOTOR - 0.1, "M", 0.7)]
        for i, x in enumerate(range(1, w)):
            if x != r:
                ports[f"n{i}"] = Port("N", x)
            ports[f"s{i}"] = Port("S", x)
        ports |= shell_ports(list(range(lo, hi + 1)))
        if jacket:
            ports["jacket_in"] = Port("W", lo)
            ports["jacket_out"] = Port("E", top)
        heads = [
            [f"n{i}" for i in range(w - 1) if i + 1 != r],
            [f"s{i}" for i in range(w - 1)],
        ]
        return Geometry(
            w, h + DRIVE, shapes, ports, label_pos="left", texts=texts,
            nozzles=True, swappable=heads,
        )  # fmt: skip
