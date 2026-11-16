"""Inline items on lines and automatic tees on shared ports."""

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
    fs.add("pump", "F")
    fs.add("vertical_drum", "D")
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
