//! Prometheus Alertmanager webhook (DECISIONS Y27): a port of the reference's
//! `parse_prometheus` and `PrometheusWebhook.incoming`
//! (`reference/alerta/alerta/webhooks/prometheus.py`, lines 16-116).
//!
//! [`parse`] turns one notification body into the `POST /alert` objects the
//! reference would build, so the API runs each through its normal receive
//! path. Behaviour kept from the reference, line by line:
//!
//! - label values are formatted with Python `str.format` against the raw
//!   labels, annotation values against the formatted labels, and a value
//!   that fails to format is kept as it is (lines 24-36);
//! - the `alertname` annotation, when set, replaces the label (39-40);
//! - severity, resource, event, environment, customer, correlate, service,
//!   group, origin and timeout are popped from the labels in the reference's
//!   order, and every label left over becomes a `k=v` tag in payload order
//!   (42-64);
//! - text is the `description` or `summary` annotation, else
//!   `"<SEVERITY>: <resource> is <event>"` (67-70);
//! - attributes are `startsAt`, `endsAt` and the remaining annotations, with
//!   `externalUrl` and `moreInfo` (72-82);
//! - `rawData` is the alert object as Python's `json.dumps` writes it (the
//!   reference stores the dict through psycopg2's `Json` adapter): key order
//!   kept, `", "` and `": "` separators, non-ASCII escaped.
//!
//! Differences, each a refusal or a narrower result, never a different
//! stored alert for the same accepted input:
//!
//! - a non-string label or annotation value, and a non-object alert,
//!   labels or annotations, are refused with 400 and alrust's own message
//!   (the reference would format or store them in Python-specific ways);
//! - Python format features beyond `{name}` (`!r`, `!a`, a non-empty format
//!   spec, `.attr`, `[index]`) are not reproduced: such a value is kept
//!   unformatted;
//! - a float anywhere in the alert object is written in `rawData` by
//!   `serde_json`, which can differ from Python's `repr` (Alertmanager sends
//!   no floats).

use std::fmt::Write as _;

use serde::Deserialize;
use serde::de::{self, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::{Map, Value};

/// A refusal: the HTTP status and the message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WebhookError {
    pub status: u16,
    pub message: String,
}

fn bad(message: impl Into<String>) -> WebhookError {
    WebhookError {
        status: 400,
        message: message.into(),
    }
}

/// A Python `KeyError`'s text, as the reference's `ApiError(str(e), 400)`
/// reports a missing key.
fn key_error(key: &str) -> WebhookError {
    bad(format!("'{key}'"))
}

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

/// Python `template.format(**args)` for the subset alrust reproduces:
/// `{{`, `}}`, `{name}`, `{name!s}` and `{name:}`. `None` when Python
/// raises (an unknown name, a positional field, an unbalanced brace, a bad
/// conversion) or when the template uses a feature not reproduced; the
/// caller then keeps the template unformatted, as the reference does on an
/// exception.
fn py_format(template: &str, args: &[(String, String)]) -> Option<String> {
    let mut out = String::new();
    let mut chars = template.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '{' if chars.peek() == Some(&'{') => {
                chars.next();
                out.push('{');
            }
            '}' if chars.peek() == Some(&'}') => {
                chars.next();
                out.push('}');
            }
            '}' => return None,
            '{' => {
                let mut field = String::new();
                loop {
                    match chars.next()? {
                        '}' => break,
                        '{' => return None,
                        ch => field.push(ch),
                    }
                }
                let (name, rest) = match field.find(['!', ':']) {
                    Some(i) => field.split_at(i),
                    None => (field.as_str(), ""),
                };
                if !matches!(rest, "" | "!s" | ":" | "!s:") {
                    return None;
                }
                if name.is_empty()
                    || name.chars().all(|ch| ch.is_ascii_digit())
                    || name.contains(['.', '['])
                {
                    return None;
                }
                let (_, v) = args.iter().find(|(k, _)| k == name)?;
                out.push_str(v);
            }
            c => out.push(c),
        }
    }
    Some(out)
}

/// Python `int(text)` for the forms Alertmanager labels use: surrounding
/// whitespace, an optional sign, ASCII digits with single `_` between them.
fn py_int(text: &str) -> Option<i64> {
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

/// An ordered `str → str` map: Python's `labels` / `annotations` dicts.
#[derive(Debug, Default)]
struct Dict(Vec<(String, String)>);

impl Dict {
    fn from_field(v: Option<&PyVal>, name: &str) -> Result<Self, WebhookError> {
        match v {
            None => Err(key_error(name)),
            Some(PyVal::Obj(o)) => o
                .iter()
                .map(|(k, v)| match v {
                    PyVal::Str(s) => Ok((k.clone(), s.clone())),
                    _ => Err(bad(format!("{name}.{k} must be a string"))),
                })
                .collect::<Result<_, _>>()
                .map(Dict),
            Some(_) => Err(bad(format!("{name} must be an object"))),
        }
    }
    fn get(&self, k: &str) -> Option<&str> {
        self.0
            .iter()
            .find(|(key, _)| key == k)
            .map(|(_, v)| v.as_str())
    }
    fn set(&mut self, k: &str, v: String) {
        match self.0.iter_mut().find(|(key, _)| key == k) {
            Some(slot) => slot.1 = v,
            None => self.0.push((k.to_owned(), v)),
        }
    }
    fn pop(&mut self, k: &str) -> Option<String> {
        let i = self.0.iter().position(|(key, _)| key == k)?;
        Some(self.0.remove(i).1)
    }
}

/// `parse_prometheus(alert, external_url)` (lines 16-101), as a `POST /alert`
/// object.
fn parse_alert(
    alert: &PyVal,
    external_url: Option<&PyVal>,
    default_environment: &str,
) -> Result<Map<String, Value>, WebhookError> {
    let PyVal::Obj(_) = alert else {
        return Err(bad("each alert must be an object"));
    };
    let status = match alert.get("status") {
        None => "firing".to_owned(),
        Some(PyVal::Str(s)) => s.clone(),
        Some(_) => return Err(bad("status must be a string")),
    };
    let raw_labels = Dict::from_field(alert.get("labels"), "labels")?;
    let mut labels = Dict::default();
    for (k, v) in &raw_labels.0 {
        let formatted = py_format(v, &raw_labels.0).unwrap_or_else(|| v.clone());
        labels.0.push((k.clone(), formatted));
    }
    let raw_annotations = Dict::from_field(alert.get("annotations"), "annotations")?;
    let mut annotations = Dict::default();
    for (k, v) in &raw_annotations.0 {
        let formatted = py_format(v, &labels.0).unwrap_or_else(|| v.clone());
        annotations.0.push((k.clone(), formatted));
    }
    if let Some(name) = annotations.get("alertname").filter(|n| !n.is_empty()) {
        let name = name.to_owned();
        labels.set("alertname", name);
    }

    let severity = match status.as_str() {
        "firing" => labels.pop("severity").unwrap_or_else(|| "warning".into()),
        "resolved" => crate::NORMAL_SEVERITY.to_owned(),
        _ => "unknown".to_owned(),
    };
    let resource = match labels.pop("exported_instance").filter(|s| !s.is_empty()) {
        Some(r) => r,
        None => labels.pop("instance").unwrap_or_else(|| "n/a".into()),
    };
    let event = match labels.pop("event").filter(|s| !s.is_empty()) {
        Some(e) => e,
        None => labels
            .pop("alertname")
            .ok_or_else(|| key_error("alertname"))?,
    };
    let environment = labels
        .pop("environment")
        .unwrap_or_else(|| default_environment.to_owned());
    let customer = labels.pop("customer");
    let correlate: Option<Vec<String>> = labels
        .pop("correlate")
        .map(|c| c.split(',').map(str::to_owned).collect());
    let service: Vec<String> = labels
        .pop("service")
        .unwrap_or_default()
        .split(',')
        .map(str::to_owned)
        .collect();
    let group = labels
        .pop("group")
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| labels.pop("job").unwrap_or_else(|| "Prometheus".into()));
    let origin = format!(
        "prometheus/{}",
        labels.pop("monitor").unwrap_or_else(|| "-".into())
    );
    let timeout = labels
        .pop("timeout")
        .map_or(Some(0), |t| py_int(&t))
        .filter(|t| *t != 0);
    let tags: Vec<String> = labels.0.iter().map(|(k, v)| format!("{k}={v}")).collect();

    let value = annotations.pop("value");
    let summary = annotations.pop("summary");
    let description = annotations.pop("description");
    let text = description
        .filter(|s| !s.is_empty())
        .or_else(|| summary.filter(|s| !s.is_empty()))
        .unwrap_or_else(|| format!("{}: {resource} is {event}", severity.to_uppercase()));

    let mut attributes = Map::new();
    for key in ["startsAt", "endsAt"] {
        let v = alert.get(key).ok_or_else(|| key_error(key))?;
        attributes.insert(key.to_owned(), v.to_json());
    }
    for (k, v) in &annotations.0 {
        attributes.insert(k.clone(), Value::String(v.clone()));
    }
    if let Some(url) = external_url.filter(|u| u.truthy()) {
        attributes.insert("externalUrl".into(), url.to_json());
    }
    if let Some(g) = alert.get("generatorURL") {
        let PyVal::Str(g) = g else {
            return Err(bad("generatorURL must be a string"));
        };
        attributes.insert(
            "moreInfo".into(),
            Value::String(format!(
                "<a href=\"{g}\" target=\"_blank\">Prometheus Graph</a>"
            )),
        );
    }
    let mut raw = String::new();
    alert.dumps(&mut raw);

    let mut out = Map::new();
    let s = Value::String;
    out.insert("resource".into(), s(resource));
    out.insert("event".into(), s(event));
    out.insert("environment".into(), s(environment));
    if let Some(c) = customer {
        out.insert("customer".into(), s(c));
    }
    out.insert("severity".into(), s(severity));
    if let Some(c) = correlate {
        out.insert("correlate".into(), Value::from(c));
    }
    out.insert("service".into(), Value::from(service));
    out.insert("group".into(), s(group));
    if let Some(v) = value {
        out.insert("value".into(), s(v));
    }
    out.insert("text".into(), s(text));
    out.insert("attributes".into(), Value::Object(attributes));
    out.insert("origin".into(), s(origin));
    out.insert("type".into(), s("prometheusAlert".into()));
    if let Some(t) = timeout {
        out.insert("timeout".into(), Value::from(t));
    }
    out.insert("rawData".into(), s(raw));
    out.insert("tags".into(), Value::from(tags));
    Ok(out)
}

/// `PrometheusWebhook.incoming` (lines 110-116): one `POST /alert` object
/// per alert in the notification, in order.
///
/// # Errors
///
/// 400 "no alerts in Prometheus notification payload" when the body is not
/// a JSON object with an `alerts` key (or is empty), and 400 for an alert
/// the reference would refuse with an exception (for example `'alertname'`
/// when neither an `event` nor an `alertname` label is set) or that alrust
/// does not reproduce (see the module notes).
pub fn parse(
    body: &[u8],
    default_environment: &str,
) -> Result<Vec<Map<String, Value>>, WebhookError> {
    let no_alerts = || bad("no alerts in Prometheus notification payload");
    let payload: PyVal = serde_json::from_slice(body).map_err(|_| no_alerts())?;
    if !payload.truthy() {
        return Err(no_alerts());
    }
    let Some(alerts) = payload.get("alerts") else {
        return Err(no_alerts());
    };
    let PyVal::List(alerts) = alerts else {
        return Err(bad("alerts must be a list"));
    };
    let external_url = payload.get("externalURL");
    alerts
        .iter()
        .map(|a| parse_alert(a, external_url, default_environment))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn one(body: &str) -> Map<String, Value> {
        let mut v = parse(body.as_bytes(), "Production").unwrap();
        assert_eq!(v.len(), 1);
        v.remove(0)
    }

    /// For bodies whose key order does not matter (`json!` sorts keys).
    fn one_v(body: &Value) -> Map<String, Value> {
        one(&body.to_string())
    }

    /// The reference's own test fixture shape (a firing alert with the
    /// common labels and annotations).
    #[test]
    fn a_firing_alert_maps_like_the_reference() {
        // Literal text: key order matters (tags, rawData).
        let body = r#"{
            "receiver": "alerta",
            "status": "firing",
            "externalURL": "http://am:9093",
            "alerts": [{
                "status": "firing",
                "labels": {
                    "alertname": "HighLoad", "instance": "web01:9100", "job": "node",
                    "severity": "critical", "monitor": "codelab", "environment": "Production",
                    "service": "Web,Node", "team": "ops", "runbook": "https://wiki/{job}/{alertname}"
                },
                "annotations": {
                    "summary": "High load on {instance}", "value": "7.5",
                    "dashboard": "https://grafana/{instance}"
                },
                "startsAt": "2026-11-14T05:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prom:9090/graph?g0.expr=up"
            }]
        }"#;
        let a = one(body);
        assert_eq!(
            Value::Object(a.clone()),
            json!({
                "resource": "web01:9100",
                "event": "HighLoad",
                "environment": "Production",
                "severity": "critical",
                "service": ["Web", "Node"],
                "group": "node",
                "value": "7.5",
                "text": "High load on web01:9100",
                "attributes": {
                    "startsAt": "2026-11-14T05:00:00Z",
                    "endsAt": "0001-01-01T00:00:00Z",
                    "dashboard": "https://grafana/web01:9100",
                    "externalUrl": "http://am:9093",
                    "moreInfo": "<a href=\"http://prom:9090/graph?g0.expr=up\" target=\"_blank\">Prometheus Graph</a>"
                },
                "origin": "prometheus/codelab",
                "type": "prometheusAlert",
                "rawData": a["rawData"],
                "tags": ["team=ops", "runbook=https://wiki/node/HighLoad"]
            })
        );
        // json.dumps: key order, ", " and ": " separators.
        assert!(a["rawData"].as_str().unwrap().starts_with(
            r#"{"status": "firing", "labels": {"alertname": "HighLoad", "instance": "#
        ));
    }

    #[test]
    fn resolved_and_other_statuses_and_defaults() {
        let base = |status: &str| {
            json!({"alerts": [{
                "status": status,
                "labels": {"alertname": "Down", "severity": "major"},
                "annotations": {},
                "startsAt": "s", "endsAt": "e"
            }]})
        };
        let r = one_v(&base("resolved"));
        assert_eq!(r["severity"], "normal");
        // The severity label is not popped when resolved: it becomes a tag.
        assert_eq!(r["tags"], json!(["severity=major"]));
        assert_eq!(r["text"], "NORMAL: n/a is Down");
        assert_eq!(r["service"], json!([""]));
        assert_eq!(r["group"], "Prometheus");
        assert_eq!(r["origin"], "prometheus/-");
        assert_eq!(r["environment"], "Production");
        assert!(r.get("timeout").is_none() && r.get("value").is_none());
        assert_eq!(one_v(&base("pending"))["severity"], "unknown");
        let mut no_status = base("x");
        no_status["alerts"][0]
            .as_object_mut()
            .unwrap()
            .remove("status");
        assert_eq!(one_v(&no_status)["severity"], "major");
    }

    #[test]
    fn pop_order_decides_which_labels_become_tags() {
        let a = one(r#"{"alerts": [{
            "labels": {"alertname": "A", "event": "E", "exported_instance": "x",
                       "instance": "i", "group": "G", "job": "j", "timeout": " 1_200 ",
                       "correlate": "E,F"},
            "annotations": {"alertname": "Renamed", "description": "d"},
            "startsAt": "s", "endsAt": "e"
        }]}"#);
        assert_eq!(
            (a["resource"].clone(), a["event"].clone()),
            (json!("x"), json!("E"))
        );
        assert_eq!(a["group"], "G");
        assert_eq!(a["timeout"], 1200);
        assert_eq!(a["correlate"], json!(["E", "F"]));
        // alertname (renamed by the annotation), instance and job stay as tags.
        assert_eq!(
            a["tags"],
            json!(["alertname=Renamed", "instance=i", "job=j"])
        );
        assert_eq!(a["text"], "d");
        assert_eq!(a["attributes"]["alertname"], "Renamed");
    }

    #[test]
    fn python_format_subset() {
        let args = vec![
            ("a".to_owned(), "1".to_owned()),
            ("b".to_owned(), "x".to_owned()),
        ];
        assert_eq!(py_format("{a}-{b}", &args).as_deref(), Some("1-x"));
        assert_eq!(
            py_format("{{a}} {a!s} {a:}", &args).as_deref(),
            Some("{a} 1 1")
        );
        for raises_or_unsupported in [
            "{c}", "{}", "{0}", "{a", "a}", "{a!r}", "{a:>3}", "{a.b}", "{a[0]}", "{a!}", "{a{b}}",
        ] {
            assert_eq!(
                py_format(raises_or_unsupported, &args),
                None,
                "{raises_or_unsupported}"
            );
        }
        assert_eq!(py_int(" +5 "), Some(5));
        assert_eq!(py_int("-1_0"), Some(-10));
        for bad_int in ["", "5.0", "_5", "5_", "1__0", "x"] {
            assert_eq!(py_int(bad_int), None, "{bad_int}");
        }
    }

    #[test]
    fn json_dumps_matches_python() {
        let v: PyVal =
            serde_json::from_str(r#"{"b": [1, true, null, "é\n\"x\"", "😀"], "a": {}, "b": 2}"#)
                .unwrap();
        let mut s = String::new();
        v.dumps(&mut s);
        // Python: json.dumps(json.loads(...)) keeps the first position of a
        // repeated key with its last value.
        assert_eq!(s, r#"{"b": 2, "a": {}}"#);
        let v: PyVal = serde_json::from_str(r#"["é\n\"x\"\u0001", "😀", 1.0, 2.5]"#).unwrap();
        let mut s = String::new();
        v.dumps(&mut s);
        assert_eq!(s, r#"["é\n\"x\"\u0001", "😀", 1.0, 2.5]"#);
    }

    #[test]
    fn refusals() {
        let err = |body: &str| parse(body.as_bytes(), "Production").unwrap_err();
        let no = "no alerts in Prometheus notification payload";
        for b in ["", "not json", "{}", "[]", "{\"x\": 1}"] {
            assert_eq!(err(b).message, no, "{b}");
        }
        assert_eq!(parse(br#"{"alerts": []}"#, "Production").unwrap().len(), 0);
        let alert = |a: &str| format!("{{\"alerts\": [{a}]}}");
        assert_eq!(err(&alert(r#"{"annotations": {}}"#)).message, "'labels'");
        assert_eq!(err(&alert(r#"{"labels": {}}"#)).message, "'annotations'");
        assert_eq!(
            err(&alert(
                r#"{"labels": {}, "annotations": {}, "startsAt": "s", "endsAt": "e"}"#
            ))
            .message,
            "'alertname'"
        );
        assert_eq!(
            err(&alert(
                r#"{"labels": {"alertname": "A"}, "annotations": {}, "endsAt": "e"}"#
            ))
            .message,
            "'startsAt'"
        );
        assert_eq!(
            err(&alert(r#"{"labels": {"a": 1}, "annotations": {}}"#)).status,
            400
        );
    }
}
