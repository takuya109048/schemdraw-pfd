//! SVG output of a [`Scene`]: drawing units of 0.5 inch (36 pt), y up, as
//! SchemDraw draws it.

use std::fmt::Write;

use crate::geometry::Point;
use crate::render::{PT_PER_UNIT, Scene, Seg};

const MARGIN: f64 = 0.1; // drawing units around the sheet

struct Frame {
    x0: f64,
    y1: f64,
}

impl Frame {
    fn x(&self, x: f64) -> f64 {
        (x - self.x0) * PT_PER_UNIT
    }
    fn y(&self, y: f64) -> f64 {
        (self.y1 - y) * PT_PER_UNIT
    }
    fn pt(&self, p: Point) -> String {
        format!("{} {}", num(self.x(p.0)), num(self.y(p.1)))
    }
}

fn num(v: f64) -> String {
    let r = (v * 1000.0).round() / 1000.0;
    let s = format!("{r:.3}");
    let s = s.trim_end_matches('0').trim_end_matches('.').to_string();
    if s == "-0" { "0".into() } else { s }
}

fn esc(t: &str) -> String {
    t.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

/// matplotlib's dash pattern for a line style, scaled by the line width.
fn dashes(ls: &str, lw: f64) -> Option<String> {
    let pattern: &[f64] = match ls {
        "--" | "dashed" => &[3.7, 1.6],
        ":" | "dotted" => &[1.0, 1.65],
        "-." | "dashdot" => &[6.4, 1.6, 1.0, 1.6],
        _ => return None,
    };
    Some(pattern.iter().map(|d| num(d * lw)).collect::<Vec<_>>().join(","))
}

fn stroke_attrs(color: &str, lw: f64, ls: &str) -> String {
    let mut s = format!(
        r#"stroke="{}" stroke-width="{}" stroke-linecap="round" stroke-linejoin="round""#,
        esc(color),
        num(lw)
    );
    if let Some(d) = dashes(ls, lw) {
        let _ = write!(s, r#" stroke-dasharray="{d}""#);
    }
    s
}

fn arrowhead(f: &Frame, tip: Point, theta: f64, width: f64, length: f64, color: &str) -> String {
    let (c, s) = (theta.to_radians().cos(), theta.to_radians().sin());
    let (dx, dy) = (length / 2.0 * c, length / 2.0 * s);
    let tail = (tip.0 - dx, tip.1 - dy);
    let full = (dx * dx + dy * dy).sqrt();
    let rot = |x: f64, y: f64| (x * c - y * s + tail.0, x * s + y * c + tail.1);
    let fin1 = rot(full - length, width / 2.0);
    let fin2 = rot(full - length, -width / 2.0);
    format!(
        r#"<path d="M {} L {} L {} Z" fill="{}" stroke="none"/>"#,
        f.pt(fin1),
        f.pt(tip),
        f.pt(fin2),
        esc(color)
    )
}

pub fn to_svg(sc: &Scene) -> String {
    let (bx0, by0, bx1, by1) = sc.bounds;
    let f = Frame {
        x0: bx0 - MARGIN,
        y1: by1 + MARGIN,
    };
    let w = (bx1 - bx0 + 2.0 * MARGIN) * PT_PER_UNIT;
    let h = (by1 - by0 + 2.0 * MARGIN) * PT_PER_UNIT;
    let lw0 = sc.theme.equipment_lw;
    let mut fonts: Vec<String> = sc.theme.fonts.iter().map(|f| format!("'{f}'")).collect();
    fonts.push("sans-serif".into());
    let family = fonts.join(", ");
    let mut out = String::new();
    let _ = writeln!(out, r#"<?xml version="1.0" encoding="utf-8" standalone="no"?>"#);
    let _ = writeln!(
        out,
        r#"<svg xmlns="http://www.w3.org/2000/svg" width="{}pt" height="{}pt" viewBox="0 0 {} {}" version="1.1">"#,
        num(w),
        num(h),
        num(w),
        num(h)
    );
    let _ = writeln!(
        out,
        r#"<rect width="100%" height="100%" fill="{}"/>"#,
        esc(&sc.theme.background)
    );
    for g in &sc.groups {
        let _ = writeln!(out, r#"<g class="{}" id="{}">"#, g.kind, esc(&g.id));
        for s in &g.segs {
            match s {
                Seg::Line {
                    path,
                    color,
                    lw,
                    ls,
                    arrow,
                    arrowwidth,
                    arrowlength,
                } => {
                    let color = color.as_deref().unwrap_or("black");
                    let lw = lw.unwrap_or(lw0);
                    let ls = ls.as_deref().unwrap_or("-");
                    let mut line: Vec<Point> = path.clone();
                    let n = path.len();
                    let head = arrow.as_deref().is_some_and(|a| a.contains('>')) && n >= 2;
                    if head {
                        let (a, b) = (path[n - 2], path[n - 1]);
                        let th = (b.1 - a.1).atan2(b.0 - a.0);
                        line[n - 1] = (b.0 - arrowlength * th.cos(), b.1 - arrowlength * th.sin());
                    }
                    let d: Vec<String> = line.iter().map(|&p| f.pt(p)).collect();
                    let _ = writeln!(
                        out,
                        r#"<path d="M {}" fill="none" {}/>"#,
                        d.join(" L "),
                        stroke_attrs(color, lw, ls)
                    );
                    if head {
                        let (a, b) = (path[n - 2], path[n - 1]);
                        let theta = (b.1 - a.1).atan2(b.0 - a.0).to_degrees();
                        let _ = writeln!(out, "{}", arrowhead(&f, b, theta, *arrowwidth, *arrowlength, color));
                    }
                }
                Seg::Text {
                    xy,
                    text,
                    align,
                    fontsize,
                    color,
                } => {
                    let color = color.as_deref().unwrap_or("black");
                    let anchor = match align.0.as_str() {
                        "left" => "start",
                        "right" => "end",
                        _ => "middle",
                    };
                    let lines: Vec<&str> = text.split('\n').collect();
                    let pitch = fontsize * 1.2;
                    let block = pitch * (lines.len() as f64 - 1.0);
                    // y of the first line's middle, in pt below the anchor
                    let first = match align.1.as_str() {
                        "top" => fontsize * 0.5,
                        "bottom" => -block - fontsize * 0.5,
                        _ => -block / 2.0,
                    };
                    let _ = write!(
                        out,
                        r#"<text font-family="{}" font-size="{}" fill="{}" text-anchor="{}">"#,
                        esc(&family),
                        num(*fontsize),
                        esc(color),
                        anchor
                    );
                    for (k, l) in lines.iter().enumerate() {
                        let y = f.y(xy.1) + first + pitch * k as f64;
                        let _ = write!(
                            out,
                            r#"<tspan x="{}" y="{}" dominant-baseline="central">{}</tspan>"#,
                            num(f.x(xy.0)),
                            num(y),
                            esc(l)
                        );
                    }
                    let _ = writeln!(out, "</text>");
                }
                Seg::Poly {
                    verts,
                    closed,
                    fill,
                    color,
                    ls,
                } => {
                    let color = color.as_deref().unwrap_or("black");
                    let d: Vec<String> = verts.iter().map(|&p| f.pt(p)).collect();
                    let _ = writeln!(
                        out,
                        r#"<path d="M {}{}" fill="{}" {}/>"#,
                        d.join(" L "),
                        if *closed { " Z" } else { "" },
                        esc(fill.as_deref().unwrap_or("none")),
                        stroke_attrs(color, lw0, ls.as_deref().unwrap_or("-"))
                    );
                }
                Seg::Circle {
                    center,
                    radius,
                    fill,
                    color,
                    ls,
                } => {
                    let color = color.as_deref().unwrap_or("black");
                    let _ = writeln!(
                        out,
                        r#"<circle cx="{}" cy="{}" r="{}" fill="{}" {}/>"#,
                        num(f.x(center.0)),
                        num(f.y(center.1)),
                        num(radius * PT_PER_UNIT),
                        esc(fill.as_deref().unwrap_or("none")),
                        stroke_attrs(color, lw0, ls.as_deref().unwrap_or("-"))
                    );
                }
                Seg::Arc {
                    center,
                    width,
                    height,
                    theta1,
                    theta2,
                    color,
                    ls,
                } => {
                    let color = color.as_deref().unwrap_or("black");
                    let (rx, ry) = (width / 2.0, height / 2.0);
                    let attrs = stroke_attrs(color, lw0, ls.as_deref().unwrap_or("-"));
                    let span = theta2 - theta1;
                    if span.abs() >= 360.0 - 1e-9 {
                        let _ = writeln!(
                            out,
                            r#"<ellipse cx="{}" cy="{}" rx="{}" ry="{}" fill="none" {attrs}/>"#,
                            num(f.x(center.0)),
                            num(f.y(center.1)),
                            num(rx * PT_PER_UNIT),
                            num(ry * PT_PER_UNIT)
                        );
                    } else {
                        let span = span.rem_euclid(360.0);
                        let at = |deg: f64| {
                            let r = deg.to_radians();
                            (center.0 + rx * r.cos(), center.1 + ry * r.sin())
                        };
                        let (a, b) = (at(*theta1), at(theta1 + span));
                        let large = if span > 180.0 { 1 } else { 0 };
                        let _ = writeln!(
                            out,
                            r#"<path d="M {} A {} {} 0 {} 0 {}" fill="none" {attrs}/>"#,
                            f.pt(a),
                            num(rx * PT_PER_UNIT),
                            num(ry * PT_PER_UNIT),
                            large,
                            f.pt(b)
                        );
                    }
                }
            }
        }
        let _ = writeln!(out, "</g>");
    }
    out.push_str("</svg>\n");
    out
}
