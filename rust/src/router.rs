//! Orthogonal pipe routing on the grid.
//!
//! Priority: (1) fewest bends, (2) shortest length.  Pipes leave and enter
//! every element perpendicular to the bounding-box face.  Strict mode forbids
//! any move opposite to a direction already travelled (no U-turns); relaxed
//! mode only forbids reversals in place.
//!
//! The search is the A* of the Python tool with the same order of states
//! (estimate, cost so far, insertion), so it finds the very same routes; the
//! Python tool's bit-board pruning only drops states that are on no cheapest
//! route, so it is not needed for the result.

use std::cmp::Reverse;
use std::collections::BinaryHeap;

use crate::error::{Error, Result};
use crate::geometry::Point;
use crate::layout::Placed;
use crate::pyval;

pub type Node = (i64, i64);
pub const DIRS: [(i64, i64); 4] = [(0, 1), (1, 0), (0, -1), (-1, 0)]; // N E S W
pub const TURN_COST: i64 = 1000;
pub const CROSS_COST: i64 = 20;

#[derive(Clone, Copy, Debug)]
pub struct Terminal {
    pub point: Point,
    pub edge: Node,
    pub first: Node,
    pub out_dir: usize,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Route {
    pub points: Vec<Point>,
    pub strict: bool,
    pub nodes: Vec<Node>,
}

impl Route {
    pub fn bends(&self) -> i64 {
        (self.points.len() as i64 - 2).max(0)
    }
}

/// Where a line meets ``p`` at its port.
pub fn resolve_port(p: &Placed, port_name: &str) -> Result<Terminal> {
    match p.geo.port_end(port_name) {
        None => {
            let mut known: Vec<&String> = p.geo.ports.keys().collect();
            known.sort();
            Err(Error::Key(format!(
                "{}: unknown port {}; known: {}",
                p.id,
                pyval::str_repr(port_name),
                pyval::list_repr(&known)
            )))
        }
        Some(Err(msg)) => Err(Error::Value(format!("{}.{}", p.id, msg))),
        Some(Ok(end)) => {
            let e = (end.edge.0 + p.x, end.edge.1 + p.y);
            Ok(Terminal {
                point: (end.point.0 + p.x as f64, end.point.1 + p.y as f64),
                edge: e,
                first: (e.0 + end.normal.0, e.1 + end.normal.1),
                out_dir: end.out_dir,
            })
        }
    }
}

/// Bends certainly needed to reach ``t`` moving in ``td``, now moving ``d``.
pub fn min_bends(p: Node, d: usize, t: Node, td: usize) -> i64 {
    if d == td {
        let (dx, dy) = DIRS[d];
        let (rx, ry) = (t.0 - p.0, t.1 - p.1);
        let on_line = rx * dy - ry * dx == 0 && rx * dx + ry * dy >= 0;
        return if on_line { 0 } else { 2 };
    }
    if d == (td + 2) % 4 {
        return 2;
    }
    1
}

fn needs(sd: usize, td: usize, s: Node, t: Node) -> [bool; 4] {
    let mut need = [false; 4];
    need[sd] = true;
    need[td] = true;
    let (dx, dy) = (t.0 - s.0, t.1 - s.1);
    if dx != 0 {
        need[if dx > 0 { 1 } else { 3 }] = true;
    }
    if dy != 0 {
        need[if dy > 0 { 0 } else { 2 }] = true;
    }
    need
}

/// Can a strict route exist at all (it is monotone in x and in y)?
pub fn strict_possible(s: Node, sd: usize, t: Node, td: usize) -> bool {
    if sd == (td + 2) % 4 {
        return true;
    }
    let need = needs(sd, td, s, t);
    !((need[0] && need[2]) || (need[1] && need[3]))
}

/// Per-thread buffers of the search, indexed by state and reused from one
/// search to the next (a state counts as unseen unless stamped with the
/// current search's number).
#[derive(Default)]
struct Scratch {
    stamp: u32,
    mark: Vec<u32>,
    dist: Vec<i64>,
    prev: Vec<u32>,
}

impl Scratch {
    fn start(&mut self, states: usize) {
        if self.mark.len() < states {
            self.mark.resize(states, 0);
            self.dist.resize(states, 0);
            self.prev.resize(states, u32::MAX);
        }
        self.stamp = self.stamp.wrapping_add(1);
        if self.stamp == 0 {
            self.mark.iter_mut().for_each(|m| *m = 0);
            self.stamp = 1;
        }
    }

    #[inline]
    fn get(&self, s: usize) -> i64 {
        if self.mark[s] == self.stamp {
            self.dist[s]
        } else {
            i64::MAX
        }
    }

    #[inline]
    fn set(&mut self, s: usize, d: i64, p: u32) {
        self.mark[s] = self.stamp;
        self.dist[s] = d;
        self.prev[s] = p;
    }

    #[inline]
    fn seen(&mut self, s: usize) -> bool {
        if self.mark[s] == self.stamp {
            return true;
        }
        self.mark[s] = self.stamp;
        false
    }
}

thread_local! {
    static SCRATCH: std::cell::RefCell<Scratch> = std::cell::RefCell::new(Scratch::default());
}

/// Routes lines one by one on the grid; a routed line becomes an obstacle.
#[derive(Clone)]
pub struct Router {
    pub x0: i64,
    pub y0: i64,
    pub h: i64,
    pub w: i64,
    pub blocked: Vec<u8>,
    pub axes: Vec<u8>,
    pub door: Vec<u8>,
    steps: [i64; 4],
}

impl Router {
    pub fn new(placed: &[Placed]) -> Router {
        Router::with_margin(placed, 3)
    }

    pub fn with_margin(placed: &[Placed], margin: i64) -> Router {
        let boxes: Vec<(i64, i64, i64, i64)> = placed.iter().map(|p| p.bbox()).collect();
        let xmin = boxes.iter().map(|b| b.0).min().unwrap() - margin;
        let xmax = boxes.iter().map(|b| b.2).max().unwrap() + margin;
        let ymin = boxes.iter().map(|b| b.1).min().unwrap() - margin;
        let ymax = boxes.iter().map(|b| b.3).max().unwrap() + margin;
        let (x0, y0) = (xmin - 1, ymin - 1);
        let h = ymax - ymin + 3;
        let w = xmax - xmin + 3;
        let n = (w * h) as usize;
        let mut blocked = vec![0u8; n];
        for i in 0..h {
            blocked[i as usize] = 1;
            blocked[((w - 1) * h + i) as usize] = 1;
        }
        for x in 0..w {
            blocked[(x * h) as usize] = 1;
            blocked[(x * h + h - 1) as usize] = 1;
        }
        for &(bx0, by0, bx1, by1) in &boxes {
            for x in bx0..=bx1 {
                let i = (x - x0) * h - y0;
                for y in by0..=by1 {
                    blocked[(i + y) as usize] = 1;
                }
            }
        }
        Router {
            x0,
            y0,
            h,
            w,
            blocked,
            axes: vec![0; n],
            door: vec![0; n],
            steps: [1, h, -1, -h],
        }
    }

    fn index(&self, p: Node) -> i64 {
        (p.0 - self.x0) * self.h + p.1 - self.y0
    }

    fn xy(&self, i: i64) -> Node {
        (i / self.h + self.x0, i % self.h + self.y0)
    }

    /// Keep the first cell outside every port (its doorstep) for that port's
    /// own line.
    pub fn reserve(&mut self, terminals: &[Terminal]) {
        for t in terminals {
            let i = self.index(t.first) as usize;
            self.door[i] = 1 << (t.out_dir % 2);
        }
    }

    /// Can a strict search that is no arch get from ``s`` to ``t`` at all?
    fn staircase(&self, s: Node, sd: usize, t: Node, td: usize) -> bool {
        let (blocked, axes, door, h) = (&self.blocked, &self.axes, &self.door, self.h);
        let (si, ti) = (self.index(s), self.index(t));
        if si == ti && sd == td {
            return true;
        }
        if axes[ti as usize] != 0 {
            return false;
        }
        let (dx, dy) = (t.0 - s.0, t.1 - s.1);
        let need = needs(sd, td, s, t);
        if (need[0] && need[2]) || (need[1] && need[3]) {
            return true;
        }
        let xstep = if need[1] {
            h
        } else if need[3] {
            -h
        } else {
            0
        };
        let ystep = if need[0] {
            1
        } else if need[2] {
            -1
        } else {
            0
        };
        let (nx, ny) = (dx.abs() + 1, dy.abs() + 1);
        let mut reach = vec![0u8; (nx * ny) as usize];
        reach[0] = 1;
        for a in 0..nx {
            let col = si + a * xstep;
            let row = a * ny;
            for b in (if a == 0 { 1 } else { 0 })..ny {
                let n = (col + b * ystep) as usize;
                if blocked[n] != 0 {
                    continue;
                }
                let k = (row + b) as usize;
                if b != 0 && reach[k - 1] != 0 && axes[n] & 1 == 0 && (door[n] != 1 || n as i64 == ti) {
                    reach[k] = 1;
                    continue;
                }
                if a != 0 && reach[k - ny as usize] != 0 && axes[n] & 2 == 0 && (door[n] != 2 || n as i64 == ti) {
                    reach[k] = 1;
                }
            }
        }
        if blocked[ti as usize] != 0 {
            return false;
        }
        let last = (nx * ny - 1) as usize;
        if td.is_multiple_of(2) {
            return ny > 1 && reach[last - 1] != 0;
        }
        nx > 1 && reach[last - ny as usize] != 0
    }

    /// A* over (node, direction, travelled-directions mask).
    fn search(&self, s: Node, sd: usize, t: Node, td: usize, strict: bool) -> Option<Vec<Node>> {
        let (blocked, axes, door) = (&self.blocked, &self.axes, &self.door);
        let (tx, ty) = t;
        let ti = self.index(t);
        let si = self.index(s);
        let (tdx, tdy) = DIRS[td];
        let back = (td + 2) % 4;
        let arch = sd == back;
        if strict && !arch && !self.staircase(s, sd, t, td) {
            return None;
        }
        // per direction: (nd, turn, step, dx, dy, rev bit, axis bit, own bit, reverse ok, bends)
        let mut moves = [[(0usize, false, 0i64, 0i64, 0i64, 0u64, 0u8, 0u64, false, 0i64); 3]; 4];
        for d in 0..4 {
            for (k, nd) in [d, (d + 1) % 4, (d + 3) % 4].into_iter().enumerate() {
                moves[d][k] = (
                    nd,
                    nd != d,
                    self.steps[nd],
                    DIRS[nd].0,
                    DIRS[nd].1,
                    1u64 << ((nd + 2) % 4),
                    1u8 << (nd % 2),
                    1u64 << nd,
                    arch && nd == td,
                    if nd == td {
                        -1
                    } else {
                        (if nd == back { 2 } else { 1 }) * TURN_COST
                    },
                );
            }
        }
        let est = (t.0 - s.0).abs() + (t.1 - s.1).abs() + TURN_COST * min_bends(s, sd, t, td);
        let start: u64 = ((si as u64) * 4 + sd as u64) * 16 + if strict { 1u64 << sd } else { 0 };
        SCRATCH.with_borrow_mut(|sc| {
            sc.start(self.blocked.len() * 64);
            sc.set(start as usize, 0, u32::MAX);
            // an entry is one number ordered like (f, cost so far, insertion, state)
            let pack = |f: i64, g: i64, n: u64, st: u64| -> u128 {
                ((f as u128) << 104) | ((g as u128) << 64) | ((n as u128) << 32) | st as u128
            };
            let mut heap: BinaryHeap<Reverse<u128>> = BinaryHeap::new();
            heap.push(Reverse(pack(est, 0, 0, start)));
            let mut counter: u64 = 1;
            while let Some(Reverse(key)) = heap.pop() {
                let state = (key & 0xffff_ffff) as u64;
                let cost = ((key >> 64) & ((1u128 << 40) - 1)) as i64;
                if sc.get(state as usize) < cost {
                    continue;
                }
                let mask = state & 15;
                let i = (state >> 6) as i64;
                let d = ((state >> 4) & 3) as usize;
                let here = axes[i as usize];
                if i == ti && d == td && here == 0 {
                    let mut path = Vec::new();
                    let mut st = state as u32;
                    loop {
                        path.push(self.xy((st >> 6) as i64));
                        let p = sc.prev[st as usize];
                        if p == u32::MAX {
                            path.reverse();
                            return Some(path);
                        }
                        st = p;
                    }
                }
                let (x, y) = self.xy(i);
                for &(nd, turn, step, dx, dy, rev, axis, bit, reverse_ok, bends0) in &moves[d] {
                    if turn && (here != 0 || (door[i as usize] != 0 && i != si)) {
                        continue;
                    }
                    if strict && mask & rev != 0 && !reverse_ok {
                        continue;
                    }
                    let n = i + step;
                    let nu = n as usize;
                    if blocked[nu] != 0 {
                        continue;
                    }
                    if door[nu] == axis && n != ti {
                        continue;
                    }
                    let used = axes[nu];
                    if used & axis != 0 {
                        continue;
                    }
                    let mut nc = cost + 1;
                    if turn {
                        nc += TURN_COST;
                    }
                    if used != 0 {
                        nc += CROSS_COST;
                    }
                    let nmask = if strict { mask | bit } else { 0 };
                    let nstate = ((n as u64) * 4 + nd as u64) * 16 + nmask;
                    if nc < sc.get(nstate as usize) {
                        let rx = tx - x - dx;
                        let ry = ty - y - dy;
                        let run = rx.abs() + ry.abs();
                        sc.set(nstate as usize, nc, state as u32);
                        let mut bends = bends0;
                        if bends < 0 {
                            let on = rx * tdy - ry * tdx == 0 && rx * tdx + ry * tdy >= 0;
                            bends = if on { 0 } else { 2 * TURN_COST };
                        }
                        heap.push(Reverse(pack(nc + run + bends, nc, counter, nstate)));
                        counter += 1;
                    }
                }
            }
            None
        })
    }

    /// Is the goal reachable with the search's moves (``axes``: the pipes
    /// laid)?  A flood over the search's states.
    fn reachable(&self, s: Node, sd: usize, t: Node, td: usize, strict: bool, axes: &[u8]) -> bool {
        let (blocked, door) = (&self.blocked, &self.door);
        let (si, ti) = (self.index(s), self.index(t));
        if axes[ti as usize] != 0 {
            return false;
        }
        if si == ti && sd == td {
            return true;
        }
        let arch = sd == (td + 2) % 4;
        let start: u64 = ((si as u64) * 4 + sd as u64) * 16 + if strict { 1u64 << sd } else { 0 };
        SCRATCH.with_borrow_mut(|sc| {
            sc.start(self.blocked.len() * 64);
            sc.seen(start as usize);
            let mut todo = vec![start];
            while let Some(state) = todo.pop() {
                let mask = state & 15;
                let i = (state >> 6) as i64;
                let d = ((state >> 4) & 3) as usize;
                if i == ti && d == td {
                    return true;
                }
                let here = axes[i as usize];
                for nd in [d, (d + 1) % 4, (d + 3) % 4] {
                    let turn = nd != d;
                    if turn && (here != 0 || (door[i as usize] != 0 && i != si)) {
                        continue;
                    }
                    if strict && mask & (1 << ((nd + 2) % 4)) != 0 && !(arch && nd == td) {
                        continue;
                    }
                    let n = i + self.steps[nd];
                    let nu = n as usize;
                    let axis = 1u8 << (nd % 2);
                    if blocked[nu] != 0 || (door[nu] == axis && n != ti) || axes[nu] & axis != 0 {
                        continue;
                    }
                    let nmask = if strict { mask | (1 << nd) } else { 0 };
                    let nstate = ((n as u64) * 4 + nd as u64) * 16 + nmask;
                    if !sc.seen(nstate as usize) {
                        todo.push(nstate);
                    }
                }
            }
            false
        })
    }

    fn commit(&mut self, nodes: &[Node]) {
        for w in nodes.windows(2) {
            let (a, b) = (w[0], w[1]);
            let bit = if a.0 == b.0 { 1 } else { 2 };
            let ia = self.index(a) as usize;
            let ib = self.index(b) as usize;
            self.axes[ia] |= bit;
            self.axes[ib] |= bit;
        }
    }

    pub fn route(&mut self, src: &Terminal, dst: &Terminal, allow_relaxed: bool, commit: bool) -> Option<Route> {
        if src.first == dst.edge && dst.first == src.edge {
            let pts = simplify(&[src.point, f(src.edge), f(dst.edge), dst.point]);
            return Some(Route {
                points: pts,
                strict: true,
                nodes: Vec::new(),
            });
        }
        let td = (dst.out_dir + 2) % 4;
        let mut modes: Vec<bool> = if allow_relaxed { vec![true, false] } else { vec![true] };
        if !strict_possible(src.first, src.out_dir, dst.first, td) {
            modes.remove(0);
        }
        for strict in modes {
            let Some(nodes) = self.search(src.first, src.out_dir, dst.first, td, strict) else {
                continue;
            };
            let mut full = vec![src.point, f(src.edge)];
            full.extend(nodes.iter().map(|&n| f(n)));
            full.push(f(dst.edge));
            full.push(dst.point);
            if commit {
                self.commit(&nodes);
            }
            return Some(Route {
                points: simplify(&full),
                strict,
                nodes,
            });
        }
        None
    }

    /// Would the line route with no other line laid?
    pub fn routes_alone(&self, src: &Terminal, dst: &Terminal, allow_relaxed: bool) -> bool {
        if src.first == dst.edge && dst.first == src.edge {
            return true;
        }
        let (s, sd, t) = (src.first, src.out_dir, dst.first);
        let td = (dst.out_dir + 2) % 4;
        if !allow_relaxed && !strict_possible(s, sd, t, td) {
            return false;
        }
        let none = vec![0u8; self.axes.len()];
        self.reachable(s, sd, t, td, !allow_relaxed, &none)
    }

    /// Route corresponding lines of repeated units with one shape.
    pub fn route_together(&mut self, ends: &[(Terminal, Terminal, Node)], allow_relaxed: bool) -> Option<Vec<Route>> {
        let (src, dst, _) = ends[0];
        if src.first == dst.edge && dst.first == src.edge {
            return None;
        }
        let shifts: Vec<Node> = ends.iter().map(|e| e.2).collect();
        let (b, a, d) = self.merged(&shifts);
        let saved_b = std::mem::replace(&mut self.blocked, b);
        let saved_a = std::mem::replace(&mut self.axes, a);
        let saved_d = std::mem::replace(&mut self.door, d);
        let model = self.route(&src, &dst, allow_relaxed, false);
        self.blocked = saved_b;
        self.axes = saved_a;
        self.door = saved_d;
        let model = model?;
        let before = self.axes.clone();
        let mut routes = Vec::new();
        for (s, d, sh) in ends {
            match self.replay(s, d, &model, *sh) {
                None => {
                    self.axes = before;
                    return None;
                }
                Some(r) => routes.push(r),
            }
        }
        Some(routes)
    }

    fn merged(&self, shifts: &[Node]) -> (Vec<u8>, Vec<u8>, Vec<u8>) {
        let (h, n) = (self.h, self.blocked.len());
        let mut blocked = self.blocked.clone();
        let mut axes = self.axes.clone();
        let mut door = self.door.clone();
        for &(dx, dy) in shifts {
            if (dx, dy) == (0, 0) {
                continue;
            }
            let off = dx * h + dy;
            for i in 0..n {
                let j = i as i64 + off;
                let inside = 0 <= j && (j as usize) < n;
                let (b, a, d) = if inside {
                    let j = j as usize;
                    (self.blocked[j], self.axes[j], self.door[j])
                } else {
                    (1, 0, 0)
                };
                let y = i as i64 % h;
                let col = if !(0 <= y + dy && y + dy < h) { 1 } else { 0 };
                blocked[i] |= b | col;
                axes[i] |= a;
                door[i] |= d;
            }
        }
        (blocked, axes, door)
    }

    /// Route like ``model``, moved by ``shift``, if that path is free.
    pub fn replay(&mut self, src: &Terminal, dst: &Terminal, model: &Route, shift: Node) -> Option<Route> {
        if model.nodes.is_empty() {
            return None;
        }
        let (dx, dy) = shift;
        let nodes: Vec<Node> = model.nodes.iter().map(|&(x, y)| (x + dx, y + dy)).collect();
        if nodes[0] != src.first || *nodes.last().unwrap() != dst.first {
            return None;
        }
        let (h, w) = (self.h, self.w);
        for &(x, y) in &nodes {
            if !(0 < x - self.x0 && x - self.x0 < w - 1 && 0 < y - self.y0 && y - self.y0 < h - 1) {
                return None;
            }
        }
        let idx: Vec<usize> = nodes.iter().map(|&p| self.index(p) as usize).collect();
        if self.blocked[idx[0]] != 0 {
            return None;
        }
        let (first, last) = (idx[0], *idx.last().unwrap());
        let mut d = src.out_dir;
        for k in 0..nodes.len() - 1 {
            let (a, b) = (nodes[k], nodes[k + 1]);
            let (i, j) = (idx[k], idx[k + 1]);
            let nd = DIRS.iter().position(|&v| v == (b.0 - a.0, b.1 - a.1))?;
            if nd == (d + 2) % 4 || self.blocked[j] != 0 || self.axes[j] & (1 << (nd % 2)) != 0 {
                return None;
            }
            if self.door[j] == 1 << (nd % 2) && j != last {
                return None;
            }
            if nd != d && (self.axes[i] != 0 || (self.door[i] != 0 && i != first)) {
                return None;
            }
            d = nd;
        }
        if d != (dst.out_dir + 2) % 4 || self.axes[last] != 0 {
            return None;
        }
        let mut full = vec![src.point, f(src.edge)];
        full.extend(nodes.iter().map(|&n| f(n)));
        full.push(f(dst.edge));
        full.push(dst.point);
        self.commit(&nodes);
        Some(Route {
            points: simplify(&full),
            strict: model.strict,
            nodes,
        })
    }
}

fn f(n: Node) -> Point {
    (n.0 as f64, n.1 as f64)
}

/// Drop duplicate and collinear points.
pub fn simplify(pts: &[Point]) -> Vec<Point> {
    let mut out: Vec<Point> = Vec::new();
    for &p in pts {
        if out.last() == Some(&p) {
            continue;
        }
        while out.len() >= 2 {
            let (a, b) = (out[out.len() - 2], out[out.len() - 1]);
            if (b.0 - a.0) * (p.1 - b.1) - (b.1 - a.1) * (p.0 - b.0) == 0.0 {
                out.pop();
            } else {
                break;
            }
        }
        out.push(p);
    }
    out
}

pub type BoxF = (f64, f64, f64, f64);

/// Does the route turn back on itself with nothing in the way?
pub fn needless_uturn(points: &[Point], boxes: &[BoxF]) -> bool {
    let dirs: Vec<usize> = points
        .windows(2)
        .map(|w| {
            let (p, q) = (w[0], w[1]);
            if p.0 == q.0 {
                if q.1 > p.1 { 0 } else { 2 }
            } else if q.0 > p.0 {
                1
            } else {
                3
            }
        })
        .collect();
    for (i, &d) in dirs.iter().enumerate() {
        let Some(j) = (i + 1..dirs.len()).find(|&j| dirs[j] == (d + 2) % 4) else {
            continue;
        };
        let a = if d == 0 || d == 2 { 1 } else { 0 };
        let get = |p: Point, k: usize| if k == 0 { p.0 } else { p.1 };
        let join = &points[i + 1..j + 1];
        let across: Vec<f64> = join.iter().map(|&p| get(p, 1 - a)).collect();
        let ends = (get(points[i], a), get(points[j + 1], a));
        let along: Vec<f64> = join.iter().map(|&p| get(p, a)).collect();
        let fmax = |v: &[f64]| v.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
        let fmin = |v: &[f64]| v.iter().cloned().fold(f64::INFINITY, f64::min);
        let depth = if d == 0 || d == 1 {
            (ends.0.min(ends.1), fmax(&along))
        } else {
            (fmin(&along), ends.0.max(ends.1))
        };
        let mut lo = [0.0f64; 2];
        let mut hi = [0.0f64; 2];
        lo[1 - a] = fmin(&across);
        hi[1 - a] = fmax(&across);
        lo[a] = depth.0;
        hi[a] = depth.1;
        let around = |b: &BoxF| -> bool {
            let b0 = [b.0, b.1];
            let b1 = [b.2, b.3];
            if (0..2).all(|k| b0[k] < hi[k] && b1[k] > lo[k]) {
                return true;
            }
            let k = 1 - a;
            let between = lo[k] <= b0[k] && b1[k] <= hi[k];
            between && b0[a] <= hi[a] && b1[a] >= lo[a]
        };
        if !boxes.iter().any(around) {
            return true;
        }
    }
    false
}
