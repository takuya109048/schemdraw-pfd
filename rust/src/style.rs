//! Visual settings: line kinds and the sheet theme.

use indexmap::IndexMap;
use serde_json::Value;

use crate::error::{Error, Result};
use crate::pyval;

/// How one kind of line (``pipe``, ``signal`` ...) is drawn.
#[derive(Clone, Debug)]
pub struct LineStyle {
    pub lw: f64,
    pub ls: String,
    pub arrow: Option<String>,
    pub process: bool,
    pub main: bool,
    /// the values as written (written back as they were)
    pub raw: serde_json::Map<String, Value>,
}

impl PartialEq for LineStyle {
    fn eq(&self, o: &LineStyle) -> bool {
        (self.lw, &self.ls, &self.arrow, self.process, self.main) == (o.lw, &o.ls, &o.arrow, o.process, o.main)
    }
}

pub const STYLE_KEYS: [&str; 5] = ["lw", "ls", "arrow", "process", "main"];

impl LineStyle {
    pub fn new(lw: f64, ls: &str, arrow: Option<&str>, process: bool, main: bool) -> LineStyle {
        LineStyle {
            lw,
            ls: ls.into(),
            arrow: arrow.map(String::from),
            process,
            main,
            raw: Default::default(),
        }
    }

    pub fn from_dict(d: &serde_json::Map<String, Value>) -> Result<LineStyle> {
        let mut unknown: Vec<&String> = d.keys().filter(|k| !STYLE_KEYS.contains(&k.as_str())).collect();
        if !unknown.is_empty() {
            unknown.sort();
            return Err(Error::Value(format!(
                "unknown line style keys {}",
                pyval::list_repr(&unknown)
            )));
        }
        Ok(LineStyle {
            lw: match d.get("lw") {
                Some(v) => pyval::float(v)?,
                None => 1.1,
            },
            ls: d.get("ls").map(pyval::str).unwrap_or_else(|| "-".into()),
            arrow: d.get("arrow").filter(|v| !v.is_null()).map(pyval::str),
            process: d.get("process").is_some_and(pyval::truthy),
            main: d.get("main").map(pyval::truthy).unwrap_or(true),
            raw: d.clone(),
        })
    }

    pub fn to_dict(&self) -> Value {
        let mut d = serde_json::Map::new();
        let mut put = |k: &str, v: Value| {
            d.insert(k.into(), self.raw.get(k).cloned().unwrap_or(v));
        };
        put("lw", Value::from(self.lw));
        put("ls", Value::from(self.ls.clone()));
        put("arrow", self.arrow.clone().map(Value::from).unwrap_or(Value::Null));
        put("process", Value::from(self.process));
        put("main", Value::from(self.main));
        Value::Object(d)
    }
}

pub fn default_line_styles() -> IndexMap<String, LineStyle> {
    let mut m = IndexMap::new();
    m.insert("pipe".into(), LineStyle::new(1.1, "-", Some("->"), true, true));
    m.insert("utility".into(), LineStyle::new(0.9, "-", Some("->"), true, false));
    m.insert("signal".into(), LineStyle::new(0.8, "--", None, false, true));
    m.insert("instrument".into(), LineStyle::new(0.8, "-", None, false, true));
    m
}

pub const JP_FONTS: [&str; 5] = ["Yu Gothic", "Meiryo", "MS Gothic", "Noto Sans CJK JP", "IPAexGothic"];

/// Sheet-wide look (everything except the per-kind line styles).
#[derive(Clone, Debug)]
pub struct Theme {
    pub fontsize: f64,
    pub fonts: Vec<String>,
    pub equipment_lw: f64,
    pub crossing_gap: f64,
    pub background: String,
    pub margin: f64,
    pub frame_lw: f64,
    pub tag_underline: bool,
    pub nozzle_ticks: bool,
    pub spec_line_height: f64,
}

impl Default for Theme {
    fn default() -> Theme {
        Theme {
            fontsize: 20.0,
            fonts: JP_FONTS.iter().map(|s| s.to_string()).collect(),
            equipment_lw: 1.3,
            crossing_gap: 0.3,
            background: "white".into(),
            margin: 2.0,
            frame_lw: 2.0,
            tag_underline: true,
            nozzle_ticks: true,
            spec_line_height: 1.45,
        }
    }
}
