"""Utility supply (ユーティリティの供給元): a circle with its abbreviation."""

from __future__ import annotations

from ..geometry import Ellipse, Port
from .base import ElementSpec, Geometry

SIDES = {"right": "E", "left": "W", "up": "N", "down": "S"}
FIT = 3  # characters that fit in the circle at full size


class UtilitySpec(ElementSpec):
    """Where a utility (steam, nitrogen, cooling water ...) comes in: a
    circle (2x2) with the utility's abbreviation in it, e.g. ``STM`` or
    ``N2``.  Utility lines start here; returns (condensate, cooling water
    return) still go out to a ``connector`` flag.

    params: ``text`` (the abbreviation; longer text is drawn smaller to fit),
    ``point`` (``right`` / ``left`` / ``up`` / ``down``: the side the line
    leaves from, default ``right``).  Port: ``out``.

    It never takes a sheet edge: it stands right beside the item it serves,
    like a flag with ``local: true``.
    """

    name = "utility"

    def build(self, params):
        point = params.get("point", "right")
        if point not in SIDES:
            raise ValueError(f"utility point must be one of {sorted(SIDES)}")
        text = str(params.get("text", ""))
        size = 0.75 * min(1.0, FIT / max(len(text), 1))
        return Geometry(
            2, 2, [Ellipse(1, 1, 1, 1)], {"out": Port(SIDES[point], 1)}, "none",
            [(1, 1, text, size)], off_sheet=True, beside=True,
        )  # fmt: skip
