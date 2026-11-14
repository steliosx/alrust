//! Generic inbound webhook mapper (KICKOFF Phase 4: "declarative
//! field-extraction config → Alert fields"). **Proposal; nothing calls it
//! yet.** No route is registered and no configuration is loaded.
//!
//! A [`Mapping`] turns one webhook payload (any JSON value) into the JSON
//! object a client would `POST /alert`. Its output is meant to go through the
//! normal receive path unchanged (`parse_alert`, the reject policy, the
//! caller's customer, the integrations' `pre_receive`), so the mapper adds
//! no second set of validation rules. Missing values are left out, and the
//! receive path then applies its usual defaults and its 400s for a missing
//! `resource` or `event`.
//!
//! A rule reads the payload with RFC 6901 JSON pointers (`/alarm/name`):
//!
//! - `{"path": "/a/b"}`: the value there;
//! - `{"value": "Production"}`: a constant;
//! - `{"template": "{/name} on {/object}"}`: text with `{pointer}`
//!   placeholders; `{{` and `}}` are literal braces;
//!
//! optionally with `"map": {"Error": "critical"}` (applied to the extracted
//! text; an unmatched value falls through to `default`, or is kept when
//! there is no default) and `"default"` (used when the path is missing or
//! null, or the map has no entry).
//!
//! It does not set `customer` (the API key does), and refuses, when the
//! mapping is checked, any attribute name that is restricted
//! (`spec/integration-hooks.md` section 4.2).

use std::collections::BTreeMap;

use serde::Deserialize;
use serde_json::{Map, Value};

use crate::is_restricted_attribute;

/// The alert fields a mapping may set, by their `POST /alert` names.
pub const MAPPABLE_FIELDS: &[&str] = &[
    "resource",
    "event",
    "environment",
    "severity",
    "status",
    "service",
    "group",
    "value",
    "text",
    "tags",
    "origin",
    "type",
    "correlate",
    "timeout",
    "createTime",
];

/// Fields whose `POST /alert` value is a list of strings.
const LIST_FIELDS: &[&str] = &["service", "tags", "correlate"];

/// One mapping: the configuration for one webhook source.
#[derive(Debug, Clone, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Mapping {
    /// A name for logs and errors.
    pub name: String,
    /// Alert field (a [`MAPPABLE_FIELDS`] name) → rule.
    #[serde(default)]
    pub fields: BTreeMap<String, Rule>,
    /// Attribute name → rule.
    #[serde(default)]
    pub attributes: BTreeMap<String, Rule>,
    /// Keep the whole payload as the alert's `rawData`, as the reference's
    /// webhooks do. Off by default: payloads can carry data the operator
    /// does not want stored.
    #[serde(default)]
    pub keep_raw: bool,
}

/// Where a value comes from, with an optional translation and default.
#[derive(Debug, Clone, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Rule {
    #[serde(default)]
    pub path: Option<String>,
    #[serde(default)]
    pub value: Option<Value>,
    #[serde(default)]
    pub template: Option<String>,
    #[serde(default)]
    pub map: Option<BTreeMap<String, String>>,
    #[serde(default)]
    pub default: Option<Value>,
}

/// Why a mapping or a payload was refused.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MapError {
    /// The mapping itself is invalid; found when it is checked.
    Config { mapping: String, reason: String },
    /// The payload has a value of the wrong kind for a field.
    Payload {
        mapping: String,
        field: String,
        reason: String,
    },
}

impl std::fmt::Display for MapError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Config { mapping, reason } => write!(f, "mapping {mapping}: {reason}"),
            Self::Payload {
                mapping,
                field,
                reason,
            } => write!(f, "mapping {mapping}: {field}: {reason}"),
        }
    }
}

impl std::error::Error for MapError {}

/// A mapping that passed [`Mapping::check`]. Only a checked mapping can be
/// applied, so no route can run an unchecked configuration.
#[derive(Debug, Clone, PartialEq)]
pub struct CheckedMapping(Mapping);

impl Mapping {
    /// Parses and checks a mapping from its JSON text.
    ///
    /// # Errors
    ///
    /// [`MapError::Config`] when the text is not a valid mapping.
    pub fn from_json(text: &str) -> Result<CheckedMapping, MapError> {
        let m: Self = serde_json::from_str(text).map_err(|e| MapError::Config {
            mapping: "?".into(),
            reason: e.to_string(),
        })?;
        m.check()
    }

    /// Checks field names, rule shapes, pointers and templates.
    ///
    /// # Errors
    ///
    /// [`MapError::Config`] naming the first problem found.
    pub fn check(self) -> Result<CheckedMapping, MapError> {
        self.problem()?;
        Ok(CheckedMapping(self))
    }

    fn problem(&self) -> Result<(), MapError> {
        let bad = |reason: String| MapError::Config {
            mapping: self.name.clone(),
            reason,
        };
        for (field, rule) in &self.fields {
            if !MAPPABLE_FIELDS.contains(&field.as_str()) {
                return Err(bad(format!("{field:?} is not a mappable alert field")));
            }
            rule.check().map_err(|r| bad(format!("{field}: {r}")))?;
        }
        for (name, rule) in &self.attributes {
            if name.is_empty() || is_restricted_attribute(name) {
                return Err(bad(format!("attribute {name:?} cannot be mapped")));
            }
            rule.check()
                .map_err(|r| bad(format!("attributes.{name}: {r}")))?;
        }
        Ok(())
    }
}

impl CheckedMapping {
    /// The checked configuration.
    pub fn mapping(&self) -> &Mapping {
        &self.0
    }

    /// The `POST /alert` object for one payload.
    ///
    /// # Errors
    ///
    /// [`MapError::Payload`] when a value has the wrong kind for its field
    /// (an object or array where text is needed, a non-numeric timeout).
    pub fn apply(&self, payload: &Value) -> Result<Map<String, Value>, MapError> {
        let m = &self.0;
        let bad = |field: &str, reason: &str| MapError::Payload {
            mapping: m.name.clone(),
            field: field.to_owned(),
            reason: reason.to_owned(),
        };
        let mut out = Map::new();
        for (field, rule) in &m.fields {
            let Some(v) = rule.eval(payload).map_err(|r| bad(field, &r))? else {
                continue;
            };
            let v = if LIST_FIELDS.contains(&field.as_str()) {
                as_list(&v).ok_or_else(|| bad(field, "expected text or a list of text"))?
            } else if field == "timeout" {
                as_integer(&v).ok_or_else(|| bad(field, "expected an integer"))?
            } else if field == "value" {
                // The receive contract keeps `value` as JSON (ADR-0005
                // decision 8, Y38): parse_alert decides its stored form.
                v
            } else {
                Value::String(as_text(&v).ok_or_else(|| bad(field, "expected text"))?)
            };
            out.insert(field.clone(), v);
        }
        let mut attributes = Map::new();
        for (name, rule) in &m.attributes {
            if let Some(v) = rule.eval(payload).map_err(|r| bad(name, &r))? {
                attributes.insert(name.clone(), v);
            }
        }
        if !attributes.is_empty() {
            out.insert("attributes".into(), Value::Object(attributes));
        }
        if m.keep_raw {
            out.insert("rawData".into(), Value::String(payload.to_string()));
        }
        Ok(out)
    }
}

impl Rule {
    fn check(&self) -> Result<(), String> {
        let sources = [
            self.path.is_some(),
            self.value.is_some(),
            self.template.is_some(),
        ];
        if sources.iter().filter(|s| **s).count() != 1 {
            return Err("give exactly one of path, value or template".into());
        }
        if let Some(p) = &self.path {
            check_pointer(p)?;
        }
        if let Some(t) = &self.template {
            for part in template_parts(t)? {
                if let Part::Pointer(p) = part {
                    check_pointer(p)?;
                }
            }
        }
        Ok(())
    }

    /// The rule's value for a payload; `None` when there is none.
    fn eval(&self, payload: &Value) -> Result<Option<Value>, String> {
        let found = if let Some(p) = &self.path {
            payload.pointer(p).filter(|v| !v.is_null()).cloned()
        } else if let Some(v) = &self.value {
            Some(v.clone())
        } else if let Some(t) = &self.template {
            Some(Value::String(render(t, payload)?))
        } else {
            None
        };
        let Some(found) = found else {
            return Ok(self.default.clone());
        };
        let Some(map) = &self.map else {
            return Ok(Some(found));
        };
        let key = as_text(&found).ok_or("a mapped value must be text or a scalar")?;
        Ok(match map.get(&key) {
            Some(to) => Some(Value::String(to.clone())),
            None => Some(self.default.clone().unwrap_or(found)),
        })
    }
}

/// RFC 6901: empty, or `/`-separated tokens in which `~` is always
/// followed by `0` or `1`.
fn check_pointer(p: &str) -> Result<(), String> {
    if !p.is_empty() && !p.starts_with('/') {
        return Err(format!(
            "{p:?} is not a JSON pointer (it must start with /)"
        ));
    }
    let mut chars = p.chars();
    while let Some(c) = chars.next() {
        if c == '~' && !matches!(chars.next(), Some('0' | '1')) {
            return Err(format!("{p:?} has a ~ not followed by 0 or 1"));
        }
    }
    Ok(())
}

enum Part<'a> {
    Text(&'a str),
    Pointer(&'a str),
}

/// Splits a template into literal text and `{pointer}` placeholders.
fn template_parts(t: &str) -> Result<Vec<Part<'_>>, String> {
    let mut parts = Vec::new();
    let mut rest = t;
    while !rest.is_empty() {
        if let Some(r) = rest.strip_prefix("{{") {
            parts.push(Part::Text("{"));
            rest = r;
        } else if let Some(r) = rest.strip_prefix("}}") {
            parts.push(Part::Text("}"));
            rest = r;
        } else if let Some(r) = rest.strip_prefix('{') {
            let end = r.find('}').ok_or("unclosed { in template")?;
            parts.push(Part::Pointer(&r[..end]));
            rest = &r[end + 1..];
        } else if rest.starts_with('}') {
            return Err("unmatched } in template".into());
        } else {
            let end = rest.find(['{', '}']).unwrap_or(rest.len());
            parts.push(Part::Text(&rest[..end]));
            rest = &rest[end..];
        }
    }
    Ok(parts)
}

/// A template with each placeholder replaced by its value's text; a
/// missing or null value is empty.
fn render(t: &str, payload: &Value) -> Result<String, String> {
    let mut out = String::new();
    for part in template_parts(t)? {
        match part {
            Part::Text(s) => out.push_str(s),
            Part::Pointer(p) => match payload.pointer(p) {
                None | Some(Value::Null) => {}
                Some(v) => out.push_str(
                    &as_text(v).ok_or_else(|| format!("{{{p}}} is not text or a scalar"))?,
                ),
            },
        }
    }
    Ok(out)
}

/// Text for a scalar: strings as they are, numbers and booleans as JSON.
fn as_text(v: &Value) -> Option<String> {
    match v {
        Value::String(s) => Some(s.clone()),
        Value::Number(_) | Value::Bool(_) => Some(v.to_string()),
        Value::Null | Value::Array(_) | Value::Object(_) => None,
    }
}

/// A list of text: a list of scalars, or one scalar as a one-item list.
fn as_list(v: &Value) -> Option<Value> {
    let items = match v {
        Value::Array(a) => a.iter().map(as_text).collect::<Option<Vec<_>>>()?,
        other => vec![as_text(other)?],
    };
    Some(Value::Array(items.into_iter().map(Value::String).collect()))
}

/// An integer, from a JSON integer or decimal-integer text.
fn as_integer(v: &Value) -> Option<Value> {
    match v {
        Value::Number(n) if n.is_i64() => Some(v.clone()),
        Value::String(s) => s.trim().parse::<i64>().ok().map(Value::from),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// A synthetic, Veeam ONE-like alarm. Not a captured payload: the real
    /// payloads are a PIN input (STATUS "PIN inputs").
    fn synthetic_alarm() -> Value {
        json!({
            "alarm": {"name": "Backup job failed", "status": "Error", "id": 4711},
            "object": {"name": "vm-db01", "type": "VirtualMachine"},
            "tags": ["backup", "veeam"],
            "jobs": 3,
            "details": {"text": "Job DB nightly failed", "retries": "2"},
            "nothing": null
        })
    }

    fn mapping() -> CheckedMapping {
        Mapping::from_json(
            r#"{
              "name": "synthetic-veeam",
              "fields": {
                "resource": {"path": "/object/name"},
                "event": {"path": "/alarm/name"},
                "environment": {"value": "Production"},
                "severity": {"path": "/alarm/status",
                             "map": {"Error": "critical", "Warning": "warning", "Resolved": "normal"},
                             "default": "indeterminate"},
                "service": {"path": "/object/type"},
                "tags": {"path": "/tags"},
                "text": {"template": "{/alarm/name} on {/object/name} ({{id {/alarm/id}}})"},
                "timeout": {"path": "/details/retries"},
                "group": {"path": "/missing", "default": "Backup"},
                "origin": {"path": "/nothing"}
              },
              "attributes": {"jobs": {"path": "/jobs"}, "details": {"path": "/details"}}
            }"#,
        )
        .expect("valid mapping")
    }

    #[test]
    fn a_payload_becomes_a_post_alert_object() {
        let out = mapping().apply(&synthetic_alarm()).unwrap();
        assert_eq!(
            Value::Object(out),
            json!({
                "resource": "vm-db01",
                "event": "Backup job failed",
                "environment": "Production",
                "severity": "critical",
                "service": ["VirtualMachine"],
                "tags": ["backup", "veeam"],
                "text": "Backup job failed on vm-db01 ({id 4711})",
                "timeout": 2,
                "group": "Backup",
                "attributes": {"jobs": 3, "details": {"text": "Job DB nightly failed", "retries": "2"}}
            })
        );
    }

    #[test]
    fn map_default_and_passthrough() {
        let m = mapping();
        let sev = |status: Value| {
            let mut p = synthetic_alarm();
            p["alarm"]["status"] = status;
            m.apply(&p).unwrap().get("severity").cloned()
        };
        assert_eq!(sev(json!("Resolved")), Some(json!("normal")));
        assert_eq!(sev(json!("Unknown")), Some(json!("indeterminate")));
        assert_eq!(sev(Value::Null), Some(json!("indeterminate")));
        let mut raw = m.mapping().clone();
        if let Some(r) = raw.fields.get_mut("severity") {
            r.default = None;
        }
        let no_default = raw.check().unwrap();
        let mut p = synthetic_alarm();
        p["alarm"]["status"] = json!("Unknown");
        assert_eq!(
            no_default.apply(&p).unwrap().get("severity"),
            Some(&json!("Unknown"))
        );
    }

    #[test]
    fn missing_values_are_left_out_and_raw_is_opt_in() {
        let out = mapping().apply(&json!({})).unwrap();
        assert_eq!(
            Value::Object(out),
            json!({
                "environment": "Production",
                "severity": "indeterminate",
                "text": " on  ({id })",
                "group": "Backup"
            })
        );
        let mut keep = mapping().mapping().clone();
        keep.keep_raw = true;
        let raw = keep.check().unwrap().apply(&json!({"a": 1})).unwrap();
        assert_eq!(raw.get("rawData"), Some(&json!(r#"{"a":1}"#)));
    }

    /// `value` reaches the receive path as JSON, whatever its kind; escaped
    /// pointer tokens resolve.
    #[test]
    fn value_keeps_its_json_and_escapes_resolve() {
        let m = Mapping::from_json(
            r#"{"name":"v","fields":{"value":{"path":"/v"},"resource":{"path":"/a~1b~0c"}}}"#,
        )
        .unwrap();
        for v in [
            json!({"k": [1, 2]}),
            json!([1, "x"]),
            json!(true),
            json!(2.5),
            json!(7),
            json!("t"),
        ] {
            let out = m.apply(&json!({"v": v, "a/b~c": "r"})).unwrap();
            assert_eq!(out.get("value"), Some(&v), "{v}");
            assert_eq!(out.get("resource"), Some(&json!("r")));
        }
    }

    #[test]
    fn wrong_kinds_are_refused_with_the_field() {
        let mut p = synthetic_alarm();
        p["object"]["name"] = json!({"nested": true});
        assert!(matches!(
            mapping().apply(&p),
            Err(MapError::Payload { field, .. }) if field == "resource"
        ));
        let mut p = synthetic_alarm();
        p["details"]["retries"] = json!("soon");
        assert!(matches!(
            mapping().apply(&p),
            Err(MapError::Payload { field, .. }) if field == "timeout"
        ));
    }

    #[test]
    fn invalid_mappings_are_refused_when_checked() {
        for (text, needle) in [
            (
                r#"{"name":"m","fields":{"customer":{"value":"x"}}}"#,
                "not a mappable",
            ),
            (
                r#"{"name":"m","fields":{"rawData":{"value":"x"}}}"#,
                "not a mappable",
            ),
            (r#"{"name":"m","fields":{"event":{}}}"#, "exactly one"),
            (
                r#"{"name":"m","fields":{"event":{"path":"/a","value":"b"}}}"#,
                "exactly one",
            ),
            (
                r#"{"name":"m","fields":{"event":{"path":"a"}}}"#,
                "JSON pointer",
            ),
            (
                r#"{"name":"m","fields":{"text":{"template":"{/a"}}}"#,
                "unclosed",
            ),
            (
                r#"{"name":"m","fields":{"text":{"template":"a}"}}}"#,
                "unmatched",
            ),
            (
                r#"{"name":"m","fields":{"text":{"template":"{a}"}}}"#,
                "JSON pointer",
            ),
            (
                r#"{"name":"m","attributes":{"zabbix_ack_sync_intent":{"value":1}}}"#,
                "cannot be mapped",
            ),
            (r#"{"name":"m","colour":"red"}"#, "unknown field"),
            (
                r#"{"name":"m","fields":{"event":{"path":"/a~2"}}}"#,
                "not followed by 0 or 1",
            ),
            (
                r#"{"name":"m","fields":{"event":{"path":"/a~"}}}"#,
                "not followed by 0 or 1",
            ),
            (
                r#"{"name":"m","fields":{"text":{"template":"{/a~x}"}}}"#,
                "not followed by 0 or 1",
            ),
        ] {
            let e = Mapping::from_json(text).expect_err(text).to_string();
            assert!(e.contains(needle), "{text}: {e}");
        }
    }
}
