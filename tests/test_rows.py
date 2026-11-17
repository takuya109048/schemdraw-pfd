"""A long flow folded into rows (flow layout, ``rows: "auto"``).

The crude distillation example runs crude through a preheat train, an
atmospheric column and a vacuum column.  With no order or row hints in the
file the sheet is folded where the main flow leaves one column for the next,
and before the first column: three rows, stacked top first.
"""

import json
from pathlib import Path

import pytest
from conftest import built

from schemdraw_pfd import Flowsheet

CRUDE = Path("examples/crude_distillation.json")


@pytest.fixture(scope="module")
def crude():
    return built("crude_distillation")


def _row_of(d):
    def row(p):
        mid = p.y + p.geo.height / 2
        return next(k for k, (lo, hi) in enumerate(d.rows) if lo - 1 <= mid <= hi + 1)

    return {p.id: row(p) for p in d.placed}


def test_the_file_has_no_order_or_row_hints():
    data = json.loads(CRUDE.read_text("utf-8"))
    assert "rows" not in data
    for e in data["elements"]:
        assert set(e) <= {"id", "type", "level", "params", "desc", "span"}


def test_crude_is_folded_into_three_rows_top_first(crude):
    assert len(crude.rows) == 3
    (lo0, _), (lo1, hi1), (_, hi2) = crude.rows
    assert hi1 < lo0 and hi2 < lo1  # each row below the one before
    row = _row_of(crude)
    for i in ("CRUDE", "P-101", "V-101", "V-102", "H-101"):
        assert row[i] == 0  # the feed preheat train
    for i in ("T-101", "T-102", "V-103", "P-103", "E-112"):
        assert row[i] == 1  # the atmospheric column and its branches
    for i in ("P-107", "H-201", "T-201", "V-201", "P-203"):
        assert row[i] == 2  # what feeds the vacuum column, and that column


def test_items_on_inline_items_go_with_their_line(crude):
    row = _row_of(crude)
    assert row["FIC-101"] == row["FV-101"] == 0  # the charge flow controller
    assert row["MSTM"] == row["J-201"] == 2  # motive steam of the ejector


def test_lines_between_rows_become_continuation_flags(crude):
    by = {p.id: p for p in crude.placed}
    out = sorted(i for i in by if i.startswith("_RO"))
    into = sorted(i for i in by if i.startswith("_RI"))
    assert [i[3:] for i in out] == [i[3:] for i in into]  # in pairs
    texts = {by[i].geo.texts[0][2] for i in out + into}
    assert {"to T-101", "from H-101", "to P-107", "from T-101"} <= texts
    row = _row_of(crude)
    for o, i in zip(out, into):
        assert row[o] < row[i]  # the main flow runs on down the sheet
        mine = [p for p in crude.placed if row[p.id] == row[o] and p.id != o]
        assert by[o].x >= max(p.bbox[2] for p in mine if not p.geo.off_sheet)
        theirs = [p for p in crude.placed if row[p.id] == row[i] and p.id != i]
        assert by[i].bbox[2] <= min(p.x for p in theirs if not p.geo.off_sheet)
    for _, r in crude.routes:  # no line runs from one row to another
        ys = [y for _, y in r.points]
        assert len({_band(crude, y) for y in ys}) == 1


def _band(d, y):
    return next(k for k, (lo, hi) in enumerate(d.rows) if lo - 1 <= y <= hi + 1)


def test_one_column_or_single_rows_stay_on_one_row():
    assert not Flowsheet.from_json("examples/hda_benzene.json").build().rows
    data = json.loads(CRUDE.read_text("utf-8"))
    data["rows"] = "single"
    fs = Flowsheet.from_dict(data)
    assert fs.rows == "single" and fs.to_dict()["rows"] == "single"
    with pytest.raises(ValueError, match="rows must be one of"):
        Flowsheet.from_dict({**data, "rows": "many"})
