//! PNG and PDF output: the sheet at 72 dpi (one point per pixel).

use std::path::Path;

use pfd::render::scene;
use pfd::{Flowsheet, raster};

fn sheet() -> pfd::render::Scene {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let text = std::fs::read_to_string(repo.join("examples/feed_heating.json")).unwrap();
    let d = Flowsheet::from_json(&text).unwrap().build(4, true).unwrap();
    scene(&d, true, None, true, None).unwrap()
}

/// The sheet's size in points: the frame plus 0.1 unit around it.
fn size(sc: &pfd::render::Scene) -> (f64, f64) {
    let (x0, y0, x1, y1) = sc.bounds;
    ((x1 - x0 + 0.2) * 36.0, (y1 - y0 + 0.2) * 36.0)
}

#[test]
fn png_is_the_sheet_at_72_dpi() {
    let sc = sheet();
    let png = raster::to_png(&sc).unwrap();
    assert_eq!(&png[..8], b"\x89PNG\r\n\x1a\n");
    let w = u32::from_be_bytes(png[16..20].try_into().unwrap()) as f64;
    let h = u32::from_be_bytes(png[20..24].try_into().unwrap()) as f64;
    let (pw, ph) = size(&sc);
    assert!((w - pw).abs() <= 1.0 && (h - ph).abs() <= 1.0, "{w}x{h} vs {pw}x{ph}");
}

#[test]
fn pdf_is_one_page_of_the_sheet_in_points() {
    let sc = sheet();
    let pdf = raster::to_pdf(&sc).unwrap();
    assert!(pdf.starts_with(b"%PDF-"));
    let text = String::from_utf8_lossy(&pdf);
    assert_eq!(
        text.matches("/Type /Page\n").count() + text.matches("/Type /Page ").count(),
        1
    );
    let (pw, ph) = size(&sc);
    let at = text.find("/MediaBox [").unwrap();
    let nums: Vec<f64> = text[at + 11..]
        .split(']')
        .next()
        .unwrap()
        .split_whitespace()
        .map(|v| v.parse().unwrap())
        .collect();
    assert!(
        (nums[2] - pw).abs() < 0.5 && (nums[3] - ph).abs() < 0.5,
        "{nums:?} vs {pw}x{ph}"
    );
}
