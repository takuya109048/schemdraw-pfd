import json

import pytest

from schemdraw_pfd import (
    DEFAULT_LIBRARY,
    CustomSpec,
    Flowsheet,
    LineStyle,
    Theme,
)
from schemdraw_pfd.cli import main
from schemdraw_pfd.layout import Placed
from schemdraw_pfd.router import resolve_port

STEM = {
    "name": "stem",
    "width": 2,
    "height": 2,
    "shapes": [{"type": "rect", "x0": 0.5, "y0": 0, "x1": 1.5, "y1": 2}],
    "ports": {"lo": {"side": "S", "offset": 1}, "hi": {"side": "N", "offset": 1}},
}


def _point(geo, port):
    return resolve_port(Placed("X", "x", 0, geo, 0, 0), port).point


# -- element library ---------------------------------------------------------
def test_json_custom_elements_stay_in_their_flowsheet():
    fs = Flowsheet.from_json("examples/pid_c201.json")
    assert "hopper_drum" in fs.library
    assert "hopper_drum" not in DEFAULT_LIBRARY
    assert "hopper_drum" not in Flowsheet().library


def test_nested_elements_come_from_the_flowsheet_library():
    fs = Flowsheet()
    fs.library.register(CustomSpec.from_dict(STEM))
    fs.add("header", "H", branches=2, branch_elements={0: "stem"})
    assert fs.geometry("H").height == 1 + (2 + 1)
    with pytest.raises(KeyError):
        Flowsheet().add("stem")


# -- common rotation / mirror ------------------------------------------------
def test_rotation_and_mirror_apply_to_every_element():
    lib = DEFAULT_LIBRARY
    assert _point(lib.build("valve", {"rotation": 90}), "in") == pytest.approx((1, 0))
    assert _point(lib.build("valve", {"rotation": 180}), "in") == pytest.approx((2, 1))
    assert _point(lib.build("valve", {"mirror": True}), "in") == pytest.approx((2, 1))
    pump = lib.build("pump", {"mirror": True})
    assert (pump.ports["in"].side, pump.ports["out"].side) == ("E", "N")
    assert _point(pump, "out") == pytest.approx((0, 3))
    hx = lib.build("heat_exchanger", {"rotation": 270})
    assert (hx.width, hx.height) == (4, 12)
    with pytest.raises(ValueError):
        lib.build("valve", {"rotation": 45})


def test_mirrored_train_flows_right_to_left():
    fs = Flowsheet()
    fs.add("pump", "P", mirror=True)  # suction now on the east face
    fs.add("valve", "V", mirror=True)  # outlet on the west face
    fs.connect("V.out", "P.in")
    d = fs.build()
    assert not d.warnings
    assert [r.bends for _, r in d.routes] == [0]  # straight across


# -- line kinds and theme ------------------------------------------------------
def test_custom_line_kind_and_theme(tmp_path):
    fs = Flowsheet()
    fs.add("valve", "A")
    fs.add("valve", "B")
    fs.add_line_kind("utility", LineStyle(lw=1.0, ls="-.", arrow="->", process=True))
    fs.connect("A.out", "B.in", kind="utility")
    with pytest.raises(ValueError):
        fs.connect("A.in", "B.out", kind="steam")
    out = tmp_path / "u.svg"
    fs.render(out, theme=Theme(fontsize=10, frame_lw=1, tag_underline=False))
    assert out.stat().st_size > 500


# -- JSON format -------------------------------------------------------------
def test_json_round_trip_keeps_the_layout(tmp_path):
    fs = Flowsheet.from_json("examples/pid_c201.json")
    path = tmp_path / "rt.json"
    fs.to_json(path)
    data = json.loads(path.read_text("utf-8"))
    assert data["version"] == 1
    assert {c["name"] for c in data["custom_elements"]} == {
        "hopper_drum",
        "steam_trap",
        "funnel",
    }
    again = Flowsheet.from_json(path)
    a, b = fs.build(compact=False), again.build(compact=False)
    assert [(p.id, p.x, p.y) for p in a.placed] == [(p.id, p.x, p.y) for p in b.placed]
    assert [r.points for _, r in a.routes] == [r.points for _, r in b.routes]


def test_line_kinds_round_trip():
    fs = Flowsheet()
    fs.add("valve", "A")
    fs.add("valve", "B")
    fs.add_line_kind("electric", LineStyle(ls=":"))
    fs.connect("A.out", "B.in", kind="electric")
    again = Flowsheet.from_dict(fs.to_dict())
    assert again.line_styles["electric"] == LineStyle(ls=":")
    assert again.connections[0].kind == "electric"


def test_json_errors_say_where():
    bad_param = {"elements": [{"id": "FV", "type": "valve", "kind": "control"}]}
    with pytest.raises(ValueError, match=r"elements\[0\] \(FV\).*params"):
        Flowsheet.from_dict(bad_param)
    bad_type = {"elements": [{"id": "X", "type": "nope"}]}
    with pytest.raises(KeyError, match=r"elements\[0\] \(X\).*unknown element"):
        Flowsheet.from_dict(bad_type)
    with pytest.raises(ValueError, match="version"):
        Flowsheet.from_dict({"version": 99})
    bad_shape = {"custom_elements": [{**STEM, "shapes": [{"type": "blob"}]}]}
    with pytest.raises(ValueError, match=r"custom_elements\[0\] \(stem\)"):
        Flowsheet.from_dict(bad_shape)


# -- cli -----------------------------------------------------------------------
def test_cli_lists_and_describes_elements(capsys):
    assert main(["--list-elements", "examples/pid_c201.json"]) == 0
    listing = capsys.readouterr().out
    assert "hopper_drum" in listing and "valve" in listing
    assert main(["--describe", "valve"]) == 0
    assert "control" in capsys.readouterr().out
