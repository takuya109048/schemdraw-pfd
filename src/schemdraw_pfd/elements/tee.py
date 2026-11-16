"""Tee (配管の分岐点)."""

from __future__ import annotations

from ..geometry import Polyline, Port, Shape
from .base import ElementSpec, Geometry


class TeeSpec(ElementSpec):
    """Pipe junction (2x2) so a line can branch or an instrument can tap a pipe.

    params: ``arms`` - which arms exist, any of ``n s e w`` (default ``"wen"``).
    Ports are named after the arms: ``w``, ``e``, ``n``, ``s``.
    """

    name = "tee"

    def build(self, params):
        arms = str(params.get("arms", "wen")).lower()
        ends = {"w": (0, 1), "e": (2, 1), "n": (1, 2), "s": (1, 0)}
        if not arms or set(arms) - set(ends):
            raise ValueError("tee arms must be a combination of n, s, e, w")
        # plain lines meeting: no junction dot (dots are not used on this sheet)
        shapes: list[Shape] = [Polyline(((1, 1), ends[a])) for a in arms]
        ports = {a: Port(a.upper(), 1) for a in arms}
        return Geometry(2, 2, shapes, ports, label_pos="none")
