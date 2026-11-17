"""Excel output: a valid package whose drawing objects sit exactly where the
SVG/PNG drawing puts them."""

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
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "asvg": "http://schemas.microsoft.com/office/drawing/2016/SVG/main",
}
EXAMPLE = "examples/parallel_trains.json"


@pytest.fixture(scope="module")
def book(tmp_path_factory):
    path = tmp_path_factory.mktemp("xlsx") / "pfd.xlsx"
    fs = Flowsheet.from_json(EXAMPLE)
    d = fs.render(path)
    return zipfile.ZipFile(path), scene(d)


def _find(el, path: str) -> ET.Element:
    found = el.find(path, NS)
    assert found is not None, path
    return found


def _attr(el, name: str) -> str:
    value = el.get(name)
    assert value is not None, name
    return value


def _is_pipe(item) -> bool:
    return isinstance(item, Polyline) and item.pipe


def _units(sc, x: float, y: float) -> tuple[float, float]:
    """EMU on the sheet -> drawing units."""
    x0, _, _, y1 = sc.bounds
    return x / UNIT + x0 - MARGIN, y1 + MARGIN - y / UNIT


def _box(el):
    off, ext = _find(el, "a:off"), _find(el, "a:ext")
    return (
        int(_attr(off, "x")),
        int(_attr(off, "y")),
        int(_attr(ext, "cx")),
        int(_attr(ext, "cy")),
    )


def test_package_is_well_formed(book):
    z, _ = book
    for name in z.namelist():
        if name.endswith((".xml", ".rels")):
            ET.fromstring(z.read(name))  # every part parses
    types = z.read("[Content_Types].xml").decode()
    assert "drawing+xml" in types and "image/" not in types  # shapes only
    sheet = z.read("xl/worksheets/sheet1.xml").decode()
    assert '<drawing r:id="rId1"/>' in sheet and 'showGridLines="0"' in sheet


def test_lines_are_arrow_shapes_exactly_on_the_routes(book):
    z, sc = book
    root = ET.fromstring(z.read("xl/drawings/drawing1.xml"))
    drawn = []
    for sp in root.iter(f"{{{NS['xdr']}}}sp"):
        path = sp.find(".//a:path", NS)
        if path is None or _find(sp, ".//xdr:cNvPr").get("descr"):
            continue  # a text box, or an equipment outline
        x, y, cx, cy = _box(_find(sp, "xdr:spPr/a:xfrm"))
        w, h = int(_attr(path, "w")), int(_attr(path, "h"))
        pts = []
        for pt in path.iter(f"{{{NS['a']}}}pt"):
            px = x + int(_attr(pt, "x")) * (cx / w if cx else 0)
            py = y + int(_attr(pt, "y")) * (cy / h if cy else 0)
            pts.append(_units(sc, px, py))
        ln = _find(sp, "xdr:spPr/a:ln")
        drawn.append((pts, ln.find("a:tailEnd", NS) is not None))
    expected = []
    for g in sc.groups:  # an element's own piping first, then its strokes
        expected += [(list(i.points), False) for i in g.items if _is_pipe(i)]
        expected += [
            (list(i.points), "arrow" in i.style)
            for i in g.items
            if isinstance(i, Stroke)
        ]
    assert len(drawn) == len(expected)
    for (pts, arrow), (want, want_arrow) in zip(drawn, expected):
        assert arrow == want_arrow
        assert len(pts) == len(want)
        for (a, b), (c, d) in zip(pts, want):
            assert abs(a - c) < 1e-5 and abs(b - d) < 1e-5  # at most 1 EMU off


def test_equipment_outlines_are_freeforms_exactly_on_the_symbol(book):
    """No pictures: every symbol is a freeform shape (arcs as arcTo) whose box
    is exactly the extent of its outline; solid parts are filled freeforms."""
    z, sc = book
    assert not [n for n in z.namelist() if n.startswith("xl/media/")]
    root = ET.fromstring(z.read("xl/drawings/drawing1.xml"))
    assert not list(root.iter(f"{{{NS['xdr']}}}pic"))
    outlines = {}
    for sp in root.iter(f"{{{NS['xdr']}}}sp"):
        pr = _find(sp, ".//xdr:cNvPr")
        if pr.get("descr"):
            outlines[_attr(pr, "name")] = sp
    for g in sc.groups:
        if g.kind != "equipment":
            continue
        shapes = [
            i
            for i in g.items
            if isinstance(i, (Polyline, Ellipse)) and not _is_pipe(i) and not i.fill
        ]
        if not shapes:
            assert g.id not in outlines  # all piping (a tee)
            continue
        sp = outlines[g.id]
        x, y, cx, cy = _box(_find(sp, "xdr:spPr/a:xfrm"))
        left, top = _units(sc, x, y)
        right, bottom = _units(sc, x + cx, y + cy)
        xs = [v for s in shapes for v in _xs(s)]
        ys = [v for s in shapes for v in _ys(s)]
        assert abs(left - min(xs)) < 1e-5 and abs(right - max(xs)) < 1e-5
        assert abs(bottom - min(ys)) < 1e-5 and abs(top - max(ys)) < 1e-5
        paths = list(sp.iter(f"{{{NS['a']}}}path"))
        assert len(paths) == len(shapes)
        arcs = sum(isinstance(s, Ellipse) for s in shapes)
        assert len(list(sp.iter(f"{{{NS['a']}}}arcTo"))) == arcs
        assert _find(sp, "xdr:spPr/a:noFill") is not None


def _xs(s) -> list[float]:
    if isinstance(s, Polyline):
        return [p[0] for p in s.points]
    return [s.cx - s.rx, s.cx + s.rx]


def _ys(s) -> list[float]:
    if isinstance(s, Polyline):
        return [p[1] for p in s.points]
    return [s.cy - s.ry, s.cy + s.ry]


def test_solid_parts_are_filled_freeforms(tmp_path):
    fs = Flowsheet()
    fs.add("valve", "V1", kind="globe")
    fs.add("valve", "V2")
    fs.connect("V1.out", "V2.in")
    fs.render(tmp_path / "v.xlsx")
    root = ET.fromstring(
        zipfile.ZipFile(tmp_path / "v.xlsx").read("xl/drawings/drawing1.xml")
    )
    filled = [
        sp
        for sp in root.iter(f"{{{NS['xdr']}}}sp")
        if _find(sp, ".//xdr:cNvPr").get("descr") == "valve"
        and sp.find("xdr:spPr/a:solidFill", NS) is not None
    ]
    assert len(filled) == 1  # the globe valve's dot
    assert _attr(_find(filled[0], ".//xdr:cNvPr"), "name") == "V1 fill 1"
    assert _attr(_find(filled[0], ".//a:srgbClr"), "val") == "000000"


def test_texts_are_text_boxes(book):
    z, sc = book
    root = ET.fromstring(z.read("xl/drawings/drawing1.xml"))
    boxes = [
        "".join(t.text or "" for t in sp.iter(f"{{{NS['a']}}}t"))
        for sp in root.iter(f"{{{NS['xdr']}}}sp")
        if _find(sp, "xdr:nvSpPr/xdr:cNvSpPr").get("txBox") == "1"
    ]
    texts = [
        i.text.replace("\n", "")
        for g in sc.groups
        for i in g.items
        if isinstance(i, Text)
    ]
    assert boxes == texts and "T-101" in boxes


def test_piping_drawn_by_elements_is_lines(tmp_path):
    """Headers and tees (main pipes, taps) are piping: line shapes in the
    equipment line width; a header's inserted valve is an outline shape."""
    fs = Flowsheet()
    fs.add("pump", "P1")
    fs.add("header", "H1", branches=2, branch_elements={1: "valve"})
    fs.add("vertical_drum", "D1")
    fs.connect("P1.out", "H1.main_w")
    fs.connect("H1.b0", "D1.w_low")
    fs.render(tmp_path / "h.xlsx")
    root = ET.fromstring(
        zipfile.ZipFile(tmp_path / "h.xlsx").read("xl/drawings/drawing1.xml")
    )
    names = [
        _attr(_find(sp, "xdr:nvSpPr/xdr:cNvPr"), "name")
        for sp in root.iter(f"{{{NS['xdr']}}}sp")
    ]
    outlines = {
        _attr(pr, "name")
        for pr in root.iter(f"{{{NS['xdr']}}}cNvPr")
        if pr.get("descr")
    }
    # main pipe + one plain branch + the valve branch's two pieces
    assert sum(n.startswith("H1 pipe") for n in names) == 4
    assert outlines == {"P1", "H1", "D1"}  # H1's outline: just its valve
    for sp in root.iter(f"{{{NS['xdr']}}}sp"):
        if _attr(_find(sp, "xdr:nvSpPr/xdr:cNvPr"), "name").startswith("H1 pipe"):
            ln = _find(sp, "xdr:spPr/a:ln")
            assert ln.get("w") == str(round(1.3 * 12700))  # equipment width
            assert ln.find("a:tailEnd", NS) is None  # no arrow on piping
