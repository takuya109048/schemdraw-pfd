"""Lines whose ports stand too close for the straight runs out of and into
them: the items move apart instead of the line turning back."""

import json
from pathlib import Path

from schemdraw_pfd import Flowsheet
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


def test_branch_into_a_port_above_the_main_pipe_lowers_the_column():
    # the column trains with the overhead flag held on level 2 used to send
    # the feed to C-101 down out of the main pipe and back up into the column
    data = json.loads(Path("examples/column_trains.json").read_text("utf-8"))
    for e in data["elements"]:
        if e["id"] == "OVHD":
            e["level"] = 2
    d = Flowsheet.from_dict(data).build()
    feed = next(r for c, r in d.routes if c.dst == "C-101.w8")
    assert not _turns_back(feed.points)
