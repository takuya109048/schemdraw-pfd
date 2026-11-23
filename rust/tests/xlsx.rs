//! Excel output: the same workbook as the Python tool writes.

use std::io::Read;
use std::path::{Path, PathBuf};

use pfd::render::{Item, Seg, scene, scene_with};
use pfd::{Flowsheet, xlsx};

fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap().to_path_buf()
}

fn sheet(rel: &str) -> pfd::render::Scene {
    let fs = Flowsheet::from_json(&std::fs::read_to_string(repo().join(rel)).unwrap()).unwrap();
    scene(&fs.build(4, true).unwrap(), true, None, true, None).unwrap()
}

#[test]
fn the_example_workbook_is_written_part_for_part() {
    // examples/loading_station.xlsx was written by the Python tool
    let sc = sheet("examples/loading_station.json");
    let file = std::fs::File::open(repo().join("examples/loading_station.xlsx")).unwrap();
    let mut z = zip::ZipArchive::new(file).unwrap();
    let parts = xlsx::package(&sc, "PFD");
    let names: Vec<&str> = parts.iter().map(|(n, _)| n.as_str()).collect();
    assert_eq!(names.len(), z.len());
    for (name, text) in &parts {
        let mut want = String::new();
        z.by_name(name).unwrap().read_to_string(&mut want).unwrap();
        assert!(*text == want, "{name} differs");
    }
}

#[test]
fn every_straight_piece_is_its_own_line() {
    let sc = sheet("examples/solvent_recovery.json");
    let xml = xlsx::drawing_xml(&sc);
    let mut want = 0;
    let mut arrows = 0;
    for g in &sc.groups {
        for i in &g.items {
            let (pts, closed): (Vec<(f64, f64)>, bool) = match i {
                Item::Shape(pfd::geometry::Shape::Poly(p)) if p.fill.is_none() => (p.points.clone(), p.closed),
                Item::Seg(Seg::Line { path, arrow, .. }) if !(g.kind == "sheet" && path.len() == 5) => {
                    arrows += arrow.as_deref().is_some_and(|a| a.ends_with('>')) as usize;
                    (path.clone(), false)
                }
                _ => continue,
            };
            let mut pts = pts;
            if closed {
                pts.push(pts[0]);
            }
            want += pts.windows(2).filter(|w| w[0] != w[1]).count();
        }
    }
    assert_eq!(xml.matches("<xdr:cxnSp ").count(), want);
    assert_eq!(xml.matches("<a:tailEnd ").count(), arrows);
    assert!(arrows > 0);
}

#[test]
fn revisions_are_drawn_in_their_colours() {
    let fs = Flowsheet::from_json(
        &std::fs::read_to_string(repo().join("tests/cases/revisions/two_revisions.json")).unwrap(),
    )
    .unwrap();
    let d = fs.build(4, true).unwrap();
    let all = xlsx::drawing_xml(&scene_with(&d, true, None, true, None, "all", None).unwrap());
    assert!(all.contains(r#"<a:srgbClr val="FF0000"/>"#)); // R1: red
    assert!(all.contains(r#"<a:srgbClr val="008080"/>"#)); // R2: #008080
    assert!(all.contains(r#"<a:prstDash val="dash"/>"#)); // what R2 took away
    let none = xlsx::drawing_xml(&scene_with(&d, true, None, true, None, "none", None).unwrap());
    assert!(!none.contains("FF0000") && !none.contains("008080"));
}
