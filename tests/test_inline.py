"""Inline items on lines and automatic tees on shared ports."""

import copy
from typing import Any

import pytest

from schemdraw_pfd import Flowsheet


def _by_id(d):
    return {p.id: p for p in d.placed}


# -- inline items --------------------------------------------------------------
def test_inline_valve_train_sits_in_line_before_the_destination():
    fs = Flowsheet()
    fs.add("vertical_drum", "D")
    fs.add("column", "C")
    fs.add("instrument", "FIC", level=1, function="FIC", number="1", mount="dcs")
    fs.connect(
        "D.e2",
        "C.w8",
        inline=[
            "valve",
            {"type": "valve", "id": "FV-1", "params": {"kind": "control"}},
            "valve",
        ],
    )
    fs.connect("FIC.w", "FV-1.signal", kind="signal")  # inline ids are addressable
    net = fs.expand()
    assert [e.id for e in net.equipment] == ["D", "_L0_0", "FV-1", "_L0_2", "C", "FIC"]
    assert [(c.src, c.dst) for c in net.connections[:4]] == [
        ("D.e2", "_L0_0.in"),
        ("_L0_0.out", "FV-1.in"),
        ("FV-1.out", "_L0_2.in"),
        ("_L0_2.out", "C.w8"),
    ]
    d = fs.build()
    assert not d.warnings
    by = _by_id(d)
    assert by["_L0_0"].y == by["FV-1"].y == by["_L0_2"].y  # one straight line
    assert [r.bends for _, r in d.routes[:4]] == [0, 0, 0, 0]  # drum lifted to suit


def test_inline_items_are_mirrored_when_the_line_runs_right_to_left():
    fs = Flowsheet()
    fs.add("column", "C")
    fs.add("vertical_drum", "D")
    fs.connect("D.w2", "C.e8", inline=["valve"])  # flows leftwards into C
    net = fs.expand()
    assert [e.id for e in net.equipment] == ["C", "_L0_0", "D"]
    valve = net.equipment[1]
    assert valve.params["mirror"] is True
    assert net.geos["_L0_0"].ports["in"].side == "E"
    assert not fs.build().warnings


def test_inline_items_stay_off_the_sheet_edge():
    fs = Flowsheet()
    fs.add("vertical_drum", "D")
    fs.add("pump", "P")
    fs.add("connector", "OUT", text="to T-1")
    fs.connect("P.out", "OUT.tail", inline=["valve", "valve"])  # dst is a flag
    order = [e.id for e in fs.expand().equipment]
    assert order == ["D", "P", "_L0_0", "_L0_1", "OUT"]
    fs2 = Flowsheet()
    fs2.add("pump", "P")
    fs2.add("vertical_drum", "D")
    fs2.connect("P.out", "D.w5", inline=["valve"], anchor="src")
    assert [e.id for e in fs2.expand().equipment] == ["P", "_L0_0", "D"]


def test_inline_port_names_can_be_chosen():
    fs = Flowsheet()
    fs.add("valve", "A")
    fs.add("valve", "B")
    with pytest.raises(KeyError, match="no port 'in'"):
        fs.connect("A.out", "B.in", inline=["instrument"])
        fs.build()
    fs.connections.clear()
    fs.connect(
        "A.out",
        "B.in",
        inline=[
            {"type": "instrument", "in": "w", "out": "e", "params": {"function": "FE"}}
        ],
    )
    assert not fs.build().warnings


def test_inline_ids_must_be_unique_and_not_reserved():
    fs = Flowsheet()
    fs.add("valve", "A")
    fs.add("valve", "B")
    with pytest.raises(ValueError, match="duplicate"):
        fs.connect("A.out", "B.in", inline=[{"type": "valve", "id": "A"}])
    with pytest.raises(ValueError, match="reserved"):
        fs.connect("A.out", "B.in", inline=[{"type": "valve", "id": "_X"}])
    with pytest.raises(ValueError, match="reserved"):
        fs.add("valve", "_T_1")


def test_inline_json_round_trip_with_forward_reference():
    data = {
        "elements": [
            {"id": "FIC", "type": "instrument", "level": 1, "params": {"mount": "dcs"}},
            {"id": "D", "type": "vertical_drum"},
            {"id": "C", "type": "column"},
        ],
        "connections": [
            # the signal names FV-1 before the line that declares it
            {"from": "FIC.w", "to": "FV-1.signal", "type": "signal"},
            {
                "from": "D.e2",
                "to": "C.w8",
                "inline": [
                    "valve",
                    {"type": "valve", "id": "FV-1", "params": {"kind": "control"}},
                ],
            },
        ],
    }
    fs = Flowsheet.from_dict(data)
    assert [c.src for c in fs.connections] == ["FIC.w", "D.e2"]  # file order kept
    again = Flowsheet.from_dict(fs.to_dict())
    assert again.to_dict() == fs.to_dict()
    assert fs.to_dict()["connections"][1]["inline"][0] == "valve"
    a, b = fs.build(compact=False), again.build(compact=False)
    assert [r.points for _, r in a.routes] == [r.points for _, r in b.routes]


# -- automatic tees --------------------------------------------------------------
def test_split_gets_a_tee_with_arms_towards_the_branches():
    fs = Flowsheet()
    fs.add("vertical_drum", "D")
    fs.add("valve", "V1")
    fs.add("valve", "V2", level=1)
    fs.connect("D.e2", "V1.in")
    fs.connect("D.e2", "V2.in")
    net = fs.expand()
    assert [e.id for e in net.equipment] == ["D", "_T_D_e2", "V1", "V2"]
    tee = net.equipment[1]
    assert tee.params["arms"] == "wen"  # trunk from D, straight on to V1, up to V2
    assert [(c.src, c.dst) for c in net.connections] == [
        ("D.e2", "_T_D_e2.w"),
        ("_T_D_e2.e", "V1.in"),
        ("_T_D_e2.n", "V2.in"),
    ]
    d = fs.build()
    assert not d.warnings
    assert "_T_D_e2" in _by_id(d)


def test_merge_gets_a_tee_in_front_of_the_shared_port():
    fs = Flowsheet()
    fs.add("valve", "A", level=1)
    fs.add("valve", "B")
    fs.add("column", "C")
    fs.connect("A.out", "C.w2")
    fs.connect("B.out", "C.w2")
    net = fs.expand()
    assert [e.id for e in net.equipment] == ["A", "B", "_T_C_w2", "C"]
    assert [(c.src, c.dst) for c in net.connections][0] == ("_T_C_w2.e", "C.w2")
    assert not fs.build().warnings


def test_tee_beside_a_vertical_port_follows_its_height():
    fs = Flowsheet()
    fs.add("horizontal_drum", "D")
    fs.add("valve", "V1")
    fs.add("valve", "V2")
    fs.connect("D.n_mid", "V1.in")
    fs.connect("D.n_mid", "V2.in")
    net = fs.expand()
    fs.build()  # settles the tee
    d = fs.build()
    by = _by_id(d)
    tee = by["_T_D_n_mid"]
    assert tee.y >= by["D"].bbox[3]  # trunk arm above the nozzle: up, then across
    trunk = next(r for c, r in d.routes if c.src == "D.n_mid")
    assert trunk.bends == 1
    assert net.pending and net.pending[0].side == "N"


def test_split_with_inline_items_on_a_branch():
    fs = Flowsheet()
    fs.add("vertical_drum", "D")
    fs.add("column", "C")
    fs.add("valve", "V", level=1)
    fs.connect("D.e2", "C.w2", inline=["valve"])
    fs.connect("D.e2", "V.in")
    net = fs.expand()
    assert [e.id for e in net.equipment] == ["D", "_T_D_e2", "_L0_0", "C", "V"]
    assert not fs.build().warnings


def test_merging_lines_cut_into_a_main_pipe_one_after_another():
    fs = Flowsheet(layout="flow")
    for i in "ABC":
        fs.add("valve", i, level=1)
    fs.add("pump", "F", level=0)
    fs.add("vertical_drum", "D", level=0)
    for i in "ABCF":
        fs.connect(f"{i}.out", "D.w1")
    net = fs.expand()
    taps = [e for e in net.equipment if e.type == "tee"]
    assert len(taps) == 3  # three taps on the main pipe, F runs straight in
    assert all(len(t.params["arms"]) == 3 for t in taps)
    assert all(t.params["arms"][2] == "n" for t in taps)  # A, B, C come from above
    assert ("_T_D_w1.e", "D.w1") in [(c.src, c.dst) for c in net.connections]
    d = fs.build()
    assert not d.warnings
    ys = {p.y for p in d.placed if p.id.startswith("_T_")}
    assert len(ys) == 1  # one straight main pipe


def test_riser_enters_the_middle_of_a_main_pipe_running_both_ways():
    fs = Flowsheet(layout="flow")
    fs.add("vertical_drum", "D-1", level=1)
    fs.add("pump", "P-1")
    fs.add("heat_exchanger", "E-1", level=1, orientation="vertical", rotation=180)
    fs.connect("D-1.bottom", "P-1.in")
    fs.connect("P-1.out", "D-1.top", recycle=True)
    fs.connect("P-1.out", "E-1.tube_in")
    net = fs.expand()
    taps = [e for e in net.equipment if e.type == "tee"]
    assert [t.params["arms"] for t in taps] == ["swe"]  # up into the middle
    pairs = [(c.src, c.dst) for c in net.connections]
    assert ("_T_P-1_out.w", "D-1.top") in pairs  # back to the feed drum
    assert ("_T_P-1_out.e", "E-1.tube_in") in pairs  # on to the exchanger
    d = fs.build()
    assert not d.warnings
    assert max(r.bends for _, r in d.routes) == 1  # every line an I or an L


# a mixing tank circulated through a header: one branch back into its own
# top, one into the top of a day tank whose bottom returns to the mixing tank
MIXING: dict[str, list[dict[str, Any]]] = {
    "elements": [
        {"id": "TK-A", "type": "vertical_drum", "desc": "Mixing tank"},
        {"id": "P-A", "type": "pump", "desc": "Circulation pump"},
        {"id": "H-A", "type": "header", "params": {"branches": 2}},
        {"id": "TK-B", "type": "vertical_drum", "desc": "Day tank"},
        {"id": "P-B", "type": "pump", "desc": "Dosing pump"},
        {"id": "OUT", "type": "connector", "params": {"text": "to dosing"}},
    ],
    "connections": [
        {"from": "TK-A.bottom", "to": "P-A.in"},
        {"from": "P-A.out", "to": "H-A.main_w"},
        {"from": "H-A.b0", "to": "TK-A.n3", "recycle": True},
        {"from": "H-A.b1", "to": "TK-B.n1"},
        {"from": "TK-B.bottom", "to": "P-B.in"},
        {"from": "P-B.out", "to": "OUT.tail"},
        {"from": "TK-B.s0", "to": "TK-A.n1", "recycle": True},
    ],
}


def test_header_branches_arch_over_into_top_nozzles():
    # branches rise from the header and drop into the tops: an arch, no warning
    d = Flowsheet.from_dict(MIXING).build()
    assert not [w for w in d.warnings if w.startswith("H-A.")]
    arches = [r for c, r in d.routes if c.src.startswith("H-A.b")]
    assert [r.bends for r in arches] == [2, 2]
    for r in arches:
        (_, y0), (_, y1), (_, y2) = r.points[0], r.points[1], r.points[-1]
        assert y1 > y0 and y1 > y2  # up, over, down


def _loop(a_branch: str, a_return: str) -> Flowsheet:
    """The mixing sheet, the day tank raised a level, with the two lines into
    the mixing tank on given ports."""
    data = copy.deepcopy(MIXING)
    for e in data["elements"]:
        if e["id"] == "TK-B":
            e["level"] = 2  # a gravity return: the day tank stands higher
    for c in data["connections"]:
        if c["from"] == "H-A.b0":
            c["to"] = f"TK-A.{a_branch}"
        if c["from"] == "TK-B.s0":
            c["to"] = f"TK-A.{a_return}"
    return Flowsheet.from_dict(data)


def test_merging_recycles_run_back_on_the_main_pipe():
    # a branch and a gravity return both end on TK-A's top: the main pipe
    # into the top runs back, the lines flow forwards into it
    fs = _loop("top", "top")
    net = fs.expand()
    back = {(c.src, c.dst) for c in net.connections if c.recycle}
    assert back == {("_T_TK-A_top.w", "TK-A.top")}
    d = fs.build()
    assert not d.warnings
    by = {p.id: p for p in d.placed}
    tap = next(r for c, r in d.routes if c.src == "H-A.b0")
    assert tap.bends == 0  # the branch rises straight into the main pipe
    assert d.crossings == 1  # only the riser to TK-B passes behind it
    assert by["TK-B"].y > by["TK-A"].bbox[3]  # the day tank stands higher


def test_lines_reach_a_drum_head_through_nozzles_of_their_own():
    fs = _loop("n3", "n1")
    net = fs.expand()
    assert not [e for e in net.equipment if e.type == "tee"]  # nothing merges
    d = fs.build()
    assert not d.warnings
    ends = {c.dst: r.points[-1] for c, r in d.routes if c.dst.startswith("TK-A.n")}
    (x1, y1), (x3, y3) = ends["TK-A.n1"], ends["TK-A.n3"]
    assert x1 < x3 and y1 == y3  # two nozzles side by side on the top head
    assert d.crossings == 1  # nested arches: only the riser to TK-B crosses


def test_tank_bottom_to_tank_top_on_one_level_keeps_the_tank_on_its_floor():
    # TK-B beside TK-A: its bottom feeds TK-A's top, so the line has to go
    # down and back up around the equipment (a U, but no warning) -- and
    # TK-B is not lifted for it
    d = Flowsheet.from_dict(MIXING).build()
    by = {p.id: p for p in d.placed}
    assert by["TK-B"].y == by["TK-A"].y
    assert d.uturns == 1 and not d.warnings
    assert d.crossings == 0
