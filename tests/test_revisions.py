"""Revisions drawn onto a built sheet (``tests/cases/revisions/``).

What a revision promises: the sheet before it keeps its look -- every item
and line it does not remove stays where it was and runs the way it ran
(when the sheet has to be moved apart, whole parts move and lines across
the cut stretch, so the shapes stay) -- and what it adds or removes is
drawn in its colour.
"""

import json

import pytest
from conftest import CASES

from schemdraw_pfd import Diagram, Flowsheet
from schemdraw_pfd.render import Text, scene
from schemdraw_pfd.revise import line_key

NAMES = sorted(p.stem for p in (CASES / "revisions").glob("*.json"))
# cases that fit their changes in without moving the sheet apart
UNMOVED = ["add_spare_pump", "remove_inline", "remove_item", "remove_line"]


def _fs(name: str) -> Flowsheet:
    return Flowsheet.from_json(CASES / "revisions" / f"{name}.json")


def _stages(name: str) -> list[Diagram]:
    fs = _fs(name)
    return [fs.build(until="base")] + [fs.build(until=r.id) for r in fs.revisions]


def _dirs(points) -> list[tuple[int, int]]:
    return [
        ((b[0] > a[0]) - (b[0] < a[0]), (b[1] > a[1]) - (b[1] < a[1]))
        for a, b in zip(points, points[1:])
    ]


@pytest.mark.parametrize("name", NAMES)
def test_what_stays_keeps_its_shape(name):
    stages = _stages(name)
    for before, after in zip(stages, stages[1:]):
        lines = {line_key(c): r for c, r in after.routes}
        for c, r in before.routes:
            if line_key(c) in lines:  # bends where it bent, the same ways
                assert _dirs(lines[line_key(c)].points) == _dirs(r.points), c
        old = {p.id: p for p in before.placed}
        new = {p.id: p for p in after.placed if p.id in old}
        for a in new:  # nothing changes sides with anything else
            for b in new:
                for k in ("x", "y"):
                    was = getattr(old[a], k) - getattr(old[b], k)
                    now = getattr(new[a], k) - getattr(new[b], k)
                    assert (was > 0) == (now > 0) and (was < 0) == (now < 0)


@pytest.mark.parametrize("name", UNMOVED)
def test_what_stays_does_not_move(name):
    stages = _stages(name)
    for before, after in zip(stages, stages[1:]):
        lines = {line_key(c): r.points for c, r in after.routes}
        for c, r in before.routes:
            if line_key(c) in lines:
                assert lines[line_key(c)] == r.points, c
        where = {p.id: (p.x, p.y) for p in after.placed}
        for p in before.placed:
            if p.id in where:
                assert where[p.id] == (p.x, p.y), p.id


def test_a_sheet_short_of_room_is_moved_apart():
    """P-1's discharge is too short to tap: the part of the sheet above it
    moves up as a whole, the riser stretches."""
    base, r1, _ = _stages("two_revisions")
    at = {p.id: (p.x, p.y) for p in r1.placed}
    was = {p.id: (p.x, p.y) for p in base.placed}
    assert at["P-1"] == was["P-1"]
    shift = at["TK-1"][1] - was["TK-1"][1]
    assert shift > 0
    for i in ("IN", "OUT"):
        assert at[i] == (was[i][0], was[i][1] + shift)


def test_the_base_is_the_sheet_as_written():
    data = json.loads((CASES / "revisions" / "add_spare_pump.json").read_text("utf-8"))
    del data["revisions"]
    plain = Flowsheet.from_dict(data).build()
    base = _fs("add_spare_pump").build(until="base")
    assert [(p.id, p.x, p.y) for p in base.placed] == [
        (p.id, p.x, p.y) for p in plain.placed
    ]
    assert [r.points for _, r in base.routes] == [r.points for _, r in plain.routes]


def test_added_items_and_lines_are_recorded():
    d = _fs("add_spare_pump").build()
    assert d.added["P-2"] == "R1"
    assert any(i.startswith("_J") for i, r in d.added.items() if r == "R1")
    added = [c for c, _ in d.routes if d.added.get(line_key(c)) == "R1"]
    assert added and all(line_key(c) in d.added for c in added)
    old = [c for c, _ in d.routes if line_key(c) not in d.added]
    assert {line_key(c) for c in old} >= {"P-1.out -> OUT.tail"}


def test_an_added_line_taps_a_line_in_use():
    """TK-1.bottom and OUT.tail already carry a line: the spare pump's lines
    join those lines at junctions, which do not change them."""
    d = _fs("add_spare_pump").build()
    junctions = [p for p in d.placed if p.id.startswith("_J")]
    assert len(junctions) == 2
    assert set(d.hosts.values()) == {"TK-1.bottom -> P-1.in", "P-1.out -> OUT.tail"}
    check = [p for p in d.placed if p.type == "valve"]  # on the added line
    assert len(check) == 1 and d.added[check[0].id] == "R1"


def test_removing_an_inline_item_joins_its_line():
    d = _fs("remove_inline").build()
    keys = [line_key(c) for c, _ in d.routes]
    assert "P-1.out -> E-1.tube_in" in keys
    assert not any("FV-1" in k or "FIC-1" in k for k in keys)
    gone = {g.placed.id for g in d.ghosts if g.placed}
    assert gone == {"FV-1", "FIC-1"}
    (route,) = [r for c, r in d.routes if line_key(c) == "P-1.out -> E-1.tube_in"]
    base = _fs("remove_inline").build(until="base")
    into = next(r for c, r in base.routes if c.src == "P-1.out")
    out = next(r for c, r in base.routes if c.dst == "E-1.tube_in")
    assert route.points[0] == into.points[0] and route.points[-1] == out.points[-1]
    # straight on through where the valve stood: no bend more than before
    assert route.bends == into.bends + out.bends


def test_removing_an_item_takes_its_lines_and_flags():
    d = _fs("remove_item").build()
    ids = {p.id for p in d.placed}
    assert not ids & {"E-1", "CWS", "CWR"}
    assert {"P-1", "IN", "OUT"} <= ids
    gone = {g.placed.id for g in d.ghosts if g.placed}
    assert gone == {"E-1", "CWS", "CWR"}
    assert any(c.dst == "OUT.tail" for c, _ in d.routes)  # the bypass


def test_removing_a_line_keeps_the_shared_main_pipe():
    d = _fs("remove_line").build()
    keys = [line_key(c) for c, _ in d.routes]
    assert not any("B." in k for k in keys)
    assert any(k.endswith("A.tail") for k in keys)
    assert "B" not in {p.id for p in d.placed}  # a flag to nothing goes
    tees = [p for p in d.placed if p.type == "tee"]
    assert all(len(p.geo.ports) == 2 for p in tees)  # the arm to B is gone


def test_a_later_revision_works_on_an_earlier_one():
    d = _fs("two_revisions").build()
    assert d.added["E-1"] == "R1" and d.added["PI-1"] == "R2"
    assert [(r.id, r.color) for r in d.revisions] == [
        ("R1", "red"),
        ("R2", "#008080"),
    ]
    assert [g.rev for g in d.ghosts if g.line] == ["R2"]


# -- drawing --------------------------------------------------------------------
def _colors(d: Diagram, highlight: str) -> dict[str, set]:
    out: dict[str, set] = {}
    for g in scene(d, highlight=highlight).groups:
        if g.kind != "sheet":
            out.setdefault(g.id, set()).add((g.color, g.dashed))
    return out


def test_highlight_all_latest_none():
    d = _fs("two_revisions").build()
    every = _colors(d, "all")
    assert every["E-1"] == {("red", False)}
    assert every["PI-1"] == {("#008080", False)}
    assert every["TK-1"] == {(None, False)}
    assert ("#008080", True) in every["E-1.tube_out -> TK-1.n3 (removed)"]
    latest = _colors(d, "latest")
    assert latest["E-1"] == {(None, False)} and latest["PI-1"] == {("#008080", False)}
    assert "E-1.tube_out -> TK-1.n3 (removed)" in latest
    plain = _colors(d, "none")
    assert all(c == {(None, False)} for c in plain.values())
    assert not any(k.endswith("(removed)") for k in plain)
    with pytest.raises(ValueError, match="highlight must be one of"):
        scene(d, highlight="some")


def test_legend_lists_the_coloured_revisions():
    d = _fs("two_revisions").build()
    sheet = [g for g in scene(d).groups if g.kind == "sheet"][0]
    legend = [i for i in sheet.items if isinstance(i, Text) and i.color]
    assert [(t.text, t.color) for t in legend] == [
        ("R1", "red"),
        ("R2: E-1 の戻りを撤去、計器を追加", "#008080"),
    ]


def test_excel_output_carries_the_colours(tmp_path):
    from zipfile import ZipFile

    path = tmp_path / "out.xlsx"
    _fs("two_revisions").render(path)
    xml = ZipFile(path).read("xl/drawings/drawing1.xml").decode()
    assert 'srgbClr val="FF0000"' in xml and 'srgbClr val="008080"' in xml


# -- input ------------------------------------------------------------------------
def test_python_api_matches_json():
    fs = Flowsheet(layout="flow")
    fs.add("connector", "IN", text="原料")
    fs.add("vertical_drum", "TK-1")
    fs.add("pump", "P-1")
    fs.add("connector", "OUT", text="製品")
    fs.connect("IN.tip", "TK-1.n1")
    fs.connect("TK-1.bottom", "P-1.in")
    fs.connect("P-1.out", "OUT.tail")
    fs.revise("R1", note="予備ポンプ")
    fs.add("pump", "P-2", desc="予備")
    fs.connect("TK-1.bottom", "P-2.in")
    fs.connect(
        "P-2.out", "OUT.tail", inline=[{"type": "valve", "params": {"kind": "check"}}]
    )
    json_fs = _fs("add_spare_pump")
    assert fs.to_dict() == json_fs.to_dict()
    a, b = fs.build(), json_fs.build()
    assert [(p.id, p.x, p.y) for p in a.placed] == [(p.id, p.x, p.y) for p in b.placed]


def test_remove_and_disconnect_need_a_revision():
    fs = _fs("remove_line")
    fs._revision = None
    with pytest.raises(ValueError, match="start a revision first"):
        fs.remove("P-1")
    with pytest.raises(ValueError, match="start a revision first"):
        fs.disconnect("P-1.out", "A.tail")
    fs.revise("R2")
    fs.disconnect("P-1.out", "A.tail")
    assert fs.revisions[-1].remove_lines == [("P-1.out", "A.tail")]
    with pytest.raises(ValueError, match="is taken"):
        fs.revise("base")


def test_unknown_revision_to_build_until():
    with pytest.raises(ValueError, match="unknown revision 'R9'"):
        _fs("remove_line").build(until="R9")
