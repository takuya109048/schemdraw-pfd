"""Filter housing (フィルターハウジング)."""

from __future__ import annotations

from ..geometry import Polyline, Port, Shape, rect
from .base import ElementSpec, Geometry, even

INSET = 0.25  # the shell's walls stand this far inside the flange plates
NECK_WIDTH = 0.225  # the neck's width, a share of the housing's width
CAP_OVERHANG = 0.2  # the cap plate reaches this far past the neck
CARTRIDGE_HEIGHT = 2 / 3  # the cartridges' height, a share of the shell's


class FilterHousingSpec(ElementSpec):
    """Cartridge-filter housing: a straight shell between two flange plates,
    a narrow neck with a cap on top, the cartridges standing inside.

    Drawn as a rectangular shell whose bottom plate and top flange reach a
    little past its walls, a neck in the middle of the top flange closed by a
    cap plate, and the cartridges as tall rectangles standing on the bottom
    plate, two thirds of the shell high (one per two cells of width).

    params: ``width`` (even, default 4), ``height`` (default 9: the shell
    two thirds of it, the neck the rest).  Ports: ``in`` (west side, low),
    ``out`` (east side, high), ``vent`` (top of the neck), ``drain`` (middle of
    the bottom plate), and ``w<i>`` / ``e<i>`` on every grid row of the shell
    (``w_low``, ``w_mid``, ``w_high``, ... aliases) so a line can reach either
    side wherever it is convenient.
    """

    name = "filter_housing"
    defaults = {"width": 4, "height": 9}
    label_pos = "above"

    def build(self, params):
        p = {**self.defaults, **params}
        w = even(p["width"], 4, f"{self.name} width")
        h = int(p["height"])
        if h < 6:
            raise ValueError(f"{self.name} height must be >= 6")
        r = w / 2
        neck = max(2, h // 3)
        top = h - neck  # the top flange; the neck stands above it
        a = INSET
        half = w * NECK_WIDTH / 2
        shapes: list[Shape] = [
            Polyline(((0, 0), (w, 0))),  # bottom plate
            Polyline(((a, 0), (a, top))),  # shell walls
            Polyline(((w - a, 0), (w - a, top))),
            Polyline(((0, top), (w, top))),  # top flange
            Polyline(((r - half, top), (r - half, h))),  # neck
            Polyline(((r + half, top), (r + half, h))),
            Polyline(((r - half - CAP_OVERHANG, h), (r + half + CAP_OVERHANG, h))),
        ]
        # cartridges on the bottom plate: the shell's inside cut into equal
        # slots, cartridges and the room around them taking turns
        n = w // 2
        slot = (w - 2 * a) / (2 * n + 1)
        y1 = top * CARTRIDGE_HEIGHT
        for k in range(n):
            x0 = a + (2 * k + 1) * slot
            shapes.append(rect(x0, 0, x0 + slot, y1, boundary=False))

        rows = list(range(1, top))
        ports = {"vent": Port("N", w // 2), "drain": Port("S", w // 2)}
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
