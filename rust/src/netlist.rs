//! What the user describes: equipment, the lines between them, inline items.

use std::sync::Arc;

use serde_json::Value;

use crate::elements::{Geometry, Library, parse_port};
use crate::error::{Error, Result};
use crate::pyval::{self, Params};

pub const ANCHORS: [&str; 3] = ["auto", "src", "dst"];

/// ``"ID.port"`` -> ``("ID", "port")`` (the id may not contain a dot).
pub fn split_ref(r: &str) -> Result<(String, String)> {
    match r.split_once('.') {
        Some((i, p)) => Ok((i.to_string(), p.to_string())),
        None => Err(Error::Value(format!(
            "connection end {} must look like 'ID.port'",
            pyval::str_repr(r)
        ))),
    }
}

pub fn check_id(id: &str) -> Result<()> {
    if id.is_empty() || id.contains('.') {
        return Err(Error::Value(format!(
            "id {} must be non-empty and contain no '.'",
            pyval::str_repr(id)
        )));
    }
    Ok(())
}

#[derive(Clone, Debug)]
pub struct Equipment {
    pub id: String,
    pub typ: String,
    pub level: Option<i64>,
    pub params: Arc<Params>,
    /// extra user ports
    pub ports: Arc<serde_json::Map<String, Value>>,
    pub desc: String,
    pub elevation: Option<i64>,
    pub span: i64,
    /// the ``point`` worked out from the item's one line
    pub aim: Option<String>,
}

impl Equipment {
    pub fn new(id: &str, typ: &str, level: Option<i64>, params: Params) -> Equipment {
        Equipment {
            id: id.into(),
            typ: typ.into(),
            level,
            params: Arc::new(params),
            ports: Arc::new(serde_json::Map::new()),
            desc: String::new(),
            elevation: None,
            span: 1,
            aim: None,
        }
    }

    /// The level, once worked out.
    pub fn tier(&self) -> i64 {
        self.level.expect("level not worked out yet")
    }

    pub fn geometry(&self, library: &Library) -> Result<Geometry> {
        let mut params: Params = (*self.params).clone();
        if let Some(aim) = &self.aim {
            if !params.contains_key("point") {
                params.insert("point".into(), Value::from(aim.clone()));
            }
        }
        let mut geo = library.build(&self.typ, &params)?;
        for (name, spec) in self.ports.iter() {
            geo.ports.insert(name.clone(), parse_port(spec)?);
        }
        if !self.ports.is_empty() {
            let mut ports = serde_json::Map::new();
            for (k, p) in &geo.ports {
                if self.ports.contains_key(k) {
                    ports.insert(k.clone(), serde_json::json!([p.side.to_string(), p.offset]));
                }
            }
            geo.recipe = Some((self.typ.clone(), params, ports));
        }
        Ok(geo)
    }

    pub fn param(&self, key: &str) -> Option<&Value> {
        self.params.get(key)
    }
}

/// Equipment that sits on a line (valve, flow meter, strainer ...).
#[derive(Clone, Debug, PartialEq)]
pub struct InlineItem {
    pub typ: String,
    pub id: Option<String>,
    pub params: Params,
    pub level: Option<i64>,
    pub elevation: Option<i64>,
    pub desc: String,
    pub port_in: String,
    pub port_out: String,
}

pub const INLINE_KEYS: [&str; 8] = ["type", "id", "params", "level", "elevation", "desc", "in", "out"];

fn opt_int(v: Option<&Value>) -> Result<Option<i64>> {
    match v {
        None | Some(Value::Null) => Ok(None),
        Some(x) => Ok(Some(pyval::int(x)?)),
    }
}

impl InlineItem {
    pub fn parse(v: &Value) -> Result<InlineItem> {
        match v {
            Value::String(s) => Ok(InlineItem {
                typ: s.clone(),
                id: None,
                params: Params::new(),
                level: None,
                elevation: None,
                desc: String::new(),
                port_in: "in".into(),
                port_out: "out".into(),
            }),
            Value::Object(o) => {
                let mut unknown: Vec<&String> = o.keys().filter(|k| !INLINE_KEYS.contains(&k.as_str())).collect();
                if !unknown.is_empty() {
                    unknown.sort();
                    return Err(Error::Value(format!(
                        "unknown inline item keys {}",
                        pyval::list_repr(&unknown)
                    )));
                }
                let item = InlineItem {
                    typ: pyval::str(o.get("type").ok_or_else(|| Error::Key("type".into()))?),
                    id: o.get("id").filter(|v| !v.is_null()).map(pyval::str),
                    params: match o.get("params") {
                        Some(Value::Object(p)) => p.clone(),
                        _ => Params::new(),
                    },
                    level: opt_int(o.get("level"))?,
                    elevation: opt_int(o.get("elevation"))?,
                    desc: o.get("desc").map(pyval::str).unwrap_or_default(),
                    port_in: o.get("in").map(pyval::str).unwrap_or_else(|| "in".into()),
                    port_out: o.get("out").map(pyval::str).unwrap_or_else(|| "out".into()),
                };
                if let Some(id) = &item.id {
                    check_id(id)?;
                }
                Ok(item)
            }
            _ => Err(Error::Type(format!(
                "inline item must be a type name or a dict, got {}",
                pyval::repr(v)
            ))),
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Connection {
    pub src: String,
    pub dst: String,
    pub kind: String,
    pub inline: Vec<InlineItem>,
    pub anchor: String,
    pub recycle: bool,
    ends: ((String, String), (String, String)),
}

impl Connection {
    pub fn new(src: &str, dst: &str, kind: &str) -> Connection {
        Connection::full(src, dst, kind, Vec::new(), "auto", false)
    }

    pub fn full(src: &str, dst: &str, kind: &str, inline: Vec<InlineItem>, anchor: &str, recycle: bool) -> Connection {
        let ends = (split_or_whole(src), split_or_whole(dst));
        Connection {
            src: src.into(),
            dst: dst.into(),
            kind: kind.into(),
            inline,
            anchor: anchor.into(),
            recycle,
            ends,
        }
    }

    /// ``(id, port)`` of each end.
    pub fn ends(&self) -> [(&str, &str); 2] {
        [(&self.ends.0.0, &self.ends.0.1), (&self.ends.1.0, &self.ends.1.1)]
    }

    pub fn end(&self, k: usize) -> (&str, &str) {
        self.ends()[k]
    }

    pub fn with_src(&self, src: &str) -> Connection {
        let mut c = self.clone();
        c.src = src.into();
        c.ends.0 = split_or_whole(src);
        c
    }

    pub fn with_dst(&self, dst: &str) -> Connection {
        let mut c = self.clone();
        c.dst = dst.into();
        c.ends.1 = split_or_whole(dst);
        c
    }

    pub fn with_recycle(&self, recycle: bool) -> Connection {
        let mut c = self.clone();
        c.recycle = recycle;
        c
    }
}

fn split_or_whole(r: &str) -> (String, String) {
    split_ref(r).unwrap_or_else(|_| (r.to_string(), String::new()))
}

/// Add ``(key, value)`` to ``d`` unless the value is the default.
fn changed(d: &mut serde_json::Map<String, Value>, key: &str, value: Value, default: Value) {
    if value != default {
        d.insert(key.into(), value);
    }
}

fn opt(v: Option<i64>) -> Value {
    v.map(Value::from).unwrap_or(Value::Null)
}

impl Equipment {
    pub fn to_dict(&self) -> Result<Value> {
        let mut d = serde_json::Map::new();
        d.insert("id".into(), Value::from(self.id.clone()));
        d.insert("type".into(), Value::from(self.typ.clone()));
        changed(&mut d, "level", opt(self.level), Value::Null);
        changed(&mut d, "elevation", opt(self.elevation), Value::Null);
        changed(&mut d, "span", Value::from(self.span), Value::from(1));
        changed(&mut d, "desc", Value::from(self.desc.clone()), Value::from(""));
        let mut ports = serde_json::Map::new();
        for (k, v) in self.ports.iter() {
            let p = parse_port(v)?;
            ports.insert(k.clone(), serde_json::json!([p.side.to_string(), p.offset]));
        }
        changed(&mut d, "ports", Value::Object(ports), Value::Object(Default::default()));
        changed(
            &mut d,
            "params",
            Value::Object((*self.params).clone()),
            Value::Object(Default::default()),
        );
        Ok(Value::Object(d))
    }
}

impl InlineItem {
    pub fn to_dict(&self) -> Value {
        let mut d = serde_json::Map::new();
        d.insert("type".into(), Value::from(self.typ.clone()));
        changed(
            &mut d,
            "id",
            self.id.clone().map(Value::from).unwrap_or(Value::Null),
            Value::Null,
        );
        changed(
            &mut d,
            "params",
            Value::Object(self.params.clone()),
            Value::Object(Default::default()),
        );
        changed(&mut d, "level", opt(self.level), Value::Null);
        changed(&mut d, "elevation", opt(self.elevation), Value::Null);
        changed(&mut d, "desc", Value::from(self.desc.clone()), Value::from(""));
        changed(&mut d, "in", Value::from(self.port_in.clone()), Value::from("in"));
        changed(&mut d, "out", Value::from(self.port_out.clone()), Value::from("out"));
        if d.len() == 1 {
            Value::from(self.typ.clone())
        } else {
            Value::Object(d)
        }
    }
}

impl Connection {
    pub fn to_dict(&self) -> Value {
        let mut d = serde_json::Map::new();
        d.insert("from".into(), Value::from(self.src.clone()));
        d.insert("to".into(), Value::from(self.dst.clone()));
        changed(&mut d, "type", Value::from(self.kind.clone()), Value::from("pipe"));
        changed(
            &mut d,
            "inline",
            Value::Array(self.inline.iter().map(|i| i.to_dict()).collect()),
            Value::Array(vec![]),
        );
        changed(&mut d, "anchor", Value::from(self.anchor.clone()), Value::from("auto"));
        changed(&mut d, "recycle", Value::from(self.recycle), Value::from(false));
        Value::Object(d)
    }
}

pub const REVISION_COLORS: [&str; 6] = ["red", "blue", "green", "magenta", "darkorange", "purple"];

/// An item on the sheet changed by a revision.
#[derive(Clone, Debug)]
pub struct Change {
    pub id: String,
    pub typ: Option<String>,
    pub params: Params,
    pub desc: Option<String>,
}

pub const CHANGE_KEYS: [&str; 4] = ["id", "type", "params", "desc"];

impl Change {
    pub fn to_dict(&self) -> Value {
        let mut d = serde_json::Map::new();
        d.insert("id".into(), Value::from(self.id.clone()));
        changed(
            &mut d,
            "type",
            self.typ.clone().map(Value::from).unwrap_or(Value::Null),
            Value::Null,
        );
        changed(
            &mut d,
            "params",
            Value::Object(self.params.clone()),
            Value::Object(Default::default()),
        );
        changed(
            &mut d,
            "desc",
            self.desc.clone().map(Value::from).unwrap_or(Value::Null),
            Value::Null,
        );
        Value::Object(d)
    }
}

/// A change drawn onto a finished sheet (see [`crate::revise`]).
#[derive(Clone, Debug, Default)]
pub struct Revision {
    pub id: String,
    pub color: Option<String>,
    pub note: String,
    pub equipment: Vec<Equipment>,
    pub connections: Vec<Connection>,
    pub remove: Vec<String>,
    pub remove_lines: Vec<(String, String)>,
    pub changes: Vec<Change>,
}

impl Revision {
    pub fn to_dict(&self) -> Result<Value> {
        let mut d = serde_json::Map::new();
        d.insert("id".into(), Value::from(self.id.clone()));
        changed(
            &mut d,
            "color",
            self.color.clone().map(Value::from).unwrap_or(Value::Null),
            Value::Null,
        );
        changed(&mut d, "note", Value::from(self.note.clone()), Value::from(""));
        if !self.remove.is_empty() || !self.remove_lines.is_empty() {
            let mut r: Vec<Value> = self.remove.iter().map(|x| Value::from(x.clone())).collect();
            r.extend(
                self.remove_lines
                    .iter()
                    .map(|(a, b)| serde_json::json!({"from": a, "to": b})),
            );
            d.insert("remove".into(), Value::Array(r));
        }
        if !self.changes.is_empty() {
            d.insert(
                "change".into(),
                Value::Array(self.changes.iter().map(Change::to_dict).collect()),
            );
        }
        if !self.equipment.is_empty() {
            d.insert(
                "elements".into(),
                Value::Array(self.equipment.iter().map(|e| e.to_dict()).collect::<Result<_>>()?),
            );
        }
        if !self.connections.is_empty() {
            d.insert(
                "connections".into(),
                Value::Array(self.connections.iter().map(Connection::to_dict).collect()),
            );
        }
        Ok(Value::Object(d))
    }
}
