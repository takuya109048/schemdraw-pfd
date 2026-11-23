"""What the user describes: equipment, the lines between them, inline items."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .elements import DEFAULT_LIBRARY, ElementLibrary, Geometry, parse_port

ANCHORS = ("auto", "src", "dst")


def split_ref(ref: str) -> tuple[str, str]:
    """``"ID.port"`` -> ``("ID", "port")`` (the id may not contain a dot)."""
    if "." not in ref:
        raise ValueError(f"connection end {ref!r} must look like 'ID.port'")
    i, p = ref.split(".", 1)
    return i, p


def jsonable(v: Any) -> Any:
    """Plain JSON data (tuples -> lists, dict keys -> str)."""
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    return v


def _changed(d: dict[str, Any], *fields: tuple[str, Any, Any]) -> dict[str, Any]:
    """Add each ``(key, value, default)`` to ``d`` unless it is the default."""
    for key, value, default in fields:
        if value != default:
            d[key] = value
    return d


def check_id(id: str) -> None:
    if not id or "." in id:
        raise ValueError(f"id {id!r} must be non-empty and contain no '.'")


@dataclass
class Equipment:
    id: str
    type: str
    level: int | None = None  # None: automatic (see :mod:`.levels`)
    params: dict[str, Any] = field(default_factory=dict)
    ports: dict[str, Any] = field(default_factory=dict)  # extra user ports
    desc: str = ""  # spec text shown in the top (bottom for pumps) row
    elevation: int | None = None  # cells above the level floor (None = automatic)
    span: int = 1  # levels a tall item (a tower) stands through

    @property
    def tier(self) -> int:
        """The level, once worked out (the layout never sees None)."""
        if self.level is None:
            raise ValueError(f"{self.id}: level not worked out yet")
        return self.level

    def geometry(self, library: ElementLibrary | None = None) -> Geometry:
        geo = (library or DEFAULT_LIBRARY).build(self.type, self.params)
        for name, spec in self.ports.items():
            geo.ports[name] = parse_port(spec)
        return geo

    def to_dict(self) -> dict[str, Any]:
        ports = {k: parse_port(v) for k, v in self.ports.items()}
        return _changed(
            {"id": self.id, "type": self.type},
            ("level", self.level, None),
            ("elevation", self.elevation, None),
            ("span", self.span, 1),
            ("desc", self.desc, ""),
            ("ports", {k: [p.side, p.offset] for k, p in ports.items()}, {}),
            ("params", jsonable(self.params), {}),
        )


@dataclass
class InlineItem:
    """Equipment that sits on a line (valve, flow meter, strainer ...).

    It does not need its own entry in the equipment order: it is placed next to
    one end of its line (``Connection.anchor``) on that end's level, and the line
    runs ``src -> item.in -> item.out -> ... -> dst``.  ``level`` / ``elevation``
    override the automatic placement.  When the line flows right to left the
    item is mirrored automatically (unless ``params`` says ``mirror``).
    """

    type: str
    id: str | None = None  # None: generated, not shown as a tag
    params: dict[str, Any] = field(default_factory=dict)
    level: int | None = None
    elevation: int | None = None
    desc: str = ""
    port_in: str = "in"
    port_out: str = "out"

    KEYS = ("type", "id", "params", "level", "elevation", "desc", "in", "out")

    @classmethod
    def parse(cls, v: Any) -> InlineItem:
        """Accept an ``InlineItem``, a type name or a dict (JSON form)."""
        if isinstance(v, InlineItem):
            return v
        if isinstance(v, str):
            return cls(v)
        if not isinstance(v, dict):
            raise TypeError(f"inline item must be a type name or a dict, got {v!r}")
        unknown = set(v) - set(cls.KEYS)
        if unknown:
            raise ValueError(f"unknown inline item keys {sorted(unknown)}")
        item = cls(
            v["type"],
            v.get("id"),
            dict(v.get("params", {})),
            v.get("level"),
            v.get("elevation"),
            v.get("desc", ""),
            v.get("in", "in"),
            v.get("out", "out"),
        )
        if item.id is not None:
            check_id(item.id)
        return item

    def to_dict(self) -> dict[str, Any] | str:
        d = _changed(
            {"type": self.type},
            ("id", self.id, None),
            ("params", jsonable(self.params), {}),
            ("level", self.level, None),
            ("elevation", self.elevation, None),
            ("desc", self.desc, ""),
            ("in", self.port_in, "in"),
            ("out", self.port_out, "out"),
        )
        return self.type if len(d) == 1 else d


@dataclass
class Connection:
    src: str  # "ID.port" (line start)
    dst: str  # "ID.port" (line end)
    kind: str = "pipe"  # a line kind of the flowsheet (pipe / signal / instrument ...)
    inline: list[InlineItem] = field(default_factory=list)
    anchor: str = "auto"  # which end inline items sit next to: auto | src | dst
    recycle: bool = False  # runs against the main flow (layout="flow")

    @property
    def ends(self) -> tuple[tuple[str, str], tuple[str, str]]:
        """``(id, port)`` of each end (split once, while src and dst stay)."""
        held = self.__dict__.get("_ends")
        if held is None or held[0] is not self.src or held[1] is not self.dst:
            ends = split_ref(self.src), split_ref(self.dst)
            held = self.__dict__["_ends"] = (self.src, self.dst, ends)
        return held[2]

    def to_dict(self) -> dict[str, Any]:
        return _changed(
            {"from": self.src, "to": self.dst},
            ("type", self.kind, "pipe"),
            ("inline", [i.to_dict() for i in self.inline], []),
            ("anchor", self.anchor, "auto"),
            ("recycle", self.recycle, False),
        )


REVISION_COLORS = ("red", "blue", "green", "magenta", "darkorange", "purple")


@dataclass
class Change:
    """An item on the sheet changed by a revision: another ``type``, its
    ``params`` (merged into those it has) or ``desc`` (None: as it is)."""

    id: str
    type: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    desc: str | None = None

    KEYS = ("id", "type", "params", "desc")

    def to_dict(self) -> dict[str, Any]:
        return _changed(
            {"id": self.id},
            ("type", self.type, None),
            ("params", jsonable(self.params), {}),
            ("desc", self.desc, None),
        )


@dataclass
class Revision:
    """A change drawn onto a finished sheet (see :mod:`.revise`).

    The sheet before it keeps its look: the items and lines it adds are
    placed and routed around what is there (moving whole parts of the sheet
    apart where room is short), and what it ``remove``-s is taken away.
    ``color``: how its changes are drawn (None: by its place in the list,
    see ``REVISION_COLORS``).  ``remove``: element ids; ``remove_lines``:
    lines as ``(from, to)``, as they were written; ``changes``: items that
    stay but change (see :class:`Change`).  An id removed may be used again
    by an item added in the same or a later revision."""

    id: str
    color: str | None = None
    note: str = ""
    equipment: list[Equipment] = field(default_factory=list)
    connections: list[Connection] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    remove_lines: list[tuple[str, str]] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = _changed(
            {"id": self.id}, ("color", self.color, None), ("note", self.note, "")
        )
        if self.remove or self.remove_lines:
            d["remove"] = list(self.remove) + [
                {"from": a, "to": b} for a, b in self.remove_lines
            ]
        if self.changes:
            d["change"] = [c.to_dict() for c in self.changes]
        if self.equipment:
            d["elements"] = [e.to_dict() for e in self.equipment]
        if self.connections:
            d["connections"] = [c.to_dict() for c in self.connections]
        return d
