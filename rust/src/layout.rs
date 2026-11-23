//! Grid placement: left-to-right order, levels (Y) and compaction (see the
//! Python ``layout`` module).

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use indexmap::IndexMap;

use crate::elements::Geometry;
use crate::error::{Error, Result};

/// One element to place.
#[derive(Clone, Debug)]
pub struct Item {
    pub id: String,
    pub typ: String,
    pub level: i64,
    pub geo: Arc<Geometry>,
    pub elevation: Option<i64>,
    pub edge: Option<String>,
    pub span: i64,
}

#[derive(Clone, Debug)]
pub struct Placed {
    pub id: String,
    pub typ: String,
    pub level: i64,
    pub geo: Arc<Geometry>,
    pub x: i64,
    pub y: i64,
}

impl Placed {
    pub fn bbox(&self) -> (i64, i64, i64, i64) {
        (self.x, self.y, self.x + self.geo.width, self.y + self.geo.height)
    }
}

pub const FLANGED_RUN: i64 = 2;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Link {
    pub a: usize,
    pub a_side: char,
    pub a_off: i64,
    pub b: usize,
    pub b_side: char,
    pub b_off: i64,
    pub flanged: bool,
}

fn ns(c: char) -> bool {
    c == 'N' || c == 'S'
}

/// Smallest x of ``b`` that lets the pipe run without doubling back.
pub fn link_min_x(link: &Link, ax: i64, a_geo: &Geometry, ay: i64, by: i64) -> Option<i64> {
    if link.a_side == 'E' {
        let right = ax + a_geo.width;
        if link.b_side == 'W' {
            if ay + link.a_off != by + link.b_off {
                return Some(right + 3);
            }
            return Some(right + if link.flanged { FLANGED_RUN } else { 1 });
        }
        if ns(link.b_side) {
            return Some(right + 2 - link.b_off);
        }
    } else if ns(link.a_side) && link.b_side == 'W' {
        return Some(ax + link.a_off + 2);
    } else if ns(link.a_side) && ns(link.b_side) && link.a_side != link.b_side {
        return Some(ax + link.a_off - link.b_off);
    }
    None
}

pub fn bands(items: &[Item], gap_y: &[i64]) -> Result<(Vec<i64>, Vec<i64>)> {
    let top = items.iter().map(|it| it.level).max().unwrap_or(0);
    let mut height = vec![0i64; (top + 1) as usize];
    let mut tall: Vec<&Item> = Vec::new();
    for it in items {
        if it.level < 0 || it.elevation.unwrap_or(0) < 0 {
            return Err(Error::Value("level and elevation must be >= 0".into()));
        }
        if it.span > 1 {
            tall.push(it);
        } else {
            let l = it.level as usize;
            height[l] = height[l].max(it.geo.height + it.elevation.unwrap_or(0));
        }
    }
    for it in &tall {
        let l = it.level as usize;
        if height[l] == 0 {
            height[l] = 1;
        }
    }
    let mut base = vec![0i64; (top + 1) as usize];
    for lv in 1..=top as usize {
        base[lv] = base[lv - 1] + height[lv - 1] + gap_y[lv];
        for it in &tall {
            if (it.level + it.span) as usize == lv {
                let head = base[it.level as usize] + it.elevation.unwrap_or(0) + it.geo.height;
                base[lv] = base[lv].max(head + gap_y[lv]);
            }
        }
    }
    Ok((height, base))
}

/// Bottom y of every item (see the Python ``place_y``).
pub fn place_y(items: &[Item], gap_y: &[i64], links: &[Link], anchor_free: bool) -> Result<Vec<i64>> {
    let (height, base) = bands(items, gap_y)?;
    let mut ys: Vec<i64> = items
        .iter()
        .map(|it| base[it.level as usize] + it.elevation.unwrap_or(0))
        .collect();
    let slack: Vec<i64> = items
        .iter()
        .map(|it| (height[it.level as usize] - it.geo.height).max(0))
        .collect();
    let mut settled: HashSet<usize> = (0..items.len())
        .filter(|&j| items[j].elevation.is_some() || slack[j] == 0)
        .collect();

    let spread = |from_flags: bool, ys: &mut Vec<i64>, settled: &mut HashSet<usize>| {
        let mut changed = true;
        while changed {
            changed = false;
            for lk in links {
                if (lk.a_side, lk.b_side) != ('E', 'W') {
                    continue;
                }
                for (j, o, mine, theirs) in [(lk.a, lk.b, lk.a_off, lk.b_off), (lk.b, lk.a, lk.b_off, lk.a_off)] {
                    if settled.contains(&j) || !settled.contains(&o) {
                        continue;
                    }
                    if items[o].edge.is_some() && !from_flags {
                        continue;
                    }
                    let (lj, lo) = (items[j].level, items[o].level);
                    if lj != lo && items[o].span < 2 {
                        continue;
                    }
                    let dy = ys[o] + theirs - (base[lj as usize] + mine);
                    let mut limit = slack[j];
                    if lj == lo || lj == lo + items[o].span - 1 {
                        let head = ys[o] + items[o].geo.height;
                        limit = limit.max(head - items[j].geo.height - base[lj as usize]);
                    }
                    if 0 <= dy && dy <= limit {
                        ys[j] = base[items[j].level as usize] + dy;
                        settled.insert(j);
                        changed = true;
                    }
                }
            }
        }
    };

    spread(false, &mut ys, &mut settled);
    if anchor_free {
        loop {
            let mut linked: Vec<usize> = Vec::new();
            for lk in links {
                if (lk.a_side, lk.b_side) == ('E', 'W') && items[lk.a].level == items[lk.b].level {
                    for j in [lk.a, lk.b] {
                        if !settled.contains(&j) && items[j].edge.is_none() && slack[j] != 0 && !linked.contains(&j) {
                            linked.push(j);
                        }
                    }
                }
            }
            if linked.is_empty() {
                break;
            }
            let pick = *linked
                .iter()
                .max_by_key(|&&j| (items[j].geo.height, -(j as i64)))
                .unwrap();
            settled.insert(pick);
            spread(false, &mut ys, &mut settled);
        }
    }
    let mut partner: HashMap<usize, usize> = HashMap::new();
    let mut up: HashSet<usize> = HashSet::new();
    for lk in links {
        for (j, o, side) in [(lk.a, lk.b, lk.b_side), (lk.b, lk.a, lk.a_side)] {
            if let std::collections::hash_map::Entry::Vacant(e) = partner.entry(j) {
                e.insert(o);
                if side == 'S' {
                    up.insert(j);
                }
            }
        }
    }
    for edge in ["left", "right"] {
        let sign: i64 = if edge == "left" { 1 } else { -1 };
        let nest = |j: usize, ys: &Vec<i64>| {
            let near = sign * partner.get(&j).map_or(0, |&p| p as i64);
            let is_up = up.contains(&j);
            (
                ys[j],
                !partner.contains_key(&j),
                is_up,
                if is_up { -near } else { near },
                j,
            )
        };
        let mut stack: Vec<usize> = (0..items.len())
            .filter(|&j| items[j].edge.as_deref() == Some(edge))
            .collect();
        stack.sort_by_key(|&j| nest(j, &ys));
        for w in 0..stack.len().saturating_sub(1) {
            let (below, j) = (stack[w], stack[w + 1]);
            ys[j] = ys[j].max(ys[below] + items[below].geo.height + 1);
        }
        settled.extend(stack);
    }
    spread(true, &mut ys, &mut settled);
    if anchor_free {
        lift_beside(items, links, &mut ys, &base, &slack);
    }
    Ok(ys)
}

fn port_y(it: &Item, y: i64, side: char, off: i64) -> i64 {
    match side {
        'N' => y + it.geo.height,
        'S' => y,
        _ => y + off,
    }
}

fn lift_beside(items: &[Item], links: &[Link], ys: &mut [i64], base: &[i64], slack: &[i64]) {
    let mut ends: HashMap<usize, Vec<(usize, char, i64, char, i64)>> = HashMap::new();
    for lk in links {
        ends.entry(lk.a)
            .or_default()
            .push((lk.b, lk.b_side, lk.b_off, lk.a_side, lk.a_off));
        ends.entry(lk.b)
            .or_default()
            .push((lk.a, lk.a_side, lk.a_off, lk.b_side, lk.b_off));
    }
    for (j, it) in items.iter().enumerate() {
        let mine_ends = ends.get(&j);
        if !it.geo.beside || it.elevation.is_some() || mine_ends.map_or(0, Vec::len) != 1 {
            continue;
        }
        let (o, side, off, mine, my_off) = mine_ends.unwrap()[0];
        let t = port_y(&items[o], ys[o], side, off);
        let own = port_y(it, 0, mine, my_off);
        let want = if side == 'S' {
            let w = t - FLANGED_RUN - own.max(0);
            if mine == 'N' {
                w.min(t - FLANGED_RUN - it.geo.height)
            } else {
                w
            }
        } else if side == 'N' {
            t + FLANGED_RUN - if mine == 'S' { 0 } else { own }
        } else {
            t - own
        };
        let lo = base[it.level as usize];
        ys[j] = want.max(lo).min(lo + slack[j]);
    }
}

/// Smallest allowed gap in front of each item.
pub fn min_gap_x(items: &[Item], ys: &[i64]) -> Vec<i64> {
    let mut gaps = vec![1i64; items.len()];
    let mut prev: Option<usize> = None;
    for (j, it) in items.iter().enumerate() {
        if it.edge.is_some() {
            continue;
        }
        if let Some(p) = prev {
            gaps[j] = if apart(ys, items, p, j) >= 1 { 0 } else { 1 };
        }
        prev = Some(j);
    }
    gaps
}

/// Main-flow arrow ``u -> v``: v's port may not lie left of u's port.
#[derive(Clone, Copy, Debug)]
pub struct FlowLink {
    pub u: usize,
    pub u_dx: i64,
    pub v: usize,
    pub v_dx: i64,
}

pub fn port_dx(geo: &Geometry, side: char, offset: i64) -> i64 {
    match side {
        'W' => 0,
        'E' => geo.width,
        _ => offset,
    }
}

fn apart(ys: &[i64], items: &[Item], i: usize, j: usize) -> i64 {
    if ys[i] <= ys[j] {
        ys[j] - (ys[i] + items[i].geo.height)
    } else {
        ys[i] - (ys[j] + items[j].geo.height)
    }
}

type Riser = (i64, i64, i64, usize);

fn risers(items: &[Item], ys: &[i64], links: &[Link]) -> HashMap<usize, Vec<Riser>> {
    let mut out: HashMap<usize, Vec<Riser>> = HashMap::new();
    for lk in links {
        for (i, side, off, o) in [(lk.a, lk.a_side, lk.a_off, lk.b), (lk.b, lk.b_side, lk.b_off, lk.a)] {
            if !ns(side) {
                continue;
            }
            let top = ys[i] + items[i].geo.height;
            let (o_lo, o_hi) = (ys[o], ys[o] + items[o].geo.height);
            let span = if side == 'N' {
                (top, (top + 2).max(if o_lo >= top { o_hi } else { 0 }))
            } else {
                ((ys[i] - 2).min(if o_hi <= ys[i] { o_lo } else { ys[i] }), ys[i])
            };
            out.entry(i).or_default().push((off, span.0, span.1, o));
        }
    }
    out
}

fn clear_risers(
    mut x: i64,
    j: usize,
    items: &[Item],
    xs: &IndexMap<usize, i64>,
    ys: &[i64],
    done: &[usize],
    risers: &HashMap<usize, Vec<Riser>>,
) -> i64 {
    let (w, y0, y1) = (items[j].geo.width, ys[j], ys[j] + items[j].geo.height);
    let none: Vec<Riser> = Vec::new();
    let mut moved = true;
    while moved {
        moved = false;
        for &i in done {
            for &(dx, lo, hi, partner) in risers.get(&i).unwrap_or(&none) {
                let c = xs[&i] + dx;
                if partner != j && lo <= y1 && y0 <= hi && x <= c && c <= x + w {
                    x = c + 1;
                    moved = true;
                }
            }
            let (i0, i1) = (ys[i], ys[i] + items[i].geo.height);
            for &(dx, lo, hi, partner) in risers.get(&j).unwrap_or(&none) {
                let right = xs[&i] + items[i].geo.width;
                if partner != i && lo <= i1 && i0 <= hi && xs[&i] <= x + dx && x + dx <= right {
                    x = right + 1 - dx;
                    moved = true;
                }
            }
        }
    }
    x
}

#[allow(clippy::too_many_arguments)]
fn pull_sources(
    items: &[Item],
    xs: &mut IndexMap<usize, i64>,
    ys: &[i64],
    links: &[Link],
    flow: &[FlowLink],
    overlap: &[bool],
    gap_x: &[i64],
    risers: &HashMap<usize, Vec<Riser>>,
    pull: &HashSet<usize>,
) {
    let n = items.len();
    let mut fed: HashMap<usize, Vec<Link>> = HashMap::new();
    let mut sources = Vec::new();
    for j in 0..n {
        if items[j].edge.is_some() || !overlap[j] || !xs.contains_key(&j) {
            continue;
        }
        if items[j].id.starts_with("_T_") {
            continue;
        }
        let mine: Vec<&Link> = links.iter().filter(|lk| lk.a == j || lk.b == j).collect();
        let ahead: Vec<Link> = mine.iter().filter(|lk| lk.a == j).map(|lk| **lk).collect();
        if ahead.iter().any(|lk| !xs.contains_key(&lk.b)) {
            continue;
        }
        if !ahead.is_empty() && (ahead.len() == mine.len() || pull.contains(&j)) {
            fed.insert(j, ahead);
            sources.push(j);
        }
    }
    sources.sort_by(|a, b| b.cmp(a));
    for j in sources {
        let w = items[j].geo.width;
        let g = gap_x[j].max(1);
        let others: Vec<usize> = xs
            .keys()
            .copied()
            .filter(|&i| i != j && items[i].edge.is_none())
            .collect();
        let near: Vec<usize> = others.iter().copied().filter(|&i| apart(ys, items, i, j) < 1).collect();
        let fits = |x: i64, xs: &IndexMap<usize, i64>| -> bool {
            for &i in &near {
                if !(x + w + g <= xs[&i] || xs[&i] + items[i].geo.width + g <= x) {
                    return false;
                }
            }
            for lk in &fed[&j] {
                if let Some(low) = link_min_x(lk, x, &items[j].geo, ys[j], ys[lk.b]) {
                    if xs[&lk.b] < low {
                        return false;
                    }
                }
            }
            for f in flow {
                if f.u == j && xs[&f.v] + f.v_dx < x + f.u_dx {
                    return false;
                }
            }
            clear_risers(x, j, items, xs, ys, &others, risers) == x
        };
        let mut x = fed[&j].iter().map(|lk| xs[&lk.b]).min().unwrap() - w - g;
        while x > xs[&j] && !fits(x, xs) {
            x -= 1;
        }
        if x > xs[&j] {
            xs.insert(j, x);
        }
    }
}

/// Place ``items`` (given in left-to-right order); see the Python ``place``.
#[allow(clippy::too_many_arguments)]
pub fn place(
    items: &[Item],
    gap_x: &[i64],
    gap_y: &[i64],
    links: &[Link],
    overlap: Option<&[bool]>,
    flow: &[FlowLink],
    anchor_free: bool,
    in_order: Option<&[bool]>,
) -> Result<Vec<Placed>> {
    if items.is_empty() {
        return Ok(Vec::new());
    }
    let ys = place_y(items, gap_y, links, anchor_free)?;
    let mut xs: IndexMap<usize, i64> = IndexMap::new();

    let link_floor = |j: usize, xs: &IndexMap<usize, i64>| -> i64 {
        let mut best: Option<i64> = None;
        for lk in links {
            if lk.b == j && xs.contains_key(&lk.a) {
                if let Some(v) = link_min_x(lk, xs[&lk.a], &items[lk.a].geo, ys[lk.a], ys[j]) {
                    best = Some(best.map_or(v, |b| b.max(v)));
                }
            }
        }
        for f in flow {
            if f.v == j && xs.contains_key(&f.u) {
                let v = xs[&f.u] + f.u_dx - f.v_dx;
                best = Some(best.map_or(v, |b| b.max(v)));
            }
        }
        best.unwrap_or(0)
    };

    let left: Vec<usize> = (0..items.len())
        .filter(|&j| items[j].edge.as_deref() == Some("left"))
        .collect();
    let right: Vec<usize> = (0..items.len())
        .filter(|&j| items[j].edge.as_deref() == Some("right"))
        .collect();
    let main: Vec<usize> = (0..items.len()).filter(|&j| items[j].edge.is_none()).collect();

    let edge_w = left.iter().map(|&j| items[j].geo.width).max().unwrap_or(0);
    for &j in &left {
        xs.insert(j, edge_w - items[j].geo.width);
    }
    let mut cursor: Option<i64> = if left.is_empty() { None } else { Some(edge_w) };
    let mut done: Vec<usize> = Vec::new();
    let any_overlap = overlap.is_some_and(|o| o.iter().any(|&b| b));
    let rs = if any_overlap {
        risers(items, &ys, links)
    } else {
        HashMap::new()
    };
    for &j in &main {
        let x = if overlap.is_some_and(|o| o[j]) {
            let mut ends: Vec<i64> = done
                .iter()
                .filter(|&&i| apart(&ys, items, i, j) < 1)
                .map(|&i| xs[&i] + items[i].geo.width)
                .collect();
            if !left.is_empty() {
                ends.push(edge_w);
            }
            let mut x = if ends.is_empty() {
                0
            } else {
                ends.iter().max().unwrap() + gap_x[j].max(1)
            };
            if in_order.is_some_and(|o| o[j]) && !done.is_empty() {
                x = x.max(xs[done.last().unwrap()]);
            }
            clear_risers(x.max(link_floor(j, &xs)), j, items, &xs, &ys, &done, &rs)
        } else {
            match cursor {
                None => 0,
                Some(c) => c + gap_x[j],
            }
        };
        let x = x.max(link_floor(j, &xs));
        xs.insert(j, x);
        cursor = Some(cursor.unwrap_or(0).max(x + items[j].geo.width));
        done.push(j);
    }
    if any_overlap {
        let pull: HashSet<usize> = xs
            .keys()
            .copied()
            .filter(|&j| in_order.is_some_and(|o| !o[j]))
            .collect();
        pull_sources(items, &mut xs, &ys, links, flow, overlap.unwrap(), gap_x, &rs, &pull);
    }
    if !right.is_empty() {
        let mut x = cursor.unwrap_or(0) + right.iter().map(|&j| gap_x[j]).max().unwrap();
        for &j in &right {
            x = x.max(link_floor(j, &xs));
        }
        for &j in &right {
            xs.insert(j, x);
        }
    }
    Ok(items
        .iter()
        .enumerate()
        .map(|(j, it)| Placed {
            id: it.id.clone(),
            typ: it.typ.clone(),
            level: it.level,
            geo: it.geo.clone(),
            x: xs[&j],
            y: ys[j],
        })
        .collect())
}
