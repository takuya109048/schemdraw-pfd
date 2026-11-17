"""Excel output: the sheet as editable drawing objects in a workbook.

Everything is an Excel shape -- nothing is a picture:

* every piece of equipment is a freeform shape of its outline: polylines,
  and the heads and casings as elliptic arcs (``arcTo``); solid parts (the
  dot of a globe valve ...) are filled freeforms of their own.  Piping an
  element draws itself (a header's pipes, a tee's arms) is a line instead;
* every line is a freeform polyline per piece between crossing gaps, in the
  line kind's width and dash, the last piece ending in an arrowhead;
* tags, texts inside symbols and specification rows are text boxes;
* the frame is a shape too.

Everything comes from the same :class:`~.render.Scene` as the SVG/PNG/PDF
output and is placed with absolute anchors (not attached to cells), drawing
units mapped to EMU at the drawing's own scale (0.5 inch per unit), so
nothing shifts.  A piece of equipment is grouped with its tag; a line with
its pieces and nozzle ticks.

The workbook is written directly (the drawing parts are DrawingML that the
usual Python libraries cannot write: freeform arcs, arrowheads).
"""

from __future__ import annotations

import math
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from matplotlib.backends.backend_agg import RendererAgg
from matplotlib.colors import to_hex
from matplotlib.font_manager import FontProperties

from .geometry import Ellipse, Polyline, Shape
from .render import (
    PT_PER_UNIT,
    Group,
    Point,
    Scene,
    Stroke,
    Text,
    _text_width,
)

INCH = 914400  # EMU
UNIT = INCH // 2  # one drawing unit: 0.5 inch (SchemDraw's default scale)
PT = 12700  # EMU per point
MARGIN = 0.5  # drawing units of blank sheet around the frame

ANGLE = 60000  # DrawingML angle units per degree
FULL = 360 * ANGLE
DASH = {"-": "solid", "--": "dash", ":": "sysDot", "-.": "dashDot"}
NS = (
    'xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


@dataclass
class _Box:
    """A rectangle in EMU, top-left origin (Excel's)."""

    x: int
    y: int
    cx: int
    cy: int

    def union(self, o: _Box) -> _Box:
        x, y = min(self.x, o.x), min(self.y, o.y)
        r = max(self.x + self.cx, o.x + o.cx)
        b = max(self.y + self.cy, o.y + o.cy)
        return _Box(x, y, r - x, b - y)

    def xfrm(
        self,
        tag: str = "a:xfrm",
        child: bool = False,
        flip_h: bool = False,
        flip_v: bool = False,
    ) -> str:
        inner = (
            f'<a:off x="{self.x}" y="{self.y}"/><a:ext cx="{self.cx}" cy="{self.cy}"/>'
        )
        if child:
            inner += (
                f'<a:chOff x="{self.x}" y="{self.y}"/>'
                f'<a:chExt cx="{self.cx}" cy="{self.cy}"/>'
            )
        flips = (' flipH="1"' if flip_h else "") + (' flipV="1"' if flip_v else "")
        return f"<{tag}{flips}>{inner}</{tag}>"


class _Sheet:
    """Drawing units -> EMU on the worksheet (x right, y down)."""

    def __init__(self, sc: Scene):
        x0, _, _, y1 = sc.bounds
        self.ox, self.oy = x0 - MARGIN, y1 + MARGIN

    def x(self, x: float) -> int:
        return round((x - self.ox) * UNIT)

    def y(self, y: float) -> int:
        return round((self.oy - y) * UNIT)

    def box(self, x0: float, y0: float, x1: float, y1: float) -> _Box:
        """Box of drawing-unit corners (x0, y0) bottom-left, (x1, y1) top-right."""
        left, top = self.x(x0), self.y(y1)
        return _Box(left, top, self.x(x1) - left, self.y(y0) - top)


# -- outlines -------------------------------------------------------------------
def _extent(shapes: list[Shape]) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for s in shapes:
        if isinstance(s, Polyline):
            xs += [p[0] for p in s.points]
            ys += [p[1] for p in s.points]
        else:
            xs += [s.cx - s.rx, s.cx + s.rx]
            ys += [s.cy - s.ry, s.cy + s.ry]
    return min(xs), min(ys), max(xs), max(ys)


# -- DrawingML -----------------------------------------------------------------
class _Writer:
    def __init__(self, sc: Scene):
        self.sc = sc
        self.at = _Sheet(sc)
        self.next_id = 2
        self.font = sc.font or sc.theme.fonts[0]
        self._pitch: dict[float, float] = {}

    def pitch(self, fontsize: float) -> float:
        """Line pitch (points) of a text block as SchemDraw draws it: line
        spacing 1, i.e. the height of "lp" in the font used."""
        if fontsize not in self._pitch:
            fp = FontProperties(family=self.sc.font or "sans-serif", size=fontsize)
            _, h, _ = RendererAgg(1, 1, 72).get_text_width_height_descent(
                "lp", fp, ismath=False
            )
            self._pitch[fontsize] = h
        return self._pitch[fontsize]

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id - 1

    # one item --------------------------------------------------------------
    def _ln(
        self, lw: float, ls: str = "-", head: bool = False, tail: bool = False
    ) -> str:
        tip = '<a:{}End type="triangle" w="lg" len="lg"/>'
        ends = (tip.format("head") if head else "") + (
            tip.format("tail") if tail else ""
        )
        return (
            f'<a:ln w="{round(lw * PT)}" cap="rnd">'
            '<a:solidFill><a:srgbClr val="000000"/></a:solidFill>'
            f'<a:prstDash val="{DASH.get(ls, "solid")}"/><a:round/>{ends}</a:ln>'
        )

    def _cnv(self, name: str, descr: str) -> str:
        d = f' descr="{escape(descr)}"' if descr else ""
        return f'<xdr:cNvPr id="{self._id()}" name="{escape(name)}"{d}/>'

    def line(
        self, a: Point, b: Point, name: str, ln: str, descr: str = ""
    ) -> tuple[str, _Box]:
        """One straight line (an Excel line, flipped to run from ``a`` to ``b``,
        so an arrowhead at its end sits at ``b``)."""
        at = self.at
        x1, y1, x2, y2 = at.x(a[0]), at.y(a[1]), at.x(b[0]), at.y(b[1])
        box = _Box(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
        xml = (
            f'<xdr:cxnSp macro=""><xdr:nvCxnSpPr>{self._cnv(name, descr)}'
            f"<xdr:cNvCxnSpPr/></xdr:nvCxnSpPr><xdr:spPr>"
            f"{box.xfrm(flip_h=x2 < x1, flip_v=y2 < y1)}"
            f'<a:prstGeom prst="line"><a:avLst/></a:prstGeom>{ln}</xdr:spPr>'
            "</xdr:cxnSp>"
        )
        return xml, box

    def lines(
        self,
        pts: Sequence[Point],
        name: str,
        lw: float,
        ls: str = "-",
        arrow: str = "",
        descr: str = "",
    ) -> list[tuple[str, _Box]]:
        """A polyline as one straight line per segment; an arrow only at the
        polyline's ends."""
        segs = [(a, b) for a, b in zip(pts, pts[1:]) if a != b]
        out = []
        for k, (a, b) in enumerate(segs):
            ln = self._ln(
                lw,
                ls,
                head=k == 0 and arrow.startswith("<"),
                tail=k == len(segs) - 1 and arrow.endswith(">"),
            )
            out.append(self.line(a, b, f"{name}-{k + 1}", ln, descr))
        return out

    def stroke(self, s: Stroke, name: str) -> list[tuple[str, _Box]]:
        lw = s.style.get("lw") or self.sc.theme.equipment_lw
        ls = str(s.style.get("ls") or "-")
        return self.lines(s.points, name, lw, ls, str(s.style.get("arrow") or ""))

    def ellipse(self, e: Ellipse, name: str, descr: str) -> tuple[str, _Box]:
        """A whole ellipse (an Excel oval, filled if solid) or an arc (an Excel
        arc, whose handles change its angles)."""
        box = self.at.box(e.cx - e.rx, e.cy - e.ry, e.cx + e.rx, e.cy + e.ry)
        if e.full:
            geom = '<a:prstGeom prst="ellipse"><a:avLst/></a:prstGeom>'
        else:
            # counter-clockwise on the drawing = clockwise on the sheet (y down);
            # the arc runs clockwise from adj1 to adj2
            adj1 = round(-e.theta2 * ANGLE) % FULL
            adj2 = round(-e.theta1 * ANGLE) % FULL
            geom = (
                '<a:prstGeom prst="arc"><a:avLst>'
                f'<a:gd name="adj1" fmla="val {adj1}"/>'
                f'<a:gd name="adj2" fmla="val {adj2}"/></a:avLst></a:prstGeom>'
            )
        paint = _paint(e.fill if e.full else None)
        xml = (
            f"<xdr:sp><xdr:nvSpPr>{self._cnv(name, descr)}<xdr:cNvSpPr/></xdr:nvSpPr>"
            f"<xdr:spPr>{box.xfrm()}{geom}{paint}"
            f"{self._ln(self.sc.theme.equipment_lw)}</xdr:spPr></xdr:sp>"
        )
        return xml, box

    def solid(self, poly: Polyline, name: str, descr: str) -> tuple[str, _Box]:
        """A solid (filled) polygon as one freeform."""
        at = self.at
        box = at.box(*_extent([poly]))
        pts = "".join(
            f'<a:{"moveTo" if k == 0 else "lnTo"}><a:pt x="{at.x(x) - box.x}" '
            f'y="{at.y(y) - box.y}"/></a:{"moveTo" if k == 0 else "lnTo"}>'
            for k, (x, y) in enumerate(poly.points)
        )
        xml = (
            f"<xdr:sp><xdr:nvSpPr>{self._cnv(name, descr)}<xdr:cNvSpPr/></xdr:nvSpPr>"
            f"<xdr:spPr>{box.xfrm()}"
            "<a:custGeom><a:avLst/><a:gdLst/><a:ahLst/><a:cxnLst/>"
            '<a:rect l="0" t="0" r="r" b="b"/><a:pathLst>'
            f'<a:path w="{max(box.cx, 1)}" h="{max(box.cy, 1)}">{pts}<a:close/>'
            f"</a:path></a:pathLst></a:custGeom>{_paint(poly.fill)}"
            f"{self._ln(self.sc.theme.equipment_lw)}</xdr:spPr></xdr:sp>"
        )
        return xml, box

    def rect(self, s: Stroke, name: str) -> tuple[str, _Box]:
        """The sheet frame: one rectangle."""
        xs = [x for x, _ in s.points]
        ys = [y for _, y in s.points]
        box = self.at.box(min(xs), min(ys), max(xs), max(ys))
        lw = s.style.get("lw") or self.sc.theme.equipment_lw
        xml = (
            f"<xdr:sp><xdr:nvSpPr>{self._cnv(name, '')}<xdr:cNvSpPr/></xdr:nvSpPr>"
            f'<xdr:spPr>{box.xfrm()}<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
            f"<a:noFill/>{self._ln(lw)}</xdr:spPr></xdr:sp>"
        )
        return xml, box

    def symbol(self, g: Group, shapes: list[Shape]) -> list[tuple[str, _Box]]:
        """The element's outline part by part: every straight piece a line,
        every arc an arc, a whole circle an oval, a solid polygon a freeform;
        each named after the element and described by its type."""
        p = g.placed
        assert p is not None
        lw = self.sc.theme.equipment_lw
        out: list[tuple[str, _Box]] = []
        for k, s in enumerate(shapes):
            name = f"{p.id} {k + 1}"
            if isinstance(s, Ellipse):
                out.append(self.ellipse(s, name, p.type))
            elif s.fill:
                out.append(self.solid(s, name, p.type))
            else:
                pts = s.points + ((s.points[0],) if s.closed else ())
                out += self.lines(pts, name, lw, descr=p.type)
        return out

    def text(self, t: Text, name: str) -> tuple[str, _Box]:
        em = t.fontsize / PT_PER_UNIT  # font size in drawing units
        lines = t.text.split("\n")
        pitch = self.pitch(t.fontsize) / PT_PER_UNIT  # line to line, units
        w = _text_width(t.text, em) * 1.4 + 2 * em  # room to spare: no wrapping
        h = len(lines) * pitch + 0.4 * em
        left = {"left": t.x, "right": t.x - w}.get(t.ha, t.x - w / 2)
        top = {"top": t.y, "bottom": t.y + h}.get(t.va, t.y + h / 2)
        box = self.at.box(left, top - h, left + w, top)
        anchor = {"top": "t", "bottom": "b"}.get(t.va, "ctr")
        algn = {"left": "l", "right": "r"}.get(t.ha, "ctr")
        font = escape(self.font)
        run = (
            f'<a:rPr lang="ja-JP" altLang="en-US" sz="{round(t.fontsize * 100)}">'
            '<a:solidFill><a:srgbClr val="000000"/></a:solidFill>'
            f'<a:latin typeface="{font}"/><a:ea typeface="{font}"/>'
            f'<a:cs typeface="{font}"/></a:rPr>'
        )
        paras = "".join(
            f'<a:p><a:pPr algn="{algn}"><a:lnSpc><a:spcPts '
            f'val="{round(pitch * PT_PER_UNIT * 100)}"/></a:lnSpc></a:pPr>'
            + (f"<a:r>{run}<a:t>{escape(line)}</a:t></a:r>" if line else "")
            + f'<a:endParaRPr lang="ja-JP" sz="{round(t.fontsize * 100)}"/></a:p>'
            for line in lines
        )
        xml = (
            f'<xdr:sp><xdr:nvSpPr><xdr:cNvPr id="{self._id()}" name="{escape(name)}"/>'
            f'<xdr:cNvSpPr txBox="1"/></xdr:nvSpPr><xdr:spPr>{box.xfrm()}'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/>'
            "<a:ln><a:noFill/></a:ln></xdr:spPr>"
            '<xdr:txBody><a:bodyPr wrap="none" lIns="0" tIns="0" rIns="0" bIns="0" '
            f'anchor="{anchor}" anchorCtr="0" rtlCol="0"><a:noAutofit/></a:bodyPr>'
            f"<a:lstStyle/>{paras}</xdr:txBody></xdr:sp>"
        )
        return xml, box

    # groups ----------------------------------------------------------------
    def group(self, g: Group) -> list[str]:
        """Anchors for one scene group: a group shape for equipment (outline,
        texts, tag) and for a line (pieces, ticks); loose items for the sheet."""
        parts: list[tuple[str, _Box]] = []
        if g.kind == "equipment":
            shapes = [i for i in g.items if isinstance(i, (Polyline, Ellipse))]
            # piping drawn by the element (header, tee) is a line, like pipes
            pipes = [s for s in shapes if isinstance(s, Polyline) and s.pipe]
            for k, s in enumerate(pipes):
                pts = s.points + ((s.points[0],) if s.closed else ())
                parts += self.stroke(Stroke(pts), f"{g.id} pipe {k + 1}")
            symbol = [s for s in shapes if not (isinstance(s, Polyline) and s.pipe)]
            if symbol:
                parts += self.symbol(g, symbol)
        for i, item in enumerate(i for i in g.items if isinstance(i, (Text, Stroke))):
            name = f"{g.id} {i + 1}"
            if isinstance(item, Text):
                parts.append(self.text(item, f"{g.id}: {item.text.splitlines()[0]}"))
            elif g.kind == "sheet" and _is_frame(item):
                parts.append(self.rect(item, "frame"))
            else:
                parts += self.stroke(item, name)
        if not parts:
            return []
        if g.kind == "sheet" or len(parts) == 1:
            return [_anchor(xml, box) for xml, box in parts]
        box = parts[0][1]
        for _, b in parts[1:]:
            box = box.union(b)
        xml = (
            f'<xdr:grpSp><xdr:nvGrpSpPr><xdr:cNvPr id="{self._id()}" '
            f'name="{escape(g.id)}"/><xdr:cNvGrpSpPr/></xdr:nvGrpSpPr>'
            f"<xdr:grpSpPr>{box.xfrm(child=True)}</xdr:grpSpPr>"
            + "".join(x for x, _ in parts)
            + "</xdr:grpSp>"
        )
        return [_anchor(xml, box)]


def _paint(fill: str | None) -> str:
    if not fill:
        return "<a:noFill/>"
    return f'<a:solidFill><a:srgbClr val="{to_hex(fill)[1:].upper()}"/></a:solidFill>'


def _is_frame(s: Stroke) -> bool:
    """A closed axis-aligned rectangle (the sheet frame)."""
    pts = s.points
    return (
        len(pts) == 5
        and pts[0] == pts[-1]
        and all(a[0] == b[0] or a[1] == b[1] for a, b in zip(pts, pts[1:]))
    )


def _anchor(xml: str, box: _Box) -> str:
    return (
        f'<xdr:absoluteAnchor><xdr:pos x="{box.x}" y="{box.y}"/>'
        f'<xdr:ext cx="{box.cx}" cy="{box.cy}"/>{xml}<xdr:clientData/>'
        "</xdr:absoluteAnchor>"
    )


# -- package -------------------------------------------------------------------
def write_xlsx(sc: Scene, path: str | Path, sheet_name: str = "PFD") -> None:
    """Write ``sc`` as a one-sheet workbook."""
    w = _Writer(sc)
    anchors = [a for g in sc.groups for a in w.group(g)]
    drawing = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<xdr:wsDr {NS}>{''.join(anchors)}</xdr:wsDr>"
    )
    x0, y0, x1, y1 = sc.bounds
    width_px = (x1 - x0 + 2 * MARGIN) * UNIT / INCH * 96
    zoom = max(10, min(100, math.floor(1800 / width_px * 100)))
    files = {
        "[Content_Types].xml": _content_types(),
        "_rels/.rels": _rels([("rId1", f"{REL}/officeDocument", "xl/workbook.xml")]),
        "xl/workbook.xml": (
            f'<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets>'
            f'<sheet name="{escape(sheet_name)}" sheetId="1" r:id="rId1"/>'
            "</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": _rels(
            [
                ("rId1", f"{REL}/worksheet", "worksheets/sheet1.xml"),
                ("rId2", f"{REL}/styles", "styles.xml"),
            ]
        ),
        "xl/styles.xml": _STYLES,
        "xl/worksheets/sheet1.xml": (
            f'<worksheet xmlns="{MAIN}" xmlns:r="{REL}">'
            '<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>'
            f'<sheetViews><sheetView workbookViewId="0" showGridLines="0" '
            f'zoomScale="{zoom}" zoomScaleNormal="{zoom}"/></sheetViews>'
            "<sheetData/>"
            '<pageMargins left="0.4" right="0.4" top="0.4" bottom="0.4" '
            'header="0" footer="0"/>'
            '<pageSetup paperSize="8" orientation="landscape" fitToWidth="1" '
            'fitToHeight="1"/><drawing r:id="rId1"/></worksheet>'
        ),
        "xl/worksheets/_rels/sheet1.xml.rels": _rels(
            [("rId1", f"{REL}/drawing", "../drawings/drawing1.xml")]
        ),
        "xl/drawings/drawing1.xml": drawing,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            if not text.startswith("<?xml"):
                text = (
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + text
                )
            z.writestr(name, text.encode("utf-8"))


def _rels(rels: list[tuple[str, str, str]]) -> str:
    body = "".join(
        f'<Relationship Id="{i}" Type="{t}" Target="{escape(target)}"/>'
        for i, t, target in rels
    )
    return f'<Relationships xmlns="{PKG_REL}">{body}</Relationships>'


def _content_types() -> str:
    ct = "application/vnd.openxmlformats-officedocument"
    parts = {
        "/xl/workbook.xml": f"{ct}.spreadsheetml.sheet.main+xml",
        "/xl/worksheets/sheet1.xml": f"{ct}.spreadsheetml.worksheet+xml",
        "/xl/styles.xml": f"{ct}.spreadsheetml.styles+xml",
        "/xl/drawings/drawing1.xml": f"{ct}.drawing+xml",
    }
    return (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        + "".join(
            f'<Override PartName="{p}" ContentType="{t}"/>' for p, t in parts.items()
        )
        + "</Types>"
    )


_STYLES = (
    f'<styleSheet xmlns="{MAIN}">'
    '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border>'
    "</borders>"
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>'
    "</cellStyleXfs>"
    '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" '
    'xfId="0"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/>'
    "</cellStyles></styleSheet>"
)
