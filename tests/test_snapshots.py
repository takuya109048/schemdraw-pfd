"""Regression net: placement, routes and the drawing of every example sheet
must not change.

Regenerate after an intended layout/routing/drawing change with::

    PFD_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_snapshots.py
"""

import json
import os
from pathlib import Path

import pytest
import schemdraw

import schemdraw_pfd.render as render
from schemdraw_pfd import Flowsheet

SNAPSHOTS = Path(__file__).parent / "snapshots"
EXAMPLES = sorted(
    p.stem
    for p in Path("examples").glob("*.json")
    if p.stem != "crude_distillation"  # too slow to build here (~3 min)
)


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


def _drawing(name: str, monkeypatch) -> list:
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

    monkeypatch.setattr(render._Shapes, "__init__", record)
    monkeypatch.setattr(schemdraw.Drawing, "save", lambda *a, **k: None)
    Flowsheet.from_json(f"examples/{name}.json").render("unused.svg")
    return shapes


def _snapshot(name: str, monkeypatch) -> dict:
    d = Flowsheet.from_json(f"examples/{name}.json").build()
    return {
        "placed": [[p.id, p.x, p.y, p.geo.width, p.geo.height] for p in d.placed],
        "routes": [
            [c.src, c.dst, c.kind, [[round(x, 6), round(y, 6)] for x, y in r.points]]
            for c, r in d.routes
        ],
        "warnings": d.warnings,
        "drawing": _drawing(name, monkeypatch),
    }


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_is_unchanged(name, monkeypatch):
    actual = json.loads(json.dumps(_snapshot(name, monkeypatch)))  # tuples -> lists
    path = SNAPSHOTS / f"{name}.json"
    if os.environ.get("PFD_UPDATE_SNAPSHOTS") or not path.exists():
        SNAPSHOTS.mkdir(exist_ok=True)
        path.write_text(json.dumps(actual, ensure_ascii=False, indent=1), "utf-8")
        pytest.skip(f"snapshot written: {path.name}")
    assert actual == json.loads(path.read_text("utf-8"))
