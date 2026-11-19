"""layout="flow": order from the direction of flow, items share X across levels."""

import random
from typing import Any

import pytest
from conftest import built_case

from schemdraw_pfd import Flowsheet
from schemdraw_pfd.render import _spread, _text_width


def _by(d):
    return {p.id: p for p in d.placed}


def _train(declared: list[str]) -> Flowsheet:
    """feed -> drum -> pump -> column -> product, declared in any order."""
    specs: dict[str, tuple[str, int, dict[str, Any]]] = {
        "IN": ("connector", 1, {"text": "feed"}),
        "D": ("vertical_drum", 1, {}),
        "P": ("pump", 0, {}),
        "C": ("column", 1, {}),
        "OUT": ("connector", 1, {"text": "product"}),
    }
    fs = Flowsheet(layout="flow")
    for i in declared:
        t, level, params = specs[i]
        fs.add(t, i, level=level, **params)
    fs.connect("IN.tip", "D.w6")
    fs.connect("D.bottom", "P.in")
    fs.connect("P.out", "C.w8")
    fs.connect("C.e8", "OUT.tail")
    return fs


def test_main_flow_runs_left_to_right_whatever_the_declared_order():
    for declared in (["OUT", "C", "P", "D", "IN"], ["P", "OUT", "IN", "C", "D"]):
        d = _train(declared).build()
        assert not d.warnings
        by = _by(d)
        assert by["IN"].x < by["D"].x < by["C"].x < by["OUT"].x
        assert by["P"].x >= by["D"].x  # the flow never runs left


def test_items_on_different_levels_share_x():
    d = _train(["IN", "D", "P", "C", "OUT"]).build()
    by = _by(d)
    drum, pump = by["D"], by["P"]
    # the pump (ground level) sits under the drum instead of in a column of its own
    assert pump.x < drum.bbox[2]
    order = _train(["IN", "D", "P", "C", "OUT"])
    order.layout = "order"
    assert d.size[0] < order.build().size[0]


def test_recycle_lines_do_not_pull_items_left():
    fs = _train(["IN", "D", "P", "C", "OUT"])
    fs.connect("C.top", "D.top", recycle=True)  # e.g. a vent back to the drum
    by = _by(fs.build())
    assert by["D"].x < by["C"].x


def test_loops_are_found_without_a_recycle_mark():
    fs = Flowsheet(layout="flow")
    fs.add("vertical_drum", "A")
    fs.add("vertical_drum", "B")
    fs.connect("A.e6", "B.w6")
    fs.connect("B.e2", "A.w2")  # closes the loop; A was first, so this runs back
    by = _by(fs.build())
    assert by["A"].x < by["B"].x


def test_flags_go_to_the_edge_their_port_faces():
    fs = Flowsheet(layout="flow")
    fs.add("connector", "CWS", text="CWS", point="left")  # tip faces west: right edge
    fs.add("heat_exchanger", "E")
    fs.add("connector", "FEED", text="feed")
    fs.connect("FEED.tip", "E.tube_in")
    fs.connect("CWS.tip", "E.shell_out_n")
    by = _by(fs.build())
    assert by["FEED"].x < by["E"].x < by["CWS"].x


def test_build_does_not_change_the_users_items():
    fs = Flowsheet.from_json("examples/hda_benzene.json")
    before = fs.to_dict()
    fs.build(compact=False)
    assert fs.to_dict() == before  # heights are settled on copies


def test_layout_is_part_of_the_json():
    fs = Flowsheet.from_json("examples/tennessee_eastman.json")
    assert fs.layout == "flow"
    assert Flowsheet.from_dict(fs.to_dict()).layout == "flow"
    with pytest.raises(ValueError, match="layout"):
        Flowsheet(layout="grid")


def test_reference_sheet_in_flow_layout_is_tighter():
    order = Flowsheet.from_json("examples/pid_c201_inline.json")
    flow = Flowsheet.from_json("examples/pid_c201_inline.json")
    flow.layout = "flow"
    a, b = order.build(), flow.build()
    assert not b.warnings
    assert b.size[0] < a.size[0] * 0.7
    assert b.bends <= a.bends


def _utility_sheet(layout: str, **flag) -> Flowsheet:
    fs = Flowsheet(layout=layout)
    fs.add("connector", "FEED", text="feed")
    fs.add("vertical_drum", "D")
    fs.add("heat_exchanger", "E")
    fs.add("column", "C")
    fs.add("connector", "OUT", text="product")
    fs.add("connector", "CWS", text="CWS", point="left", **flag)
    fs.add("connector", "CWR", text="CWR", **flag)
    fs.connect("FEED.tip", "D.w6")
    fs.connect("D.e2", "E.tube_in")
    fs.connect("E.tube_out", "C.w8")
    fs.connect("C.e8", "OUT.tail")
    fs.connect("CWS.tip", "E.shell_in_s", kind="utility")
    fs.connect("E.shell_out_n", "CWR.tail", kind="utility")
    return fs


def test_utility_flags_sit_beside_what_they_serve():
    d = _utility_sheet("flow").build()
    assert not d.warnings
    by = _by(d)
    for flag in ("CWS", "CWR"):
        assert by[flag].x < by["OUT"].x  # not stacked on the sheet edge
        assert by[flag].x - by["E"].bbox[2] <= 3  # right next to the exchanger
    # utility lines do not shape the main flow
    assert by["FEED"].x < by["D"].x < by["E"].x < by["C"].x < by["OUT"].x


def test_local_flags_can_be_forced_either_way():
    edge = _utility_sheet("flow", local=False).build()
    by = _by(edge)
    assert by["CWS"].x == by["OUT"].x  # back in the right edge column
    order = _utility_sheet("order", local=True).build()
    by = _by(order)
    assert by["CWS"].x < by["OUT"].x  # placed in its declared position


@pytest.mark.parametrize(
    ("name", "max_warnings"), [("hda_benzene", 7), ("tennessee_eastman", 5)]
)
def test_complex_reference_processes_route(name, max_warnings):
    # benchmark flowsheets written with levels only (no order, no coordinates)
    results = []
    for seed in (None, 5):
        fs = Flowsheet.from_json(f"examples/{name}.json")
        if seed is not None:
            random.Random(seed).shuffle(fs.equipment)
        d = fs.build(compact=False)
        assert len(d.warnings) <= max_warnings
        results.append((len(d.warnings), d.bends))
    assert results[0] == results[1]  # independent of the declared order


def test_spec_texts_never_overlap():
    em = 16 / 36
    texts = [(10.0, "E-201\ncondenser"), (10.5, "E-202\nreboiler")]
    (x1, t1), (x2, t2) = _spread(texts, em)
    assert x1 == 10.0  # the first keeps its place
    assert x2 - _text_width(t2, em) / 2 >= x1 + _text_width(t1, em) / 2  # no overlap
    assert _spread([(0.0, "A"), (50.0, "B")], em) == [(0.0, "A"), (50.0, "B")]


@pytest.mark.parametrize(
    "name",
    [
        "topology/split_3",  # taps above, lines down to the right edge
        "flags/stack_right_from_taps",  # taps below a vessel's bottom
        "flags/stack_left_into_bottoms",  # left edge, up into bottom nozzles
    ],
)
def test_flags_stacked_on_an_edge_nest_their_lines(name):
    assert built_case(name).crossings == 0
