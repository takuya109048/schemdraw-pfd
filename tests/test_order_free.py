"""The flow layout ignores the order of the JSON lists.

Equipment is placed left to right by the direction of flow; which item and
which line was written first must not change the sheet.
"""

import json
import random
from pathlib import Path

import pytest

from schemdraw_pfd import Flowsheet

FLOW_EXAMPLES = sorted(
    p.stem
    for p in Path("examples").glob("*.json")
    if json.loads(p.read_text("utf-8-sig")).get("layout", "flow") == "flow"
    and p.stem != "crude_distillation"  # too slow to build here (~3 min)
)


def _sheet(d):
    """Everything drawn, independent of the order it is listed in."""
    return (
        sorted((p.id, p.x, p.y) for p in d.placed),
        sorted((c.src, c.dst, tuple(r.points)) for c, r in d.routes),
        sorted(d.warnings),
    )


@pytest.mark.parametrize("name", FLOW_EXAMPLES)
def test_shuffled_json_draws_the_same_sheet(name):
    data = json.loads(Path(f"examples/{name}.json").read_text("utf-8-sig"))
    expected = _sheet(Flowsheet.from_dict(data).build())
    rng = random.Random(name)
    rng.shuffle(data["elements"])
    rng.shuffle(data["connections"])
    assert _sheet(Flowsheet.from_dict(data).build()) == expected


def test_reversed_lists_draw_the_same_sheet():
    data = json.loads(Path("examples/column_trains.json").read_text("utf-8"))
    expected = _sheet(Flowsheet.from_dict(data).build())
    data["elements"].reverse()
    data["connections"].reverse()
    assert _sheet(Flowsheet.from_dict(data).build()) == expected


def test_json_lays_out_by_the_flow_unless_told_otherwise():
    assert Flowsheet.from_dict({"elements": []}).layout == "flow"
    assert Flowsheet.from_dict({"layout": "order", "elements": []}).layout == "order"
    fs = Flowsheet()  # the Python API keeps the declared order by default
    assert fs.layout == "order"
    assert fs.to_dict()["layout"] == "order"  # written out explicitly


def test_crude_distillation_loads_without_any_order_hints():
    # the large example carries no row / order hints: levels and lines only
    data = json.loads(Path("examples/crude_distillation.json").read_text("utf-8"))
    assert all(
        set(e) <= {"id", "type", "level", "params", "desc", "span"}
        for e in data["elements"]
    )
    net = Flowsheet.from_dict(data).expand()
    assert len(net.equipment) > 70
