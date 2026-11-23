//! Flowsheet model: add equipment, define connections, build the diagram
//! (see the Python ``model`` module for the reasoning behind every step).

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use indexmap::IndexMap;
use serde_json::Value;

use crate::elements::{CustomSpec, Geometry, Library};
use crate::error::{Error, Result};
use crate::expand::{Expanded, GENERATED_PREFIX, TAP_PREFIX, expand};
use crate::flow::{plan_flow, split_rows};
use crate::geometry::{Point, count_crossings};
use crate::layout::{FLANGED_RUN, FlowLink, Item, Link, Placed, bands, min_gap_x, place, place_y, port_dx};
use crate::levels::{Levels, MAX_ROUNDS, moves, search_levels, start_levels};
use crate::netlist::{ANCHORS, Connection, Equipment, InlineItem, check_id, split_ref};
use crate::pool::pool_map;
use crate::pyval::{self, Params};
use crate::router::{BoxF, DIRS, Route, Router, Terminal, min_bends, needless_uturn, resolve_port};
use crate::style::{LineStyle, Theme, default_line_styles};
use crate::symmetry::{Symmetry, find_units, gap_groups};

pub const FORMAT_VERSION: i64 = 1;
pub const MIN_GAP: i64 = 1;
pub const REROUTE_ROUNDS: usize = 3;
pub const SWAP_ROUNDS: usize = 2;
pub const STUB_ROUNDS: usize = 8;
pub const STUB_REACH: i64 = 12;
pub const QUICK_EXPAND: usize = 3;
pub const BEND_COST: i64 = 6;
pub const ROW_GAP: i64 = 6;
pub const LAYOUTS: [&str; 2] = ["order", "flow"];
pub const ROW_MODES: [&str; 2] = ["auto", "single"];
pub const ELEMENT_KEYS: [&str; 8] = ["id", "type", "level", "params", "ports", "desc", "elevation", "span"];
pub const CONNECTION_KEYS: [&str; 6] = ["from", "to", "type", "inline", "anchor", "recycle"];
pub const TOP_KEYS: [&str; 9] = [
    "version",
    "layout",
    "rows",
    "custom_elements",
    "line_kinds",
    "elements",
    "connections",
    "revisions",
    "frozen",
];

/// A routed sheet.
#[derive(Clone, Debug, Default)]
pub struct Diagram {
    pub placed: Vec<Placed>,
    pub routes: Vec<(Connection, Route)>,
    pub warnings: Vec<String>,
    pub descs: IndexMap<String, String>,
    pub line_styles: IndexMap<String, LineStyle>,
    pub uturns: i64,
    /// a sheet folded into rows: each row's (bottom, top) y
    pub rows: Vec<(f64, f64)>,
}

impl Diagram {
    pub fn bends(&self) -> i64 {
        self.routes.iter().map(|(_, r)| r.bends()).sum()
    }

    pub fn crossings(&self) -> i64 {
        let paths: Vec<&[Point]> = self.routes.iter().map(|(_, r)| r.points.as_slice()).collect();
        count_crossings(&paths) as i64
    }

    pub fn size(&self) -> (i64, i64) {
        size(&self.placed)
    }
}

fn size(placed: &[Placed]) -> (i64, i64) {
    let boxes: Vec<_> = placed.iter().map(|p| p.bbox()).collect();
    (
        boxes.iter().map(|b| b.2).max().unwrap() - boxes.iter().map(|b| b.0).min().unwrap(),
        boxes.iter().map(|b| b.3).max().unwrap() - boxes.iter().map(|b| b.1).min().unwrap(),
    )
}

fn score(d: &Diagram) -> (i64, i64) {
    (d.uturns, d.bends())
}

/// What one build works out once: the geometries built and whether the
/// lines and ports have been checked.
#[derive(Debug, Default)]
pub struct Memo {
    geos: Mutex<HashMap<(String, Option<String>, String, String), Arc<Geometry>>>,
    checked: AtomicBool,
}

/// What a sheet is placed from.
#[derive(Clone)]
pub struct LayoutInput {
    pub net: Expanded,
    pub items: Vec<Item>,
    pub links: Vec<Link>,
    pub flow: Vec<FlowLink>,
    pub free: bool,
    pub order: Option<Vec<bool>>,
    pub floor_x: Vec<i64>,
    pub floor_y: Vec<i64>,
    pub overlap: Vec<bool>,
}

/// The sort key of ``model._natural``: ``P-2`` before ``P-10``.
pub fn natural(text: &str) -> Vec<(u8, i64, String)> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut digits = false;
    let flush = |cur: &mut String, digits: bool, out: &mut Vec<(u8, i64, String)>| {
        if cur.is_empty() {
            return;
        }
        if digits {
            out.push((0, cur.parse().unwrap_or(i64::MAX), String::new()));
        } else {
            out.push((1, 0, cur.clone()));
        }
        cur.clear();
    };
    for ch in text.chars() {
        let d = ch.is_ascii_digit();
        if d != digits {
            flush(&mut cur, digits, &mut out);
            digits = d;
        }
        cur.push(ch);
    }
    flush(&mut cur, digits, &mut out);
    out
}

#[derive(Clone)]
pub struct Flowsheet {
    pub layout: String,
    pub rows: String,
    in_order: bool,
    flag_room: bool,
    quick: bool,
    pub jobs: Option<usize>,
    memo: Option<Arc<Memo>>,
    pub bend_cost: i64,
    pub library: Arc<Library>,
    pub line_styles: IndexMap<String, LineStyle>,
    pub equipment: Vec<Equipment>,
    pub connections: Vec<Connection>,
    /// the JSON lists ``revisions`` / ``frozen`` (not drawn by this port)
    pub revisions: Vec<Value>,
}

/// The way a utility circle points to face a port on each side.
fn aim_for(side: char) -> Option<&'static str> {
    match side {
        'N' => Some("down"),
        'E' => Some("left"),
        'W' => Some("right"),
        _ => None,
    }
}

fn opt_int(v: Option<&Value>) -> Result<Option<i64>> {
    match v {
        None | Some(Value::Null) => Ok(None),
        Some(x) => Ok(Some(pyval::int(x)?)),
    }
}

fn sorted_keys_repr<'a>(keys: impl Iterator<Item = &'a String>) -> String {
    let mut k: Vec<&String> = keys.collect();
    k.sort();
    pyval::list_repr(&k)
}

impl Flowsheet {
    pub fn new(layout: &str) -> Result<Flowsheet> {
        if !LAYOUTS.contains(&layout) {
            return Err(Error::Value(format!(
                "layout must be one of ('order', 'flow'), got {}",
                pyval::str_repr(layout)
            )));
        }
        Ok(Flowsheet {
            layout: layout.into(),
            rows: "auto".into(),
            in_order: false,
            flag_room: false,
            quick: false,
            jobs: None,
            memo: None,
            bend_cost: BEND_COST,
            library: Arc::new(Library::default()),
            line_styles: default_line_styles(),
            equipment: Vec::new(),
            connections: Vec::new(),
            revisions: Vec::new(),
        })
    }

    // -- editing ------------------------------------------------------------------
    /// Ids of all equipment, including inline items that were given an id.
    pub fn ids(&self) -> HashSet<String> {
        let mut out: HashSet<String> = self.equipment.iter().map(|e| e.id.clone()).collect();
        for c in &self.connections {
            for i in &c.inline {
                if let Some(id) = &i.id {
                    out.insert(id.clone());
                }
            }
        }
        out
    }

    fn check_new_id(id: &str, known: &HashSet<String>) -> Result<()> {
        if id.starts_with(GENERATED_PREFIX) {
            return Err(Error::Value(format!(
                "id {}: ids starting with '_' are reserved",
                pyval::str_repr(id)
            )));
        }
        if known.contains(id) {
            return Err(Error::Value(format!("duplicate id {}", pyval::str_repr(id))));
        }
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn add(
        &mut self,
        typ: &str,
        id: Option<&str>,
        level: Option<i64>,
        ports: serde_json::Map<String, Value>,
        desc: &str,
        elevation: Option<i64>,
        span: i64,
        params: Params,
    ) -> Result<()> {
        self.library.check(typ)?;
        if level.is_some_and(|l| l < 0) {
            return Err(Error::Value("level must be >= 0".into()));
        }
        if elevation.is_some_and(|e| e < 0) {
            return Err(Error::Value("elevation must be >= 0".into()));
        }
        if span < 1 {
            return Err(Error::Value("span must be >= 1".into()));
        }
        let id = match id {
            Some(i) if !i.is_empty() => i.to_string(),
            _ => format!("{typ}{}", self.equipment.len() + 1),
        };
        check_id(&id)?;
        Self::check_new_id(&id, &self.ids())?;
        let mut eq = Equipment::new(&id, typ, level, params);
        eq.ports = Arc::new(ports);
        eq.desc = desc.into();
        eq.elevation = elevation;
        eq.span = span;
        self.equipment.push(eq);
        Ok(())
    }

    pub fn connect(
        &mut self,
        src: &str,
        dst: &str,
        kind: Option<&str>,
        inline: &[Value],
        anchor: &str,
        recycle: bool,
    ) -> Result<()> {
        let kind = match kind {
            Some(k) => k.to_string(),
            None => {
                if self.starts_at_utility(src)? {
                    "utility".into()
                } else {
                    "pipe".into()
                }
            }
        };
        if !self.line_styles.contains_key(&kind) {
            return Err(Error::Value(format!(
                "kind must be one of {}, got {}",
                sorted_keys_repr(self.line_styles.keys()),
                pyval::str_repr(&kind)
            )));
        }
        if !ANCHORS.contains(&anchor) {
            return Err(Error::Value(format!(
                "anchor must be one of ('auto', 'src', 'dst'), got {}",
                pyval::str_repr(anchor)
            )));
        }
        let items: Vec<InlineItem> = inline.iter().map(InlineItem::parse).collect::<Result<_>>()?;
        let mut known = self.ids();
        for item in &items {
            self.library.check(&item.typ)?;
            if let Some(id) = &item.id {
                Self::check_new_id(id, &known)?;
                known.insert(id.clone());
            }
        }
        for r in [src, dst] {
            let (i, _) = split_ref(r)?;
            if !known.contains(&i) {
                return Err(Error::Key(format!("unknown equipment {}", pyval::str_repr(&i))));
            }
        }
        self.connections
            .push(Connection::full(src, dst, &kind, items, anchor, recycle));
        for r in [src, dst] {
            let (i, _) = split_ref(r)?;
            self.aim(&i)?;
        }
        Ok(())
    }

    /// Turn an item that aims (a utility circle) to face the port its one
    /// line goes to; with no line or several, it keeps its default.
    fn aim(&mut self, id: &str) -> Result<()> {
        let Some(k) = self.equipment.iter().position(|e| e.id == id) else {
            return Ok(());
        };
        if !self.library.aims(&self.equipment[k].typ) {
            return Ok(());
        }
        let mut ends: Vec<(String, String)> = Vec::new();
        for c in &self.connections {
            let e = c.ends();
            for (a, other) in [(e[0], e[1]), (e[1], e[0])] {
                if a.0 == id {
                    ends.push((other.0.to_string(), other.1.to_string()));
                }
            }
        }
        self.equipment[k].aim = None;
        if ends.len() != 1 {
            return Ok(());
        }
        let (o, port) = &ends[0];
        let Some(other) = self.equipment.iter().find(|e| &e.id == o).cloned() else {
            return Ok(());
        };
        let geo = self.geometry(&other)?;
        if let Some(p) = geo.ports.get(port) {
            self.equipment[k].aim = aim_for(p.side).map(String::from);
        }
        Ok(())
    }

    /// Does the line start at a utility supply (a ``beside`` item)?
    fn starts_at_utility(&self, src: &str) -> Result<bool> {
        let (i, _) = split_ref(src)?;
        let Some(eq) = self.equipment.iter().find(|e| e.id == i) else {
            return Ok(false);
        };
        Ok(self.geometry(eq)?.beside && self.line_styles.contains_key("utility"))
    }

    pub fn add_line_kind(&mut self, name: &str, style: LineStyle) {
        self.line_styles.insert(name.into(), style);
    }

    /// Built geometry of one item (with this flowsheet's library); during a
    /// build, items alike share one, built once.
    pub fn geometry(&self, e: &Equipment) -> Result<Arc<Geometry>> {
        self.raw_geometry(e).map_err(|err| err.context(&e.id))
    }

    fn raw_geometry(&self, e: &Equipment) -> Result<Arc<Geometry>> {
        let Some(memo) = &self.memo else {
            return Ok(Arc::new(e.geometry(&self.library)?));
        };
        let key = (
            e.typ.clone(),
            e.aim.clone(),
            pyval::params_key(&e.params),
            pyval::params_key(&e.ports),
        );
        if let Some(g) = memo.geos.lock().unwrap().get(&key) {
            return Ok(g.clone());
        }
        let g = Arc::new(e.geometry(&self.library)?);
        memo.geos.lock().unwrap().insert(key, g.clone());
        Ok(g)
    }

    fn geos_of(&self, eqs: &[Equipment]) -> Result<HashMap<String, Arc<Geometry>>> {
        let mut out = HashMap::new();
        for e in eqs {
            out.insert(e.id.clone(), self.geometry(e)?);
        }
        Ok(out)
    }

    /// The flat netlist that is laid out (``equipment``: the items in another
    /// order; default: as added).
    pub fn expand(&self, equipment: Option<Vec<Equipment>>) -> Result<Expanded> {
        let eqs = match equipment {
            Some(e) => e,
            None => self.with_levels(None),
        };
        let edges = if self.layout == "flow" {
            let geos = self.geos_of(&eqs)?;
            Some(self.flag_edges(&eqs, &self.connections, &geos))
        } else {
            None
        };
        let build = |e: &Equipment| self.geometry(e);
        expand(&eqs, &self.connections, &build, edges.as_ref())
    }

    // -- build ----------------------------------------------------------------------
    pub fn build(&self, max_expand: usize, compact: bool) -> Result<Diagram> {
        if !self.revisions.is_empty() {
            return Err(Error::Value(
                "revisions are not supported by the Rust port yet; use the Python tool".into(),
            ));
        }
        self.build_base(max_expand, compact)
    }

    fn build_base(&self, max_expand: usize, compact: bool) -> Result<Diagram> {
        let mut fs = self.clone();
        fs.memo = Some(Arc::new(Memo::default()));
        if fs.layout != "flow" {
            return fs.build_swapped(max_expand, compact);
        }
        fs.equipment.sort_by_key(|e| natural(&e.id));
        fs.connections.sort_by(|a, b| {
            (natural(&a.src), natural(&a.dst), &a.kind).cmp(&(natural(&b.src), natural(&b.dst), &b.kind))
        });
        let rows = if fs.rows == "auto" { fs.row_split()? } else { Vec::new() };
        if rows.len() > 1 {
            return fs.build_rows(&rows, max_expand, compact);
        }
        fs.build_swapped(max_expand, compact)
    }

    /// The equipment with every level left out filled in.
    pub fn with_levels(&self, levels: Option<&Levels>) -> Vec<Equipment> {
        if self.equipment.iter().all(|e| e.level.is_some()) {
            return self.equipment.clone();
        }
        let start;
        let lv = match levels {
            Some(l) => l,
            None => {
                start = start_levels(&self.equipment);
                &start
            }
        };
        self.equipment
            .iter()
            .map(|e| {
                let mut e = e.clone();
                if e.level.is_none() {
                    e.level = Some(lv[&e.id]);
                }
                e
            })
            .collect()
    }

    fn level_cost(&self, d: &Diagram) -> i64 {
        let mut length = 0.0f64;
        for (_, r) in &d.routes {
            for w in r.points.windows(2) {
                length += (w[1].0 - w[0].0).abs() + (w[1].1 - w[0].1).abs();
            }
        }
        (d.bends() + d.crossings()) * self.bend_cost + pyval::round_half_even(length)
    }

    fn estimate_cost(&self) -> Result<i64> {
        let (net, items, links, flow, _) = self.prepare()?;
        let free = self.layout == "flow";
        let levels = band_count(&net.equipment);
        let gx = if free {
            vec![MIN_GAP; items.len()]
        } else {
            min_gap_x(&items, &place_y(&items, &vec![MIN_GAP; levels], &links, free)?)
        };
        let overlap: Vec<bool> = items.iter().map(|it| free && it.edge.is_none()).collect();
        let placed = place(
            &items,
            &gx,
            &vec![MIN_GAP; levels],
            &links,
            Some(&overlap),
            &flow,
            free,
            None,
        )?;
        let by: HashMap<&str, &Placed> = placed.iter().map(|p| (p.id.as_str(), p)).collect();
        let mut total = 0;
        for c in &net.connections {
            let [(a, pa), (b, pb)] = c.ends();
            let s = resolve_port(by[a], pa)?;
            let t = resolve_port(by[b], pb)?;
            let bends = min_bends(s.first, s.out_dir, t.first, (t.out_dir + 2) % 4);
            let dist = (t.first.0 - s.first.0).abs() + (t.first.1 - s.first.1).abs();
            total += bends * self.bend_cost + dist + 2;
        }
        Ok(total)
    }

    fn branchy(&self) -> bool {
        let kinds = self.process_kinds();
        let mut out: HashMap<&str, HashSet<&str>> = HashMap::new();
        for c in &self.connections {
            if kinds.contains(&c.kind) {
                let [(s, _), (d, _)] = c.ends();
                out.entry(s).or_default().insert(d);
            }
        }
        out.values().any(|v| v.len() >= 3)
    }

    // -- rows -----------------------------------------------------------------------
    fn row_split(&self) -> Result<Vec<HashSet<String>>> {
        let (eqs, conns) = (&self.equipment, &self.connections);
        let geos = self.geos_of(eqs)?;
        let plan = plan_flow(eqs, conns, &geos, &self.process_kinds(), None);
        let majors: HashSet<usize> = (0..eqs.len())
            .filter(|&i| eqs[i].typ == "column" || eqs[i].span > 1)
            .collect();
        let edge: HashSet<usize> = (0..eqs.len()).filter(|&i| geos[&eqs[i].id].off_sheet).collect();
        let rows = split_rows(&plan, &majors, &edge, 3);
        if rows.len() < 2 {
            return Ok(Vec::new());
        }
        let mut row: IndexMap<String, usize> = IndexMap::new();
        for (k, r) in rows.iter().enumerate() {
            for &i in r {
                row.insert(eqs[i].id.clone(), k);
            }
        }
        let main = self.process_kinds();
        for e in eqs {
            let mut links: Vec<(String, String)> = Vec::new();
            for c in conns {
                let en = c.ends();
                for (a, o) in [(en[0], en[1]), (en[1], en[0])] {
                    if a.0 == e.id {
                        links.push((c.kind.clone(), o.0.to_string()));
                    }
                }
            }
            let mut served: Vec<usize> = links.iter().filter_map(|(_, o)| row.get(o).copied()).collect();
            served.sort();
            served.dedup();
            if !links.is_empty() && links.iter().all(|(k, _)| !main.contains(k)) && served.len() == 1 {
                row.insert(e.id.clone(), served[0]);
            }
        }
        let hosts = self.inline_rows(&row)?;
        for e in eqs {
            let mut others: Vec<String> = Vec::new();
            for c in conns {
                let en = c.ends();
                for (a, o) in [(en[0], en[1]), (en[1], en[0])] {
                    if a.0 == e.id {
                        others.push(o.0.to_string());
                    }
                }
            }
            if !others.is_empty() && others.iter().all(|o| hosts.contains_key(o)) {
                row.insert(e.id.clone(), hosts[&others[0]]);
            }
        }
        let mut out: Vec<HashSet<String>> = vec![HashSet::new(); rows.len()];
        for (i, k) in row {
            out[k].insert(i);
        }
        Ok(out)
    }

    fn inline_rows(&self, row: &IndexMap<String, usize>) -> Result<HashMap<String, usize>> {
        let mut out = HashMap::new();
        for c in &self.connections {
            let [(s, _), (d, _)] = c.ends();
            if !row.contains_key(s) || !row.contains_key(d) {
                continue;
            }
            let de = self
                .equipment
                .iter()
                .find(|e| e.id == d)
                .ok_or_else(|| Error::Key(d.into()))?;
            let off = self.geometry(de)?.off_sheet;
            let side = if c.anchor == "src" || (c.anchor == "auto" && off) {
                s
            } else {
                d
            };
            for item in &c.inline {
                if let Some(id) = &item.id {
                    out.insert(id.clone(), row[side]);
                }
            }
        }
        Ok(out)
    }

    fn build_rows(&self, rows: &[HashSet<String>], max_expand: usize, compact: bool) -> Result<Diagram> {
        let mut row: IndexMap<String, usize> = IndexMap::new();
        for (k, r) in rows.iter().enumerate() {
            for i in r {
                row.insert(i.clone(), k);
            }
        }
        let extra = self.inline_rows(&row)?;
        for (k, v) in extra {
            row.insert(k, v);
        }
        let by_id: HashMap<&str, &Equipment> = self.equipment.iter().map(|e| (e.id.as_str(), e)).collect();
        let mut subs: Vec<Flowsheet> = Vec::new();
        for r in rows {
            let mut sub = self.clone();
            sub.layout = "flow".into();
            sub.rows = "single".into();
            sub.equipment = self.equipment.iter().filter(|e| r.contains(&e.id)).cloned().collect();
            sub.connections = Vec::new();
            subs.push(sub);
        }
        for (n, c) in self.connections.iter().enumerate() {
            let [(s, _), (d, _)] = c.ends();
            let a = *row.get(s).ok_or_else(|| Error::Key(pyval::str_repr(s)))?;
            let b = *row.get(d).ok_or_else(|| Error::Key(pyval::str_repr(d)))?;
            if a == b {
                subs[a].connections.push(c.clone());
                continue;
            }
            let out_id = format!("{GENERATED_PREFIX}RO{n}");
            let in_id = format!("{GENERATED_PREFIX}RI{n}");
            let level_of = |i: &str| by_id.get(i).map_or(Some(0), |e| e.level);
            let flag = |id: &str, level: Option<i64>, text: String| {
                let mut p = Params::new();
                p.insert("text".into(), Value::from(text));
                p.insert("point".into(), Value::from("right"));
                Equipment::new(id, "connector", level, p)
            };
            subs[a].equipment.push(flag(&out_id, level_of(s), format!("to {d}")));
            subs[b].equipment.push(flag(&in_id, level_of(d), format!("from {s}")));
            subs[a]
                .connections
                .push(Connection::new(&c.src, &format!("{out_id}.tail"), &c.kind));
            let mut cin = c.with_src(&format!("{in_id}.tip"));
            cin.recycle = false;
            subs[b].connections.push(cin);
        }
        let diagrams = build_sheets(subs, max_expand, compact, self.jobs)?;
        Ok(stack(&diagrams, &self.line_styles))
    }

    fn build_swapped(&self, max_expand: usize, compact: bool) -> Result<Diagram> {
        Ok(build_sheets(vec![self.clone()], max_expand, compact, self.jobs)?.remove(0))
    }

    /// Placement modes worth a build: (in order, flag room).
    fn modes(&self) -> Result<Vec<(bool, bool)>> {
        let mut orders = vec![false];
        if self.layout == "flow" && self.branchy() {
            orders.push(true);
        }
        let mut rooms = vec![false];
        if self.layout == "flow" {
            let mut probe = self.clone();
            probe.flag_room = true;
            let (.., used) = probe.prepare()?;
            if used {
                rooms.push(true);
            }
        }
        Ok(orders
            .iter()
            .flat_map(|&o| rooms.iter().map(move |&r| (o, r)))
            .collect())
    }

    fn first(&self, mode: (bool, bool), max_expand: usize, compact: bool) -> Result<Diagram> {
        let mut fs = self.clone();
        (fs.in_order, fs.flag_room) = mode;
        fs.build_core(max_expand, compact, None)
    }

    /// Re-sort the lines on swappable ports and rebuild while it costs no more.
    fn swap(&self, mode: (bool, bool), best: Diagram, max_expand: usize, compact: bool) -> Result<Diagram> {
        let mut fs = self.clone();
        (fs.in_order, fs.flag_room) = mode;
        let mut best = best;
        for _ in 0..SWAP_ROUNDS {
            let Some(conns) = fs.reassign(&best)? else { break };
            fs.connections = conns;
            let trial = match fs.build_core(max_expand, compact, None) {
                Ok(d) => d,
                Err(Error::Routing(_)) => break,
                Err(e) => return Err(e),
            };
            if fs.cost(&trial) > fs.cost(&best) {
                break;
            }
            best = trial;
        }
        Ok(best)
    }

    fn cost(&self, d: &Diagram) -> (i64, i64) {
        if self.layout != "flow" {
            return score(d);
        }
        let (w, h) = d.size();
        (d.uturns, (d.bends() + d.crossings()) * self.bend_cost + w + h)
    }

    /// The connections with the lines on every swappable port group sorted
    /// along their face; None if that changes nothing.
    fn reassign(&self, d: &Diagram) -> Result<Option<Vec<Connection>>> {
        let placed: HashMap<&str, &Placed> = d.placed.iter().map(|p| (p.id.as_str(), p)).collect();
        let mut far: HashMap<(String, String), Point> = HashMap::new();
        for (c, r) in &d.routes {
            let [(a, pa), (b, pb)] = c.ends();
            let pts = &r.points;
            far.entry((a.into(), pa.into())).or_insert(pts[2.min(pts.len() - 1)]);
            far.entry((b.into(), pb.into()))
                .or_insert(pts[(pts.len() as i64 - 3).max(0) as usize]);
        }
        let mut uses: IndexMap<(String, String), Vec<(usize, usize)>> = IndexMap::new();
        for (k, c) in self.connections.iter().enumerate() {
            for (end, r) in c.ends().iter().enumerate() {
                uses.entry((r.0.into(), r.1.into())).or_default().push((k, end));
            }
        }
        let mut new = self.connections.clone();
        let mut changed = false;
        for e in &self.equipment {
            let Some(p) = placed.get(e.id.as_str()) else { continue };
            if p.geo.swappable.is_empty() {
                continue;
            }
            let ports = &p.geo.ports;
            for group in &p.geo.swappable {
                let mut slot_name: IndexMap<(char, i64), String> = IndexMap::new();
                for name in group {
                    let q = ports[name];
                    slot_name.entry((q.side, q.offset)).or_insert_with(|| name.clone());
                }
                let mut lines: Vec<(Point, (char, i64), usize, usize)> = Vec::new();
                for ((eq, name), us) in &uses {
                    let port = if *eq == e.id { ports.get(name) } else { None };
                    let Some(port) = port else { continue };
                    let slot = (port.side, port.offset);
                    let key = (eq.clone(), name.clone());
                    if !slot_name.contains_key(&slot) || us.len() != 1 || !far.contains_key(&key) {
                        continue;
                    }
                    lines.push((far[&key], slot, us[0].0, us[0].1));
                }
                if lines.len() < 2 {
                    continue;
                }
                let side = lines[0].1.0;
                let along = if side == 'N' || side == 'S' { 0 } else { 1 };
                let base = match side {
                    'N' => (p.y + p.geo.height) as f64,
                    'S' => p.y as f64,
                    'E' => (p.x + p.geo.width) as f64,
                    _ => p.x as f64,
                };
                let origin = if along == 0 { p.x } else { p.y } as f64;
                let total: i64 = lines.iter().map(|l| l.1.1).sum();
                let mid = origin + total as f64 / lines.len() as f64;
                let get = |pt: Point, k: usize| if k == 0 { pt.0 } else { pt.1 };
                let key = |l: &(Point, (char, i64), usize, usize)| -> (f64, f64) {
                    let pt = l.0;
                    let dist = (get(pt, 1 - along) - base).abs();
                    (get(pt, along), if get(pt, along) > mid { -dist } else { dist })
                };
                let mut slots: Vec<(char, i64)> = lines.iter().map(|l| l.1).collect();
                slots.sort_by_key(|s| s.1);
                let mut sorted_lines = lines.clone();
                sorted_lines.sort_by(|a, b| key(a).partial_cmp(&key(b)).unwrap());
                for (line, target) in sorted_lines.iter().zip(slots) {
                    let (_, slot, k, end) = *line;
                    if target == slot {
                        continue;
                    }
                    let r = format!("{}.{}", e.id, slot_name[&target]);
                    new[k] = if end == 0 {
                        new[k].with_src(&r)
                    } else {
                        new[k].with_dst(&r)
                    };
                    changed = true;
                }
            }
        }
        Ok(if changed { Some(new) } else { None })
    }

    /// Lay out and route with the ports as named (see the Python ``_build``).
    fn build_core(&self, max_expand: usize, compact: bool, lay: Option<LayoutInput>) -> Result<Diagram> {
        let lay = match lay {
            Some(l) => l,
            None => self.layout_input()?,
        };
        let LayoutInput {
            net,
            mut items,
            links,
            flow,
            free,
            order,
            floor_x,
            floor_y,
            overlap,
        } = lay;
        let sym = find_units(&net);
        let weight = if free { self.bend_cost } else { 0 };
        let mut seen: HashMap<(bool, Vec<(i64, i64)>), Option<Diagram>> = HashMap::new();
        let mut first_fail: HashMap<(bool, Vec<(i64, i64)>), Vec<String>> = HashMap::new();

        #[allow(clippy::too_many_arguments)]
        fn attempt(
            fs: &Flowsheet,
            net: &Expanded,
            items: &[Item],
            links: &[Link],
            flow: &[FlowLink],
            free: bool,
            order: Option<&[bool]>,
            overlap: &[bool],
            sym: &Symmetry,
            weight: i64,
            gx: &[i64],
            gy: &[i64],
            relaxed: bool,
            best: Option<&Diagram>,
            seen: &mut HashMap<(bool, Vec<(i64, i64)>), Option<Diagram>>,
            fails: &mut HashMap<(bool, Vec<(i64, i64)>), Vec<String>>,
        ) -> Result<(Option<Diagram>, Vec<String>)> {
            let placed = place(items, gx, gy, links, Some(overlap), flow, free, order)?;
            let key = (relaxed, placed.iter().map(|p| (p.x, p.y)).collect::<Vec<_>>());
            if !seen.contains_key(&key) {
                let mut bound = None;
                if let Some(best) = best {
                    let (turns, total) = fs.cost(best);
                    let sz = if weight != 0 {
                        let (w, h) = size(&placed);
                        w + h
                    } else {
                        0
                    };
                    bound = Some((turns, pyval::floordiv(total - sz, weight.max(1))));
                }
                let (d, f) = fs.route_all(net, &placed, relaxed, bound, Some(sym))?;
                seen.insert(key.clone(), d);
                fails.insert(key.clone(), f);
            }
            Ok((seen[&key].clone(), fails[&key].clone()))
        }

        let spread = |k: i64| -> (Vec<i64>, Vec<i64>) {
            (
                floor_x.iter().map(|f| f + k).collect(),
                floor_y.iter().map(|f| f + k).collect(),
            )
        };
        let order_ref = order.as_deref();
        macro_rules! try_at {
            ($gx:expr, $gy:expr, $relaxed:expr, $best:expr) => {
                attempt(
                    self,
                    &net,
                    &items,
                    &links,
                    &flow,
                    free,
                    order_ref,
                    &overlap,
                    &sym,
                    weight,
                    $gx,
                    $gy,
                    $relaxed,
                    $best,
                    &mut seen,
                    &mut first_fail,
                )?
            };
        }

        let mut best: Option<Diagram> = None;
        let mut failures: Vec<String> = Vec::new();
        let mut relaxed = false;
        let (mut gx, mut gy) = spread(0);
        for k in 0..max_expand as i64 {
            let (sx, sy) = spread(k);
            let (trial, fails) = try_at!(&sx, &sy, false, None);
            if failures.is_empty() {
                failures = fails;
            }
            if let Some(t) = trial {
                if best.as_ref().is_none_or(|b| self.cost(&t) < self.cost(b)) {
                    best = Some(t);
                    (gx, gy) = (sx, sy);
                }
            }
            if best.is_some() && (weight == 0 || self.quick) {
                break;
            }
        }
        if best.is_none() {
            relaxed = true;
            for k in 0..max_expand as i64 {
                let (sx, sy) = spread(k);
                let (trial, fails) = try_at!(&sx, &sy, true, None);
                if let Some(t) = trial {
                    if best.as_ref().is_none_or(|b| self.cost(&t) < self.cost(b)) {
                        best = Some(t);
                        (gx, gy) = (sx, sy);
                    }
                }
                if failures.is_empty() {
                    failures = fails;
                }
            }
        }
        let mut k = max_expand as i64;
        while best.is_none() && k < 4 * max_expand as i64 && !self.quick {
            let (sx, sy) = spread(k);
            let (trial, _) = try_at!(&sx, &sy, true, None);
            if trial.is_some() {
                best = trial;
                (gx, gy) = (sx, sy);
            }
            k += 1;
        }
        let Some(mut best) = best else {
            return Err(Error::Routing(format!("could not route: {}", failures.join("; "))));
        };
        if !compact {
            return Ok(best);
        }

        // squeeze every gap: straight to its floor if that holds, else the
        // smallest squeeze that holds (bisection)
        let x_groups = gap_groups(&sym);
        let y_groups: Vec<Vec<usize>> = (0..gy.len()).map(|i| vec![i]).collect();
        for axis in 0..2 {
            let groups = if axis == 0 { &x_groups } else { &y_groups };
            for group in groups {
                let floor = if axis == 0 { &floor_x } else { &floor_y };
                let start: Vec<i64> = group.iter().map(|&i| if axis == 0 { gx[i] } else { gy[i] }).collect();
                let mut ok = 0;
                let mut bad = group.iter().zip(&start).map(|(&i, &g)| g - floor[i]).min().unwrap() + 1;
                if bad <= 1 {
                    continue;
                }
                let mut step = bad - 1;
                while bad - ok > 1 {
                    for (&i, &g) in group.iter().zip(&start) {
                        if axis == 0 {
                            gx[i] = g - step;
                        } else {
                            gy[i] = g - step;
                        }
                    }
                    let (trial, _) = try_at!(&gx, &gy, relaxed, Some(&best));
                    match trial {
                        Some(t) if self.cost(&t) <= self.cost(&best) => {
                            best = t;
                            ok = step;
                        }
                        _ => bad = step,
                    }
                    step = (ok + bad) / 2;
                }
                for (&i, &g) in group.iter().zip(&start) {
                    if axis == 0 {
                        gx[i] = g - ok;
                    } else {
                        gy[i] = g - ok;
                    }
                }
            }
        }

        if free {
            let fixed: HashSet<String> = self
                .equipment
                .iter()
                .filter(|e| e.elevation.is_some())
                .map(|e| e.id.clone())
                .collect();
            for _ in 0..STUB_ROUNDS {
                let shifts = stub_shifts(&net, &items, &best, &gy, &fixed)?;
                let mut taken = false;
                for change in shifts {
                    let undo = change.apply(&mut items, &mut gx);
                    let (trial, _) = try_at!(&gx, &gy, relaxed, Some(&best));
                    if let Some(t) = trial {
                        if self.cost(&t) < self.cost(&best) && self.level_cost(&t) <= self.level_cost(&best) {
                            best = t;
                            taken = true;
                            break;
                        }
                    }
                    undo.revert(&mut items, &mut gx);
                }
                if !taken {
                    break;
                }
            }
        }
        Ok(best)
    }

    /// What ``build_core`` places the sheet from, with the tightest gaps.
    fn layout_input(&self) -> Result<LayoutInput> {
        let (net, items, links, flow, _) = self.prepare()?;
        let free = self.layout == "flow";
        let mut order = None;
        if self.in_order {
            let mut back: HashSet<String> = HashSet::new();
            for (ci, c) in self.connections.iter().enumerate() {
                if c.recycle {
                    for (k, item) in c.inline.iter().enumerate() {
                        back.insert(
                            item.id
                                .clone()
                                .unwrap_or_else(|| format!("{GENERATED_PREFIX}L{ci}_{k}")),
                        );
                    }
                }
            }
            order = Some(
                net.equipment
                    .iter()
                    .map(|e| !(e.id.starts_with(TAP_PREFIX) || back.contains(&e.id)))
                    .collect(),
            );
        }
        let levels = band_count(&net.equipment);
        let mut floor_x = min_gap_x(&items, &place_y(&items, &vec![MIN_GAP; levels], &links, free)?);
        if free {
            floor_x = vec![MIN_GAP; items.len()];
        }
        let floor_y = vec![MIN_GAP; levels];
        let overlap = items.iter().map(|it| free && it.edge.is_none()).collect();
        Ok(LayoutInput {
            net,
            items,
            links,
            flow,
            free,
            order,
            floor_x,
            floor_y,
            overlap,
        })
    }

    /// A cost ``level_cost`` cannot go below on the first ``spacings``
    /// uniform spacings.
    fn level_floor(&self, spacings: usize, lay: &LayoutInput) -> Result<i64> {
        let mut best: Option<i64> = None;
        for k in 0..spacings as i64 {
            let gx: Vec<i64> = lay.floor_x.iter().map(|f| f + k).collect();
            let gy: Vec<i64> = lay.floor_y.iter().map(|f| f + k).collect();
            let placed = place(
                &lay.items,
                &gx,
                &gy,
                &lay.links,
                Some(&lay.overlap),
                &lay.flow,
                lay.free,
                lay.order.as_deref(),
            )?;
            let by_id: HashMap<&str, &Placed> = placed.iter().map(|p| (p.id.as_str(), p)).collect();
            let mut bends = 0;
            let mut length = 0.0;
            for c in &lay.net.connections {
                let [(a, pa), (b, pb)] = c.ends();
                let s = resolve_port(by_id[a], pa)?;
                let t = resolve_port(by_id[b], pb)?;
                let (bk, lk) = line_floor(&s, &t);
                bends += bk;
                length += lk;
            }
            let cost = bends * self.bend_cost + pyval::round_half_even(length);
            best = Some(best.map_or(cost, |b| b.min(cost)));
        }
        Ok(best.unwrap())
    }

    /// Layout input: the expanded netlist in left-to-right order, the items,
    /// facing-port links, main-flow arrows, and whether a flag got room.
    #[allow(clippy::type_complexity)]
    fn prepare(&self) -> Result<(Expanded, Vec<Item>, Vec<Link>, Vec<FlowLink>, bool)> {
        if self.equipment.is_empty() {
            return Err(Error::Value("flowsheet is empty".into()));
        }
        let mut flow = Vec::new();
        let mut flags: HashMap<String, String> = HashMap::new();
        let mut net;
        if self.layout != "flow" {
            net = self.expand(None)?;
            validate(&net)?;
        } else {
            let checked = self.memo.as_ref().is_some_and(|m| m.checked.load(Ordering::Relaxed));
            if !checked {
                validate(&self.expand(None)?)?;
                if let Some(m) = &self.memo {
                    m.checked.store(true, Ordering::Relaxed);
                }
            }
            let eqs = self.with_levels(None);
            let geos = self.geos_of(&eqs)?;
            let first = plan_flow(&eqs, &self.connections, &geos, &self.process_kinds(), None);
            let pos: HashMap<&str, usize> = first
                .order
                .iter()
                .enumerate()
                .map(|(k, &i)| (eqs[i].id.as_str(), k))
                .collect();
            let mut sorted = eqs.clone();
            sorted.sort_by_key(|e| pos[e.id.as_str()]);
            net = self.expand(Some(sorted))?;
            (flow, flags) = self.apply_flow(&mut net);
        }
        let ticks: HashSet<String> = self
            .line_styles
            .iter()
            .filter(|(_, s)| s.process)
            .map(|(k, _)| k.clone())
            .collect();
        let links = links_of(&net, &ticks);
        settle_tees(&mut net, &links)?;
        let mut used = false;
        if self.layout == "flow" {
            used = settle_lifts(&mut net, &flags, &links, self.flag_room)?;
        }
        let items = items_of(&net, Some(&flags))?;
        Ok((net, items, links, flow, used))
    }

    fn process_kinds(&self) -> HashSet<String> {
        self.line_styles
            .iter()
            .filter(|(_, s)| s.process && s.main)
            .map(|(k, _)| k.clone())
            .collect()
    }

    /// Reorder ``net`` by the direction of flow.
    fn apply_flow(&self, net: &mut Expanded) -> (Vec<FlowLink>, HashMap<String, String>) {
        let plan = plan_flow(
            &net.equipment,
            &net.connections,
            &net.geos,
            &self.process_kinds(),
            Some(&net.owners),
        );
        let old = std::mem::take(&mut net.equipment);
        net.equipment = plan.order.iter().map(|&i| old[i].clone()).collect();
        let new: HashMap<&str, usize> = net
            .equipment
            .iter()
            .enumerate()
            .map(|(j, e)| (e.id.as_str(), j))
            .collect();
        let mut flow = Vec::new();
        for arrow in &plan.forward {
            let (a, b) = (&old[arrow.a].id, &old[arrow.b].id);
            let (ga, gb) = (&net.geos[a], &net.geos[b]);
            let (pa, pb) = (ga.ports[&arrow.a_port], gb.ports[&arrow.b_port]);
            flow.push(FlowLink {
                u: new[a.as_str()],
                u_dx: port_dx(ga, pa.side, pa.offset),
                v: new[b.as_str()],
                v_dx: port_dx(gb, pb.side, pb.offset),
            });
        }
        let flags = self.flag_edges(&net.equipment, &net.connections, &net.geos);
        (flow, flags)
    }

    /// Where each off-sheet flag goes (flow layout).
    fn flag_edges(
        &self,
        eqs: &[Equipment],
        conns: &[Connection],
        geos: &HashMap<String, Arc<Geometry>>,
    ) -> HashMap<String, String> {
        let mut faces: HashMap<&str, char> = HashMap::new();
        let mut kinds: HashMap<&str, HashSet<&str>> = HashMap::new();
        for c in conns {
            for (eq, port) in c.ends() {
                if let Some(g) = geos.get(eq) {
                    if g.off_sheet {
                        if let Some(p) = g.ports.get(port) {
                            faces.entry(eq).or_insert(p.side);
                            kinds.entry(eq).or_default().insert(&c.kind);
                        }
                    }
                }
            }
        }
        let mut flags = HashMap::new();
        for e in eqs {
            let g = &geos[&e.id];
            if !g.off_sheet {
                continue;
            }
            let local = if g.beside {
                true
            } else {
                match e.param("local") {
                    None => self.auto_local(kinds.get(e.id.as_str())),
                    Some(Value::String(s)) if s == "auto" => self.auto_local(kinds.get(e.id.as_str())),
                    Some(v) => pyval::truthy(v),
                }
            };
            let v = if local {
                "local"
            } else if faces.get(e.id.as_str()) == Some(&'W') {
                "right"
            } else {
                "left"
            };
            flags.insert(e.id.clone(), v.to_string());
        }
        flags
    }

    fn auto_local(&self, used: Option<&HashSet<&str>>) -> bool {
        match used {
            None => false,
            Some(u) => !u.is_empty() && !u.iter().any(|k| self.line_styles[*k].main),
        }
    }

    /// Process lines from a top nozzle straight up into a bottom nozzle of an
    /// item on a level above, less than ``FLANGED_RUN`` cells apart.
    fn stacked_too_close(&self, net: &Expanded, placed: &[Placed]) -> Result<Vec<String>> {
        if !placed.iter().any(|p| p.geo.nozzles) {
            return Ok(Vec::new());
        }
        let by_id: HashMap<&str, &Placed> = placed.iter().map(|p| (p.id.as_str(), p)).collect();
        let eqs: HashMap<&str, &Equipment> = net.equipment.iter().map(|e| (e.id.as_str(), e)).collect();
        let mut out = Vec::new();
        for c in &net.connections {
            if !self.line_styles[&c.kind].process {
                continue;
            }
            let [(a, pa), (b, pb)] = c.ends();
            if !(by_id[a].geo.nozzles && by_id[b].geo.nozzles) {
                continue;
            }
            let s = resolve_port(by_id[a], pa)?;
            let t = resolve_port(by_id[b], pb)?;
            let dirs = [s.out_dir.min(t.out_dir), s.out_dir.max(t.out_dir)];
            if dirs != [0, 2] || s.point.0 != t.point.0 {
                continue;
            }
            let (lo, hi) = if s.out_dir == 2 {
                ((b, t), (a, s))
            } else {
                ((a, s), (b, t))
            };
            let (low, up) = (eqs[lo.0], eqs[hi.0]);
            if up.tier() < low.tier() + low.span {
                continue;
            }
            let gap = hi.1.point.1 - lo.1.point.1;
            if 0.0 < gap && gap < FLANGED_RUN as f64 {
                out.push(format!("{} -> {}", c.src, c.dst));
            }
        }
        Ok(out)
    }

    /// Route every line on one placement, with rip-up and reroute.
    fn route_all(
        &self,
        net: &Expanded,
        placed: &[Placed],
        relaxed: bool,
        bound: Option<(i64, i64)>,
        sym: Option<&Symmetry>,
    ) -> Result<(Option<Diagram>, Vec<String>)> {
        let close = self.stacked_too_close(net, placed)?;
        if !close.is_empty() {
            return Ok((
                None,
                close
                    .iter()
                    .map(|c| format!("{c}: nozzles stacked closer than {FLANGED_RUN}"))
                    .collect(),
            ));
        }
        let mut order: Vec<usize> = (0..net.connections.len()).collect();
        if self.layout == "flow" {
            let pos: HashMap<&str, i64> = net
                .equipment
                .iter()
                .enumerate()
                .map(|(i, e)| (e.id.as_str(), i as i64))
                .collect();
            order.sort_by_key(|&ci| {
                let [(a, _), (b, _)] = net.connections[ci].ends();
                let (a, b) = (pos[a], pos[b]);
                (-a.min(b), -a.max(b), ci)
            });
        }
        let mut best: Option<Diagram> = None;
        let mut first_failures: Option<Vec<String>> = None;
        let rounds = if bound.is_none() { REROUTE_ROUNDS } else { 1 };
        let mut alone: HashMap<usize, bool> = HashMap::new();
        for _ in 0..rounds + 1 {
            let (d, failures, problems) = self.route_order(net, placed, relaxed, &order, bound, sym, &mut alone)?;
            if let Some(d) = d {
                if best.as_ref().is_none_or(|b| score(&d) < score(b)) {
                    let done = d.uturns == 0;
                    best = Some(d);
                    if done {
                        break;
                    }
                }
            }
            if first_failures.is_none() {
                first_failures = Some(failures);
            }
            if problems.is_empty() || !problems.iter().all(|i| *alone.get(i).unwrap_or(&true)) {
                break;
            }
            let mut again = problems.clone();
            again.extend(order.iter().copied().filter(|i| !problems.contains(i)));
            if again == order {
                break;
            }
            order = again;
        }
        if best.is_some() {
            return Ok((best, Vec::new()));
        }
        Ok((None, first_failures.unwrap_or_default()))
    }

    #[allow(clippy::too_many_arguments, clippy::type_complexity)]
    fn route_order(
        &self,
        net: &Expanded,
        placed: &[Placed],
        relaxed: bool,
        order: &[usize],
        bound: Option<(i64, i64)>,
        sym: Option<&Symmetry>,
        alone: &mut HashMap<usize, bool>,
    ) -> Result<(Option<Diagram>, Vec<String>, Vec<usize>)> {
        let by_id: HashMap<&str, &Placed> = placed.iter().map(|p| (p.id.as_str(), p)).collect();
        let mut router = Router::new(placed);
        let mut terms = Vec::new();
        for c in &net.connections {
            for (eq, port) in c.ends() {
                terms.push(resolve_port(by_id[eq], port)?);
            }
        }
        router.reserve(&terms);
        let mut routes: HashMap<usize, Route> = HashMap::new();
        let mut shapes: HashMap<usize, (usize, Route)> = HashMap::new();
        let mut ready: HashMap<usize, Route> = HashMap::new();
        let mut members: IndexMap<usize, Vec<usize>> = IndexMap::new();
        if let Some(s) = sym {
            if s.repeated() {
                for (cj, &k) in s.line_class.iter().enumerate() {
                    members.entry(k).or_default().push(cj);
                }
            }
        }
        let offset = |p: &Placed, q: &Placed| (p.x - q.x, p.y - q.y);
        let mut turned: Vec<usize> = Vec::new();
        let mut failures: Vec<String> = Vec::new();
        let mut failed: Vec<usize> = Vec::new();
        let mut bends = 0;
        for &ci in order {
            let c = &net.connections[ci];
            let [(si, sp), (di, dp)] = c.ends();
            let src = resolve_port(by_id[si], sp)?;
            let dst = resolve_port(by_id[di], dp)?;
            let cls = sym.map_or(ci, |s| s.line_class[ci]);
            let mut r = ready.remove(&ci);
            if r.is_none() && !shapes.contains_key(&cls) {
                let mut copies = vec![ci];
                for &cj in members.get(&cls).map(|v| v.as_slice()).unwrap_or(&[]) {
                    if cj == ci || routes.contains_key(&cj) {
                        continue;
                    }
                    let [(sj, _), (dj, _)] = net.connections[cj].ends();
                    let shift = offset(by_id[sj], by_id[si]);
                    if shift == offset(by_id[dj], by_id[di]) {
                        copies.push(cj);
                    }
                }
                if copies.len() > 1 {
                    let mut ends = Vec::new();
                    for &cj in &copies {
                        let [(sj, spj), (dj, dpj)] = net.connections[cj].ends();
                        ends.push((
                            resolve_port(by_id[sj], spj)?,
                            resolve_port(by_id[dj], dpj)?,
                            offset(by_id[sj], by_id[si]),
                        ));
                    }
                    if let Some(together) = router.route_together(&ends, relaxed) {
                        let mut it = together.into_iter();
                        r = it.next();
                        for (cj, rr) in copies[1..].iter().zip(it) {
                            ready.insert(*cj, rr);
                        }
                    }
                }
            } else if r.is_none() {
                let (cm, model) = shapes[&cls].clone();
                let [(msi, _), (mdi, _)] = net.connections[cm].ends();
                let shift = offset(by_id[si], by_id[msi]);
                if shift == offset(by_id[di], by_id[mdi]) {
                    r = router.replay(&src, &dst, &model, shift);
                }
            }
            if r.is_none() {
                r = router.route(&src, &dst, relaxed, true);
            }
            if let Some(rr) = &r {
                shapes.entry(cls).or_insert_with(|| (ci, rr.clone()));
            }
            let Some(r) = r else {
                failures.push(format!("{} -> {}", c.src, c.dst));
                failed.push(ci);
                if let std::collections::hash_map::Entry::Vacant(e) = alone.entry(ci) {
                    let laid = !routes.is_empty() || !ready.is_empty();
                    e.insert(laid && router.routes_alone(&src, &dst, relaxed));
                }
                let doomed = !alone[&ci];
                if bound.is_some() || (doomed && self.quick) {
                    return Ok((None, failures, failed));
                }
                continue;
            };
            if !r.strict {
                turned.push(ci);
            }
            bends += r.bends();
            routes.insert(ci, r);
            if let Some(b) = bound {
                if (turned.len() as i64, bends) > b {
                    return Ok((None, vec!["worse than the best layout so far".into()], failed));
                }
            }
        }
        if !failures.is_empty() {
            return Ok((None, failures, failed));
        }
        let boxes: Vec<BoxF> = placed
            .iter()
            .map(|p| {
                let b = p.bbox();
                (b.0 as f64, b.1 as f64, b.2 as f64, b.3 as f64)
            })
            .collect();
        let eqs: HashMap<&str, &Equipment> = net.equipment.iter().map(|e| (e.id.as_str(), e)).collect();
        let mut sorted_turned = turned.clone();
        sorted_turned.sort();
        let mut warnings = Vec::new();
        for ci in sorted_turned {
            let c = &net.connections[ci];
            let [(a, pa), (b, pb)] = c.ends();
            let out = by_id[a].geo.ports[pa].side;
            let into = by_id[b].geo.ports[pb].side;
            if needless_uturn(&routes[&ci].points, &boxes) && !forced_back(eqs[a], out, eqs[b], into) {
                warnings.push(format!("{} -> {}: U-turn with nothing in the way", c.src, c.dst));
            }
        }
        let descs: IndexMap<String, String> = net
            .equipment
            .iter()
            .filter(|e| !e.desc.is_empty())
            .map(|e| (e.id.clone(), e.desc.clone()))
            .collect();
        let mut keys: Vec<usize> = routes.keys().copied().collect();
        keys.sort();
        let diagram = Diagram {
            placed: placed.to_vec(),
            routes: keys
                .iter()
                .map(|ci| (net.connections[*ci].clone(), routes[ci].clone()))
                .collect(),
            warnings,
            descs,
            line_styles: self.line_styles.clone(),
            uturns: turned.len() as i64,
            rows: Vec::new(),
        };
        Ok((Some(diagram), Vec::new(), turned))
    }

    // -- io ---------------------------------------------------------------------------
    pub fn from_json(text: &str) -> Result<Flowsheet> {
        let text = text.strip_prefix('\u{feff}').unwrap_or(text);
        let v: Value = serde_json::from_str(text).map_err(|e| Error::Value(format!("invalid JSON: {e}")))?;
        let Value::Object(d) = v else {
            return Err(Error::Value("the flowsheet must be a JSON object".into()));
        };
        Flowsheet::from_dict(&d)
    }

    pub fn from_dict(d: &serde_json::Map<String, Value>) -> Result<Flowsheet> {
        let version = d.get("version").cloned().unwrap_or(Value::from(FORMAT_VERSION));
        let one = match &version {
            Value::Number(n) => n.as_f64() == Some(FORMAT_VERSION as f64),
            Value::Bool(b) => *b,
            _ => false,
        };
        if !one {
            return Err(Error::Value(format!(
                "unsupported flowsheet format version {} (this tool reads version {FORMAT_VERSION})",
                pyval::repr(&version)
            )));
        }
        let unknown: Vec<&String> = d.keys().filter(|k| !TOP_KEYS.contains(&k.as_str())).collect();
        if !unknown.is_empty() {
            return Err(Error::Value(format!(
                "unknown top-level keys {}",
                sorted_keys_repr(unknown.into_iter())
            )));
        }
        let layout = d.get("layout").cloned().unwrap_or(Value::from("flow"));
        let mut fs = match layout.as_str() {
            Some(l) if LAYOUTS.contains(&l) => Flowsheet::new(l)?,
            _ => {
                return Err(Error::Value(format!(
                    "layout must be one of ('order', 'flow'), got {}",
                    pyval::repr(&layout)
                )));
            }
        };
        let rows = d.get("rows").cloned().unwrap_or(Value::from("auto"));
        match rows.as_str() {
            Some(r) if ROW_MODES.contains(&r) => fs.rows = r.into(),
            _ => {
                return Err(Error::Value(format!(
                    "rows must be one of ('auto', 'single'), got {}",
                    pyval::repr(&rows)
                )));
            }
        }
        let empty = Vec::new();
        let list = |k: &str| match d.get(k) {
            Some(Value::Array(a)) => a,
            _ => &empty,
        };
        let mut lib = Library::default();
        for (i, spec) in list("custom_elements").iter().enumerate() {
            let name = spec.get("name").map(pyval::str).unwrap_or_else(|| "?".into());
            let at = format!("custom_elements[{i}] ({name})");
            let o = spec
                .as_object()
                .ok_or_else(|| Error::Type(format!("{at}: must be a dict")))?;
            lib.register(CustomSpec::from_dict(o).map_err(|e| e.context(&at))?);
        }
        fs.library = Arc::new(lib);
        if let Some(Value::Object(kinds)) = d.get("line_kinds") {
            for (name, style) in kinds {
                let at = format!("line_kinds.{name}");
                let o = style
                    .as_object()
                    .ok_or_else(|| Error::Type(format!("{at}: must be a dict")))?;
                fs.add_line_kind(name, LineStyle::from_dict(o).map_err(|e| e.context(&at))?);
            }
        }
        for (i, e) in list("elements").iter().enumerate() {
            let id = e.get("id").map(pyval::str).unwrap_or_else(|| "?".into());
            let at = format!("elements[{i}] ({id})");
            fs.add_dict(e).map_err(|err| err.context(&at))?;
        }
        let mut pending: Vec<(usize, &Value)> = list("connections").iter().enumerate().collect();
        while !pending.is_empty() {
            let mut retry = Vec::new();
            for &(i, c) in &pending {
                match fs.connect_dict(c) {
                    Ok(()) => {}
                    Err(Error::Key(_)) => retry.push((i, c)),
                    Err(e) => return Err(e),
                }
            }
            if retry.len() == pending.len() {
                let (i, c) = retry[0];
                let show = |k: &str| c.get(k).map(pyval::str).unwrap_or_else(|| "None".into());
                let at = format!("connections[{i}] ({} -> {})", show("from"), show("to"));
                fs.connect_dict(c).map_err(|e| e.context(&at))?;
            }
            pending = retry;
        }
        let pos: HashMap<(String, String), usize> = list("connections")
            .iter()
            .enumerate()
            .map(|(i, c)| {
                let s = |k: &str| c.get(k).map(pyval::str).unwrap_or_default();
                ((s("from"), s("to")), i)
            })
            .collect();
        let n = pos.len();
        fs.connections
            .sort_by_key(|c| *pos.get(&(c.src.clone(), c.dst.clone())).unwrap_or(&n));
        if let Some(Value::Array(revs)) = d.get("revisions") {
            fs.revisions = revs.clone();
        }
        if let Some(frozen) = d.get("frozen") {
            let ok = matches!(frozen, Value::Object(o) if {
                let mut k: Vec<&String> = o.keys().collect();
                k.sort();
                k == ["fingerprint", "sheet", "through"]
            });
            if !ok {
                return Err(Error::Value(
                    "frozen: must have exactly the keys ['fingerprint', 'sheet', 'through']".into(),
                ));
            }
        }
        Ok(fs)
    }

    fn add_dict(&mut self, e: &Value) -> Result<()> {
        let o = e
            .as_object()
            .ok_or_else(|| Error::Type("an element must be a dict".into()))?;
        let unknown: Vec<&String> = o.keys().filter(|k| !ELEMENT_KEYS.contains(&k.as_str())).collect();
        if !unknown.is_empty() {
            return Err(Error::Value(format!(
                "unknown keys {} (element parameters go in 'params')",
                sorted_keys_repr(unknown.into_iter())
            )));
        }
        let typ = pyval::str(o.get("type").ok_or_else(|| Error::Key("type".into()))?);
        let id = o.get("id").filter(|v| !v.is_null()).map(pyval::str);
        let ports = match o.get("ports") {
            Some(Value::Object(p)) => p.clone(),
            _ => serde_json::Map::new(),
        };
        let params = match o.get("params") {
            Some(Value::Object(p)) => p.clone(),
            _ => Params::new(),
        };
        let span = match o.get("span") {
            Some(v) => pyval::int(v)?,
            None => 1,
        };
        self.add(
            &typ,
            id.as_deref(),
            opt_int(o.get("level"))?,
            ports,
            &o.get("desc").map(pyval::str).unwrap_or_default(),
            opt_int(o.get("elevation"))?,
            span,
            params,
        )
    }

    fn connect_dict(&mut self, c: &Value) -> Result<()> {
        let o = c
            .as_object()
            .ok_or_else(|| Error::Type("a connection must be a dict".into()))?;
        let unknown: Vec<&String> = o.keys().filter(|k| !CONNECTION_KEYS.contains(&k.as_str())).collect();
        if !unknown.is_empty() {
            return Err(Error::Value(format!(
                "unknown keys {}",
                sorted_keys_repr(unknown.into_iter())
            )));
        }
        let src = pyval::str(o.get("from").ok_or_else(|| Error::Key("from".into()))?);
        let dst = pyval::str(o.get("to").ok_or_else(|| Error::Key("to".into()))?);
        let kind = o.get("type").filter(|v| !v.is_null()).map(pyval::str);
        let inline = match o.get("inline") {
            Some(Value::Array(a)) => a.clone(),
            _ => Vec::new(),
        };
        let anchor = o.get("anchor").map(pyval::str).unwrap_or_else(|| "auto".into());
        let recycle = o.get("recycle").is_some_and(pyval::truthy);
        self.connect(&src, &dst, kind.as_deref(), &inline, &anchor, recycle)
    }

    /// The theme used to draw (font size may be overridden).
    pub fn theme(&self) -> Theme {
        Theme::default()
    }
}

// -- free functions ------------------------------------------------------------------

/// (bends, length) no route of a line between ``s`` and ``t`` can go below.
fn line_floor(s: &Terminal, t: &Terminal) -> (i64, f64) {
    let dist = |a: Point, b: Point| (b.0 - a.0).abs() + (b.1 - a.1).abs();
    let fp = |n: (i64, i64)| (n.0 as f64, n.1 as f64);
    if s.first == t.edge && t.first == s.edge {
        return (
            0,
            dist(s.point, fp(s.edge)) + dist(fp(s.edge), fp(t.edge)) + dist(fp(t.edge), t.point),
        );
    }
    let bends = min_bends(s.first, s.out_dir, t.first, (t.out_dir + 2) % 4);
    let run = dist(fp(s.first), fp(t.first)) + 2.0;
    (bends, dist(s.point, fp(s.edge)) + run + dist(fp(t.edge), t.point))
}

/// Move items apart for one line: a wider gap in front of item ``gap``, or
/// new elevations.
#[derive(Clone, Debug)]
struct Shift {
    gap: Option<(usize, i64)>,
    lift: Vec<(usize, i64)>,
}

struct Undo {
    gap: Option<(usize, i64)>,
    lift: Vec<(usize, Option<i64>)>,
}

impl Shift {
    fn apply(&self, items: &mut [Item], gx: &mut [i64]) -> Undo {
        if let Some((j, by)) = self.gap {
            gx[j] += by;
            return Undo {
                gap: Some((j, by)),
                lift: Vec::new(),
            };
        }
        let old: Vec<(usize, Option<i64>)> = self.lift.iter().map(|&(j, _)| (j, items[j].elevation)).collect();
        for &(j, e) in &self.lift {
            items[j].elevation = Some(e);
        }
        Undo { gap: None, lift: old }
    }
}

impl Undo {
    fn revert(self, items: &mut [Item], gx: &mut [i64]) {
        if let Some((j, by)) = self.gap {
            gx[j] -= by;
        }
        for (j, e) in self.lift {
            items[j].elevation = e;
        }
    }
}

fn turns_back(points: &[Point]) -> bool {
    let mut seen: Vec<(i64, i64)> = Vec::new();
    for w in points.windows(2) {
        let (p, q) = (w[0], w[1]);
        let d = (
            ((q.0 > p.0) as i64) - ((q.0 < p.0) as i64),
            ((q.1 > p.1) as i64) - ((q.1 < p.1) as i64),
        );
        if seen.contains(&(-d.0, -d.1)) {
            return true;
        }
        seen.push(d);
    }
    false
}

/// Must a line turn back, wherever its items stand?
fn forced_back(src: &Equipment, out: char, dst: &Equipment, into: char) -> bool {
    let top = |e: &Equipment| e.tier() + e.span - 1;
    (out == 'S' && dst.tier() > src.tier())
        || (out == 'N' && top(dst) < top(src))
        || (into == 'S' && src.tier() > dst.tier())
        || (into == 'N' && top(src) < top(dst))
}

/// Can a line leaving ``s`` moving ``sd`` arrive at ``t`` moving ``td``
/// without ever turning back, nothing in the way?
fn reaches(s: (i64, i64), sd: usize, t: (i64, i64), td: usize) -> bool {
    let (x0, x1) = (s.0.min(t.0) - 2, s.0.max(t.0) + 2);
    let (y0, y1) = (s.1.min(t.1) - 2, s.1.max(t.1) + 2);
    let start = (s, sd, 1u8 << sd);
    let mut seen: HashSet<((i64, i64), usize, u8)> = HashSet::new();
    seen.insert(start);
    let mut todo = vec![start];
    while let Some(((x, y), d, mask)) = todo.pop() {
        for nd in [d, (d + 1) % 4, (d + 3) % 4] {
            if mask & (1 << ((nd + 2) % 4)) != 0 {
                continue;
            }
            let (nx, ny) = (x + DIRS[nd].0, y + DIRS[nd].1);
            if !(x0 <= nx && nx <= x1 && y0 <= ny && ny <= y1) {
                continue;
            }
            if (nx, ny) == t && nd == td {
                return true;
            }
            let state = ((nx, ny), nd, mask | (1 << nd));
            if seen.insert(state) {
                todo.push(state);
            }
        }
    }
    false
}

fn missing(s: (i64, i64), sd: usize, t: (i64, i64), td: usize, step: (i64, i64)) -> Option<i64> {
    (1..=STUB_REACH).find(|&k| reaches(s, sd, (t.0 + step.0 * k, t.1 + step.1 * k), td))
}

/// Ways to ease the lines of ``d`` that turn back only because their ports
/// stand too close.
fn stub_shifts(net: &Expanded, items: &[Item], d: &Diagram, gy: &[i64], fixed: &HashSet<String>) -> Result<Vec<Shift>> {
    let index: HashMap<&str, usize> = items.iter().enumerate().map(|(j, it)| (it.id.as_str(), j)).collect();
    let placed: HashMap<&str, &Placed> = d.placed.iter().map(|p| (p.id.as_str(), p)).collect();
    let (_, base) = bands(items, gy)?;
    let mut pipe: HashMap<&str, &Vec<String>> = HashMap::new();
    for hs in &net.headers {
        for t in hs {
            pipe.insert(t.as_str(), hs);
        }
    }
    let lifted = |j: usize, by: i64| -> Option<Vec<(usize, i64)>> {
        let own = vec![items[j].id.clone()];
        let group = pipe.get(items[j].id.as_str()).copied().unwrap_or(&own);
        let mut out: Vec<(usize, i64)> = Vec::new();
        for t in group {
            let i = index[t.as_str()];
            let it = &items[i];
            let e = placed[it.id.as_str()].y - base[it.level as usize] + by;
            if it.edge.is_some() || e < 0 || fixed.contains(&it.id) {
                return None;
            }
            match out.iter_mut().find(|(k, _)| *k == i) {
                Some(x) => x.1 = e,
                None => out.push((i, e)),
            }
        }
        Some(out)
    };
    let mut shifts = Vec::new();
    for (c, r) in &d.routes {
        if !turns_back(&r.points) {
            continue;
        }
        let [(a, pa), (b, pb)] = c.ends();
        let s = resolve_port(placed[a], pa)?;
        let t = resolve_port(placed[b], pb)?;
        let (sd, td) = (s.out_dir, (t.out_dir + 2) % 4);
        if sd == (td + 2) % 4 || reaches(s.first, sd, t.first, td) {
            continue;
        }
        let (ja, jb) = (index[a], index[b]);
        for sign in [-1i64, 1] {
            let Some(k) = missing(s.first, sd, t.first, td, (0, sign)) else {
                continue;
            };
            for (j, by) in [(jb, sign * k), (ja, -sign * k)] {
                if let Some(lift) = lifted(j, by) {
                    if !lift.is_empty() {
                        shifts.push(Shift { gap: None, lift });
                    }
                }
            }
        }
        if let Some(k) = missing(s.first, sd, t.first, td, (1, 0)) {
            if jb > ja {
                shifts.push(Shift {
                    gap: Some((jb, k)),
                    lift: Vec::new(),
                });
            }
        }
        if let Some(k) = missing(s.first, sd, t.first, td, (-1, 0)) {
            if ja > jb {
                shifts.push(Shift {
                    gap: Some((ja, k)),
                    lift: Vec::new(),
                });
            }
        }
    }
    Ok(shifts)
}

/// Cost of one sheet on the given levels (None: it does not route).
fn run_level_cost(fs: &Flowsheet, levels: &Levels, max_expand: usize, bound: Option<i64>) -> Result<Option<i64>> {
    let mut sheet = fs.clone();
    sheet.equipment = fs.with_levels(Some(levels));
    sheet.in_order = false;
    sheet.flag_room = false;
    sheet.quick = true;
    let spacings = max_expand.min(QUICK_EXPAND);
    let lay = sheet.layout_input()?;
    if let Some(b) = bound {
        if sheet.level_floor(spacings, &lay)? >= b {
            return Ok(None);
        }
    }
    match sheet.build_core(spacings, false, Some(lay)) {
        Ok(d) => Ok(Some(sheet.level_cost(&d))),
        Err(Error::Routing(_)) => Ok(None),
        Err(e) => Err(e),
    }
}

fn run_level_estimate(fs: &Flowsheet, levels: &Levels) -> Result<i64> {
    let mut sheet = fs.clone();
    sheet.equipment = fs.with_levels(Some(levels));
    sheet.in_order = false;
    sheet.flag_room = false;
    sheet.estimate_cost()
}

/// Ids of the corresponding items of repeated units.
fn repeated(fs: &Flowsheet) -> Result<Vec<Vec<String>>> {
    let mut sheet = fs.clone();
    sheet.equipment = fs.with_levels(None);
    let net = sheet.prepare()?.0;
    let sym = find_units(&net);
    let mut classes: IndexMap<usize, Vec<String>> = IndexMap::new();
    for (e, &c) in net.equipment.iter().zip(&sym.item_class) {
        classes.entry(c).or_default().push(e.id.clone());
    }
    Ok(classes.into_values().filter(|ids| ids.len() > 1).collect())
}

fn settle_levels(sheets: &mut [Flowsheet], max_expand: usize, jobs: Option<usize>) -> Result<()> {
    for fs in sheets.iter_mut() {
        let sets = moves(&fs.equipment, &fs.connections, &repeated(fs)?);
        let mut lv = start_levels(&fs.equipment);
        if !sets.is_empty() {
            let mut error: Option<Error> = None;
            let shared: &Flowsheet = fs;
            let mut evaluate = |tasks: &[Levels], bound: Option<i64>| -> Vec<Option<i64>> {
                let got = pool_map(tasks, jobs, |t| run_level_cost(shared, t, max_expand, bound));
                got.into_iter()
                    .map(|r| match r {
                        Ok(v) => v,
                        Err(e) => {
                            error.get_or_insert(e);
                            None
                        }
                    })
                    .collect()
            };
            let mut est_error: Option<Error> = None;
            let mut estimate = |tasks: &[Levels]| -> Vec<i64> {
                let got = pool_map(tasks, jobs, |t| run_level_estimate(shared, t));
                got.into_iter()
                    .map(|r| match r {
                        Ok(v) => v,
                        Err(e) => {
                            est_error.get_or_insert(e);
                            0
                        }
                    })
                    .collect()
            };
            lv = search_levels(&lv, &sets, &mut evaluate, Some(&mut estimate), MAX_ROUNDS);
            if let Some(e) = error.or(est_error) {
                return Err(e);
            }
        }
        fs.equipment = fs.with_levels(Some(&lv));
    }
    Ok(())
}

/// Build several sheets: every placement mode of every sheet at once, the
/// cheapest per sheet kept (the first on a tie), then each sheet's ports
/// re-sorted.
fn build_sheets(
    mut sheets: Vec<Flowsheet>,
    max_expand: usize,
    compact: bool,
    jobs: Option<usize>,
) -> Result<Vec<Diagram>> {
    settle_levels(&mut sheets, max_expand, jobs)?;
    let mut modes = Vec::new();
    for fs in &sheets {
        modes.push(fs.modes()?);
    }
    let tasks: Vec<(usize, (bool, bool))> = modes
        .iter()
        .enumerate()
        .flat_map(|(k, ms)| ms.iter().map(move |&m| (k, m)))
        .collect();
    let results = pool_map(&tasks, jobs, |&(k, m)| sheets[k].first(m, max_expand, compact));
    let mut results = results.into_iter();
    let mut chosen: Vec<((bool, bool), Diagram)> = Vec::new();
    for (fs, ms) in sheets.iter().zip(&modes) {
        let mut best: Option<((bool, bool), Diagram)> = None;
        let mut error: Option<Error> = None;
        for &m in ms {
            match results.next().unwrap() {
                Err(Error::Routing(e)) => error = Some(Error::Routing(e)),
                Err(e) => return Err(e),
                Ok(r) => {
                    if best.as_ref().is_none_or(|b| fs.cost(&r) < fs.cost(&b.1)) {
                        best = Some((m, r));
                    }
                }
            }
        }
        match best {
            Some(b) => chosen.push(b),
            None => return Err(error.unwrap()),
        }
    }
    let swaps: Vec<(usize, (bool, bool), Diagram)> =
        chosen.into_iter().enumerate().map(|(k, (m, d))| (k, m, d)).collect();
    pool_map(&swaps, jobs, |(k, m, d)| {
        sheets[*k].swap(*m, d.clone(), max_expand, compact)
    })
    .into_iter()
    .collect()
}

/// Rows stacked top first, each row's left edge at x = 0.
fn stack(diagrams: &[Diagram], styles: &IndexMap<String, LineStyle>) -> Diagram {
    let theme = Theme::default();
    let em = theme.fontsize / 36.0;
    let pitch = theme.spec_line_height * em;
    let extent = |d: &Diagram| -> (f64, f64, f64) {
        let mut ys: Vec<f64> = Vec::new();
        let mut xs: Vec<f64> = Vec::new();
        for p in &d.placed {
            let b = p.bbox();
            ys.push(b.1 as f64);
            ys.push(b.3 as f64);
            xs.push(b.0 as f64);
        }
        for (_, r) in &d.routes {
            for &(x, y) in &r.points {
                ys.push(y);
                xs.push(x);
            }
        }
        let min = |v: &[f64]| v.iter().cloned().fold(f64::INFINITY, f64::min);
        let max = |v: &[f64]| v.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
        (min(&xs), min(&ys), max(&ys))
    };
    let text_lines = |d: &Diagram, rotating: bool| -> i64 {
        d.placed
            .iter()
            .filter(|p| d.descs.contains_key(&p.id) && p.geo.rotating == rotating)
            .map(|p| d.descs[&p.id].matches('\n').count() as i64 + 2)
            .max()
            .unwrap_or(0)
    };
    let mut out = Diagram {
        line_styles: styles.clone(),
        ..Default::default()
    };
    let mut top = 0.0f64;
    for (k, d) in diagrams.iter().enumerate() {
        let (x0, y0, y1) = extent(d);
        if k > 0 {
            let gap = (text_lines(&diagrams[k - 1], true) + text_lines(d, false)) as f64 * pitch;
            top -= (ROW_GAP + (gap + 0.999).trunc() as i64) as f64;
        }
        let dx = -(x0.trunc() as i64);
        let dy = (top - y1).trunc() as i64;
        for p in &d.placed {
            let mut q = p.clone();
            q.x += dx;
            q.y += dy;
            out.placed.push(q);
        }
        for (c, r) in &d.routes {
            out.routes.push((
                c.clone(),
                Route {
                    points: r.points.iter().map(|&(x, y)| (x + dx as f64, y + dy as f64)).collect(),
                    strict: r.strict,
                    nodes: r.nodes.iter().map(|&(x, y)| (x + dx, y + dy)).collect(),
                },
            ));
        }
        out.warnings.extend(d.warnings.iter().cloned());
        for (k, v) in &d.descs {
            out.descs.insert(k.clone(), v.clone());
        }
        out.rows.push((y0 + dy as f64, y1 + dy as f64));
        out.uturns += d.uturns;
        top = y0 + dy as f64;
    }
    out
}

/// Off-sheet flags go to an edge of the sheet.
fn items_of(net: &Expanded, flags: Option<&HashMap<String, String>>) -> Result<Vec<Item>> {
    let geos: Vec<&Arc<Geometry>> = net.equipment.iter().map(|e| &net.geos[&e.id]).collect();
    let first = geos.iter().position(|g| !g.off_sheet).unwrap_or(geos.len());
    let mut items = Vec::new();
    for (i, (e, g)) in net.equipment.iter().zip(&geos).enumerate() {
        let mut edge = None;
        let flag = flags.and_then(|f| f.get(&e.id));
        let local = g.beside || e.param("local") == Some(&Value::Bool(true)) || flag.is_some_and(|f| f == "local");
        if g.off_sheet && !local {
            let auto = if i < first { "left" } else { "right" };
            let v = match e.param("edge") {
                Some(v) if pyval::truthy(v) => pyval::str(v),
                _ => flag.cloned().unwrap_or_else(|| auto.to_string()),
            };
            if v != "left" && v != "right" {
                return Err(Error::Value(format!("{}: edge must be 'left' or 'right'", e.id)));
            }
            edge = Some(v);
        }
        items.push(Item {
            id: e.id.clone(),
            typ: e.typ.clone(),
            level: e.tier(),
            geo: (*g).clone(),
            elevation: e.elevation,
            edge,
            span: e.span,
        });
    }
    Ok(items)
}

/// Level bands needed: the highest level, or the top of a tall item.
fn band_count(eqs: &[Equipment]) -> usize {
    eqs.iter().map(|e| e.tier() + e.span.max(1)).max().unwrap_or(0) as usize
}

fn validate(net: &Expanded) -> Result<()> {
    for c in &net.connections {
        for (eq, port) in c.ends() {
            let Some(g) = net.geos.get(eq) else {
                return Err(Error::Key(format!(
                    "{} -> {}: unknown equipment {}",
                    c.src,
                    c.dst,
                    pyval::str_repr(eq)
                )));
            };
            if !g.ports.contains_key(port) {
                return Err(Error::Key(format!(
                    "{eq}: unknown port {}; known: {}",
                    pyval::str_repr(port),
                    sorted_keys_repr(g.ports.keys())
                )));
            }
        }
    }
    Ok(())
}

/// Facing-port links of every line.
fn links_of(net: &Expanded, process: &HashSet<String>) -> Vec<Link> {
    let index: HashMap<&str, usize> = net
        .equipment
        .iter()
        .enumerate()
        .map(|(i, e)| (e.id.as_str(), i))
        .collect();
    let mut links = Vec::new();
    for c in &net.connections {
        let mut ends: Vec<(usize, crate::geometry::Port)> = c
            .ends()
            .iter()
            .map(|(eq, port)| (index[eq], net.geos[*eq].ports[*port]))
            .collect();
        ends.sort_by_key(|e| e.0);
        let (a, pa) = ends[0];
        let (b, pb) = ends[1];
        let flanged = process.contains(&c.kind) && c.ends().iter().all(|(eq, _)| net.geos[*eq].nozzles);
        if a != b {
            links.push(Link {
                a,
                a_side: pa.side,
                a_off: pa.offset,
                b,
                b_side: pb.side,
                b_off: pb.offset,
                flanged,
            });
        }
    }
    links
}

fn heights(
    net: &Expanded,
    links: &[Link],
    flags: Option<&HashMap<String, String>>,
    anchor_free: bool,
) -> Result<(Vec<Item>, Vec<i64>, Vec<i64>)> {
    let items = items_of(net, flags)?;
    let gap = vec![MIN_GAP; band_count(&net.equipment)];
    let (_, base) = bands(&items, &gap)?;
    let ys = place_y(&items, &gap, links, anchor_free)?;
    Ok((items, ys, base))
}

fn lift(eq: &mut Equipment, elevation: i64) -> bool {
    if eq.elevation == Some(elevation) {
        return false;
    }
    eq.elevation = Some(elevation);
    true
}

/// Heights that a top or bottom nozzle needs (flow layout).
fn settle_lifts(net: &mut Expanded, flags: &HashMap<String, String>, links: &[Link], flag_room: bool) -> Result<bool> {
    let fixed: HashSet<String> = net
        .equipment
        .iter()
        .filter(|e| e.elevation.is_some())
        .map(|e| e.id.clone())
        .collect();
    let mut used = false;
    let index: HashMap<String, usize> = net
        .equipment
        .iter()
        .enumerate()
        .map(|(i, e)| (e.id.clone(), i))
        .collect();
    let mut group: HashMap<String, Vec<usize>> = HashMap::new();
    let mut taps: HashSet<usize> = HashSet::new();
    for h in &net.headers {
        let members: Vec<usize> = h.iter().map(|u| index[u]).collect();
        for t in h {
            group.insert(t.clone(), members.clone());
            taps.insert(index[t]);
        }
    }
    let sym = find_units(net);
    let mut classes: IndexMap<usize, Vec<usize>> = IndexMap::new();
    for (i, &k) in sym.item_class.iter().enumerate() {
        classes.entry(k).or_default().push(i);
    }
    let alike: HashMap<usize, Vec<usize>> = sym
        .item_class
        .iter()
        .enumerate()
        .map(|(i, k)| (i, classes[k].clone()))
        .collect();
    for _ in 0..8 {
        let (items, ys, base) = heights(net, links, Some(flags), true)?;
        let mut moved = false;
        for lk in links {
            if items[lk.a].level != items[lk.b].level {
                continue;
            }
            for (q, q_side, p, p_side, p_off) in [
                (lk.a, lk.a_side, lk.b, lk.b_side, lk.b_off),
                (lk.b, lk.b_side, lk.a, lk.a_side, lk.a_off),
            ] {
                let top = ys[q] + items[q].geo.height;
                if (q_side, p_side) == ('N', 'S') {
                    let can = !fixed.contains(&net.equipment[p].id) && !items[p].geo.nozzles;
                    if ys[p] < top + 2 && can {
                        moved |= lift(&mut net.equipment[p], top + 2 - base[items[p].level as usize]);
                    }
                    continue;
                }
                if !(q_side == 'N' || q_side == 'S') || !(p_side == 'W' || p_side == 'E') {
                    continue;
                }
                let port = ys[p] + p_off;
                let room = if flag_room && items[p].geo.off_sheet { 1 } else { 0 };
                let (j, y);
                if q_side == 'N' && port <= top + 1 {
                    if items[p].span > 1 && !fixed.contains(&net.equipment[q].id) {
                        (j, y) = (q, port - 2 - items[q].geo.height);
                    } else {
                        (j, y) = (p, top + 2 - p_off);
                    }
                } else if q_side == 'S' && port >= ys[q] - room {
                    let low = ys[q] - 2 - p_off;
                    let near = room != 0 && port == ys[q] - 1;
                    used |= room != 0;
                    if near && !fixed.contains(&net.equipment[p].id) && low >= base[items[p].level as usize] {
                        (j, y) = (p, low);
                    } else if !fixed.contains(&net.equipment[q].id) {
                        (j, y) = (q, port + 1 + room);
                    } else {
                        (j, y) = (p, -1);
                    }
                } else {
                    continue;
                }
                if fixed.contains(&net.equipment[j].id) || y < base[items[j].level as usize] {
                    continue;
                }
                let mut elevation = y - base[items[j].level as usize];
                let members: Vec<usize> = match group.get(&net.equipment[j].id) {
                    Some(m) if !m.is_empty() => m.clone(),
                    _ => alike
                        .get(&j)
                        .cloned()
                        .unwrap_or_else(|| vec![j])
                        .into_iter()
                        .filter(|&k| !fixed.contains(&net.equipment[k].id))
                        .collect(),
                };
                if members.len() > 1 {
                    for &k in &members {
                        elevation = elevation.max(net.equipment[k].elevation.unwrap_or(0));
                    }
                }
                for &k in &members {
                    moved |= lift(&mut net.equipment[k], elevation);
                }
            }
        }
        moved |= clear_main_pipes(&mut net.equipment, &items, &ys, &base, links, &taps, &fixed, &alike);
        if !moved {
            return Ok(used);
        }
    }
    Ok(used)
}

/// Lift vessels that stand across a main pipe on their own level.
#[allow(clippy::too_many_arguments)]
fn clear_main_pipes(
    eqs: &mut [Equipment],
    items: &[Item],
    ys: &[i64],
    base: &[i64],
    links: &[Link],
    taps: &HashSet<usize>,
    fixed: &HashSet<String>,
    alike: &HashMap<usize, Vec<usize>>,
) -> bool {
    let mut moved = false;
    for lk in links {
        if (lk.a_side, lk.b_side) != ('E', 'W') || !(taps.contains(&lk.a) || taps.contains(&lk.b)) {
            continue;
        }
        let level = items[lk.a].level;
        if items[lk.b].level != level {
            continue;
        }
        let tap = if taps.contains(&lk.a) { lk.a } else { lk.b };
        let pipe = ys[tap] + if tap == lk.a { lk.a_off } else { lk.b_off };
        for j in lk.a + 1..lk.b {
            let it = &items[j];
            if taps.contains(&j) || it.level != level || !it.geo.nozzles {
                continue;
            }
            if !(ys[j] <= pipe && pipe <= ys[j] + it.geo.height) {
                continue;
            }
            let elevation = pipe + 1 - base[level as usize];
            for &k in alike.get(&j).map(|v| v.as_slice()).unwrap_or(&[j]) {
                if !fixed.contains(&eqs[k].id) && eqs[k].elevation.unwrap_or(0) < elevation {
                    moved |= lift(&mut eqs[k], elevation);
                }
            }
        }
    }
    moved
}

/// Height of tees beside a vertical shared port on the same level.
fn settle_tees(net: &mut Expanded, links: &[Link]) -> Result<()> {
    if net.pending.is_empty() {
        return Ok(());
    }
    let index: HashMap<String, usize> = net
        .equipment
        .iter()
        .enumerate()
        .map(|(i, e)| (e.id.clone(), i))
        .collect();
    for _ in 0..4 {
        let (items, ys, base) = heights(net, links, None, false)?;
        let mut moved = false;
        for t in net.pending.clone() {
            let o = index[&t.owner];
            let port_y = ys[o] + if t.side == 'N' { net.geos[&t.owner].height } else { 0 };
            let ti = index[&t.tee];
            let floor = base[items[ti].level as usize];
            let want = (port_y - floor + if t.side == 'N' { 1 } else { -3 }).max(0);
            moved |= lift(&mut net.equipment[ti], want);
        }
        if !moved {
            return Ok(());
        }
    }
    Ok(())
}
