"""User-defined element described by plain data (ユーザー定義エレメント)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..geometry import Ellipse, Polyline, Shape, rect
from .base import LABEL_POSITIONS, ElementSpec, Geometry, parse_port


@dataclass
class CustomSpec(ElementSpec):
    """User-defined element described by plain data (e.g. loaded from JSON).

    ``shapes`` items: ``{"type": "rect", "x0":..,"y0":..,"x1":..,"y1":..}``,
    ``{"type": "polyline", "points": [[x,y],..], "closed": false}``,
    ``{"type": "ellipse", "cx","cy","rx","ry"}``,
    ``{"type": "arc", "cx","cy","rx","ry","theta1","theta2"}``.
    Add ``"boundary": false`` for decoration that must not receive ports,
    ``"fill": "black"`` for solid shapes and ``"pipe": true`` for a polyline
    that is a piece of piping (drawn as a line, not a picture, in xlsx).
    ``ports``: ``{"name": {"side": "N|S|E|W", "offset": int}}``.
    ``texts``: ``[[x, y, "text", scale], ...]`` fixed text inside the symbol.
    """

    name: str = "custom"
    width: int = 2
    height: int = 2
    shapes: list[dict] = field(default_factory=list)
    ports: dict[str, dict] = field(default_factory=dict)
    label_pos: str = "center"
    nozzles: bool = False
    texts: list = field(default_factory=list)

    KEYS = tuple("name width height shapes ports label_pos nozzles texts".split())

    @classmethod
    def from_dict(cls, d: dict) -> CustomSpec:
        unknown = set(d) - set(cls.KEYS)
        if unknown:
            raise ValueError(f"unknown keys {sorted(unknown)}")
        if d.get("label_pos", "center") not in LABEL_POSITIONS:
            raise ValueError(f"label_pos must be one of {LABEL_POSITIONS}")
        spec = cls(
            d["name"],
            int(d["width"]),
            int(d["height"]),
            d.get("shapes", []),
            d.get("ports", {}),
            d.get("label_pos", "center"),
            bool(d.get("nozzles", False)),
            d.get("texts", []),
        )
        spec.build({})  # fail now, not at layout time, on a bad shape or port
        return spec

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.KEYS}
        d["ports"] = {
            k: {"side": p.side, "offset": p.offset}
            for k, p in ((k, parse_port(v)) for k, v in self.ports.items())
        }
        return d

    def build(self, params):
        shapes: list[Shape] = []
        for s in self.shapes:
            b, fill = s.get("boundary", True), s.get("fill")
            t = s["type"]
            if t == "rect":
                shapes.append(
                    replace(rect(s["x0"], s["y0"], s["x1"], s["y1"], b), fill=fill)
                )
            elif t == "polyline":
                pts = tuple((float(x), float(y)) for x, y in s["points"])
                pipe = bool(s.get("pipe", False))
                shapes.append(
                    Polyline(pts, bool(s.get("closed", False)), b, fill, pipe)
                )
            elif t in ("ellipse", "arc"):
                th1, th2 = s.get("theta1", 0.0), s.get("theta2", 360.0)
                shapes.append(
                    Ellipse(s["cx"], s["cy"], s["rx"], s["ry"], th1, th2, b, fill)
                )
            else:
                raise ValueError(f"unknown shape type {t!r}")
        ports = {k: parse_port(v) for k, v in self.ports.items()}
        texts = [(float(x), float(y), str(s), float(k)) for x, y, s, k in self.texts]
        return Geometry(
            self.width, self.height, shapes, ports, self.label_pos, texts, self.nozzles
        )
