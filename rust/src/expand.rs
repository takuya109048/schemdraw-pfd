//! Turn the user's description into the flat netlist that is laid out:
//! inline items become equipment next to one end of their line, and a port
//! used by several lines gets a main pipe with a tap (a generated tee) per
//! line.  Generated ids start with ``_``.

use std::cell::RefCell;
use std::collections::HashMap;
use std::sync::Arc;

use indexmap::IndexMap;
use serde_json::Value;

use crate::elements::Geometry;
use crate::error::{Error, Result};
use crate::netlist::{Connection, Equipment};
use crate::pyval::{self, Params};

pub const GENERATED_PREFIX: &str = "_";
pub const TAP_PREFIX: &str = "_T_";

/// A tee on the level of an element with a vertical (N/S) shared port.
#[derive(Clone, Debug)]
pub struct PendingTee {
    pub tee: String,
    pub owner: String,
    pub port: String,
    pub side: char,
}

#[derive(Clone, Debug, Default)]
pub struct Expanded {
    pub equipment: Vec<Equipment>,
    pub connections: Vec<Connection>,
    pub geos: HashMap<String, Arc<Geometry>>,
    pub pending: Vec<PendingTee>,
    pub headers: Vec<Vec<String>>,
    pub owners: IndexMap<String, String>,
}

/// Geometries by item id, built once.
pub struct Geos<'a> {
    build: &'a dyn Fn(&Equipment) -> Result<Arc<Geometry>>,
    cache: RefCell<HashMap<String, Arc<Geometry>>>,
}

impl<'a> Geos<'a> {
    pub fn new(build: &'a dyn Fn(&Equipment) -> Result<Arc<Geometry>>) -> Geos<'a> {
        Geos {
            build,
            cache: RefCell::new(HashMap::new()),
        }
    }

    pub fn get(&self, e: &Equipment) -> Result<Arc<Geometry>> {
        if let Some(g) = self.cache.borrow().get(&e.id) {
            return Ok(g.clone());
        }
        let g = (self.build)(e)?;
        self.cache.borrow_mut().insert(e.id.clone(), g.clone());
        Ok(g)
    }
}

/// ``edges``: off-sheet flags that will stand on a sheet edge (``"left"`` /
/// ``"right"``, flow layout; ``"local"`` beside their item).
pub fn expand(
    equipment: &[Equipment],
    connections: &[Connection],
    build: &dyn Fn(&Equipment) -> Result<Arc<Geometry>>,
    edges: Option<&HashMap<String, String>>,
) -> Result<Expanded> {
    let mut eqs: Vec<Equipment> = equipment.to_vec();
    let geos = Geos::new(build);
    let flat = inline(&mut eqs, connections, &geos)?;
    let mut pending = Vec::new();
    let mut headers = Vec::new();
    let mut owners = IndexMap::new();
    let empty = HashMap::new();
    let flat = auto_tees(
        &mut eqs,
        flat,
        &geos,
        &mut pending,
        &mut headers,
        &mut owners,
        edges.unwrap_or(&empty),
    )?;
    let mut out_geos = HashMap::new();
    for e in &eqs {
        out_geos.insert(e.id.clone(), geos.get(e)?);
    }
    Ok(Expanded {
        equipment: eqs,
        connections: flat,
        geos: out_geos,
        pending,
        headers,
        owners,
    })
}

// -- inline items ----------------------------------------------------------------
fn inline(eqs: &mut Vec<Equipment>, connections: &[Connection], geos: &Geos) -> Result<Vec<Connection>> {
    let mut by_id: HashMap<String, Equipment> = eqs.iter().map(|e| (e.id.clone(), e.clone())).collect();
    let mut results: Vec<Option<Vec<Connection>>> = connections
        .iter()
        .map(|c| {
            if c.inline.is_empty() {
                Some(vec![c.clone()])
            } else {
                None
            }
        })
        .collect();
    let mut todo: Vec<usize> = (0..connections.len()).filter(|&ci| results[ci].is_none()).collect();
    while !todo.is_empty() {
        let ready: Vec<usize> = todo
            .iter()
            .copied()
            .filter(|&ci| connections[ci].ends().iter().all(|(e, _)| by_id.contains_key(*e)))
            .collect();
        if ready.is_empty() {
            let c = &connections[todo[0]];
            let missing = c.ends().iter().find(|(e, _)| !by_id.contains_key(*e)).unwrap().0;
            return Err(Error::Key(format!(
                "{} -> {}: unknown equipment {}",
                c.src,
                c.dst,
                pyval::str_repr(missing)
            )));
        }
        for ci in ready {
            results[ci] = Some(expand_line(ci, &connections[ci], eqs, &mut by_id, geos)?);
            todo.retain(|&x| x != ci);
        }
    }
    Ok(results.into_iter().flatten().flatten().collect())
}

fn expand_line(
    ci: usize,
    c: &Connection,
    eqs: &mut Vec<Equipment>,
    by_id: &mut HashMap<String, Equipment>,
    geos: &Geos,
) -> Result<Vec<Connection>> {
    let order: HashMap<&str, usize> = eqs.iter().enumerate().map(|(i, e)| (e.id.as_str(), i)).collect();
    let [(s, _), (d, _)] = c.ends();
    let forward = order[s] <= order[d];
    let mut anchor = c.anchor.clone();
    let d_off = geos.get(&by_id[d])?.off_sheet;
    if anchor == "auto" {
        let off = d_off && !geos.get(&by_id[s])?.off_sheet;
        anchor = if off { "src" } else { "dst" }.into();
    }
    let a = by_id[if anchor == "dst" { d } else { s }].clone();
    let home = if d_off { by_id[d].clone() } else { a.clone() };

    let mut chain: Vec<Equipment> = Vec::new();
    for (k, item) in c.inline.iter().enumerate() {
        let mut params: Params = item.params.clone();
        if !forward && !params.contains_key("mirror") {
            params.insert("mirror".into(), Value::Bool(true));
        }
        let level = match item.level {
            None => home.tier(),
            Some(l) => l,
        };
        let id = item
            .id
            .clone()
            .unwrap_or_else(|| format!("{GENERATED_PREFIX}L{ci}_{k}"));
        let mut eq = Equipment::new(&id, &item.typ, Some(level), params);
        eq.desc = item.desc.clone();
        eq.elevation = item.elevation;
        if by_id.contains_key(&eq.id) {
            return Err(Error::Value(format!("duplicate id {}", pyval::str_repr(&eq.id))));
        }
        let ports = &geos.get(&eq)?.ports;
        for p in [&item.port_in, &item.port_out] {
            if !ports.contains_key(p) {
                let mut known: Vec<&String> = ports.keys().collect();
                known.sort();
                return Err(Error::Key(format!(
                    "inline {} ({}) has no port {}; known: {} (set 'in' / 'out')",
                    pyval::str_repr(&item.typ),
                    eq.id,
                    pyval::str_repr(p),
                    pyval::list_repr(&known)
                )));
            }
        }
        by_id.insert(eq.id.clone(), eq.clone());
        chain.push(eq);
    }

    let i = order[a.id.as_str()];
    let at = if (anchor == "dst") == forward { i } else { i + 1 };
    let mut ins = chain.clone();
    if !forward {
        ins.reverse();
    }
    eqs.splice(at..at, ins);
    let mut refs = vec![c.src.clone()];
    for (eq, item) in chain.iter().zip(&c.inline) {
        refs.push(format!("{}.{}", eq.id, item.port_in));
        refs.push(format!("{}.{}", eq.id, item.port_out));
    }
    refs.push(c.dst.clone());
    let last = refs.len() - 2;
    Ok((0..refs.len())
        .step_by(2)
        .map(|j| {
            let mut n = Connection::new(&refs[j], &refs[j + 1], &c.kind);
            n.recycle = c.recycle && j == last;
            n
        })
        .collect())
}

// -- shared ports -> main pipe (header) with taps ------------------------------------
type Use = (usize, usize); // (connection index, end: 0 = src, 1 = dst)

fn sign(v: i64) -> i64 {
    (v > 0) as i64 - (v < 0) as i64
}

/// What the main pipe of one shared port is worked out from.
struct Port<'a> {
    conns: &'a [Connection],
    by_id: &'a HashMap<String, Equipment>,
    order: HashMap<String, usize>,
    n_eqs: i64,
    edges: &'a HashMap<String, String>,
    geos: &'a Geos<'a>,
    owner_id: &'a str,
    level: i64,
    across: &'static str,
    pipe_h: Option<i64>,
}

impl Port<'_> {
    fn other(&self, u: Use) -> Option<&Equipment> {
        self.by_id.get(self.conns[u.0].end(1 - u.1).0)
    }

    fn other_port(&self, u: Use) -> &str {
        self.conns[u.0].end(1 - u.1).1
    }

    /// Where the line's other end lies in the order (+: right).
    fn rel(&self, u: Use) -> i64 {
        let Some(o) = self.other(u) else { return 0 };
        let edge = match self.edges.get(&o.id).map(String::as_str) {
            Some("left") => -1,
            Some("right") => 1,
            _ => 0,
        };
        let owner_flag = self.edges.get(self.owner_id).is_some_and(|s| !s.is_empty());
        if edge != 0 && !owner_flag {
            return edge * self.n_eqs;
        }
        self.order[&o.id] as i64 - self.order[self.owner_id] as i64
    }

    fn dy(&self, u: Use) -> i64 {
        match self.other(u) {
            None => 0,
            Some(o) => sign(o.tier() - self.level),
        }
    }

    /// The side the line's other port faces (``""`` if unknown).
    fn faces(&self, u: Use) -> Result<String> {
        let Some(o) = self.other(u) else {
            return Ok(String::new());
        };
        let g = self.geos.get(o)?;
        Ok(g.ports
            .get(self.other_port(u))
            .map(|p| p.side.to_string())
            .unwrap_or_default())
    }

    fn height(&self, e: &Equipment, side: &str, offset: i64) -> Result<i64> {
        let g = self.geos.get(e)?;
        let at = match side {
            "N" => g.height,
            "S" => 0,
            _ => offset,
        };
        Ok(e.elevation.unwrap_or(0) + at)
    }

    /// The tap's arm the line enters by (see the Python ``arm_of``).
    fn arm_of(&self, u: Use, way: &str, passes: bool) -> Result<String> {
        let dy = self.dy(u);
        if dy != 0 {
            return Ok(if dy > 0 { "n" } else { "s" }.into());
        }
        let f = self.faces(u)?;
        let Some(o) = self.other(u) else {
            return Ok(self.across.into());
        };
        if f != "N" && f != "S" {
            let facing = if way == "e" { "W" } else { "E" };
            let g = self.geos.get(o)?;
            if passes && f == facing && g.nozzles {
                if let Some(ph) = self.pipe_h {
                    if o.elevation.unwrap_or(0) + g.height >= ph {
                        return Ok("n".into());
                    }
                }
            }
            return Ok(self.across.into());
        }
        if let Some(ph) = self.pipe_h {
            let port = self.geos.get(o)?.ports[self.other_port(u)];
            let h = self.height(o, &f, port.offset)?;
            let beyond = if f == "N" { h < ph } else { h > ph };
            if beyond {
                return Ok(if f == "N" { "s" } else { "n" }.into());
            }
            return Ok(self.across.into());
        }
        Ok(if f == "N" { "s" } else { "n" }.into())
    }

    fn ahead(&self, u: Use, sign: i64) -> bool {
        sign * self.rel(u) > 0 && self.dy(u) == 0
    }

    fn level_run(&self, u: Use, sign: i64) -> Result<bool> {
        let f = self.faces(u)?;
        Ok(self.ahead(u, sign) && f != "N" && f != "S")
    }

    fn far_end(&self, u: Use) -> String {
        match self.other(u) {
            None => "None".into(),
            Some(o) => format!("{}|{}|{:?}|{:?}", o.typ, self.other_port(u), o.level, o.elevation),
        }
    }

    fn at_edge(&self, u: Use) -> bool {
        self.other(u)
            .is_some_and(|o| matches!(self.edges.get(&o.id).map(String::as_str), Some("left" | "right")))
    }
}

/// One main pipe being laid out.
struct Run<'a, 'b> {
    p: &'b Port<'a>,
    alike: bool,
    split: bool,
    kind: String,
    level: i64,
    ids: std::vec::IntoIter<String>,
    refs: HashMap<Use, String>,
    pieces: Vec<Connection>,
}

impl Run<'_, '_> {
    fn join(&mut self, a: &str, b: &str) {
        let c = if self.split {
            Connection::new(a, b, &self.kind)
        } else {
            Connection::new(b, a, &self.kind)
        };
        self.pieces.push(c);
    }

    fn tee(&mut self, arms: &str) -> Equipment {
        let mut params = Params::new();
        params.insert("arms".into(), Value::from(arms));
        Equipment::new(&self.ids.next().unwrap(), "tee", Some(self.level), params)
    }

    /// Main pipe from ``start`` running ``way``; the lines cut into it in
    /// order.  Returns its taps, nearest first.
    fn run(&mut self, start: &str, way: &str, lines: &[Use]) -> Result<Vec<Equipment>> {
        let p = self.p;
        let sign = if way == "e" { 1 } else { -1 };
        let back = if way == "e" { "w" } else { "e" };
        let key = |u: Use| (p.ahead(u, sign), sign * p.rel(u) > 0, sign * p.rel(u));
        let mut last = lines[0];
        for &u in &lines[1..] {
            if key(u) > key(last) {
                last = u;
            }
        }
        let straight = !(self.alike && !p.level_run(last, sign)?);
        let mut taps: Vec<Use> = lines.iter().copied().filter(|&u| u != last || !straight).collect();
        taps.sort_by_key(|&u| sign * p.rel(u));
        let mut chain = Vec::new();
        let mut prev = start.to_string();
        let n = taps.len();
        for (k, &u) in taps.iter().enumerate() {
            let end = k == n - 1 && !straight;
            let arm = p.arm_of(u, way, !end)?;
            let arms = if end {
                format!("{back}{arm}")
            } else {
                format!("{back}{way}{arm}")
            };
            let tee = self.tee(&arms);
            self.join(&prev, &format!("{}.{}", tee.id, back));
            self.refs.insert(u, format!("{}.{}", tee.id, arm));
            prev = format!("{}.{}", tee.id, way);
            chain.push(tee);
        }
        if straight {
            self.refs.insert(last, prev);
        }
        Ok(chain)
    }
}

#[allow(clippy::too_many_arguments)]
fn auto_tees(
    eqs: &mut Vec<Equipment>,
    conns: Vec<Connection>,
    geos: &Geos,
    pending: &mut Vec<PendingTee>,
    headers: &mut Vec<Vec<String>>,
    owners: &mut IndexMap<String, String>,
    edges: &HashMap<String, String>,
) -> Result<Vec<Connection>> {
    let mut uses: IndexMap<(String, String), Vec<Use>> = IndexMap::new();
    for (ci, c) in conns.iter().enumerate() {
        for (end, (e, p)) in c.ends().iter().enumerate() {
            uses.entry((e.to_string(), p.to_string())).or_default().push((ci, end));
        }
    }
    let mut by_id: HashMap<String, Equipment> = eqs.iter().map(|e| (e.id.clone(), e.clone())).collect();
    let top = eqs.iter().map(|e| e.tier()).max().unwrap_or(0);
    let mut conns = conns;
    let mut trunks: HashMap<usize, Vec<Connection>> = HashMap::new();

    for ((owner_id, pname), us) in &uses {
        if us.len() < 2 || !by_id.contains_key(owner_id) {
            continue;
        }
        let owner = by_id[owner_id].clone();
        let owner_geo = geos.get(&owner)?;
        let Some(&port) = owner_geo.ports.get(pname) else {
            continue;
        };
        let roles: Vec<usize> = {
            let mut r: Vec<usize> = us.iter().map(|u| u.1).collect();
            r.sort();
            r.dedup();
            r
        };
        if roles.len() > 1 {
            return Err(Error::Value(format!(
                "port {owner_id}.{pname} is both the start and the end of lines"
            )));
        }
        let order: HashMap<String, usize> = eqs.iter().enumerate().map(|(i, e)| (e.id.clone(), i)).collect();
        let base_id = format!("{TAP_PREFIX}{owner_id}_{pname}");
        let mut ids = vec![base_id.clone()];
        for k in 2..us.len() + 2 {
            ids.push(format!("{base_id}_{k}"));
        }
        for t in &ids {
            if by_id.contains_key(t) {
                return Err(Error::Value(format!("duplicate id {}", pyval::str_repr(t))));
            }
        }
        let side = port.side;
        let mut level = owner.tier();
        let mut settle = false;
        let ns = side == 'N' || side == 'S';
        if ns {
            let mut levels = Vec::new();
            for &(ci, e) in us {
                if let Some(o) = by_id.get(conns[ci].end(1 - e).0) {
                    if !geos.get(o)?.off_sheet {
                        levels.push(o.tier());
                    }
                }
            }
            let back = if side == 'S' {
                !levels.is_empty() && *levels.iter().min().unwrap() > owner.tier()
            } else {
                !levels.is_empty() && *levels.iter().max().unwrap() < owner.tier()
            };
            if side == 'S' && owner.tier() > 0 && !back {
                level -= 1;
            } else if side == 'N' && owner.tier() < top && !back {
                level += 1;
            } else {
                settle = true;
            }
        }
        let across = if side == 'N' { "s" } else { "n" };
        let mut p = Port {
            conns: &conns,
            by_id: &by_id,
            order,
            n_eqs: eqs.len() as i64,
            edges,
            geos,
            owner_id,
            level,
            across,
            pipe_h: None,
        };
        if level == owner.level.unwrap() && !owner_geo.off_sheet {
            let h = p.height(&owner, &side.to_string(), port.offset)?;
            p.pipe_h = Some(
                h + match side {
                    'N' => 2,
                    'S' => -2,
                    _ => 0,
                },
            );
        }
        let kind = conns[us[0].0].kind.clone();
        let mut sides: Vec<bool> = us.iter().map(|&u| p.rel(u) < 0).collect();
        sides.dedup();
        let one_way = {
            let mut s = sides.clone();
            s.sort();
            s.dedup();
            s.len() == 1
        };
        let alike = one_way && !us.iter().any(|&u| p.at_edge(u)) && {
            let mut f: Vec<String> = us.iter().map(|&u| p.far_end(u)).collect();
            f.sort();
            f.dedup();
            f.len() == 1
        };
        let split = roles == [0];
        let made = trunks.get(&us[0].0).map_or(0, |v| v.len());
        let port_ref = format!("{owner_id}.{pname}");
        let west: Vec<Use> = us.iter().copied().filter(|&u| p.rel(u) < 0).collect();
        let east: Vec<Use> = us.iter().copied().filter(|&u| p.rel(u) >= 0).collect();
        let mut at = p.order[owner_id.as_str()] + 1;
        let mut run = Run {
            p: &p,
            alike,
            split,
            kind,
            level,
            ids: ids.into_iter(),
            refs: HashMap::new(),
            pieces: Vec::new(),
        };
        let placed: Vec<Equipment> = if ns && !west.is_empty() && !east.is_empty() {
            let riser = if side == 'N' { "s" } else { "n" };
            let mid = run.tee(&format!("{riser}we"));
            run.join(&port_ref, &format!("{}.{}", mid.id, riser));
            let mut w_chain = run.run(&format!("{}.w", mid.id), "w", &west)?;
            let e_chain = run.run(&format!("{}.e", mid.id), "e", &east)?;
            w_chain.reverse();
            w_chain.push(mid);
            w_chain.extend(e_chain);
            w_chain
        } else {
            let way = if !ns {
                side.to_ascii_lowercase().to_string()
            } else if 2 * east.len() >= us.len() {
                "e".to_string()
            } else {
                "w".to_string()
            };
            let mut chain = run.run(&port_ref, &way, us)?;
            if way == "w" {
                chain.reverse();
                at -= 1;
            }
            chain
        };
        let refs = std::mem::take(&mut run.refs);
        let pieces = std::mem::take(&mut run.pieces);
        drop(run);
        drop(p);
        trunks.entry(us[0].0).or_default().extend(pieces);
        eqs.splice(at..at, placed.iter().cloned());
        headers.push(placed.iter().map(|t| t.id.clone()).collect());
        if alike {
            for t in &placed {
                owners.insert(t.id.clone(), owner_id.clone());
            }
        }
        for tee in &placed {
            by_id.insert(tee.id.clone(), tee.clone());
            if settle {
                pending.push(PendingTee {
                    tee: tee.id.clone(),
                    owner: owner_id.clone(),
                    port: pname.clone(),
                    side,
                });
            }
        }
        let back = ns && !split && us.iter().all(|&(ci, _)| conns[ci].recycle);
        for &(ci, end) in us {
            let r = &refs[&(ci, end)];
            let c = if end == 0 {
                conns[ci].with_src(r)
            } else {
                conns[ci].with_dst(r)
            };
            conns[ci] = if back { c.with_recycle(false) } else { c };
        }
        if back {
            let pieces = trunks.get_mut(&us[0].0).unwrap();
            for c in pieces[made..].iter_mut() {
                c.recycle = true;
            }
        }
    }

    let mut out = Vec::new();
    for (ci, c) in conns.into_iter().enumerate() {
        if let Some(t) = trunks.remove(&ci) {
            out.extend(t);
        }
        out.push(c);
    }
    Ok(out)
}
