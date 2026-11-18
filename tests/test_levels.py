"""Automatic levels: items without a level get one from the lines alone."""

import json
import random
from pathlib import Path

from schemdraw_pfd import Flowsheet


def _levels(fs: Flowsheet) -> dict[str, int]:
    return {e.id: e.tier for e in fs._with_levels()}


def _reflux() -> Flowsheet:
    """Column, overhead condenser draining into its reflux drum, reflux pump."""
    fs = Flowsheet(layout="flow")
    fs.add("connector", "FEED", text="FEED")
    fs.add("pump", "P-1")
    fs.add("column", "T-1")
    fs.add("heat_exchanger", "E-1")
    fs.add("horizontal_drum", "V-1")
    fs.add("pump", "P-2")
    fs.add("connector", "DIST", text="DIST", point="right")
    fs.connect("FEED.tip", "P-1.in")
    fs.connect("P-1.out", "T-1.w4")
    fs.connect("T-1.top", "E-1.shell_in")
    fs.connect("E-1.shell_out", "V-1.n1")
    fs.connect("V-1.s5", "P-2.in")
    fs.connect("P-2.out", "T-1.e7", recycle=True)
    fs.connect("P-2.out", "DIST.tail")
    return fs


def test_pumps_on_the_ground_and_fed_from_above():
    lv = _levels(_reflux())
    assert lv["P-1"] == lv["P-2"] == 0
    assert lv["V-1"] >= 1  # the reflux drum feeds a pump: above it


def test_column_stands_on_the_ground_through_the_structure():
    eqs = {e.id: e for e in _reflux()._with_levels()}
    assert (eqs["T-1"].level, eqs["T-1"].span) == (0, 2)


def test_condenser_drains_down_into_its_drum_and_stands_over_the_column():
    lv = _levels(_reflux())
    assert lv["E-1"] > lv["V-1"]  # liquid falls from bottom to top nozzle
    assert lv["E-1"] >= 2  # overhead vapour rises above the column's top


def test_condensate_drops_straight_into_the_reflux_drum():
    # the hda example used to put both on one level: the line turned back up
    d = Flowsheet.from_json("examples/hda_benzene.json").build()
    route = next(r for c, r in d.routes if c.src == "E-104.shell_out")
    assert route.bends <= 2
    by = {p.id: p for p in d.placed}
    assert by["E-104"].y > by["V-104"].y + by["V-104"].geo.height


def test_given_levels_are_kept():
    fs = _reflux()
    next(e for e in fs.equipment if e.id == "V-1").level = 3
    lv = _levels(fs)
    assert lv["V-1"] == 3
    assert lv["E-1"] >= 4  # the rules work around it


def test_flags_follow_what_they_serve():
    lv = _levels(_reflux())
    assert lv["FEED"] == 0  # into a pump's suction, on the ground
    assert lv["DIST"] == 0  # off a pump's outlet


def test_supply_main_above_collecting_main():
    fs = Flowsheet.from_json("examples/parallel_trains.json")
    lv = _levels(fs)
    # as the file used to give them by hand
    assert lv["SUPPLY"] == 3 and lv["MAIN"] == 2
    assert {lv[f"T-{k}01"] for k in (1, 2, 3)} == {1}


def test_levels_do_not_depend_on_the_order_of_the_lists():
    data = json.loads(Path("examples/hda_benzene.json").read_text("utf-8"))
    want = _levels(Flowsheet.from_dict(data))
    rng = random.Random(7)
    for _ in range(3):
        rng.shuffle(data["elements"])
        rng.shuffle(data["connections"])
        assert _levels(Flowsheet.from_dict(data)) == want


def test_left_out_level_stays_left_out_in_json():
    fs = _reflux()
    assert "level" not in fs.to_dict()["elements"][0]
    fs.equipment[0].level = 1
    assert fs.to_dict()["elements"][0]["level"] == 1
    assert all(e.level is None for e in fs.equipment[1:])  # build copies
    fs.build()
    assert all(e.level is None for e in fs.equipment[1:])
