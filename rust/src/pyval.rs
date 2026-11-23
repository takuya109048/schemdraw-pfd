//! Python value semantics for the loosely typed JSON input: ``int(v)``,
//! ``float(v)``, ``bool(v)``, ``str(v)`` and ``repr(v)`` as the Python tool
//! applies them to parameters.

use crate::error::{Error, Result};
use serde_json::{Map, Value};

pub type Params = Map<String, Value>;

/// ``int(v)``.
pub fn int(v: &Value) -> Result<i64> {
    match v {
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Ok(i)
            } else if let Some(f) = n.as_f64() {
                if !f.is_finite() {
                    return Err(Error::Value(format!("cannot convert float {} to integer", repr(v))));
                }
                Ok(f.trunc() as i64)
            } else {
                Err(Error::Value(format!("invalid integer {n}")))
            }
        }
        Value::Bool(b) => Ok(*b as i64),
        Value::String(s) => s
            .trim()
            .replace('_', "")
            .parse::<i64>()
            .map_err(|_| Error::Value(format!("invalid literal for int() with base 10: {}", repr(v)))),
        Value::Null => Err(Error::Type(
            "int() argument must be a string, a bytes-like object or a real number, not 'NoneType'".into(),
        )),
        _ => Err(Error::Type(format!(
            "int() argument must be a string, a bytes-like object or a real number, not '{}'",
            type_name(v)
        ))),
    }
}

/// ``float(v)``.
pub fn float(v: &Value) -> Result<f64> {
    match v {
        Value::Number(n) => Ok(n.as_f64().unwrap_or(0.0)),
        Value::Bool(b) => Ok(*b as i64 as f64),
        Value::String(s) => s
            .trim()
            .parse::<f64>()
            .map_err(|_| Error::Value(format!("could not convert string to float: {}", repr(v)))),
        _ => Err(Error::Type(format!(
            "float() argument must be a string or a real number, not '{}'",
            type_name(v)
        ))),
    }
}

/// ``bool(v)`` (truthiness).
pub fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

pub fn type_name(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) => {
            if n.is_f64() {
                "float"
            } else {
                "int"
            }
        }
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

/// Python's ``repr`` of a float (shortest round trip; scientific notation
/// below 1e-4 and from 1e16 on).
pub fn float_repr(f: f64) -> String {
    if f.is_nan() {
        return "nan".into();
    }
    if f.is_infinite() {
        return if f > 0.0 { "inf".into() } else { "-inf".into() };
    }
    if f == 0.0 {
        return if f.is_sign_negative() {
            "-0.0".into()
        } else {
            "0.0".into()
        };
    }
    let sci = format!("{:e}", f); // e.g. "-1.25e-5"
    let (mant, exp) = sci.split_once('e').unwrap();
    let exp: i32 = exp.parse().unwrap();
    let neg = mant.starts_with('-');
    let digits: String = mant.chars().filter(|c| c.is_ascii_digit()).collect();
    let sign = if neg { "-" } else { "" };
    if (-4..16).contains(&exp) {
        let n = digits.len() as i32;
        let point = exp + 1; // digits before the decimal point
        let body = if point <= 0 {
            format!("0.{}{}", "0".repeat((-point) as usize), digits)
        } else if point >= n {
            format!("{}{}.0", digits, "0".repeat((point - n) as usize))
        } else {
            format!("{}.{}", &digits[..point as usize], &digits[point as usize..])
        };
        format!("{sign}{body}")
    } else {
        let m = if digits.len() > 1 {
            format!("{}.{}", &digits[..1], &digits[1..])
        } else {
            digits.clone()
        };
        let es = if exp < 0 { "-" } else { "+" };
        format!("{sign}{m}e{es}{:02}", exp.abs())
    }
}

/// ``str(v)``.
pub fn str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        _ => repr(v),
    }
}

/// ``repr(v)`` of the Python value the JSON value reads as.
pub fn repr(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i.to_string()
            } else if let Some(u) = n.as_u64() {
                u.to_string()
            } else {
                float_repr(n.as_f64().unwrap_or(0.0))
            }
        }
        Value::String(s) => str_repr(s),
        Value::Array(a) => {
            let items: Vec<String> = a.iter().map(repr).collect();
            format!("[{}]", items.join(", "))
        }
        Value::Object(o) => {
            let items: Vec<String> = o.iter().map(|(k, x)| format!("{}: {}", str_repr(k), repr(x))).collect();
            format!("{{{}}}", items.join(", "))
        }
    }
}

/// ``repr(s)`` of a string (single quotes unless it holds one).
pub fn str_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::new();
    out.push(quote);
    for ch in s.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// ``repr`` of a list of strings: ``['a', 'b']``.
pub fn list_repr<S: AsRef<str>>(items: &[S]) -> String {
    let parts: Vec<String> = items.iter().map(|s| str_repr(s.as_ref())).collect();
    format!("[{}]", parts.join(", "))
}

/// A text equal for equal values, the order of dict keys aside (like
/// ``json.dumps(v, sort_keys=True)``).
pub fn key(v: &Value) -> String {
    let mut out = String::new();
    write_key(v, &mut out);
    out
}

pub fn params_key(p: &Params) -> String {
    let mut out = String::new();
    write_object(p, &mut out);
    out
}

fn write_object(o: &Map<String, Value>, out: &mut String) {
    let mut keys: Vec<&String> = o.keys().collect();
    keys.sort();
    out.push('{');
    for (i, k) in keys.iter().enumerate() {
        if i > 0 {
            out.push_str(", ");
        }
        out.push_str(&serde_json::to_string(k).unwrap());
        out.push_str(": ");
        write_key(&o[k.as_str()], out);
    }
    out.push('}');
}

fn write_key(v: &Value, out: &mut String) {
    match v {
        Value::Object(o) => write_object(o, out),
        Value::Array(a) => {
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_key(x, out);
            }
            out.push(']');
        }
        Value::Number(n) => {
            if n.is_f64() {
                out.push_str(&float_repr(n.as_f64().unwrap()));
            } else {
                out.push_str(&n.to_string());
            }
        }
        other => out.push_str(&other.to_string()),
    }
}

/// Python's ``round(x)`` of a float: to the nearest integer, ties to even.
pub fn round_half_even(x: f64) -> i64 {
    x.round_ties_even() as i64
}

/// Python's ``a // b`` on integers (floor division).
pub fn floordiv(a: i64, b: i64) -> i64 {
    let q = a / b;
    if a % b != 0 && ((a < 0) != (b < 0)) { q - 1 } else { q }
}

/// Python's ``a % b`` on floats (the result takes the divisor's sign).
pub fn fmod(a: f64, b: f64) -> f64 {
    let r = a % b;
    if r != 0.0 && (r < 0.0) != (b < 0.0) { r + b } else { r }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn float_reprs() {
        assert_eq!(float_repr(1.0), "1.0");
        assert_eq!(float_repr(16.0), "16.0");
        assert_eq!(float_repr(0.1), "0.1");
        assert_eq!(float_repr(1e-5), "1e-05");
        assert_eq!(float_repr(1.5e16), "1.5e+16");
        assert_eq!(float_repr(123.456), "123.456");
        assert_eq!(float_repr(0.0001), "0.0001");
        assert_eq!(float_repr(-2.5), "-2.5");
    }

    #[test]
    fn floor_division() {
        assert_eq!(floordiv(-7, 2), -4);
        assert_eq!(floordiv(7, 2), 3);
        assert_eq!(floordiv(-6, 2), -3);
        assert_eq!(fmod(-30.0, 360.0), 330.0);
    }
}
