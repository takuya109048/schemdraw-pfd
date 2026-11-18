"""Automatic levels: pumps on the ground, every other item left without a
level on whatever level makes the drawing best."""

import json
import random
from pathlib import Path

from schemdraw_pfd import Flowsheet
from schemdraw_pfd.levels import moves, search_levels, start_levels
from schemdraw_pfd.netlist import Connection, Equipment


def _placed_levels(fs: Flowsheet) -> dict[str, int]:
    return {p.id: p.level for p in fs.build().placed if not p.id.startswith("_")}


def _reflux() -> Flowsheet:
    """Condenser draining into its reflux drum, pumped on to a product flag."""
    fs = Flowsheet(layout="flow")
    fs.add("connector", "VAP", text="VAP")
    fs.add("heat_exchanger", "E-1")
    fs.add("horizontal_drum", "V-1")
    fs.add("pump", "P-1")
    fs.add("connector", "DIST", text="DIST", point="right")
    fs.connect("VAP.tip", "E-1.tube_in")
    fs.connect("E-1.shell_out", "V-1.n1")
    fs.connect("V-1.s5", "P-1.in")
    fs.connect("P-1.out", "DIST.tail")
    return fs


def test_search_starts_with_pumps_on_the_ground_and_the_rest_above():
    eqs = [
        Equipment("P", "pump"),
        Equipment("D", "vertical_drum"),
        Equipment("X", "valve", 3),
    ]
    assert start_levels(eqs) == {"P": 0, "D": 1, "X": 3}
    assert start_levels(eqs[1:]) == {"D": 0, "X": 3}  # no pumps: all on the ground


def test_moves_take_satellites_partners_and_copies_along():
    eqs = [
        Equipment("P", "pump"),
        Equipment("D", "vertical_drum"),
        Equipment("E", "heat_exchanger"),
        Equipment("F", "connector"),
        Equipment("G", "vertical_drum", 2),  # given: never moves
    ]
    conns = [
        Connection("D.bottom", "P.in"),
        Connection("D.top", "E.tube_in"),
        Connection("F.tip", "E.shell_in"),
        Connection("E.tube_out", "G.top"),
    ]
    sets = moves(eqs, conns)
    assert ["E", "F"] in sets  # the exchanger with its flag
    assert ["D", "E"] in sets  # both ends of a line
    assert all("P" not in m and "G" not in m for m in sets)
    assert ["D", "F"] in moves(eqs, conns, together=[["D", "F"]])


def test_search_walks_downhill_to_the_cheapest_levels():
    target = {"A": 2, "B": 0, "C": 3}

    def evaluate(tasks):
        return [sum((lv[i] - target[i]) ** 2 for i in lv) for lv in tasks]

    start = {"A": 1, "B": 1, "C": 1}
    assert search_levels(start, [["A"], ["B"], ["C"]], evaluate) == target


def test_pumps_stay_on_the_ground():
    lv = _placed_levels(_reflux())
    assert lv["P-1"] == 0


def test_condensate_drops_into_the_drum_without_turning_back():
    d = _reflux().build()
    route = next(r for c, r in d.routes if c.src == "E-1.shell_out")
    assert route.bends <= 2


def test_given_levels_are_kept():
    fs = _reflux()
    next(e for e in fs.equipment if e.id == "V-1").level = 3
    assert _placed_levels(fs)["V-1"] == 3


def test_levels_do_not_depend_on_the_order_of_the_lists():
    data = json.loads(Path("examples/header_loop.json").read_text("utf-8"))
    want = _placed_levels(Flowsheet.from_dict(data))
    rng = random.Random(7)
    for _ in range(2):
        rng.shuffle(data["elements"])
        rng.shuffle(data["connections"])
        assert _placed_levels(Flowsheet.from_dict(data)) == want


def test_left_out_level_stays_left_out():
    fs = _reflux()
    assert "level" not in fs.to_dict()["elements"][0]
    fs.build()
    assert all(e.level is None for e in fs.equipment)  # the build used copies
