//! ``json.dumps`` as Python writes it, so that fingerprints of frozen
//! sheets (a hash of the text) match the Python tool's.

use serde_json::Value;

use crate::pyval::float_repr;

#[derive(Clone, Copy)]
pub struct Options {
    pub sort_keys: bool,
    pub ensure_ascii: bool,
    /// None: one line (``", "`` and ``": "``); Some(n): indented by n
    pub indent: Option<usize>,
}

pub fn dumps(v: &Value, opts: Options) -> String {
    let mut out = String::new();
    write(v, opts, 0, &mut out);
    out
}

/// ``json.dumps(v, sort_keys=True, ensure_ascii=False)``
pub fn dumps_sorted(v: &Value) -> String {
    dumps(
        v,
        Options {
            sort_keys: true,
            ensure_ascii: false,
            indent: None,
        },
    )
}

fn string(s: &str, ascii: bool, out: &mut String) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (ascii && (c as u32) > 0x7e) => {
                let mut buf = [0u16; 2];
                for u in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{:04x}", u));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

fn newline(opts: Options, depth: usize, out: &mut String) {
    if let Some(n) = opts.indent {
        out.push('\n');
        out.push_str(&" ".repeat(n * depth));
    }
}

fn write(v: &Value, opts: Options, depth: usize, out: &mut String) {
    let (item_sep, key_sep) = if opts.indent.is_some() {
        (",", ": ")
    } else {
        (", ", ": ")
    };
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => {
            if n.is_f64() {
                let f = n.as_f64().unwrap();
                if f.is_nan() {
                    out.push_str("NaN");
                } else if f.is_infinite() {
                    out.push_str(if f > 0.0 { "Infinity" } else { "-Infinity" });
                } else {
                    out.push_str(&float_repr(f));
                }
            } else {
                out.push_str(&n.to_string());
            }
        }
        Value::String(s) => string(s, opts.ensure_ascii, out),
        Value::Array(a) => {
            if a.is_empty() {
                out.push_str("[]");
                return;
            }
            out.push('[');
            for (k, x) in a.iter().enumerate() {
                if k > 0 {
                    out.push_str(item_sep);
                }
                newline(opts, depth + 1, out);
                write(x, opts, depth + 1, out);
            }
            newline(opts, depth, out);
            out.push(']');
        }
        Value::Object(o) => {
            if o.is_empty() {
                out.push_str("{}");
                return;
            }
            let mut keys: Vec<&String> = o.keys().collect();
            if opts.sort_keys {
                keys.sort();
            }
            out.push('{');
            for (k, key) in keys.iter().enumerate() {
                if k > 0 {
                    out.push_str(item_sep);
                }
                newline(opts, depth + 1, out);
                string(key, opts.ensure_ascii, out);
                out.push_str(key_sep);
                write(&o[key.as_str()], opts, depth + 1, out);
            }
            newline(opts, depth, out);
            out.push('}');
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn like_python() {
        let v = json!({"b": [1, 2.5, "é\n"], "a": {"x": null, "y": true}, "c": 1.0});
        assert_eq!(
            dumps_sorted(&v),
            r#"{"a": {"x": null, "y": true}, "b": [1, 2.5, "é\n"], "c": 1.0}"#
        );
        let ascii = dumps(
            &json!("é𝄞"),
            Options {
                sort_keys: true,
                ensure_ascii: true,
                indent: None,
            },
        );
        let want = format!("\"{}u00e9{}ud834{}udd1e\"", "\\", "\\", "\\");
        assert_eq!(ascii, want);
    }
}
