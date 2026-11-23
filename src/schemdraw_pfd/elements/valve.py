"""Valve (弁): gate, globe, ball, butterfly, three-way, control and check valves."""

from __future__ import annotations

from ..geometry import Ellipse, Polyline, Port, Shape, rect
from .base import ElementSpec, Geometry, rotate_ccw


class ValveSpec(ElementSpec):
    """Valve in a 2x2 box.

    params:
      kind: ``gate`` (plain bow-tie, default), ``globe`` (bow-tie with a solid
          dot), ``ball`` (two triangles with a circle between their tips),
          ``butterfly`` (a box with its diagonal and a dot: the disc), ``three_way`` (bow-tie with a
          third leg down; extra ``branch`` port at the bottom), ``control``
          (bow-tie with a dome actuator; extra ``signal`` port on the actuator)
          or ``check`` (Z-shaped check valve).
      orientation: ``horizontal`` (ports W/E, default) or ``vertical`` (ports S/N).
    """

    name = "valve"
    KINDS = ("gate", "globe", "ball", "butterfly", "three_way", "control", "check")

    def build(self, params):
        kind = params.get("kind", "gate")
        if kind not in self.KINDS:
            raise ValueError(f"valve kind must be one of {self.KINDS}, got {kind!r}")
        ports = {"in": Port("W", 1), "out": Port("E", 1)}
        if kind == "check":
            z = ((0, 1), (0.6, 1), (0.6, 1.5), (1.4, 0.5), (1.4, 1), (2, 1))
            shapes: list[Shape] = [Polyline(z)]
        elif kind == "butterfly":  # the disc: a box with its diagonal and pivot
            shapes = [
                rect(0, 0.5, 2, 1.5),
                Polyline(((0, 0.5), (2, 1.5)), boundary=False),
                Ellipse(1, 1, 0.16, 0.16, boundary=False, fill="black"),
            ]
        elif kind == "ball":  # two triangles with the ball between their tips
            shapes = [
                Polyline(((0, 0.5), (0, 1.5), (0.7, 1)), True),
                Polyline(((2, 0.5), (2, 1.5), (1.3, 1)), True),
                Ellipse(1, 1, 0.3, 0.3, boundary=False),
            ]
        else:
            shapes = [Polyline(((0, 0.5), (0, 1.5), (2, 0.5), (2, 1.5)), True)]
        if kind == "globe":
            shapes.append(Ellipse(1, 1, 0.22, 0.22, boundary=False, fill="black"))
        if kind == "three_way":
            shapes.append(Polyline(((0.5, 0), (1.5, 0), (1, 1)), True))
            ports["branch"] = Port("S", 1)
        if kind == "control":
            shapes += [
                Polyline(((1, 1), (1, 1.6)), False, False),
                Polyline(((0.5, 1.6), (1.5, 1.6))),
                Ellipse(1, 1.6, 0.5, 0.4, 0, 180),
            ]
            ports["signal"] = Port("N", 1)
        geo = Geometry(2, 2, shapes, ports, label_pos="none")
        if params.get("orientation", "horizontal") == "vertical":
            geo = rotate_ccw(geo)
        return geo
