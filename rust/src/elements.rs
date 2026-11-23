//! Element (equipment) definitions and the element library.
//!
//! An element turns user parameters into a [`Geometry`]: a bounding box on
//! the grid, outline shapes and named ports.  Every element accepts the
//! common parameters ``rotation`` (0/90/180/270, counter-clockwise) and
//! ``mirror`` (left-right flip, applied before rotating).

use std::sync::{Arc, OnceLock};

use indexmap::IndexMap;
use serde_json::Value;

use crate::error::{Error, Result};
use crate::geometry::{Ellipse, Point, Polyline, Port, Shape, normal, outline_distance, ray_hit, rect};
use crate::pyval::{self, Params};

/// ``(x, y, text, size relative to the tag font)``
pub type Text = (f64, f64, String, f64);
pub const LABEL_POSITIONS: [&str; 5] = ["center", "above", "below", "left", "none"];
pub const ROTATIONS: [i64; 4] = [0, 90, 180, 270];

/// Where a line meets a port, relative to the element's corner: the exact
/// point on the outline, the point on the bounding box, the outward normal
/// and the direction (index into ``router::DIRS``) leaving the element.
#[derive(Clone, Copy, Debug)]
pub struct PortEnd {
    pub point: Point,
    pub edge: (i64, i64),
    pub normal: (i64, i64),
    pub out_dir: usize,
}

#[derive(Debug, Default)]
pub struct Geometry {
    pub width: i64,
    pub height: i64,
    pub shapes: Vec<Shape>,
    pub ports: IndexMap<String, Port>,
    pub label_pos: String,
    pub texts: Vec<Text>,
    pub nozzles: bool,
    pub rotating: bool,
    pub off_sheet: bool,
    pub beside: bool,
    pub swappable: Vec<Vec<String>>,
    /// what it was built from -- (type, parameters, extra ports) -- so a
    /// laid out sheet can be written down and read back (see ``freeze``)
    pub recipe: Option<(String, Params, serde_json::Map<String, Value>)>,
    port_ends: OnceLock<IndexMap<String, std::result::Result<PortEnd, String>>>,
}

impl Clone for Geometry {
    fn clone(&self) -> Geometry {
        Geometry {
            width: self.width,
            height: self.height,
            shapes: self.shapes.clone(),
            ports: self.ports.clone(),
            label_pos: self.label_pos.clone(),
            texts: self.texts.clone(),
            nozzles: self.nozzles,
            rotating: self.rotating,
            off_sheet: self.off_sheet,
            beside: self.beside,
            swappable: self.swappable.clone(),
            recipe: self.recipe.clone(),
            port_ends: OnceLock::new(),
        }
    }
}

impl Geometry {
    pub fn new(width: i64, height: i64, shapes: Vec<Shape>, ports: IndexMap<String, Port>) -> Geometry {
        Geometry {
            width,
            height,
            shapes,
            ports,
            label_pos: "center".into(),
            ..Default::default()
        }
    }

    pub fn label(mut self, pos: &str) -> Geometry {
        self.label_pos = pos.into();
        self
    }

    pub fn translated(&self, dx: f64, dy: f64) -> Vec<Shape> {
        self.shapes.iter().map(|s| s.shifted(dx, dy)).collect()
    }

    /// Where lines meet the port (see the Python ``router._port_ends``);
    /// worked out once per geometry.  The error is the message after the
    /// item's id.
    pub fn port_end(&self, name: &str) -> Option<std::result::Result<PortEnd, String>> {
        let ends = self.port_ends.get_or_init(|| {
            self.ports
                .iter()
                .map(|(n, p)| (n.clone(), self.compute_port_end(n, p)))
                .collect()
        });
        ends.get(name).cloned()
    }

    fn compute_port_end(&self, name: &str, port: &Port) -> std::result::Result<PortEnd, String> {
        let (w, h, off) = (self.width, self.height, port.offset);
        let span = if "WE".contains(port.side) { h } else { w };
        if !(0 <= off && off <= span) {
            return Err(format!("{name}: offset {off} outside 0..{span}"));
        }
        let (nx, ny) = normal(port.side);
        let edge = match port.side {
            'W' => (0, off),
            'E' => (w, off),
            'S' => (off, 0),
            _ => (off, h),
        };
        let origin = ((edge.0 + nx) as f64, (edge.1 + ny) as f64);
        let t = ray_hit(&self.shapes, origin, (-nx as f64, -ny as f64))
            .ok_or_else(|| format!("{name}: no outline found for this port"))?;
        let point = (origin.0 - nx as f64 * t, origin.1 - ny as f64 * t);
        let out_dir = match (nx, ny) {
            (0, 1) => 0,
            (1, 0) => 1,
            (0, -1) => 2,
            _ => 3,
        };
        Ok(PortEnd {
            point,
            edge,
            normal: (nx, ny),
            out_dir,
        })
    }
}

fn ports(list: &[(&str, char, i64)]) -> IndexMap<String, Port> {
    list.iter().map(|&(n, s, o)| (n.to_string(), Port::new(s, o))).collect()
}

/// Rotate a geometry by 90 degrees counter-clockwise (W->S, E->N, N->W, S->E).
pub fn rotate_ccw(geo: &Geometry) -> Geometry {
    let h = geo.height as f64;
    let pt = |x: f64, y: f64| (h - y, x);
    let shapes = geo
        .shapes
        .iter()
        .map(|s| match s {
            Shape::Poly(p) => Shape::Poly(Polyline {
                points: p.points.iter().map(|&(x, y)| pt(x, y)).collect(),
                ..p.clone()
            }),
            Shape::Ell(e) => {
                let (cx, cy) = pt(e.cx, e.cy);
                Shape::Ell(Ellipse {
                    cx,
                    cy,
                    rx: e.ry,
                    ry: e.rx,
                    theta1: e.theta1 + 90.0,
                    theta2: e.theta2 + 90.0,
                    ..e.clone()
                })
            }
        })
        .collect();
    let turn = |s: char| match s {
        'W' => 'S',
        'E' => 'N',
        'N' => 'W',
        _ => 'E',
    };
    let ports = geo
        .ports
        .iter()
        .map(|(n, p)| {
            let off = if "WE".contains(p.side) {
                geo.height - p.offset
            } else {
                p.offset
            };
            (n.clone(), Port::new(turn(p.side), off))
        })
        .collect();
    let texts = geo
        .texts
        .iter()
        .map(|(x, y, t, s)| {
            let (a, b) = pt(*x, *y);
            (a, b, t.clone(), *s)
        })
        .collect();
    Geometry {
        width: geo.height,
        height: geo.width,
        shapes,
        ports,
        texts,
        ..geo.clone()
    }
}

/// Mirror a geometry left-right (W <-> E).
pub fn mirror_x(geo: &Geometry) -> Geometry {
    let w = geo.width as f64;
    let shapes = geo
        .shapes
        .iter()
        .map(|s| match s {
            Shape::Poly(p) => Shape::Poly(Polyline {
                points: p.points.iter().map(|&(x, y)| (w - x, y)).collect(),
                ..p.clone()
            }),
            Shape::Ell(e) => Shape::Ell(Ellipse {
                cx: w - e.cx,
                theta1: 180.0 - e.theta2,
                theta2: 180.0 - e.theta1,
                ..e.clone()
            }),
        })
        .collect();
    let flip = |s: char| match s {
        'W' => 'E',
        'E' => 'W',
        c => c,
    };
    let ports = geo
        .ports
        .iter()
        .map(|(n, p)| {
            let off = if "NS".contains(p.side) {
                geo.width - p.offset
            } else {
                p.offset
            };
            (n.clone(), Port::new(flip(p.side), off))
        })
        .collect();
    let texts = geo
        .texts
        .iter()
        .map(|(x, y, t, s)| (w - x, *y, t.clone(), *s))
        .collect();
    Geometry {
        shapes,
        ports,
        texts,
        ..geo.clone()
    }
}

/// Common placement transform: mirror left-right first, then rotate CCW.
pub fn transform(geo: Geometry, rotation: Option<&Value>, mirror: Option<&Value>) -> Result<Geometry> {
    let rot = match rotation {
        Some(v) => pyval::int(v)?,
        None => 0,
    };
    if !ROTATIONS.contains(&rot) {
        return Err(Error::Value(format!(
            "rotation must be one of (0, 90, 180, 270), got {}",
            pyval::repr(rotation.unwrap())
        )));
    }
    let mut geo = geo;
    if mirror.is_some_and(pyval::truthy) {
        geo = mirror_x(&geo);
    }
    for _ in 0..rot / 90 {
        geo = rotate_ccw(&geo);
    }
    Ok(geo)
}

/// Validate a size that must be an even number of grid cells.
pub fn even(value: &Value, minimum: i64, what: &str) -> Result<i64> {
    let v = pyval::int(value)?;
    if v < minimum || v % 2 != 0 {
        return Err(Error::Value(format!(
            "{what} must be an even number >= {minimum}, got {}",
            pyval::str(value)
        )));
    }
    Ok(v)
}

/// ``w<i>`` / ``e<i>`` on the given grid rows of a vessel's shell, with the
/// ``w_low``, ``w_mid``, ``w_high`` (and ``e_...``) aliases.
pub fn shell_ports(rows: &[i64]) -> IndexMap<String, Port> {
    let mut ports = IndexMap::new();
    for (i, &y) in rows.iter().enumerate() {
        ports.insert(format!("w{i}"), Port::new('W', y));
        ports.insert(format!("e{i}"), Port::new('E', y));
    }
    for side in ['w', 'e'] {
        let low = ports[&format!("{side}0")];
        let mid = ports[&format!("{side}{}", rows.len() / 2)];
        let high = ports[&format!("{side}{}", rows.len() - 1)];
        ports.insert(format!("{side}_low"), low);
        ports.insert(format!("{side}_mid"), mid);
        ports.insert(format!("{side}_high"), high);
    }
    ports
}

/// Accept ``{"side": "N", "offset": 1}`` or ``["N", 1]``.
pub fn parse_port(v: &Value) -> Result<Port> {
    let (side, offset) = match v {
        Value::Object(o) => (
            o.get("side").ok_or_else(|| Error::Key("side".into()))?,
            o.get("offset").ok_or_else(|| Error::Key("offset".into()))?,
        ),
        Value::Array(a) if a.len() == 2 => (&a[0], &a[1]),
        _ => return Err(Error::Value(format!("bad port {}", pyval::repr(v)))),
    };
    let s = match side {
        Value::String(s) if ["N", "S", "E", "W"].contains(&s.as_str()) => s.chars().next().unwrap(),
        _ => {
            return Err(Error::Value(format!(
                "port side must be N/S/E/W, got {}",
                pyval::repr(side)
            )));
        }
    };
    let f = pyval::float(offset)?;
    if f.trunc() != f {
        return Err(Error::Value(format!(
            "port offset must be on the grid (integer), got {}",
            pyval::repr(offset)
        )));
    }
    Ok(Port::new(s, f as i64))
}

fn get<'a>(params: &'a Params, key: &str) -> Option<&'a Value> {
    params.get(key)
}

fn get_int(params: &Params, key: &str, default: i64) -> Result<i64> {
    match params.get(key) {
        Some(v) => pyval::int(v),
        None => Ok(default),
    }
}

fn get_even(params: &Params, key: &str, default: i64, minimum: i64, what: &str) -> Result<i64> {
    match params.get(key) {
        Some(v) => even(v, minimum, what),
        None => even(&Value::from(default), minimum, what),
    }
}

fn get_str(params: &Params, key: &str, default: &str) -> String {
    params.get(key).map(pyval::str).unwrap_or_else(|| default.to_string())
}

/// ``params.get(key, default) == value`` for a text value.
fn is(params: &Params, key: &str, default: &str, value: &str) -> bool {
    match params.get(key) {
        Some(Value::String(s)) => s == value,
        Some(_) => false,
        None => default == value,
    }
}

// -- the standard elements ------------------------------------------------------

pub const STANDARD: [&str; 27] = [
    "pump",
    "compressor",
    "blower",
    "centrifuge",
    "valve",
    "safety_valve",
    "strainer",
    "steam_trap",
    "orifice",
    "static_mixer",
    "ejector",
    "vertical_drum",
    "column",
    "reactor",
    "tank",
    "horizontal_drum",
    "cyclone",
    "heat_exchanger",
    "plate_heat_exchanger",
    "air_cooler",
    "furnace",
    "filter_housing",
    "header",
    "tee",
    "connector",
    "utility",
    "instrument",
];

pub const VALVE_KINDS: [&str; 7] = ["gate", "globe", "ball", "butterfly", "three_way", "control", "check"];

fn pl(points: &[Point]) -> Polyline {
    Polyline::new(points.to_vec())
}

fn deco(points: &[Point]) -> Shape {
    pl(points).boundary(false).into()
}

fn pump(_: &Params) -> Result<Geometry> {
    let shapes = vec![
        Ellipse::new(1.0, 2.0, 1.0, 1.0).into(),
        pl(&[(2.0, 2.0), (2.0, 3.0)]).into(),
        deco(&[(0.0, 2.0), (1.2, 2.0)]),
        deco(&[(0.8, 2.2), (1.2, 2.0), (0.8, 1.8)]),
        deco(&[(0.5, 1.13), (0.1, 0.3), (1.9, 0.3), (1.5, 1.13)]),
    ];
    let mut g = Geometry::new(2, 3, shapes, ports(&[("in", 'W', 2), ("out", 'N', 2)])).label("below");
    g.rotating = true;
    Ok(g)
}

fn compressor(_: &Params) -> Result<Geometry> {
    let shapes = vec![
        pl(&[(0.0, 0.0), (4.0, 1.0), (4.0, 3.0), (0.0, 4.0)])
            .closed(true)
            .into(),
    ];
    let mut g = Geometry::new(4, 4, shapes, ports(&[("in", 'W', 2), ("out", 'E', 2)])).label("below");
    g.rotating = true;
    Ok(g)
}

fn blower(_: &Params) -> Result<Geometry> {
    const R: f64 = 1.5;
    let c = 2.0;
    let mut shapes: Vec<Shape> = vec![
        Ellipse::new(c, c, R, R).into(),
        deco(&[(c, c + R), (c + R, c + 0.35)]),
        deco(&[(c, c - R), (c + R, c - 0.35)]),
    ];
    for k in 0..3 {
        let a = ((90 + 120 * k) as f64).to_radians();
        let tip = (c + 0.75 * a.cos(), c + 0.75 * a.sin());
        shapes.push(deco(&[(c, c), tip]));
    }
    let mut g = Geometry::new(4, 4, shapes, ports(&[("in", 'W', 2), ("out", 'E', 2)])).label("below");
    g.rotating = true;
    Ok(g)
}

fn centrifuge(_: &Params) -> Result<Geometry> {
    let shapes = vec![
        rect(0.0, 1.0, 4.0, 4.0, true).into(),
        deco(&[(0.6, 3.6), (1.0, 1.5), (3.0, 1.5), (3.4, 3.6)]),
        deco(&[(2.0, 0.6), (2.0, 1.5)]),
        rect(1.4, 0.0, 2.6, 0.6, true).into(),
    ];
    let p = ports(&[("in", 'N', 2), ("out", 'E', 2), ("solids", 'S', 1)]);
    let mut g = Geometry::new(4, 4, shapes, p).label("below");
    g.rotating = true;
    Ok(g)
}

fn valve(params: &Params) -> Result<Geometry> {
    let kind_v = get(params, "kind").cloned().unwrap_or(Value::from("gate"));
    let kind = match &kind_v {
        Value::String(s) if VALVE_KINDS.contains(&s.as_str()) => s.clone(),
        _ => {
            return Err(Error::Value(format!(
                "valve kind must be one of ('gate', 'globe', 'ball', 'butterfly', 'three_way', 'control', 'check'), got {}",
                pyval::repr(&kind_v)
            )));
        }
    };
    let mut p = ports(&[("in", 'W', 1), ("out", 'E', 1)]);
    let mut shapes: Vec<Shape> = match kind.as_str() {
        "check" => vec![pl(&[(0.0, 1.0), (0.6, 1.0), (0.6, 1.5), (1.4, 0.5), (1.4, 1.0), (2.0, 1.0)]).into()],
        "butterfly" => vec![
            rect(0.0, 0.5, 2.0, 1.5, true).into(),
            deco(&[(0.0, 0.5), (2.0, 1.5)]),
            Ellipse::new(1.0, 1.0, 0.16, 0.16).boundary(false).fill("black").into(),
        ],
        "ball" => vec![
            pl(&[(0.0, 0.5), (0.0, 1.5), (0.7, 1.0)]).closed(true).into(),
            pl(&[(2.0, 0.5), (2.0, 1.5), (1.3, 1.0)]).closed(true).into(),
            Ellipse::new(1.0, 1.0, 0.3, 0.3).boundary(false).into(),
        ],
        _ => vec![
            pl(&[(0.0, 0.5), (0.0, 1.5), (2.0, 0.5), (2.0, 1.5)])
                .closed(true)
                .into(),
        ],
    };
    if kind == "globe" {
        shapes.push(Ellipse::new(1.0, 1.0, 0.22, 0.22).boundary(false).fill("black").into());
    }
    if kind == "three_way" {
        shapes.push(pl(&[(0.5, 0.0), (1.5, 0.0), (1.0, 1.0)]).closed(true).into());
        p.insert("branch".into(), Port::new('S', 1));
    }
    if kind == "control" {
        shapes.push(deco(&[(1.0, 1.0), (1.0, 1.6)]));
        shapes.push(pl(&[(0.5, 1.6), (1.5, 1.6)]).into());
        shapes.push(Ellipse::arc(1.0, 1.6, 0.5, 0.4, 0.0, 180.0).into());
        p.insert("signal".into(), Port::new('N', 1));
    }
    let mut g = Geometry::new(2, 2, shapes, p).label("none");
    if is(params, "orientation", "horizontal", "vertical") {
        g = rotate_ccw(&g);
    }
    Ok(g)
}

fn safety_valve(_: &Params) -> Result<Geometry> {
    let mut spring: Vec<Point> = vec![(1.0, 1.0)];
    for k in 0..4 {
        spring.push((if k % 2 == 1 { 0.65 } else { 1.35 }, 1.35 + 0.3 * k as f64));
    }
    spring.push((1.0, 2.6));
    let shapes = vec![
        pl(&[(0.5, 0.0), (1.5, 0.0), (1.0, 1.0)]).closed(true).into(),
        pl(&[(2.0, 0.5), (2.0, 1.5), (1.0, 1.0)]).closed(true).into(),
        deco(&spring),
        deco(&[(0.6, 2.6), (1.4, 2.6)]),
    ];
    Ok(Geometry::new(2, 3, shapes, ports(&[("in", 'S', 1), ("out", 'E', 1)])).label("left"))
}

fn strainer(_: &Params) -> Result<Geometry> {
    let shapes = vec![
        pl(&[(0.0, 1.0), (2.0, 1.0)]).pipe().into(),
        deco(&[(0.6, 1.0), (1.4, 0.2), (1.8, 0.6), (1.0, 1.4)]),
    ];
    Ok(Geometry::new(2, 2, shapes, ports(&[("in", 'W', 1), ("out", 'E', 1)])).label("none"))
}

fn steam_trap(_: &Params) -> Result<Geometry> {
    const R: f64 = 0.6;
    let mut half = Vec::new();
    let mut a = 45i64;
    while a > -136 {
        let r = (a as f64).to_radians();
        half.push((1.0 + R * r.cos(), 1.0 + R * r.sin()));
        a -= 15;
    }
    let shapes = vec![
        pl(&[(0.0, 1.0), (1.0 - R, 1.0)]).pipe().into(),
        Ellipse::new(1.0, 1.0, R, R).into(),
        Polyline::new(half).closed(true).boundary(false).fill("black").into(),
        pl(&[(1.0 + R, 1.0), (2.0, 1.0)]).pipe().into(),
    ];
    Ok(Geometry::new(2, 2, shapes, ports(&[("in", 'W', 1), ("out", 'E', 1)])).label("none"))
}

fn orifice(_: &Params) -> Result<Geometry> {
    let shapes = vec![
        pl(&[(0.0, 1.0), (2.0, 1.0)]).pipe().into(),
        deco(&[(0.8, 0.4), (0.8, 1.6)]),
        deco(&[(1.2, 0.4), (1.2, 1.6)]),
    ];
    let p = ports(&[("in", 'W', 1), ("out", 'E', 1), ("tap", 'N', 1)]);
    Ok(Geometry::new(2, 2, shapes, p).label("none"))
}

fn static_mixer(_: &Params) -> Result<Geometry> {
    let mut shapes: Vec<Shape> = vec![rect(0.0, 0.4, 4.0, 1.6, true).into()];
    for k in 0..4 {
        let x = 0.4 + 0.8 * k as f64;
        let (y0, y1) = if k % 2 == 1 { (0.5, 1.5) } else { (1.5, 0.5) };
        shapes.push(deco(&[(x, y0), (x + 0.8, y1)]));
    }
    Ok(Geometry::new(4, 2, shapes, ports(&[("in", 'W', 1), ("out", 'E', 1)])).label("above"))
}

fn ejector(_: &Params) -> Result<Geometry> {
    let body = [
        (0.0, 0.6),
        (1.5, 0.6),
        (2.2, 0.85),
        (4.0, 0.3),
        (4.0, 1.7),
        (2.2, 1.15),
        (1.5, 1.4),
        (0.0, 1.4),
    ];
    let shapes = vec![
        pl(&body).closed(true).into(),
        deco(&[(0.0, 1.0), (1.2, 1.0)]),
        deco(&[(1.0, 0.85), (1.4, 1.0), (1.0, 1.15)]),
    ];
    let p = ports(&[("motive", 'W', 1), ("suction", 'S', 1), ("out", 'E', 1)]);
    Ok(Geometry::new(4, 2, shapes, p).label("above"))
}

fn vertical_drum(params: &Params, name: &str, defaults: (i64, i64, i64), label: &str) -> Result<Geometry> {
    let w = get_even(params, "width", defaults.0, 2, &format!("{name} width"))?;
    let h = get_int(params, "height", defaults.1)?;
    let beds = get_int(params, "beds", defaults.2)?;
    let r = w / 2;
    let head = 1.max(r.min(if w > 4 { w / 3 } else { r }));
    if h < 2 * head + 1 {
        return Err(Error::Value(format!("{name} height must be >= {}", 2 * head + 1)));
    }
    let (lo, hi) = (head, h - head);
    let (lof, hif, wf, rf) = (lo as f64, hi as f64, w as f64, r as f64);
    let mut shapes: Vec<Shape> = vec![
        pl(&[(0.0, lof), (0.0, hif)]).into(),
        pl(&[(wf, lof), (wf, hif)]).into(),
        Ellipse::arc(rf, hif, rf, head as f64, 0.0, 180.0).into(),
        Ellipse::arc(rf, lof, rf, head as f64, 180.0, 360.0).into(),
    ];
    if beds != 0 {
        let gap = 1.0;
        let bed_h = ((hi - lo) as f64 - gap * (beds + 1) as f64) / beds as f64;
        if bed_h <= 0.0 {
            return Err(Error::Value(format!("{name} is too short for {beds} beds")));
        }
        for i in 0..beds {
            let y0 = lof + gap + i as f64 * (bed_h + gap);
            let y1 = y0 + bed_h;
            shapes.push(rect(0.0, y0, wf, y1, false).into());
            shapes.push(deco(&[(0.0, y0), (wf, y1)]));
            shapes.push(deco(&[(0.0, y1), (wf, y0)]));
        }
    }
    let mut p = ports(&[("top", 'N', r), ("bottom", 'S', r)]);
    for (i, x) in (1..w).enumerate() {
        p.insert(format!("n{i}"), Port::new('N', x));
        p.insert(format!("s{i}"), Port::new('S', x));
    }
    let rows: Vec<i64> = (lo..=hi).collect();
    p.extend(shell_ports(&rows));
    let heads = vec![
        (0..w - 1).map(|i| format!("n{i}")).collect(),
        (0..w - 1).map(|i| format!("s{i}")).collect(),
    ];
    let mut g = Geometry::new(w, h, shapes, p).label(label);
    g.nozzles = true;
    g.swappable = heads;
    Ok(g)
}

fn reactor(params: &Params) -> Result<Geometry> {
    const DRIVE: i64 = 3;
    const MOTOR: f64 = 0.9;
    const JACKET: f64 = 0.4;
    let w = get_even(params, "width", 6, 4, "reactor width")?;
    let h = get_int(params, "height", 10)?;
    let jacket = params.get("jacket").is_some_and(pyval::truthy);
    let r = w / 2;
    let head = 1.max(r.min(if w > 4 { w / 3 } else { r }));
    if h < 2 * head + 3 {
        return Err(Error::Value(format!("reactor height must be >= {}", 2 * head + 3)));
    }
    let (lo, hi) = (head, h - head);
    let a = if jacket { JACKET } else { 0.0 };
    let (lof, hif, wf, rf, hf) = (lo as f64, hi as f64, w as f64, r as f64, h as f64);
    let mut shapes: Vec<Shape> = vec![
        pl(&[(a, lof), (a, hif)]).into(),
        pl(&[(wf - a, lof), (wf - a, hif)]).into(),
        Ellipse::arc(rf, hif, rf - a, head as f64, 0.0, 180.0).into(),
        Ellipse::arc(rf, lof, rf - a, head as f64 - a, 180.0, 360.0).into(),
    ];
    let mut p = ports(&[("bottom", 'S', r)]);
    let top = lo + 2.max((hi - lo) * 2 / 3);
    if jacket {
        let j = top as f64 + 0.5;
        shapes.push(pl(&[(a, j), (0.0, j - a), (0.0, lof)]).into());
        shapes.push(pl(&[(wf - a, j), (wf, j - a), (wf, lof)]).into());
        shapes.push(Ellipse::arc(rf, lof, rf, head as f64, 180.0, 360.0).into());
    }
    let d = DRIVE as f64;
    shapes.push(Ellipse::new(rf, hf + d - MOTOR - 0.1, MOTOR, MOTOR).into());
    shapes.push(deco(&[(rf, hf + d - 2.0 * MOTOR - 0.1), (rf, lof + 0.5)]));
    shapes.push(Ellipse::new(rf - 0.6, lof + 0.5, 0.6, 0.18).boundary(false).into());
    shapes.push(Ellipse::new(rf + 0.6, lof + 0.5, 0.6, 0.18).boundary(false).into());
    let texts = vec![(rf, hf + d - MOTOR - 0.1, "M".to_string(), 0.7)];
    for (i, x) in (1..w).enumerate() {
        if x != r {
            p.insert(format!("n{i}"), Port::new('N', x));
        }
        p.insert(format!("s{i}"), Port::new('S', x));
    }
    let rows: Vec<i64> = (lo..=hi).collect();
    p.extend(shell_ports(&rows));
    if jacket {
        p.insert("jacket_in".into(), Port::new('W', lo));
        p.insert("jacket_out".into(), Port::new('E', top));
    }
    let heads = vec![
        (0..w - 1).filter(|i| i + 1 != r).map(|i| format!("n{i}")).collect(),
        (0..w - 1).map(|i| format!("s{i}")).collect(),
    ];
    let mut g = Geometry::new(w, h + DRIVE, shapes, p).label("left");
    g.texts = texts;
    g.nozzles = true;
    g.swappable = heads;
    Ok(g)
}

fn tank(params: &Params) -> Result<Geometry> {
    const ROOF: i64 = 1;
    let w = get_even(params, "width", 8, 4, "tank width")?;
    let h = get_int(params, "height", 8)?;
    if h < 4 {
        return Err(Error::Value("tank height must be >= 4".into()));
    }
    let eave = h - ROOF;
    let (wf, ef) = (w as f64, eave as f64);
    let shapes = vec![
        pl(&[(0.0, ef), (0.0, 0.0), (wf, 0.0), (wf, ef)]).into(),
        pl(&[(0.0, ef), (wf / 2.0, h as f64), (wf, ef)]).into(),
    ];
    let mut p = ports(&[("top", 'N', w / 2), ("bottom", 'S', w / 2)]);
    for (i, x) in (1..w).enumerate() {
        p.insert(format!("n{i}"), Port::new('N', x));
        p.insert(format!("s{i}"), Port::new('S', x));
    }
    let rows: Vec<i64> = (1..eave).collect();
    p.extend(shell_ports(&rows));
    let groups = vec![
        (0..w - 1).map(|i| format!("n{i}")).collect(),
        (0..w - 1).map(|i| format!("s{i}")).collect(),
    ];
    let mut g = Geometry::new(w, h, shapes, p);
    g.nozzles = true;
    g.swappable = groups;
    Ok(g)
}

fn horizontal_drum(params: &Params) -> Result<Geometry> {
    let n = get_even(params, "length", 8, 4, "horizontal_drum length")?;
    let d = get_even(params, "diameter", 4, 2, "horizontal_drum diameter")?;
    let (r, head) = (d / 2, 1.max(d / 4));
    if n < 2 * head + 2 {
        return Err(Error::Value("horizontal_drum is too short".into()));
    }
    let (nf, df, rf, hf) = (n as f64, d as f64, r as f64, head as f64);
    let shapes = vec![
        pl(&[(hf, 0.0), (nf - hf, 0.0)]).into(),
        pl(&[(hf, df), (nf - hf, df)]).into(),
        Ellipse::arc(hf, rf, hf, rf, 90.0, 270.0).into(),
        Ellipse::arc(nf - hf, rf, hf, rf, -90.0, 90.0).into(),
    ];
    let mut p = ports(&[("w", 'W', r), ("e", 'E', r)]);
    let cols: Vec<i64> = (head..=n - head).collect();
    for (i, &x) in cols.iter().enumerate() {
        p.insert(format!("n{i}"), Port::new('N', x));
        p.insert(format!("s{i}"), Port::new('S', x));
    }
    let nm = p[&format!("n{}", cols.len() / 2)];
    let sm = p[&format!("s{}", cols.len() / 2)];
    p.insert("n_mid".into(), nm);
    p.insert("s_mid".into(), sm);
    let groups = vec![
        (0..cols.len()).map(|i| format!("n{i}")).collect(),
        (0..cols.len()).map(|i| format!("s{i}")).collect(),
    ];
    let mut g = Geometry::new(n, d, shapes, p);
    g.nozzles = true;
    g.swappable = groups;
    Ok(g)
}

fn cyclone(_: &Params) -> Result<Geometry> {
    let body = [
        (0.0, 8.0),
        (4.0, 8.0),
        (4.0, 5.0),
        (2.5, 1.0),
        (2.5, 0.0),
        (1.5, 0.0),
        (1.5, 1.0),
        (0.0, 5.0),
    ];
    let shapes = vec![
        pl(&body).closed(true).into(),
        deco(&[(1.4, 8.0), (1.4, 6.3), (2.6, 6.3), (2.6, 8.0)]),
    ];
    let p = ports(&[("in", 'W', 7), ("gas_out", 'N', 2), ("solids", 'S', 2)]);
    let mut g = Geometry::new(4, 8, shapes, p).label("left");
    g.nozzles = true;
    Ok(g)
}

fn heat_exchanger(params: &Params) -> Result<Geometry> {
    let n = get_even(params, "length", 12, 8, "heat_exchanger length")?;
    let d = get_even(params, "diameter", 4, 2, "heat_exchanger diameter")?;
    let (nf, df) = (n as f64, d as f64);
    let r = df / 2.0;
    let mut shapes: Vec<Shape> = vec![
        pl(&[(1.0, 0.0), (nf - 1.0, 0.0)]).into(),
        pl(&[(1.0, df), (nf - 1.0, df)]).into(),
        Ellipse::arc(1.0, r, 1.0, r, 90.0, 270.0).into(),
        Ellipse::arc(nf - 1.0, r, 1.0, r, -90.0, 90.0).into(),
    ];
    for x in [1.8, 2.1, nf - 2.1, nf - 1.8] {
        shapes.push(deco(&[(x, -0.25), (x, df + 0.25)]));
    }
    let tubes = 3.max(d);
    for i in 1..=tubes {
        let y = (d * i) as f64 / (tubes + 1) as f64;
        shapes.push(deco(&[(2.1, y), (nf - 2.1, y)]));
    }
    let p = ports(&[
        ("tube_in", 'W', d / 2),
        ("tube_out", 'E', d / 2),
        ("shell_in", 'N', 4),
        ("shell_out", 'S', n - 4),
        ("shell_in_s", 'S', 4),
        ("shell_out_n", 'N', n - 4),
    ]);
    let mut g = Geometry::new(n, d, shapes, p).label("above");
    g.nozzles = true;
    if is(params, "orientation", "horizontal", "vertical") {
        g = rotate_ccw(&g).label("left");
    }
    Ok(g)
}

fn plate_heat_exchanger(_: &Params) -> Result<Geometry> {
    let mut shapes: Vec<Shape> = vec![rect(0.0, 0.0, 4.0, 6.0, true).into()];
    for k in 0..7 {
        let x = 0.8 + 0.4 * k as f64;
        shapes.push(deco(&[(x, 0.0), (x, 6.0)]));
    }
    shapes.push(deco(&[(0.0, 0.0), (4.0, 6.0)]));
    shapes.push(deco(&[(0.0, 6.0), (4.0, 0.0)]));
    let p = ports(&[("in1", 'W', 5), ("out1", 'E', 1), ("in2", 'W', 1), ("out2", 'E', 5)]);
    let mut g = Geometry::new(4, 6, shapes, p).label("above");
    g.nozzles = true;
    Ok(g)
}

fn air_cooler(params: &Params) -> Result<Geometry> {
    const BUNDLE: f64 = 2.0;
    let n = get_even(params, "length", 8, 4, "air_cooler length")?;
    let fans = get_int(params, "fans", 1)?;
    if !(1 <= fans && fans <= n / 4) {
        return Err(Error::Value(format!(
            "air_cooler fans must be 1 to {}, got {fans}",
            n / 4
        )));
    }
    let h = 2 + 2;
    let nf = n as f64;
    let mut shapes: Vec<Shape> = vec![rect(0.0, BUNDLE, nf, h as f64, true).into()];
    for y in [BUNDLE + 2.0 / 3.0, BUNDLE + 4.0 / 3.0] {
        shapes.push(deco(&[(0.0, y), (nf, y)]));
    }
    let bay = nf / fans as f64;
    for k in 0..fans {
        let cx = bay * (k as f64 + 0.5);
        shapes.push(deco(&[(cx - bay / 2.0 + 0.3, BUNDLE), (cx - 1.0, 1.2)]));
        shapes.push(deco(&[(cx + bay / 2.0 - 0.3, BUNDLE), (cx + 1.0, 1.2)]));
        shapes.push(Ellipse::new(cx, 1.2, 1.0, 0.25).boundary(false).into());
        shapes.push(deco(&[(cx, 1.2), (cx, 0.6)]));
        shapes.push(rect(cx - 0.4, 0.0, cx + 0.4, 0.6, false).into());
    }
    let mut g = Geometry::new(n, h, shapes, ports(&[("in", 'W', 3), ("out", 'E', 3)])).label("above");
    g.nozzles = true;
    Ok(g)
}

fn furnace(params: &Params) -> Result<Geometry> {
    const CONVECTION: i64 = 2;
    const STACK: i64 = 3;
    let w = get_even(params, "width", 8, 6, "furnace width")?;
    let h = get_int(params, "height", 12)?;
    if h < 10 {
        return Err(Error::Value("furnace height must be >= 10".into()));
    }
    let box_ = h - CONVECTION - STACK;
    let (wf, bf, hf) = (w as f64, box_ as f64, h as f64);
    let c = wf / 2.0;
    let cv = CONVECTION as f64;
    let mut shapes: Vec<Shape> = vec![
        rect(0.0, 0.0, wf, bf, true).into(),
        pl(&[(0.0, bf), (1.0, bf + cv), (wf - 1.0, bf + cv)]).into(),
        pl(&[(wf - 1.0, bf + cv), (wf, bf)]).into(),
        rect(c - 1.0, bf + cv, c + 1.0, hf, true).into(),
    ];
    let mut coil: Vec<Point> = vec![(0.5, bf + 1.0), (wf - 1.2, bf + 1.0), (wf - 1.2, bf - 0.5)];
    coil.push((1.0, bf - 0.5));
    for x in 1..w {
        let top = if x == 1 { bf - 0.5 } else { bf - 1.3 };
        let down = x % 2 == 1;
        let ys = if down { (top, 1.8) } else { (1.8, top) };
        coil.push((x as f64, ys.0));
        coil.push((x as f64, ys.1));
    }
    let last = coil.len() - 1;
    coil[last] = (wf - 1.0, 1.0);
    coil.push((wf, 1.0));
    shapes.push(deco(&coil));
    for x in [c - 1.5, c, c + 1.5] {
        shapes.push(deco(&[(x - 0.3, 0.0), (x, 0.9), (x + 0.3, 0.0)]));
    }
    let p = ports(&[
        ("in", 'W', box_ + 1),
        ("out", 'E', 1),
        ("fuel", 'S', w / 2),
        ("flue", 'N', w / 2),
    ]);
    let mut g = Geometry::new(w, h, shapes, p).label("left");
    g.nozzles = true;
    Ok(g)
}

fn filter_housing(params: &Params) -> Result<Geometry> {
    const INSET: f64 = 0.25;
    const LEGS: i64 = 2;
    const HEAD: i64 = 1;
    const LOWER: i64 = 2;
    const BAND: f64 = 0.15;
    const FOOT: f64 = 0.7;
    const LEG_INSET: f64 = 0.35;
    const CARTRIDGE_GAP: f64 = 0.8;
    let name = "filter_housing";
    let w = get_even(params, "width", 4, 4, &format!("{name} width"))?;
    let h = get_int(params, "height", 10)?;
    if h < 9 {
        return Err(Error::Value(format!("{name} height must be >= 9")));
    }
    let wf = w as f64;
    let r = wf / 2.0;
    let a = INSET;
    let bottom = LEGS + HEAD;
    let plate = bottom + LOWER;
    let joint = h - HEAD;
    let (bt, pt, jt) = (bottom as f64, plate as f64, joint as f64);
    let mut shapes: Vec<Shape> = vec![
        pl(&[(a, bt), (a, jt)]).into(),
        pl(&[(wf - a, bt), (wf - a, jt)]).into(),
        Ellipse::arc(r, bt, r - a, HEAD as f64, 180.0, 360.0).into(),
        Ellipse::arc(r, jt, r - a, HEAD as f64, 0.0, 180.0).into(),
        rect(a - BAND, jt - BAND, wf - a + BAND, jt + BAND, false).into(),
        deco(&[(a, pt), (wf - a, pt)]),
    ];
    for x in [LEG_INSET, wf - LEG_INSET] {
        shapes.push(pl(&[(x, 0.0), (x, bt)]).into());
        let x0 = (x - FOOT / 2.0).max(0.0).min(wf - FOOT);
        shapes.push(pl(&[(x0, 0.0), (x0 + FOOT, 0.0)]).into());
    }
    let n = w / 2;
    let slot = (wf - 2.0 * a) / (2 * n + 1) as f64;
    for k in 0..n {
        let x0 = a + (2 * k + 1) as f64 * slot;
        shapes.push(rect(x0, pt, x0 + slot, jt - CARTRIDGE_GAP, false).into());
    }
    let mut p = ports(&[("vent", 'N', w / 2), ("drain", 'S', w / 2)]);
    let rows: Vec<i64> = (bottom + 1..joint).collect();
    p.extend(shell_ports(&rows));
    p.insert("in".into(), Port::new('W', plate + 1));
    p.insert("out".into(), Port::new('E', plate - 1));
    let mut g = Geometry::new(w, h, shapes, p).label("above");
    g.nozzles = true;
    Ok(g)
}

fn header(params: &Params, lib: &Library) -> Result<Geometry> {
    let n = get_int(params, "branches", 3)?;
    if n < 1 {
        return Err(Error::Value("header needs at least one branch".into()));
    }
    let pitch = get_int(params, "pitch", 2)?;
    let empty = serde_json::Map::new();
    let raw = match params.get("branch_elements") {
        Some(Value::Object(o)) => o,
        Some(v) if !pyval::truthy(v) => &empty,
        None => &empty,
        Some(v) => {
            return Err(Error::Type(format!(
                "branch_elements must be a dict, got {}",
                pyval::repr(v)
            )));
        }
    };
    let stacks = header_stacks(raw, n, lib)?;
    let none: Vec<Geometry> = Vec::new();
    let stack = |i: i64| stacks.get(&i).unwrap_or(&none);
    let half: Vec<i64> = (0..n)
        .map(|i| stack(i).iter().map(|g| g.width / 2).max().unwrap_or(0))
        .collect();
    let mut xs = vec![1.max(half[0])];
    for i in 1..n as usize {
        let prev = *xs.last().unwrap();
        xs.push(prev + pitch.max(half[i - 1] + half[i] + 1));
    }
    let width = xs.last().unwrap() + 1.max(*half.last().unwrap());
    let mut top = 2;
    for s in stacks.values() {
        top = top.max(1 + s.iter().map(|g| g.height + 1).sum::<i64>());
    }
    let topf = top as f64;
    let mut shapes: Vec<Shape> = vec![pl(&[(0.0, 0.0), (width as f64, 0.0)]).pipe().into()];
    let mut p = ports(&[("main_w", 'W', 0), ("main_e", 'E', 0)]);
    for (i, &x) in xs.iter().enumerate() {
        p.insert(format!("b{i}"), Port::new('N', x));
        p.insert(format!("m{i}"), Port::new('S', x));
        let xf = x as f64;
        let mut y = 0.0;
        let mut base = 1i64;
        for g in stack(i as i64) {
            let lo = *g.ports.values().find(|q| q.side == 'S').unwrap();
            let hi = *g.ports.values().find(|q| q.side == 'N').unwrap();
            shapes.extend(g.translated((x - lo.offset) as f64, base as f64));
            let below = outline_distance(&g.shapes, (lo.offset as f64, -1.0), (0.0, 1.0))?;
            let above = outline_distance(&g.shapes, (hi.offset as f64, g.height as f64 + 1.0), (0.0, -1.0))?;
            let y_lo = base as f64 + below - 1.0;
            let y_hi = (base + g.height) as f64 + 1.0 - above;
            shapes.push(pl(&[(xf, y), (xf, y_lo)]).pipe().into());
            y = y_hi;
            base += g.height + 1;
        }
        shapes.push(pl(&[(xf, y), (xf, topf)]).pipe().into());
    }
    let plain: Vec<i64> = (0..n).filter(|i| stack(*i).is_empty()).collect();
    let groups = vec![
        plain.iter().map(|i| format!("b{i}")).collect(),
        (0..n).map(|i| format!("m{i}")).collect(),
    ];
    let mut g = Geometry::new(width, top, shapes, p).label("none");
    g.swappable = groups;
    Ok(g)
}

/// Geometries of the elements inserted on each header branch, bottom to top.
fn header_stacks(raw: &serde_json::Map<String, Value>, n: i64, lib: &Library) -> Result<IndexMap<i64, Vec<Geometry>>> {
    let mut stacks: IndexMap<i64, Vec<Geometry>> = IndexMap::new();
    for (key, value) in raw {
        let idx = key.trim().parse::<i64>().map_err(|_| {
            Error::Value(format!(
                "invalid literal for int() with base 10: {}",
                pyval::str_repr(key)
            ))
        })?;
        if !(0 <= idx && idx < n) {
            return Err(Error::Value(format!("branch index {idx} out of range 0..{}", n - 1)));
        }
        let items: Vec<&Value> = match value {
            Value::Array(a) => a.iter().collect(),
            v => vec![v],
        };
        for item in items {
            let (tname, tparams) = match item {
                Value::String(s) => (s.clone(), Params::new()),
                Value::Object(o) => {
                    let t = o.get("type").ok_or_else(|| Error::Key("type".into()))?;
                    let tp = match o.get("params") {
                        Some(Value::Object(p)) => p.clone(),
                        _ => Params::new(),
                    };
                    (pyval::str(t), tp)
                }
                v => return Err(Error::Type(format!("bad branch element {}", pyval::repr(v)))),
            };
            let mut params = Params::new();
            params.insert("orientation".into(), Value::from("vertical"));
            for (k, v) in tparams {
                params.insert(k, v);
            }
            let geo = lib.build(&tname, &params)?;
            let south: Vec<&Port> = geo.ports.values().filter(|p| p.side == 'S').collect();
            let north: Vec<&Port> = geo.ports.values().filter(|p| p.side == 'N').collect();
            if south.is_empty() || north.is_empty() || south[0].offset != north[0].offset {
                return Err(Error::Value(format!(
                    "{} needs aligned S and N ports to sit on a branch",
                    pyval::str_repr(&tname)
                )));
            }
            if geo.width != 2 * south[0].offset {
                return Err(Error::Value(format!(
                    "{} must be centred on its S/N ports",
                    pyval::str_repr(&tname)
                )));
            }
            stacks.entry(idx).or_default().push(geo);
        }
    }
    Ok(stacks)
}

fn tee(params: &Params) -> Result<Geometry> {
    let arms = get_str(params, "arms", "wen").to_lowercase();
    let ends = |a: char| -> Option<Point> {
        match a {
            'w' => Some((0.0, 1.0)),
            'e' => Some((2.0, 1.0)),
            'n' => Some((1.0, 2.0)),
            's' => Some((1.0, 0.0)),
            _ => None,
        }
    };
    if arms.is_empty() || arms.chars().any(|a| ends(a).is_none()) {
        return Err(Error::Value("tee arms must be a combination of n, s, e, w".into()));
    }
    let shapes = arms
        .chars()
        .map(|a| pl(&[(1.0, 1.0), ends(a).unwrap()]).pipe().into())
        .collect();
    let p = arms
        .chars()
        .map(|a| (a.to_string(), Port::new(a.to_ascii_uppercase(), 1)))
        .collect();
    Ok(Geometry::new(2, 2, shapes, p).label("none"))
}

fn connector(params: &Params) -> Result<Geometry> {
    let n = get_even(params, "length", 6, 4, "connector length")?;
    let text = get_str(params, "text", "");
    let nf = n as f64;
    let (pts, p, tx) = if is(params, "point", "right", "left") {
        (
            vec![(nf, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 2.0), (nf, 2.0)],
            ports(&[("tip", 'W', 1), ("tail", 'E', 1)]),
            nf / 2.0 + 0.3,
        )
    } else {
        (
            vec![(0.0, 0.0), (nf - 1.0, 0.0), (nf, 1.0), (nf - 1.0, 2.0), (0.0, 2.0)],
            ports(&[("tip", 'E', 1), ("tail", 'W', 1)]),
            nf / 2.0 - 0.3,
        )
    };
    let mut g = Geometry::new(n, 2, vec![Polyline::new(pts).closed(true).into()], p).label("none");
    g.texts = vec![(tx, 1.0, text, 0.8)];
    g.off_sheet = true;
    Ok(g)
}

pub const UTILITY_POINTS: [&str; 4] = ["right", "left", "up", "down"];

fn utility(params: &Params) -> Result<Geometry> {
    const FIT: f64 = 3.0;
    let point = match params.get("point") {
        None => "right".to_string(),
        Some(Value::String(s)) if UTILITY_POINTS.contains(&s.as_str()) => s.clone(),
        Some(_) => {
            return Err(Error::Value(
                "utility point must be one of ['right', 'left', 'up', 'down']".into(),
            ));
        }
    };
    let lead = get_int(params, "lead", 4)?;
    if lead < 1 {
        return Err(Error::Value(format!("utility lead must be at least 1, got {lead}")));
    }
    let text = get_str(params, "text", "");
    let size = 0.75 * 1.0f64.min(FIT / (text.chars().count().max(1)) as f64);
    let n = 2 + lead;
    let nf = n as f64;
    let (c, a, b, port, w, h) = match point.as_str() {
        "right" => ((1.0, 1.0), (2.0, 1.0), (nf, 1.0), Port::new('E', 1), n, 2),
        "left" => ((nf - 1.0, 1.0), (nf - 2.0, 1.0), (0.0, 1.0), Port::new('W', 1), n, 2),
        "up" => ((1.0, 1.0), (1.0, 2.0), (1.0, nf), Port::new('N', 1), 2, n),
        _ => ((1.0, nf - 1.0), (1.0, nf - 2.0), (1.0, 0.0), Port::new('S', 1), 2, n),
    };
    let mut lead_line = pl(&[a, b]).pipe();
    lead_line.line = Some("utility".into());
    let shapes = vec![Ellipse::new(c.0, c.1, 1.0, 1.0).into(), lead_line.into()];
    let mut p = IndexMap::new();
    p.insert("out".to_string(), port);
    let mut g = Geometry::new(w, h, shapes, p).label("none");
    g.texts = vec![(c.0, c.1, text, size)];
    g.off_sheet = true;
    g.beside = true;
    Ok(g)
}

fn instrument(params: &Params) -> Result<Geometry> {
    let mount = params.get("mount").cloned().unwrap_or(Value::from("field"));
    let dcs = match mount.as_str() {
        Some("field") => false,
        Some("dcs") => true,
        _ => return Err(Error::Value("instrument mount must be 'field' or 'dcs'".into())),
    };
    let mut shapes: Vec<Shape> = vec![Ellipse::new(1.0, 1.0, 1.0, 1.0).boundary(!dcs).into()];
    if dcs {
        shapes.push(rect(0.0, 0.0, 2.0, 2.0, true).into());
    }
    let texts = vec![
        (1.0, 1.38, get_str(params, "function", ""), 0.75),
        (1.0, 0.62, get_str(params, "number", ""), 0.75),
    ];
    let p = ports(&[("n", 'N', 1), ("s", 'S', 1), ("e", 'E', 1), ("w", 'W', 1)]);
    let mut g = Geometry::new(2, 2, shapes, p).label("none");
    g.texts = texts;
    Ok(g)
}

// -- user-defined elements -------------------------------------------------------

pub const CUSTOM_KEYS: [&str; 8] = [
    "name",
    "width",
    "height",
    "shapes",
    "ports",
    "label_pos",
    "nozzles",
    "texts",
];

/// User-defined element described by plain data (e.g. loaded from JSON).
#[derive(Clone, Debug)]
pub struct CustomSpec {
    pub name: String,
    pub width: i64,
    pub height: i64,
    pub shapes: Vec<Value>,
    pub ports: serde_json::Map<String, Value>,
    pub label_pos: String,
    pub nozzles: bool,
    pub texts: Vec<Value>,
}

impl CustomSpec {
    pub fn from_dict(d: &serde_json::Map<String, Value>) -> Result<CustomSpec> {
        let mut unknown: Vec<&String> = d.keys().filter(|k| !CUSTOM_KEYS.contains(&k.as_str())).collect();
        if !unknown.is_empty() {
            unknown.sort();
            return Err(Error::Value(format!("unknown keys {}", pyval::list_repr(&unknown))));
        }
        let label = d.get("label_pos").map(pyval::str).unwrap_or_else(|| "center".into());
        if !LABEL_POSITIONS.contains(&label.as_str()) || d.get("label_pos").is_some_and(|v| !v.is_string()) {
            return Err(Error::Value(
                "label_pos must be one of ('center', 'above', 'below', 'left', 'none')".into(),
            ));
        }
        let need = |k: &str| d.get(k).ok_or_else(|| Error::Key(k.into()));
        let spec = CustomSpec {
            name: pyval::str(need("name")?),
            width: pyval::int(need("width")?)?,
            height: pyval::int(need("height")?)?,
            shapes: match d.get("shapes") {
                Some(Value::Array(a)) => a.clone(),
                _ => Vec::new(),
            },
            ports: match d.get("ports") {
                Some(Value::Object(o)) => o.clone(),
                _ => serde_json::Map::new(),
            },
            label_pos: label,
            nozzles: d.get("nozzles").is_some_and(pyval::truthy),
            texts: match d.get("texts") {
                Some(Value::Array(a)) => a.clone(),
                _ => Vec::new(),
            },
        };
        spec.build()?; // fail now, not at layout time
        Ok(spec)
    }

    pub fn build(&self) -> Result<Geometry> {
        let mut shapes: Vec<Shape> = Vec::new();
        for s in &self.shapes {
            let o = s
                .as_object()
                .ok_or_else(|| Error::Type("shape must be a dict".into()))?;
            let need = |k: &str| -> Result<f64> { pyval::float(o.get(k).ok_or_else(|| Error::Key(k.into()))?) };
            let b = o.get("boundary").map(pyval::truthy).unwrap_or(true);
            let fill = o.get("fill").filter(|v| !v.is_null()).map(pyval::str);
            let t = pyval::str(o.get("type").ok_or_else(|| Error::Key("type".into()))?);
            match t.as_str() {
                "rect" => {
                    let mut r = rect(need("x0")?, need("y0")?, need("x1")?, need("y1")?, b);
                    r.fill = fill;
                    shapes.push(r.into());
                }
                "polyline" => {
                    let pts = match o.get("points") {
                        Some(Value::Array(a)) => a
                            .iter()
                            .map(|p| match p {
                                Value::Array(xy) if xy.len() == 2 => Ok((pyval::float(&xy[0])?, pyval::float(&xy[1])?)),
                                _ => Err(Error::Value(format!("bad point {}", pyval::repr(p)))),
                            })
                            .collect::<Result<Vec<Point>>>()?,
                        _ => return Err(Error::Key("points".into())),
                    };
                    let mut p = Polyline::new(pts)
                        .closed(o.get("closed").is_some_and(pyval::truthy))
                        .boundary(b);
                    p.fill = fill;
                    p.pipe = o.get("pipe").is_some_and(pyval::truthy);
                    shapes.push(p.into());
                }
                "ellipse" | "arc" => {
                    let th1 = match o.get("theta1") {
                        Some(v) => pyval::float(v)?,
                        None => 0.0,
                    };
                    let th2 = match o.get("theta2") {
                        Some(v) => pyval::float(v)?,
                        None => 360.0,
                    };
                    let mut e = Ellipse::arc(need("cx")?, need("cy")?, need("rx")?, need("ry")?, th1, th2).boundary(b);
                    e.fill = fill;
                    shapes.push(e.into());
                }
                _ => return Err(Error::Value(format!("unknown shape type {}", pyval::str_repr(&t)))),
            }
        }
        let mut ports = IndexMap::new();
        for (k, v) in &self.ports {
            ports.insert(k.clone(), parse_port(v)?);
        }
        let mut texts = Vec::new();
        for t in &self.texts {
            match t {
                Value::Array(a) if a.len() == 4 => texts.push((
                    pyval::float(&a[0])?,
                    pyval::float(&a[1])?,
                    pyval::str(&a[2]),
                    pyval::float(&a[3])?,
                )),
                _ => return Err(Error::Value(format!("bad text {}", pyval::repr(t)))),
            }
        }
        let mut g = Geometry::new(self.width, self.height, shapes, ports).label(&self.label_pos);
        g.texts = texts;
        g.nozzles = self.nozzles;
        Ok(g)
    }

    pub fn to_dict(&self) -> Value {
        let mut d = serde_json::Map::new();
        d.insert("name".into(), Value::from(self.name.clone()));
        d.insert("width".into(), Value::from(self.width));
        d.insert("height".into(), Value::from(self.height));
        d.insert("shapes".into(), Value::Array(self.shapes.clone()));
        let mut ports = serde_json::Map::new();
        for (k, v) in &self.ports {
            if let Ok(p) = parse_port(v) {
                let mut o = serde_json::Map::new();
                o.insert("side".into(), Value::from(p.side.to_string()));
                o.insert("offset".into(), Value::from(p.offset));
                ports.insert(k.clone(), Value::Object(o));
            }
        }
        d.insert("ports".into(), Value::Object(ports));
        d.insert("label_pos".into(), Value::from(self.label_pos.clone()));
        d.insert("nozzles".into(), Value::from(self.nozzles));
        d.insert("texts".into(), Value::Array(self.texts.clone()));
        Value::Object(d)
    }
}

// -- registry --------------------------------------------------------------------

/// A set of element types: the standard ones plus a flowsheet's own
/// (JSON-defined) elements on top.
#[derive(Clone, Debug, Default)]
pub struct Library {
    pub custom: IndexMap<String, Arc<CustomSpec>>,
}

/// One-line description of each standard element (``--list-elements``).
pub fn summary(name: &str) -> &'static str {
    match name {
        "pump" => "Centrifugal pump (2x3), the size of a valve.",
        "compressor" => "Centrifugal compressor (4x4): a trapezoid, wide at the suction.",
        "blower" => "Centrifugal blower or fan (4x4), drawn as the ISO 10628 fan: a round",
        "centrifuge" => "Basket centrifuge (4x4): a casing with the basket inside, standing on",
        "valve" => "Valve in a 2x2 box.",
        "safety_valve" => "Spring-loaded safety (relief) valve (2x3): an angle body, its inlet",
        "strainer" => "Y-type strainer (2x2), drawn on the pipe: the screen leg slants down",
        "steam_trap" => "Steam trap (2x2), drawn as in ISO 10628: a circle on the condensate",
        "orifice" => "Flow orifice plate (2x2): two short bars across the pipe.  Put it on",
        "static_mixer" => "In-line static mixer (4x2): a pipe section with twisted elements.",
        "ejector" => "Steam / gas ejector (4x2): the motive nozzle, the throat and the",
        "vertical_drum" => "Vertical vessel with rounded heads.",
        "column" => "Tall tower with packed beds (defaults: 4 wide, 20 tall, 2 beds).",
        "reactor" => "Vertical vessel with an agitator driven by a motor on the top head,",
        "tank" => "Flat-bottomed storage tank with a cone roof.",
        "horizontal_drum" => "Horizontal vessel with rounded heads.",
        "cyclone" => "Cyclone separator (4x8): a cylinder over a cone, with the vortex",
        "heat_exchanger" => "Shell & tube exchanger with rounded channel heads.",
        "plate_heat_exchanger" => "Plate heat exchanger (4x6), drawn as in ISO 10628: the frame with its",
        "air_cooler" => "Fin-fan air cooler: the tube bundle on top of forced-draft fans.",
        "furnace" => "Fired heater: a radiant box with burners on its floor, the convection",
        "filter_housing" => "Cartridge-filter housing on legs, its lid held by a V-band clamp.",
        "header" => "Horizontal main pipe with branches that all rise in +Y.",
        "tee" => "Pipe junction (2x2) so a line can branch or an instrument can tap a pipe.",
        "connector" => "Off-sheet connector flag (pentagon) with up to two lines of text.",
        "utility" => "Where a utility (steam, nitrogen, cooling water ...) comes in: a",
        "instrument" => "Instrument bubble (2x2).",
        _ => "User-defined element described by plain data (e.g. loaded from JSON).",
    }
}

impl Library {
    pub fn contains(&self, name: &str) -> bool {
        self.custom.contains_key(name) || STANDARD.contains(&name)
    }

    pub fn check(&self, name: &str) -> Result<()> {
        if self.contains(name) {
            Ok(())
        } else {
            Err(Error::Key(format!(
                "unknown element type {}; known: {}",
                pyval::str_repr(name),
                pyval::list_repr(&self.names())
            )))
        }
    }

    pub fn names(&self) -> Vec<String> {
        let mut out: Vec<String> = STANDARD.iter().map(|s| s.to_string()).collect();
        for k in self.custom.keys() {
            if !out.contains(k) {
                out.push(k.clone());
            }
        }
        out.sort();
        out
    }

    /// Does the element face the port of its one line (a utility circle)?
    pub fn aims(&self, name: &str) -> bool {
        !self.custom.contains_key(name) && name == "utility"
    }

    pub fn register(&mut self, spec: CustomSpec) {
        self.custom.insert(spec.name.clone(), Arc::new(spec));
    }

    /// Build an element, then apply the common ``rotation`` / ``mirror``.
    pub fn build(&self, name: &str, params: &Params) -> Result<Geometry> {
        let mut own = params.clone();
        let rotation = own.shift_remove("rotation");
        let mirror = own.shift_remove("mirror");
        let geo = if let Some(spec) = self.custom.get(name) {
            spec.build()?
        } else {
            match name {
                "pump" => pump(&own),
                "compressor" => compressor(&own),
                "blower" => blower(&own),
                "centrifuge" => centrifuge(&own),
                "valve" => valve(&own),
                "safety_valve" => safety_valve(&own),
                "strainer" => strainer(&own),
                "steam_trap" => steam_trap(&own),
                "orifice" => orifice(&own),
                "static_mixer" => static_mixer(&own),
                "ejector" => ejector(&own),
                "vertical_drum" => vertical_drum(&own, "vertical_drum", (6, 10, 0), "center"),
                "column" => vertical_drum(&own, "column", (4, 20, 2), "left"),
                "reactor" => reactor(&own),
                "tank" => tank(&own),
                "horizontal_drum" => horizontal_drum(&own),
                "cyclone" => cyclone(&own),
                "heat_exchanger" => heat_exchanger(&own),
                "plate_heat_exchanger" => plate_heat_exchanger(&own),
                "air_cooler" => air_cooler(&own),
                "furnace" => furnace(&own),
                "filter_housing" => filter_housing(&own),
                "header" => header(&own, self),
                "tee" => tee(&own),
                "connector" => connector(&own),
                "utility" => utility(&own),
                "instrument" => instrument(&own),
                _ => {
                    self.check(name)?;
                    unreachable!()
                }
            }?
        };
        let mut geo = transform(geo, rotation.as_ref(), mirror.as_ref())?;
        geo.recipe = Some((name.to_string(), params.clone(), serde_json::Map::new()));
        Ok(geo)
    }
}
