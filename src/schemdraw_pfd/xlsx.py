"""Excel output: the sheet as editable drawing objects in a workbook.

* every piece of equipment is a picture of its outline -- SVG (sharp at any
  zoom in Excel 365) with a high-resolution PNG fallback for other readers --
  cut to exactly the area it covers on the sheet, aspect ratio locked;
* every line is an Excel shape: one freeform polyline per piece between
  crossing gaps, in the line kind's width and dash, the last piece ending in
  an arrowhead;
* tags, texts inside symbols and specification rows are text boxes;
* the frame is a shape too.

Everything comes from the same :class:`~.render.Scene` as the SVG/PNG/PDF
output and is placed with absolute anchors (not attached to cells), drawing
units mapped to EMU at the drawing's own scale (0.5 inch per unit), so
nothing shifts.  A piece of equipment is grouped with its tag; a line with
its pieces and nozzle ticks.

The workbook is written directly (the drawing parts are DrawingML that the
usual Python libraries cannot write: freeform arrows, SVG pictures).
"""

from __future__ import annotations

import io
import math
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import matplotlib
import schemdraw
from matplotlib.backends.backend_agg import RendererAgg
from matplotlib.figure import Figure
from matplotlib.font_manager import FontProperties

from .geometry import Ellipse, Polyline, Shape
from .render import (
    PT_PER_UNIT,
    Group,
    Scene,
    Stroke,
    Text,
    _segment,
    _Shapes,
    _text_width,
    configure,
)

INCH = 914400  # EMU
UNIT = INCH // 2  # one drawing unit: 0.5 inch (SchemDraw's default scale)
PT = 12700  # EMU per point
PNG_DPI = 400  # fallback picture resolution
PAD = 0.15  # drawing units around an outline in its picture (line width, caps)
MARGIN = 0.5  # drawing units of blank sheet around the frame

DASH = {"-": "solid", "--": "dash", ":": "sysDot", "-.": "dashDot"}
SVG_EXT = "{96DAC541-7B7A-43D3-8B79-37D633B846F1}"
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

    def xfrm(self, tag: str = "a:xfrm", child: bool = False) -> str:
        inner = (
            f'<a:off x="{self.x}" y="{self.y}"/><a:ext cx="{self.cx}" cy="{self.cy}"/>'
        )
        if child:
            inner += (
                f'<a:chOff x="{self.x}" y="{self.y}"/>'
                f'<a:chExt cx="{self.cx}" cy="{self.cy}"/>'
            )
        return f"<{tag}>{inner}</{tag}>"


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


# -- pictures ------------------------------------------------------------------
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


def _picture(
    shapes: list[Shape], area: tuple[float, float, float, float], sc: Scene
) -> tuple[bytes, bytes]:
    """PNG and SVG of ``shapes`` drawn exactly as on the sheet, covering exactly
    ``area`` (drawing units) at the sheet's scale, transparent elsewhere."""
    matplotlib.use("Agg")
    schemdraw.use("matplotlib")
    x0, y0, x1, y1 = area
    w, h = x1 - x0, y1 - y0
    fig = Figure(figsize=(w * UNIT / INCH, h * UNIT / INCH))
    ax = fig.add_axes((0.0, 0.0, 1.0, 1.0))
    d = schemdraw.Drawing(show=False)
    configure(d, sc)
    d.add(_Shapes([_segment(s) for s in shapes]).at((0, 0)))
    d.draw(show=False, canvas=ax)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("auto")
    ax.set_position((0.0, 0.0, 1.0, 1.0))
    ax.set_axis_off()
    out = []
    for fmt, kw in (("png", {"dpi": PNG_DPI}), ("svg", {})):
        buf = io.BytesIO()
        fig.savefig(buf, format=fmt, transparent=True, **kw)
        out.append(buf.getvalue())
    return out[0], out[1]


# -- DrawingML -----------------------------------------------------------------
class _Writer:
    def __init__(self, sc: Scene):
        self.sc = sc
        self.at = _Sheet(sc)
        self.next_id = 2
        self.media: list[tuple[str, bytes]] = []  # (file name, data)
        self.rels: list[tuple[str, str]] = []  # (rId, target)
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

    def _rel(self, name: str, data: bytes) -> str:
        self.media.append((name, data))
        rid = f"rId{len(self.rels) + 1}"
        self.rels.append((rid, f"../media/{name}"))
        return rid

    # one item --------------------------------------------------------------
    def picture(self, g: Group, shapes: list[Shape]) -> tuple[str, _Box]:
        p = g.placed
        assert p is not None
        ex = _extent(shapes)
        bx0, by0, bx1, by1 = p.bbox
        area = (
            min(ex[0], bx0) - PAD,
            min(ex[1], by0) - PAD,
            max(ex[2], bx1) + PAD,
            max(ex[3], by1) + PAD,
        )
        png, svg = _picture(shapes, area, self.sc)
        k = len(self.media) // 2 + 1
        png_id = self._rel(f"image{k}.png", png)
        svg_id = self._rel(f"image{k}.svg", svg)
        box = self.at.box(*area)
        xml = (
            f'<xdr:pic><xdr:nvPicPr><xdr:cNvPr id="{self._id()}" '
            f'name="{escape(p.id)}" descr="{escape(p.type)}"/>'
            '<xdr:cNvPicPr><a:picLocks noChangeAspect="1"/></xdr:cNvPicPr>'
            f'</xdr:nvPicPr><xdr:blipFill><a:blip r:embed="{png_id}"><a:extLst>'
            f'<a:ext uri="{SVG_EXT}"><asvg:svgBlip xmlns:asvg='
            '"http://schemas.microsoft.com/office/drawing/2016/SVG/main" '
            f'r:embed="{svg_id}"/></a:ext></a:extLst></a:blip>'
            "<a:stretch><a:fillRect/></a:stretch></xdr:blipFill>"
            f'<xdr:spPr>{box.xfrm()}<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
            "</xdr:spPr></xdr:pic>"
        )
        return xml, box

    def stroke(self, s: Stroke, name: str) -> tuple[str, _Box]:
        at = self.at
        xs = [at.x(x) for x, _ in s.points]
        ys = [at.y(y) for _, y in s.points]
        box = _Box(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
        w, h = max(box.cx, 1), max(box.cy, 1)
        pts = [(x - box.x, y - box.y) for x, y in zip(xs, ys)]
        path = f'<a:moveTo><a:pt x="{pts[0][0]}" y="{pts[0][1]}"/></a:moveTo>'
        path += "".join(f'<a:lnTo><a:pt x="{x}" y="{y}"/></a:lnTo>' for x, y in pts[1:])
        lw = s.style.get("lw") or self.sc.theme.equipment_lw
        dash = DASH.get(str(s.style.get("ls") or "-"), "solid")
        arrow = str(s.style.get("arrow") or "")
        ends = ""
        tip = '<a:{}End type="triangle" w="lg" len="lg"/>'
        if arrow.startswith("<"):
            ends += tip.format("head")
        if arrow.endswith(">"):
            ends += tip.format("tail")
        xml = (
            f'<xdr:sp><xdr:nvSpPr><xdr:cNvPr id="{self._id()}" name="{escape(name)}"/>'
            f"<xdr:cNvSpPr/></xdr:nvSpPr><xdr:spPr>{box.xfrm()}"
            "<a:custGeom><a:avLst/><a:gdLst/><a:ahLst/><a:cxnLst/>"
            '<a:rect l="0" t="0" r="r" b="b"/>'
            f'<a:pathLst><a:path w="{w}" h="{h}" fill="none">{path}</a:path>'
            "</a:pathLst></a:custGeom><a:noFill/>"
            f'<a:ln w="{round(lw * PT)}" cap="rnd">'
            '<a:solidFill><a:srgbClr val="000000"/></a:solidFill>'
            f'<a:prstDash val="{dash}"/><a:round/>{ends}</a:ln></xdr:spPr></xdr:sp>'
        )
        return xml, box

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
        """Anchors for one scene group: a group shape for equipment (picture,
        texts, tag) and for a line (pieces, ticks); loose items for the sheet."""
        parts: list[tuple[str, _Box]] = []
        if g.kind == "equipment":
            shapes = [i for i in g.items if isinstance(i, (Polyline, Ellipse))]
            if shapes:
                parts.append(self.picture(g, shapes))
        for i, item in enumerate(i for i in g.items if isinstance(i, (Text, Stroke))):
            name = f"{g.id} {i + 1}"
            if isinstance(item, Text):
                parts.append(self.text(item, f"{g.id}: {item.text.splitlines()[0]}"))
            else:
                parts.append(self.stroke(item, name))
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
        "xl/drawings/_rels/drawing1.xml.rels": _rels(
            [(rid, f"{REL}/image", target) for rid, target in w.rels]
        ),
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            if not text.startswith("<?xml"):
                text = (
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + text
                )
            z.writestr(name, text.encode("utf-8"))
        for name, data in w.media:
            z.writestr(f"xl/media/{name}", data)


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
        '<Default Extension="png" ContentType="image/png"/>'
        '<Default Extension="svg" ContentType="image/svg+xml"/>'
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
