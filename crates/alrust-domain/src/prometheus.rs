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
//! Not reproduced (the item becomes invalid instead): a label used as an
//! alert field (resource, event, severity, environment, customer, group,
//! text) whose value is not a string or an integer, a leftover label whose
//! value is a list or an object (the reference writes Python's `repr` into
//! the tag), and a non-string `correlate`, `service` or `monitor` label (an
//! exception in the reference too).

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
            PyVal::Num(n) => match n.as_f64() {
                Some(f)
                    if !n.is_i64()
                        && !n.is_u64()
                        && f.fract().abs() < f64::EPSILON
                        && f.abs() < 1e16 =>
                {
                    let _ = write!(out, "{f:.1}");
                }
                _ => out.push_str(&n.to_string()),
            },
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
            c if c < ' ' || !c.is_ascii() => {
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

/// Python `str()` of a scalar: `None`, `True`, `False`, integers as digits,
/// floats as Python writes them. `None` for a list or an object.
fn py_str(v: &PyVal) -> Option<String> {
    match v {
        PyVal::Null => Some("None".into()),
        PyVal::Bool(true) => Some("True".into()),
        PyVal::Bool(false) => Some("False".into()),
        PyVal::Str(s) => Some(s.clone()),
        PyVal::Num(n) if n.is_i64() || n.is_u64() => Some(n.to_string()),
        PyVal::Num(n) => n.as_f64().map(py_float),
        PyVal::List(_) | PyVal::Obj(_) => None,
    }
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
                push(&mut out, &py_str(v)?)?;
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

/// Python `int(x)` on a label value: `Some(Some(n))`, `Some(None)` for the
/// `ValueError` the reference catches, `None` for a `TypeError` (invalid).
fn py_int(v: &PyVal) -> Option<Option<i64>> {
    match v {
        PyVal::Str(text) => Some(py_int_text(text)),
        PyVal::Bool(b) => Some(Some(i64::from(*b))),
        PyVal::Num(n) => Some(n.as_i64().or_else(|| {
            n.as_f64()
                .filter(|f| f.is_finite() && f.abs() < 9.0e18)
                .map(|f| {
                    #[expect(
                        clippy::as_conversions,
                        clippy::cast_possible_truncation,
                        reason = "Python int() truncates toward zero; the range is checked"
                    )]
                    let t = f.trunc() as i64;
                    t
                })
        })),
        PyVal::Null | PyVal::List(_) | PyVal::Obj(_) => None,
    }
}

/// Python `int(text)` for surrounding whitespace, a sign and ASCII digits
/// with single `_` between them.
fn py_int_text(text: &str) -> Option<i64> {
    let t = text.trim();
    let (neg, digits) = match t.as_bytes().first() {
        Some(b'-') => (true, &t[1..]),
        Some(b'+') => (false, &t[1..]),
        _ => (false, t),
    };
    if digits.is_empty()
        || digits.starts_with('_')
        || digits.ends_with('_')
        || digits.contains("__")
        || !digits.chars().all(|c| c.is_ascii_digit() || c == '_')
    {
        return None;
    }
    let n: i64 = digits.replace('_', "").parse().ok()?;
    Some(if neg { -n } else { n })
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

    let severity = match status.as_str() {
        "firing" => field(
            &labels
                .pop("severity")
                .unwrap_or(PyVal::Str("warning".into())),
        )?,
        "resolved" => crate::NORMAL_SEVERITY.to_owned(),
        _ => "unknown".to_owned(),
    };
    let resource =
        field(&labels.pop_or("exported_instance", "instance", PyVal::Str("n/a".into())))?;
    let event = match labels.pop("event").filter(PyVal::truthy) {
        Some(e) => field(&e)?,
        None => field(&labels.pop("alertname")?)?,
    };
    let environment = match labels.pop("environment") {
        Some(e) => field(&e)?,
        None => default_environment.to_owned(),
    };
    let customer = match labels.pop("customer") {
        Some(c) => Some(field(&c)?),
        None => None,
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
    let group = field(&labels.pop_or("group", "job", PyVal::Str("Prometheus".into())))?;
    let monitor = match labels.pop("monitor") {
        Some(PyVal::Str(m)) => m,
        Some(_) => return None,
        None => "-".to_owned(),
    };
    let timeout = match labels.pop("timeout") {
        Some(t) => py_int(&t)?,
        None => None,
    }
    .filter(|t| *t != 0);
    let tags = labels
        .0
        .iter()
        .map(|(k, v)| py_str(v).map(|v| format!("{k}={v}")))
        .collect::<Option<Vec<_>>>()?;

    let value = annotations.pop("value");
    let summary = annotations.pop("summary");
    let description = annotations.pop("description");
    let text = match (
        description.filter(PyVal::truthy),
        summary.filter(PyVal::truthy),
    ) {
        (Some(d), _) => field(&d)?,
        (None, Some(s)) => field(&s)?,
        (None, None) => format!("{}: {resource} is {event}", severity.to_uppercase()),
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
    out.insert("severity".into(), Value::String(severity));
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
            r#"{"labels": {"alertname": "A", "x": [1]}, "annotations": {}, "startsAt": "s", "endsAt": "e"}"#,
            r#""not an object""#,
        ] {
            assert_eq!(
                items(&format!(r#"{{"alerts": [{bad}]}}"#)),
                vec![Item::Invalid],
                "{bad}"
            );
        }
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

    #[test]
    fn list_and_object_placeholders_keep_the_value() {
        // Vector 19b, checked on expand() directly: a list or object leaves
        // the value unchanged (the label itself would make the item invalid
        // as a tag).
        let args = vec![("x".to_owned(), PyVal::List(vec![PyVal::Num(1.into())]))];
        assert_eq!(expand("{x}", &args), None);
        let args = vec![("x".to_owned(), PyVal::Obj(vec![]))];
        assert_eq!(expand("{x}", &args), None);
    }

    #[test]
    fn python_scalars() {
        assert_eq!(py_float(2.5), "2.5");
        assert_eq!(py_float(1.0), "1.0");
        assert_eq!(py_float(1e16), "1e+16");
        assert_eq!(py_float(2.5e-5), "2.5e-05");
        assert_eq!(py_int_text(" +5 "), Some(5));
        assert_eq!(py_int_text("-1_0"), Some(-10));
        for bad in ["", "5.0", "_5", "5_", "1__0", "x"] {
            assert_eq!(py_int_text(bad), None, "{bad}");
        }
        assert_eq!(
            py_int(&PyVal::Num(serde_json::Number::from_f64(5.7).unwrap())),
            Some(Some(5))
        );
        assert_eq!(py_int(&PyVal::Null), None);
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
        assert_eq!(s, r#"["é\n\"x\"\u0001", "😀", 1.0, 2.5]"#);
    }
}
