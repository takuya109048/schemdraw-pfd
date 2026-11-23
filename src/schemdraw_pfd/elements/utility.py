"""Utility supply (ユーティリティの供給元): a circle with its abbreviation."""

from __future__ import annotations

from ..geometry import Ellipse, Polyline, Port
from .base import ElementSpec, Geometry, Text

POINTS = ("right", "left", "up", "down")
FIT = 3  # characters that fit in the circle at full size
LEAD = 4  # default cells of line between the circle and the port


class UtilitySpec(ElementSpec):
    """Where a utility (steam, nitrogen, cooling water ...) comes in: a
    circle (2x2) with the utility's abbreviation in it, e.g. ``STM`` or
    ``N2``, and a lead of line from the circle to the port, so the circle
    always stands well off the item it serves (and is not taken for an
    instrument bubble).  Utility lines start here; returns (condensate,
    cooling water return) still go out to a ``connector`` flag.

    params: ``text`` (the abbreviation; longer text is drawn smaller to fit),
    ``point`` (``right`` / ``left`` / ``up`` / ``down``: the way the line
    leaves; left out, it faces the port its one line goes to -- straight
    down onto a top nozzle, left into an east one ... -- and ``right`` when
    it has several lines), ``lead`` (cells of line between the circle
    and the port, default 4).  Port: ``out``, at the end of the lead.

    It never takes a sheet edge: it stands right beside the item it serves,
    like a flag with ``local: true``.
    """

    name = "utility"
    aims = True

    def build(self, params):
        point = params.get("point", "right")
        if point not in POINTS:
            raise ValueError(f"utility point must be one of {list(POINTS)}")
        lead = int(params.get("lead", LEAD))
        if lead < 1:
            raise ValueError(f"utility lead must be at least 1, got {lead}")
        text = str(params.get("text", ""))
        size = 0.75 * min(1.0, FIT / max(len(text), 1))
        n = 2 + lead  # the long side: circle and lead
        # circle centre, lead (from the circle to the port), port, box size
        if point == "right":
            c, a, b, port, w, h = (1, 1), (2, 1), (n, 1), Port("E", 1), n, 2
        elif point == "left":
            c, a, b, port, w, h = (n - 1, 1), (n - 2, 1), (0, 1), Port("W", 1), n, 2
        elif point == "up":
            c, a, b, port, w, h = (1, 1), (1, 2), (1, n), Port("N", 1), 2, n
        else:
            c, a, b, port, w, h = (1, n - 1), (1, n - 2), (1, 0), Port("S", 1), 2, n
        texts: list[Text] = [(c[0], c[1], text, size)]
        shapes = [
            Ellipse(c[0], c[1], 1, 1),
            Polyline((a, b), pipe=True, line="utility"),
        ]
        return Geometry(
            w, h, shapes, {"out": port}, "none", texts, off_sheet=True, beside=True
        )
