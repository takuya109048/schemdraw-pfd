"""Regression net: placement, routes and the drawing of every example sheet
must not change.

Regenerate after an intended layout/routing/drawing change with::

    PFD_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_snapshots.py tests/test_cases.py
"""

from pathlib import Path

import pytest
from conftest import built, check_snapshot, snapshot

SNAPSHOTS = Path(__file__).parent / "snapshots"
EXAMPLES = sorted(p.stem for p in Path("examples").glob("*.json"))


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_is_unchanged(name, monkeypatch):
    check_snapshot(snapshot(built(name), monkeypatch), SNAPSHOTS / f"{name}.json")
