//! PNG and PDF output: the sheet's SVG (see [`crate::svg`]) rendered with
//! resvg / converted with svg2pdf, at 72 dpi like the Python tool (one
//! point per pixel), texts in the first installed font of the theme's.

use std::sync::Arc;

use resvg::{tiny_skia, usvg};

use crate::error::{Error, Result};
use crate::render::Scene;

/// Fonts tried for ``sans-serif`` after the theme's own (Japanese) ones.
const FALLBACK: [&str; 6] = [
    "IPAexGothic",
    "IPAGothic",
    "Noto Sans CJK JP",
    "Hiragino Sans",
    "DejaVu Sans",
    "Arial",
];

fn tree(sc: &Scene) -> Result<usvg::Tree> {
    let mut db = usvg::fontdb::Database::new();
    db.load_system_fonts();
    let installed = |name: &str| db.faces().any(|f| f.families.iter().any(|(n, _)| n == name));
    let font = sc
        .theme
        .fonts
        .iter()
        .map(String::as_str)
        .chain(FALLBACK)
        .find(|f| installed(f))
        .map(String::from);
    if let Some(f) = &font {
        db.set_sans_serif_family(f.clone());
    }
    let opts = usvg::Options {
        dpi: 72.0,
        fontdb: Arc::new(db),
        ..Default::default()
    };
    usvg::Tree::from_str(&crate::svg::to_svg(sc), &opts).map_err(|e| Error::Value(format!("svg: {e}")))
}

/// The sheet as a PNG image (white background).
pub fn to_png(sc: &Scene) -> Result<Vec<u8>> {
    let t = tree(sc)?;
    let size = t.size().to_int_size();
    let mut pix = tiny_skia::Pixmap::new(size.width(), size.height())
        .ok_or_else(|| Error::Value("the sheet is too large for an image".into()))?;
    pix.fill(tiny_skia::Color::WHITE);
    resvg::render(&t, tiny_skia::Transform::identity(), &mut pix.as_mut());
    pix.encode_png().map_err(|e| Error::Io(format!("png: {e}")))
}

/// The sheet as a one-page PDF (vector: lines and texts stay sharp).
pub fn to_pdf(sc: &Scene) -> Result<Vec<u8>> {
    let t = tree(sc)?;
    svg2pdf::to_pdf(
        &t,
        svg2pdf::ConversionOptions::default(),
        svg2pdf::PageOptions { dpi: 72.0 },
    )
    .map_err(|e| Error::Io(format!("pdf: {e}")))
}
