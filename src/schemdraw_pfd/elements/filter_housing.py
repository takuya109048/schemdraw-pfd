"""Filter housing (フィルターハウジング)."""

from __future__ import annotations

from ..geometry import Ellipse, Polyline, Port, Shape, rect
from .base import ElementSpec, Geometry, even


class FilterHousingSpec(ElementSpec):
    """Metal cartridge-filter housing: a pressure shell with a bolted top cover.

    Drawn as a cylindrical shell with a dished bottom head, a flange pair at the
    top (the cover is bolted on so the cartridges can be pulled out) and a domed
    cover above it.  The cartridge inside is shown as a pleated element.

    params: ``width`` (diameter, even, default 4), ``height`` (default 8).
    Ports: ``in`` (west side, low), ``out`` (east side, high), ``vent`` (top of
    the cover), ``drain`` (bottom apex), and ``w<i>`` / ``e<i>`` on every grid row
    of the shell below the flange (``w_low``, ``w_mid``, ``w_high``, ... aliases)
    so a line can reach either side wherever it is convenient.
    """

    name = "filter_housing"
    defaults = {"width": 4, "height": 8}
    label_pos = "left"

    def build(self, params):
        p = {**self.defaults, **params}
        w = even(p["width"], 4, f"{self.name} width")
        h = int(p["height"])
        if h < 6:
            raise ValueError(f"{self.name} height must be >= 6")
        r = w // 2
        lo = 1  # bottom head depth
        flange = h - 2  # bottom face of the flange pair
        top = h - 1  # top face of the flange pair; the cover dome sits above
        shapes: list[Shape] = [
            Polyline(((0, lo), (0, flange))),
            Polyline(((w, lo), (w, flange))),
            Ellipse(r, lo, r, lo, 180, 360),  # dished bottom head
            rect(0, flange, w, top),  # flange pair (bolted cover joint)
            Polyline(((0, flange + 0.5), (w, flange + 0.5)), False, False),
            Ellipse(r, top, r, 1, 0, 180),  # domed cover
        ]
        # pleated cartridge inside the shell
        cx0, cx1 = 1.0, w - 1.0
        cy0, cy1 = lo + 0.5, flange - 0.5
        shapes.append(rect(cx0, cy0, cx1, cy1, boundary=False))
        pleats = 2 * (w - 2) + 1
        step = (cx1 - cx0) / pleats
        zig = [(cx0 + i * step, cy0 if i % 2 == 0 else cy1) for i in range(pleats + 1)]
        shapes.append(Polyline(tuple(zig), False, False))

        rows = list(range(lo, flange + 1))
        ports = {"vent": Port("N", r), "drain": Port("S", r)}
        for i, y in enumerate(rows):
            ports[f"w{i}"] = Port("W", y)
            ports[f"e{i}"] = Port("E", y)
        for side in "we":
            ports[f"{side}_low"] = ports[f"{side}0"]
            ports[f"{side}_mid"] = ports[f"{side}{len(rows) // 2}"]
            ports[f"{side}_high"] = ports[f"{side}{len(rows) - 1}"]
        ports["in"] = ports["w_low"] if len(rows) < 3 else ports["w1"]
        ports["out"] = ports["e_high"]
        return Geometry(w, h, shapes, ports, label_pos=self.label_pos, nozzles=True)
