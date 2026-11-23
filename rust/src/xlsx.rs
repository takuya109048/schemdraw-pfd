//! Excel output: the sheet as editable drawing objects in a workbook (see
//! the Python ``xlsx`` module).  Every piece of equipment is drawn part by
//! part as Excel shapes (lines, arcs, ovals, filled freeforms), every line
//! as one Excel line per straight piece (the last with an arrowhead), texts
//! as text boxes; each element and line is a group.  Shapes sit at absolute
//! positions, drawing units mapped to EMU at 0.5 inch per unit.

use std::io::Write;

use crate::colors;
use crate::error::{Error, Result};
use crate::geometry::{Ellipse, Point, Polyline, Shape};
use crate::pyval::round_half_even;
use crate::render::{Group, Item, PT_PER_UNIT, Scene, Seg, text_width};

pub const INCH: i64 = 914400;
pub const UNIT: i64 = INCH / 2;
pub const PT: i64 = 12700;
pub const MARGIN: f64 = 0.5;
const ANGLE: i64 = 60000;
const FULL: i64 = 360 * ANGLE;
const NS: &str = r#"xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships""#;
const MAIN: &str = "http://schemas.openxmlformats.org/spreadsheetml/2006/main";
const REL: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
const PKG_REL: &str = "http://schemas.openxmlformats.org/package/2006/relationships";
const XML_HEAD: &str = "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n";

fn dash(ls: &str) -> &'static str {
    match ls {
        "-" => "solid",
        "--" => "dash",
        ":" => "sysDot",
        "-." => "dashDot",
        _ => "solid",
    }
}

/// ``xml.sax.saxutils.escape`` with attribute quotes escaped as well.
fn esc(t: &str) -> String {
    t.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

fn esc_text(t: &str) -> String {
    t.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

/// ``RRGGBB`` of a colour (black if unknown).
fn hex(color: &str) -> String {
    colors::hex(color).unwrap_or_else(|| "000000".into())
}

/// A rectangle in EMU, top-left origin (Excel's).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct EmuBox {
    pub x: i64,
    pub y: i64,
    pub cx: i64,
    pub cy: i64,
}

impl EmuBox {
    fn union(&self, o: &EmuBox) -> EmuBox {
        let (x, y) = (self.x.min(o.x), self.y.min(o.y));
        let r = (self.x + self.cx).max(o.x + o.cx);
        let b = (self.y + self.cy).max(o.y + o.cy);
        EmuBox {
            x,
            y,
            cx: r - x,
            cy: b - y,
        }
    }

    fn xfrm(&self, tag: &str, child: bool, flip_h: bool, flip_v: bool) -> String {
        let mut inner = format!(
            r#"<a:off x="{}" y="{}"/><a:ext cx="{}" cy="{}"/>"#,
            self.x, self.y, self.cx, self.cy
        );
        if child {
            inner += &format!(
                r#"<a:chOff x="{}" y="{}"/><a:chExt cx="{}" cy="{}"/>"#,
                self.x, self.y, self.cx, self.cy
            );
        }
        let flips = format!(
            "{}{}",
            if flip_h { r#" flipH="1""# } else { "" },
            if flip_v { r#" flipV="1""# } else { "" }
        );
        format!("<{tag}{flips}>{inner}</{tag}>")
    }

    fn plain(&self) -> String {
        self.xfrm("a:xfrm", false, false, false)
    }
}

/// Drawing units -> EMU on the worksheet (x right, y down).
struct At {
    ox: f64,
    oy: f64,
}

impl At {
    fn x(&self, x: f64) -> i64 {
        round_half_even((x - self.ox) * UNIT as f64)
    }
    fn y(&self, y: f64) -> i64 {
        round_half_even((self.oy - y) * UNIT as f64)
    }
    fn bx(&self, x0: f64, y0: f64, x1: f64, y1: f64) -> EmuBox {
        let (left, top) = (self.x(x0), self.y(y1));
        EmuBox {
            x: left,
            y: top,
            cx: self.x(x1) - left,
            cy: self.y(y0) - top,
        }
    }
}

fn extent(p: &Polyline) -> (f64, f64, f64, f64) {
    let xs = p.points.iter().map(|q| q.0);
    let ys = p.points.iter().map(|q| q.1);
    (
        xs.clone().fold(f64::INFINITY, f64::min),
        ys.clone().fold(f64::INFINITY, f64::min),
        xs.fold(f64::NEG_INFINITY, f64::max),
        ys.fold(f64::NEG_INFINITY, f64::max),
    )
}

/// Line pitch (points) of a text block as SchemDraw draws it: the height
/// of "lp" in the font (as matplotlib measures DejaVu Sans, its fallback:
/// 0.9677 em, on 1/64 point).
pub fn pitch(fontsize: f64) -> f64 {
    (fontsize * 0.9677 * 64.0).round() / 64.0
}

type Part = (String, EmuBox);

struct Writer<'a> {
    sc: &'a Scene,
    at: At,
    next_id: i64,
    font: String,
    color: String,
    dashed: bool,
}

/// A stroke: points and style (``lw`` None: the equipment line width).
struct Stroke<'a> {
    points: &'a [Point],
    lw: Option<f64>,
    ls: Option<&'a str>,
    arrow: Option<&'a str>,
}

impl Writer<'_> {
    fn id(&mut self) -> i64 {
        self.next_id += 1;
        self.next_id - 1
    }

    fn tint(&self, fill: Option<&str>) -> Option<String> {
        fill.map(|f| {
            if self.color != "000000" {
                format!("#{}", self.color)
            } else {
                f.to_string()
            }
        })
    }

    fn ln(&self, lw: f64, ls: &str, head: bool, tail: bool) -> String {
        let tip = |w: &str| format!(r#"<a:{w}End type="triangle" w="lg" len="lg"/>"#);
        let d = dash(if self.dashed { "--" } else { ls });
        let ends = format!(
            "{}{}",
            if head { tip("head") } else { String::new() },
            if tail { tip("tail") } else { String::new() }
        );
        format!(
            r#"<a:ln w="{}" cap="rnd"><a:solidFill><a:srgbClr val="{}"/></a:solidFill><a:prstDash val="{d}"/><a:round/>{ends}</a:ln>"#,
            round_half_even(lw * PT as f64),
            self.color
        )
    }

    fn cnv(&mut self, name: &str, descr: &str) -> String {
        let d = if descr.is_empty() {
            String::new()
        } else {
            format!(r#" descr="{}""#, esc(descr))
        };
        format!(r#"<xdr:cNvPr id="{}" name="{}"{d}/>"#, self.id(), esc(name))
    }

    /// One straight line, flipped to run from ``a`` to ``b``.
    fn line(&mut self, a: Point, b: Point, name: &str, ln: &str, descr: &str) -> Part {
        let (x1, y1, x2, y2) = (self.at.x(a.0), self.at.y(a.1), self.at.x(b.0), self.at.y(b.1));
        let bx = EmuBox {
            x: x1.min(x2),
            y: y1.min(y2),
            cx: (x2 - x1).abs(),
            cy: (y2 - y1).abs(),
        };
        let cnv = self.cnv(name, descr);
        let xml = format!(
            r#"<xdr:cxnSp macro=""><xdr:nvCxnSpPr>{cnv}<xdr:cNvCxnSpPr/></xdr:nvCxnSpPr><xdr:spPr>{}<a:prstGeom prst="line"><a:avLst/></a:prstGeom>{ln}</xdr:spPr></xdr:cxnSp>"#,
            bx.xfrm("a:xfrm", false, x2 < x1, y2 < y1)
        );
        (xml, bx)
    }

    /// A polyline as one straight line per segment; arrows only at its ends.
    fn lines(&mut self, pts: &[Point], name: &str, lw: f64, ls: &str, arrow: &str, descr: &str) -> Vec<Part> {
        let segs: Vec<(Point, Point)> = pts.windows(2).filter(|w| w[0] != w[1]).map(|w| (w[0], w[1])).collect();
        let n = segs.len();
        let mut out = Vec::new();
        for (k, (a, b)) in segs.into_iter().enumerate() {
            let ln = self.ln(
                lw,
                ls,
                k == 0 && arrow.starts_with('<'),
                k == n - 1 && arrow.ends_with('>'),
            );
            out.push(self.line(a, b, &format!("{name}-{}", k + 1), &ln, descr));
        }
        out
    }

    fn stroke(&mut self, s: &Stroke, name: &str) -> Vec<Part> {
        let lw = s.lw.filter(|v| *v != 0.0).unwrap_or(self.sc.theme.equipment_lw);
        let ls = s.ls.filter(|v| !v.is_empty()).unwrap_or("-").to_string();
        self.lines(s.points, name, lw, &ls, s.arrow.unwrap_or(""), "")
    }

    /// A whole ellipse (an Excel oval) or an arc (an Excel arc).
    fn ellipse(&mut self, e: &Ellipse, name: &str, descr: &str) -> Part {
        let bx = self.at.bx(e.cx - e.rx, e.cy - e.ry, e.cx + e.rx, e.cy + e.ry);
        let geom = if e.full() {
            r#"<a:prstGeom prst="ellipse"><a:avLst/></a:prstGeom>"#.to_string()
        } else {
            let adj1 = round_half_even(-e.theta2 * ANGLE as f64).rem_euclid(FULL);
            let adj2 = round_half_even(-e.theta1 * ANGLE as f64).rem_euclid(FULL);
            format!(
                r#"<a:prstGeom prst="arc"><a:avLst><a:gd name="adj1" fmla="val {adj1}"/><a:gd name="adj2" fmla="val {adj2}"/></a:avLst></a:prstGeom>"#
            )
        };
        let fill = if e.full() { self.tint(e.fill.as_deref()) } else { None };
        let cnv = self.cnv(name, descr);
        let xml = format!(
            "<xdr:sp><xdr:nvSpPr>{cnv}<xdr:cNvSpPr/></xdr:nvSpPr><xdr:spPr>{}{geom}{}{}</xdr:spPr></xdr:sp>",
            bx.plain(),
            paint(fill.as_deref()),
            self.ln(self.sc.theme.equipment_lw, "-", false, false)
        );
        (xml, bx)
    }

    /// A solid (filled) polygon as one freeform.
    fn solid(&mut self, poly: &Polyline, name: &str, descr: &str) -> Part {
        let (a, b, c, d) = extent(poly);
        let bx = self.at.bx(a, b, c, d);
        let pts: String = poly
            .points
            .iter()
            .enumerate()
            .map(|(k, &(x, y))| {
                let tag = if k == 0 { "moveTo" } else { "lnTo" };
                format!(
                    r#"<a:{tag}><a:pt x="{}" y="{}"/></a:{tag}>"#,
                    self.at.x(x) - bx.x,
                    self.at.y(y) - bx.y
                )
            })
            .collect();
        let cnv = self.cnv(name, descr);
        let xml = format!(
            r#"<xdr:sp><xdr:nvSpPr>{cnv}<xdr:cNvSpPr/></xdr:nvSpPr><xdr:spPr>{}<a:custGeom><a:avLst/><a:gdLst/><a:ahLst/><a:cxnLst/><a:rect l="0" t="0" r="r" b="b"/><a:pathLst><a:path w="{}" h="{}">{pts}<a:close/></a:path></a:pathLst></a:custGeom>{}{}</xdr:spPr></xdr:sp>"#,
            bx.plain(),
            bx.cx.max(1),
            bx.cy.max(1),
            paint(self.tint(poly.fill.as_deref()).as_deref()),
            self.ln(self.sc.theme.equipment_lw, "-", false, false)
        );
        (xml, bx)
    }

    /// The sheet frame: one rectangle.
    fn rect(&mut self, s: &Stroke, name: &str) -> Part {
        let xs = s.points.iter().map(|p| p.0);
        let ys = s.points.iter().map(|p| p.1);
        let bx = self.at.bx(
            xs.clone().fold(f64::INFINITY, f64::min),
            ys.clone().fold(f64::INFINITY, f64::min),
            xs.fold(f64::NEG_INFINITY, f64::max),
            ys.fold(f64::NEG_INFINITY, f64::max),
        );
        let lw = s.lw.filter(|v| *v != 0.0).unwrap_or(self.sc.theme.equipment_lw);
        let cnv = self.cnv(name, "");
        let xml = format!(
            r#"<xdr:sp><xdr:nvSpPr>{cnv}<xdr:cNvSpPr/></xdr:nvSpPr><xdr:spPr>{}<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/>{}</xdr:spPr></xdr:sp>"#,
            bx.plain(),
            self.ln(lw, "-", false, false)
        );
        (xml, bx)
    }

    /// The element's outline part by part.
    fn symbol(&mut self, g: &Group, shapes: &[&Shape]) -> Vec<Part> {
        let typ = g.typ.clone().unwrap_or_default();
        let lw = self.sc.theme.equipment_lw;
        let mut out = Vec::new();
        for (k, s) in shapes.iter().enumerate() {
            let name = format!("{} {}", g.pid.as_deref().unwrap_or(&g.id), k + 1);
            match s {
                Shape::Ell(e) => out.push(self.ellipse(e, &name, &typ)),
                Shape::Poly(p) if p.fill.is_some() => out.push(self.solid(p, &name, &typ)),
                Shape::Poly(p) => {
                    let mut pts = p.points.clone();
                    if p.closed {
                        pts.push(p.points[0]);
                    }
                    out.extend(self.lines(&pts, &name, lw, "-", "", &typ));
                }
            }
        }
        out
    }

    #[allow(clippy::too_many_arguments)]
    fn text(
        &mut self,
        x: f64,
        y: f64,
        text: &str,
        fontsize: f64,
        ha: &str,
        va: &str,
        color: Option<&str>,
        name: &str,
    ) -> Part {
        let em = fontsize / PT_PER_UNIT;
        let lines: Vec<&str> = text.split('\n').collect();
        let pitch_u = pitch(fontsize) / PT_PER_UNIT;
        let w = text_width(text, em) * 1.4 + 2.0 * em;
        let h = lines.len() as f64 * pitch_u + 0.4 * em;
        let left = match ha {
            "left" => x,
            "right" => x - w,
            _ => x - w / 2.0,
        };
        let top = match va {
            "top" => y,
            "bottom" => y + h,
            _ => y + h / 2.0,
        };
        let bx = self.at.bx(left, top - h, left + w, top);
        let anchor = match va {
            "top" => "t",
            "bottom" => "b",
            _ => "ctr",
        };
        let algn = match ha {
            "left" => "l",
            "right" => "r",
            _ => "ctr",
        };
        let font = esc(&self.font);
        let color = color.map(hex).unwrap_or_else(|| self.color.clone());
        let sz = round_half_even(fontsize * 100.0);
        let run = format!(
            r#"<a:rPr lang="ja-JP" altLang="en-US" sz="{sz}"><a:solidFill><a:srgbClr val="{color}"/></a:solidFill><a:latin typeface="{font}"/><a:ea typeface="{font}"/><a:cs typeface="{font}"/></a:rPr>"#
        );
        let spc = round_half_even(pitch_u * PT_PER_UNIT * 100.0);
        let paras: String = lines
            .iter()
            .map(|line| {
                let r = if line.is_empty() { String::new() } else { format!("<a:r>{run}<a:t>{}</a:t></a:r>", esc_text(line)) };
                format!(
                    r#"<a:p><a:pPr algn="{algn}"><a:lnSpc><a:spcPts val="{spc}"/></a:lnSpc></a:pPr>{r}<a:endParaRPr lang="ja-JP" sz="{sz}"/></a:p>"#
                )
            })
            .collect();
        let id = self.id();
        let xml = format!(
            r#"<xdr:sp><xdr:nvSpPr><xdr:cNvPr id="{id}" name="{}"/><xdr:cNvSpPr txBox="1"/></xdr:nvSpPr><xdr:spPr>{}<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/><a:ln><a:noFill/></a:ln></xdr:spPr><xdr:txBody><a:bodyPr wrap="none" lIns="0" tIns="0" rIns="0" bIns="0" anchor="{anchor}" anchorCtr="0" rtlCol="0"><a:noAutofit/></a:bodyPr><a:lstStyle/>{paras}</xdr:txBody></xdr:sp>"#,
            esc(name),
            bx.plain()
        );
        (xml, bx)
    }

    /// Anchors for one scene group: a group shape for equipment and for a
    /// line; loose items for the sheet.
    fn group(&mut self, g: &Group) -> Vec<String> {
        self.color = g.color.as_deref().map(hex).unwrap_or_else(|| "000000".into());
        self.dashed = g.dashed;
        let out = self.group_parts(g);
        self.color = "000000".into();
        self.dashed = false;
        out
    }

    fn group_parts(&mut self, g: &Group) -> Vec<String> {
        let mut parts: Vec<Part> = Vec::new();
        if g.kind == "equipment" {
            let shapes: Vec<&Shape> = g
                .items
                .iter()
                .filter_map(|i| if let Item::Shape(s) = i { Some(s) } else { None })
                .collect();
            let is_pipe = |s: &Shape| matches!(s, Shape::Poly(p) if p.pipe);
            for (k, s) in shapes.iter().filter(|s| is_pipe(s)).enumerate() {
                let Shape::Poly(p) = s else { continue };
                let mut pts = p.points.clone();
                if p.closed {
                    pts.push(p.points[0]);
                }
                let st = Stroke {
                    points: &pts,
                    lw: None,
                    ls: None,
                    arrow: None,
                };
                parts.extend(self.stroke(&st, &format!("{} pipe {}", g.id, k + 1)));
            }
            let symbol: Vec<&Shape> = shapes.iter().copied().filter(|s| !is_pipe(s)).collect();
            if !symbol.is_empty() {
                parts.extend(self.symbol(g, &symbol));
            }
        }
        let others = g
            .items
            .iter()
            .filter_map(|i| if let Item::Seg(s) = i { Some(s) } else { None });
        for (i, seg) in others.enumerate() {
            let name = format!("{} {}", g.id, i + 1);
            match seg {
                Seg::Text {
                    xy,
                    text,
                    align,
                    fontsize,
                    color,
                } => {
                    let first = text.lines().next().unwrap_or("");
                    let part = self.text(
                        xy.0,
                        xy.1,
                        text,
                        *fontsize,
                        &align.0,
                        &align.1,
                        color.as_deref(),
                        &format!("{}: {first}", g.id),
                    );
                    parts.push(part);
                }
                Seg::Line {
                    path, lw, ls, arrow, ..
                } => {
                    let st = Stroke {
                        points: path,
                        lw: *lw,
                        ls: ls.as_deref(),
                        arrow: arrow.as_deref(),
                    };
                    if g.kind == "sheet" && is_frame(path) {
                        parts.push(self.rect(&st, "frame"));
                    } else {
                        parts.extend(self.stroke(&st, &name));
                    }
                }
                _ => {}
            }
        }
        if parts.is_empty() {
            return Vec::new();
        }
        if g.kind == "sheet" || parts.len() == 1 {
            return parts.iter().map(|(x, b)| anchor(x, b)).collect();
        }
        let mut bx = parts[0].1;
        for (_, b) in &parts[1..] {
            bx = bx.union(b);
        }
        let inner: String = parts.iter().map(|(x, _)| x.as_str()).collect();
        let xml = format!(
            r#"<xdr:grpSp><xdr:nvGrpSpPr><xdr:cNvPr id="{}" name="{}"/><xdr:cNvGrpSpPr/></xdr:nvGrpSpPr><xdr:grpSpPr>{}</xdr:grpSpPr>{inner}</xdr:grpSp>"#,
            self.id(),
            esc(&g.id),
            bx.xfrm("a:xfrm", true, false, false)
        );
        vec![anchor(&xml, &bx)]
    }
}

fn paint(fill: Option<&str>) -> String {
    match fill {
        None | Some("") => "<a:noFill/>".into(),
        Some(f) => format!(r#"<a:solidFill><a:srgbClr val="{}"/></a:solidFill>"#, hex(f)),
    }
}

/// A closed axis-aligned rectangle (the sheet frame).
fn is_frame(pts: &[Point]) -> bool {
    pts.len() == 5 && pts[0] == pts[4] && pts.windows(2).all(|w| w[0].0 == w[1].0 || w[0].1 == w[1].1)
}

fn anchor(xml: &str, b: &EmuBox) -> String {
    format!(
        r#"<xdr:absoluteAnchor><xdr:pos x="{}" y="{}"/><xdr:ext cx="{}" cy="{}"/>{xml}<xdr:clientData/></xdr:absoluteAnchor>"#,
        b.x, b.y, b.cx, b.cy
    )
}

fn rels(list: &[(&str, String, &str)]) -> String {
    let body: String = list
        .iter()
        .map(|(i, t, target)| format!(r#"<Relationship Id="{i}" Type="{t}" Target="{}"/>"#, esc(target)))
        .collect();
    format!(r#"<Relationships xmlns="{PKG_REL}">{body}</Relationships>"#)
}

fn content_types() -> String {
    let ct = "application/vnd.openxmlformats-officedocument";
    let parts = [
        ("/xl/workbook.xml", format!("{ct}.spreadsheetml.sheet.main+xml")),
        ("/xl/worksheets/sheet1.xml", format!("{ct}.spreadsheetml.worksheet+xml")),
        ("/xl/styles.xml", format!("{ct}.spreadsheetml.styles+xml")),
        ("/xl/drawings/drawing1.xml", format!("{ct}.drawing+xml")),
    ];
    let overrides: String = parts
        .iter()
        .map(|(p, t)| format!(r#"<Override PartName="{p}" ContentType="{t}"/>"#))
        .collect();
    format!(
        r#"<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>{overrides}</Types>"#
    )
}

fn styles() -> String {
    format!(
        r#"<styleSheet xmlns="{MAIN}"><fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>"#
    )
}

/// The drawing part (``xl/drawings/drawing1.xml``) of ``sc``.
pub fn drawing_xml(sc: &Scene) -> String {
    let (x0, _, _, y1) = sc.bounds;
    let mut w = Writer {
        sc,
        at: At {
            ox: x0 - MARGIN,
            oy: y1 + MARGIN,
        },
        next_id: 2,
        font: sc.theme.fonts.first().cloned().unwrap_or_else(|| "sans-serif".into()),
        color: "000000".into(),
        dashed: false,
    };
    let anchors: String = sc.groups.iter().flat_map(|g| w.group(g)).collect();
    format!("{XML_HEAD}<xdr:wsDr {NS}>{anchors}</xdr:wsDr>")
}

/// The parts of the workbook, in the order they are written.
pub fn package(sc: &Scene, sheet_name: &str) -> Vec<(String, String)> {
    let (x0, _, x1, _) = sc.bounds;
    let width_px = (x1 - x0 + 2.0 * MARGIN) * UNIT as f64 / INCH as f64 * 96.0;
    let zoom = ((1800.0 / width_px * 100.0).floor() as i64).clamp(10, 100);
    let files: Vec<(&str, String)> = vec![
        ("[Content_Types].xml", content_types()),
        (
            "_rels/.rels",
            rels(&[("rId1", format!("{REL}/officeDocument"), "xl/workbook.xml")]),
        ),
        (
            "xl/workbook.xml",
            format!(
                r#"<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets><sheet name="{}" sheetId="1" r:id="rId1"/></sheets></workbook>"#,
                esc(sheet_name)
            ),
        ),
        (
            "xl/_rels/workbook.xml.rels",
            rels(&[
                ("rId1", format!("{REL}/worksheet"), "worksheets/sheet1.xml"),
                ("rId2", format!("{REL}/styles"), "styles.xml"),
            ]),
        ),
        ("xl/styles.xml", styles()),
        (
            "xl/worksheets/sheet1.xml",
            format!(
                r#"<worksheet xmlns="{MAIN}" xmlns:r="{REL}"><sheetPr><pageSetUpPr fitToPage="1"/></sheetPr><sheetViews><sheetView workbookViewId="0" showGridLines="0" zoomScale="{zoom}" zoomScaleNormal="{zoom}"/></sheetViews><sheetData/><pageMargins left="0.4" right="0.4" top="0.4" bottom="0.4" header="0" footer="0"/><pageSetup paperSize="8" orientation="landscape" fitToWidth="1" fitToHeight="1"/><drawing r:id="rId1"/></worksheet>"#
            ),
        ),
        (
            "xl/worksheets/_rels/sheet1.xml.rels",
            rels(&[("rId1", format!("{REL}/drawing"), "../drawings/drawing1.xml")]),
        ),
        ("xl/drawings/drawing1.xml", drawing_xml(sc)),
    ];
    files
        .into_iter()
        .map(|(n, t)| {
            (
                n.to_string(),
                if t.starts_with("<?xml") {
                    t
                } else {
                    format!("{XML_HEAD}{t}")
                },
            )
        })
        .collect()
}

/// Write ``sc`` as a one-sheet workbook.
pub fn write_xlsx(sc: &Scene, path: &str) -> Result<()> {
    let io = |e: std::io::Error| Error::Io(format!("{path}: {e}"));
    let file = std::fs::File::create(path).map_err(io)?;
    let mut z = zip::ZipWriter::new(file);
    let opts = zip::write::SimpleFileOptions::default().compression_method(zip::CompressionMethod::Deflated);
    for (name, text) in package(sc, "PFD") {
        z.start_file(name, opts)
            .map_err(|e| Error::Io(format!("{path}: {e}")))?;
        z.write_all(text.as_bytes()).map_err(io)?;
    }
    z.finish().map_err(|e| Error::Io(format!("{path}: {e}")))?;
    Ok(())
}
