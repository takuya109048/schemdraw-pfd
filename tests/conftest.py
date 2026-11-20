"""Shared helpers: example sheets are built once per test session, and the
snapshot of a built sheet (placement, routes, warnings and drawing)."""

import json
import os
from functools import cache
from pathlib import Path

import pytest

import schemdraw_pfd.render as render
from schemdraw_pfd import Diagram, Flowsheet
from schemdraw_pfd._vendor import schemdraw

CASES = Path(__file__).parent / "cases"


@cache
def built(name: str) -> Diagram:
    """The example ``examples/<name>.json`` laid out and routed (cached: a
    build of the large examples takes tens of seconds)."""
    return Flowsheet.from_json(f"examples/{name}.json").build()


@cache
def built_case(name: str) -> Diagram:
    """The small case ``tests/cases/<name>.json`` laid out and routed."""
    return Flowsheet.from_json(CASES / f"{name}.json").build()


def _plain(v):
    """JSON-ready copy of a segment attribute (floats rounded)."""
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, (int, str, bool)) or v is None:
        return v
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in sorted(v.items())}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    try:
        return [_plain(x) for x in v]  # points and other iterables
    except TypeError:
        return str(v)


def _drawing(d: Diagram, monkeypatch) -> list:
    """Every segment the renderer hands to schemdraw, in order."""
    shapes: list = []
    init = render._Shapes.__init__

    def record(self, segments, **kw):
        shapes.append(
            [
                [type(s).__name__, _plain({k: x for k, x in vars(s).items()})]
                for s in segments
            ]
        )
        init(self, segments, **kw)

    with monkeypatch.context() as m:
        m.setattr(render._Shapes, "__init__", record)
        m.setattr(schemdraw.Drawing, "save", lambda *a, **k: None)
        render.draw(d, "unused.svg")
    return shapes


def snapshot(d: Diagram, monkeypatch) -> dict:
    """Placement, routes, warnings and drawing of ``d`` as plain JSON data."""
    data = {
        "placed": [[p.id, p.x, p.y, p.geo.width, p.geo.height] for p in d.placed],
        "routes": [
            [c.src, c.dst, c.kind, [[round(x, 6), round(y, 6)] for x, y in r.points]]
            for c, r in d.routes
        ],
        "warnings": d.warnings,
        "drawing": _drawing(d, monkeypatch),
    }
    return json.loads(json.dumps(data))  # tuples -> lists


def check_snapshot(actual: dict, path: Path) -> None:
    """``actual`` must equal the snapshot in ``path``; written instead when
    ``PFD_UPDATE_SNAPSHOTS`` is set or there is none yet."""
    if os.environ.get("PFD_UPDATE_SNAPSHOTS") or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(actual, ensure_ascii=False, indent=1), "utf-8")
        pytest.skip(f"snapshot written: {path.name}")
    assert actual == json.loads(path.read_text("utf-8"))
