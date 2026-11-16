import math

import pytest

from schemdraw_pfd import CustomSpec, Flowsheet, element_names, register_element
from schemdraw_pfd.elements import get_element
from schemdraw_pfd.geometry import ray_hit
from schemdraw_pfd.layout import Placed
from schemdraw_pfd.render import find_crossings
from schemdraw_pfd.router import resolve_port


def _placed(type_, x=0, y=0, **params):
    return Placed("X", type_, 0, get_element(type_).build(params), x, y)


# -- 2. initial elements ------------------------------------------------------
def test_initial_elements_registered():
    assert {"pump", "valve", "vertical_drum", "heat_exchanger", "header"} <= set(
        element_names()
    )


def test_relative_sizes():
    area = {
        n: (g.width * g.height)
        for n in ("valve", "pump", "heat_exchanger", "vertical_drum")
        for g in [get_element(n).build({})]
    }
    assert area["valve"] < area["pump"] < area["heat_exchanger"] < area["vertical_drum"]


def test_header_branch_count_and_equal_pitch():
    geo = get_element("header").build({"branches": 5})
    xs = [geo.ports[f"b{i}"].offset for i in range(5)]
    assert {b - a for a, b in zip(xs, xs[1:])} == {2}
    assert all(geo.ports[f"b{i}"].side == "N" for i in range(5))  # all rise in +Y


def test_header_widens_only_where_elements_are_inserted():
    plain = get_element("header").build({"branches": 4})
    valved = get_element("header").build(
        {"branches": 4, "branch_elements": {0: "valve", 1: ["valve", "valve"]}}
    )
    xs = [valved.ports[f"b{i}"].offset for i in range(4)]
    assert xs[1] - xs[0] == 3  # two valves of width 2 + 1 clearance
    assert xs[2] - xs[1] == 2  # valve next to a bare branch still fits the pitch
    assert xs[3] - xs[2] == 2
    assert valved.width == plain.width + 1  # main pipe grew by exactly the extra
    assert valved.height == 1 + 2 * (2 + 1)  # two stacked valves


# -- 4. connection points on the outline ---------------------------------------
def test_pump_ports_on_outline():
    p = _placed("pump", 5, 3)
    t = resolve_port(p, "in")
    assert t.point == pytest.approx((5, 5))  # horizontal suction
    assert math.dist(t.point, (6, 5)) == pytest.approx(1.0)  # on the casing circle
    assert t.first == (4, 5)
    t = resolve_port(p, "out")  # vertical discharge, tangent to the east edge
    assert t.point == pytest.approx((7, 6))
    assert t.first == (7, 7)
    assert set(p.geo.ports) == {"in", "out"}


# -- P&ID style symbols ----------------------------------------------------------
def test_valve_kinds_and_orientation():
    for kind in ("gate", "globe", "control", "check"):
        h = _placed("valve", kind=kind)
        assert resolve_port(h, "in").point == pytest.approx((0, 1))
        assert resolve_port(h, "out").point == pytest.approx((2, 1))
        v = _placed("valve", kind=kind, orientation="vertical")
        assert resolve_port(v, "in").point == pytest.approx((1, 0))
        assert resolve_port(v, "out").point == pytest.approx((1, 2))
    ctrl = _placed("valve", kind="control")
    assert resolve_port(ctrl, "signal").point == pytest.approx((1, 2))  # dome top
    with pytest.raises(ValueError):
        _placed("valve", kind="butterfly")


def test_instrument_bubbles():
    field = _placed("instrument", function="PG", number="201")
    dcs = _placed("instrument", function="FIC", number="201", mount="dcs")
    for inst in (field, dcs):
        assert resolve_port(inst, "w").point == pytest.approx((0, 1))
        assert resolve_port(inst, "n").point == pytest.approx((1, 2))
    assert [t[2] for t in dcs.geo.texts] == ["FIC", "201"]
    assert len(dcs.geo.shapes) == len(field.geo.shapes) + 1  # square around it


def test_tee_connector_and_column():
    tee = _placed("tee", arms="wes")
    assert set(tee.geo.ports) == {"w", "e", "s"}
    assert resolve_port(tee, "s").point == pytest.approx((1, 0))
    flag = _placed("connector", text="PID-001\nfrom P-103")
    assert resolve_port(flag, "tip").point == pytest.approx((6, 1))
    left = _placed("connector", point="left")
    assert resolve_port(left, "tip").point == pytest.approx((0, 1))
    col = get_element("column").build({})
    assert (col.width, col.height) == (4, 20) and col.nozzles
    hd = _placed("horizontal_drum")
    assert resolve_port(hd, "w").point == pytest.approx((0, 2))


def test_signal_lines_and_port_alignment():
    fs = Flowsheet()
    fs.add("valve", "FV", kind="control")
    fs.add("instrument", "FIC", elevation=13, function="FIC", number="1", mount="dcs")
    fs.add("column", "C")
    fs.connect("FIC.w", "FV.signal", kind="signal")
    fs.connect("FV.out", "C.w8")
    with pytest.raises(ValueError):
        fs.connect("FV.out", "C.w9", kind="steam")
    d = fs.build()
    assert not d.warnings
    by = {p.id: p for p in d.placed}
    assert by["FIC"].y == 13
    assert by["FV"].y + 1 == by["C"].y + by["C"].geo.ports["w8"].offset  # lined up
    assert [r.bends for _, r in d.routes] == [1, 0]
    assert d.routes[0][0].kind == "signal"


def test_distillation_example_builds_strictly():
    d = Flowsheet.from_json("examples/distillation.json").build()
    assert not d.warnings
    assert d.descs["C-201"].startswith("蒸留塔")


def test_drum_head_and_wall_points():
    geo = get_element("vertical_drum").build({})
    assert ray_hit(geo.shapes, (3, 11), (0, -1)) == pytest.approx(1.0)  # apex
    assert ray_hit(geo.shapes, (-1, 5), (1, 0)) == pytest.approx(1.0)  # wall


def test_port_follows_curved_outline():
    # a port away from the apex of an ellipse still lands on the ellipse
    register_element(
        CustomSpec.from_dict(
            {
                "name": "tank_t",
                "width": 4,
                "height": 2,
                "shapes": [{"type": "ellipse", "cx": 2, "cy": 1, "rx": 2, "ry": 1}],
                "ports": {"in": {"side": "W", "offset": 1}},
            }
        )
    )
    fs = Flowsheet()
    fs.add("tank_t", "T", ports={"vent": ("N", 1)})
    geo = fs.equipment[0].geometry()
    t = resolve_port(Placed("T", "tank_t", 0, geo, 0, 0), "vent")
    x, y = t.point
    assert x == 1 and ((x - 2) / 2) ** 2 + (y - 1) ** 2 == pytest.approx(1.0)
    assert t.edge == (1, 2)  # leaves perpendicular to the top face, on the grid
    fs.build()


# -- 3. layout ---------------------------------------------------------------
def test_levels_never_overlap_in_x_and_order_is_kept():
    fs = Flowsheet()
    fs.add("pump", "A", level=0)
    fs.add("vertical_drum", "B", level=2)
    fs.add("pump", "C", level=1)
    fs.add("pump", "D", level=0)
    d = fs.build()
    assert [p.id for p in d.placed] == ["A", "B", "C", "D"]
    for a, b in zip(d.placed, d.placed[1:]):
        assert b.x >= a.x + a.geo.width  # separate columns, strictly in order
    y = {p.id: p.y for p in d.placed}
    assert y["A"] == y["D"] == 0 and y["A"] < y["C"] < y["B"]


def test_unconnected_elements_are_packed_to_the_limit():
    fs = Flowsheet()
    fs.add("pump", "A")
    fs.add("pump", "B")
    fs.add("valve", "C", level=1)
    a, b, c = fs.build().placed
    assert b.x - (a.x + a.geo.width) == 1
    # C is on another level, so it can never touch B: its column may abut B's
    assert c.x - (b.x + b.geo.width) == 0
    assert c.y - (a.y + a.geo.height) == 1


def test_facing_ports_join_across_a_single_cell():
    fs = Flowsheet()
    for i in "ABC":
        fs.add("valve", i)
    fs.connect("A.out", "B.in")
    fs.connect("B.out", "C.in")
    d = fs.build()
    a, b, c = d.placed
    assert b.x - (a.x + 2) == 1 and c.x - (b.x + 2) == 1
    assert [r.bends for _, r in d.routes] == [0, 0]
    assert d.routes[0][1].points == [(2, 1), (3, 1)]


def test_edge_flags_share_a_column_and_stack():
    fs = Flowsheet()
    fs.add("connector", "IN1", elevation=6, text="a")
    fs.add("connector", "IN2", elevation=9, text="b")
    fs.add("horizontal_drum", "V")
    fs.add("valve", "GV")
    fs.add("connector", "OUT1", text="c")
    fs.add("connector", "OUT2", text="d", length=8)
    fs.connect("IN1.tip", "V.n1")
    fs.connect("IN2.tip", "V.n3")
    fs.connect("V.e", "GV.in")
    fs.connect("GV.out", "OUT1.tail")
    d = fs.build()
    assert not d.warnings
    by = {p.id: p for p in d.placed}
    assert by["IN1"].x == by["IN2"].x == 0  # stacked on the left edge
    assert by["OUT1"].x == by["OUT2"].x  # stacked on the right edge
    assert by["OUT1"].x >= by["GV"].bbox[2] + 1
    for lo, hi in (("IN1", "IN2"), ("OUT1", "OUT2")):
        a, b = sorted((by[lo], by[hi]), key=lambda p: p.y)
        assert b.y >= a.bbox[3] + 1  # never on top of each other
    equipment = [p for p in d.placed if not p.geo.off_sheet]
    for a, b in zip(equipment, equipment[1:]):
        assert b.x >= a.bbox[2]


def test_everything_is_on_the_grid():
    d = Flowsheet.from_json("examples/sample.json").build()
    for p in d.placed:
        assert all(isinstance(v, int) for v in p.bbox)
    for _, r in d.routes:
        for x, y in r.points[1:-1]:  # all corners (outline ends may be on a curve)
            assert x == int(x) and y == int(y)


# -- 5. routing --------------------------------------------------------------
def _directions(points):
    out = []
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        assert ax == bx or ay == by  # orthogonal
        out.append(((bx > ax) - (bx < ax), (by > ay) - (by < ay)))
    return out


def test_routes_have_no_u_or_i_turns_and_enter_perpendicular():
    d = Flowsheet.from_json("examples/sample.json").build()
    assert not d.warnings
    by = {p.id: p for p in d.placed}
    for c, r in d.routes:
        dirs = _directions(r.points)
        for dx, dy in dirs:  # never travels both ways along an axis
            assert (-dx, -dy) not in dirs
        (si, sp), (di, dp) = c.ends
        s, t = resolve_port(by[si], sp), resolve_port(by[di], dp)
        # leaves / arrives along the face normal with a straight run of >= 1 cell
        out = (s.first[0] - s.edge[0], s.first[1] - s.edge[1])
        into = (t.edge[0] - t.first[0], t.edge[1] - t.first[1])
        assert dirs[0] == out and dirs[-1] == into
        assert math.dist(r.points[0], r.points[1]) >= 1 - 1e-9
        assert math.dist(r.points[-1], r.points[-2]) >= 1 - 1e-9


def test_fewest_bends_first():
    fs = Flowsheet()
    fs.add("vertical_drum", "D")
    fs.add("pump", "P")
    fs.add("heat_exchanger", "E", level=1)
    fs.connect("D.w2", "P.in")  # replaced below
    fs.connections.clear()
    fs.connect("D.e0", "P.in")
    fs.connect("P.out", "E.tube_in")
    d = fs.build()
    assert [r.bends for _, r in d.routes] == [0, 1]  # I shape, L shape


def test_unavoidable_uturn_is_reported():
    fs = Flowsheet()
    fs.add("header", "H", level=1)
    fs.add("pump", "P", level=0)
    fs.connect("H.b0", "P.in")  # branch points up, pump is below
    assert fs.build().warnings


def test_shared_port_limits():
    fs = Flowsheet()
    for i in "ABCDE":
        fs.add("valve", i)
    for i in "BCDE":
        fs.connect("A.out", f"{i}.in")
    with pytest.raises(ValueError, match="at most 3"):
        fs.build()  # more than a tee can take: use a header
    fs = Flowsheet()
    for i in "ABC":
        fs.add("valve", i)
    fs.connect("A.out", "B.in")
    fs.connect("C.out", "A.out")
    with pytest.raises(ValueError, match="both the start and the end"):
        fs.build()


# -- rendering ---------------------------------------------------------------
def test_crossings_are_detected():
    paths = [[(0, 1), (4, 1)], [(2, 0), (2, 3)], [(4, 0), (4, 3)]]
    assert find_crossings(paths) == [(0, 0, 1, 0, (2, 1))]  # touching end != crossing


@pytest.mark.parametrize("crossing", ["hop", "gap"])
def test_render(tmp_path, crossing):
    fs = Flowsheet.from_json("examples/sample.json")
    out = tmp_path / "x.svg"
    d = fs.render(out, crossing=crossing)
    assert find_crossings([r.points for _, r in d.routes])  # sample has a crossing
    assert out.stat().st_size > 1000


def test_reference_pid_reproduction_routes_strictly():
    # the P&ID example sheet: 40 elements, 44 lines, custom + vertical equipment
    fs = Flowsheet.from_json("examples/pid_c201.json")
    d = fs.build(compact=False)
    assert not d.warnings
    assert len(d.routes) == len(fs.connections) == 44
    by = {p.id: p for p in d.placed}
    equipment = [p for p in d.placed if not p.geo.off_sheet]
    for a, b in zip(equipment, equipment[1:]):
        assert b.x >= a.x + a.geo.width
    flags = [p for p in d.placed if p.geo.off_sheet]
    assert len({p.x for p in flags}) == 2  # one column per sheet edge
    # valve train in front of the column sits on one straight line
    assert by["GV1"].y == by["FV-201"].y == by["GV2"].y
    assert by["E-202"].geo.height > by["E-202"].geo.width  # stands vertically
