"""A long flow folded into rows (flow layout, ``rows: "auto"``).

The BTX fractionation example runs a feed through a preheater and three
columns in series (benzene, toluene, xylene).  With no order or row hints in
the file the sheet is folded where the main flow leaves one column for the
next, and before the first column: four rows, stacked top first.
"""

import json
from pathlib import Path

import pytest
from conftest import built, built_case

from schemdraw_pfd import Flowsheet

BTX = Path("examples/btx_fractionation.json")


@pytest.fixture(scope="module")
def btx():
    return built("btx_fractionation")


def _row_of(d):
    def row(p):
        mid = p.y + p.geo.height / 2
        return next(k for k, (lo, hi) in enumerate(d.rows) if lo - 1 <= mid <= hi + 1)

    return {p.id: row(p) for p in d.placed}


def test_the_file_has_no_order_or_row_hints():
    data = json.loads(BTX.read_text("utf-8"))
    assert "rows" not in data and "layout" not in data
    for e in data["elements"]:
        assert set(e) <= {"id", "type", "params", "desc"}


def test_btx_is_folded_into_four_rows_top_first(btx):
    assert len(btx.rows) == 4
    for (lo, _), (_, hi) in zip(btx.rows, btx.rows[1:]):
        assert hi < lo  # each row below the one before
    row = _row_of(btx)
    for i in ("FEED", "TK-201", "P-201", "E-201"):
        assert row[i] == 0  # the feed preheat train
    for i in ("T-210", "E-211", "V-211", "P-211", "E-212"):
        assert row[i] == 1  # the benzene column and its branches
    for i in ("P-212", "T-220", "V-221", "P-221", "E-222"):
        assert row[i] == 2  # what feeds the toluene column, and that column
    for i in ("P-222", "T-230", "V-231", "P-232", "E-202", "HEAVY"):
        assert row[i] == 3  # the xylene column and the heavy end


def test_items_on_inline_items_go_with_their_line(btx):
    row = _row_of(btx)
    assert row["FIC-201"] == row["FV-201"] == 0  # the feed flow controller
    # the toluene column's feed valve stands in front of the column, not
    # behind the benzene column's bottoms pump in the row above
    assert row["FIC-221"] == row["FV-221"] == row["T-220"] == 2


def test_utility_flags_stay_with_what_they_serve(btx):
    # the preheater's steam and condensate flags: not past the cut in the
    # flow order, on the row of the preheater
    row = _row_of(btx)
    assert row["LPS-201"] == row["SC-201"] == row["E-201"] == 0
    for n in ("21", "22", "23"):  # each column's cooling water and steam
        flags = (f"CWS-{n}", f"CWR-{n}", f"HPS-{n}", f"HSC-{n}")
        assert {row[f] for f in flags} == {row[f"T-2{n[1]}0"]}


def test_a_utility_flag_after_the_cut_stays_on_its_row():
    # the condensate flag of the last preheater sorts after the cut before
    # the first column: it stays beside the preheater, no flag pair for it
    d = built_case("layout/rows_utility_flags")
    texts = {p.geo.texts[0][2] for p in d.placed if p.id.startswith("_R")}
    assert texts == {"to T-1", "from E-2", "to P-2", "from T-1"}


def test_lines_between_rows_become_continuation_flags(btx):
    by = {p.id: p for p in btx.placed}
    out = sorted(i for i in by if i.startswith("_RO"))
    into = sorted(i for i in by if i.startswith("_RI"))
    assert [i[3:] for i in out] == [i[3:] for i in into]  # in pairs
    texts = {by[i].geo.texts[0][2] for i in out + into}
    assert texts == {
        "to T-210",
        "from E-201",
        "to P-212",
        "from T-210",
        "to P-222",
        "from T-220",
    }
    row = _row_of(btx)
    for o, i in zip(out, into):
        assert row[o] + 1 == row[i]  # the main flow runs on down the sheet
        mine = [p for p in btx.placed if row[p.id] == row[o] and p.id != o]
        assert by[o].x >= max(p.bbox[2] for p in mine if not p.geo.off_sheet)
        theirs = [p for p in btx.placed if row[p.id] == row[i] and p.id != i]
        assert by[i].bbox[2] <= min(p.x for p in theirs if not p.geo.off_sheet)
    for _, r in btx.routes:  # no line runs from one row to another
        ys = [y for _, y in r.points]
        assert len({_band(btx, y) for y in ys}) == 1


def _band(d, y):
    return next(k for k, (lo, hi) in enumerate(d.rows) if lo - 1 <= y <= hi + 1)


def test_one_column_or_single_rows_stay_on_one_row():
    assert not built("solvent_recovery").rows
    data = json.loads(BTX.read_text("utf-8"))
    data["rows"] = "single"
    fs = Flowsheet.from_dict(data)
    assert fs.rows == "single" and fs.to_dict()["rows"] == "single"
    with pytest.raises(ValueError, match="rows must be one of"):
        Flowsheet.from_dict({**data, "rows": "many"})


def test_btx_has_no_needless_u_turns(btx):
    # reboilers beside their columns, reflux drums under the condensers,
    # cooling water flags under the coolers: all plain Ls
    assert btx.warnings == []
