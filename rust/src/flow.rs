//! Left-to-right order from the direction of flow (``layout="flow"``); see
//! the Python ``flow`` module for the rules.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::Arc;

use indexmap::IndexMap;

use crate::elements::Geometry;
use crate::netlist::{Connection, Equipment};

/// ``a`` lies left of ``b``; the line joins ``a.a_port`` and ``b.b_port``.
#[derive(Clone, Debug)]
pub struct Arrow {
    pub ci: usize,
    pub a: usize,
    pub a_port: String,
    pub b: usize,
    pub b_port: String,
}

#[derive(Clone, Debug, Default)]
pub struct FlowPlan {
    pub order: Vec<usize>,
    pub back: HashSet<usize>,
    pub forward: Vec<Arrow>,
    pub main: Vec<usize>,
}

pub fn plan_flow(
    eqs: &[Equipment],
    conns: &[Connection],
    geos: &HashMap<String, Arc<Geometry>>,
    process_kinds: &HashSet<String>,
    owners: Option<&IndexMap<String, String>>,
) -> FlowPlan {
    let n = eqs.len();
    let index: HashMap<&str, usize> = eqs.iter().enumerate().map(|(i, e)| (e.id.as_str(), i)).collect();
    let mut arrows: Vec<Arrow> = Vec::new();
    let mut attach: Vec<(usize, usize, String, usize, String)> = Vec::new();
    for (ci, c) in conns.iter().enumerate() {
        let [(s, sp), (d, dp)] = c.ends();
        let (Some(&u), Some(&v)) = (index.get(s), index.get(d)) else {
            continue;
        };
        if u == v || !geos[s].ports.contains_key(sp) || !geos[d].ports.contains_key(dp) {
            continue;
        }
        if !process_kinds.contains(&c.kind) {
            attach.push((ci, u, sp.into(), v, dp.into()));
            continue;
        }
        let leftwards = runs_left(geos[s].ports[sp].side, geos[d].ports[dp].side);
        arrows.push(if leftwards {
            Arrow {
                ci,
                a: v,
                a_port: dp.into(),
                b: u,
                b_port: sp.into(),
            }
        } else {
            Arrow {
                ci,
                a: u,
                a_port: sp.into(),
                b: v,
                b_port: dp.into(),
            }
        });
    }
    let edges: Vec<(usize, usize, usize)> = arrows.iter().map(|a| (a.ci, a.a, a.b)).collect();
    let by_ci: HashMap<usize, &Arrow> = arrows.iter().map(|a| (a.ci, a)).collect();
    let mut back: HashSet<usize> = edges.iter().filter(|e| conns[e.0].recycle).map(|e| e.0).collect();
    let mut out: Vec<Vec<(usize, usize)>> = vec![Vec::new(); n];
    let mut indeg = vec![0usize; n];
    for &(ci, u, v) in &edges {
        if !back.contains(&ci) {
            out[u].push((ci, v));
            indeg[v] += 1;
        }
    }
    close_loops(n, &mut out, &indeg, &mut back);
    let forward: Vec<(usize, usize, usize)> = edges.iter().copied().filter(|e| !back.contains(&e.0)).collect();

    let empty = IndexMap::new();
    let owners = owners.unwrap_or(&empty);
    let taps: BTreeSet<usize> = owners.keys().filter_map(|t| index.get(t.as_str()).copied()).collect();
    let mut process: BTreeSet<usize> = edges.iter().flat_map(|&(_, u, v)| [u, v]).collect();
    process.retain(|i| !taps.contains(i));
    let mut rise: HashMap<usize, i64> = HashMap::new();
    for a in &arrows {
        let g = &geos[&eqs[a.a].id];
        if !g.nozzles {
            continue;
        }
        let port = g.ports[&a.a_port];
        rise.insert(
            a.ci,
            match port.side {
                'N' => 1 << 20,
                'S' => -(1 << 20),
                _ => port.offset,
            },
        );
    }
    let through = through_taps(&forward, &taps);
    let walk = branch_order(n, &through, &process, &rise);
    let mut keys: HashMap<usize, Vec<i64>> = walk.iter().enumerate().map(|(k, &i)| (i, vec![k as i64])).collect();
    place_taps(&mut keys, &taps, conns, &index, eqs, owners, geos);
    attach_items(&mut keys, &attach, eqs, geos);
    for i in 0..n {
        keys.entry(i).or_insert_with(|| vec![(n + i) as i64]);
    }
    let width = keys.values().map(Vec::len).max().unwrap_or(0);
    let padded = |i: usize| {
        let mut k = keys[&i].clone();
        k.resize(width, 0);
        k
    };
    let mut order: Vec<usize> = (0..n).collect();
    order.sort_by_key(|&i| padded(i));
    let main = main_path(n, &through, &process, &rise);
    FlowPlan {
        order,
        back,
        forward: forward.iter().map(|&(ci, _, _)| by_ci[&ci].clone()).collect(),
        main,
    }
}

fn longest(u: usize, succ: &[Vec<usize>], tail: &mut HashMap<usize, i64>) -> i64 {
    if let Some(&t) = tail.get(&u) {
        return t;
    }
    tail.insert(u, 0);
    let mut best = 0;
    for &v in &succ[u] {
        best = best.max(1 + longest(v, succ, tail));
    }
    tail.insert(u, best);
    best
}

/// The main flow: from the feed with the longest way on, always on to the
/// item with the longest way on.
fn main_path(
    n: usize,
    forward: &[(usize, usize, usize)],
    process: &BTreeSet<usize>,
    rise: &HashMap<usize, i64>,
) -> Vec<usize> {
    let mut succ: Vec<Vec<(usize, i64)>> = vec![Vec::new(); n];
    let mut pred = vec![0usize; n];
    for &(ci, u, v) in forward {
        succ[u].push((v, *rise.get(&ci).unwrap_or(&0)));
        pred[v] += 1;
    }
    let plain: Vec<Vec<usize>> = succ.iter().map(|s| s.iter().map(|e| e.0).collect()).collect();
    let mut tail = HashMap::new();
    let mut feeds: Vec<usize> = process.iter().copied().filter(|&i| pred[i] == 0).collect();
    let lens: HashMap<usize, i64> = (0..n).map(|i| (i, longest(i, &plain, &mut tail))).collect();
    feeds.sort_by_key(|&i| (-lens[&i], i));
    let mut path: Vec<usize> = Vec::new();
    let mut u = feeds.first().map(|&f| f as i64).unwrap_or(-1);
    while u >= 0 && !path.contains(&(u as usize)) {
        let uu = u as usize;
        path.push(uu);
        let mut best: Option<(i64, i64, i64, usize)> = None;
        for &(v, r) in &succ[uu] {
            let k = (lens[&v], -r, -(v as i64), v);
            if best.is_none_or(|b| (k.0, k.1, k.2) > (b.0, b.1, b.2)) {
                best = Some(k);
            }
        }
        u = best.map(|b| b.3 as i64).unwrap_or(-1);
    }
    path
}

/// Fold a long flow into rows, one major unit (a column) per row.
pub fn split_rows(plan: &FlowPlan, majors: &HashSet<usize>, edge: &HashSet<usize>, min_head: usize) -> Vec<Vec<usize>> {
    let order = &plan.order;
    let pos: HashMap<usize, usize> = order.iter().enumerate().map(|(k, &i)| (i, k)).collect();
    let on_main: Vec<usize> = plan.main.iter().copied().filter(|i| majors.contains(i)).collect();
    if on_main.len() < 2 {
        return vec![order.clone()];
    }
    let mut cuts: BTreeSet<usize> = BTreeSet::new();
    let idx = |m: usize| plan.main.iter().position(|&x| x == m).unwrap();
    let first = idx(on_main[0]);
    if first > 0 {
        let at = pos[&plan.main[first - 1]] + 1;
        if order[..at].iter().filter(|i| !edge.contains(i)).count() >= min_head {
            cuts.insert(at);
        }
    }
    for &m in &on_main[..on_main.len() - 1] {
        let k = idx(m);
        if k + 1 < plan.main.len() {
            cuts.insert(pos[&plan.main[k + 1]]);
        }
    }
    let mut bounds = vec![0];
    bounds.extend(cuts.into_iter().filter(|&c| 0 < c && c < order.len()));
    bounds.push(order.len());
    bounds
        .windows(2)
        .filter(|w| w[1] > w[0])
        .map(|w| order[w[0]..w[1]].to_vec())
        .collect()
}

/// The arrows with every run of taps taken out.
fn through_taps(forward: &[(usize, usize, usize)], taps: &BTreeSet<usize>) -> Vec<(usize, usize, usize)> {
    if taps.is_empty() {
        return forward.to_vec();
    }
    let mut succ: HashMap<usize, Vec<usize>> = HashMap::new();
    for &(_, u, v) in forward {
        succ.entry(u).or_default().push(v);
    }
    let mut out = Vec::new();
    for &(ci, u, v) in forward {
        if taps.contains(&u) {
            continue;
        }
        let mut stack = vec![v];
        let mut seen = HashSet::new();
        while let Some(w) = stack.pop() {
            if !seen.insert(w) {
                continue;
            }
            if taps.contains(&w) {
                if let Some(s) = succ.get(&w) {
                    stack.extend(s);
                }
            } else {
                out.push((ci, u, w));
            }
        }
    }
    out
}

fn place_taps(
    keys: &mut HashMap<usize, Vec<i64>>,
    taps: &BTreeSet<usize>,
    conns: &[Connection],
    index: &HashMap<&str, usize>,
    eqs: &[Equipment],
    owners: &IndexMap<String, String>,
    geos: &HashMap<String, Arc<Geometry>>,
) {
    let mut partner: HashMap<usize, (i64, i64)> = HashMap::new();
    for c in conns {
        let ends = c.ends();
        for ((t, tp), (o, op)) in [(ends[0], ends[1]), (ends[1], ends[0])] {
            if tp != "n" && tp != "s" {
                continue;
            }
            if owners.contains_key(t) && !owners.contains_key(o) && index.contains_key(o) && o != owners[t] {
                let side = geos[o].ports.get(op).map(|p| p.side);
                partner.insert(index[t], (index[o] as i64, if side == Some('W') { -1 } else { 1 }));
            }
        }
    }
    for &t in taps {
        let (p, where_) = partner.get(&t).copied().unwrap_or_else(|| {
            (
                index.get(owners[&eqs[t].id].as_str()).map(|&i| i as i64).unwrap_or(-1),
                1,
            )
        });
        if p >= 0 {
            if let Some(kp) = keys.get(&(p as usize)).cloned() {
                let mut k = kp;
                k.push(where_);
                k.push(t as i64);
                keys.insert(t, k);
            }
        }
    }
}

/// Does a line leaving a port on ``out_side`` and entering one on
/// ``in_side`` run leftwards?
pub fn runs_left(out_side: char, in_side: char) -> bool {
    if out_side == 'W' || out_side == 'E' {
        return out_side == 'W';
    }
    if in_side == 'W' || in_side == 'E' {
        return in_side == 'E';
    }
    false
}

/// Mark every arrow that closes a loop as a recycle.
fn close_loops(n: usize, out: &mut [Vec<(usize, usize)>], indeg: &[usize], back: &mut HashSet<usize>) {
    let mut color = vec![0u8; n];
    let roots: Vec<usize> = (0..n).filter(|&i| indeg[i] == 0).chain(0..n).collect();
    for root in roots {
        if color[root] != 0 {
            continue;
        }
        color[root] = 1;
        let mut stack: Vec<(usize, usize)> = vec![(root, 0)];
        while let Some(&mut (node, ref mut pos)) = stack.last_mut() {
            if *pos >= out[node].len() {
                color[node] = 2;
                stack.pop();
                continue;
            }
            let (ci, v) = out[node][*pos];
            *pos += 1;
            if color[v] == 1 {
                back.insert(ci);
            } else if color[v] == 0 {
                color[v] = 1;
                stack.push((v, 0));
            }
        }
    }
    for edges in out.iter_mut() {
        edges.retain(|(ci, _)| !back.contains(ci));
    }
}

struct Walk<'a> {
    succ: Vec<Vec<usize>>,
    pred: Vec<Vec<usize>>,
    height: HashMap<(usize, usize), i64>,
    tail: HashMap<usize, i64>,
    up: HashMap<usize, HashSet<usize>>,
    order: Vec<usize>,
    seen: HashSet<usize>,
    _p: std::marker::PhantomData<&'a ()>,
}

impl Walk<'_> {
    fn length(&mut self, u: usize) -> i64 {
        longest(u, &self.succ, &mut self.tail)
    }

    fn ancestors(&mut self, u: usize) -> HashSet<usize> {
        if let Some(a) = self.up.get(&u) {
            return a.clone();
        }
        self.up.insert(u, HashSet::new());
        let preds = self.pred[u].clone();
        let mut all: HashSet<usize> = preds.iter().copied().collect();
        for p in preds {
            all.extend(self.ancestors(p));
        }
        self.up.insert(u, all.clone());
        all
    }

    fn waits(&mut self, v: usize) -> bool {
        if self.pred[v].len() <= 1 {
            return false;
        }
        for p in self.pred[v].clone() {
            if !self.seen.contains(&p) {
                let anc = self.ancestors(p);
                if anc.iter().any(|a| self.seen.contains(a)) {
                    return true;
                }
            }
        }
        false
    }

    fn visit(&mut self, u: usize) {
        if !self.seen.insert(u) {
            return;
        }
        for p in self.pred[u].clone() {
            self.visit(p);
        }
        self.order.push(u);
        let succ = self.succ[u].clone();
        let mut ranks: Vec<((i64, i64), usize)> = Vec::new();
        for &v in &succ {
            let l = self.length(v);
            ranks.push(((l, -self.height[&(u, v)]), v));
        }
        ranks.sort();
        let mut main: i64 = -1;
        if ranks.len() > 1 && ranks[ranks.len() - 1].0 != ranks[ranks.len() - 2].0 {
            main = ranks[ranks.len() - 1].1 as i64;
        }
        let mut keyed: Vec<((bool, i64, i64, bool, usize), usize)> = Vec::new();
        for &v in &succ {
            let k = (
                v as i64 == main,
                self.height[&(u, v)],
                self.length(v),
                self.pred[v].len() > 1,
                v,
            );
            keyed.push((k, v));
        }
        keyed.sort_by_key(|a| a.0);
        for (_, v) in keyed {
            if !self.waits(v) {
                self.visit(v);
            }
        }
    }
}

/// Left-to-right order that keeps every branch together.
fn branch_order(
    n: usize,
    forward: &[(usize, usize, usize)],
    process: &BTreeSet<usize>,
    rise: &HashMap<usize, i64>,
) -> Vec<usize> {
    let mut w = Walk {
        succ: vec![Vec::new(); n],
        pred: vec![Vec::new(); n],
        height: HashMap::new(),
        tail: HashMap::new(),
        up: HashMap::new(),
        order: Vec::new(),
        seen: HashSet::new(),
        _p: std::marker::PhantomData,
    };
    for &(ci, u, v) in forward {
        w.succ[u].push(v);
        w.pred[v].push(u);
        let h = *rise.get(&ci).unwrap_or(&0);
        let cur = w.height.get(&(u, v)).copied().unwrap_or(h);
        w.height.insert((u, v), cur.max(h));
    }
    let mut feeds: Vec<usize> = process.iter().copied().filter(|&i| w.pred[i].is_empty()).collect();
    let lens: HashMap<usize, i64> = feeds.iter().map(|&i| (i, w.length(i))).collect();
    feeds.sort_by_key(|&i| (-lens[&i], i));
    let mut starts: Vec<usize> = feeds.into_iter().take(1).collect();
    starts.extend(process.iter().copied());
    for i in starts {
        w.visit(i);
    }
    w.order
}

/// Order items joined only by instrument / signal lines next to their partner.
fn attach_items(
    keys: &mut HashMap<usize, Vec<i64>>,
    attach: &[(usize, usize, String, usize, String)],
    eqs: &[Equipment],
    geos: &HashMap<String, Arc<Geometry>>,
) {
    let mut pending: Vec<&(usize, usize, String, usize, String)> = attach.iter().collect();
    while !pending.is_empty() {
        let mut left = Vec::new();
        for item in &pending {
            let (_, a, ap, b, bp) = item;
            let mut done = false;
            for (anchor, aport, it, iport) in [(*a, ap, *b, bp), (*b, bp, *a, ap)] {
                if keys.contains_key(&anchor) && !keys.contains_key(&it) {
                    let side = geos[&eqs[anchor].id].ports[aport.as_str()].side;
                    let own = geos[&eqs[it].id].ports[iport.as_str()].side;
                    let before = if side == 'W' || side == 'E' {
                        side == 'W'
                    } else {
                        own == 'E'
                    };
                    let mut k = keys[&anchor].clone();
                    k.push(if before { -1 } else { 1 });
                    k.push(it as i64);
                    keys.insert(it, k);
                    done = true;
                    break;
                }
            }
            if !done && !(keys.contains_key(a) && keys.contains_key(b)) {
                left.push(*item);
            }
        }
        if left.len() == pending.len() {
            break;
        }
        pending = left;
    }
}
