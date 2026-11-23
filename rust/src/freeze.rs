//! A laid out sheet written down, to be read back as it is (see the Python
//! ``freeze`` module): every item as what it was built from and where it
//! stands, every line as its ends, kind and corner points.

use std::sync::Arc;

use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::elements::Library;
use crate::error::{Error, Result};
use crate::layout::Placed;
use crate::model::Diagram;
use crate::netlist::{Connection, Equipment};
use crate::pyjson;
use crate::pyval::{self, Params};
use crate::revise::{Ghost, JUNCTION, RevisionInfo, junction_geometry};
use crate::router::Route;
use crate::style::LineStyle;

/// What a frozen sheet was built from, as a short hash.
pub fn fingerprint(data: &Value) -> String {
    let text = pyjson::dumps_sorted(data);
    let digest = Sha256::digest(text.as_bytes());
    digest.iter().map(|b| format!("{b:02x}")).collect::<String>()[..16].to_string()
}

fn item(p: &Placed) -> Result<Value> {
    if p.typ == JUNCTION {
        return Ok(json!([p.id, JUNCTION, {}, {}, p.x, p.y, p.level]));
    }
    let Some((kind, params, ports)) = &p.geo.recipe else {
        return Err(Error::Value(format!(
            "{}: cannot be frozen (built without a recipe)",
            p.id
        )));
    };
    Ok(json!([p.id, kind, params, ports, p.x, p.y, p.level]))
}

fn placed(v: &Value, library: &Library) -> Result<Placed> {
    let bad = || Error::Value("frozen: bad item".to_string());
    let a = v.as_array().filter(|a| a.len() == 7).ok_or_else(bad)?;
    let id = pyval::str(&a[0]);
    let kind = pyval::str(&a[1]);
    let (x, y, level) = (pyval::int(&a[4])?, pyval::int(&a[5])?, pyval::int(&a[6])?);
    let geo = if kind == JUNCTION {
        junction_geometry()
    } else {
        let mut e = Equipment::new(
            &id,
            &kind,
            Some(level),
            a[2].as_object().cloned().unwrap_or_else(Params::new),
        );
        e.ports = Arc::new(a[3].as_object().cloned().unwrap_or_default());
        e.geometry(library).map_err(|err| err.context(&id))?
    };
    Ok(Placed {
        id,
        typ: kind,
        level,
        geo: Arc::new(geo),
        x,
        y,
    })
}

fn line(c: &Connection, r: &Route) -> Value {
    let pts: Vec<Value> = r.points.iter().map(|&(x, y)| json!([x, y])).collect();
    json!([c.src, c.dst, c.kind, pts, r.strict])
}

fn route(v: &Value) -> Result<(Connection, Route)> {
    let bad = || Error::Value("frozen: bad line".to_string());
    let a = v.as_array().filter(|a| a.len() == 5).ok_or_else(bad)?;
    let mut points = Vec::new();
    for p in a[3].as_array().ok_or_else(bad)? {
        let xy = p.as_array().filter(|xy| xy.len() == 2).ok_or_else(bad)?;
        points.push((pyval::float(&xy[0])?, pyval::float(&xy[1])?));
    }
    let c = Connection::new(&pyval::str(&a[0]), &pyval::str(&a[1]), &pyval::str(&a[2]));
    Ok((
        c,
        Route {
            points,
            strict: pyval::truthy(&a[4]),
            nodes: Vec::new(),
        },
    ))
}

fn strings(m: &indexmap::IndexMap<String, String>) -> Value {
    Value::Object(m.iter().map(|(k, v)| (k.clone(), Value::from(v.clone()))).collect())
}

/// A built diagram as plain JSON data.
pub fn to_data(d: &Diagram) -> Result<Value> {
    let mut ghosts = Vec::new();
    for g in &d.ghosts {
        if let Some(p) = &g.placed {
            ghosts.push(json!({"rev": g.rev, "item": item(p)?}));
        } else if let Some((c, r)) = &g.line {
            ghosts.push(json!({"rev": g.rev, "line": line(c, r)}));
        }
    }
    Ok(json!({
        "items": d.placed.iter().map(item).collect::<Result<Vec<_>>>()?,
        "lines": d.routes.iter().map(|(c, r)| line(c, r)).collect::<Vec<_>>(),
        "warnings": d.warnings,
        "descs": strings(&d.descs),
        "uturns": d.uturns,
        "rows": d.rows.iter().map(|r| json!([r.0, r.1])).collect::<Vec<_>>(),
        "revisions": d.revisions.iter().map(|r| json!([r.id, r.color, r.note])).collect::<Vec<_>>(),
        "added": strings(&d.added),
        "ghosts": ghosts,
        "hosts": strings(&d.hosts),
        "replaced": d.replaced.iter().map(|r| json!([r.0, r.1, r.2])).collect::<Vec<_>>(),
        "changed": d.changed.iter().map(|c| json!([c.0, c.1])).collect::<Vec<_>>(),
    }))
}

fn str_map(v: Option<&Value>) -> indexmap::IndexMap<String, String> {
    v.and_then(Value::as_object)
        .map(|o| o.iter().map(|(k, v)| (k.clone(), pyval::str(v))).collect())
        .unwrap_or_default()
}

fn list(v: Option<&Value>) -> Vec<Value> {
    v.and_then(Value::as_array).cloned().unwrap_or_default()
}

/// The diagram [`to_data`] wrote, read back.
pub fn from_data(data: &Value, library: &Library, styles: &indexmap::IndexMap<String, LineStyle>) -> Result<Diagram> {
    let mut ghosts = Vec::new();
    for g in list(data.get("ghosts")) {
        let rev = g.get("rev").map(pyval::str).unwrap_or_default();
        if let Some(i) = g.get("item") {
            ghosts.push(Ghost {
                rev,
                placed: Some(placed(i, library)?),
                line: None,
            });
        } else if let Some(l) = g.get("line") {
            ghosts.push(Ghost {
                rev,
                placed: None,
                line: Some(route(l)?),
            });
        }
    }
    let texts = |v: &Value| -> Vec<String> {
        v.as_array()
            .map(|a| a.iter().map(pyval::str).collect())
            .unwrap_or_default()
    };
    Ok(Diagram {
        placed: list(data.get("items"))
            .iter()
            .map(|v| placed(v, library))
            .collect::<Result<_>>()?,
        routes: list(data.get("lines")).iter().map(route).collect::<Result<_>>()?,
        warnings: list(data.get("warnings")).iter().map(pyval::str).collect(),
        descs: str_map(data.get("descs")),
        line_styles: styles.clone(),
        uturns: data.get("uturns").map(pyval::int).transpose()?.unwrap_or(0),
        rows: list(data.get("rows"))
            .iter()
            .map(|r| Ok((pyval::float(&r[0])?, pyval::float(&r[1])?)))
            .collect::<Result<_>>()?,
        revisions: list(data.get("revisions"))
            .iter()
            .map(|r| {
                let t = texts(r);
                RevisionInfo {
                    id: t.first().cloned().unwrap_or_default(),
                    color: t.get(1).cloned().unwrap_or_default(),
                    note: t.get(2).cloned().unwrap_or_default(),
                }
            })
            .collect(),
        added: str_map(data.get("added")),
        ghosts,
        hosts: str_map(data.get("hosts")),
        replaced: list(data.get("replaced"))
            .iter()
            .map(|r| {
                let t = texts(r);
                (t[0].clone(), t[1].clone(), t[2].clone())
            })
            .collect(),
        changed: list(data.get("changed"))
            .iter()
            .map(|r| {
                let t = texts(r);
                (t[0].clone(), t[1].clone())
            })
            .collect(),
    })
}
