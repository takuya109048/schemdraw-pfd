"""Small cases (``tests/cases/<group>/<name>.json``): one short sheet for each
element, port, parameter value, JSON key and pair of port directions.

Every valid case must build with no needless U-turns, draw exactly as its
snapshot (``tests/snapshots/cases/``), draw the same sheet with its lists
reversed (flow layout) and survive a JSON round trip.  Every case under
``invalid/`` must be refused with its message.  The coverage tests check
that the cases use every option the code knows, so a new element type,
port, parameter value or key fails here until a case uses it.

Regenerate the snapshots after an intended change with::

    PFD_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_cases.py
"""

import json
import re
from dataclasses import fields
from itertools import product
from pathlib import Path

import pytest
from conftest import CASES, built_case, check_snapshot, snapshot

from schemdraw_pfd import Diagram, Flowsheet
from schemdraw_pfd.elements import DEFAULT_LIBRARY, CustomSpec
from schemdraw_pfd.elements.base import LABEL_POSITIONS, ROTATIONS
from schemdraw_pfd.elements.valve import ValveSpec
from schemdraw_pfd.model import (
    _CONNECTION_KEYS,
    _ELEMENT_KEYS,
    _TOP_KEYS,
    LAYOUTS,
    ROW_MODES,
)
from schemdraw_pfd.netlist import ANCHORS, InlineItem
from schemdraw_pfd.router import RoutingError
from schemdraw_pfd.style import DEFAULT_LINE_STYLES, LineStyle

SNAPSHOTS = Path(__file__).parent / "snapshots" / "cases"
ALL = sorted(
    p.relative_to(CASES).with_suffix("").as_posix() for p in CASES.rglob("*.json")
)
INVALID_CASES = [n for n in ALL if n.startswith("invalid/")]
VALID = [n for n in ALL if not n.startswith("invalid/")]


def _data(name: str) -> dict:
    return json.loads((CASES / f"{name}.json").read_text("utf-8"))


FLOW = [n for n in VALID if _data(n).get("layout", "flow") == "flow"]

# Defects these cases found.  Each is a strict xfail: the run stays green
# while it lasts and fails once it is fixed, so the entry can be removed.
NO_ROUTE = {
    "topology/merge_3": "three lines into one nozzle: the last tap is not routed",
}
NEEDLESS_UTURN = {
    "transform/pump_r180": "pump turned upside down: the discharge line turns "
    "back with nothing in the way",
}


def _marked(names: list[str], uturns: bool = False) -> list:
    """``names`` with the known defects marked as strict xfails: the cases
    that do not route, and (``uturns``) those with a needless U-turn."""
    out: list = []
    for n in names:
        if n in NO_ROUTE:
            mark = pytest.mark.xfail(
                raises=RoutingError, strict=True, reason=NO_ROUTE[n]
            )
        elif uturns and n in NEEDLESS_UTURN:
            reason = NEEDLESS_UTURN[n]
            mark = pytest.mark.xfail(raises=AssertionError, strict=True, reason=reason)
        else:
            out.append(n)
            continue
        out.append(pytest.param(n, marks=mark))
    return out


def _sheet(d: Diagram) -> tuple:
    """Everything drawn, independent of the order it is listed in."""
    return (
        sorted((p.id, p.x, p.y) for p in d.placed),
        sorted((c.src, c.dst, tuple(r.points)) for c, r in d.routes),
        sorted(d.warnings),
    )


# -- every valid case -------------------------------------------------------------
@pytest.mark.parametrize("name", _marked(VALID))
def test_case_draws_as_its_snapshot(name, monkeypatch):
    actual = snapshot(built_case(name), monkeypatch)
    check_snapshot(actual, SNAPSHOTS / f"{name}.json")


@pytest.mark.parametrize("name", _marked(VALID, uturns=True))
def test_case_has_no_needless_uturns(name):
    assert built_case(name).warnings == []


@pytest.mark.parametrize("name", _marked(FLOW))
def test_flow_case_ignores_the_order_of_its_lists(name):
    data = _data(name)
    data["elements"].reverse()
    data["connections"].reverse()
    assert _sheet(Flowsheet.from_dict(data).build()) == _sheet(built_case(name))


def _written(c: dict) -> dict:
    """A line as ``to_dict`` writes it: default values left out."""
    defaults = {"type": "pipe", "anchor": "auto", "recycle": False}
    return {k: v for k, v in c.items() if defaults.get(k, ...) != v}


@pytest.mark.parametrize("name", VALID)
def test_case_survives_a_json_round_trip(name):
    data = _data(name)
    out = Flowsheet.from_dict(data).to_dict()
    assert out["layout"] == data.get("layout", "flow")
    assert out.get("rows", "auto") == data.get("rows", "auto")
    assert out["elements"] == data["elements"]
    assert out["connections"] == [_written(c) for c in data["connections"]]
    assert Flowsheet.from_dict(out).to_dict() == out


# -- invalid input ----------------------------------------------------------------
INVALID = {
    "invalid/bad_anchor": (ValueError, "anchor must be one of"),
    "invalid/bad_label_pos": (ValueError, "label_pos must be one of"),
    "invalid/bad_layout": (ValueError, "layout must be one of"),
    "invalid/bad_mount": (ValueError, "instrument mount must be"),
    "invalid/bad_port_side": (ValueError, "port side must be N/S/E/W"),
    "invalid/bad_rotation": (ValueError, "rotation must be one of"),
    "invalid/bad_rows": (ValueError, "rows must be one of"),
    "invalid/bad_tee_arms": (ValueError, "tee arms must be a combination"),
    "invalid/bad_valve_kind": (ValueError, "valve kind must be one of"),
    "invalid/bad_version": (ValueError, "unsupported flowsheet format version"),
    "invalid/drum_too_short": (ValueError, "vertical_drum height must be >="),
    "invalid/duplicate_id": (ValueError, "duplicate id 'P-1'"),
    "invalid/end_without_port": (ValueError, "must look like 'ID.port'"),
    "invalid/header_branch_not_centred": (ValueError, "needs aligned S and N"),
    "invalid/header_branch_out_of_range": (ValueError, "branch index 5 out of range"),
    "invalid/header_no_branches": (ValueError, "header needs at least one branch"),
    "invalid/id_reserved": (ValueError, "ids starting with '_' are reserved"),
    "invalid/id_with_dot": (ValueError, "must be non-empty and contain no '.'"),
    "invalid/negative_elevation": (ValueError, "elevation must be >= 0"),
    "invalid/negative_level": (ValueError, "level must be >= 0"),
    "invalid/odd_width": (ValueError, "width must be an even number"),
    "invalid/too_many_beds": (ValueError, "too short for 9 beds"),
    "invalid/unknown_connection_key": (ValueError, "unknown keys ['via']"),
    "invalid/unknown_custom_key": (ValueError, "unknown keys ['colour']"),
    "invalid/unknown_custom_shape": (ValueError, "unknown shape type 'star'"),
    "invalid/unknown_element_key": (ValueError, "element parameters go in 'params'"),
    "invalid/unknown_equipment": (KeyError, "unknown equipment 'P-9'"),
    "invalid/unknown_inline_key": (ValueError, "unknown inline item keys ['tag']"),
    "invalid/unknown_inline_type": (KeyError, "unknown element type 'orifice'"),
    "invalid/unknown_line_kind": (ValueError, "kind must be one of"),
    "invalid/unknown_line_style_key": (ValueError, "unknown line style keys"),
    "invalid/unknown_port": (KeyError, "unknown port 'suction'"),
    "invalid/unknown_top_key": (ValueError, "unknown top-level keys ['order']"),
    "invalid/unknown_type": (KeyError, "unknown element type 'compressor'"),
    "invalid/zero_span": (ValueError, "span must be >= 1"),
}


def test_every_invalid_case_has_its_message():
    assert sorted(INVALID) == INVALID_CASES


@pytest.mark.parametrize("name", INVALID_CASES)
def test_invalid_case_is_refused(name):
    error, message = INVALID[name]
    with pytest.raises(error, match=re.escape(message)):
        Flowsheet.from_json(CASES / f"{name}.json").build()


# -- coverage ---------------------------------------------------------------------
# parameters that pick a variant: (default when left out, every value)
ENUMS = {
    ("valve", "kind"): ("gate", ValveSpec.KINDS),
    ("valve", "orientation"): ("horizontal", ("horizontal", "vertical")),
    ("heat_exchanger", "orientation"): ("horizontal", ("horizontal", "vertical")),
    ("instrument", "mount"): ("field", ("field", "dcs")),
    ("connector", "point"): ("right", ("right", "left")),
    ("connector", "edge"): (None, ("left", "right")),
    ("connector", "local"): ("auto", (True, False, "auto")),
}
# other parameters of each element type: each written by some case
PARAMS = {
    "column": {"width", "height", "beds"},
    "connector": {"text", "length"},
    "filter_housing": {"width", "height"},
    "header": {"branches", "pitch", "branch_elements"},
    "heat_exchanger": {"length", "diameter"},
    "horizontal_drum": {"length", "diameter"},
    "instrument": {"function", "number"},
    "pump": set(),
    "tee": {"arms"},
    "valve": set(),
    "vertical_drum": {"width", "height", "beds"},
}
# parameters that add ports (the valve actuator, the fourth tee arm)
PORT_VARIANTS = {"valve": [{}, {"kind": "control"}], "tee": [{"arms": "nsew"}]}
SHAPES = {"rect", "polyline", "ellipse", "arc"}
SHAPE_KEYS = {"boundary", "fill", "pipe", "closed"}
RELATIONS = ("same", "up", "down")  # level of a line's end against its start


def _family(port: str) -> str:
    """``e12`` -> ``e#``: the numbered ports of one face are one family."""
    return re.sub(r"\d+$", "#", port)


def _items(data: dict) -> list[dict]:
    """Every item with a type: elements, inline items, header branch items."""
    out = list(data["elements"])
    for c in data["connections"]:
        out += [{"type": i} if isinstance(i, str) else i for i in c.get("inline", [])]
    for e in data["elements"]:
        for v in e.get("params", {}).get("branch_elements", {}).values():
            for i in v if isinstance(v, list) else [v]:
                out.append({"type": i} if isinstance(i, str) else i)
    return out


def _features(data: dict) -> set[str]:
    """What one case uses, as ``kind:value`` strings."""
    out: set[str] = set()
    out |= {f"top_key:{k}" for k in data}
    out.add(f"layout:{data.get('layout', 'omitted')}")
    out.add(f"rows:{data.get('rows', 'auto')}")
    layout = data.get("layout", "flow")
    for name, style in data.get("line_kinds", {}).items():
        out |= {f"line_style_key:{k}" for k in style}
        out.add("line_kinds:" + ("builtin" if name in DEFAULT_LINE_STYLES else "new"))
    for spec in data.get("custom_elements", []):
        out |= {f"custom_key:{k}" for k in spec}
        out.add(f"label_pos:{spec.get('label_pos', 'center')}")
        out |= {f"custom_port_side:{p['side']}" for p in spec["ports"].values()}
        for s in spec["shapes"]:
            out.add(f"shape:{s['type']}")
            out |= {f"shape_key:{k}" for k in s if k in SHAPE_KEYS}
    for e in data["elements"]:
        out |= {f"element_key:{k}" for k in e}
        kind = "pump" if e["type"] == "pump" else "other"
        out.add(f"level:{'given' if 'level' in e else 'omitted'}:{kind}")
    for item in _items(data):
        t, params = item["type"], item.get("params", {})
        out.add(f"type:{t}")
        out.add(f"rotation:{params.get('rotation', 0)}")
        out.add(f"mirror:{bool(params.get('mirror', False))}")
        out |= {f"param:{t}.{k}" for k in params}
        for (et, key), (default, _) in ENUMS.items():
            if et == t and params.get(key, default) is not None:
                out.add(f"param:{t}.{key}={params.get(key, default)}")
        if t == "tee":
            out |= {f"tee_arm:{a}" for a in params.get("arms", "wen")}
        if t == "connector" and "\n" in params.get("text", ""):
            out.add("connector:two_line_text")
        for v in params.get("branch_elements", {}).values():
            out.add("branch_element:" + type(v).__name__)
    fs = Flowsheet.from_dict(data)
    by_id = {e["id"]: e for e in data["elements"]}
    for c in data["connections"]:  # lines may end on an inline item named later
        for i in c.get("inline", []):
            if isinstance(i, dict) and "id" in i:
                by_id[i["id"]] = i
    for c in data["connections"]:
        out |= {f"connection_key:{k}" for k in c}
        kind = c.get("type", "pipe")
        out.add(f"kind:{kind if kind in DEFAULT_LINE_STYLES else 'custom'}")
        out.add(f"recycle:{c.get('recycle', False)}")
        if c.get("inline"):
            out.add(f"anchor:{c.get('anchor', 'auto')}")
        for i in c.get("inline", []):
            item = {"type": i} if isinstance(i, str) else i
            out.add("inline:" + type(i).__name__)
            out |= {f"inline_key:{k}" for k in item}
            for port in (item.get("in", "in"), item.get("out", "out")):
                out.add(f"port:{item['type']}.{_family(port)}")
        ends = []
        for ref in (c["from"], c["to"]):
            i, port = ref.split(".", 1)
            item = by_id[i]
            out.add(f"port:{item['type']}.{_family(port)}")
            ends.append((item, fs.geometry(i).ports[port].side))  # as placed
        (a, sa), (b, sb) = ends
        if "level" in a and "level" in b:
            rel = RELATIONS[(b["level"] > a["level"]) + 2 * (b["level"] < a["level"])]
            out.add(f"route:{layout}:{sa}->{sb}:{rel}")
    return out


def _required() -> set[str]:
    names = DEFAULT_LIBRARY.names()
    req = {f"type:{t}" for t in names}
    for t in names:
        for v in PORT_VARIANTS.get(t, [{}]):
            req |= {f"port:{t}.{_family(p)}" for p in DEFAULT_LIBRARY.build(t, v).ports}
        req |= {f"param:{t}.{k}" for k in PARAMS[t]}
    for (t, key), (_, values) in ENUMS.items():
        req |= {f"param:{t}.{key}={v}" for v in values}
    req |= {f"rotation:{r}" for r in ROTATIONS} | {"mirror:True", "mirror:False"}
    req |= {f"tee_arm:{a}" for a in "nsew"} | {"connector:two_line_text"}
    req |= {f"branch_element:{f}" for f in ("str", "dict", "list")}
    req |= {f"element_key:{k}" for k in _ELEMENT_KEYS}
    req |= {f"level:{g}:{k}" for g in ("given", "omitted") for k in ("pump", "other")}
    req |= {f"connection_key:{k}" for k in _CONNECTION_KEYS}
    req |= {f"kind:{k}" for k in DEFAULT_LINE_STYLES} | {"kind:custom"}
    req |= {f"anchor:{a}" for a in ANCHORS} | {"recycle:True", "recycle:False"}
    req |= {"inline:str", "inline:dict"} | {f"inline_key:{k}" for k in InlineItem.KEYS}
    req |= {f"top_key:{k}" for k in _TOP_KEYS}
    req |= {f"layout:{v}" for v in (*LAYOUTS, "omitted")}
    req |= {f"rows:{v}" for v in ROW_MODES}
    req |= {f"line_style_key:{f.name}" for f in fields(LineStyle)}
    req |= {"line_kinds:builtin", "line_kinds:new"}
    req |= {f"custom_key:{k}" for k in CustomSpec.KEYS}
    req |= {f"label_pos:{p}" for p in LABEL_POSITIONS}
    req |= {f"custom_port_side:{s}" for s in "NSEW"}
    req |= {f"shape:{s}" for s in SHAPES} | {f"shape_key:{k}" for k in SHAPE_KEYS}
    req |= {
        f"route:{layout}:{a}->{b}:{rel}"
        for layout, a, b, rel in product(LAYOUTS, "NSEW", "NSEW", RELATIONS)
    }
    return req


def test_every_element_type_has_its_parameters_listed():
    assert sorted(PARAMS) == DEFAULT_LIBRARY.names()


def test_cases_cover_every_option():
    used = set().union(*(_features(_data(n)) for n in VALID))
    assert sorted(_required() - used) == []


def test_cases_show_every_behaviour():
    """Beyond the input: what the layout does with it, each in some case."""
    seen = set()
    for name in VALID:
        if name in NO_ROUTE:
            continue
        d = built_case(name)
        if len(d.rows) > 1:
            seen.add("folded into rows")
        if any(p.id.startswith("_T_") for p in d.placed):
            seen.add("automatic main pipe")
        if d.crossings:
            seen.add("lines crossing")
        if d.uturns and not d.warnings:
            seen.add("line around equipment")
        for p, q in product(d.placed, repeat=2):
            if p.x < q.x + q.geo.width and q.x < p.x + p.geo.width and p.y > q.y:
                seen.add("items stacked on one column")
    assert seen == {
        "folded into rows",
        "automatic main pipe",
        "lines crossing",
        "line around equipment",
        "items stacked on one column",
    }
