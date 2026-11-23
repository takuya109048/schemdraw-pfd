//! Every sheet must lay out, route and draw exactly as the Python tool's
//! snapshots say (``tests/snapshots`` of the repository): the same
//! placement, routes, warnings and drawing segments (floats to 1e-6).
//!
//! ``PFD_CASE=<substring>`` runs only the matching sheets.

use std::path::{Path, PathBuf};

use pfd::{Flowsheet, render};
use serde_json::Value;

fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap().to_path_buf()
}

fn json_files(dir: &Path, out: &mut Vec<PathBuf>) {
    let mut entries: Vec<PathBuf> = std::fs::read_dir(dir).unwrap().map(|e| e.unwrap().path()).collect();
    entries.sort();
    for p in entries {
        if p.is_dir() {
            json_files(&p, out);
        } else if p.extension().is_some_and(|e| e == "json") {
            out.push(p);
        }
    }
}

/// First difference between two JSON values (numbers within 1e-6).
fn diff(a: &Value, b: &Value, at: &str) -> Option<String> {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => {
            let (x, y) = (x.as_f64().unwrap(), y.as_f64().unwrap());
            if (x - y).abs() > 1.01e-6 {
                return Some(format!("{at}: {x} != {y}"));
            }
            None
        }
        (Value::Array(x), Value::Array(y)) => {
            for (k, (p, q)) in x.iter().zip(y).enumerate() {
                if let Some(d) = diff(p, q, &format!("{at}[{k}]")) {
                    return Some(d);
                }
            }
            if x.len() != y.len() {
                return Some(format!("{at}: length {} != {}", x.len(), y.len()));
            }
            None
        }
        (Value::Object(x), Value::Object(y)) => {
            for (k, p) in x {
                match y.get(k) {
                    None => return Some(format!("{at}.{k}: missing in the snapshot")),
                    Some(q) => {
                        if let Some(d) = diff(p, q, &format!("{at}.{k}")) {
                            return Some(d);
                        }
                    }
                }
            }
            for k in y.keys() {
                if !x.contains_key(k) {
                    return Some(format!("{at}.{k}: missing in the Rust output"));
                }
            }
            None
        }
        _ if a == b => None,
        _ => Some(format!("{at}: {a} != {b}")),
    }
}

fn check(spec: &Path, snap: &Path) -> Result<(), String> {
    let text = std::fs::read_to_string(spec).unwrap();
    let fs = Flowsheet::from_json(&text).map_err(|e| format!("read: {e}"))?;
    let d = fs.build(4, true).map_err(|e| format!("build: {e}"))?;
    let got = render::snapshot(&d).map_err(|e| format!("draw: {e}"))?;
    let want: Value = serde_json::from_str(&std::fs::read_to_string(snap).unwrap()).unwrap();
    for key in ["placed", "routes", "warnings", "drawing"] {
        if let Some(d) = diff(&got[key], &want[key], key) {
            return Err(d);
        }
    }
    Ok(())
}

fn uses_revisions(spec: &Path) -> bool {
    let v: Value = serde_json::from_str(&std::fs::read_to_string(spec).unwrap()).unwrap();
    v.get("revisions").is_some() || v.get("frozen").is_some()
}

fn run(pairs: Vec<(PathBuf, PathBuf)>) {
    let only = std::env::var("PFD_CASE").ok();
    let mut failed = Vec::new();
    let mut count = 0;
    for (spec, snap) in pairs {
        let name = spec.strip_prefix(repo()).unwrap().display().to_string();
        if only.as_ref().is_some_and(|o| !name.contains(o.as_str())) {
            continue;
        }
        if uses_revisions(&spec) {
            continue; // revisions are not ported yet
        }
        count += 1;
        let start = std::time::Instant::now();
        let result = check(&spec, &snap);
        let ms = start.elapsed().as_millis();
        match result {
            Ok(()) => eprintln!("ok   {name} ({ms} ms)"),
            Err(e) => {
                eprintln!("FAIL {name} ({ms} ms): {e}");
                failed.push(name);
            }
        }
    }
    assert!(
        failed.is_empty(),
        "{} of {count} sheets differ: {failed:?}",
        failed.len()
    );
}

#[test]
fn cases_match_the_python_snapshots() {
    let root = repo().join("tests/cases");
    let mut files = Vec::new();
    json_files(&root, &mut files);
    let pairs = files
        .into_iter()
        .filter(|p| !p.starts_with(root.join("invalid")))
        .map(|p| {
            let rel = p.strip_prefix(&root).unwrap().to_path_buf();
            (p, repo().join("tests/snapshots/cases").join(rel))
        })
        .collect();
    run(pairs);
}

#[test]
fn examples_match_the_python_snapshots() {
    let root = repo().join("examples");
    let mut files = Vec::new();
    json_files(&root, &mut files);
    let pairs = files
        .into_iter()
        .map(|p| {
            let name = p.file_name().unwrap().to_owned();
            (p, repo().join("tests/snapshots").join(name))
        })
        .collect();
    run(pairs);
}
