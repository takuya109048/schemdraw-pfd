//! Revisions: changes drawn onto a finished sheet (see the Python
//! ``revise`` module).  Each revision works on the sheet as the ones before
//! it left it and never builds it anew, so what was there keeps its look.

use std::collections::{BTreeSet, HashMap, HashSet, VecDeque};
use std::sync::Arc;

use indexmap::IndexMap;
use serde_json::Value;

use crate::elements::{Geometry, Library};
use crate::error::{Error, Result};
use crate::expand::{GENERATED_PREFIX, TAP_PREFIX};
use crate::geometry::{Point, Port};
use crate::layout::Placed;
use crate::model::Diagram;
use crate::netlist::{Change, Connection, Equipment, InlineItem, REVISION_COLORS, Revision, split_ref};
use crate::pyval::{self, Params};
use crate::router::{DIRS, Node, Route, Router, Terminal, min_bends, resolve_port, simplify};
use crate::style::LineStyle;

pub const JUNCTION_PREFIX: &str = "_J";
pub const ROW_FLAG_PREFIXES: [&str; 2] = ["_RO", "_RI"];
pub const LINE_ITEM_PREFIX: &str = "_L";
pub const JUNCTION: &str = "junction";
const REACH: i64 = 10;
const CANDIDATES: usize = 8;
const SPOTS: usize = 24;
const TAPS: usize = 16;
const ROOM_TRIES: i64 = 3;
const BEND_COST: f64 = 6.0;
const MARGIN: i64 = 24;

type BoxI = (i64, i64, i64, i64);

/// One revision applied to a diagram, in order.
#[derive(Clone, Debug, PartialEq)]
pub struct RevisionInfo {
    pub id: String,
    pub color: String,
    pub note: String,
}

/// An item or a line a revision took away (drawn dashed if shown).
#[derive(Clone, Debug)]
pub struct Ghost {
    pub rev: String,
    pub placed: Option<Placed>,
    pub line: Option<(Connection, Route)>,
}

pub fn line_key(c: &Connection) -> String {
    format!("{} -> {}", c.src, c.dst)
}

pub fn revision_color(rev: &Revision, index: usize) -> String {
    rev.color
        .clone()
        .unwrap_or_else(|| REVISION_COLORS[index % REVISION_COLORS.len()].to_string())
}

/// What a revision needs from its flowsheet.
#[derive(Clone, Debug, Default)]
pub struct Context {
    pub library: Arc<Library>,
    pub styles: IndexMap<String, LineStyle>,
    /// lines as the user wrote them so far
    pub lines: Vec<Connection>,
    /// named inline items: id -> (in, out) ports
    pub inline: HashMap<String, (String, String)>,
    /// every named item as written
    pub specs: HashMap<String, Equipment>,
}

impl Context {
    pub fn geometry(&self, e: &Equipment) -> Result<Arc<Geometry>> {
        e.geometry(&self.library)
            .map(Arc::new)
            .map_err(|err| err.context(&e.id))
    }
}

/// The zero-size item where an added line taps a line in use.
pub fn junction_geometry() -> Geometry {
    let mut ports = IndexMap::new();
    ports.insert("j".to_string(), Port::new('N', 0));
    Geometry::new(0, 0, Vec::new(), ports).label("none")
}

fn int_of(v: f64) -> Option<i64> {
    if v.fract() == 0.0 { Some(v as i64) } else { None }
}

fn inclusive(lo: i64, hi: i64, step: i64) -> Vec<i64> {
    if step > 0 {
        (lo..=hi).collect()
    } else {
        (hi..=lo).rev().collect()
    }
}

/// Grid cells along a polyline, in order (no repeats).
fn lattice(points: &[Point]) -> Vec<Node> {
    let mut out: Vec<Node> = Vec::new();
    for w in points.windows(2) {
        let (p, q) = (w[0], w[1]);
        let cells: Vec<Node> = if p.1 == q.1 {
            let Some(y) = int_of(p.1) else { continue };
            let (a, b) = (p.0, q.0);
            let step = if b >= a { 1 } else { -1 };
            let lo = if step > 0 { a.ceil() } else { a.floor() } as i64;
            let hi = if step > 0 { b.floor() } else { b.ceil() } as i64;
            inclusive(lo, hi, step).into_iter().map(|x| (x, y)).collect()
        } else if p.0 == q.0 {
            let Some(x) = int_of(p.0) else { continue };
            let (a, b) = (p.1, q.1);
            let step = if b >= a { 1 } else { -1 };
            let lo = if step > 0 { a.ceil() } else { a.floor() } as i64;
            let hi = if step > 0 { b.floor() } else { b.ceil() } as i64;
            inclusive(lo, hi, step).into_iter().map(|y| (x, y)).collect()
        } else {
            continue;
        };
        for c in cells {
            if out.last() != Some(&c) {
                out.push(c);
            }
        }
    }
    out
}

/// Cells a line runs through, with the axes it uses there.
fn pipe_cells(points: &[Point]) -> IndexMap<Node, u8> {
    let mut out: IndexMap<Node, u8> = IndexMap::new();
    for w in points.windows(2) {
        let bit = if w[0].0 == w[1].0 { 1 } else { 2 };
        for c in lattice(w) {
            *out.entry(c).or_insert(0) |= bit;
        }
    }
    out
}

fn inline_equipment(i: &InlineItem, params: Params) -> Equipment {
    Equipment::new(i.id.as_deref().unwrap_or("?"), &i.typ, Some(0), params)
}

/// Straight cells a line needs for its inline items.
fn run_needed(c: &Connection, ctx: &Context) -> Result<i64> {
    if c.inline.is_empty() {
        return Ok(0);
    }
    let mut sizes = Vec::new();
    for i in &c.inline {
        sizes.push(ctx.geometry(&inline_equipment(i, i.params.clone()))?.width);
    }
    Ok(sizes.iter().sum::<i64>() + sizes.len() as i64 - 1 + 6)
}

fn length(points: &[Point]) -> f64 {
    points
        .windows(2)
        .fold(0.0, |s, w| s + (w[1].0 - w[0].0).abs() + (w[1].1 - w[0].1).abs())
}

fn bbox(p: &Placed) -> BoxI {
    p.bbox()
}

/// A routed line ready to be put on the sheet.
#[derive(Clone)]
struct Found {
    r: Route,
    a: Terminal,
    b: Terminal,
    hosts: [Option<String>; 2],
    crossings: i64,
}

/// A line waiting to be routed (``tag``: which one, as the Python tool
/// tells them apart by identity).
#[derive(Clone)]
struct PLine {
    tag: usize,
    c: Connection,
}

#[derive(Clone)]
struct State {
    placed: IndexMap<String, Placed>,
    lines: IndexMap<usize, (Connection, Route)>,
    ghosts: Vec<Ghost>,
    rows: Vec<(f64, f64)>,
    added: IndexMap<String, String>,
    descs: IndexMap<String, String>,
    hosts: IndexMap<String, String>,
    next_line: usize,
    warnings: Vec<String>,
    uturns: i64,
    open: HashSet<String>,
    replaced: Vec<(String, String, String)>,
    changed: Vec<(String, String)>,
}

struct Sheet {
    ctx: Context,
    rev: String,
    index: usize,
    st: State,
    serial: usize,
    orphans: BTreeSet<String>,
    reuse: IndexMap<String, Placed>,
}

enum Todo {
    Item(String),
    Line(usize),
}

type Score = (i64, i64, i64, f64);
type Extra = (Vec<Point>, Option<BoxI>);
type Plan = (Vec<Node>, Vec<(Placed, usize, usize)>);

impl Sheet {
    fn new(d: &Diagram, ctx: Context, rev: &str, index: usize) -> Sheet {
        Sheet {
            ctx,
            rev: rev.into(),
            index,
            st: State {
                placed: d.placed.iter().map(|p| (p.id.clone(), p.clone())).collect(),
                lines: d.routes.iter().cloned().enumerate().collect(),
                ghosts: d.ghosts.clone(),
                rows: d.rows.clone(),
                added: d.added.clone(),
                descs: d.descs.clone(),
                hosts: d.hosts.clone(),
                next_line: d.routes.len(),
                warnings: d.warnings.clone(),
                uturns: d.uturns,
                open: HashSet::new(),
                replaced: d.replaced.clone(),
                changed: d.changed.clone(),
            },
            serial: 0,
            orphans: BTreeSet::new(),
            reuse: IndexMap::new(),
        }
    }

    // -- bookkeeping ------------------------------------------------------------
    fn new_id(&mut self, prefix: &str) -> String {
        loop {
            self.serial += 1;
            let i = format!("{prefix}{}_{}", self.index + 1, self.serial);
            if !self.st.placed.contains_key(&i) {
                return i;
            }
        }
    }

    fn add_line(&mut self, c: Connection, r: Route) -> usize {
        let k = self.st.next_line;
        self.st.next_line += 1;
        self.st.added.insert(line_key(&c), self.rev.clone());
        self.st.lines.insert(k, (c, r));
        k
    }

    fn add_item(&mut self, p: Placed, desc: &str) {
        self.st.added.insert(p.id.clone(), self.rev.clone());
        if !desc.is_empty() {
            self.st.descs.insert(p.id.clone(), desc.into());
        }
        self.st.placed.insert(p.id.clone(), p);
    }

    fn lines_at(&self, item: &str) -> Vec<usize> {
        self.st
            .lines
            .iter()
            .filter(|(_, (c, _))| c.ends().iter().any(|e| e.0 == item))
            .map(|(k, _)| *k)
            .collect()
    }

    /// Does ``item`` belong to the line it sits on?
    fn bound(&self, item: &str) -> bool {
        item.starts_with(LINE_ITEM_PREFIX)
            || ROW_FLAG_PREFIXES.iter().any(|p| item.starts_with(p))
            || self.ctx.inline.contains_key(item)
    }

    // -- removing ---------------------------------------------------------------
    fn drop_all(&mut self, items: Vec<String>, lines: Vec<usize>) -> Result<()> {
        let mut todo: VecDeque<Todo> = items.into_iter().map(Todo::Item).collect();
        todo.extend(lines.into_iter().map(Todo::Line));
        while let Some(t) = todo.pop_front() {
            match t {
                Todo::Item(x) => {
                    let Some(p) = self.st.placed.shift_remove(&x) else {
                        continue;
                    };
                    self.st.descs.shift_remove(&x);
                    self.st.hosts.shift_remove(&x);
                    if !x.starts_with(JUNCTION_PREFIX) {
                        self.st.ghosts.push(Ghost {
                            rev: self.rev.clone(),
                            placed: Some(p),
                            line: None,
                        });
                    }
                    todo.extend(self.lines_at(&x).into_iter().map(Todo::Line));
                    for (prefix, other) in [
                        (ROW_FLAG_PREFIXES[0], ROW_FLAG_PREFIXES[1]),
                        (ROW_FLAG_PREFIXES[1], ROW_FLAG_PREFIXES[0]),
                    ] {
                        if let Some(rest) = x.strip_prefix(prefix) {
                            todo.push_back(Todo::Item(format!("{other}{rest}")));
                        }
                    }
                }
                Todo::Line(k) => {
                    let Some((c, r)) = self.st.lines.shift_remove(&k) else {
                        continue;
                    };
                    let key = line_key(&c);
                    self.st.ghosts.push(Ghost {
                        rev: self.rev.clone(),
                        placed: None,
                        line: Some((c.clone(), r)),
                    });
                    for (j, h) in &self.st.hosts {
                        if *h == key {
                            todo.push_back(Todo::Item(j.clone()));
                        }
                    }
                    for (e, _) in c.ends() {
                        if !self.st.placed.contains_key(e) {
                            continue;
                        }
                        let left = self.lines_at(e);
                        if self.bound(e) {
                            todo.push_back(Todo::Item(e.into()));
                        } else if e.starts_with(TAP_PREFIX) {
                            if left.len() <= 1 {
                                todo.push_back(Todo::Item(e.into()));
                            } else {
                                self.trim_tee(e, &left)?;
                            }
                        } else if e.starts_with(JUNCTION_PREFIX) && left.is_empty() {
                            todo.push_back(Todo::Item(e.into()));
                        } else if self.st.placed[e].geo.off_sheet && left.is_empty() {
                            self.orphans.insert(e.into());
                        }
                    }
                }
            }
        }
        Ok(())
    }

    /// Draw only the arms of a tee that still carry a line.
    fn trim_tee(&mut self, tee: &str, left: &[usize]) -> Result<()> {
        let p = self.st.placed[tee].clone();
        let mut arms = String::new();
        for k in left {
            for (e, port) in self.st.lines[k].0.ends() {
                if e == tee && "nsew".contains(port) {
                    arms.push_str(port);
                }
            }
        }
        let gone: String = p
            .geo
            .ports
            .keys()
            .filter(|a| !arms.contains(a.as_str()))
            .cloned()
            .collect();
        if !arms.is_empty() && !gone.is_empty() {
            let mut params = Params::new();
            params.insert("arms".into(), Value::from(arms));
            let geo = Arc::new(self.ctx.library.build("tee", &params)?);
            let mut q = p.clone();
            q.geo = geo;
            self.st.placed.insert(tee.into(), q);
            let mut params = Params::new();
            params.insert("arms".into(), Value::from(gone));
            let mut g = p;
            g.geo = Arc::new(self.ctx.library.build("tee", &params)?);
            self.st.ghosts.push(Ghost {
                rev: self.rev.clone(),
                placed: Some(g),
                line: None,
            });
        }
        Ok(())
    }

    /// Remove an inline item and join its line up again through where it
    /// stood.  False (nothing done) if it does not sit on one straight line.
    fn unplug(&mut self, item: &str) -> Result<bool> {
        let (pin, pout) = self.ctx.inline[item].clone();
        let ks = self.lines_at(item);
        let into: Vec<usize> = ks
            .iter()
            .copied()
            .filter(|k| self.st.lines[k].0.end(1) == (item, pin.as_str()))
            .collect();
        let out: Vec<usize> = ks
            .iter()
            .copied()
            .filter(|k| self.st.lines[k].0.end(0) == (item, pout.as_str()))
            .collect();
        if into.len() != 1 || out.len() != 1 {
            return Ok(false);
        }
        let (ca, ra) = self.st.lines[&into[0]].clone();
        let (cb, rb) = self.st.lines[&out[0]].clone();
        if ca.kind != cb.kind {
            return Ok(false);
        }
        let a = &ra.points[..ra.points.len() - 1];
        let b = &rb.points[1..];
        if a.is_empty() || b.is_empty() || (a[a.len() - 1].0 != b[0].0 && a[a.len() - 1].1 != b[0].1) {
            return Ok(false);
        }
        let others: Vec<usize> = ks.iter().copied().filter(|k| *k != into[0] && *k != out[0]).collect();
        let joined = Connection::new(&ca.src, &cb.dst, &ca.kind);
        let mut pts = a.to_vec();
        pts.extend_from_slice(b);
        let route = Route {
            points: simplify(&pts),
            strict: ra.strict && rb.strict,
            nodes: Vec::new(),
        };
        let p = self.st.placed.shift_remove(item).unwrap();
        self.st.descs.shift_remove(item);
        self.st.ghosts.push(Ghost {
            rev: self.rev.clone(),
            placed: Some(p),
            line: None,
        });
        for k in into.iter().chain(&out) {
            self.st.lines.shift_remove(k);
        }
        let rev = self.st.added.get(&line_key(&ca)).cloned();
        let k = self.st.next_line;
        self.st.next_line += 1;
        let jk = line_key(&joined);
        self.st.lines.insert(k, (joined, route));
        if let Some(r) = rev {
            self.st.added.insert(jk.clone(), r);
        }
        let (ka, kb) = (line_key(&ca), line_key(&cb));
        for h in self.st.hosts.values_mut() {
            if *h == ka || *h == kb {
                *h = jk.clone();
            }
        }
        self.drop_all(Vec::new(), others)?;
        Ok(true)
    }

    // -- the lines as a graph ---------------------------------------------------
    fn same_port(&self, item: &str, a: &str, b: &str) -> bool {
        if a == b {
            return true;
        }
        self.st.placed.get(item).is_some_and(|p| {
            p.geo
                .swappable
                .iter()
                .any(|g| g.iter().any(|x| x == a) && g.iter().any(|x| x == b))
        })
    }

    /// The lines on the sheet that carry the written line ``src -> dst``.
    fn path(&self, src: &str, dst: &str, without: &HashSet<usize>) -> Result<Option<Vec<usize>>> {
        let (s, sp) = split_ref(src)?;
        let (d, dp) = split_ref(dst)?;
        let through: HashSet<&str> = self
            .st
            .placed
            .keys()
            .filter(|i| self.bound(i) || i.starts_with(TAP_PREFIX) || i.starts_with(JUNCTION_PREFIX))
            .map(String::as_str)
            .collect();
        let mut adj: HashMap<String, Vec<(String, Option<usize>, String)>> = HashMap::new();
        let mut link = |a: &str, pa: &str, b: &str, pb: &str, k: Option<usize>| {
            adj.entry(a.into()).or_default().push((b.into(), k, pa.into()));
            adj.entry(b.into()).or_default().push((a.into(), k, pb.into()));
        };
        let mut keys: HashMap<String, usize> = HashMap::new();
        for (&k, (c, _)) in &self.st.lines {
            keys.insert(line_key(c), k);
            if without.contains(&k) {
                continue;
            }
            let [(a, pa), (b, pb)] = c.ends();
            link(a, pa, b, pb, Some(k));
        }
        for (j, h) in &self.st.hosts {
            let Some(&k) = keys.get(h) else { continue };
            if without.contains(&k) || !self.st.placed.contains_key(j) {
                continue;
            }
            let [(a, pa), (b, pb)] = self.st.lines[&k].0.ends();
            link(a, pa, j, "j", None);
            link(j, "j", b, pb, None);
        }
        for i in self.st.placed.keys() {
            if let Some(rest) = i.strip_prefix(ROW_FLAG_PREFIXES[0]) {
                let other = format!("{}{rest}", ROW_FLAG_PREFIXES[1]);
                if self.st.placed.contains_key(&other) {
                    link(i, "", &other, "", None);
                }
            }
        }
        let empty = Vec::new();
        let at = |n: &str| adj.get(n).unwrap_or(&empty);
        let mut prev: HashMap<String, (String, Option<usize>)> = HashMap::new();
        let mut queue: VecDeque<String> = VecDeque::new();
        for (o, k, port) in at(&s) {
            if !self.same_port(&s, port, &sp) {
                continue;
            }
            if *o == d {
                let ends: Vec<&String> = at(&d)
                    .iter()
                    .filter(|(oo, kk, _)| *oo == s && kk == k)
                    .map(|e| &e.2)
                    .collect();
                if ends.iter().any(|p| self.same_port(&d, p, &dp)) {
                    return Ok(Some(k.map(|k| vec![k]).unwrap_or_default()));
                }
            }
            if through.contains(o.as_str()) && !prev.contains_key(o) {
                prev.insert(o.clone(), (s.clone(), *k));
                queue.push_back(o.clone());
            }
        }
        while let Some(n) = queue.pop_front() {
            for (o, k, _) in at(&n) {
                if *o == d {
                    let ends: Vec<&String> = at(&d)
                        .iter()
                        .filter(|(oo, kk, _)| *oo == n && kk == k)
                        .map(|e| &e.2)
                        .collect();
                    if ends.iter().any(|p| self.same_port(&d, p, &dp)) {
                        let mut out: Vec<usize> = k.iter().copied().collect();
                        let mut m = n.clone();
                        while m != s {
                            let (pm, kk) = prev[&m].clone();
                            if let Some(kk) = kk {
                                out.push(kk);
                            }
                            m = pm;
                        }
                        out.reverse();
                        return Ok(Some(out));
                    }
                }
                if through.contains(o.as_str()) && !prev.contains_key(o) && *o != s {
                    prev.insert(o.clone(), (n.clone(), *k));
                    queue.push_back(o.clone());
                }
            }
        }
        Ok(None)
    }

    fn remove_line(&mut self, src: &str, dst: &str) -> Result<()> {
        let none = HashSet::new();
        let Some(path) = self.path(src, dst, &none)? else {
            return Err(Error::Key(format!("remove: no line {src} -> {dst} on the sheet")));
        };
        let mut others = Vec::new();
        for c in &self.ctx.lines {
            if (c.src.as_str(), c.dst.as_str()) != (src, dst) && self.path(&c.src, &c.dst, &none)?.is_some() {
                others.push(c.clone());
            }
        }
        for &k in path.iter().rev() {
            let gone: HashSet<usize> = [k].into_iter().collect();
            let mut all = true;
            for c in &others {
                if self.path(&c.src, &c.dst, &gone)?.is_none() {
                    all = false;
                    break;
                }
            }
            if all {
                return self.drop_all(Vec::new(), vec![k]);
            }
        }
        self.drop_all(Vec::new(), path.last().copied().into_iter().collect())
    }

    // -- moving apart -----------------------------------------------------------
    /// Where ``make_room`` really cuts: moved on past every item.
    fn cut(&self, axis: usize, at: i64, by: i64) -> i64 {
        let mut boxes: Vec<BoxI> = self.st.placed.values().map(bbox).collect();
        boxes.extend(self.st.ghosts.iter().filter_map(|g| g.placed.as_ref().map(bbox)));
        let mut at = at;
        let mut moved = true;
        while moved {
            moved = false;
            for b in &boxes {
                let (lo, hi) = if axis == 0 { (b.0, b.2) } else { (b.1, b.3) };
                if by > 0 && lo < at && at <= hi {
                    at = hi + 1;
                    moved = true;
                } else if by < 0 && lo <= at && at < hi {
                    at = lo - 1;
                    moved = true;
                }
            }
        }
        at
    }

    /// Move everything at or beyond ``at`` by ``by`` cells.
    fn make_room(&mut self, axis: usize, at: i64, by: i64) {
        let at = self.cut(axis, at, by) as f64;
        let byf = by as f64;
        let mv = |v: f64| {
            if (by > 0 && v >= at) || (by <= 0 && v <= at) {
                v + byf
            } else {
                v
            }
        };
        let pt = |p: Point| if axis == 0 { (mv(p.0), p.1) } else { (p.0, mv(p.1)) };
        let node = |n: Node| {
            if axis == 0 {
                (mv(n.0 as f64) as i64, n.1)
            } else {
                (n.0, mv(n.1 as f64) as i64)
            }
        };
        let item = |p: &Placed| {
            let mut q = p.clone();
            if axis == 0 {
                q.x = mv(p.x as f64) as i64;
            } else {
                q.y = mv(p.y as f64) as i64;
            }
            q
        };
        let route = |r: &Route| Route {
            points: r.points.iter().map(|&q| pt(q)).collect(),
            strict: r.strict,
            nodes: r.nodes.iter().map(|&n| node(n)).collect(),
        };
        for p in self.st.placed.values_mut() {
            *p = item(p);
        }
        for (_, r) in self.st.lines.values_mut() {
            *r = route(r);
        }
        for g in self.st.ghosts.iter_mut() {
            if let Some(p) = &g.placed {
                g.placed = Some(item(p));
            }
            if let Some((_, r)) = &mut g.line {
                *r = route(r);
            }
        }
        if axis == 1 {
            for r in self.st.rows.iter_mut() {
                *r = (mv(r.0), mv(r.1));
            }
        }
    }

    // -- routing ----------------------------------------------------------------
    /// A router over the sheet with every line laid.
    fn board(&self, also: Option<&str>) -> Router {
        let placed: Vec<Placed> = self.st.placed.values().cloned().collect();
        let boxes: Vec<BoxI> = placed.iter().map(bbox).collect();
        let x0 = boxes.iter().map(|b| b.0).min().unwrap() as f64;
        let y0 = boxes.iter().map(|b| b.1).min().unwrap() as f64;
        let x1 = boxes.iter().map(|b| b.2).max().unwrap() as f64;
        let y1 = boxes.iter().map(|b| b.3).max().unwrap() as f64;
        let mut over = 0.0f64;
        for (_, r) in self.st.lines.values() {
            for &(x, y) in &r.points {
                over = over.max(x0 - x).max(x - x1).max(y0 - y).max(y - y1);
            }
        }
        let mut router = Router::with_margin(&placed, MARGIN + over.trunc() as i64 + 1);
        for (_, r) in self.st.lines.values() {
            lay(&mut router, &r.points);
        }
        for g in self.cleared(also) {
            block(&mut router, bbox(&g));
        }
        router
    }

    /// Items this revision removed whose place stays clear.
    fn cleared(&self, also: Option<&str>) -> Vec<Placed> {
        self.st
            .ghosts
            .iter()
            .filter(|g| g.rev == self.rev)
            .filter_map(|g| g.placed.clone())
            .filter(|p| !self.st.open.contains(&p.id) && Some(p.id.as_str()) != also)
            .collect()
    }

    fn used(&self, item: &str, port: &str) -> Vec<usize> {
        self.st
            .lines
            .iter()
            .filter(|(_, (c, _))| c.ends().iter().any(|&(e, p)| e == item && p == port))
            .map(|(k, _)| *k)
            .collect()
    }

    /// The port a new line onto ``item.port`` takes.
    fn port_for(&self, item: &str, port: &str) -> Result<String> {
        let p = &self.st.placed[item];
        if !p.geo.ports.contains_key(port) {
            let mut known: Vec<&String> = p.geo.ports.keys().collect();
            known.sort();
            return Err(Error::Key(format!(
                "{item}: unknown port {}; known: {}",
                pyval::str_repr(port),
                pyval::list_repr(&known)
            )));
        }
        if self.used(item, port).is_empty() {
            return Ok(port.into());
        }
        let written: HashSet<(String, String)> = self
            .ctx
            .lines
            .iter()
            .flat_map(|c| c.ends().map(|(e, q)| (e.to_string(), q.to_string())))
            .filter(|(e, _)| e == item)
            .collect();
        if written.contains(&(item.to_string(), port.to_string())) {
            return Ok(port.into());
        }
        for g in &p.geo.swappable {
            if g.iter().any(|x| x == port) {
                for q in g {
                    if self.used(item, q).is_empty() && !written.contains(&(item.to_string(), q.clone())) {
                        return Ok(q.clone());
                    }
                }
            }
        }
        Ok(port.into())
    }

    /// Where a new line may meet ``item.port``, nearest first.
    fn terminals(&self, router: &Router, item: &str, port: &str) -> Result<Vec<(Terminal, Option<String>)>> {
        let p = &self.st.placed[item];
        let port = self.port_for(item, port)?;
        let own = resolve_port(p, &port)?;
        if self.used(item, &port).is_empty() {
            return Ok(vec![(own, None)]);
        }
        let mut keys: IndexMap<usize, bool> = IndexMap::new();
        let mut seen: HashSet<String> = [item.to_string()].into_iter().collect();
        let mut frontier: VecDeque<(String, String, bool)> = VecDeque::new();
        frontier.push_back((item.to_string(), port.clone(), false));
        while let Some((e, q, far)) = frontier.pop_front() {
            let ks = if q.is_empty() {
                self.lines_at(&e)
            } else {
                self.used(&e, &q)
            };
            for k in ks {
                if keys.contains_key(&k) {
                    continue;
                }
                keys.insert(k, far);
                for (o, _) in self.st.lines[&k].0.ends() {
                    if seen.contains(o) {
                        continue;
                    }
                    if o.starts_with(TAP_PREFIX) {
                        seen.insert(o.into());
                        frontier.push_back((o.into(), String::new(), far));
                    } else if self.bound(o) && !ROW_FLAG_PREFIXES.iter().any(|x| o.starts_with(x)) {
                        seen.insert(o.into());
                        frontier.push_back((o.into(), String::new(), true));
                    }
                }
            }
        }
        let h = router.h;
        let n = router.axes.len() as i64;
        let mut near: HashSet<Node> = HashSet::new();
        for (o, q) in &self.st.placed {
            if o == item || seen.contains(o) {
                continue;
            }
            let (x0, y0, x1, y1) = bbox(q);
            for x in x0 - 1..x1 + 2 {
                for y in y0 - 1..y1 + 2 {
                    near.insert((x, y));
                }
            }
        }
        let mut out: Vec<(f64, Terminal, String)> = Vec::new();
        for (&k, &far) in &keys {
            let (c, r) = &self.st.lines[&k];
            let cells = lattice(&r.points);
            let corners: HashSet<Node> = r
                .points
                .iter()
                .filter(|(x, _)| int_of(*x).is_some())
                .map(|&(x, y)| (x as i64, y.trunc() as i64))
                .collect();
            if cells.len() < 3 {
                continue;
            }
            for j in 1..cells.len() - 1 {
                let cell = cells[j];
                if corners.contains(&cell) || near.contains(&cell) {
                    continue;
                }
                let i = (cell.0 - router.x0) * h + cell.1 - router.y0;
                if !(0 <= i && i < n) || router.blocked[i as usize] != 0 || router.axes[i as usize] == 3 {
                    continue;
                }
                if router.door[i as usize] != 0 {
                    continue;
                }
                let (prev, nxt) = (cells[j - 1], cells[j + 1]);
                let vertical = prev.0 == nxt.0;
                for d in if vertical { [1usize, 3] } else { [0, 2] } {
                    let (dx, dy) = DIRS[d];
                    let f = (cell.0 + dx, cell.1 + dy);
                    let fi = (f.0 - router.x0) * h + f.1 - router.y0;
                    if !(0 <= fi && fi < n) {
                        continue;
                    }
                    let fu = fi as usize;
                    if router.blocked[fu] != 0 || router.axes[fu] != 0 || router.door[fu] != 0 {
                        continue;
                    }
                    let t = Terminal {
                        point: (cell.0 as f64, cell.1 as f64),
                        edge: cell,
                        first: f,
                        out_dir: d,
                    };
                    let dist = (cell.0 as f64 - own.point.0).abs() + (cell.1 as f64 - own.point.1).abs();
                    out.push((dist + 1000.0 * far as i64 as f64, t, line_key(c)));
                }
            }
        }
        out.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
        Ok(out.into_iter().take(TAPS).map(|(_, t, k)| (t, Some(k))).collect())
    }

    /// Route ``c`` (its ends placed) on ``router`` and lay it there.
    fn route(&self, router: &mut Router, c: &Connection) -> Result<Option<Found>> {
        let [(s, sp), (d, dp)] = c.ends();
        let srcs = self.terminals(router, s, sp)?;
        let dsts = self.terminals(router, d, dp)?;
        for (t, host) in srcs.iter().chain(&dsts) {
            if host.is_none() {
                router.reserve(std::slice::from_ref(t));
            }
        }
        let guess = |a: &Terminal, b: &Terminal| (a.first.0 - b.first.0).abs() + (a.first.1 - b.first.1).abs();
        let mut pairs: Vec<(&(Terminal, Option<String>), &(Terminal, Option<String>))> =
            srcs.iter().flat_map(|a| dsts.iter().map(move |b| (a, b))).collect();
        pairs.sort_by_key(|(a, b)| guess(&a.0, &b.0));
        pairs.truncate(CANDIDATES);
        let mut best: Option<((bool, f64), Found)> = None;
        for ((a, ha), (b, hb)) in pairs {
            let Some(r) = router.route(a, b, true, false) else {
                continue;
            };
            let crossings = r
                .nodes
                .iter()
                .filter(|&&(x, y)| router.axes[((x - router.x0) * router.h + y - router.y0) as usize] != 0)
                .count() as i64;
            let cost = (r.bends() + crossings) as f64 * BEND_COST + length(&r.points);
            let key = (!r.strict, cost);
            if best
                .as_ref()
                .is_none_or(|b| key.partial_cmp(&b.0) == Some(std::cmp::Ordering::Less))
            {
                best = Some((
                    key,
                    Found {
                        r,
                        a: *a,
                        b: *b,
                        hosts: [ha.clone(), hb.clone()],
                        crossings,
                    },
                ));
            }
        }
        let Some((_, found)) = best else { return Ok(None) };
        router.commit_nodes(&found.r.nodes);
        Ok(Some(found))
    }

    /// Put a routed line on the sheet (junctions where it taps lines).
    fn commit(&mut self, router: &mut Router, c: &Connection, found: &Found) -> Result<(Connection, usize)> {
        let mut ends: Vec<(String, String)> = c.ends().iter().map(|(a, b)| (a.to_string(), b.to_string())).collect();
        for (end, (t, host)) in [&found.a, &found.b].into_iter().zip(&found.hosts).enumerate() {
            match host {
                None => {
                    let (item, port) = ends[end].clone();
                    ends[end] = (item.clone(), self.port_for(&item, &port)?);
                }
                Some(h) => {
                    let j = self.new_id(JUNCTION_PREFIX);
                    let p = Placed {
                        id: j.clone(),
                        typ: JUNCTION.into(),
                        level: 0,
                        geo: Arc::new(junction_geometry()),
                        x: t.edge.0,
                        y: t.edge.1,
                    };
                    self.add_item(p, "");
                    self.st.hosts.insert(j.clone(), h.clone());
                    block(router, (t.edge.0, t.edge.1, t.edge.0, t.edge.1));
                    ends[end] = (j, "j".into());
                }
            }
        }
        let mut flat = c
            .with_src(&format!("{}.{}", ends[0].0, ends[0].1))
            .with_dst(&format!("{}.{}", ends[1].0, ends[1].1));
        flat.inline = Vec::new();
        if !found.r.strict {
            self.st.uturns += 1;
        }
        let k = self.add_line(flat.clone(), found.r.clone());
        Ok((flat, k))
    }

    // -- inline items -----------------------------------------------------------
    fn oriented(&self, item: &InlineItem, way: usize) -> Result<Option<Arc<Geometry>>> {
        let mut params = item.params.clone();
        if !params.contains_key("rotation") && !params.contains_key("mirror") {
            match way {
                0 => {
                    params.insert("rotation".into(), Value::from(90));
                }
                2 => {
                    params.insert("rotation".into(), Value::from(270));
                }
                3 => {
                    params.insert("mirror".into(), Value::Bool(true));
                }
                _ => {}
            }
        }
        let geo = self.ctx.geometry(&inline_equipment(item, params))?;
        let (Some(pi), Some(po)) = (geo.ports.get(&item.port_in), geo.ports.get(&item.port_out)) else {
            let mut known: Vec<&String> = geo.ports.keys().collect();
            known.sort();
            return Err(Error::Key(format!(
                "inline {} has no port {} / {}; known: {} (set 'in' / 'out')",
                pyval::str_repr(&item.typ),
                pyval::str_repr(&item.port_in),
                pyval::str_repr(&item.port_out),
                pyval::list_repr(&known)
            )));
        };
        let sides_in = ['S', 'W', 'N', 'E'];
        let sides_out = ['N', 'E', 'S', 'W'];
        if pi.side != sides_in[way] || po.side != sides_out[way] {
            return Ok(None);
        }
        Ok(if pi.offset == po.offset {
            Some(geo.clone())
        } else {
            None
        })
    }

    /// Where the inline items of ``user`` go on its route ``r``.
    fn inline_plan(
        &self,
        user: &Connection,
        r: &Route,
        ends: (Option<&Placed>, Option<&Placed>),
        skip: Option<usize>,
        extra: &[Extra],
    ) -> Result<Option<Plan>> {
        let mut cells = lattice(&r.points);
        let (sp, dp) = ends;
        let inside = |cell: Node, p: Option<&Placed>| {
            p.is_some_and(|p| {
                let (x0, y0, x1, y1) = bbox(p);
                x0 <= cell.0 && cell.0 <= x1 && y0 <= cell.1 && cell.1 <= y1
            })
        };
        while cells.len() > 1 && inside(cells[0], sp) && inside(cells[1], sp) {
            cells.remove(0);
        }
        while cells.len() > 1 && inside(cells[cells.len() - 1], dp) && inside(cells[cells.len() - 2], dp) {
            cells.pop();
        }
        if cells.len() < 4 {
            return Ok(None);
        }
        let own: HashSet<Node> = cells.iter().copied().collect();
        let mut others: HashMap<Node, u8> = HashMap::new();
        for (&kk, (_, rr)) in &self.st.lines {
            if Some(kk) != skip {
                for (cell, bits) in pipe_cells(&rr.points) {
                    *others.entry(cell).or_insert(0) |= bits;
                }
            }
        }
        let mut boxes: Vec<BoxI> = self
            .st
            .placed
            .iter()
            .filter(|(i, _)| !i.starts_with(JUNCTION_PREFIX))
            .map(|(_, p)| bbox(p))
            .collect();
        for (pts, b) in extra {
            if !pts.is_empty() {
                for (cell, bits) in pipe_cells(pts) {
                    *others.entry(cell).or_insert(0) |= bits;
                }
            }
            if let Some(b) = b {
                boxes.push(*b);
            }
        }
        let mut dirs = vec![0usize];
        for w in cells.windows(2) {
            let step = (w[1].0 - w[0].0, w[1].1 - w[0].1);
            dirs.push(
                DIRS.iter()
                    .position(|&v| v == step)
                    .ok_or_else(|| Error::Value("broken route".into()))?,
            );
        }
        let mut anchor = user.anchor.clone();
        if anchor == "auto" {
            let flag = dp.is_some_and(|p| p.geo.off_sheet) && !sp.is_some_and(|p| p.geo.off_sheet);
            anchor = if flag { "src" } else { "dst" }.into();
        }
        let n = cells.len();
        let fits = |at0: usize, items: &[InlineItem]| -> Result<Option<Vec<(Placed, usize, usize)>>> {
            let mut out = Vec::new();
            let mut at = at0;
            for item in items {
                if at < 2 || at >= n - 2 {
                    return Ok(None);
                }
                let way = dirs[at];
                let Some(geo) = self.oriented(item, way)? else {
                    return Ok(None);
                };
                let tmp = Placed {
                    id: "?".into(),
                    typ: item.typ.clone(),
                    level: 0,
                    geo: geo.clone(),
                    x: 0,
                    y: 0,
                };
                let ti = resolve_port(&tmp, &item.port_in)?;
                let to = resolve_port(&tmp, &item.port_out)?;
                let end = at + ((to.edge.0 - ti.edge.0).abs() + (to.edge.1 - ti.edge.1).abs()) as usize;
                if end + 1 > n - 2 {
                    return Ok(None);
                }
                if (at - 1..end + 2).any(|j| dirs[j] != way) {
                    return Ok(None);
                }
                let p = Placed {
                    id: "?".into(),
                    typ: item.typ.clone(),
                    level: 0,
                    geo,
                    x: cells[at].0 - ti.edge.0,
                    y: cells[at].1 - ti.edge.1,
                };
                if resolve_port(&p, &item.port_out)?.edge != cells[end] {
                    return Ok(None);
                }
                let (bx0, by0, bx1, by1) = bbox(&p);
                let mine: HashSet<Node> = cells[at..=end].iter().copied().collect();
                for cx in bx0..=bx1 {
                    for cy in by0..=by1 {
                        let cell = (cx, cy);
                        if others.contains_key(&cell) || (own.contains(&cell) && !mine.contains(&cell)) {
                            return Ok(None);
                        }
                    }
                }
                for b in &boxes {
                    if b.0 <= bx1 + 1 && b.2 >= bx0 - 1 && b.1 <= by1 + 1 && b.3 >= by0 - 1 {
                        return Ok(None);
                    }
                }
                out.push((p, at, end));
                at = end + 2;
            }
            Ok(Some(out))
        };
        let items = &user.inline;
        let mut best: Option<((bool, i64), Vec<(Placed, usize, usize)>)> = None;
        for at in 2..n.saturating_sub(2) {
            let Some(trial) = fits(at, items)? else { continue };
            if trial.is_empty() {
                continue;
            }
            let (first, last) = (trial[0].1, trial[trial.len() - 1].2);
            let way = dirs[first];
            let lead = first >= 3 && dirs[first - 2] == way;
            let tail = last + 2 < n && dirs[last + 2] == way;
            let near = if anchor == "dst" {
                (n - 1 - last) as i64
            } else {
                first as i64
            };
            let key = (!(lead && tail), near);
            if best.as_ref().is_none_or(|b| key < b.0) {
                best = Some((key, trial));
            }
        }
        Ok(best.map(|(_, t)| (cells, t)))
    }

    /// Put the inline items of ``user`` on its routed line ``k``.
    fn put_inline(&mut self, user: &Connection, k: usize, ids: &[String]) -> Result<bool> {
        let (c, r) = self.st.lines[&k].clone();
        let [(s, _), (d, _)] = c.ends();
        let plan = self.inline_plan(
            user,
            &r,
            (Some(&self.st.placed[s]), Some(&self.st.placed[d])),
            Some(k),
            &[],
        )?;
        let Some((cells, found)) = plan else { return Ok(false) };
        self.st.lines.shift_remove(&k);
        self.st.added.shift_remove(&line_key(&c));
        let mut refs = vec![c.src.clone()];
        let mut placed_items = Vec::new();
        for ((item, iid), (p, a, b)) in user.inline.iter().zip(ids).zip(found) {
            let mut p = p;
            p.id = iid.clone();
            self.add_item(p.clone(), &item.desc);
            refs.push(format!("{iid}.{}", item.port_in));
            refs.push(format!("{iid}.{}", item.port_out));
            placed_items.push((p, item, a, b));
        }
        refs.push(c.dst.clone());
        let mut cuts: Vec<(usize, Point)> = vec![(0, r.points[0])];
        for (p, item, a, b) in &placed_items {
            cuts.push((*a, resolve_port(p, &item.port_in)?.point));
            cuts.push((*b, resolve_port(p, &item.port_out)?.point));
        }
        cuts.push((cells.len() - 1, *r.points.last().unwrap()));
        for j in (0..refs.len()).step_by(2) {
            let ((a, pa), (b, pb)) = (cuts[j], cuts[j + 1]);
            let mut pts: Vec<Point> = vec![pa];
            pts.extend(cells[a..=b].iter().map(|&(x, y)| (x as f64, y as f64)));
            pts.push(pb);
            let nodes = if b > a + 1 {
                cells[a + 1..b].to_vec()
            } else {
                Vec::new()
            };
            let piece = Route {
                points: simplify(&pts),
                strict: r.strict,
                nodes,
            };
            self.add_line(Connection::new(&refs[j], &refs[j + 1], &c.kind), piece);
        }
        Ok(true)
    }

    // -- placing ------------------------------------------------------------------
    /// Cells an added item may not cover: around items, and on lines.
    fn free_cells(&self, also: Option<&str>) -> (HashSet<Node>, HashSet<Node>) {
        let mut near: HashSet<Node> = HashSet::new();
        let kept = self.cleared(also);
        let boxes: Vec<BoxI> = self.st.placed.values().map(bbox).chain(kept.iter().map(bbox)).collect();
        for (x0, y0, x1, y1) in boxes {
            for x in x0 - 1..x1 + 2 {
                for y in y0 - 1..y1 + 2 {
                    near.insert((x, y));
                }
            }
        }
        let mut pipes: HashSet<Node> = HashSet::new();
        for (_, r) in self.st.lines.values() {
            pipes.extend(lattice(&r.points));
        }
        let kept_ids: HashSet<&str> = kept.iter().map(|p| p.id.as_str()).collect();
        let freed: HashSet<&str> = self
            .st
            .ghosts
            .iter()
            .filter(|g| g.rev == self.rev)
            .filter_map(|g| g.placed.as_ref().map(|p| p.id.as_str()))
            .filter(|i| !kept_ids.contains(i))
            .collect();
        for g in &self.st.ghosts {
            if g.rev == self.rev {
                if let Some((c, r)) = &g.line {
                    if !c.ends().iter().any(|(e, _)| freed.contains(e)) {
                        pipes.extend(lattice(&r.points));
                    }
                }
            }
        }
        (near, pipes)
    }

    /// Route ``lines`` one after another on ``router``, without changing
    /// the sheet.
    fn try_lines(&self, router: &mut Router, lines: &[PLine]) -> Result<(Score, Vec<(PLine, Found)>)> {
        let (mut fails, mut short, mut turns, mut cost) = (0i64, 0i64, 0i64, 0.0f64);
        let mut extra: Vec<Extra> = Vec::new();
        let mut done = Vec::new();
        for pl in lines {
            let c = &pl.c;
            let Some(found) = self.route(router, c)? else {
                fails += 1;
                continue;
            };
            if !c.inline.is_empty() {
                let [(s, _), (d, _)] = c.ends();
                let ends = (
                    if found.hosts[0].is_none() {
                        Some(&self.st.placed[s])
                    } else {
                        None
                    },
                    if found.hosts[1].is_none() {
                        Some(&self.st.placed[d])
                    } else {
                        None
                    },
                );
                match self.inline_plan(c, &found.r, ends, None, &extra)? {
                    None => short += 1,
                    Some((_, items)) => {
                        for (p, _, _) in items {
                            block(router, bbox(&p));
                            extra.push((Vec::new(), Some(bbox(&p))));
                        }
                    }
                }
            }
            extra.push((found.r.points.clone(), None));
            turns += !found.r.strict as i64;
            cost += (found.r.bends() + found.crossings) as f64 * BEND_COST + length(&found.r.points);
            done.push((pl.clone(), found));
        }
        Ok(((fails, short, turns, cost), done))
    }
}

fn lay(router: &mut Router, points: &[Point]) {
    let (h, n) = (router.h, router.axes.len() as i64);
    for (c, bits) in pipe_cells(points) {
        let i = (c.0 - router.x0) * h + c.1 - router.y0;
        if 0 <= i && i < n {
            router.axes[i as usize] |= bits;
        }
    }
}

/// Block a box on ``router``; False if it leaves the grid.
fn block(router: &mut Router, b: BoxI) -> bool {
    let (h, w) = (router.h, router.w);
    let (x0, y0, x1, y1) = b;
    if x0 - router.x0 < 1 || x1 - router.x0 > w - 2 {
        return false;
    }
    if y0 - router.y0 < 1 || y1 - router.y0 > h - 2 {
        return false;
    }
    for x in x0..=x1 {
        let i = (x - router.x0) * h - router.y0;
        for y in y0..=y1 {
            router.blocked[(i + y) as usize] = 1;
        }
    }
    true
}

/// ``d`` with ``rev`` drawn onto it.
pub fn apply_revision(d: Diagram, rev: &Revision, index: usize, ctx: &mut Context) -> Result<Diagram> {
    let mut sheet = Sheet::new(&d, std::mem::take(ctx), &rev.id, index);
    let result = apply(&mut sheet, &d, rev, index);
    *ctx = std::mem::take(&mut sheet.ctx);
    result
}

fn apply(sheet: &mut Sheet, d: &Diagram, rev: &Revision, index: usize) -> Result<Diagram> {
    // -- what goes
    for item in &rev.remove {
        if !sheet.st.placed.contains_key(item) {
            return Err(Error::Key(format!(
                "remove: no item {} on the sheet",
                pyval::str_repr(item)
            )));
        }
        let unplugged = sheet.ctx.inline.contains_key(item) && sheet.unplug(item)?;
        if !unplugged {
            sheet.drop_all(vec![item.clone()], Vec::new())?;
        }
    }
    for (src, dst) in &rev.remove_lines {
        sheet.remove_line(src, dst)?;
    }
    forget(rev, &mut sheet.ctx);

    // -- what changes
    let mut again: Vec<Equipment> = Vec::new();
    let mut relines: Vec<Connection> = Vec::new();
    for ch in &rev.changes {
        if let Some((eq, lines)) = change(sheet, ch)? {
            again.push(eq);
            for c in lines {
                if !relines.contains(&c) {
                    relines.push(c);
                }
            }
        }
    }

    // -- what comes
    for e in &rev.equipment {
        if sheet.st.placed.contains_key(&e.id) {
            return Err(Error::Value(format!(
                "duplicate id {}: it is on the sheet",
                pyval::str_repr(&e.id)
            )));
        }
    }
    let mut adding: Vec<Equipment> = rev.equipment.clone();
    adding.extend(again);
    let new: HashMap<String, Equipment> = adding.iter().map(|e| (e.id.clone(), e.clone())).collect();
    for c in &rev.connections {
        for i in &c.inline {
            if let Some(id) = &i.id {
                if sheet.st.placed.contains_key(id) {
                    return Err(Error::Value(format!(
                        "duplicate id {}: it is on the sheet",
                        pyval::str_repr(id)
                    )));
                }
            }
        }
    }
    learn(rev, &mut sheet.ctx);
    pair(sheet, rev, &adding)?;
    let all: Vec<Connection> = rev.connections.iter().cloned().chain(relines.iter().cloned()).collect();
    let mut pending: Vec<PLine> = all
        .iter()
        .enumerate()
        .map(|(tag, c)| PLine { tag, c: c.clone() })
        .collect();
    let mut next_tag = pending.len();
    let mut inline_ids: HashMap<usize, Vec<String>> = HashMap::new();
    for (n, c) in all.iter().enumerate() {
        let ids = c
            .inline
            .iter()
            .enumerate()
            .map(|(k, item)| {
                item.id
                    .clone()
                    .unwrap_or_else(|| format!("{LINE_ITEM_PREFIX}R{}_{n}_{k}", index + 1))
            })
            .collect();
        inline_ids.insert(n, ids);
    }
    let known: HashSet<String> = new
        .keys()
        .cloned()
        .chain(inline_ids.values().flatten().cloned())
        .collect();
    for pl in &pending {
        for (e, _) in pl.c.ends() {
            if !sheet.st.placed.contains_key(e) && !known.contains(e) {
                return Err(Error::Key(format!(
                    "{} -> {}: unknown equipment {}",
                    pl.c.src,
                    pl.c.dst,
                    pyval::str_repr(e)
                )));
            }
        }
    }

    struct Run<'a> {
        rev: &'a Revision,
        pending: Vec<PLine>,
        inline_ids: HashMap<usize, Vec<String>>,
        next_tag: usize,
    }

    fn put(sheet: &mut Sheet, run: &Run, c: &PLine, k: usize) -> Result<()> {
        let ids = run.inline_ids[&c.tag].clone();
        if sheet.put_inline(&c.c, k, &ids)? {
            return Ok(());
        }
        let pts = sheet.st.lines[&k].1.points.clone();
        let mut runs: Vec<(f64, Point, Point)> = pts
            .windows(2)
            .map(|w| ((w[1].0 - w[0].0).abs() + (w[1].1 - w[0].1).abs(), w[0], w[1]))
            .collect();
        runs.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap());
        let need = run_needed(&c.c, &sheet.ctx)?;
        for (size, a, b) in runs {
            let axis = if a.1 == b.1 { 0 } else { 1 };
            let (va, vb) = if axis == 0 {
                (a.0 as i64, b.0 as i64)
            } else {
                (a.1 as i64, b.1 as i64)
            };
            let (lo, hi) = (va.min(vb), va.max(vb));
            let by = (need - size as i64).max(2);
            let Some(at) = (lo + 1..hi + 1).find(|&v| sheet.cut(axis, v, by) == v) else {
                continue;
            };
            let state = sheet.st.clone();
            sheet.make_room(axis, at, by);
            if sheet.put_inline(&c.c, k, &ids)? {
                return Ok(());
            }
            sheet.st = state;
        }
        sheet.st.warnings.push(format!(
            "{}: no room on {} -> {} for its inline items",
            run.rev.id, c.c.src, c.c.dst
        ));
        Ok(())
    }

    fn lay_done(sheet: &mut Sheet, run: &mut Run, router: &mut Router, done: Vec<(PLine, Found)>) -> Result<()> {
        for (pl, found) in done {
            run.pending.retain(|p| p.tag != pl.tag);
            let (_, k) = sheet.commit(router, &pl.c, &found)?;
            if !pl.c.inline.is_empty() {
                put(sheet, run, &pl, k)?;
            }
        }
        Ok(())
    }

    fn route_ready(sheet: &mut Sheet, run: &mut Run) -> Result<()> {
        loop {
            let Some(c) = run
                .pending
                .iter()
                .find(|p| p.c.ends().iter().all(|(e, _)| sheet.st.placed.contains_key(*e)))
                .cloned()
            else {
                return Ok(());
            };
            let mut router = sheet.board(None);
            let ((fails, _, _, _), done) = sheet.try_lines(&mut router, std::slice::from_ref(&c))?;
            if fails != 0 {
                split(sheet, run, &c)?;
            } else {
                lay_done(sheet, run, &mut router, done)?;
            }
        }
    }

    fn split(sheet: &mut Sheet, run: &mut Run, c: &PLine) -> Result<()> {
        if c.c
            .ends()
            .iter()
            .any(|(e, _)| ROW_FLAG_PREFIXES.iter().any(|p| e.starts_with(p)))
        {
            return Err(Error::Routing(format!(
                "{}: could not route {} -> {}",
                run.rev.id, c.c.src, c.c.dst
            )));
        }
        let tag = sheet.new_id("");
        let (out, back) = (
            format!("{}{tag}", ROW_FLAG_PREFIXES[0]),
            format!("{}{tag}", ROW_FLAG_PREFIXES[1]),
        );
        let [(s, _), (d, _)] = c.c.ends();
        let flag = |id: &str, text: String| {
            let mut p = Params::new();
            p.insert("text".into(), Value::from(text));
            p.insert("point".into(), Value::from("right"));
            Equipment::new(id, "connector", Some(0), p)
        };
        let flags = [flag(&out, format!("to {d}")), flag(&back, format!("from {s}"))];
        let mut second = c.c.with_src(&format!("{back}.tip"));
        second.recycle = false;
        let halves = [
            PLine {
                tag: run.next_tag,
                c: Connection::new(&c.c.src, &format!("{out}.tail"), &c.c.kind),
            },
            PLine {
                tag: run.next_tag + 1,
                c: second,
            },
        ];
        run.next_tag += 2;
        run.pending.retain(|p| p.tag != c.tag);
        run.pending.extend(halves.iter().cloned());
        run.inline_ids.insert(halves[0].tag, Vec::new());
        let ids = run.inline_ids[&c.tag].clone();
        run.inline_ids.insert(halves[1].tag, ids);
        for (flag, half) in flags.iter().zip(halves) {
            let geo = sheet.ctx.geometry(flag)?;
            let (mut router, done) = place(sheet, flag, geo, &[half], &run.rev.id)?;
            lay_done(sheet, run, &mut router, done)?;
        }
        Ok(())
    }

    let mut run = Run {
        rev,
        pending: std::mem::take(&mut pending),
        inline_ids,
        next_tag,
    };
    next_tag = 0;
    let _ = next_tag;
    route_ready(sheet, &mut run)?;
    let mut todo: Vec<String> = adding.iter().map(|e| e.id.clone()).collect();
    while !todo.is_empty() {
        let links = |sheet: &Sheet, run: &Run, i: &str| -> Vec<PLine> {
            run.pending
                .iter()
                .filter(|p| {
                    p.c.ends().iter().any(|(e, _)| *e == i)
                        && p.c
                            .ends()
                            .iter()
                            .all(|(e, _)| *e == i || sheet.st.placed.contains_key(*e))
                })
                .cloned()
                .collect()
        };
        let mut pick = 0;
        let mut most = links(sheet, &run, &todo[0]).len();
        for (k, i) in todo.iter().enumerate().skip(1) {
            let n = links(sheet, &run, i).len();
            if n > most {
                most = n;
                pick = k;
            }
        }
        let id = todo.remove(pick);
        let eq = new[&id].clone();
        let lines = links(sheet, &run, &id);
        let geo = sheet.ctx.geometry(&eq)?;
        let (mut router, done) = place(sheet, &eq, geo, &lines, &rev.id)?;
        lay_done(sheet, &mut run, &mut router, done)?;
        route_ready(sheet, &mut run)?;
    }
    if let Some(c) = run.pending.first() {
        return Err(Error::Routing(format!(
            "{}: could not route {} -> {}",
            rev.id, c.c.src, c.c.dst
        )));
    }
    let orphans: Vec<String> = sheet
        .orphans
        .iter()
        .filter(|i| sheet.lines_at(i).is_empty())
        .cloned()
        .collect();
    sheet.drop_all(orphans, Vec::new())?;

    let mut info = d.revisions.clone();
    info.push(RevisionInfo {
        id: rev.id.clone(),
        color: revision_color(rev, index),
        note: rev.note.clone(),
    });
    let st = sheet.st.clone();
    Ok(Diagram {
        placed: st.placed.into_values().collect(),
        routes: st.lines.into_values().collect(),
        warnings: st.warnings,
        descs: st.descs,
        line_styles: d.line_styles.clone(),
        uturns: st.uturns,
        rows: st.rows,
        revisions: info,
        added: st.added,
        ghosts: st.ghosts,
        hosts: st.hosts,
        replaced: st.replaced,
        changed: st.changed,
    })
}

/// Added items that replace removed ones (``sheet.reuse``).
fn pair(sheet: &mut Sheet, rev: &Revision, adding: &[Equipment]) -> Result<()> {
    let gone: Vec<Placed> = sheet
        .st
        .ghosts
        .iter()
        .filter(|g| g.rev == rev.id)
        .filter_map(|g| g.placed.clone())
        .collect();
    let lines: Vec<Connection> = sheet
        .st
        .ghosts
        .iter()
        .filter(|g| g.rev == rev.id)
        .filter_map(|g| g.line.as_ref().map(|l| l.0.clone()))
        .collect();
    let neighbours = |sheet: &Sheet, item: &str, conns: &[Connection]| -> HashSet<String> {
        let mut out = HashSet::new();
        let mut todo = vec![item.to_string()];
        let mut seen: HashSet<String> = [item.to_string()].into_iter().collect();
        for _ in 0..2 {
            let mut nxt = Vec::new();
            for i in &todo {
                for c in conns {
                    let ids: Vec<&str> = c.ends().iter().map(|e| e.0).collect();
                    if ids.contains(&i.as_str()) {
                        for o in ids {
                            if seen.insert(o.to_string()) {
                                if o.starts_with(GENERATED_PREFIX) || sheet.bound(o) {
                                    nxt.push(o.to_string());
                                } else {
                                    out.insert(o.to_string());
                                }
                            }
                        }
                    }
                }
            }
            todo = nxt;
        }
        out
    };
    let mut taken: HashSet<String> = HashSet::new();
    let mine_lines: Vec<Connection> = rev.connections.iter().cloned().chain(lines.iter().cloned()).collect();
    for e in adding {
        let geo = sheet.ctx.geometry(e)?;
        if let Some(r) = sheet.reuse.get(&e.id) {
            taken.insert(r.id.clone());
            continue;
        }
        let mine = neighbours(sheet, &e.id, &mine_lines);
        for g in &gone {
            if taken.contains(&g.id) || g.typ != e.typ {
                continue;
            }
            if (g.geo.width, g.geo.height) != (geo.width, geo.height) {
                continue;
            }
            if !mine.is_disjoint(&neighbours(sheet, &g.id, &lines)) {
                sheet.reuse.insert(e.id.clone(), g.clone());
                taken.insert(g.id.clone());
                break;
            }
        }
    }
    Ok(())
}

fn placed_at(eq: &Equipment, geo: &Arc<Geometry>, x: i64, y: i64) -> Placed {
    Placed {
        id: eq.id.clone(),
        typ: eq.typ.clone(),
        level: 0,
        geo: geo.clone(),
        x,
        y,
    }
}

/// Put an added item where its lines route best.
fn place(
    sheet: &mut Sheet,
    eq: &Equipment,
    geo: Arc<Geometry>,
    lines: &[PLine],
    rev: &str,
) -> Result<(Router, Vec<(PLine, Found)>)> {
    let (w, h) = (geo.width, geo.height);
    if lines.is_empty() {
        let x = sheet.st.placed.values().map(|p| bbox(p).2).max().unwrap() + 4;
        let y = sheet.st.placed.values().map(|p| bbox(p).1).min().unwrap();
        sheet.add_item(placed_at(eq, &geo, x, y), &eq.desc);
        return Ok((sheet.board(None), Vec::new()));
    }
    let state = sheet.st.clone();
    for attempt in 0..=ROOM_TRIES {
        if let Some(((x, y), router, done)) = best_spot(sheet, eq, &geo, lines, false)? {
            sheet.add_item(placed_at(eq, &geo, x, y), &eq.desc);
            if let Some(home) = sheet.reuse.get(&eq.id).cloned() {
                if (home.x, home.y) == (x, y) {
                    sheet.st.open.insert(home.id.clone());
                    let swap = (sheet.rev.clone(), eq.id.clone(), home.id.clone());
                    if !sheet.st.replaced.contains(&swap) {
                        sheet.st.replaced.push(swap);
                    }
                }
            }
            return Ok((router, done));
        }
        if attempt == ROOM_TRIES {
            break;
        }
        let c = &lines[0].c;
        let [(s, sp), (d, dp)] = c.ends();
        let (other, port) = if s == eq.id { (d, dp) } else { (s, sp) };
        let port = sheet.port_for(other, port)?;
        let t = resolve_port(&sheet.st.placed[other], &port)?;
        let axis = if t.out_dir == 1 || t.out_dir == 3 { 0 } else { 1 };
        let size = (if axis == 0 { w } else { h }) + 4 + 4 * attempt;
        let sign = if t.out_dir == 0 || t.out_dir == 1 { 1 } else { -1 };
        let at = if axis == 0 { t.first.0 } else { t.first.1 };
        sheet.make_room(axis, at, sign * size);
    }
    sheet.st = state;
    if lines.len() > 1 {
        if let Some(((x, y), router, done)) = best_spot(sheet, eq, &geo, lines, true)? {
            sheet.add_item(placed_at(eq, &geo, x, y), &eq.desc);
            return Ok((router, done));
        }
    }
    Err(Error::Routing(format!("{rev}: found no place for {}", eq.id)))
}

type SpotKey = (i64, i64, bool, i64, f64, f64);

fn key_less(a: &SpotKey, b: &SpotKey) -> bool {
    (a.0, a.1, a.2, a.3)
        .cmp(&(b.0, b.1, b.2, b.3))
        .then(a.4.partial_cmp(&b.4).unwrap())
        .then(a.5.partial_cmp(&b.5).unwrap())
        == std::cmp::Ordering::Less
}

/// The best free spot for ``eq``, with the router and the lines routed there.
#[allow(clippy::type_complexity)]
fn best_spot(
    sheet: &mut Sheet,
    eq: &Equipment,
    geo: &Arc<Geometry>,
    lines: &[PLine],
    partial: bool,
) -> Result<Option<((i64, i64), Router, Vec<(PLine, Found)>)>> {
    let home = sheet.reuse.get(&eq.id).cloned();
    let also = home.as_ref().map(|p| p.id.clone());
    let (near, pipes) = sheet.free_cells(also.as_deref());
    let (w, h) = (geo.width, geo.height);
    let probe = placed_at(eq, geo, 0, 0);
    let base = sheet.board(also.as_deref());
    struct End {
        own: Terminal,
        meets: Vec<Terminal>,
        mine_src: bool,
        need: i64,
    }
    let mut ends: Vec<End> = Vec::new();
    for pl in lines {
        let c = &pl.c;
        let [(s, sp), (d, dp)] = c.ends();
        let mine_src = s == eq.id;
        let (other, oport) = if mine_src { (d, dp) } else { (s, sp) };
        let mut meets: Vec<Terminal> = sheet.terminals(&base, other, oport)?.into_iter().map(|e| e.0).collect();
        if meets.is_empty() {
            let port = sheet.port_for(other, oport)?;
            meets = vec![resolve_port(&sheet.st.placed[other], &port)?];
        }
        meets.truncate(TAPS);
        let need = run_needed(c, &sheet.ctx)?;
        let own = resolve_port(&probe, if mine_src { sp } else { dp })?;
        ends.push(End {
            own,
            meets,
            mine_src,
            need,
        });
    }
    let free = |x: i64, y: i64| {
        for cx in x..=x + w {
            for cy in y..=y + h {
                if near.contains(&(cx, cy)) || pipes.contains(&(cx, cy)) {
                    return false;
                }
            }
        }
        true
    };
    let estimate = |x: i64, y: i64| -> f64 {
        let mut total = 0.0;
        for e in &ends {
            let own = Terminal {
                point: (e.own.point.0 + x as f64, e.own.point.1 + y as f64),
                edge: (e.own.edge.0 + x, e.own.edge.1 + y),
                first: (e.own.first.0 + x, e.own.first.1 + y),
                out_dir: e.own.out_dir,
            };
            let mut best: Option<f64> = None;
            for t in &e.meets {
                let (a, b) = if e.mine_src { (&own, t) } else { (t, &own) };
                let bends = min_bends(a.first, a.out_dir, b.first, (b.out_dir + 2) % 4);
                let dist = (a.first.0 - b.first.0).abs() + (a.first.1 - b.first.1).abs();
                let cost = bends as f64 * BEND_COST + dist as f64 + ((e.need - dist).max(0)) as f64 * BEND_COST;
                best = Some(best.map_or(cost, |b| b.min(cost)));
            }
            total += best.unwrap_or(0.0);
        }
        total
    };
    let mut spots: IndexMap<(i64, i64), f64> = IndexMap::new();
    for e in &ends {
        for t in e.meets.iter().take(4) {
            let (fx, fy) = t.first;
            for x in fx - w - REACH..=fx + REACH {
                for y in fy - h - REACH..=fy + REACH {
                    if !spots.contains_key(&(x, y)) && free(x, y) {
                        spots.insert((x, y), estimate(x, y));
                    }
                }
            }
        }
    }
    let fits_home = |p: &Placed| {
        let (x0, y0, x1, y1) = bbox(p);
        for q in sheet.st.placed.values() {
            let b = bbox(q);
            if b.0 <= x1 && x0 <= b.2 && b.1 <= y1 && y0 <= b.3 {
                return false;
            }
        }
        for x in x0..=x1 {
            for y in y0..=y1 {
                if pipes.contains(&(x, y)) {
                    return false;
                }
            }
        }
        true
    };
    let at_home = home.as_ref().is_some_and(|hp| {
        let mut q = hp.clone();
        q.geo = geo.clone();
        fits_home(&q)
    });
    if at_home {
        let hp = home.as_ref().unwrap();
        let v = estimate(hp.x, hp.y);
        spots.insert((hp.x, hp.y), v);
    }
    if spots.is_empty() {
        return Ok(None);
    }
    let mut ranked: Vec<(i64, i64)> = spots.keys().copied().collect();
    ranked.sort_by(|a, b| {
        spots[a]
            .partial_cmp(&spots[b])
            .unwrap()
            .then(a.1.abs().cmp(&b.1.abs()))
            .then(a.0.cmp(&b.0))
    });
    ranked.truncate(SPOTS);
    if at_home {
        let hp = home.as_ref().unwrap();
        if !ranked.contains(&(hp.x, hp.y)) {
            ranked.insert(0, (hp.x, hp.y));
        }
    }
    let mut best: Option<(SpotKey, (i64, i64), Router, Vec<(PLine, Found)>)> = None;
    for (x, y) in ranked {
        let mut router = base.clone();
        if !block(&mut router, (x, y, x + w, y + h)) {
            continue;
        }
        sheet.st.placed.insert(eq.id.clone(), placed_at(eq, geo, x, y));
        let tried = sheet.try_lines(&mut router, lines);
        sheet.st.placed.shift_remove(&eq.id);
        let (score, done) = tried?;
        let moved = !home.as_ref().is_some_and(|hp| (x, y) == (hp.x, hp.y));
        let (fails, short, turns, cost) = score;
        let key: SpotKey = (fails, short, moved, turns, cost, spots[&(x, y)]);
        if best.as_ref().is_none_or(|b| key_less(&key, &b.0)) {
            best = Some((key, (x, y), router, done));
        }
    }
    let limit = if partial { lines.len() as i64 } else { 1 };
    match best {
        Some((key, at, router, done)) if key.0 < limit => Ok(Some((at, router, done))),
        _ => Ok(None),
    }
}

/// Change an item on the sheet: in place if it fits, else taken off with
/// its lines and returned with them, to be put anew.
fn change(sheet: &mut Sheet, ch: &Change) -> Result<Option<(Equipment, Vec<Connection>)>> {
    if !sheet.st.placed.contains_key(&ch.id) {
        return Err(Error::Key(format!(
            "change: no item {} on the sheet",
            pyval::str_repr(&ch.id)
        )));
    }
    let Some(spec) = sheet.ctx.specs.get(&ch.id).cloned() else {
        return Err(Error::Key(format!(
            "change: {} cannot be changed (no item written)",
            pyval::str_repr(&ch.id)
        )));
    };
    let new = changed_spec(&spec, ch);
    sheet.ctx.specs.insert(ch.id.clone(), new.clone());
    let p = sheet.st.placed[&ch.id].clone();
    let mut used: Vec<String> = Vec::new();
    for k in sheet.lines_at(&ch.id) {
        for (e, port) in sheet.st.lines[&k].0.ends() {
            if e == ch.id && !used.iter().any(|u| u == port) {
                used.push(port.into());
            }
        }
    }
    let mut turns: Vec<(Value, Value)> = Vec::new();
    for m in [false, true] {
        for r in [0, 90, 180, 270] {
            turns.push((Value::from(r), Value::Bool(m)));
        }
    }
    if ch.params.contains_key("rotation") || ch.params.contains_key("mirror") {
        turns = vec![(
            new.params.get("rotation").cloned().unwrap_or(Value::from(0)),
            new.params.get("mirror").cloned().unwrap_or(Value::Bool(false)),
        )];
    }
    for (rot, mir) in turns {
        let mut params = (*new.params).clone();
        params.insert("rotation".into(), rot);
        params.insert("mirror".into(), mir);
        let mut e = new.clone();
        e.params = Arc::new(params);
        let geo = match sheet.ctx.geometry(&e) {
            Ok(g) => g,
            Err(Error::Key(_)) | Err(Error::Value(_)) => continue,
            Err(err) => return Err(err),
        };
        if (geo.width, geo.height) != (p.geo.width, p.geo.height) {
            continue;
        }
        let same = used.iter().all(|q| match (geo.ports.get(q), p.geo.ports.get(q)) {
            (Some(a), Some(b)) => (a.side, a.offset) == (b.side, b.offset),
            _ => false,
        });
        if !same {
            continue;
        }
        let mut q = p.clone();
        q.typ = new.typ.clone();
        q.geo = geo;
        sheet.st.placed.insert(ch.id.clone(), q.clone());
        for k in sheet.lines_at(&ch.id) {
            let (c, r) = sheet.st.lines[&k].clone();
            let mut pts = r.points.clone();
            let [(a, pa), (b, pb)] = c.ends();
            if a == ch.id {
                pts[0] = resolve_port(&q, pa)?.point;
            }
            if b == ch.id {
                let n = pts.len();
                pts[n - 1] = resolve_port(&q, pb)?.point;
            }
            sheet.st.lines.insert(
                k,
                (
                    c,
                    Route {
                        points: simplify(&pts),
                        strict: r.strict,
                        nodes: r.nodes,
                    },
                ),
            );
        }
        mark_changed(sheet, &ch.id, &new.desc);
        return Ok(None);
    }
    let lines: Vec<Connection> = sheet
        .ctx
        .lines
        .iter()
        .filter(|c| c.ends().iter().any(|(e, _)| *e == ch.id))
        .cloned()
        .collect();
    let ports = sheet.ctx.geometry(&new)?.ports.clone();
    for c in &lines {
        for (e, port) in c.ends() {
            if e == ch.id && !ports.contains_key(port) {
                let mut known: Vec<&String> = ports.keys().collect();
                known.sort();
                return Err(Error::Key(format!(
                    "change: {} as changed has no port {} for {} -> {}; known: {}",
                    ch.id,
                    pyval::str_repr(port),
                    c.src,
                    c.dst,
                    pyval::list_repr(&known)
                )));
            }
        }
    }
    if sheet.ctx.inline.contains_key(&ch.id) {
        return Err(Error::Value(format!(
            "change: {} does not fit its line any more; remove the line and add it again instead",
            ch.id
        )));
    }
    let old = sheet.st.placed[&ch.id].clone();
    let before = sheet.st.ghosts.len();
    sheet.drop_all(vec![ch.id.clone()], Vec::new())?;
    let tail: Vec<Ghost> = sheet.st.ghosts.drain(before..).collect();
    sheet.st.ghosts.extend(tail.into_iter().filter(|g| {
        g.placed
            .as_ref()
            .is_some_and(|q| q.id == old.id && q.x == old.x && q.y == old.y && Arc::ptr_eq(&q.geo, &old.geo))
    }));
    let geo = sheet.ctx.geometry(&new)?;
    let (_, _, x1, y1) = bbox(&old);
    if geo.height > old.geo.height {
        sheet.make_room(1, y1 + 1, geo.height - old.geo.height);
    }
    if geo.width > old.geo.width {
        sheet.make_room(0, x1 + 1, geo.width - old.geo.width);
    }
    sheet.reuse.insert(ch.id.clone(), old);
    sheet.st.changed.push((sheet.rev.clone(), ch.id.clone()));
    sheet
        .st
        .replaced
        .push((sheet.rev.clone(), ch.id.clone(), ch.id.clone()));
    Ok(Some((new, lines)))
}

fn mark_changed(sheet: &mut Sheet, item: &str, desc: &str) {
    sheet.st.added.insert(item.into(), sheet.rev.clone());
    sheet.st.changed.push((sheet.rev.clone(), item.into()));
    if !desc.is_empty() {
        sheet.st.descs.insert(item.into(), desc.into());
    } else {
        sheet.st.descs.shift_remove(item);
    }
}

pub fn changed_spec(spec: &Equipment, ch: &Change) -> Equipment {
    let mut e = spec.clone();
    if let Some(t) = &ch.typ {
        if !t.is_empty() {
            e.typ = t.clone();
        }
    }
    let mut params = (*spec.params).clone();
    for (k, v) in &ch.params {
        params.insert(k.clone(), v.clone());
    }
    e.params = Arc::new(params);
    if let Some(d) = &ch.desc {
        e.desc = d.clone();
    }
    e
}

/// The lines ``rev`` removes, or removes an end of, are gone.
fn forget(rev: &Revision, ctx: &mut Context) {
    let gone: HashSet<&String> = rev.remove.iter().collect();
    ctx.lines.retain(|c| {
        !rev.remove_lines.iter().any(|(a, b)| *a == c.src && *b == c.dst)
            && !c.ends().iter().any(|(e, _)| gone.contains(&e.to_string()))
    });
}

/// The items and lines ``rev`` adds (named inline items included).
fn learn(rev: &Revision, ctx: &mut Context) {
    for e in &rev.equipment {
        ctx.specs.insert(e.id.clone(), e.clone());
    }
    for c in &rev.connections {
        for i in &c.inline {
            if let Some(id) = &i.id {
                let mut e = Equipment::new(id, &i.typ, None, i.params.clone());
                e.desc = i.desc.clone();
                ctx.specs.insert(id.clone(), e);
                ctx.inline.insert(id.clone(), (i.port_in.clone(), i.port_out.clone()));
            }
        }
    }
    ctx.lines.extend(rev.connections.iter().cloned());
}

/// What ``apply_revision`` learns from ``rev``, for a revision that is on
/// the sheet already (a frozen sheet).
pub fn account_revision(rev: &Revision, ctx: &mut Context) -> Result<()> {
    forget(rev, ctx);
    for ch in &rev.changes {
        let spec = ctx
            .specs
            .get(&ch.id)
            .cloned()
            .ok_or_else(|| Error::Key(pyval::str_repr(&ch.id)))?;
        ctx.specs.insert(ch.id.clone(), changed_spec(&spec, ch));
    }
    learn(rev, ctx);
    Ok(())
}
