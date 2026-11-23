"""Filter housing (フィルターハウジング)."""

from __future__ import annotations

from ..geometry import Ellipse, Polyline, Port, Shape, rect
from .base import ElementSpec, Geometry, even, shell_ports

INSET = 0.25  # the shell's walls stand this far inside the element's box
LEGS = 2  # cells of legs under the bottom head
HEAD = 1  # depth of the dished bottom head and of the lid
LOWER = 2  # the filtrate chamber: from the bottom head up to the partition
BAND = 0.15  # half the thickness of the V-band clamp, and its overhang
FOOT = 0.7  # width of a leg's foot plate
LEG_INSET = 0.35  # the legs stand this far inside the element's box
CARTRIDGE_GAP = 0.8  # room between the cartridges' tops and the lid joint


class FilterHousingSpec(ElementSpec):
    """Cartridge-filter housing on legs, its lid held by a V-band clamp.

    Drawn after the usual vertical housing: a tall cylindrical shell with a
    dished bottom head, a dished lid clamped to the shell by a V-band, and
    legs with foot plates under the bottom head.  A partition plate low in
    the shell splits it into the filtrate chamber below and the raw-liquid
    chamber above, where the cartridges stand on the plate (one per two
    cells of width).  The raw liquid enters through the side just above the
    partition, passes through the cartridges and down through the plate,
    and leaves through the side of the chamber below it.

    params: ``width`` (even, default 4), ``height`` (default 10, at least 9:
    two cells of legs, a one-cell bottom head, a two-cell filtrate chamber,
    the cartridge chamber and a one-cell lid).  Ports: ``in`` (west side,
    just above the partition), ``out`` (east side, filtrate chamber), ``vent``
    (top of the lid), ``drain`` (bottom of the bottom head, between the
    legs), and ``w<i>`` / ``e<i>`` on every grid row of the shell (``w_low``,
    ``w_mid``, ``w_high``, ... aliases) so a line can reach either side
    wherever it is convenient (``e<i>`` just above the partition: the raw
    side's drain).
    """

    name = "filter_housing"
    defaults = {"width": 4, "height": 10}
    label_pos = "above"

    def build(self, params):
        p = {**self.defaults, **params}
        w = even(p["width"], 4, f"{self.name} width")
        h = int(p["height"])
        if h < 9:
            raise ValueError(f"{self.name} height must be >= 9")
        r = w / 2
        a = INSET
        bottom = LEGS + HEAD  # where the bottom head meets the shell
        plate = bottom + LOWER  # the partition plate
        joint = h - HEAD  # where the lid meets the shell (the V-band)
        shapes: list[Shape] = [
            Polyline(((a, bottom), (a, joint))),  # shell walls
            Polyline(((w - a, bottom), (w - a, joint))),
            Ellipse(r, bottom, r - a, HEAD, 180, 360),  # dished bottom head
            Ellipse(r, joint, r - a, HEAD, 0, 180),  # dished lid
            # the V-band clamp around the joint
            rect(a - BAND, joint - BAND, w - a + BAND, joint + BAND, boundary=False),
            Polyline(((a, plate), (w - a, plate)), boundary=False),  # partition
        ]
        for x in (LEG_INSET, w - LEG_INSET):  # legs on foot plates
            shapes.append(Polyline(((x, 0), (x, bottom))))
            x0 = min(max(x - FOOT / 2, 0), w - FOOT)
            shapes.append(Polyline(((x0, 0), (x0 + FOOT, 0))))
        # cartridges on the partition: the shell's inside cut into equal
        # slots, cartridges and the room around them taking turns
        n = w // 2
        slot = (w - 2 * a) / (2 * n + 1)
        for k in range(n):
            x0 = a + (2 * k + 1) * slot
            shapes.append(
                rect(x0, plate, x0 + slot, joint - CARTRIDGE_GAP, boundary=False)
            )

        ports = {"vent": Port("N", w // 2), "drain": Port("S", w // 2)}
        ports |= shell_ports(list(range(bottom + 1, joint)))
        ports["in"] = Port("W", plate + 1)  # raw liquid, above the partition
        ports["out"] = Port("E", plate - 1)  # filtrate, below it
        return Geometry(w, h, shapes, ports, label_pos=self.label_pos, nozzles=True)
