//! Revisions and frozen sheets (the Python ``tests/test_revisions.py``
//! properties that the snapshots do not already cover).

use std::path::{Path, PathBuf};
use std::sync::Arc;

use pfd::freeze::{from_data, to_data};
use pfd::{Diagram, Flowsheet};
use serde_json::Value;

fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap().to_path_buf()
}

fn case(name: &str) -> Flowsheet {
    let text = std::fs::read_to_string(repo().join(format!("tests/cases/{name}.json"))).unwrap();
    Flowsheet::from_json(&text).unwrap()
}

fn revision_cases() -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(repo().join("tests/cases/revisions"))
        .unwrap()
        .map(|e| format!("revisions/{}", e.unwrap().path().file_stem().unwrap().to_string_lossy()))
        .collect();
    names.sort();
    names
}

type Sheet = (
    Vec<(String, i64, i64)>,
    Vec<(String, String, Vec<(f64, f64)>)>,
    Vec<String>,
);

fn sheet(d: &Diagram) -> Sheet {
    let mut placed: Vec<_> = d.placed.iter().map(|p| (p.id.clone(), p.x, p.y)).collect();
    placed.sort();
    let mut routes: Vec<_> = d
        .routes
        .iter()
        .map(|(c, r)| (c.src.clone(), c.dst.clone(), r.points.clone()))
        .collect();
    routes.sort_by(|a, b| (&a.0, &a.1).cmp(&(&b.0, &b.1)));
    (placed, routes, d.warnings.clone())
}

#[test]
fn a_frozen_sheet_reads_back_as_it_was() {
    for name in revision_cases() {
        let mut fs = case(&name);
        let built = fs.freeze(4, None).unwrap();
        let back = Flowsheet::from_json(&fs.to_json().unwrap()).unwrap();
        assert_eq!(back.frozen, fs.frozen, "{name}");
        assert_eq!(sheet(&back.build(4, true).unwrap()), sheet(&built), "{name}");
    }
}

#[test]
fn revisions_after_the_frozen_one_are_drawn_onto_it() {
    let mut fs = case("revisions/two_revisions");
    let frozen = fs.freeze(4, Some("R1")).unwrap();
    let mut shifted = fs.frozen.clone().unwrap();
    for item in shifted["sheet"]["items"].as_array_mut().unwrap() {
        item[4] = Value::from(item[4].as_i64().unwrap() + 100);
    }
    for line in shifted["sheet"]["lines"].as_array_mut().unwrap() {
        for p in line[3].as_array_mut().unwrap() {
            p[0] = Value::from(p[0].as_f64().unwrap() + 100.0);
        }
    }
    fs.frozen = Some(shifted);
    let after = fs.build(4, true).unwrap();
    assert!(after.warnings.is_empty());
    let tk = after.placed.iter().find(|p| p.id == "TK-1").unwrap();
    let was = frozen.placed.iter().find(|p| p.id == "TK-1").unwrap();
    assert_eq!((tk.x, tk.y), (was.x + 100, was.y));
    assert_eq!(after.added["PI-1"], "R2");
    let base = fs.build_until(4, true, Some("base")).unwrap();
    let plain = case("revisions/two_revisions")
        .build_until(4, true, Some("base"))
        .unwrap();
    assert_eq!(sheet(&base), sheet(&plain));
}

#[test]
fn a_frozen_sheet_out_of_date_is_laid_out_anew() {
    let mut fs = case("revisions/two_revisions");
    fs.freeze(4, Some("R1")).unwrap();
    let mut params = (*fs.equipment[0].params).clone();
    params.insert("text".into(), Value::from("IN-2"));
    fs.equipment[0].params = Arc::new(params);
    let d = fs.build(4, true).unwrap();
    assert!(d.warnings.iter().any(|w| w.contains("out of date")), "{:?}", d.warnings);
    fs.frozen.as_mut().unwrap()["through"] = Value::from("R9");
    let d = fs.build(4, true).unwrap();
    assert!(
        d.warnings.iter().any(|w| w.contains("no revision 'R9'")),
        "{:?}",
        d.warnings
    );
}

#[test]
fn every_sheet_can_be_frozen() {
    let mut names = Vec::new();
    for group in std::fs::read_dir(repo().join("tests/cases")).unwrap() {
        let dir = group.unwrap().path();
        if !dir.is_dir() || dir.ends_with("invalid") {
            continue;
        }
        for f in std::fs::read_dir(&dir).unwrap() {
            let p = f.unwrap().path();
            if p.extension().is_none_or(|e| e != "json") {
                continue;
            }
            let rel = p.strip_prefix(repo().join("tests/cases")).unwrap().with_extension("");
            names.push(rel.to_string_lossy().to_string());
        }
    }
    names.sort();
    for name in names {
        let fs = case(&name);
        let d = fs.build(4, true).unwrap();
        let data = to_data(&d).unwrap();
        let back = from_data(&data, &fs.library, &fs.line_styles).unwrap();
        let shapes = |x: &Diagram| -> Vec<_> {
            x.placed
                .iter()
                .map(|p| {
                    (
                        p.id.clone(),
                        p.x,
                        p.y,
                        p.geo.width,
                        p.geo.height,
                        p.geo.shapes.clone(),
                        p.geo.ports.clone(),
                    )
                })
                .collect()
        };
        assert_eq!(shapes(&back), shapes(&d), "{name}");
        let pts = |x: &Diagram| -> Vec<_> { x.routes.iter().map(|(_, r)| r.points.clone()).collect() };
        assert_eq!(pts(&back), pts(&d), "{name}");
    }
}

#[test]
fn building_until_a_revision() {
    let fs = case("revisions/two_revisions");
    let err = fs.build_until(4, true, Some("R9")).unwrap_err();
    assert!(err.message().contains("unknown revision 'R9'"), "{err}");
    let r1 = fs.build_until(4, true, Some("R1")).unwrap();
    assert_eq!(r1.revisions.len(), 1);
    assert!(!r1.placed.iter().any(|p| p.id == "PI-1"));
}

#[test]
fn remove_and_disconnect_need_a_revision() {
    let mut fs = case("revisions/two_revisions");
    assert!(
        fs.remove(&["P-1"])
            .unwrap_err()
            .message()
            .contains("start a revision first")
    );
    assert!(
        fs.disconnect("P-1.out", "OUT.tail")
            .unwrap_err()
            .message()
            .contains("start a revision first")
    );
}
