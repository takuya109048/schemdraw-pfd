"""Instrument bubble (計器)."""

from __future__ import annotations

from ..geometry import Ellipse, Port, Shape, rect
from .base import ElementSpec, Geometry, Text


class InstrumentSpec(ElementSpec):
    """Instrument bubble (2x2).

    params:
      function: ISA style letters, e.g. ``PG``, ``TI``, ``FIC``, ``LIC``
      number: loop number, e.g. ``201``
      mount: ``field`` (plain circle: local gauge / field instrument, default) or
          ``dcs`` (circle inside a square: shared display / control in the DCS)
    Ports ``n``, ``s``, ``e``, ``w`` sit on the outline (circle or square).
    """

    name = "instrument"

    def build(self, params):
        mount = params.get("mount", "field")
        if mount not in ("field", "dcs"):
            raise ValueError("instrument mount must be 'field' or 'dcs'")
        dcs = mount == "dcs"
        shapes: list[Shape] = [Ellipse(1, 1, 1, 1, boundary=not dcs)]
        if dcs:
            shapes.append(rect(0, 0, 2, 2))
        texts: list[Text] = [
            (1, 1.38, str(params.get("function", "")), 0.75),
            (1, 0.62, str(params.get("number", "")), 0.75),
        ]
        ports = {s.lower(): Port(s, 1) for s in "NSEW"}
        return Geometry(2, 2, shapes, ports, "none", texts)
