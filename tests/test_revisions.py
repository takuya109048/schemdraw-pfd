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
UNMOVED = [
    "add_spare_pump",
    "remove_inline",
    "remove_item",
    "remove_line",
    "replace_in_place",
    "reuse_id",
    "change_in_place",
]


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


def test_a_replacement_stands_where_the_old_item_stood():
    """Removed and added in one revision, alike and joined to the same
    items: the new pump takes the old one's place, again and again, and
    its lines run the way the old ones ran."""
    stages = _stages("replace_in_place")
    pumps = [next(p for p in d.placed if p.type == "pump") for d in stages]
    assert [p.id for p in pumps] == ["P-1", "P-1N", "P-1R"]
    assert len({(p.x, p.y) for p in pumps}) == 1
    assert stages[-1].replaced == [("R1", "P-1N", "P-1"), ("R2", "P-1R", "P-1N")]
    for d in stages:
        assert [_dirs(r.points) for c, r in d.routes if "TK-1.bottom" in c.src] == [
            _dirs(r.points) for c, r in stages[0].routes if "TK-1.bottom" in c.src
        ]


def test_a_replaced_item_is_named_in_the_legend_not_drawn_under_the_new():
    d = _fs("replace_in_place").build()
    groups = scene(d).groups
    assert not any(g.id.startswith("P-1 (removed)") for g in groups)
    sheet = [g for g in groups if g.kind == "sheet"][0]
    legend = [i.text for i in sheet.items if isinstance(i, Text) and i.color]
    assert legend == ["R1: ポンプ更新 (P-1 → P-1N)", "R2: 再更新 (P-1N → P-1R)"]


def test_since_draws_the_earlier_revisions_as_settled():
    d = _fs("two_revisions").build()
    since = _colors_since(d, "R2")
    assert since["E-1"] == {(None, False)}  # R1: settled, plain
    assert since["PI-1"] == {("#008080", False)}
    assert "E-1.tube_out -> TK-1.n3 (removed)" in since  # R2 removed it
    assert _colors_since(d, "base") == _colors(d, "all")
    with pytest.raises(ValueError, match="unknown revision 'R9'"):
        scene(d, since="R9")


def _colors_since(d: Diagram, since: str) -> dict[str, set]:
    out: dict[str, set] = {}
    for g in scene(d, since=since).groups:
        if g.kind != "sheet":
            out.setdefault(g.id, set()).add((g.color, g.dashed))
    return out


def test_cli_revision_options(tmp_path):
    from schemdraw_pfd.cli import main

    spec = CASES / "revisions" / "two_revisions.json"
    for args in (
        ["--revision", "base"],
        ["--revision", "R1", "--highlight", "latest"],
        ["--since", "R2"],
        ["--highlight", "none"],
    ):
        out = tmp_path / f"{'_'.join(args)}.svg"
        assert main([str(spec), "-o", str(out), "-j", "1", *args]) == 0
        assert out.exists()


def test_a_removed_id_may_be_used_again():
    base, after = _stages("reuse_id")
    old = next(p for p in base.placed if p.id == "P-1")
    new = next(p for p in after.placed if p.id == "P-1")
    assert (new.x, new.y) == (old.x, old.y)
    assert after.added["P-1"] == "R1" and after.descs["P-1"] == "新ポンプ"
    sheet = [g for g in scene(after).groups if g.kind == "sheet"][0]
    legend = [i.text for i in sheet.items if isinstance(i, Text) and i.ha == "left"]
    assert legend == ["R1: 同じタグで取替 (P-1 取替)"]
    assert not any(g.id == "P-1 (removed)" for g in scene(after).groups)


def test_an_id_on_the_sheet_may_not_be_used_again():
    fs = _fs("reuse_id")
    fs.revise("R2")
    with pytest.raises(ValueError, match="duplicate id 'P-1'"):
        fs.add("pump", "P-1")
    fs.remove("P-1")
    fs.add("pump", "P-1")  # removed in this revision: free again
    fs.connect("TK-1.bottom", "P-1.in")
    fs.connect("P-1.out", "OUT.tail")
    d = fs.build()
    assert [p.id for p in d.placed].count("P-1") == 1


def test_a_change_that_fits_stays_in_place():
    """A gate valve made a globe valve, a pump's description: drawn in the
    revision's colour where they stand; their lines do not change."""
    base, after = _stages("change_in_place")
    assert [(p.id, p.x, p.y) for p in after.placed] == [
        (p.id, p.x, p.y) for p in base.placed
    ]
    assert [r.points for _, r in after.routes] == [r.points for _, r in base.routes]
    assert after.changed == [("R1", "V-1"), ("R1", "P-1")]
    assert after.added == {"V-1": "R1", "P-1": "R1"}
    assert after.descs["P-1"] == "原料ポンプ（更新）"
    valve = next(p for p in after.placed if p.id == "V-1")
    assert any(getattr(s, "fill", None) for s in valve.geo.shapes)  # the dot
    colors = _colors(after, "all")
    assert colors["V-1"] == {("red", False)}
    assert colors["P-1.out -> V-1.in"] == {(None, False)}


def test_a_change_that_grows_grows_where_it_stands():
    """A tank made a taller column: what lies above it moves up by the
    growth, the column stands on the tank's place, its lines are laid again
    in the revision's colour and its old outline is not drawn."""
    base, after = _stages("change_resize")
    was = {p.id: p for p in base.placed}
    now = {p.id: p for p in after.placed}
    tk = now["TK-1"]
    assert tk.type == "column" and (tk.x, tk.y) == (was["TK-1"].x, was["TK-1"].y)
    grow = tk.geo.height - was["TK-1"].geo.height
    assert now["IN"].y == was["IN"].y + grow
    assert (now["P-1"].x, now["P-1"].y) == (was["P-1"].x, was["P-1"].y)
    assert after.changed == [("R1", "TK-1")]
    lines = [line_key(c) for c, _ in after.routes if "TK-1" in line_key(c)]
    assert lines and all(after.added[k] == "R1" for k in lines)
    groups = scene(after).groups
    assert not any(g.id.endswith("(removed)") for g in groups)


def test_python_change():
    fs = _fs("change_in_place")
    fs.revise("R2")
    fs.change("V-1", kind="check")
    fs.change("P-1", desc="")
    assert fs.to_dict()["revisions"][-1] == {
        "id": "R2",
        "change": [
            {"id": "V-1", "params": {"kind": "check"}},
            {"id": "P-1", "desc": ""},
        ],
    }
    d = fs.build()
    assert "P-1" not in d.descs
    with pytest.raises(KeyError, match="no item 'X'"):
        fs.change("X", desc="?")


# -- frozen sheet ------------------------------------------------------------------
def _sheet_of(d: Diagram) -> tuple:
    return (
        [(p.id, p.type, p.x, p.y, p.geo.width, p.geo.height) for p in d.placed],
        [(c.src, c.dst, c.kind, [tuple(q) for q in r.points]) for c, r in d.routes],
        d.added,
        [(g.rev, g.placed and g.placed.id) for g in d.ghosts],
        d.descs,
    )


@pytest.mark.parametrize("name", NAMES)
def test_a_frozen_sheet_reads_back_as_it_was(name, tmp_path):
    fs = _fs(name)
    built = fs.freeze()
    path = tmp_path / "frozen.json"
    fs.to_json(path)
    back = Flowsheet.from_json(path)
    assert back.frozen == fs.frozen
    assert _sheet_of(back.build()) == _sheet_of(built)


def test_a_frozen_sheet_is_not_laid_out_again(monkeypatch):
    fs = _fs("two_revisions")
    fs.freeze()

    def no_layout(*_a, **_k):
        raise AssertionError("laid out again")

    monkeypatch.setattr(Flowsheet, "_build_base", no_layout)
    fs.build()  # read back
    fs.build(until="R2")


def test_revisions_after_the_frozen_one_are_drawn_onto_it(monkeypatch):
    """Frozen through R1; R2 is drawn onto the frozen sheet, which stays as
    it was even when the layout would now come out otherwise."""
    fs = _fs("two_revisions")
    frozen = fs.freeze(until="R1")
    assert fs.frozen is not None and fs.frozen["through"] == "R1"
    shifted = json.loads(json.dumps(fs.frozen))
    for item in shifted["sheet"]["items"]:  # a sheet the layout would not make
        item[4] += 100
    for line in shifted["sheet"]["lines"]:
        line[3] = [[x + 100, y] for x, y in line[3]]
    fs.frozen = shifted
    after = fs.build()
    assert after.warnings == []
    tk = next(p for p in after.placed if p.id == "TK-1")
    was = next(p for p in frozen.placed if p.id == "TK-1")
    assert (tk.x, tk.y) == (was.x + 100, was.y)
    assert after.added["PI-1"] == "R2"
    # an earlier stage than the frozen one is laid out as written
    assert _sheet_of(fs.build(until="base")) == _sheet_of(
        _fs("two_revisions").build(until="base")
    )


def test_a_frozen_sheet_out_of_date_is_laid_out_anew():
    fs = _fs("two_revisions")
    fs.freeze(until="R1")
    fs.equipment[0].params["text"] = "IN-2"  # what it was built from changed
    d = fs.build()
    assert any("out of date" in w for w in d.warnings)
    fs.frozen = dict(fs.frozen or {}, through="R9")
    assert any("no revision 'R9'" in w for w in fs.build().warnings)


def test_cli_freeze(tmp_path, monkeypatch):
    from schemdraw_pfd.cli import main

    monkeypatch.chdir(tmp_path)
    spec = CASES / "revisions" / "two_revisions.json"
    out = tmp_path / "frozen.json"
    assert main([str(spec), "--freeze", str(out), "--revision", "R1", "-j", "1"]) == 0
    data = json.loads(out.read_text("utf-8"))
    assert data["frozen"]["through"] == "R1"
    assert not (tmp_path / "pfd.svg").exists()


ALL_CASES = sorted(
    p.relative_to(CASES).with_suffix("").as_posix()
    for p in CASES.rglob("*.json")
    if "invalid" not in p.parts
)


@pytest.mark.parametrize("name", ALL_CASES)
def test_every_sheet_can_be_frozen(name):
    """Every item is built from a recipe that reads back the same shape."""
    from conftest import built_case

    from schemdraw_pfd.freeze import from_data, to_data

    d = built_case(name)
    fs = Flowsheet.from_json(CASES / f"{name}.json")
    back = from_data(json.loads(json.dumps(to_data(d))), fs.library, fs.line_styles)

    def shapes(x: Diagram) -> list:
        return [
            (p.id, p.x, p.y, p.geo.width, p.geo.height, p.geo.shapes, p.geo.ports)
            for p in x.placed
        ]

    assert shapes(back) == shapes(d)
    assert [r.points for _, r in back.routes] == [r.points for _, r in d.routes]


def test_a_line_that_routes_nowhere_becomes_a_pair_of_flags(monkeypatch):
    """(As between the rows of a folded sheet.)  Forced here: the line from
    P-1 to the added exchanger finds no way."""
    from schemdraw_pfd.revise import _Sheet

    route = _Sheet.route

    def blocked(self, router, c):
        if (c.src, c.dst) == ("P-1.out", "E-1.tube_in"):
            return None
        return route(self, router, c)

    monkeypatch.setattr(_Sheet, "route", blocked)
    d = _fs("two_revisions").build(until="R1")
    flags = {p.id: p for p in d.placed if p.id.startswith(("_RO", "_RI"))}
    assert len(flags) == 2
    keys = [line_key(c) for c, _ in d.routes]
    out = next(i for i in flags if i.startswith("_RO"))
    back = next(i for i in flags if i.startswith("_RI"))
    # P-1.out carries a line: the "to" half taps it
    assert any(k.startswith("_J") and k.endswith(f"-> {out}.tail") for k in keys)
    assert f"{back}.tip -> E-1.tube_in" in keys
    # the pair goes as one when the line is removed
    fs = _fs("two_revisions")
    fs.revise("R3")
    fs.disconnect("P-1.out", "E-1.tube_in")
    d = fs.build()
    assert not any(p.id.startswith(("_RO", "_RI")) for p in d.placed)
