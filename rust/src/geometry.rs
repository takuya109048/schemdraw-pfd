//! Outline primitives and ray casting used to find exact connection points.
//!
//! All coordinates are in grid units, local to an element (origin = bottom-left
//! of the element's bounding box).

use crate::error::{Error, Result};
use crate::pyval::fmod;

pub type Point = (f64, f64);
pub const EPS: f64 = 1e-9;

/// side -> outward unit normal
pub fn normal(side: char) -> (i64, i64) {
    match side {
        'N' => (0, 1),
        'S' => (0, -1),
        'E' => (1, 0),
        _ => (-1, 0),
    }
}

/// Open or closed polyline (see the Python ``geometry.Polyline``).
#[derive(Clone, Debug, PartialEq)]
pub struct Polyline {
    pub points: Vec<Point>,
    pub closed: bool,
    pub boundary: bool,
    pub fill: Option<String>,
    pub pipe: bool,
    pub line: Option<String>,
}

impl Polyline {
    pub fn new(points: Vec<Point>) -> Polyline {
        Polyline {
            points,
            closed: false,
            boundary: true,
            fill: None,
            pipe: false,
            line: None,
        }
    }

    pub fn closed(mut self, closed: bool) -> Polyline {
        self.closed = closed;
        self
    }

    pub fn boundary(mut self, boundary: bool) -> Polyline {
        self.boundary = boundary;
        self
    }

    pub fn pipe(mut self) -> Polyline {
        self.pipe = true;
        self
    }

    pub fn fill(mut self, fill: &str) -> Polyline {
        self.fill = Some(fill.into());
        self
    }

    pub fn segments(&self) -> Vec<(Point, Point)> {
        let pts = &self.points;
        let mut segs: Vec<(Point, Point)> = pts.windows(2).map(|w| (w[0], w[1])).collect();
        if self.closed && pts.len() > 2 {
            segs.push((pts[pts.len() - 1], pts[0]));
        }
        segs
    }
}

/// Ellipse or elliptic arc (angles in degrees, counter-clockwise).
#[derive(Clone, Debug, PartialEq)]
pub struct Ellipse {
    pub cx: f64,
    pub cy: f64,
    pub rx: f64,
    pub ry: f64,
    pub theta1: f64,
    pub theta2: f64,
    pub boundary: bool,
    pub fill: Option<String>,
}

impl Ellipse {
    pub fn new(cx: f64, cy: f64, rx: f64, ry: f64) -> Ellipse {
        Ellipse {
            cx,
            cy,
            rx,
            ry,
            theta1: 0.0,
            theta2: 360.0,
            boundary: true,
            fill: None,
        }
    }

    pub fn arc(cx: f64, cy: f64, rx: f64, ry: f64, theta1: f64, theta2: f64) -> Ellipse {
        Ellipse {
            cx,
            cy,
            rx,
            ry,
            theta1,
            theta2,
            boundary: true,
            fill: None,
        }
    }

    pub fn boundary(mut self, boundary: bool) -> Ellipse {
        self.boundary = boundary;
        self
    }

    pub fn fill(mut self, fill: &str) -> Ellipse {
        self.fill = Some(fill.into());
        self
    }

    pub fn full(&self) -> bool {
        (self.theta2 - self.theta1).abs() >= 360.0 - EPS
    }

    pub fn contains_angle(&self, deg: f64) -> bool {
        if self.full() {
            return true;
        }
        let span = fmod(self.theta2 - self.theta1, 360.0);
        fmod(deg - self.theta1, 360.0) <= span + 1e-7
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum Shape {
    Poly(Polyline),
    Ell(Ellipse),
}

impl Shape {
    pub fn boundary(&self) -> bool {
        match self {
            Shape::Poly(p) => p.boundary,
            Shape::Ell(e) => e.boundary,
        }
    }

    pub fn shifted(&self, dx: f64, dy: f64) -> Shape {
        match self {
            Shape::Poly(p) => Shape::Poly(Polyline {
                points: p.points.iter().map(|&(x, y)| (x + dx, y + dy)).collect(),
                ..p.clone()
            }),
            Shape::Ell(e) => Shape::Ell(Ellipse {
                cx: e.cx + dx,
                cy: e.cy + dy,
                ..e.clone()
            }),
        }
    }
}

impl From<Polyline> for Shape {
    fn from(p: Polyline) -> Shape {
        Shape::Poly(p)
    }
}

impl From<Ellipse> for Shape {
    fn from(e: Ellipse) -> Shape {
        Shape::Ell(e)
    }
}

pub fn rect(x0: f64, y0: f64, x1: f64, y1: f64, boundary: bool) -> Polyline {
    Polyline::new(vec![(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
        .closed(true)
        .boundary(boundary)
}

/// Smallest t >= 0 where o + t*d touches segment ab (d is axis aligned).
fn ray_segment(o: Point, d: Point, a: Point, b: Point) -> Option<f64> {
    let (ex, ey) = (b.0 - a.0, b.1 - a.1);
    let denom = d.0 * ey - d.1 * ex;
    if denom.abs() < EPS {
        if ((a.0 - o.0) * d.1 - (a.1 - o.1) * d.0).abs() > EPS {
            return None;
        }
        let ts: Vec<f64> = [a, b]
            .iter()
            .map(|p| (p.0 - o.0) * d.0 + (p.1 - o.1) * d.1)
            .filter(|&t| t >= -EPS)
            .collect();
        if ts.is_empty() {
            return None;
        }
        let m = ts.iter().cloned().fold(f64::INFINITY, f64::min);
        return Some(m.max(0.0));
    }
    let t = ((a.0 - o.0) * ey - (a.1 - o.1) * ex) / denom;
    let u = ((a.0 - o.0) * d.1 - (a.1 - o.1) * d.0) / denom;
    if t >= -EPS && -EPS <= u && u <= 1.0 + EPS {
        return Some(t.max(0.0));
    }
    None
}

fn ray_ellipse(o: Point, d: Point, e: &Ellipse) -> Option<f64> {
    let (ox, oy) = ((o.0 - e.cx) / e.rx, (o.1 - e.cy) / e.ry);
    let (dx, dy) = (d.0 / e.rx, d.1 / e.ry);
    let a = dx * dx + dy * dy;
    let b = 2.0 * (ox * dx + oy * dy);
    let c = ox * ox + oy * oy - 1.0;
    let disc = b * b - 4.0 * a * c;
    if disc < -EPS {
        return None;
    }
    let disc = disc.max(0.0).sqrt();
    let mut ts = [(-b - disc) / (2.0 * a), (-b + disc) / (2.0 * a)];
    if ts[1] < ts[0] {
        ts.swap(0, 1);
    }
    for t in ts {
        if t < -EPS {
            continue;
        }
        let (px, py) = (o.0 + t * d.0, o.1 + t * d.1);
        let ang = ((py - e.cy) / e.ry).atan2((px - e.cx) / e.rx).to_degrees();
        if e.contains_angle(ang) {
            return Some(t.max(0.0));
        }
    }
    None
}

/// Distance from ``origin`` along ``direction`` to the first outline hit.
pub fn ray_hit(shapes: &[Shape], origin: Point, direction: Point) -> Option<f64> {
    let mut best: Option<f64> = None;
    for s in shapes {
        if !s.boundary() {
            continue;
        }
        let cands: Vec<Option<f64>> = match s {
            Shape::Poly(p) => p
                .segments()
                .into_iter()
                .map(|(a, b)| ray_segment(origin, direction, a, b))
                .collect(),
            Shape::Ell(e) => vec![ray_ellipse(origin, direction, e)],
        };
        for t in cands.into_iter().flatten() {
            if best.is_none_or(|b| t < b) {
                best = Some(t);
            }
        }
    }
    best
}

/// Like [`ray_hit`], but an outline must be hit.
pub fn outline_distance(shapes: &[Shape], origin: Point, direction: Point) -> Result<f64> {
    ray_hit(shapes, origin, direction).ok_or_else(|| {
        Error::Value(format!(
            "no outline found from ({}, {}) towards ({}, {})",
            crate::pyval::float_repr(origin.0),
            crate::pyval::float_repr(origin.1),
            direction.0,
            direction.1
        ))
    })
}

/// Connection point request: a side of the bounding box plus a grid offset.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Port {
    pub side: char,
    pub offset: i64,
}

impl Port {
    pub const fn new(side: char, offset: i64) -> Port {
        Port { side, offset }
    }
}

/// One line crossing: ``(h_route, h_seg, v_route, v_seg, point)``.
pub type Crossing = (usize, usize, usize, usize, Point);

/// All line crossings; ``h_*`` identify the horizontal segment, ``v_*`` the
/// vertical one.
pub fn find_crossings(paths: &[&[Point]]) -> Vec<Crossing> {
    let mut horiz = Vec::new();
    let mut vert = Vec::new();
    for (ri, pts) in paths.iter().enumerate() {
        for (si, w) in pts.windows(2).enumerate() {
            let (p, q) = (w[0], w[1]);
            if p.1 == q.1 {
                horiz.push((ri, si, p.1, p.0.min(q.0), p.0.max(q.0)));
            } else {
                vert.push((ri, si, p.0, p.1.min(q.1), p.1.max(q.1)));
            }
        }
    }
    let mut out = Vec::new();
    for &(hr, hs, y, x0, x1) in &horiz {
        for &(vr, vs, x, y0, y1) in &vert {
            if x0 < x && x < x1 && y0 < y && y < y1 {
                out.push((hr, hs, vr, vs, (x, y)));
            }
        }
    }
    out
}

/// Number of crossings among ``paths`` (without listing them).
pub fn count_crossings(paths: &[&[Point]]) -> usize {
    find_crossings(paths).len()
}
