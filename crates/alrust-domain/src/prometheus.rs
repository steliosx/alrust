//! Prometheus Alertmanager webhook (DECISIONS Y27), as `spec/security-fixes.md`
//! SF-WEBHOOKS requires: the reference mapping (`reference/alerta/alerta/
//! webhooks/prometheus.py`, lines 16-116) with the owner's fixes Y10 (every
//! item is mapped and processed on its own), Y11 (`moreInfo` is the plain
//! URL) and Y12 (the placeholder rules below).
//!
//! [`parse`] checks the envelope and maps each item to the `POST /alert`
//! object the reference would build, or to [`Item::Invalid`] when the
//! reference's mapping would fail for it. The API then processes the mapped
//! items one by one.
//!
//! Kept from the reference: the pop order of labels (so the same labels are
//! left over as `k=v` tags, in payload order), the alertname annotation
//! override, text, attributes, origin, timeout through Python `int()`, and
//! `rawData` as Python's `json.dumps` writes the alert.
//!
//! Placeholders (Y12, SF-WEBHOOKS alrust item 3): label values expand against
//! the original labels, annotation values against the expanded labels. A
//! non-string value is kept as it is. A value is read as literal text, `{{`,
//! `}}` and groups from a `{` to the next `}`; it is kept unchanged on an
//! unmatched `{` or lone `}`, an empty or all-digit group, or a plain-name
//! group (none of `:`, `!`, `.`, `[`) whose name is missing or names a list
//! or object; otherwise a plain-name group becomes the value (a string as is,
//! a number, boolean or null as Python's `str()`), and every other group is
//! copied as written. Output over 65,536 UTF-8 bytes keeps the original.
//!
//! A null label takes the value the reference's `Alert()` constructor
//! stores: `customer` absent, `environment` `''` (then refused by the reject
//! policy), `job` (as the group) `Misc`, `severity` the normal severity when a
//! description or summary gives the text. Where the reference raises
//! (`resource` or `event` null, `None.upper()` for the default text, a
//! non-string `correlate`, `service` or `monitor`, `int(None)` for
//! `timeout`), the item is invalid. A label used as an alert field whose
//! value is a boolean, a float, a list or an object also makes the item
//! invalid; the PR's audit table gives the reference outcome per case. A
//! leftover label becomes the tag `k=v` with Python's `str()` of the value,
//! also for a list or an object (`x=[1]`, `x={'k': 'v'}`).

use std::borrow::Cow;
use std::fmt::Write as _;

use serde::Deserialize;
use serde::de::{self, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::{Map, Value};

/// The envelope refusal (`no alerts in Prometheus notification payload`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WebhookError {
    pub status: u16,
    pub message: String,
}

/// One notification item after mapping.
#[derive(Debug, Clone, PartialEq)]
pub enum Item {
    /// The `POST /alert` object for the item.
    Mapped(Map<String, Value>),
    /// The reference's mapping would fail for this item (SF-WEBHOOKS 2).
    Invalid,
}

/// Placeholder output limit, in UTF-8 bytes (SF-WEBHOOKS alrust item 3.5).
const EXPANSION_CAP: usize = 65_536;

/// A JSON value that keeps object key order, as a Python `dict` from
/// `json.loads` does. A repeated key keeps its first position and its last
/// value, as in Python.
#[derive(Debug, Clone, PartialEq)]
enum PyVal {
    Null,
    Bool(bool),
    Num(serde_json::Number),
    Str(String),
    List(Vec<PyVal>),
    Obj(Vec<(String, PyVal)>),
}

impl<'de> Deserialize<'de> for PyVal {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = PyVal;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("any JSON value")
            }
            fn visit_bool<E: de::Error>(self, b: bool) -> Result<PyVal, E> {
                Ok(PyVal::Bool(b))
            }
            fn visit_i64<E: de::Error>(self, n: i64) -> Result<PyVal, E> {
                Ok(PyVal::Num(n.into()))
            }
            fn visit_u64<E: de::Error>(self, n: u64) -> Result<PyVal, E> {
                Ok(PyVal::Num(n.into()))
            }
            fn visit_f64<E: de::Error>(self, n: f64) -> Result<PyVal, E> {
                serde_json::Number::from_f64(n)
                    .map(PyVal::Num)
                    .ok_or_else(|| E::custom("not a finite number"))
            }
            fn visit_str<E: de::Error>(self, s: &str) -> Result<PyVal, E> {
                Ok(PyVal::Str(s.to_owned()))
            }
            fn visit_string<E: de::Error>(self, s: String) -> Result<PyVal, E> {
                Ok(PyVal::Str(s))
            }
            fn visit_unit<E: de::Error>(self) -> Result<PyVal, E> {
                Ok(PyVal::Null)
            }
            fn visit_none<E: de::Error>(self) -> Result<PyVal, E> {
                Ok(PyVal::Null)
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut a: A) -> Result<PyVal, A::Error> {
                let mut v = Vec::new();
                while let Some(x) = a.next_element()? {
                    v.push(x);
                }
                Ok(PyVal::List(v))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut a: A) -> Result<PyVal, A::Error> {
                let mut o: Vec<(String, PyVal)> = Vec::new();
                while let Some((k, v)) = a.next_entry::<String, PyVal>()? {
                    match o.iter_mut().find(|(key, _)| *key == k) {
                        Some(slot) => slot.1 = v,
                        None => o.push((k, v)),
                    }
                }
                Ok(PyVal::Obj(o))
            }
        }
        d.deserialize_any(V)
    }
}

impl PyVal {
    fn get(&self, key: &str) -> Option<&PyVal> {
        match self {
            PyVal::Obj(o) => o.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    /// Python truthiness.
    fn truthy(&self) -> bool {
        match self {
            PyVal::Null => false,
            PyVal::Bool(b) => *b,
            PyVal::Num(n) => n.as_f64().is_some_and(|f| f.abs() > 0.0),
            PyVal::Str(s) => !s.is_empty(),
            PyVal::List(l) => !l.is_empty(),
            PyVal::Obj(o) => !o.is_empty(),
        }
    }

    fn to_json(&self) -> Value {
        match self {
            PyVal::Null => Value::Null,
            PyVal::Bool(b) => Value::Bool(*b),
            PyVal::Num(n) => Value::Number(n.clone()),
            PyVal::Str(s) => Value::String(s.clone()),
            PyVal::List(l) => Value::Array(l.iter().map(PyVal::to_json).collect()),
            PyVal::Obj(o) => {
                Value::Object(o.iter().map(|(k, v)| (k.clone(), v.to_json())).collect())
            }
        }
    }

    /// Python `json.dumps(self)`: `", "` and `": "` separators, keys in
    /// order, `ensure_ascii`.
    fn dumps(&self, out: &mut String) {
        match self {
            PyVal::Null => out.push_str("null"),
            PyVal::Bool(true) => out.push_str("true"),
            PyVal::Bool(false) => out.push_str("false"),
            // json.dumps writes a float as float.__repr__ (1e+16, 1e-05,
            // 100.0 for 1e2) and an int as its digits.
            PyVal::Num(n) => out.push_str(&py_number(n)),
            PyVal::Str(s) => dumps_str(s, out),
            PyVal::List(l) => {
                out.push('[');
                for (i, v) in l.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    v.dumps(out);
                }
                out.push(']');
            }
            PyVal::Obj(o) => {
                out.push('{');
                for (i, (k, v)) in o.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    dumps_str(k, out);
                    out.push_str(": ");
                    v.dumps(out);
                }
                out.push('}');
            }
        }
    }
}

/// A Python `json.dumps` string with `ensure_ascii=True`.
fn dumps_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            // ensure_ascii escapes everything outside ' '..='~', DEL included.
            c if !(' '..='~').contains(&c) => {
                let mut units = [0u16; 2];
                for u in c.encode_utf16(&mut units) {
                    let _ = write!(out, "\\u{u:04x}");
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// A plain-name placeholder's text (SF-WEBHOOKS alrust item 3.4): a string
/// as is; a number, boolean or null as Python's `str()`. `None` for a list
/// or an object, which keeps the original value (Y12, item 3.3). A string
/// is borrowed, so nothing longer than the cap is copied before the
/// size check (item 3.5).
fn group_text(v: &PyVal) -> Option<Cow<'_, str>> {
    match v {
        PyVal::Str(s) => Some(Cow::Borrowed(s.as_str())),
        PyVal::List(_) | PyVal::Obj(_) => None,
        scalar => Some(Cow::Owned(py_str(scalar))),
    }
}

/// Python `str(v)`: a string as is, anything else as [`py_repr`].
fn py_str(v: &PyVal) -> String {
    match v {
        PyVal::Str(s) => s.clone(),
        other => {
            let mut out = String::new();
            py_repr(other, &mut out);
            out
        }
    }
}

/// Python `repr(v)` of a value from `json.loads`: `None`, `True`, `False`,
/// integers as digits, floats as [`py_float`], strings as [`py_repr_str`],
/// lists as `[a, b]` and dicts as `{'k': v}`.
fn py_repr(v: &PyVal, out: &mut String) {
    match v {
        PyVal::Null => out.push_str("None"),
        PyVal::Bool(true) => out.push_str("True"),
        PyVal::Bool(false) => out.push_str("False"),
        PyVal::Num(n) => out.push_str(&py_number(n)),
        PyVal::Str(s) => py_repr_str(s, out),
        PyVal::List(l) => {
            out.push('[');
            for (i, e) in l.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                py_repr(e, out);
            }
            out.push(']');
        }
        PyVal::Obj(o) => {
            out.push('{');
            for (i, (k, e)) in o.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                py_repr_str(k, out);
                out.push_str(": ");
                py_repr(e, out);
            }
            out.push('}');
        }
    }
}

/// A JSON number as Python writes the `int` or `float` that `json.loads`
/// makes of it.
fn py_number(n: &serde_json::Number) -> String {
    match n.as_f64() {
        Some(f) if !n.is_i64() && !n.is_u64() => py_float(f),
        _ => n.to_string(),
    }
}

/// Python `repr(s)` of a string: single quotes, or double quotes when the
/// string has a `'` and no `"`; `\\`, `\t`, `\n`, `\r` and the quote
/// escaped; every other character that `str.isprintable()` rejects as
/// `\xhh`, `\uhhhh` or `\Uhhhhhhhh`.
fn py_repr_str(s: &str, out: &mut String) {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if crate::pytext::is_printable(c) => out.push(c),
            c => {
                let n = u32::from(c);
                let _ = if n <= 0xFF {
                    write!(out, "\\x{n:02x}")
                } else if n <= 0xFFFF {
                    write!(out, "\\u{n:04x}")
                } else {
                    write!(out, "\\U{n:08x}")
                };
            }
        }
    }
    out.push(quote);
}

/// Python's `repr` of a finite float: the shortest digits, with an exponent
/// below 1e-4 or from 1e16, as `1e+16` / `2.5e-05`; `1.0` for whole numbers.
fn py_float(f: f64) -> String {
    let a = f.abs();
    if a > 0.0 && !(1e-4..1e16).contains(&a) {
        let e = format!("{f:e}");
        let (mant, exp) = e.split_once('e').unwrap_or((e.as_str(), "0"));
        let exp: i32 = exp.parse().unwrap_or(0);
        let sign = if exp < 0 { '-' } else { '+' };
        format!("{mant}e{sign}{:02}", exp.unsigned_abs())
    } else if f.fract().abs() < f64::EPSILON {
        format!("{f:.1}")
    } else {
        format!("{f}")
    }
}

/// SF-WEBHOOKS alrust item 3: the expanded value, or `None` to keep the
/// original.
fn expand(template: &str, args: &[(String, PyVal)]) -> Option<String> {
    let mut out = String::new();
    let push = |out: &mut String, piece: &str| -> Option<()> {
        (out.len().checked_add(piece.len())? <= EXPANSION_CAP).then(|| out.push_str(piece))
    };
    let mut rest = template;
    while let Some(c) = rest.chars().next() {
        if let Some(r) = rest.strip_prefix("{{") {
            push(&mut out, "{")?;
            rest = r;
        } else if let Some(r) = rest.strip_prefix("}}") {
            push(&mut out, "}")?;
            rest = r;
        } else if c == '}' {
            return None;
        } else if let Some(r) = rest.strip_prefix('{') {
            let end = r.find('}')?;
            let group = &r[..end];
            if group.contains([':', '!', '.', '[']) {
                push(&mut out, &rest[..end + 2])?;
            } else {
                if group.is_empty() || group.chars().all(|ch| ch.is_ascii_digit()) {
                    return None;
                }
                let (_, v) = args.iter().find(|(k, _)| k == group)?;
                push(&mut out, &group_text(v)?)?;
            }
            rest = &r[end + 1..];
        } else {
            let end = rest.find(['{', '}']).unwrap_or(rest.len());
            push(&mut out, &rest[..end])?;
            rest = &rest[end..];
        }
    }
    Some(out)
}

/// Why `int(x)` gives alrust no value; either way the item is invalid.
#[derive(Debug, PartialEq, Eq)]
enum NoInt {
    /// Python raises `TypeError`, which the reference does not catch.
    TypeError,
    /// Python succeeds but the integer does not fit `i64`. The reference
    /// then fails later (a negative timeout in `Alert()`, a too large one in
    /// the database); it never falls back to the default timeout, so
    /// alrust must not either (Codex 41481c78).
    OutOfRange,
}

/// Python `int(x)` on a label value: `Ok(Some(n))`, `Ok(None)` for the
/// `ValueError` the reference catches (no timeout), `Err` otherwise.
fn py_int(v: &PyVal) -> Result<Option<i64>, NoInt> {
    match v {
        PyVal::Str(text) => py_int_text(text),
        PyVal::Bool(b) => Ok(Some(i64::from(*b))),
        PyVal::Num(n) => match (n.as_i64(), n.as_f64()) {
            (Some(i), _) => Ok(Some(i)),
            (None, _) if n.is_u64() => Err(NoInt::OutOfRange),
            (None, Some(f)) => {
                // int(float) truncates toward zero. 2^63 is exact as a float,
                // so this range check is exact too.
                let t = f.trunc();
                if (-9_223_372_036_854_775_808.0..9_223_372_036_854_775_808.0).contains(&t) {
                    #[expect(
                        clippy::as_conversions,
                        clippy::cast_possible_truncation,
                        reason = "a whole float inside the i64 range converts exactly"
                    )]
                    let i = t as i64;
                    Ok(Some(i))
                } else {
                    Err(NoInt::OutOfRange)
                }
            }
            (None, None) => Err(NoInt::OutOfRange),
        },
        PyVal::Null | PyVal::List(_) | PyVal::Obj(_) => Err(NoInt::TypeError),
    }
}

/// Python `int(text)` (base 10): the characters `int()` strips around the
/// number, a sign, then Unicode decimal digits (any script, as `int()`
/// reads them) with single `_` between them. `Ok(None)` where `int()`
/// raises `ValueError`; `Err(OutOfRange)` for a valid integer outside
/// `i64` (Python's int has no limit). A negative number is accumulated
/// negatively, so `i64::MIN` is read exactly.
fn py_int_text(text: &str) -> Result<Option<i64>, NoInt> {
    let t = text.trim_matches(crate::pytext::is_int_space);
    let (neg, digits) = match t.strip_prefix('-') {
        Some(d) => (true, d),
        None => (false, t.strip_prefix('+').unwrap_or(t)),
    };
    if digits.is_empty()
        || digits.starts_with('_')
        || digits.ends_with('_')
        || digits.contains("__")
    {
        return Ok(None);
    }
    let mut values = Vec::with_capacity(digits.len());
    for c in digits.chars().filter(|&c| c != '_') {
        match crate::pytext::decimal(c) {
            Some(d) => values.push(i64::from(d)),
            None => return Ok(None),
        }
    }
    let mut n: i64 = 0;
    for d in values {
        let next = n.checked_mul(10).and_then(|m| {
            if neg {
                m.checked_sub(d)
            } else {
                m.checked_add(d)
            }
        });
        n = next.ok_or(NoInt::OutOfRange)?;
    }
    Ok(Some(n))
}

/// An ordered `str → value` map: Python's `labels` / `annotations` dicts.
#[derive(Debug, Default, Clone)]
struct Dict(Vec<(String, PyVal)>);

impl Dict {
    fn from_field(v: Option<&PyVal>) -> Option<Self> {
        match v {
            Some(PyVal::Obj(o)) => Some(Dict(o.clone())),
            _ => None,
        }
    }
    fn get(&self, k: &str) -> Option<&PyVal> {
        self.0.iter().find(|(key, _)| key == k).map(|(_, v)| v)
    }
    fn set(&mut self, k: &str, v: PyVal) {
        match self.0.iter_mut().find(|(key, _)| key == k) {
            Some(slot) => slot.1 = v,
            None => self.0.push((k.to_owned(), v)),
        }
    }
    fn pop(&mut self, k: &str) -> Option<PyVal> {
        let i = self.0.iter().position(|(key, _)| key == k)?;
        Some(self.0.remove(i).1)
    }
    /// Python `d.pop(a, None) or d.pop(b, default)`.
    fn pop_or(&mut self, first: &str, second: &str, default: PyVal) -> PyVal {
        match self.pop(first).filter(PyVal::truthy) {
            Some(v) => v,
            None => self.pop(second).unwrap_or(default),
        }
    }
    fn expanded(&self, args: &[(String, PyVal)]) -> Self {
        Dict(
            self.0
                .iter()
                .map(|(k, v)| match v {
                    PyVal::Str(s) => (
                        k.clone(),
                        expand(s, args).map_or_else(|| v.clone(), PyVal::Str),
                    ),
                    _ => (k.clone(), v.clone()),
                })
                .collect(),
        )
    }
}

/// A label value used as an alert field: a string, or an integer as its
/// digits (what the reference's database adapter stores).
fn field(v: &PyVal) -> Option<String> {
    match v {
        PyVal::Str(s) => Some(s.clone()),
        PyVal::Num(n) if n.is_i64() || n.is_u64() => Some(n.to_string()),
        _ => None,
    }
}

/// `parse_prometheus(alert, external_url)` (lines 16-101) for one item;
/// `None` where the reference's mapping fails or alrust does not reproduce
/// the value (module notes).
#[expect(
    clippy::too_many_lines,
    reason = "one reference function, ported in order"
)]
fn map_item(
    alert: &PyVal,
    external_url: Option<&PyVal>,
    default_environment: &str,
) -> Option<Map<String, Value>> {
    let PyVal::Obj(_) = alert else { return None };
    // Only "firing" and "resolved" matter; any other value means "unknown".
    let status = match alert.get("status") {
        None => "firing".to_owned(),
        Some(PyVal::Str(s)) => s.clone(),
        Some(_) => String::new(),
    };
    let raw_labels = Dict::from_field(alert.get("labels"))?;
    let mut labels = raw_labels.expanded(&raw_labels.0);
    let raw_annotations = Dict::from_field(alert.get("annotations"))?;
    let mut annotations = raw_annotations.expanded(&labels.0);
    if let Some(name) = annotations.get("alertname").filter(|v| v.truthy()) {
        let name = name.clone();
        labels.set("alertname", name);
    }

    // `None` is Python's None: a `severity: null` label. The constructor
    // stores the normal severity for it; only the default text's
    // `severity.upper()` fails on it (below).
    let severity: Option<String> = match status.as_str() {
        "firing" => match labels.pop("severity") {
            Some(PyVal::Null) => None,
            Some(v) => Some(field(&v)?),
            None => Some("warning".to_owned()),
        },
        "resolved" => Some(crate::NORMAL_SEVERITY.to_owned()),
        _ => Some("unknown".to_owned()),
    };
    let resource =
        field(&labels.pop_or("exported_instance", "instance", PyVal::Str("n/a".into())))?;
    let event = match labels.pop("event").filter(PyVal::truthy) {
        Some(e) => field(&e)?,
        None => field(&labels.pop("alertname")?)?,
    };
    // `environment: null`: the constructor stores '' (`or ''`), which the
    // reject policy then refuses, as for any environment not allowed.
    let environment = match labels.pop("environment") {
        Some(PyVal::Null) => String::new(),
        Some(e) => field(&e)?,
        None => default_environment.to_owned(),
    };
    // `customer: null` is the ordinary no-customer state (Codex 7dda6a95).
    let customer = match labels.pop("customer") {
        Some(PyVal::Null) | None => None,
        Some(c) => Some(field(&c)?),
    };
    let correlate: Option<Vec<String>> = match labels.pop("correlate") {
        Some(PyVal::Str(c)) => Some(c.split(',').map(str::to_owned).collect()),
        Some(_) => return None,
        None => None,
    };
    let service: Vec<String> = match labels.pop("service") {
        Some(PyVal::Str(s)) => s.split(',').map(str::to_owned).collect(),
        Some(_) => return None,
        None => vec![String::new()],
    };
    // A falsy `group` falls to `job`; `job: null` makes the constructor
    // store 'Misc'.
    let group = match labels.pop_or("group", "job", PyVal::Str("Prometheus".into())) {
        PyVal::Null => "Misc".to_owned(),
        g => field(&g)?,
    };
    let monitor = match labels.pop("monitor") {
        Some(PyVal::Str(m)) => m,
        Some(_) => return None,
        None => "-".to_owned(),
    };
    let timeout = match labels.pop("timeout") {
        Some(t) => py_int(&t).ok()?,
        None => None,
    }
    .filter(|t| *t != 0);
    // f'{k}={v}': Python's str() of the value, also for a list or object.
    let tags: Vec<String> = labels
        .0
        .iter()
        .map(|(k, v)| format!("{k}={}", py_str(v)))
        .collect();

    let value = annotations.pop("value");
    let summary = annotations.pop("summary");
    let description = annotations.pop("description");
    let text = match (
        description.filter(PyVal::truthy),
        summary.filter(PyVal::truthy),
    ) {
        (Some(d), _) => field(&d)?,
        (None, Some(s)) => field(&s)?,
        // `None.upper()` raises: the item fails.
        (None, None) => format!(
            "{}: {resource} is {event}",
            severity.as_deref()?.to_uppercase()
        ),
    };

    let mut attributes = Map::new();
    for key in ["startsAt", "endsAt"] {
        attributes.insert(key.to_owned(), alert.get(key)?.to_json());
    }
    for (k, v) in &annotations.0 {
        attributes.insert(k.clone(), v.to_json());
    }
    if let Some(url) = external_url.filter(|u| u.truthy()) {
        attributes.insert("externalUrl".into(), url.to_json());
    }
    if let Some(g) = alert.get("generatorURL") {
        // Y11: the plain URL, not the reference's HTML anchor.
        attributes.insert("moreInfo".into(), g.to_json());
    }
    let mut raw = String::new();
    alert.dumps(&mut raw);

    let mut out = Map::new();
    out.insert("resource".into(), Value::String(resource));
    out.insert("event".into(), Value::String(event));
    out.insert("environment".into(), Value::String(environment));
    if let Some(c) = customer {
        out.insert("customer".into(), Value::String(c));
    }
    out.insert(
        "severity".into(),
        Value::String(severity.unwrap_or_else(|| crate::NORMAL_SEVERITY.to_owned())),
    );
    if let Some(c) = correlate {
        out.insert("correlate".into(), Value::from(c));
    }
    out.insert("service".into(), Value::from(service));
    out.insert("group".into(), Value::String(group));
    if let Some(v) = value {
        out.insert("value".into(), v.to_json());
    }
    out.insert("text".into(), Value::String(text));
    out.insert("attributes".into(), Value::Object(attributes));
    out.insert(
        "origin".into(),
        Value::String(format!("prometheus/{monitor}")),
    );
    out.insert("type".into(), Value::String("prometheusAlert".into()));
    if let Some(t) = timeout {
        out.insert("timeout".into(), Value::from(t));
    }
    out.insert("rawData".into(), Value::String(raw));
    out.insert("tags".into(), Value::from(tags));
    Some(out)
}

/// The envelope check and the per-item mapping (SF-WEBHOOKS alrust item 2).
///
/// # Errors
///
/// 400 `no alerts in Prometheus notification payload` when the body is not
/// a JSON object whose `alerts` is a list (missing, `null`, a number, a
/// string or an object).
pub fn parse(body: &[u8], default_environment: &str) -> Result<Vec<Item>, WebhookError> {
    let no_alerts = || WebhookError {
        status: 400,
        message: "no alerts in Prometheus notification payload".into(),
    };
    let payload: PyVal = serde_json::from_slice(body).map_err(|_| no_alerts())?;
    let Some(PyVal::List(alerts)) = payload.get("alerts") else {
        return Err(no_alerts());
    };
    let external_url = payload.get("externalURL");
    Ok(alerts
        .iter()
        .map(|a| map_item(a, external_url, default_environment).map_or(Item::Invalid, Item::Mapped))
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn items(body: &str) -> Vec<Item> {
        parse(body.as_bytes(), "Production").unwrap()
    }

    fn one(body: &str) -> Map<String, Value> {
        match items(body).as_slice() {
            [Item::Mapped(m)] => m.clone(),
            other => panic!("{other:?}"),
        }
    }

    /// An item in the fixture shape of SF-WEBHOOKS (labels alertname,
    /// instance, severity; annotation summary), with extra labels and
    /// annotations as literal JSON members.
    fn item(n: &str, labels: &str, annotations: &str) -> String {
        format!(
            r#"{{"status": "firing", "labels": {{"alertname": "A-{n}", "instance": "h-{n}", "severity": "major"{labels}}}, "annotations": {{"summary": "S-{n}"{annotations}}}, "startsAt": "s", "endsAt": "e"}}"#
        )
    }

    fn note(labels: &str, note: &str) -> Value {
        let m = one(&format!(
            r#"{{"alerts": [{}]}}"#,
            item("x", labels, &format!(r#", "note": {note}"#))
        ));
        m["attributes"]["note"].clone()
    }

    #[test]
    fn a_firing_alert_maps_like_the_reference() {
        let body = r#"{"externalURL": "http://am:9093", "alerts": [{
            "status": "firing",
            "labels": {"alertname": "HighLoad", "instance": "web01:9100", "job": "node",
                       "severity": "critical", "monitor": "codelab", "service": "Web,Node",
                       "team": "ops", "runbook": "https://wiki/{job}/{alertname}"},
            "annotations": {"summary": "High load on {instance}", "value": "7.5"},
            "startsAt": "s1", "endsAt": "e1",
            "generatorURL": "http://prom.test/graph?g0.expr=up"}]}"#;
        let a = one(body);
        assert_eq!(
            Value::Object(a.clone()),
            json!({
                "resource": "web01:9100", "event": "HighLoad", "environment": "Production",
                "severity": "critical", "service": ["Web", "Node"], "group": "node",
                "value": "7.5", "text": "High load on web01:9100",
                "attributes": {"startsAt": "s1", "endsAt": "e1", "externalUrl": "http://am:9093",
                               "moreInfo": "http://prom.test/graph?g0.expr=up"},
                "origin": "prometheus/codelab", "type": "prometheusAlert",
                "rawData": a["rawData"],
                "tags": ["team=ops", "runbook=https://wiki/node/HighLoad"]
            })
        );
        assert!(
            a["rawData"]
                .as_str()
                .unwrap()
                .starts_with(r#"{"status": "firing", "labels": {"alertname": "HighLoad", "#)
        );
    }

    #[test]
    fn resolved_unknown_status_and_defaults() {
        let r = one(
            r#"{"alerts": [{"status": "resolved", "labels": {"alertname": "Down", "severity": "major"}, "annotations": {}, "startsAt": "s", "endsAt": "e"}]}"#,
        );
        assert_eq!(r["severity"], "normal");
        assert_eq!(r["tags"], json!(["severity=major"]));
        assert_eq!(r["text"], "NORMAL: n/a is Down");
        assert_eq!(r["service"], json!([""]));
        assert_eq!(
            (r["group"].clone(), r["origin"].clone()),
            (json!("Prometheus"), json!("prometheus/-"))
        );
        let u = one(
            r#"{"alerts": [{"status": 7, "labels": {"alertname": "D"}, "annotations": {}, "startsAt": "s", "endsAt": "e"}]}"#,
        );
        assert_eq!(u["severity"], "unknown");
    }

    #[test]
    fn pop_order_decides_which_labels_become_tags() {
        let a = one(
            r#"{"alerts": [{"labels": {"alertname": "A", "event": "E", "exported_instance": "x",
            "instance": "i", "group": "G", "job": "j", "timeout": " 1_200 ", "correlate": "E,F", "n": 5, "b": true},
            "annotations": {"alertname": "Renamed", "description": "d"}, "startsAt": "s", "endsAt": "e"}]}"#,
        );
        assert_eq!(
            (
                a["resource"].clone(),
                a["event"].clone(),
                a["group"].clone()
            ),
            (json!("x"), json!("E"), json!("G"))
        );
        assert_eq!(a["timeout"], 1200);
        assert_eq!(a["correlate"], json!(["E", "F"]));
        assert_eq!(
            a["tags"],
            json!(["alertname=Renamed", "instance=i", "job=j", "n=5", "b=True"])
        );
    }

    /// SF-WEBHOOKS vectors 8, 8b, 8c, 8d and 8d2: the envelope.
    #[test]
    fn envelope() {
        let no = "no alerts in Prometheus notification payload";
        for b in [
            r#"{"foo": 1}"#,
            r#"{"alerts": {}}"#,
            r#"{"alerts": ""}"#,
            r#"{"alerts": null}"#,
            r#"{"alerts": 5}"#,
            r#"{"alerts": "ab"}"#,
            r#"{"alerts": {"x": 1}}"#,
            "",
            "not json",
            "[]",
        ] {
            assert_eq!(
                parse(b.as_bytes(), "Production").unwrap_err().message,
                no,
                "{b}"
            );
        }
        assert_eq!(items(r#"{"alerts": []}"#), vec![]);
    }

    /// Vectors 7, 8e and 8f: a failing item is invalid, the others map.
    #[test]
    fn invalid_items_do_not_stop_the_others() {
        let body = format!(
            r#"{{"alerts": [{}, {{"annotations": {{}}, "startsAt": "s", "endsAt": "e"}}, {}]}}"#,
            item("7-1", "", ""),
            item("7-3", "", "")
        );
        let v = items(&body);
        assert!(
            matches!(
                v.as_slice(),
                [Item::Mapped(_), Item::Invalid, Item::Mapped(_)]
            ),
            "{v:?}"
        );
        for bad in [
            r#"{"labels": {"alertname": "A", "timeout": []}, "annotations": {}, "startsAt": "s", "endsAt": "e"}"#,
            r#"{"labels": {}, "annotations": {}, "startsAt": "s", "endsAt": "e"}"#,
            r#"{"labels": {"alertname": "A"}, "annotations": {}, "endsAt": "e"}"#,
            r#""not an object""#,
        ] {
            assert_eq!(
                items(&format!(r#"{{"alerts": [{bad}]}}"#)),
                vec![Item::Invalid],
                "{bad}"
            );
        }
        // Codex 41481c78: a negative timeout inside i64 is kept (the API's
        // R0.4 check then refuses it); one beyond i64 makes the item invalid.
        // Neither may fall back to the default.
        for (label, want) in [
            (r#""-9223372036854775808""#, json!(i64::MIN)),
            ("-9e18", json!(-9_000_000_000_000_000_000_i64)),
        ] {
            let t = one(&format!(
                r#"{{"alerts": [{{"labels": {{"alertname": "A", "timeout": {label}}}, "annotations": {{}}, "startsAt": "s", "endsAt": "e"}}]}}"#
            ));
            assert_eq!(t["timeout"], want, "{label}");
        }
        for label in [r#""9223372036854775808""#, "1e19"] {
            let body = format!(
                r#"{{"alerts": [{{"labels": {{"alertname": "A", "timeout": {label}}}, "annotations": {{}}, "startsAt": "s", "endsAt": "e"}}]}}"#
            );
            assert_eq!(items(&body), vec![Item::Invalid], "{label}");
        }
        // Python int() reads Arabic-Indic digits: timeout 12, not the default.
        let t = one(
            r#"{"alerts": [{"labels": {"alertname": "A", "timeout": "\u0661\u0662"}, "annotations": {}, "startsAt": "s", "endsAt": "e"}]}"#,
        );
        assert_eq!(t["timeout"], 12);
        // A ValueError from int() is caught by the reference: no timeout.
        let t = one(
            r#"{"alerts": [{"labels": {"alertname": "A", "timeout": "soon"}, "annotations": {}, "startsAt": "s", "endsAt": "e"}]}"#,
        );
        assert!(t.get("timeout").is_none());
    }

    /// Vectors 11 to 19b: placeholders.
    #[test]
    fn placeholders() {
        assert_eq!(
            note("", r#""https://wiki.test/{instance}/{alertname}""#),
            "https://wiki.test/h-x/A-x"
        );
        assert_eq!(note("", r#""{{literal}} {alertname}""#), "{literal} A-x");
        for kept in [
            r#""{missing} {alertname}""#,
            r#""{0}""#,
            r#""{alertname""#,
            r#""{}""#,
            r#""a}""#,
        ] {
            let raw: Value = serde_json::from_str(kept).unwrap();
            assert_eq!(note("", kept), raw, "{kept}");
        }
        assert_eq!(
            note("", r#""x {alertname:>20} {instance}""#),
            "x {alertname:>20} h-x"
        );
        assert_eq!(
            note("", r#""{alertname.__class__}""#),
            "{alertname.__class__}"
        );
        assert_eq!(note(r#", "n": 5"#, r#""v={n}""#), "v=5");
        assert_eq!(note("", "7"), json!(7));
        assert_eq!(
            note(r#", "f": 2.5, "t": true, "z": null"#, r#""{f} {t} {z}""#),
            "2.5 True None"
        );
        assert_eq!(
            note(
                r#", "x": [1]"#.replace("[1]", "\"[1]\"").as_str(),
                r#""{x}""#
            ),
            "[1]"
        );
        let big = "a".repeat(1000);
        let groups = "{big}".repeat(66);
        assert_eq!(
            note(&format!(r#", "big": "{big}""#), &format!(r#""{groups}""#)),
            groups
        );
        let big = "a".repeat(1024);
        let groups = "{big}".repeat(64);
        assert_eq!(
            note(&format!(r#", "big": "{big}""#), &format!(r#""{groups}""#))
                .as_str()
                .unwrap()
                .len(),
            65_536
        );
        assert_eq!(
            note(&format!(r#", "big": "{big}""#), &format!(r#""{groups}x""#)),
            format!("{groups}x")
        );
        let u = "ü".repeat(30_000);
        assert_eq!(note(&format!(r#", "u": "{u}""#), r#""{u}{u}""#), "{u}{u}");
    }

    /// Null labels hold what the reference's `Alert()` constructor stores
    /// (Codex 7dda6a95: `customer: null` is the no-customer state).
    #[test]
    fn null_labels_take_the_constructor_values() {
        let m = one(&format!(
            r#"{{"alerts": [{}]}}"#,
            item(
                "n",
                r#", "customer": null, "environment": null, "job": null"#,
                ""
            )
        ));
        assert!(m.get("customer").is_none());
        assert_eq!(m["environment"], "");
        assert_eq!(m["group"], "Misc");
        // severity null with a summary: stored as the normal severity.
        let m = one(&format!(
            r#"{{"alerts": [{}]}}"#,
            item("n", "", "").replace(r#""severity": "major""#, r#""severity": null"#)
        ));
        assert_eq!(m["severity"], "normal");
        assert_eq!(m["text"], "S-n");
        // Without a description or summary, None.upper() raises.
        let body = r#"{"alerts": [{"labels": {"alertname": "A", "severity": null}, "annotations": {}, "startsAt": "s", "endsAt": "e"}]}"#;
        assert_eq!(items(body), vec![Item::Invalid]);
        // instance null: resource None, a constructor ValueError.
        let body = r#"{"alerts": [{"labels": {"alertname": "A", "instance": null}, "annotations": {}, "startsAt": "s", "endsAt": "e"}]}"#;
        assert_eq!(items(body), vec![Item::Invalid]);
    }

    /// Vector 19b through `parse`: a list or object label keeps an
    /// annotation that names it (Y12), and the item still maps; the label
    /// becomes a tag with Python's `str()` of the value.
    #[test]
    fn list_and_object_labels_vector_19b() {
        for (label, tag) in [("[1]", "x=[1]"), (r#"{"k": "v"}"#, "x={'k': 'v'}")] {
            let m = one(&format!(
                r#"{{"alerts": [{}]}}"#,
                item("x", &format!(r#", "x": {label}"#), r#", "note": "{x}""#)
            ));
            assert_eq!(m["attributes"]["note"], "{x}", "{label}");
            // alertname, instance and severity are popped; x is left over.
            assert_eq!(m["tags"], json!([tag]), "{label}");
        }
    }

    /// Expected text from the pinned Python 3.12.14: `str(v)` of the
    /// `json.loads` value below (see the PR for the command).
    #[test]
    fn python_str_of_lists_and_dicts() {
        let v: PyVal = serde_json::from_str(
            r#"["a'b", "q\"", "both'\"", "\u007f", "\u200b", "é", "\u0085", "\t\\", 1.5, 1e16, null, true, {"n": [2.5e-05]}, "😀", "\udb40\udc01"]"#,
        )
        .unwrap();
        assert_eq!(
            py_str(&v),
            r#"["a'b", 'q"', 'both\'"', '\x7f', '\u200b', 'é', '\x85', '\t\\', 1.5, 1e+16, None, True, {'n': [2.5e-05]}, '😀', '\U000e0001']"#
        );
        assert_eq!(py_str(&PyVal::Str("plain".into())), "plain");
    }

    #[test]
    fn python_scalars() {
        assert_eq!(py_float(2.5), "2.5");
        assert_eq!(py_float(1.0), "1.0");
        assert_eq!(py_float(1e16), "1e+16");
        assert_eq!(py_float(2.5e-5), "2.5e-05");
        assert_eq!(py_int_text(" +5 "), Ok(Some(5)));
        assert_eq!(py_int_text("-1_0"), Ok(Some(-10)));
        for bad in [
            "",
            "5.0",
            "_5",
            "5_",
            "1__0",
            "x",
            "- 1",
            "+-1",
            "\u{1c}12\u{1f}",
        ] {
            assert_eq!(py_int_text(bad), Ok(None), "{bad}");
        }
        // Expected values from the pinned CPython 3.12.14 int().
        for (text, n) in [
            ("\u{661}\u{662}", 12),
            ("\u{ff11}_\u{ff12}", 12),
            (" \u{966}\u{967} ", 1),
            ("1\u{a0}", 1),
            ("\u{661}_\u{662}", 12),
            ("+\u{661}", 1),
            ("0012", 12),
        ] {
            assert_eq!(py_int_text(text), Ok(Some(n)), "{text:?}");
        }
        assert_eq!(
            py_int(&PyVal::Num(serde_json::Number::from_f64(5.7).unwrap())),
            Ok(Some(5))
        );
        assert_eq!(py_int(&PyVal::Null), Err(NoInt::TypeError));
        // Codex 41481c78: valid integers are read exactly to the i64 limits,
        // and beyond them are out of range, never "no timeout".
        assert_eq!(py_int_text("-9223372036854775808"), Ok(Some(i64::MIN)));
        assert_eq!(py_int_text("9223372036854775807"), Ok(Some(i64::MAX)));
        for big in ["9223372036854775808", "-9223372036854775809"] {
            assert_eq!(py_int_text(big), Err(NoInt::OutOfRange), "{big}");
        }
        // A syntax error wins over the size, as in int().
        assert_eq!(py_int_text("99999999999999999999x"), Ok(None));
        let num = |f: f64| PyVal::Num(serde_json::Number::from_f64(f).unwrap());
        assert_eq!(py_int(&num(-9e18)), Ok(Some(-9_000_000_000_000_000_000)));
        assert_eq!(
            py_int(&num(-9.223_372_036_854_775_808e18)),
            Ok(Some(i64::MIN))
        );
        assert_eq!(
            py_int(&num(9.223_372_036_854_775_808e18)),
            Err(NoInt::OutOfRange)
        );
        assert_eq!(py_int(&PyVal::Num(u64::MAX.into())), Err(NoInt::OutOfRange));
    }

    #[test]
    fn json_dumps_matches_python() {
        let v: PyVal = serde_json::from_str(r#"{"b": [1, true, null], "a": {}, "b": 2}"#).unwrap();
        let mut s = String::new();
        v.dumps(&mut s);
        assert_eq!(s, r#"{"b": 2, "a": {}}"#);
        let v: PyVal = serde_json::from_str(r#"["é\n\"x\"\u0001", "😀", 1.0, 2.5]"#).unwrap();
        let mut s = String::new();
        v.dumps(&mut s);
        assert_eq!(s, r#"["\u00e9\n\"x\"\u0001", "\ud83d\ude00", 1.0, 2.5]"#);
        // Expected text from the pinned CPython 3.12.14:
        // json.dumps(json.loads('[1e16, 1e-5, 1e2, 0.1, 123.0, -0.0, 1.5e300, 7]')).
        let v: PyVal =
            serde_json::from_str("[1e16, 1e-5, 1e2, 0.1, 123.0, -0.0, 1.5e300, 7]").unwrap();
        let mut s = String::new();
        v.dumps(&mut s);
        assert_eq!(s, "[1e+16, 1e-05, 100.0, 0.1, 123.0, -0.0, 1.5e+300, 7]");
        let v: PyVal = serde_json::from_str(r#""a\u007fb""#).unwrap();
        let mut s = String::new();
        v.dumps(&mut s);
        assert_eq!(s, r#""a\u007fb""#);
    }
}
