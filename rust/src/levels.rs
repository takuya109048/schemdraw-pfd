//! Automatic levels: the height tier of every item whose level is not given
//! (see the Python ``levels`` module).

use std::collections::{HashMap, HashSet};

use indexmap::IndexMap;

use crate::netlist::{Connection, Equipment};

pub const PUMP: &str = "pump";
pub const MAX_ROUNDS: usize = 100;
pub const BATCH: usize = 16;
pub const ESTIMATED_BATCHES: usize = 2;

pub type Levels = IndexMap<String, i64>;

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
enum Part {
    Num(i64),
    Text(String),
}

/// Sort key of ``levels._natural`` (numbers compared as numbers).
fn natural(text: &str) -> Vec<Part> {
    // re.split(r"(\d+)", text): text, digits, text, digits ..., text
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut digits = false;
    let mut first = true;
    for ch in text.chars() {
        let d = ch.is_ascii_digit();
        if first {
            digits = d;
            first = false;
            if d {
                out.push(Part::Text(String::new()));
            }
        }
        if d != digits {
            out.push(if digits {
                Part::Num(cur.parse().unwrap_or(i64::MAX))
            } else {
                Part::Text(cur.clone())
            });
            cur.clear();
            digits = d;
        }
        cur.push(ch);
    }
    if digits {
        out.push(Part::Num(cur.parse().unwrap_or(i64::MAX)));
        out.push(Part::Text(String::new()));
    } else {
        out.push(Part::Text(cur));
    }
    out
}

fn natural_cmp(a: &str, b: &str) -> std::cmp::Ordering {
    natural(a).cmp(&natural(b))
}

/// Where the search starts: given levels kept, pumps on the ground, the
/// rest one level up (on the ground when the sheet has no pumps).
pub fn start_levels(eqs: &[Equipment]) -> Levels {
    let up = eqs.iter().any(|e| e.typ == PUMP) as i64;
    eqs.iter()
        .map(|e| {
            let lv = match e.level {
                Some(l) => l,
                None if e.typ == PUMP => 0,
                None => up,
            };
            (e.id.clone(), lv)
        })
        .collect()
}

/// The sets of items one move of the search shifts a level up or down.
pub fn moves(eqs: &[Equipment], conns: &[Connection], together: &[Vec<String>]) -> Vec<Vec<String>> {
    let free: HashSet<&str> = eqs
        .iter()
        .filter(|e| e.level.is_none() && e.typ != PUMP)
        .map(|e| e.id.as_str())
        .collect();
    let ids: HashSet<&str> = eqs.iter().map(|e| e.id.as_str()).collect();
    let mut group: HashMap<&str, Vec<&str>> = HashMap::new();
    for g in together {
        let members: Vec<&str> = g.iter().map(String::as_str).filter(|i| free.contains(i)).collect();
        for &i in &members {
            group.insert(i, members.clone());
        }
    }
    let mut partners: HashMap<&str, HashSet<&str>> = ids.iter().map(|&i| (i, HashSet::new())).collect();
    for c in conns {
        let [(a, _), (b, _)] = c.ends();
        if ids.contains(a) && ids.contains(b) && a != b {
            partners.get_mut(a).unwrap().insert(b);
            partners.get_mut(b).unwrap().insert(a);
        }
    }
    let close = |items: &[&str]| -> Vec<String> {
        let mut out: Vec<&str> = Vec::new();
        for &i in items {
            let g = group.get(i).cloned().unwrap_or_else(|| vec![i]);
            for j in g {
                if free.contains(j) && !out.contains(&j) {
                    out.push(j);
                }
            }
        }
        out.sort_by(|a, b| natural_cmp(a, b));
        out.into_iter().map(String::from).collect()
    };
    let mut sets: IndexMap<Vec<String>, ()> = IndexMap::new();
    let mut sorted_free: Vec<&str> = free.iter().copied().collect();
    sorted_free.sort_by(|a, b| natural_cmp(a, b));
    for &i in &sorted_free {
        sets.insert(close(&[i]), ());
        let sats: Vec<&str> = free
            .iter()
            .copied()
            .filter(|j| partners[j].len() == 1 && partners[j].contains(i))
            .collect();
        if !sats.is_empty() {
            let mut items = vec![i];
            items.extend(sats);
            sets.insert(close(&items), ());
        }
        let mut others: Vec<&str> = partners[i].iter().copied().filter(|o| free.contains(o)).collect();
        others.sort_by(|a, b| natural_cmp(a, b));
        for o in others {
            sets.insert(close(&[i, o]), ());
        }
    }
    sets.into_keys().filter(|m| !m.is_empty()).collect()
}

fn known(levels: &Levels) -> Vec<(String, i64)> {
    let mut v: Vec<(String, i64)> = levels.iter().map(|(k, v)| (k.clone(), *v)).collect();
    v.sort();
    v
}

/// Descent over the levels of one sheet from ``start`` (see the Python
/// ``search_levels``).
pub fn search_levels(
    start: &Levels,
    sets: &[Vec<String>],
    evaluate: &mut dyn FnMut(&[Levels], Option<i64>) -> Vec<Option<i64>>,
    mut estimate: Option<&mut dyn FnMut(&[Levels]) -> Vec<i64>>,
    rounds: usize,
) -> Levels {
    let mut costs: HashMap<Vec<(String, i64)>, Option<i64>> = HashMap::new();
    let mut guesses: HashMap<Vec<(String, i64)>, i64> = HashMap::new();

    let mut evaluate_new =
        |tasks: &[Levels], bound: Option<i64>, costs: &mut HashMap<Vec<(String, i64)>, Option<i64>>| {
            let keys: Vec<Vec<(String, i64)>> = tasks.iter().map(known).collect();
            let mut new: IndexMap<Vec<(String, i64)>, Levels> = IndexMap::new();
            for (k, lv) in keys.iter().zip(tasks) {
                if !costs.contains_key(k) && !new.contains_key(k) {
                    new.insert(k.clone(), lv.clone());
                }
            }
            if !new.is_empty() {
                let lvs: Vec<Levels> = new.values().cloned().collect();
                let got = evaluate(&lvs, bound);
                for (k, c) in new.keys().zip(got) {
                    costs.insert(k.clone(), c);
                }
            }
            keys.iter().map(|k| costs[k]).collect::<Vec<Option<i64>>>()
        };

    let mut current = start.clone();
    let mut cost = evaluate_new(std::slice::from_ref(&current), None, &mut costs)[0];
    for _ in 0..rounds {
        let top = current.values().copied().max().unwrap_or(0) + 1;
        let mut tasks: Vec<Levels> = Vec::new();
        for mv in sets {
            for step in [-1i64, 1] {
                let shifted: Vec<(String, i64)> = mv.iter().map(|i| (i.clone(), current[i] + step)).collect();
                if shifted.iter().all(|(_, v)| 0 <= *v && *v <= top) {
                    let mut lv = current.clone();
                    for (i, v) in shifted {
                        lv.insert(i, v);
                    }
                    tasks.push(lv);
                }
            }
        }
        if let Some(est) = estimate.as_mut() {
            if !tasks.is_empty() {
                let keys: Vec<Vec<(String, i64)>> = tasks.iter().map(known).collect();
                let mut new: IndexMap<Vec<(String, i64)>, Levels> = IndexMap::new();
                for (k, lv) in keys.iter().zip(&tasks) {
                    if !guesses.contains_key(k) && !new.contains_key(k) {
                        new.insert(k.clone(), lv.clone());
                    }
                }
                if !new.is_empty() {
                    let lvs: Vec<Levels> = new.values().cloned().collect();
                    let got = est(&lvs);
                    for (k, g) in new.keys().zip(got) {
                        guesses.insert(k.clone(), g);
                    }
                }
                let guess: Vec<i64> = keys.iter().map(|k| guesses[k]).collect();
                let mut ranked: Vec<usize> = (0..tasks.len()).collect();
                ranked.sort_by_key(|&k| (guess[k], k));
                ranked.truncate(ESTIMATED_BATCHES * BATCH);
                tasks = ranked.into_iter().map(|k| tasks[k].clone()).collect();
            }
        }
        let mut found: Option<(i64, Levels)> = None;
        for batch in tasks.chunks(BATCH) {
            let got = evaluate_new(batch, cost, &mut costs);
            for (lv, c) in batch.iter().zip(got) {
                if let Some(c) = c {
                    if cost.is_none_or(|b| c < b) && found.as_ref().is_none_or(|f| c < f.0) {
                        found = Some((c, lv.clone()));
                    }
                }
            }
            if found.is_some() {
                break;
            }
        }
        let Some((c, lv)) = found else { break };
        cost = Some(c);
        current = lv;
    }
    current
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn natural_order() {
        let mut v = vec!["P-10", "P-2", "A", "P-1a", "10"];
        v.sort_by(|a, b| natural_cmp(a, b));
        assert_eq!(v, vec!["10", "A", "P-1a", "P-2", "P-10"]);
    }
}
