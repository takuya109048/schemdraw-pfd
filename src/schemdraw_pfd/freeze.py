"""A laid out sheet written down, to be read back as it is.

A flowsheet is laid out afresh on every build, so a change to what was
written -- or to the layout code itself -- can move what a revision was
drawn onto.  Freezing a flowsheet (``Flowsheet.freeze``, ``pfd --freeze``)
keeps the built sheet, its revisions so far included, in the JSON file
(``"frozen"``): later builds read it back instead of laying it out, so the
revisions written after it are drawn onto exactly that sheet.

The frozen sheet carries a fingerprint of what it was built from (the
elements, lines and revisions up to the one frozen through).  When those
change, the sheet is out of date: it is laid out afresh, with a warning.

Every item is written as what it was built from -- type, parameters,
extra ports (``Geometry.recipe``) -- and where it stands; every line as its
ends, kind and corner points.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .elements import ElementLibrary, Geometry
from .geometry import Port
from .layout import Placed
from .netlist import Connection, Equipment
from .revise import Ghost, RevisionInfo
from .router import Route

JUNCTION = "junction"  # a tap on a line (see revise.JUNCTION_PREFIX)


def fingerprint(data: dict[str, Any]) -> str:
    """What a frozen sheet was built from, as a short hash."""
    text = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _junction() -> Geometry:
    return Geometry(0, 0, [], {"j": Port("N", 0)}, label_pos="none")


def _item(p: Placed) -> list[Any]:
    if p.type == JUNCTION:
        return [p.id, JUNCTION, {}, {}, p.x, p.y, p.level]
    recipe = p.geo.recipe
    if recipe is None:
        raise ValueError(f"{p.id}: cannot be frozen (built without a recipe)")
    kind, params, ports = recipe
    return [p.id, kind, params, ports, p.x, p.y, p.level]


def _placed(v: list[Any], library: ElementLibrary) -> Placed:
    id, kind, params, ports, x, y, level = v
    if kind == JUNCTION:
        geo = _junction()
    else:
        geo = Equipment(id, kind, level, dict(params), dict(ports)).geometry(library)
    return Placed(id, kind, level, geo, x, y)


def _line(c: Connection, r: Route) -> list[Any]:
    return [c.src, c.dst, c.kind, [list(p) for p in r.points], r.strict]


def _route(v: list[Any]) -> tuple[Connection, Route]:
    src, dst, kind, points, strict = v
    return Connection(src, dst, kind), Route([tuple(p) for p in points], strict)


def to_data(d: Any) -> dict[str, Any]:
    """A built diagram as plain JSON data."""
    ghosts = []
    for g in d.ghosts:
        if g.placed is not None:
            ghosts.append({"rev": g.rev, "item": _item(g.placed)})
        elif g.line is not None:
            ghosts.append({"rev": g.rev, "line": _line(*g.line)})
    return {
        "items": [_item(p) for p in d.placed],
        "lines": [_line(c, r) for c, r in d.routes],
        "warnings": list(d.warnings),
        "descs": dict(d.descs),
        "uturns": d.uturns,
        "rows": [list(r) for r in d.rows],
        "revisions": [[r.id, r.color, r.note] for r in d.revisions],
        "added": dict(d.added),
        "ghosts": ghosts,
        "hosts": dict(d.hosts),
        "replaced": [list(r) for r in d.replaced],
        "changed": [list(c) for c in d.changed],
    }


def from_data(data: dict[str, Any], library: ElementLibrary, line_styles: dict) -> Any:
    """The diagram :func:`to_data` wrote, read back."""
    from .model import Diagram

    ghosts = []
    for g in data.get("ghosts", []):
        if "item" in g:
            ghosts.append(Ghost(g["rev"], placed=_placed(g["item"], library)))
        else:
            ghosts.append(Ghost(g["rev"], line=_route(g["line"])))
    return Diagram(
        [_placed(v, library) for v in data["items"]],
        [_route(v) for v in data["lines"]],
        list(data.get("warnings", [])),
        dict(data.get("descs", {})),
        dict(line_styles),
        int(data.get("uturns", 0)),
        [tuple(r) for r in data.get("rows", [])],
        [RevisionInfo(*r) for r in data.get("revisions", [])],
        dict(data.get("added", {})),
        ghosts,
        dict(data.get("hosts", {})),
        [tuple(r) for r in data.get("replaced", [])],
        [tuple(c) for c in data.get("changed", [])],
    )
