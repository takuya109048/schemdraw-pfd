"""Off-sheet connector flag (図面間の行き先・行き元を示すフラグ)."""

from __future__ import annotations

from ..geometry import Polyline, Port
from .base import ElementSpec, Geometry, even


class ConnectorSpec(ElementSpec):
    """Off-sheet connector flag (pentagon) with up to two lines of text.

    params: ``text`` (e.g. ``"PID-001\\nfrom P-103"``), ``point`` (``right`` or
    ``left``: the way the flag points, i.e. the flow direction), ``length``
    (even, default 6), ``edge`` (``left`` / ``right``: which edge of the sheet it
    sits on; default: left if it comes before all equipment, otherwise right).
    Ports: ``tip`` (pointed end) and ``tail`` (flat end).

    Flags do not take a column of their own: all flags of one edge share a column
    and are stacked vertically.  ``local`` (``true`` / ``false`` / ``"auto"``,
    the default) instead places the flag right beside the item it serves, like
    equipment; ``"auto"`` does so in the flow layout when only utility lines
    (line kinds that are not ``main``) reach the flag.
    """

    name = "connector"

    def build(self, params):
        n = even(params.get("length", 6), 4, "connector length")
        text = str(params.get("text", ""))
        if params.get("point", "right") == "left":
            pts = ((n, 0), (1, 0), (0, 1), (1, 2), (n, 2))
            ports = {"tip": Port("W", 1), "tail": Port("E", 1)}
            tx = n / 2 + 0.3
        else:
            pts = ((0, 0), (n - 1, 0), (n, 1), (n - 1, 2), (0, 2))
            ports = {"tip": Port("E", 1), "tail": Port("W", 1)}
            tx = n / 2 - 0.3
        return Geometry(
            n, 2, [Polyline(pts, True)], ports, "none", [(tx, 1, text, 0.8)],
            off_sheet=True,
        )  # fmt: skip
