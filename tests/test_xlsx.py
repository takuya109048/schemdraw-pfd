"""Excel output: a valid package of Excel shapes -- one per part, grouped per
element / line -- sitting exactly where the SVG/PNG drawing puts them."""

import xml.etree.ElementTree as ET
import zipfile

import pytest

from schemdraw_pfd import Flowsheet
from schemdraw_pfd.geometry import Ellipse, Polyline
from schemdraw_pfd.render import Stroke, Text, scene
from schemdraw_pfd.xlsx import MARGIN, UNIT

NS = {
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
}
X = f"{{{NS['xdr']}}}"
EXAMPLE = "examples/loading_station.json"


@pytest.fixture(scope="module")
def book(tmp_path_factory):
    path = tmp_path_factory.mktemp("xlsx") / "pfd.xlsx"
    d = Flowsheet.from_json(EXAMPLE).render(path)
    z = zipfile.ZipFile(path)
    return z, ET.fromstring(z.read("xl/drawings/drawing1.xml")), scene(d)


def _render(tmp_path, fs) -> ET.Element:
    fs.render(tmp_path / "f.xlsx")
    z = zipfile.ZipFile(tmp_path / "f.xlsx")
    return ET.fromstring(z.read("xl/drawings/drawing1.xml"))


def _find(el, path: str) -> ET.Element:
    found = el.find(path, NS)
    assert found is not None, path
    return found


def _attr(el, name: str) -> str:
    value = el.get(name)
    assert value is not None, name
    return value


def _name(shape) -> str:
    return _attr(_find(shape, ".//xdr:cNvPr"), "name")


def _box(shape) -> tuple[int, int, int, int]:
    xfrm = _find(shape, "xdr:spPr/a:xfrm")
    off, ext = _find(xfrm, "a:off"), _find(xfrm, "a:ext")
    return (
        int(_attr(off, "x")),
        int(_attr(off, "y")),
        int(_attr(ext, "cx")),
        int(_attr(ext, "cy")),
    )


def _units(sc, x: float, y: float) -> tuple[float, float]:
    """EMU on the sheet -> drawing units."""
    x0, _, _, y1 = sc.bounds
    return x / UNIT + x0 - MARGIN, y1 + MARGIN - y / UNIT


def _segment(sc, line) -> tuple[tuple[float, float], tuple[float, float]]:
    """A line shape's start and end in drawing units (flips say which way)."""
    x, y, cx, cy = _box(line)
    xfrm = _find(line, "xdr:spPr/a:xfrm")
    fh, fv = xfrm.get("flipH") == "1", xfrm.get("flipV") == "1"
    a = (x + cx if fh else x, y + cy if fv else y)
    b = (x if fh else x + cx, y if fv else y + cy)
    return _units(sc, *a), _units(sc, *b)


def _key(seg) -> tuple:
    return tuple(round(v, 4) for p in seg for v in p)


def _segments(points, closed=False) -> list:
    pts = list(points) + ([points[0]] if closed else [])
    return [(a, b) for a, b in zip(pts, pts[1:]) if a != b]


def test_package_is_well_formed(book):
    z, _, _ = book
    for name in z.namelist():
        if name.endswith((".xml", ".rels")):
            ET.fromstring(z.read(name))  # every part parses
    types = z.read("[Content_Types].xml").decode()
    assert "drawing+xml" in types and "image/" not in types  # shapes only
    assert not [n for n in z.namelist() if n.startswith("xl/media/")]
    sheet = z.read("xl/worksheets/sheet1.xml").decode()
    assert '<drawing r:id="rId1"/>' in sheet and 'showGridLines="0"' in sheet


def test_every_straight_piece_is_its_own_line_exactly_in_place(book):
    """Pipes, element outlines, ticks, underlines: one Excel line per straight
    piece, start and end within 1 EMU of the drawing; arrowheads only on the
    last piece of an arrowed line."""
    _, root, sc = book
    drawn = {}
    arrows = 0
    for line in root.iter(f"{X}cxnSp"):
        drawn.setdefault(_key(_segment(sc, line)), 0)
        drawn[_key(_segment(sc, line))] += 1
        arrows += _find(line, "xdr:spPr/a:ln").find("a:tailEnd", NS) is not None
    want: dict[tuple, int] = {}
    want_arrows = 0
    for g in sc.groups:
        for i in g.items:
            if isinstance(i, Polyline) and not i.fill:
                segs = _segments(i.points, i.closed)
            elif isinstance(i, Stroke) and not (
                g.kind == "sheet" and len(i.points) == 5
            ):
                segs = _segments(i.points)
                want_arrows += str(i.style.get("arrow", "")).endswith(">")
            else:
                continue
            for s in segs:
                want[_key(s)] = want.get(_key(s), 0) + 1
    assert drawn == want
    assert arrows == want_arrows > 0
    assert not [sp for sp in root.iter(f"{X}sp") if sp.find(".//a:custGeom", NS) is not None and sp.find("xdr:spPr/a:noFill", NS) is not None]  # fmt: skip


def test_arcs_and_circles_are_excel_arcs_and_ovals(book):
    _, root, sc = book
    shapes = {
        _name(sp): sp
        for sp in root.iter(f"{X}sp")
        if sp.find(".//a:prstGeom[@prst='arc']", NS) is not None
        or sp.find(".//a:prstGeom[@prst='ellipse']", NS) is not None
    }
    count = 0
    for g in sc.groups:
        if g.kind != "equipment":
            continue
        k = 0
        for i in g.items:
            if not isinstance(i, (Polyline, Ellipse)) or getattr(i, "pipe", False):
                continue
            k += 1
            if not isinstance(i, Ellipse):
                continue
            sp = shapes[f"{g.id} {k}"]
            x, y, cx, cy = _box(sp)
            left, top = _units(sc, x, y)
            right, bottom = _units(sc, x + cx, y + cy)
            assert (
                abs(left - (i.cx - i.rx)) < 1e-5 and abs(right - (i.cx + i.rx)) < 1e-5
            )
            assert (
                abs(bottom - (i.cy - i.ry)) < 1e-5 and abs(top - (i.cy + i.ry)) < 1e-5
            )
            prst = _attr(_find(sp, ".//a:prstGeom"), "prst")
            assert prst == ("ellipse" if i.full else "arc")
            count += 1
    assert count == len(shapes) > 0  # tank and filter heads, pump casings


def test_a_vessel_head_arc_runs_over_the_top():
    """An upper head (0..180 degrees on the drawing) is the Excel arc from
    180 to 0 degrees clockwise (Excel's y points down)."""
    from schemdraw_pfd.xlsx import _Writer

    fs = Flowsheet()
    fs.add("vertical_drum", "D1")
    sc = scene(fs.build())
    xml, _ = _Writer(sc).ellipse(Ellipse(3, 8, 3, 2, 0, 180), "head", "drum")
    assert 'name="adj1" fmla="val 10800000"' in xml
    assert 'name="adj2" fmla="val 0"' in xml


def test_parts_are_grouped_per_element_and_per_line(book):
    _, root, sc = book
    groups = {_name(g): g for g in root.iter(f"{X}grpSp")}
    tank = groups["TK-301"]
    lines = list(tank.iter(f"{X}cxnSp"))
    ovals = [
        sp for sp in tank.iter(f"{X}sp") if sp.find(".//a:prstGeom[@prst='arc']", NS) is not None
    ]  # fmt: skip
    assert len(ovals) == 2 and len(lines) >= 2  # two heads, two shell sides
    texts = [
        sp for sp in tank.iter(f"{X}sp") if _find(sp, ".//xdr:cNvSpPr").get("txBox")
    ]
    assert len(texts) == 1  # its tag
    route = next(n for n in groups if n.startswith("TK-301.bottom"))
    pieces = list(groups[route].iter(f"{X}cxnSp"))
    assert len(pieces) >= 2  # a bend: two straight pieces (and the nozzle tick)


def test_solid_parts_are_filled(tmp_path):
    fs = Flowsheet()
    fs.add("valve", "V1", kind="globe")
    fs.add("valve", "V2")
    fs.connect("V1.out", "V2.in")
    root = _render(tmp_path, fs)
    filled = [
        sp
        for sp in root.iter(f"{X}sp")
        if _find(sp, ".//xdr:cNvPr").get("descr") == "valve"
        and sp.find("xdr:spPr/a:solidFill", NS) is not None
    ]
    assert len(filled) == 1  # the globe valve's dot, an oval
    assert _find(filled[0], ".//a:prstGeom").get("prst") == "ellipse"
    assert _attr(_find(filled[0], ".//a:srgbClr"), "val") == "000000"


def test_texts_are_text_boxes(book):
    _, root, sc = book
    boxes = [
        "".join(t.text or "" for t in sp.iter(f"{{{NS['a']}}}t"))
        for sp in root.iter(f"{X}sp")
        if _find(sp, "xdr:nvSpPr/xdr:cNvSpPr").get("txBox") == "1"
    ]
    texts = [
        i.text.replace("\n", "")
        for g in sc.groups
        for i in g.items
        if isinstance(i, Text)
    ]
    assert boxes == texts and "TK-301" in boxes


def test_frame_is_one_rectangle(book):
    _, root, _ = book
    frames = [sp for sp in root.iter(f"{X}sp") if _name(sp) == "frame"]
    assert len(frames) == 1
    assert _find(frames[0], ".//a:prstGeom").get("prst") == "rect"


def test_piping_drawn_by_elements_is_lines(tmp_path):
    """Headers and tees (main pipes, taps) are piping: plain lines in the
    equipment line width; a header's inserted valve is its outline."""
    fs = Flowsheet()
    fs.add("pump", "P1")
    fs.add("header", "H1", branches=2, branch_elements={1: "valve"})
    fs.add("vertical_drum", "D1")
    fs.connect("P1.out", "H1.main_w")
    fs.connect("H1.b0", "D1.w_low")
    root = _render(tmp_path, fs)
    pipes = [ln for ln in root.iter(f"{X}cxnSp") if _name(ln).startswith("H1 pipe")]
    # main pipe + one plain branch + the valve branch's two pieces
    assert len(pipes) == 4
    for ln in pipes:
        stroke = _find(ln, "xdr:spPr/a:ln")
        assert stroke.get("w") == str(round(1.3 * 12700))  # equipment width
        assert stroke.find("a:tailEnd", NS) is None  # no arrow on piping
    valve = [
        ln
        for ln in root.iter(f"{X}cxnSp")
        if _find(ln, ".//xdr:cNvPr").get("descr") == "header"  # the inserted valve
    ]
    assert len(valve) == 4  # its bow-tie: one line per side
