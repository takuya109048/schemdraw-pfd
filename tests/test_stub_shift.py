"""Lines whose ports stand too close for the straight runs out of and into
them: the items move apart instead of the line turning back."""

from schemdraw_pfd import Flowsheet, model
from schemdraw_pfd.model import _reaches, _turns_back

S, E = 2, 1  # router directions


def test_reaches_needs_room_for_both_straight_runs():
    # down out of a port, then east into one: the target must not be above
    assert _reaches((0, 0), S, (4, 0), E)
    assert _reaches((0, 0), S, (4, -3), E)
    assert not _reaches((0, 0), S, (4, 2), E)
    # down out, down in: the target must be at least a cell lower
    assert not _reaches((0, 0), S, (4, 0), S)
    assert _reaches((0, 0), S, (4, -1), S)


def test_turns_back_spots_opposite_runs():
    assert _turns_back([(0, 0), (0, -1), (3, -1), (3, 1), (5, 1)])
    assert not _turns_back([(0, 0), (0, -1), (3, -1), (3, -4)])


def _three_columns() -> Flowsheet:
    """One feed pump sharing its discharge among three columns' side
    nozzles, the overheads merging into a flag held on level 2 and the
    bottoms pumped to a common flag."""
    fs = Flowsheet(layout="flow")
    fs.add("connector", "FEED", text="feed")
    fs.add("pump", "P-1")
    for k in (1, 2, 3):
        fs.add("column", f"C-{k}")
        fs.add("pump", f"P-{k}1")
    fs.add("connector", "OVHD", text="overhead", level=2)
    fs.add("connector", "BTMS", text="bottoms")
    fs.connect("FEED.tip", "P-1.in")
    for k in (1, 2, 3):
        fs.connect("P-1.out", f"C-{k}.w8")
        fs.connect(f"C-{k}.top", "OVHD.tail")
        fs.connect(f"C-{k}.bottom", f"P-{k}1.in")
        fs.connect(f"P-{k}1.out", "BTMS.tail")
    return fs


def _feeds_turning_back(d) -> list[str]:
    return sorted(
        c.dst for c, r in d.routes if c.dst.endswith(".w8") and _turns_back(r.points)
    )


def test_branch_into_a_port_above_the_main_pipe_lowers_the_column(monkeypatch):
    # the overhead flag on level 2 lifts the columns: the feed main pipe
    # stands level with the nozzles, and a branch would drop out of it and
    # come back up into the column -- unless the column is lowered for it
    monkeypatch.setattr(model, "STUB_ROUNDS", 0)
    assert _feeds_turning_back(_three_columns().build()) == ["C-1.w8", "C-2.w8"]
    monkeypatch.undo()
    assert _feeds_turning_back(_three_columns().build()) == []
