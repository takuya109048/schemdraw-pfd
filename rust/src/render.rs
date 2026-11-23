//! Draw a built diagram in the style of a Japanese P&ID sheet (see the
//! Python ``render`` module): equipment outlines, underlined tags, flange
//! ticks at vessel nozzles, crossing gaps in the vertical process line,
//! specification rows and a frame.  What is drawn is collected as SchemDraw
//! style segments, which [`svg`] writes out and [`snapshot`] lists.

use std::collections::HashMap;

use indexmap::IndexMap;
use serde_json::{Value, json};

use crate::error::{Error, Result};
use crate::expand::GENERATED_PREFIX;
use crate::geometry::{Point, Shape, find_crossings};
use crate::layout::Placed;
use crate::model::Diagram;
use crate::style::{LineStyle, Theme};

pub const PT_PER_UNIT: f64 = 36.0;

/// One drawing segment, as the Python tool hands them to SchemDraw.
#[derive(Clone, Debug, PartialEq)]
pub enum Seg {
    Line {
        path: Vec<Point>,
        color: Option<String>,
        lw: Option<f64>,
        ls: Option<String>,
        arrow: Option<String>,
        arrowwidth: f64,
        arrowlength: f64,
    },
    Text {
        xy: Point,
        text: String,
        align: (String, String),
        fontsize: f64,
        color: Option<String>,
    },
    Poly {
        verts: Vec<Point>,
        closed: bool,
        fill: Option<String>,
        color: Option<String>,
        ls: Option<String>,
    },
    Circle {
        center: Point,
        radius: f64,
        fill: Option<String>,
        color: Option<String>,
        ls: Option<String>,
    },
    Arc {
        center: Point,
        width: f64,
        height: f64,
        theta1: f64,
        theta2: f64,
        color: Option<String>,
        ls: Option<String>,
    },
}

#[derive(Clone, Debug)]
pub struct Group {
    pub kind: &'static str,
    pub id: String,
    pub segs: Vec<Seg>,
}

#[derive(Clone, Debug)]
pub struct Scene {
    pub groups: Vec<Group>,
    pub theme: Theme,
    pub bounds: (f64, f64, f64, f64),
}

#[derive(Clone, Debug, Default)]
struct Style {
    lw: Option<f64>,
    ls: Option<String>,
    arrow: Option<String>,
}

fn stroke(points: Vec<Point>, st: Style) -> Seg {
    let arrowed = st.arrow.is_some();
    Seg::Line {
        path: points,
        color: None,
        lw: st.lw,
        ls: st.ls,
        arrow: st.arrow,
        arrowwidth: if arrowed { 0.2 } else { 0.15 },
        arrowlength: if arrowed { 0.3 } else { 0.25 },
    }
}

fn text(x: f64, y: f64, t: &str, fontsize: f64, ha: &str, va: &str) -> Seg {
    Seg::Text {
        xy: (x, y),
        text: t.into(),
        align: (ha.into(), va.into()),
        fontsize,
        color: None,
    }
}

/// The segment of one outline shape (a piece drawn in a line kind's style
/// becomes a stroke with that style's width and dashes).
fn shape_seg(s: &Shape, styles: Option<&IndexMap<String, LineStyle>>) -> Seg {
    match s {
        Shape::Poly(p) => {
            if let Some(st) = p.line.as_ref().and_then(|l| styles.and_then(|m| m.get(l))) {
                return stroke(
                    p.points.clone(),
                    Style {
                        lw: Some(st.lw),
                        ls: Some(st.ls.clone()),
                        arrow: None,
                    },
                );
            }
            if let Some(fill) = &p.fill {
                return Seg::Poly {
                    verts: p.points.clone(),
                    closed: p.closed,
                    fill: Some(fill.clone()),
                    color: None,
                    ls: None,
                };
            }
            let mut pts = p.points.clone();
            if p.closed {
                pts.push(p.points[0]);
            }
            stroke(pts, Style::default())
        }
        Shape::Ell(e) => {
            if e.fill.is_some() && e.full() && e.rx == e.ry {
                return Seg::Circle {
                    center: (e.cx, e.cy),
                    radius: e.rx,
                    fill: e.fill.clone(),
                    color: None,
                    ls: None,
                };
            }
            Seg::Arc {
                center: (e.cx, e.cy),
                width: 2.0 * e.rx,
                height: 2.0 * e.ry,
                theta1: e.theta1,
                theta2: e.theta2,
                color: None,
                ls: None,
            }
        }
    }
}

/// A segment in a revision's colour (``dashed``: what it took away).
fn paint(seg: Seg, color: Option<&str>, dashed: bool) -> Seg {
    let c = color.map(String::from);
    match seg {
        Seg::Text {
            xy,
            text,
            align,
            fontsize,
            color: own,
        } => Seg::Text {
            xy,
            text,
            align,
            fontsize,
            color: own.or(c),
        },
        Seg::Line {
            path,
            color: own,
            lw,
            ls,
            arrow,
            arrowwidth,
            arrowlength,
        } => Seg::Line {
            path,
            color: c.or(own),
            lw,
            ls: if dashed { Some("--".into()) } else { ls },
            arrow,
            arrowwidth,
            arrowlength,
        },
        Seg::Poly {
            verts,
            closed,
            fill,
            color: own,
            ls,
        } => Seg::Poly {
            verts,
            closed,
            fill: if fill.is_some() { c.clone().or(fill) } else { None },
            color: c.or(own),
            ls: if dashed { Some("--".into()) } else { ls },
        },
        Seg::Circle {
            center,
            radius,
            fill,
            color: own,
            ls,
        } => Seg::Circle {
            center,
            radius,
            fill: if fill.is_some() { c.clone().or(fill) } else { None },
            color: c.or(own),
            ls: if dashed { Some("--".into()) } else { ls },
        },
        Seg::Arc {
            center,
            width,
            height,
            theta1,
            theta2,
            color: own,
            ls,
        } => Seg::Arc {
            center,
            width,
            height,
            theta1,
            theta2,
            color: c.or(own),
            ls: if dashed { Some("--".into()) } else { ls },
        },
    }
}

/// Revision id -> colour of the revisions drawn in colour.
pub fn colors(d: &Diagram, highlight: &str, since: Option<&str>) -> Result<HashMap<String, String>> {
    if !["all", "latest", "none"].contains(&highlight) {
        return Err(Error::Value(format!(
            "highlight must be one of ('all', 'latest', 'none'), got {}",
            crate::pyval::str_repr(highlight)
        )));
    }
    let mut revs: &[crate::revise::RevisionInfo] = &d.revisions;
    if let Some(since) = since.filter(|s| *s != "base") {
        let Some(k) = revs.iter().position(|r| r.id == since) else {
            let mut known = vec!["base".to_string()];
            known.extend(revs.iter().map(|r| r.id.clone()));
            return Err(Error::Value(format!(
                "unknown revision {}; known: {}",
                crate::pyval::str_repr(since),
                crate::pyval::list_repr(&known)
            )));
        };
        revs = &revs[k..];
    }
    if highlight == "none" || revs.is_empty() {
        return Ok(HashMap::new());
    }
    if highlight == "latest" {
        revs = &revs[revs.len() - 1..];
    }
    Ok(revs.iter().map(|r| (r.id.clone(), r.color.clone())).collect())
}

/// Split a route at its crossing marks into line pieces (a gap at each);
/// the last piece carries the arrow.
fn line_strokes(pts: &[Point], marks: &HashMap<usize, Vec<f64>>, style: &LineStyle, r: f64) -> Vec<Seg> {
    let plain = Style {
        lw: Some(style.lw),
        ls: Some(style.ls.clone()),
        arrow: None,
    };
    let mut strokes = Vec::new();
    let mut run: Vec<Point> = vec![pts[0]];
    for (si, w) in pts.windows(2).enumerate() {
        let (p, q) = (w[0], w[1]);
        let horizontal = p.1 == q.1;
        let (pa, qa) = if horizontal { (p.0, q.0) } else { (p.1, q.1) };
        let sign = if qa > pa { 1.0 } else { -1.0 };
        let mut ms: Vec<f64> = marks.get(&si).cloned().unwrap_or_default();
        ms.sort_by(|a, b| a.partial_cmp(b).unwrap());
        if sign < 0.0 {
            ms.reverse();
        }
        for m in ms {
            let (before, after) = if horizontal {
                ((m - sign * r, p.1), (m + sign * r, p.1))
            } else {
                ((p.0, m - sign * r), (p.0, m + sign * r))
            };
            run.push(before);
            strokes.push(stroke(std::mem::take(&mut run), plain.clone()));
            run = vec![after];
        }
        run.push(q);
    }
    let mut last = plain.clone();
    last.arrow = style.arrow.clone();
    strokes.push(stroke(run, last));
    strokes
}

type Pipe = (Point, Point);

/// Equipment tag (underlined) placed according to ``label_pos``.
fn tag_items(p: &Placed, theme: &Theme, busy: &[f64], pipes: &[Pipe]) -> Vec<Seg> {
    let pos = p.geo.label_pos.as_str();
    if pos == "none" {
        return Vec::new();
    }
    let (x0, y0, x1, y1) = p.bbox();
    let (cx, cy) = ((x0 + x1) as f64 / 2.0, (y0 + y1) as f64 / 2.0);
    let em = theme.fontsize / PT_PER_UNIT;
    let width = 0.62 * em * p.id.chars().count() as f64;
    let spot = |w: &str| -> Point {
        match w {
            "above" => (cx, y1 as f64 + 0.5 + em / 2.0),
            "below" => (cx, y0 as f64 - 0.5 - em / 2.0),
            "left" => {
                let hgt = y1 - y0;
                let mut offsets: Vec<i64> = (crate::pyval::floordiv(-hgt, 2) + 1..hgt / 2).collect();
                offsets.sort_by_key(|o| o.abs());
                let y = offsets
                    .iter()
                    .map(|&o| cy + o as f64)
                    .find(|&y| busy.iter().all(|b| (y - b).abs() >= 1.0))
                    .unwrap_or(cy);
                (x0 as f64 - 0.5 - width / 2.0, y)
            }
            "right" => (x1 as f64 + 0.5 + width / 2.0, cy),
            _ => (cx, cy),
        }
    };
    let clear = |at: Point| -> bool {
        let (tx, ty) = at;
        let (bx0, bx1) = (tx - width / 2.0 - 0.1, tx + width / 2.0 + 0.1);
        let (by0, by1) = (ty - 0.75 * em, ty + em / 2.0);
        !pipes
            .iter()
            .any(|(a, b)| a.0.min(b.0) <= bx1 && a.0.max(b.0) >= bx0 && a.1.min(b.1) <= by1 && a.1.max(b.1) >= by0)
    };
    let (mut x, mut y) = spot(pos);
    if pos != "center" && !clear((x, y)) {
        let mut spots: Vec<Point> = ["left", "above", "below", "right"]
            .iter()
            .filter(|&&o| o != pos)
            .map(|o| spot(o))
            .collect();
        for w in ["above", "below"] {
            let (sx, sy) = spot(w);
            for dx in [-0.5, 0.5, -1.0, 1.0] {
                spots.push((sx + dx, sy));
            }
        }
        if let Some(s) = spots.into_iter().find(|&s| clear(s)) {
            (x, y) = s;
        }
    }
    let mut items = vec![text(x, y, &p.id, theme.fontsize, "center", "center")];
    if theme.tag_underline {
        let under = y - 0.62 * em;
        items.push(stroke(
            vec![(x - width / 2.0, under), (x + width / 2.0, under)],
            Style {
                lw: Some(0.8),
                ..Default::default()
            },
        ));
    }
    items
}

/// Rough width of a text block: full-width (CJK) characters take one em,
/// others a bit over half.
fn text_width(t: &str, em: f64) -> f64 {
    t.split('\n')
        .map(|line| {
            line.chars()
                .fold(0.0, |s, ch| s + if ch as u32 > 0x2E7F { 1.0 } else { 0.6 })
                * em
        })
        .fold(f64::NEG_INFINITY, f64::max)
}

/// Spec texts of one row, each centred over its item where possible.
fn spread(entries: &[(f64, String)], em: f64) -> Vec<(f64, String)> {
    let mut sorted: Vec<(f64, String)> = entries.to_vec();
    sorted.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
    let mut out = Vec::new();
    let mut right: Option<f64> = None;
    for (mut cx, t) in sorted {
        let half = text_width(&t, em) / 2.0;
        if let Some(r) = right {
            if cx - half < r + em {
                cx = r + em + half;
            }
        }
        right = Some(cx + half);
        out.push((cx, t));
    }
    out
}

/// Short flange line across a pipe, just outside the equipment outline.
fn nozzle_tick(a: Point, b: Point) -> Seg {
    let sgn = |v: f64, w: f64| ((v > w) as i64 - (v < w) as i64) as f64;
    let (dx, dy) = (sgn(b.0, a.0), sgn(b.1, a.1));
    let (cx, cy) = (a.0 + 0.45 * dx, a.1 + 0.45 * dy);
    stroke(
        vec![(cx - 0.3 * dy, cy - 0.3 * dx), (cx + 0.3 * dy, cy + 0.3 * dx)],
        Style::default(),
    )
}

/// Everything drawn on the sheet, in drawing order.
pub fn scene(d: &Diagram, label: bool, fontsize: Option<f64>, frame: bool, theme: Option<Theme>) -> Result<Scene> {
    scene_with(d, label, fontsize, frame, theme, "all", None)
}

/// [`scene`] with the revisions drawn in colour chosen: ``highlight`` (all,
/// latest or none) from revision ``since`` on.
pub fn scene_with(
    d: &Diagram,
    label: bool,
    fontsize: Option<f64>,
    frame: bool,
    theme: Option<Theme>,
    highlight: &str,
    since: Option<&str>,
) -> Result<Scene> {
    let colors = colors(d, highlight, since)?;
    let color_of = |key: &str| d.added.get(key).and_then(|r| colors.get(r)).map(String::as_str);
    let mut theme = theme.unwrap_or_default();
    if let Some(f) = fontsize {
        theme.fontsize = f;
    }
    let styles = &d.line_styles;
    let mut missing: Vec<&String> = d
        .routes
        .iter()
        .map(|(c, _)| &c.kind)
        .filter(|k| !styles.contains_key(*k))
        .collect();
    if !missing.is_empty() {
        missing.sort();
        missing.dedup();
        return Err(Error::Value(format!(
            "no line style for kinds {}",
            crate::pyval::list_repr(&missing)
        )));
    }
    let mut groups = Vec::new();
    let by_id: HashMap<&str, &Placed> = d.placed.iter().map(|p| (p.id.as_str(), p)).collect();
    let mut west: HashMap<&str, Vec<f64>> = HashMap::new();
    for (c, r) in &d.routes {
        let ends = c.ends();
        for ((eq, _), pt) in ends.iter().zip([r.points[0], *r.points.last().unwrap()]) {
            if pt.0 <= by_id[eq].x as f64 {
                west.entry(eq).or_default().push(pt.1);
            }
        }
    }
    let pipes: Vec<Pipe> = d
        .routes
        .iter()
        .flat_map(|(_, r)| r.points.windows(2).map(|w| (w[0], w[1])))
        .collect();

    for p in &d.placed {
        let mut items: Vec<Seg> = p
            .geo
            .translated(p.x as f64, p.y as f64)
            .iter()
            .map(|s| shape_seg(s, Some(styles)))
            .collect();
        for (x, y, t, scale) in &p.geo.texts {
            if !t.is_empty() {
                items.push(text(
                    p.x as f64 + x,
                    p.y as f64 + y,
                    t,
                    theme.fontsize * scale,
                    "center",
                    "center",
                ));
            }
        }
        if label && !p.id.starts_with(GENERATED_PREFIX) {
            let busy = west.get(p.id.as_str()).cloned().unwrap_or_default();
            items.extend(tag_items(p, &theme, &busy, &pipes));
        }
        if !items.is_empty() {
            let c = color_of(&p.id);
            let segs = items.into_iter().map(|g| paint(g, c, false)).collect();
            groups.push(Group {
                kind: "equipment",
                id: p.id.clone(),
                segs,
            });
        }
    }

    let paths: Vec<&[Point]> = d.routes.iter().map(|(_, r)| r.points.as_slice()).collect();
    let mut marks: Vec<HashMap<usize, Vec<f64>>> = vec![HashMap::new(); paths.len()];
    for (_, _, vr, vs, (_, y)) in find_crossings(&paths) {
        if styles[&d.routes[vr].0.kind].process {
            marks[vr].entry(vs).or_default().push(y);
        }
    }
    for (((c, r), pts), m) in d.routes.iter().zip(&paths).zip(&marks) {
        let style = &styles[&c.kind];
        let mut line = line_strokes(pts, m, style, theme.crossing_gap);
        if style.process && theme.nozzle_ticks {
            let [(src, _), (dst, _)] = c.ends();
            if by_id[src].geo.nozzles {
                line.push(nozzle_tick(pts[0], pts[1]));
            }
            if by_id[dst].geo.nozzles {
                line.push(nozzle_tick(pts[pts.len() - 1], pts[pts.len() - 2]));
            }
        }
        let _ = r;
        let key = format!("{} -> {}", c.src, c.dst);
        let col = color_of(&key);
        let segs = line.into_iter().map(|g| paint(g, col, false)).collect();
        groups.push(Group {
            kind: "line",
            id: key,
            segs,
        });
    }

    // what the coloured revisions took away
    let in_place: std::collections::HashSet<(&str, &str)> = d
        .replaced
        .iter()
        .map(|(r, _, old)| (r.as_str(), old.as_str()))
        .collect();
    for g in &d.ghosts {
        let Some(color) = colors.get(&g.rev) else { continue };
        if let Some(p) = &g.placed {
            if in_place.contains(&(g.rev.as_str(), p.id.as_str())) {
                continue;
            }
            let mut items: Vec<Seg> = p
                .geo
                .translated(p.x as f64, p.y as f64)
                .iter()
                .map(|s| shape_seg(s, None))
                .collect();
            for (x, y, t, scale) in &p.geo.texts {
                if !t.is_empty() {
                    items.push(text(
                        p.x as f64 + x,
                        p.y as f64 + y,
                        t,
                        theme.fontsize * scale,
                        "center",
                        "center",
                    ));
                }
            }
            if label && !p.id.starts_with(GENERATED_PREFIX) {
                items.extend(tag_items(p, &theme, &[], &pipes));
            }
            if !items.is_empty() {
                let segs = items.into_iter().map(|s| paint(s, Some(color), true)).collect();
                groups.push(Group {
                    kind: "equipment",
                    id: format!("{} (removed)", p.id),
                    segs,
                });
            }
        } else if let Some((c, r)) = &g.line {
            let strokes = line_strokes(&r.points, &HashMap::new(), &styles[&c.kind], 0.0);
            let segs = strokes.into_iter().map(|s| paint(s, Some(color), true)).collect();
            groups.push(Group {
                kind: "line",
                id: format!("{} -> {} (removed)", c.src, c.dst),
                segs,
            });
        }
    }

    // sheet: specification rows and border
    let mut xs: Vec<f64> = Vec::new();
    let mut ys: Vec<f64> = Vec::new();
    for p in &d.placed {
        let b = p.bbox();
        xs.push(b.0 as f64);
        xs.push(b.2 as f64);
        ys.push(b.1 as f64);
        ys.push(b.3 as f64);
    }
    for pts in &paths {
        for &(x, y) in pts.iter() {
            xs.push(x);
            ys.push(y);
        }
    }
    for g in &d.ghosts {
        if let Some(p) = &g.placed {
            if colors.contains_key(&g.rev) && !in_place.contains(&(g.rev.as_str(), p.id.as_str())) {
                let b = p.bbox();
                xs.push(b.0 as f64);
                xs.push(b.2 as f64);
                ys.push(b.1 as f64);
                ys.push(b.3 as f64);
            }
        }
    }
    let fmin = |v: &[f64]| v.iter().cloned().fold(f64::INFINITY, f64::min);
    let fmax = |v: &[f64]| v.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    let mg = theme.margin;
    let (mut x0, mut x1, mut y0, mut y1) = (fmin(&xs) - mg, fmax(&xs) + mg, fmin(&ys) - mg, fmax(&ys) + mg);
    let em = theme.fontsize / PT_PER_UNIT;
    let mut sheet: Vec<Seg> = Vec::new();
    let mut rows = [0i64, 0i64]; // [not rotating, rotating]
    let bands: Vec<(f64, f64)> = if d.rows.is_empty() {
        vec![(y0 + mg, y1 - mg)]
    } else {
        d.rows.clone()
    };
    let last = bands.len() - 1;
    let mut specs: IndexMap<(usize, bool), Vec<(f64, String, Option<String>)>> = IndexMap::new();
    for p in &d.placed {
        let Some(desc) = d.descs.get(&p.id) else { continue };
        if !label || desc.is_empty() {
            continue;
        }
        let t = format!("{}\n{}", p.id, desc);
        let mid = p.y as f64 + p.geo.height as f64 / 2.0;
        let mut band = 0;
        let mut best = f64::INFINITY;
        for (k, b) in bands.iter().enumerate() {
            let dist = ((b.0 + b.1) / 2.0 - mid).abs();
            if dist < best {
                best = dist;
                band = k;
            }
        }
        let rot = p.geo.rotating;
        if (band, rot) == (0, false) || (band, rot) == (last, true) {
            let r = &mut rows[rot as usize];
            *r = (*r).max(t.matches('\n').count() as i64 + 1);
        }
        let b = p.bbox();
        let col = color_of(&p.id).map(String::from);
        specs
            .entry((band, rot))
            .or_default()
            .push(((b.0 + b.2) as f64 / 2.0, t, col));
    }
    let mut keys: Vec<(usize, bool)> = specs.keys().copied().collect();
    keys.sort_by_key(|&(band, rot)| (rot, band));
    for (band, rotating) in keys {
        let entries = &specs[&(band, rotating)];
        let va = if rotating { "top" } else { "bottom" };
        let (lo, hi) = bands[band];
        let y = if rotating {
            if band == last { y0 } else { lo - mg }
        } else if band == 0 {
            y1
        } else {
            hi + mg
        };
        let mut order: Vec<usize> = (0..entries.len()).collect();
        order.sort_by(|&a, &b| entries[a].0.partial_cmp(&entries[b].0).unwrap());
        let plain: Vec<(f64, String)> = entries.iter().map(|e| (e.0, e.1.clone())).collect();
        for (k, (cx, t)) in order.into_iter().zip(spread(&plain, em)) {
            x0 = x0.min(cx - text_width(&t, em) / 2.0 - 1.0);
            x1 = x1.max(cx + text_width(&t, em) / 2.0 + 1.0);
            sheet.push(paint(
                text(cx, y, &t, theme.fontsize, "center", va),
                entries[k].2.as_deref(),
                false,
            ));
        }
    }
    let pitch = theme.spec_line_height * em;
    y0 -= rows[1] as f64 * pitch + if rows[1] != 0 { 1.0 } else { 0.0 };
    y1 += rows[0] as f64 * pitch + if rows[0] != 0 { 1.0 } else { 0.0 };
    let legend: Vec<&crate::revise::RevisionInfo> = d.revisions.iter().filter(|r| colors.contains_key(&r.id)).collect();
    if !legend.is_empty() {
        let mut y = y0;
        let changed: std::collections::HashSet<(&str, &str)> =
            d.changed.iter().map(|(r, i)| (r.as_str(), i.as_str())).collect();
        for r in legend {
            let mut swaps: Vec<String> = d
                .replaced
                .iter()
                .filter(|(rev, new, _)| *rev == r.id && !changed.contains(&(rev.as_str(), new.as_str())))
                .map(|(_, new, old)| {
                    if new != old {
                        format!("{old} → {new}")
                    } else {
                        format!("{new} 取替")
                    }
                })
                .collect();
            swaps.extend(
                d.changed
                    .iter()
                    .filter(|(rev, _)| *rev == r.id)
                    .map(|(_, i)| format!("{i} 変更")),
            );
            let swaps = swaps.join(", ");
            let mut t = [r.id.as_str(), r.note.as_str()]
                .iter()
                .filter(|t| !t.is_empty())
                .copied()
                .collect::<Vec<_>>()
                .join(": ");
            if !swaps.is_empty() {
                t.push_str(&format!(" ({swaps})"));
            }
            sheet.push(paint(
                text(x0 + 1.0, y, &t, theme.fontsize, "left", "top"),
                Some(&r.color),
                false,
            ));
            x1 = x1.max(x0 + 2.0 + text_width(&t, em));
            y -= pitch;
        }
        y0 = y - 0.5;
    }
    if frame {
        let corners = vec![(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)];
        sheet.push(stroke(
            corners,
            Style {
                lw: Some(theme.frame_lw),
                ..Default::default()
            },
        ));
    }
    if !sheet.is_empty() {
        groups.push(Group {
            kind: "sheet",
            id: "sheet".into(),
            segs: sheet,
        });
    }
    Ok(Scene {
        groups,
        theme,
        bounds: (x0, y0, x1, y1),
    })
}

fn opt_str(v: &Option<String>) -> Value {
    v.clone().map(Value::from).unwrap_or(Value::Null)
}

fn opt_f(v: Option<f64>) -> Value {
    v.map(Value::from).unwrap_or(Value::Null)
}

fn pts(v: &[Point]) -> Value {
    Value::Array(v.iter().map(|&(x, y)| json!([x, y])).collect())
}

/// One segment as the Python snapshot lists it: ``[type, attributes]``.
pub fn seg_json(s: &Seg) -> Value {
    match s {
        Seg::Line {
            path,
            color,
            lw,
            ls,
            arrow,
            arrowwidth,
            arrowlength,
        } => json!(["Segment", {
            "arrow": opt_str(arrow), "arrowlength": arrowlength, "arrowwidth": arrowwidth,
            "capstyle": null, "clip": null, "color": opt_str(color), "fill": null,
            "joinstyle": null, "ls": opt_str(ls), "lw": opt_f(*lw), "path": pts(path),
            "visible": true, "zorder": null}]),
        Seg::Text {
            xy,
            text,
            align,
            fontsize,
            color,
        } => json!(["SegmentText", {
            "align": [align.0, align.1], "bgcolor": null, "clip": null, "color": opt_str(color),
            "decoration": null, "font": null, "fontsize": fontsize, "href": null, "mathfont": null,
            "rotation": null, "rotation_global": true, "rotation_mode": null, "text": text,
            "visible": true, "xy": [xy.0, xy.1], "zorder": null}]),
        Seg::Poly {
            verts,
            closed,
            fill,
            color,
            ls,
        } => json!(["SegmentPoly", {
            "capstyle": null, "clip": null, "closed": closed, "color": opt_str(color),
            "cornerradius": 0, "fill": opt_str(fill), "hatch": false, "joinstyle": null,
            "ls": opt_str(ls), "lw": null, "verts": pts(verts), "visible": true, "zorder": null}]),
        Seg::Circle {
            center,
            radius,
            fill,
            color,
            ls,
        } => json!(["SegmentCircle", {
            "center": [center.0, center.1], "clip": null, "color": opt_str(color), "endref": null,
            "fill": opt_str(fill), "ls": opt_str(ls), "lw": null, "radius": radius,
            "visible": true, "zorder": null}]),
        Seg::Arc {
            center,
            width,
            height,
            theta1,
            theta2,
            color,
            ls,
        } => json!(["SegmentArc", {
            "angle": 0, "arrow": null, "arrowlength": 0.25, "arrowwidth": 0.15,
            "center": [center.0, center.1], "clip": null, "color": opt_str(color), "fill": null,
            "height": height, "ls": opt_str(ls), "lw": null, "theta1": theta1, "theta2": theta2,
            "visible": true, "width": width, "zorder": null}]),
    }
}

/// Placement, routes, warnings and drawing of ``d`` as the Python snapshot
/// tests list them (``tests/snapshots``).
pub fn snapshot(d: &Diagram) -> Result<Value> {
    snapshot_with(d, "all", None)
}

/// [`snapshot`] with the revisions drawn in colour chosen (see [`scene_with`]).
pub fn snapshot_with(d: &Diagram, highlight: &str, since: Option<&str>) -> Result<Value> {
    let sc = scene_with(d, true, None, true, None, highlight, since)?;
    Ok(json!({
        "placed": d.placed.iter().map(|p| json!([p.id, p.x, p.y, p.geo.width, p.geo.height])).collect::<Vec<_>>(),
        "routes": d.routes.iter().map(|(c, r)| json!([c.src, c.dst, c.kind, pts(&r.points)])).collect::<Vec<_>>(),
        "warnings": d.warnings,
        "drawing": sc.groups.iter().map(|g| Value::Array(g.segs.iter().map(seg_json).collect())).collect::<Vec<_>>(),
    }))
}
