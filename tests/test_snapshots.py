"""Regression net: placement and routes of the example sheets must not change.

Regenerate after an intended layout/routing change with::

    PFD_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_snapshots.py
"""

import json
import os
from pathlib import Path

import pytest

from schemdraw_pfd import Flowsheet

SNAPSHOTS = Path(__file__).parent / "snapshots"
EXAMPLES = ["sample", "distillation", "pid_c201", "pid_c201_inline"]


def _snapshot(name: str) -> dict:
    d = Flowsheet.from_json(f"examples/{name}.json").build()
    return {
        "placed": [[p.id, p.x, p.y, p.geo.width, p.geo.height] for p in d.placed],
        "routes": [
            [c.src, c.dst, c.kind, [[round(x, 6), round(y, 6)] for x, y in r.points]]
            for c, r in d.routes
        ],
        "warnings": d.warnings,
    }


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_layout_is_unchanged(name):
    actual = json.loads(json.dumps(_snapshot(name)))  # tuples -> lists
    path = SNAPSHOTS / f"{name}.json"
    if os.environ.get("PFD_UPDATE_SNAPSHOTS") or not path.exists():
        SNAPSHOTS.mkdir(exist_ok=True)
        path.write_text(json.dumps(actual, ensure_ascii=False, indent=1), "utf-8")
        pytest.skip(f"snapshot written: {path.name}")
    assert actual == json.loads(path.read_text("utf-8"))
