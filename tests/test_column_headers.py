"""Automatic main pipes (shared ports) around distillation columns.

Each case builds a small column system in the flow layout and checks that the
main pipes come out tidy: no needless U-turns, straight main pipes, taps
beside the nozzles they serve, and repeated columns drawn alike.
"""

from schemdraw_pfd import Flowsheet


def _by_id(d):
    return {p.id: p for p in d.placed}


def _route(d, src, dst=None):
    for c, r in d.routes:
        if c.src == src and (dst is None or c.dst == dst):
            return r
    raise KeyError(src)


def _taps(d, prefix):
    return [p for p in d.placed if p.id.startswith(prefix)]


# -- A: two column tops merge into one vent ------------------------------------
def _two_tops_to_vent() -> Flowsheet:
    fs = Flowsheet(layout="flow")
    fs.add("connector", "FEED", text="FEED")
    fs.add("pump", "P-1")
    fs.add("column", "C-1")
    fs.add("column", "C-2")
    fs.add("connector", "VENT", level=2, text="VENT", point="right")
    fs.add("connector", "B2", text="BTM", point="right")
    fs.connect("FEED.tip", "P-1.in")
    fs.connect("P-1.out", "C-1.w8")
    fs.connect("C-1.bottom", "C-2.w8")
    fs.connect("C-1.top", "VENT.tail")
    fs.connect("C-2.top", "VENT.tail")
    fs.connect("C-2.bottom", "B2.tail")
    return fs


def test_column_tops_merge_into_one_straight_vent_pipe():
    d = _two_tops_to_vent().build()
    assert not d.warnings and not d.uturns
    assert d.crossings == 0
    assert len({p.y for p in _taps(d, "_T_VENT_tail")}) == 1  # one straight pipe
    for col in ("C-1", "C-2"):
        assert _route(d, f"{col}.top").bends <= 1  # up, then into the pipe


# -- B: one feed pump distributes to three columns -----------------------------
def _feed_to_three_columns() -> Flowsheet:
    fs = Flowsheet(layout="flow")
    fs.add("connector", "FEED", text="FEED")
    fs.add("pump", "P-1")
    for i in (1, 2, 3):
        fs.add("column", f"C-{i}")
        fs.add("connector", f"OH{i}", level=2, text=f"OH{i}", point="right")
        fs.add("connector", f"BT{i}", text=f"BTM{i}", point="right")
    fs.connect("FEED.tip", "P-1.in")
    for i in (1, 2, 3):
        fs.connect("P-1.out", f"C-{i}.w8")
        fs.connect(f"C-{i}.top", f"OH{i}.tail")
        fs.connect(f"C-{i}.bottom", f"BT{i}.tail")
    return fs


def test_feed_main_pipe_drops_beside_each_column():
    d = _feed_to_three_columns().build()
    assert not d.warnings and not d.uturns
    by = _by_id(d)
    # alike columns stand alike, though their bottoms run to stacked flags
    assert len({by[f"C-{i}"].y for i in (1, 2, 3)}) == 1
    for i in (1, 2, 3):
        r = _route(d, next(c.src for c, _ in d.routes if c.dst == f"C-{i}.w8"))
        assert r.bends == 1  # down from the main pipe, then into the nozzle
        assert r.points[0][0] < by[f"C-{i}"].x  # the tap stands in front of it


# -- C: column bottom to the reboiler and the bottoms pump ---------------------
def _bottom_split() -> Flowsheet:
    fs = Flowsheet(layout="flow")
    fs.add("connector", "FEED", level=1, text="FEED")
    fs.add("column", "C-1")
    fs.add("heat_exchanger", "E-2")
    fs.add("pump", "P-2")
    fs.add("connector", "OUT", text="BTM", point="right")
    fs.add("connector", "OH", level=2, text="OH", point="right")
    fs.connect("FEED.tip", "C-1.w8")
    fs.connect("C-1.top", "OH.tail")
    fs.connect("C-1.bottom", "E-2.tube_in")
    fs.connect("E-2.tube_out", "C-1.e3", recycle=True)
    fs.connect("C-1.bottom", "P-2.in")
    fs.connect("P-2.out", "OUT.tail")
    return fs


def test_column_bottom_splits_to_reboiler_and_pump():
    d = _bottom_split().build()
    assert not d.warnings and not d.uturns
    assert d.crossings == 0


# -- D: reflux and a slop return share one side nozzle -------------------------
def _reflux_and_slop() -> Flowsheet:
    fs = Flowsheet(layout="flow")
    fs.add("connector", "FEED", text="FEED")
    fs.add("column", "C-1")
    fs.add("heat_exchanger", "E-1", level=2)
    fs.add("vertical_drum", "D-1", level=2, width=4, height=6)
    fs.add("pump", "P-3", level=1)
    fs.add("connector", "SLOP", level=1, text="SLOP", point="left")
    fs.add("connector", "DIST", level=1, text="DIST", point="right")
    fs.add("connector", "BT", text="BTM", point="right")
    fs.connect("FEED.tip", "C-1.w8")
    fs.connect("C-1.top", "E-1.tube_in")
    fs.connect("E-1.tube_out", "D-1.w_mid")
    fs.connect("D-1.bottom", "P-3.in")
    fs.connect("P-3.out", "C-1.e14", recycle=True)
    fs.connect("SLOP.tip", "C-1.e14")
    fs.connect("P-3.out", "DIST.tail")
    fs.connect("C-1.bottom", "BT.tail")
    return fs


def test_reflux_pump_outlet_splits_on_its_own_level():
    fs = _reflux_and_slop()
    net = fs.expand()
    riser = next(e for e in net.equipment if e.id == "_T_P-3_out")
    assert riser.level == 1  # beside the pump, not up among the condenser
    d = fs.build()
    assert not d.warnings and not d.uturns
    up = _route(d, "P-3.out")
    assert up.bends == 0  # the outlet rises straight into the tee
    # reflux back to the column and the slop return do not cross
    assert d.crossings <= 1  # only the drum's outlet passes the main pipe
    slop = _route(d, "SLOP.tip")
    assert slop.bends <= 2


# -- E: three column trains into common overhead and bottoms pipes -------------
def _column_trains() -> Flowsheet:
    fs = Flowsheet(layout="flow")
    fs.add("connector", "FEED", text="FEED")
    fs.add("pump", "P-0")
    for i in (1, 2, 3):
        fs.add("column", f"C-{i}")
        fs.add("pump", f"P-{i}")
    fs.add("connector", "OH", level=2, text="OVHD", point="right")
    fs.add("connector", "BT", text="BTMS", point="right")
    fs.connect("FEED.tip", "P-0.in")
    for i in (1, 2, 3):
        fs.connect("P-0.out", f"C-{i}.w8")
        fs.connect(f"C-{i}.top", "OH.tail")
        fs.connect(f"C-{i}.bottom", f"P-{i}.in")
        fs.connect(f"P-{i}.out", "BT.tail")
    return fs


def test_pump_outlets_rise_into_their_taps():
    net = _column_trains().expand()
    arms = {
        c.dst.split(".")[1]
        for c in net.connections
        if c.src.startswith("P-") and c.src != "P-0.out"
    }
    assert arms == {"s"}  # from below: a pump's outlet faces up


def test_column_trains_stay_together_and_alike():
    d = _column_trains().build()
    assert not d.warnings and not d.uturns
    by = _by_id(d)
    xs = [by[i].x for i in ("C-1", "P-1", "C-2", "P-2", "C-3", "P-3")]
    assert xs == sorted(xs)  # train by train, in declared order
    assert len({by[f"C-{i}"].y for i in (1, 2, 3)}) == 1
    assert len({by[f"P-{i}"].y for i in (1, 2, 3)}) == 1


def test_bottoms_main_pipe_runs_straight_under_the_columns():
    d = _column_trains().build()
    by = _by_id(d)
    taps = _taps(d, "_T_BT_tail")
    assert len({p.y for p in taps}) == 1  # straight
    pipe = taps[0].y + 1
    for i in (1, 2, 3):
        assert by[f"C-{i}"].y > pipe  # the columns stand clear of it
        assert by[f"P-{i}"].bbox[3] < pipe  # the pumps below feed up into it
        assert _route(d, f"P-{i}.out").bends == 0  # straight up
    for c, r in d.routes:  # main pipe pieces have no bends
        if c.src.startswith("_T_BT_tail") and c.dst.startswith(("_T_BT", "BT")):
            assert r.bends == 0


def test_feed_main_pipe_feeds_each_column_with_an_l():
    d = _column_trains().build()
    for i in (1, 2, 3):
        r = _route(d, next(c.src for c, _ in d.routes if c.dst == f"C-{i}.w8"))
        assert r.bends == 1


# -- F: column overhead to the condenser and a relief line ---------------------
def _overhead_and_relief() -> Flowsheet:
    fs = Flowsheet(layout="flow")
    fs.add("connector", "FEED", text="FEED")
    fs.add("column", "C-1")
    fs.add("heat_exchanger", "E-1", level=2)
    fs.add("vertical_drum", "D-1", level=2, width=4, height=6)
    fs.add("pump", "P-2")
    fs.add("connector", "FL", level=2, text="FLARE", point="right")
    fs.add("connector", "BT", text="BTM", point="right")
    fs.connect("FEED.tip", "C-1.w8")
    fs.connect("C-1.top", "E-1.tube_in")
    fs.connect("C-1.top", "FL.tail")
    fs.connect("E-1.tube_out", "D-1.w_mid")
    fs.connect("D-1.top", "FL.tail")
    fs.connect("C-1.bottom", "P-2.in")
    fs.connect("P-2.out", "BT.tail")
    return fs


def test_overhead_splits_to_condenser_and_flare():
    d = _overhead_and_relief().build()
    assert not d.warnings and not d.uturns
    assert d.crossings == 0
    assert len({p.y for p in _taps(d, "_T_C-1_top")}) == 1
