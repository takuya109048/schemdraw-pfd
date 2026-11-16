"""Vertical drum (縦型ドラム)."""

from __future__ import annotations

from ..geometry import Ellipse, Polyline, Port, Shape, rect
from .base import ElementSpec, Geometry, even


class VerticalDrumSpec(ElementSpec):
    """Vertical vessel with rounded heads.

    params: ``width`` (diameter, even, default 6), ``height`` (default 10),
    ``beds`` (number of packed beds drawn as crossed boxes, default 0).
    Ports: ``top``, ``bottom`` (head apexes), ``n<i>`` / ``s<i>`` on every inner
    grid column of the top / bottom head (left to right; the centre one is
    ``top`` / ``bottom``), so several lines can reach a head through nozzles of
    their own, and ``w<i>`` / ``e<i>`` on every grid row of the cylindrical
    shell (``w_low``, ``w_mid``, ``w_high``, ... aliases).
    """

    name = "vertical_drum"
    defaults = {"width": 6, "height": 10, "beds": 0}
    label_pos = "center"

    def build(self, params):
        p = {**self.defaults, **params}
        w = even(p["width"], 2, f"{self.name} width")
        h, beds = int(p["height"]), int(p["beds"])
        r = w // 2
        head = max(1, min(r, w // 3 if w > 4 else r))  # head depth
        if h < 2 * head + 1:
            raise ValueError(f"{self.name} height must be >= {2 * head + 1}")
        lo, hi = head, h - head
        shapes: list[Shape] = [
            Polyline(((0, lo), (0, hi))),
            Polyline(((w, lo), (w, hi))),
            Ellipse(r, hi, r, head, 0, 180),
            Ellipse(r, lo, r, head, 180, 360),
        ]
        if beds:
            gap = 1.0
            bed_h = (hi - lo - gap * (beds + 1)) / beds
            if bed_h <= 0:
                raise ValueError(f"{self.name} is too short for {beds} beds")
            for i in range(beds):
                y0 = lo + gap + i * (bed_h + gap)
                y1 = y0 + bed_h
                shapes += [
                    rect(0, y0, w, y1, boundary=False),
                    Polyline(((0, y0), (w, y1)), False, False),
                    Polyline(((0, y1), (w, y0)), False, False),
                ]
        ports = {"top": Port("N", r), "bottom": Port("S", r)}
        # nozzles on the heads, one per inner grid column (top = the centre one)
        for i, x in enumerate(range(1, w)):
            ports[f"n{i}"] = Port("N", x)
            ports[f"s{i}"] = Port("S", x)
        rows = list(range(lo, hi + 1))
        for i, y in enumerate(rows):
            ports[f"w{i}"] = Port("W", y)
            ports[f"e{i}"] = Port("E", y)
        for side in "we":
            ports[f"{side}_low"] = ports[f"{side}0"]
            ports[f"{side}_mid"] = ports[f"{side}{len(rows) // 2}"]
            ports[f"{side}_high"] = ports[f"{side}{len(rows) - 1}"]
        return Geometry(w, h, shapes, ports, label_pos=self.label_pos, nozzles=True)
