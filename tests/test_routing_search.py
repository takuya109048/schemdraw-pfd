"""Router search (A*) and rip-up & reroute."""

import copy
import json
import random
from pathlib import Path

import pytest

from schemdraw_pfd import Flowsheet, RoutingError, model, router
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


def _random_router(
    rng: random.Random,
    size: int = 7,
    walls: float = 0.15,
    pipes: float = 0.5,
    doors: float = 1 / 3,
) -> Router:
    """A router on a ``size`` x ``size`` area with random obstacles, routed
    pipes and doorsteps (no equipment needed); the shares of cells blocked,
    on a pipe and on a doorstep."""
    r = object.__new__(Router)
    r.x0 = r.y0 = -1
    r.h = h = size + 2
    n = h * h
    r.blocked = bytearray(n)
    for i in range(n):
        x, y = divmod(i, h)
        frame = x in (0, h - 1) or y in (0, h - 1)
        r.blocked[i] = 1 if frame or rng.random() < walls else 0
    r.axes = bytearray(
        rng.choice((1, 2, 3)) if rng.random() < pipes else 0 for _ in range(n)
    )
    r.door = bytearray(
        rng.choice((1, 2)) if rng.random() < doors else 0 for _ in range(n)
    )
    r.steps = (1, h, -1, -h)
    r._coords = None
    return r


def _flood_cell_by_cell(r: Router, s, sd: int, t, td: int, strict: bool) -> bool:
    """The search's states flooded one by one: what ``Router._reachable``
    (on bit boards) must answer -- whether the search gets there at all."""
    blocked, axes, door, steps = r.blocked, r.axes, r.door, r.steps
    si, ti = r._index(s), r._index(t)
    if axes[ti]:
        return False
    if si == ti and sd == td:
        return True  # the search starts on its goal
    arch = sd == (td + 2) % 4
    start = (si, sd, (1 << sd) if strict else 0)
    seen, todo = {start}, [start]
    while todo:
        i, d, mask = todo.pop()
        here = axes[i] or (door[i] and i != si)
        for nd in (d, (d + 1) % 4, (d + 3) % 4):
            if nd != d and here:
                continue  # no turn on a pipe or another port's doorstep
            if strict and mask & 1 << (nd + 2) % 4 and not (arch and nd == td):
                continue  # against a direction travelled
            n, axis = i + steps[nd], 1 << nd % 2
            if blocked[n] or axes[n] & axis or (door[n] == axis and n != ti):
                continue
            if n == ti and nd == td:
                return True
            k = (n, nd, (mask | 1 << nd) if strict else 0)
            if k not in seen:
                seen.add(k)
                todo.append(k)
    return False


def test_flood_on_bit_boards_answers_like_the_flood_cell_by_cell():
    rng = random.Random(7)
    answers = set()
    for _ in range(2000):
        size = rng.choice((3, 6, 11, 18))
        r = _random_router(
            rng,
            size,
            walls=rng.choice((0, 0.1, 0.3)),
            pipes=rng.choice((0, 0.1, 0.4)),
            doors=rng.choice((0, 0.05, 0.2)),
        )
        s = (rng.randrange(size), rng.randrange(size))
        t = s if rng.random() < 0.05 else (rng.randrange(size), rng.randrange(size))
        sd, td = rng.randrange(4), rng.randrange(4)
        for strict in (True, False):
            exact = _flood_cell_by_cell(r, s, sd, t, td, strict)
            assert r._reachable(s, sd, t, td, strict) is exact
            answers.add((strict, exact))
    assert len(answers) == 4  # both answers, in both modes


def test_staircase_never_calls_off_a_routable_line():
    # the staircase is a necessary condition of the exact flood (which fails
    # exactly when the search does): its "no" must never meet a "yes" from
    # it -- and it does say "no" often
    rng = random.Random(7)
    said_no = 0
    for _ in range(3000):
        r = _random_router(rng)
        s = (rng.randrange(7), rng.randrange(7))
        t = (rng.randrange(7), rng.randrange(7))
        sd, td = rng.randrange(4), rng.randrange(4)
        if sd != (td + 2) % 4 and not r._staircase(s, sd, t, td):
            said_no += 1
            assert not r._reachable(s, sd, t, td, True)
    assert said_no > 1000


def test_dropping_dearer_states_finds_the_very_same_route(monkeypatch):
    # once a search drags on, states that cannot beat a route known are
    # dropped (see Router._search): the route found must not change, ties
    # (equally cheap routes, crossings) included
    rng = random.Random(11)
    found = 0
    for _ in range(300):
        size = rng.choice((4, 8, 13, 20))
        r = _random_router(
            rng,
            size,
            walls=rng.choice((0, 0.1, 0.25)),
            pipes=rng.choice((0, 0.1, 0.3)),
            doors=rng.choice((0, 0.05)),
        )
        s = (rng.randrange(size), rng.randrange(size))
        t = (rng.randrange(size), rng.randrange(size))
        sd, td = rng.randrange(4), rng.randrange(4)
        for strict in (True, False):
            monkeypatch.setattr(router, "FLOOD_AFTER", 10**9)  # never
            plain = r._search(s, sd, t, td, strict)
            monkeypatch.setattr(router, "FLOOD_AFTER", 2)
            assert r._search(s, sd, t, td, strict) == plain
            found += plain is not None
    assert found > 150


def test_bend_levels_count_the_fewest_bends_to_the_goal():
    # an L around the corner of a wall: one bend from the start
    rng = random.Random(5)
    r = _random_router(rng, 6, walls=0, pipes=0, doors=0)
    for y in range(0, 5):
        r.blocked[r._index((2, y))] = 1  # a wall up the middle, open at the top
    found = r._bend_levels((0, 0), 0, (5, 0), 2, strict=False)
    assert found is not None
    levels, cost = found
    start, goal = r._index((0, 0)), r._index((5, 0))
    assert levels[0 * 16][start] == 2  # up, across the top, down: two bends
    assert levels[2 * 16][goal] == 0
    assert cost == 2 * router.TURN_COST + 5 + 5 + 5
    r.blocked[r._index((2, 5))] = 1  # the wall closed: no route
    assert r._bend_levels((0, 0), 0, (5, 0), 2, strict=False) is None


def test_routes_alone_tells_whether_the_line_routes_with_no_pipes_laid():
    # what rip-up and reroute relies on: a line that does not route alone
    # routes in no order (see Flowsheet._route_all)
    from schemdraw_pfd.router import Terminal

    def end(cell, out):
        edge = (cell[0] - DIRS[out][0], cell[1] - DIRS[out][1])
        return Terminal(edge, edge, cell, out)

    rng = random.Random(3)
    seen = set()
    for _ in range(600):
        r = _random_router(rng, 8, walls=0.2, pipes=0.3, doors=0.1)
        a = (rng.randrange(8), rng.randrange(8))
        b = (rng.randrange(8), rng.randrange(8))
        src, dst = end(a, rng.randrange(4)), end(b, rng.randrange(4))
        for relaxed in (True, False):
            alone = r.routes_alone(src, dst, relaxed)
            laid, r.axes = r.axes, bytearray(len(r.axes))
            routed = r.route(src, dst, relaxed, commit=False) is not None
            r.axes = laid
            assert alone is routed
            seen.add(alone)
    assert seen == {True, False}


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


def test_flanged_nozzles_facing_on_one_row_stand_two_cells_apart():
    from schemdraw_pfd.elements import DEFAULT_LIBRARY
    from schemdraw_pfd.layout import Link, _link_min_x

    geo = DEFAULT_LIBRARY.build("heat_exchanger")
    plain = Link(0, "E", 2, 1, "W", 2)
    flanged = Link(0, "E", 2, 1, "W", 2, flanged=True)
    assert _link_min_x(plain, 0, geo, 0, 0) == geo.width + 1
    assert _link_min_x(flanged, 0, geo, 0, 0) == geo.width + 2


@pytest.mark.parametrize("name", ["routing/flow/n_to_s_up", "routing/flow/s_to_n_down"])
def test_flanged_nozzles_stacked_on_two_levels_stand_two_cells_apart(name):
    data = json.loads((CASES / f"{name}.json").read_text("utf-8"))
    ((c, r),) = Flowsheet.from_dict(data).build().routes
    assert r.bends == 0 and abs(r.points[1][1] - r.points[0][1]) == 2
