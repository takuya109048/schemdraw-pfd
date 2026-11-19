"""Router search (A*) and rip-up & reroute."""

import copy
import json
import random
from pathlib import Path

import pytest

from schemdraw_pfd import Flowsheet, RoutingError, model
from schemdraw_pfd.levels import start_levels
from schemdraw_pfd.netlist import Equipment
from schemdraw_pfd.router import DIRS, Router, _estimate, _min_bends, needless_uturn

EXAMPLES = Path(__file__).parent.parent / "examples"
CASES = Path(__file__).parent / "cases"

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


def _random_router(rng: random.Random, size: int = 7) -> Router:
    """A router on a ``size`` x ``size`` area with random obstacles, routed
    pipes and doorsteps (no equipment needed)."""
    r = object.__new__(Router)
    r.x0 = r.y0 = -1
    r.h = h = size + 2
    n = h * h
    r.blocked = bytearray(n)
    for i in range(n):
        x, y = divmod(i, h)
        frame = x in (0, h - 1) or y in (0, h - 1)
        r.blocked[i] = 1 if frame or rng.random() < 0.15 else 0
    r.axes = bytearray(rng.choice((0, 0, 0, 1, 2, 3)) for _ in range(n))
    r.door = bytearray(rng.choice((0, 0, 0, 0, 1, 2)) for _ in range(n))
    r.steps = (1, h, -1, -h)
    r._coords = None
    return r


def test_cheap_checks_never_call_off_a_routable_line():
    # the staircase and the cell flood are necessary conditions of the exact
    # flood (which fails exactly when the search does): a "no" from either
    # must never meet a "yes" from it -- and they do say "no" often
    rng = random.Random(7)
    said_no = 0
    for _ in range(3000):
        r = _random_router(rng)
        s = (rng.randrange(7), rng.randrange(7))
        t = (rng.randrange(7), rng.randrange(7))
        sd, td = rng.randrange(4), rng.randrange(4)
        for strict in (True, False):
            exact = r._reachable(s, sd, t, td, strict)
            if not r._cells_reachable(s, t, td):
                said_no += 1
                assert not exact
            if strict and sd != (td + 2) % 4 and not r._staircase(s, sd, t, td):
                said_no += 1
                assert not exact
    assert said_no > 1000


def test_level_floor_never_exceeds_the_quick_cost():
    fs = Flowsheet.from_json(EXAMPLES / "parallel_trains.json")
    fs.jobs = 1
    sheet = copy.copy(fs)
    sheet.equipment = fs._with_levels()
    sheet._in_order = sheet._flag_room = False
    sheet._quick = True
    floor = sheet._level_floor(model.QUICK_EXPAND)
    cost = model._run_level_cost(fs, start_levels(fs.equipment), 4)
    assert cost is not None and 0 < floor <= cost
    # a bound it cannot beat skips the routing
    assert model._run_level_cost(fs, start_levels(fs.equipment), 4, floor) is None


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


def _on(level: int, span: int = 1) -> Equipment:
    return Equipment("X", "vertical_drum", level, span=span)


@pytest.mark.parametrize(
    ("src", "out", "dst", "into", "forced"),
    [
        # a pump turned upside down feeding a flag on the level above
        (_on(0), "S", _on(1), "W", True),
        # the same on one level: placing the items apart may avoid it
        (_on(0), "S", _on(0), "W", False),
        (_on(1), "S", _on(0), "W", False),  # down out, down on: no turning back
        (_on(1), "N", _on(0), "W", True),  # up out, on to a level below
        # a tower's top on its second level, a drum beside it on that level
        (_on(0, span=2), "N", _on(1), "W", False),
        (_on(1), "E", _on(0), "S", True),  # into a bottom nozzle from above
        (_on(0), "E", _on(1), "N", True),  # into a top nozzle from below
        (_on(0), "E", _on(1), "W", False),  # side to side: never forced
    ],
)
def test_ports_facing_away_from_the_other_level_force_a_uturn(
    src, out, dst, into, forced
):
    assert model._forced_back(src, out, dst, into) is forced


def test_a_uturn_forced_by_the_levels_is_counted_but_not_warned():
    data = json.loads((CASES / "transform" / "pump_r180.json").read_text("utf-8"))
    d = Flowsheet.from_dict(data).build()
    assert d.uturns == 1 and not d.warnings
