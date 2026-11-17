"""Router search (A*) and rip-up & reroute."""

import pytest

from schemdraw_pfd import Flowsheet, RoutingError, model
from schemdraw_pfd.router import DIRS, _estimate, _min_bends, needless_uturn

# found by fuzzing: routed in the given order, an early line takes the only
# corridor of a later one; routing the failing line first solves it
CORRIDOR_CLASH = {
    "layout": "order",  # written in the order the items stand
    "elements": [
        {"id": "E0", "type": "vertical_drum", "level": 0},
        {"id": "E1", "type": "valve", "level": 1},
        {"id": "E2", "type": "valve", "level": 1},
        {"id": "E3", "type": "vertical_drum", "level": 0},
    ],
    "connections": [
        {"from": "E2.in", "to": "E0.e6"},
        {"from": "E3.e6", "to": "E0.w4"},
        {"from": "E3.bottom", "to": "E1.out"},
    ],
}


def test_rip_up_and_reroute_solves_a_corridor_clash(monkeypatch):
    monkeypatch.setattr(model, "REROUTE_ROUNDS", 0)
    with pytest.raises(RoutingError):
        Flowsheet.from_dict(CORRIDOR_CLASH).build(compact=False)
    monkeypatch.setattr(model, "REROUTE_ROUNDS", 3)
    d = Flowsheet.from_dict(CORRIDOR_CLASH).build()
    # the routes are reported in connection order, whatever order they took
    assert [c.src for c, _ in d.routes] == ["E2.in", "E3.e6", "E3.bottom"]


@pytest.mark.parametrize(
    ("p", "d", "t", "td", "bends"),
    [
        ((0, 0), 1, (5, 0), 1, 0),  # straight ahead, already the right way
        ((0, 0), 1, (5, 2), 1, 2),  # right way but off the line: a jog
        ((6, 0), 1, (5, 0), 1, 2),  # target behind
        ((0, 0), 3, (5, 0), 1, 2),  # moving the opposite way
        ((0, 0), 0, (5, 3), 1, 1),  # perpendicular
    ],
)
def test_bend_estimate(p, d, t, td, bends):
    assert _min_bends(p, d, t, td) == bends


def test_estimate_is_consistent():
    # moving one cell (cost 1, +TURN_COST when turning) never lowers the
    # estimate by more than the step costs: A* stays optimal
    from schemdraw_pfd.router import TURN_COST

    t, td = (3, 2), 1
    for x in range(-2, 6):
        for y in range(-2, 5):
            for d in range(4):
                h = _estimate((x, y), d, t, td)
                for nd in (d, (d + 1) % 4, (d + 3) % 4):
                    dx, dy = DIRS[nd]
                    step = 1 + (TURN_COST if nd != d else 0)
                    assert h <= step + _estimate((x + dx, y + dy), nd, t, td)


# -- which U-turns are worth a warning ---------------------------------------
@pytest.mark.parametrize(
    ("points", "boxes", "needless"),
    [
        # down, under a vessel, up: goes around it
        ([(0, 5), (0, 0), (10, 0), (10, 5)], [(3, 1, 7, 6)], False),
        # the same U with nothing in it
        ([(0, 5), (0, 0), (10, 0), (10, 5)], [(20, 0, 25, 5)], True),
        # a hop over a small item whose top edge is on the line's own row
        ([(0, 8), (3, 8), (3, 9), (7, 9), (7, 8), (9, 8)], [(4, 6, 6, 8)], False),
        # a dip in front of a nozzle: the vessel touches but is not inside
        ([(8, 2), (2, 2), (2, 1), (1, 1), (1, 3)], [(-3, 3, 7, 7)], True),
        # an L never turns back
        ([(0, 0), (5, 0), (5, 5)], [], False),
    ],
)
def test_needless_uturn(points, boxes, needless):
    assert needless_uturn(points, boxes) is needless
