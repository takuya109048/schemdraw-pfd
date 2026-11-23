//! Repeated units (同じ構成の繰り返し): find copies so that layout and
//! routing can keep them alike (see the Python ``symmetry`` module).

use std::collections::{BTreeMap, HashMap};

use indexmap::IndexMap;

use crate::expand::{Expanded, TAP_PREFIX};
use crate::pyval;

#[derive(Clone, Debug, Default)]
pub struct Symmetry {
    pub item_class: Vec<usize>,
    pub line_class: Vec<usize>,
    pub shared: Vec<bool>,
    pub units: Vec<Vec<Vec<usize>>>,
}

impl Symmetry {
    pub fn repeated(&self) -> bool {
        !self.units.is_empty()
    }
}

fn is_shared(net: &Expanded, i: usize) -> bool {
    let e = &net.equipment[i];
    e.id.starts_with(TAP_PREFIX) || net.geos[&e.id].off_sheet
}

type LineKey = (i64, String, i64, String, String, bool);

pub fn find_units(net: &Expanded) -> Symmetry {
    let n = net.equipment.len();
    let index: HashMap<&str, usize> = net
        .equipment
        .iter()
        .enumerate()
        .map(|(i, e)| (e.id.as_str(), i))
        .collect();
    let ends: Vec<(usize, usize)> = net
        .connections
        .iter()
        .map(|c| {
            let [(a, _), (b, _)] = c.ends();
            (index[a], index[b])
        })
        .collect();
    let shared: Vec<bool> = (0..n).map(|i| is_shared(net, i)).collect();

    let mut parent: Vec<usize> = (0..n).collect();
    fn root(parent: &mut [usize], mut i: usize) -> usize {
        while parent[i] != i {
            parent[i] = parent[parent[i]];
            i = parent[i];
        }
        i
    }
    for &(a, b) in &ends {
        if !(shared[a] || shared[b]) {
            let ra = root(&mut parent, a);
            let rb = root(&mut parent, b);
            parent[ra] = rb;
        }
    }
    let mut comps: IndexMap<usize, Vec<usize>> = IndexMap::new();
    for i in 0..n {
        if !shared[i] {
            let r = root(&mut parent, i);
            comps.entry(r).or_default().push(i);
        }
    }
    let mut lines_of: HashMap<usize, Vec<usize>> = HashMap::new();
    for (ci, &(a, b)) in ends.iter().enumerate() {
        for i in [a, b] {
            if !shared[i] {
                let r = root(&mut parent, i);
                lines_of.entry(r).or_default().push(ci);
                break;
            }
        }
    }

    let line_key = |ci: usize, local: &HashMap<usize, i64>| -> LineKey {
        let c = &net.connections[ci];
        let [(_, pa), (_, pb)] = c.ends();
        let (a, b) = ends[ci];
        (
            *local.get(&a).unwrap_or(&-1),
            pa.to_string(),
            *local.get(&b).unwrap_or(&-1),
            pb.to_string(),
            c.kind.clone(),
            c.recycle,
        )
    };

    type Items = Vec<(String, String, Option<i64>, Option<i64>, i64)>;
    let mut groups: IndexMap<(Items, Vec<LineKey>), Vec<(Vec<usize>, BTreeMap<LineKey, usize>)>> = IndexMap::new();
    let none: Vec<usize> = Vec::new();
    for (r, members) in &comps {
        let lines = lines_of.get(r).unwrap_or(&none);
        if members.len() < 2 && lines.is_empty() {
            continue;
        }
        let local: HashMap<usize, i64> = members.iter().enumerate().map(|(k, &i)| (i, k as i64)).collect();
        let items: Items = members
            .iter()
            .map(|&i| {
                let e = &net.equipment[i];
                (
                    e.typ.clone(),
                    pyval::params_key(&e.params),
                    e.level,
                    e.elevation,
                    e.span,
                )
            })
            .collect();
        let mut keyed: BTreeMap<LineKey, usize> = BTreeMap::new();
        for &ci in lines {
            keyed.insert(line_key(ci, &local), ci);
        }
        if keyed.len() != lines.len() {
            continue;
        }
        let keys: Vec<LineKey> = keyed.keys().cloned().collect();
        groups.entry((items, keys)).or_default().push((members.clone(), keyed));
    }

    let mut item_class: Vec<usize> = (0..n).collect();
    let mut line_class: Vec<usize> = (0..net.connections.len()).collect();
    let mut units = Vec::new();
    for copies in groups.values() {
        if copies.len() < 2 {
            continue;
        }
        let (first_items, first_lines) = &copies[0];
        for (members, keyed) in &copies[1..] {
            for (&i, &j) in first_items.iter().zip(members) {
                item_class[j] = item_class[i];
            }
            for (key, &ci) in keyed {
                line_class[ci] = line_class[first_lines[key]];
            }
        }
        units.push(copies.iter().map(|(m, _)| m.clone()).collect());
    }
    Symmetry {
        item_class,
        line_class,
        shared,
        units,
    }
}

/// Item indices whose gaps (cells in front of the item) move together.
pub fn gap_groups(sym: &Symmetry) -> Vec<Vec<usize>> {
    let mut groups: IndexMap<(usize, i64), Vec<usize>> = IndexMap::new();
    let mut before: i64 = -1;
    for (i, &c) in sym.item_class.iter().enumerate() {
        groups.entry((c, before)).or_default().push(i);
        if !sym.shared[i] {
            before = c as i64;
        }
    }
    let mut out: Vec<Vec<usize>> = groups.into_values().collect();
    out.sort();
    out
}
