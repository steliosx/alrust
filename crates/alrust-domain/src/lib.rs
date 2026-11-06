//! Alert domain types shared across alrust crates.
//!
//! Field names and JSON shapes follow the reference Alerta v9.1.0 API
//! (`alerta/models/alert.py` `serialize`, `history.py`, `heartbeat.py`,
//! `key.py`), so existing Alerta clients can talk to alrust unchanged.

pub mod bearer;

use std::fmt::Write as _;

use chrono::{DateTime, SecondsFormat, Utc};
use serde::{Deserialize, Serialize, Serializer};
use serde_json::{Map, Value};

/// `id` as one URI path segment for an `href`: every byte outside RFC 3986's
/// unreserved set is percent-encoded, so a caller-supplied id containing `/`,
/// `?`, `#`, `%` or non-ASCII stays one segment. Generated UUIDs are
/// unchanged. The ids `.` and `..` remain dot segments, which a normalizing
/// URI client may remove (a known limitation; ids are not validated).
pub fn path_segment(id: &str) -> String {
    let mut out = String::with_capacity(id.len());
    for b in id.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'.' | b'_' | b'~') {
            out.push(char::from(b));
        } else {
            // Writing to a String cannot fail.
            let _ = write!(out, "%{b:02X}");
        }
    }
    out
}

/// Severity levels of the default `ALERTA` alarm model (`sm.py:7-58`).
/// A lower level is more severe.
pub fn severity_level(severity: &str) -> Option<u8> {
    Some(match severity {
        "security" => 0,
        "critical" => 1,
        "major" => 2,
        "minor" => 3,
        "warning" => 4,
        "indeterminate" => 5,
        "informational" => 6,
        "normal" | "ok" | "cleared" => 7,
        "debug" => 8,
        "trace" => 9,
        "unknown" => 10,
        _ => return None,
    })
}

pub const NORMAL_SEVERITY: &str = "normal";
pub const NORMAL_SEVERITY_LEVEL: u8 = 7;
pub const DEFAULT_PREVIOUS_SEVERITY: &str = "indeterminate";
pub const INFORM_SEVERITY: &str = "informational";

/// The severities in level order, as the reference lists them in errors.
pub const VALID_SEVERITIES: &[&str] = &[
    "security",
    "critical",
    "major",
    "minor",
    "warning",
    "indeterminate",
    "informational",
    "normal",
    "ok",
    "cleared",
    "debug",
    "trace",
    "unknown",
];

/// Statuses. Stored as plain strings: `PUT /alert/<id>/status` accepts any
/// non-empty string (spec A1.2), so the set is open.
pub mod status {
    pub const OPEN: &str = "open";
    pub const ASSIGN: &str = "assign";
    pub const ACK: &str = "ack";
    pub const SHELVED: &str = "shelved";
    pub const BLACKOUT: &str = "blackout";
    pub const CLOSED: &str = "closed";
    pub const EXPIRED: &str = "expired";
    pub const UNKNOWN: &str = "unknown";
}

/// Operator actions understood by the state machine (`ACTION_ALL`).
pub mod action {
    pub const OPEN: &str = "open";
    pub const ASSIGN: &str = "assign";
    pub const ACK: &str = "ack";
    pub const UNACK: &str = "unack";
    pub const SHELVE: &str = "shelve";
    pub const UNSHELVE: &str = "unshelve";
    pub const CLOSE: &str = "close";
    pub const EXPIRED: &str = "expired";
    pub const TIMEOUT: &str = "timeout";
    pub const ALL: &[&str] = &[
        OPEN, ASSIGN, ACK, UNACK, SHELVE, UNSHELVE, CLOSE, EXPIRED, TIMEOUT,
    ];
}

/// History change types (`enums.py` `ChangeType`).
pub mod change_type {
    pub const NEW: &str = "new";
    pub const STATUS: &str = "status";
    pub const VALUE: &str = "value";
    pub const SEVERITY: &str = "severity";
    pub const ACTION: &str = "action";
    pub const ALL: &[&str] = &[
        "open", "assign", "ack", "unack", "shelve", "unshelve", "close", "new", "action", "status",
        "value", "severity", "note", "dismiss", "timeout", "expired",
    ];
}

/// `trend(prev, cur)` (`sm.py:102-111`).
pub fn trend(previous: &str, current: &str) -> &'static str {
    match (severity_level(previous), severity_level(current)) {
        (Some(p), Some(c)) if p > c => "moreSevere",
        (Some(p), Some(c)) if p < c => "lessSevere",
        _ => "noChange",
    }
}

/// Reference timestamp format: `YYYY-MM-DDThh:mm:ss.sssZ` (`format.py:62-64`).
pub fn iso8601(t: &DateTime<Utc>) -> String {
    t.to_rfc3339_opts(SecondsFormat::Millis, true)
}

/// Parse a client timestamp. The reference accepts exactly
/// `%Y-%m-%dT%H:%M:%S.%fZ`; alrust also accepts any RFC 3339 time.
pub fn parse_time(s: &str) -> Option<DateTime<Utc>> {
    if let Ok(t) = chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%S%.fZ") {
        return Some(t.and_utc());
    }
    DateTime::parse_from_rfc3339(s)
        .ok()
        .map(|t| t.with_timezone(&Utc))
}

/// The reference truncates stored times to milliseconds.
pub fn truncate_ms(t: DateTime<Utc>) -> DateTime<Utc> {
    DateTime::from_timestamp_millis(t.timestamp_millis()).unwrap_or(t)
}

/// Alert attribute keys that are restricted staging data of an integration and
/// never appear in a response, on any read path, for any caller
/// (`spec/integration-hooks.md` section 4.2; Zabalack's `zabbix_ack_sync_intent`,
/// `zab-al-ack@eaf2acf:src/zabalack/crypto.py:42`). Only these exact keys are
/// restricted: Zabalack's other `zabbix_ack_sync*` attributes stay visible.
pub const RESTRICTED_ATTRIBUTES: &[&str] = &["zabbix_ack_sync_intent"];

/// Whether `key` is a restricted attribute key.
pub fn is_restricted_attribute(key: &str) -> bool {
    RESTRICTED_ATTRIBUTES.contains(&key)
}

/// Text that marks restricted capture evidence, compared case-insensitively:
/// a Zabalack or Ackline marker prefix, or the restricted attribute's name
/// (hook spec section 4.2, after `_contains_evidence`,
/// `zab-al-ack@eaf2acf:src/zabalack/plugins.py:26-27`).
const EVIDENCE: &[&str] = &["[zabalack:", "[ackline:", "zabbix_ack_sync_intent"];

/// What an error body says instead of a message that carries evidence
/// (`plugins.py:49-56`).
pub const REDACTED_ERROR: &str = "zabalack_restricted_error_redacted";

/// What a log record says instead of a diagnostic that carries evidence
/// (`plugins.py:30-42`).
pub const REDACTED_DIAGNOSTIC: &str = "zabalack_restricted_diagnostic_redacted";

/// Whether `text` may carry restricted capture evidence.
pub fn contains_evidence(text: &str) -> bool {
    let lower = text.to_lowercase();
    EVIDENCE.iter().any(|e| lower.contains(e))
}

/// `text` for a log record: unchanged, or `REDACTED_DIAGNOSTIC` when it may
/// carry evidence.
pub fn redact_diagnostic(text: String) -> String {
    if contains_evidence(&text) {
        REDACTED_DIAGNOSTIC.to_owned()
    } else {
        text
    }
}

/// An alert's attributes as a response shows them: every key except the
/// restricted ones, values unchanged.
#[expect(
    clippy::trivially_copy_pass_by_ref,
    reason = "serde's serialize_with passes the field by reference"
)]
fn ser_public_attributes<S: Serializer>(
    attributes: &&Map<String, Value>,
    s: S,
) -> Result<S::Ok, S::Error> {
    use serde::ser::SerializeMap as _;
    let shown = attributes
        .iter()
        .filter(|(k, _)| !is_restricted_attribute(k));
    let mut map = s.serialize_map(None)?;
    for (k, v) in shown {
        map.serialize_entry(k, v)?;
    }
    map.end()
}

fn ser_time<S: Serializer>(t: &DateTime<Utc>, s: S) -> Result<S::Ok, S::Error> {
    s.serialize_str(&iso8601(t))
}

#[expect(
    clippy::ref_option,
    reason = "serde's serialize_with passes the field by reference"
)]
fn ser_opt_time<S: Serializer>(t: &Option<DateTime<Utc>>, s: S) -> Result<S::Ok, S::Error> {
    match t {
        Some(t) => s.serialize_str(&iso8601(t)),
        None => s.serialize_none(),
    }
}

/// One history entry, as stored (newest first) and as serialized.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct HistoryEntry {
    pub id: String,
    pub event: String,
    pub severity: Option<String>,
    pub status: Option<String>,
    /// Stored as text, as in the reference's `history` type.
    pub value: Option<String>,
    pub text: Option<String>,
    #[serde(rename = "type")]
    pub change_type: String,
    pub update_time: DateTime<Utc>,
    pub user: Option<String>,
    pub timeout: Option<i64>,
}

/// A stored alert.
#[derive(Debug, Clone, PartialEq)]
pub struct Alert {
    pub id: String,
    pub resource: String,
    pub event: String,
    pub environment: String,
    pub severity: String,
    pub correlate: Vec<String>,
    pub status: String,
    pub service: Vec<String>,
    pub group: String,
    /// The reference column is `text`; NULL is `None`.
    pub value: Option<String>,
    pub text: String,
    pub tags: Vec<String>,
    pub attributes: Map<String, Value>,
    pub origin: String,
    pub event_type: String,
    pub create_time: DateTime<Utc>,
    /// `None` only when `PUT /status` stored NULL (spec A1.3); reads show
    /// `ALERT_TIMEOUT` instead (A1.3a).
    pub timeout: Option<i64>,
    pub raw_data: Option<String>,
    pub customer: Option<String>,
    // The next six are NULL only in rows alrust did not write; the
    // reference's model keeps them `None` (ADR-0005 decision 7).
    pub duplicate_count: Option<i64>,
    pub repeat: Option<bool>,
    pub previous_severity: Option<String>,
    pub trend_indication: Option<String>,
    pub receive_time: DateTime<Utc>,
    pub last_receive_id: Option<String>,
    pub last_receive_time: Option<DateTime<Utc>>,
    /// NULL in reference rows written before the column existed.
    pub update_time: Option<DateTime<Utc>>,
    /// Storage order: newest first.
    pub history: Vec<HistoryEntry>,
}

/// The JSON body of an alert, as the reference serializes it.
#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AlertBody<'a> {
    pub id: &'a str,
    pub href: String,
    pub resource: &'a str,
    pub event: &'a str,
    pub environment: &'a str,
    pub severity: &'a str,
    pub correlate: &'a [String],
    pub status: &'a str,
    pub service: &'a [String],
    pub group: &'a str,
    pub value: Option<&'a str>,
    pub text: &'a str,
    pub tags: &'a [String],
    /// Without the restricted keys (section 4.2 of the hook spec).
    #[serde(serialize_with = "ser_public_attributes")]
    pub attributes: &'a Map<String, Value>,
    pub origin: &'a str,
    #[serde(rename = "type")]
    pub event_type: &'a str,
    #[serde(serialize_with = "ser_time")]
    pub create_time: DateTime<Utc>,
    pub timeout: i64,
    pub raw_data: Option<&'a str>,
    pub customer: Option<&'a str>,
    pub duplicate_count: Option<i64>,
    pub repeat: Option<bool>,
    pub previous_severity: Option<&'a str>,
    pub trend_indication: Option<&'a str>,
    #[serde(serialize_with = "ser_time")]
    pub receive_time: DateTime<Utc>,
    pub last_receive_id: Option<&'a str>,
    #[serde(serialize_with = "ser_opt_time")]
    pub last_receive_time: Option<DateTime<Utc>>,
    #[serde(serialize_with = "ser_opt_time")]
    pub update_time: Option<DateTime<Utc>>,
    pub history: Vec<HistoryBody<'a>>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct HistoryBody<'a> {
    pub id: &'a str,
    pub href: String,
    pub event: &'a str,
    pub severity: Option<&'a str>,
    pub status: Option<&'a str>,
    pub value: Option<&'a str>,
    pub text: Option<&'a str>,
    #[serde(rename = "type")]
    pub change_type: &'a str,
    #[serde(serialize_with = "ser_time")]
    pub update_time: DateTime<Utc>,
    pub user: Option<&'a str>,
    pub timeout: Option<i64>,
}

/// Whether a serialized alert carries its history list (`show-history`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HistoryOutput {
    Include,
    Omit,
}

impl Alert {
    /// Serialize like `Alert.serialize`: history sorted ascending by
    /// `updateTime`, stable, so equal times keep storage order (spec R7.3b).
    pub fn body<'a>(
        &'a self,
        base_url: &str,
        alert_timeout: i64,
        history: HistoryOutput,
    ) -> AlertBody<'a> {
        let history = if history == HistoryOutput::Include {
            let mut h: Vec<&HistoryEntry> = self.history.iter().collect();
            h.sort_by_key(|e| e.update_time);
            h.into_iter()
                .map(|e| HistoryBody {
                    id: &e.id,
                    href: format!("{base_url}/alert/{}", path_segment(&e.id)),
                    event: &e.event,
                    severity: e.severity.as_deref(),
                    status: e.status.as_deref(),
                    value: e.value.as_deref(),
                    text: e.text.as_deref(),
                    change_type: &e.change_type,
                    update_time: e.update_time,
                    user: e.user.as_deref(),
                    timeout: e.timeout,
                })
                .collect()
        } else {
            Vec::new()
        };
        AlertBody {
            id: &self.id,
            href: format!("{base_url}/alert/{}", path_segment(&self.id)),
            resource: &self.resource,
            event: &self.event,
            environment: &self.environment,
            severity: &self.severity,
            correlate: &self.correlate,
            status: &self.status,
            service: &self.service,
            group: &self.group,
            value: self.value.as_deref(),
            text: &self.text,
            tags: &self.tags,
            attributes: &self.attributes,
            origin: &self.origin,
            event_type: &self.event_type,
            create_time: self.create_time,
            timeout: self.timeout.unwrap_or(alert_timeout),
            raw_data: self.raw_data.as_deref(),
            customer: self.customer.as_deref(),
            duplicate_count: self.duplicate_count,
            repeat: self.repeat,
            previous_severity: self.previous_severity.as_deref(),
            trend_indication: self.trend_indication.as_deref(),
            receive_time: self.receive_time,
            last_receive_id: self.last_receive_id.as_deref(),
            last_receive_time: self.last_receive_time,
            update_time: self.update_time,
            history,
        }
    }
}

/// A stored heartbeat.
#[derive(Debug, Clone, PartialEq)]
pub struct Heartbeat {
    pub id: String,
    pub origin: String,
    pub tags: Vec<String>,
    pub attributes: Map<String, Value>,
    pub event_type: String,
    pub create_time: DateTime<Utc>,
    pub timeout: i64,
    pub receive_time: DateTime<Utc>,
    pub customer: Option<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct HeartbeatBody<'a> {
    pub id: &'a str,
    pub href: String,
    pub origin: &'a str,
    pub tags: &'a [String],
    pub attributes: &'a Map<String, Value>,
    #[serde(rename = "type")]
    pub event_type: &'a str,
    #[serde(serialize_with = "ser_time")]
    pub create_time: DateTime<Utc>,
    pub timeout: i64,
    pub max_latency: i64,
    #[serde(serialize_with = "ser_time")]
    pub receive_time: DateTime<Utc>,
    pub customer: Option<&'a str>,
    pub latency: i64,
    pub since: i64,
    pub status: &'static str,
}

impl Heartbeat {
    /// Derived fields are recomputed on every read (spec HB2).
    pub fn body<'a>(
        &'a self,
        base_url: &str,
        max_latency_ms: i64,
        now: DateTime<Utc>,
    ) -> HeartbeatBody<'a> {
        let latency = (self.receive_time - self.create_time).num_milliseconds();
        let elapsed = now - self.receive_time;
        // Expiry compares the full elapsed time (`since.total_seconds() > timeout`);
        // only the serialized `since` is truncated to whole seconds (spec HB2).
        let since = elapsed.num_seconds();
        // Compare in i128 microseconds: any stored i64 timeout fits, so a very
        // large timeout reads as not expired instead of overflowing.
        let elapsed_us = elapsed
            .num_microseconds()
            .map_or_else(|| i128::from(elapsed.num_seconds()) * 1_000_000, i128::from);
        let status = if elapsed_us > i128::from(self.timeout) * 1_000_000 {
            "expired"
        } else if latency > max_latency_ms {
            "slow"
        } else {
            "ok"
        };
        HeartbeatBody {
            id: &self.id,
            href: format!("{base_url}/heartbeat/{}", path_segment(&self.id)),
            origin: &self.origin,
            tags: &self.tags,
            attributes: &self.attributes,
            event_type: &self.event_type,
            create_time: self.create_time,
            timeout: self.timeout,
            max_latency: max_latency_ms,
            receive_time: self.receive_time,
            customer: self.customer.as_deref(),
            latency,
            since,
            status,
        }
    }
}

/// An API key.
#[derive(Debug, Clone, PartialEq)]
pub struct ApiKey {
    pub id: String,
    pub key: String,
    pub user: String,
    /// NULL only in rows alrust did not write; such a key never authenticates
    /// (ADR-0005 decision 7).
    pub scopes: Option<Vec<String>>,
    pub text: String,
    /// NULL only in rows alrust did not write; such a key never authenticates.
    pub expire_time: Option<DateTime<Utc>>,
    pub count: i64,
    pub last_used_time: Option<DateTime<Utc>>,
    pub customer: Option<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ApiKeyBody<'a> {
    pub id: &'a str,
    /// The secret: present only in the creation response (SF-KEYS S1).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub key: Option<&'a str>,
    pub status: &'static str,
    pub href: String,
    pub user: &'a str,
    pub scopes: &'a [String],
    #[serde(rename = "type")]
    pub key_type: &'static str,
    pub text: &'a str,
    #[serde(serialize_with = "ser_opt_time")]
    pub expire_time: Option<DateTime<Utc>>,
    pub count: i64,
    #[serde(serialize_with = "ser_opt_time")]
    pub last_used_time: Option<DateTime<Utc>>,
    pub customer: Option<&'a str>,
}

/// Whether a serialized key carries its secret: only the creation response does.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeySecret {
    Reveal,
    Hide,
}

impl ApiKey {
    /// Serialize a key.
    pub fn body<'a>(
        &'a self,
        base_url: &str,
        now: DateTime<Utc>,
        secret: KeySecret,
    ) -> ApiKeyBody<'a> {
        let scopes: &[String] = self.scopes.as_deref().unwrap_or(&[]);
        let key_type = if scopes
            .iter()
            .any(|s| s.starts_with("write") || s.starts_with("admin"))
        {
            "read-write"
        } else {
            "read-only"
        };
        ApiKeyBody {
            id: &self.id,
            key: (secret == KeySecret::Reveal).then_some(self.key.as_str()),
            // A NULL expiry or NULL scopes (rows alrust did not write) can
            // never authenticate: "inactive", an alrust-only status
            // (ADR-0005 decision 7).
            status: match (self.expire_time, &self.scopes) {
                (None, _) | (_, None) => "inactive",
                (Some(t), Some(_)) if t >= now => "active",
                _ => "expired",
            },
            href: format!("{base_url}/key/{}", path_segment(&self.id)),
            user: &self.user,
            scopes,
            key_type,
            text: &self.text,
            expire_time: self.expire_time,
            count: self.count,
            last_used_time: self.last_used_time,
            customer: self.customer.as_deref(),
        }
    }

    /// Scope check: `admin` grants everything; `write` and `write:<x>` grant
    /// the matching read too, as in the reference's scope hierarchy.
    pub fn has_scope(&self, wanted: &str) -> bool {
        scope_allows(self.scopes.as_deref().unwrap_or(&[]), wanted)
    }
}

pub fn scope_allows(scopes: &[String], wanted: &str) -> bool {
    let (want_type, want_res) = match wanted.split_once(':') {
        Some((t, r)) => (t, Some(r)),
        None => (wanted, None),
    };
    scopes.iter().any(|s| {
        let (have_type, have_res) = match s.split_once(':') {
            Some((t, r)) => (t, Some(r)),
            None => (s.as_str(), None),
        };
        let type_ok = matches!(
            (have_type, want_type),
            ("admin", _) | ("write", "write" | "read") | ("read", "read")
        );
        let res_ok = match (have_res, want_res) {
            (None, _) => true,
            (Some(h), Some(w)) => h == w,
            (Some(_), None) => false,
        };
        type_ok && res_ok
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn trend_matches_reference() {
        assert_eq!(trend("indeterminate", "major"), "moreSevere");
        assert_eq!(trend("major", "normal"), "lessSevere");
        assert_eq!(trend("ok", "normal"), "noChange");
        assert_eq!(trend("bogus", "major"), "noChange");
    }

    #[test]
    fn iso8601_has_millis_and_z() {
        let t = parse_time("2026-10-13T01:02:03.456Z").unwrap();
        assert_eq!(iso8601(&t), "2026-10-13T01:02:03.456Z");
    }

    fn alert_with(attributes: Map<String, Value>, history_text: &str) -> Alert {
        let t = parse_time("2026-10-13T01:00:00.000Z").unwrap();
        Alert {
            id: "a".into(),
            resource: "r".into(),
            event: "e".into(),
            environment: "Production".into(),
            severity: "major".into(),
            correlate: vec![],
            status: "ack".into(),
            service: vec![],
            group: "Misc".into(),
            value: None,
            text: "t".into(),
            tags: vec![],
            attributes,
            origin: "o".into(),
            event_type: "exceptionAlert".into(),
            create_time: t,
            timeout: Some(86_400),
            raw_data: None,
            customer: None,
            duplicate_count: Some(0),
            repeat: Some(false),
            previous_severity: None,
            trend_indication: None,
            receive_time: t,
            last_receive_id: None,
            last_receive_time: None,
            update_time: Some(t),
            history: vec![HistoryEntry {
                id: "a".into(),
                event: "e".into(),
                severity: None,
                status: Some("ack".into()),
                value: None,
                text: Some(history_text.into()),
                change_type: "ack".into(),
                update_time: t,
                user: Some("op".into()),
                timeout: None,
            }],
        }
    }

    // Hook spec section 4.2: the restricted key never appears in a response
    // (every alert response is built from AlertBody); every other key,
    // including Zabalack's other sync attributes, is shown unchanged; the
    // signed marker in history text stays visible.
    #[test]
    fn restricted_attribute_is_never_serialized() {
        let attributes: Map<String, Value> = serde_json::from_value(serde_json::json!({
            "zabbix_ack_sync_intent": {"v": 1, "action_id": "x"},
            "zabbix_ack_sync": "pending",
            "zabbix_ack_sync_reason": "awaiting_capture",
            "zabbix_ack_sync_action_id": "x",
            "zabbix_ack_sync_intent_other": 1,
            "Zabbix_ack_sync_intent": 2,
            "source_system": "zabbix",
        }))
        .unwrap();
        let marker = "done\n[zabalack:v1 action=x kid=k mac=00]";
        let a = alert_with(attributes, marker);
        for output in [HistoryOutput::Include, HistoryOutput::Omit] {
            let v = serde_json::to_value(a.body("http://x", 86_400, output)).unwrap();
            let shown = v["attributes"].as_object().unwrap();
            assert!(!shown.contains_key("zabbix_ack_sync_intent"));
            let mut keys: Vec<&str> = shown.keys().map(String::as_str).collect();
            keys.sort_unstable();
            assert_eq!(
                keys,
                [
                    "Zabbix_ack_sync_intent",
                    "source_system",
                    "zabbix_ack_sync",
                    "zabbix_ack_sync_action_id",
                    "zabbix_ack_sync_intent_other",
                    "zabbix_ack_sync_reason",
                ]
            );
            assert_eq!(shown["zabbix_ack_sync"], "pending");
            assert!(!v.to_string().contains("\"action_id\":\"x\""));
        }
        let v = serde_json::to_value(a.body("http://x", 86_400, HistoryOutput::Include)).unwrap();
        assert_eq!(v["history"][0]["text"], marker);
        // The stored alert is unchanged: only the response omits the key.
        assert!(a.attributes.contains_key("zabbix_ack_sync_intent"));
    }

    #[test]
    fn evidence_is_recognized_case_insensitively() {
        for s in [
            "x [zabalack:v1 action=a kid=k mac=00]",
            "[ZABALACK:",
            "[ackline:v2",
            "[AckLine:",
            "column zabbix_ack_sync_intent",
            "ZABBIX_ACK_SYNC_INTENT",
        ] {
            assert!(contains_evidence(s), "{s}");
            assert_eq!(redact_diagnostic(s.to_owned()), REDACTED_DIAGNOSTIC);
        }
        for s in [
            "zabalack:",
            "[zabalack",
            "zabbix_ack_sync",
            "ackline",
            "database error",
            "",
        ] {
            assert!(!contains_evidence(s), "{s}");
            assert_eq!(redact_diagnostic(s.to_owned()), s);
        }
    }

    #[test]
    fn restricted_attribute_alone_serializes_as_empty_object() {
        let attributes: Map<String, Value> =
            serde_json::from_value(serde_json::json!({"zabbix_ack_sync_intent": "s"})).unwrap();
        let a = alert_with(attributes, "t");
        let v = serde_json::to_value(a.body("http://x", 86_400, HistoryOutput::Omit)).unwrap();
        assert_eq!(v["attributes"], serde_json::json!({}));
    }

    #[test]
    fn heartbeat_expires_inside_the_boundary_second() {
        let t = parse_time("2026-10-13T01:00:00.000Z").unwrap();
        let hb = Heartbeat {
            id: "h".into(),
            origin: "o".into(),
            tags: vec![],
            attributes: Map::new(),
            event_type: "Heartbeat".into(),
            create_time: t,
            timeout: 0,
            receive_time: t,
            customer: None,
        };
        let now = parse_time("2026-10-13T01:00:00.500Z").unwrap();
        let b = hb.body("http://x", 2000, now);
        assert_eq!((b.status, b.since), ("expired", 0));
        assert_eq!(hb.body("http://x", 2000, t).status, "ok");
    }

    #[test]
    fn heartbeat_huge_timeouts_do_not_overflow() {
        let t = parse_time("2026-10-13T01:00:00.000Z").unwrap();
        let now = parse_time("2026-10-14T01:00:00.500Z").unwrap();
        for timeout in [i64::MAX, 9_223_372_036_854_776, 86_400] {
            let hb = Heartbeat {
                id: "h".into(),
                origin: "o".into(),
                tags: vec![],
                attributes: Map::new(),
                event_type: "Heartbeat".into(),
                create_time: t,
                timeout,
                receive_time: t,
                customer: None,
            };
            let b = hb.body("http://x", 2000, now);
            let want = if timeout == 86_400 { "expired" } else { "ok" };
            assert_eq!((b.status, b.since), (want, 86_400), "timeout {timeout}");
        }
    }

    #[test]
    fn key_secret_only_with_secret_and_href_by_id() {
        let t = parse_time("2026-10-13T01:00:00.000Z").unwrap();
        let k = ApiKey {
            id: "pub-id".into(),
            key: "secret".into(),
            user: "u".into(),
            scopes: Some(vec!["read".into()]),
            text: String::new(),
            expire_time: Some(t),
            count: 0,
            last_used_time: None,
            customer: None,
        };
        let listed = serde_json::to_value(k.body("http://x", t, KeySecret::Hide)).unwrap();
        assert!(listed.get("key").is_none());
        assert_eq!(listed["href"], "http://x/key/pub-id");
        let odd = ApiKey {
            id: "a/b?c#d%e é".into(),
            ..k.clone()
        };
        let b = serde_json::to_value(odd.body("http://x", t, KeySecret::Hide)).unwrap();
        assert_eq!(b["href"], "http://x/key/a%2Fb%3Fc%23d%25e%20%C3%A9");
        let created = serde_json::to_value(k.body("http://x", t, KeySecret::Reveal)).unwrap();
        assert_eq!(created["key"], "secret");
    }

    #[test]
    fn null_expiry_and_scopes_list_as_inactive_with_no_scopes() {
        let t = parse_time("2026-10-13T01:00:00.000Z").unwrap();
        let k = ApiKey {
            id: "i".into(),
            key: "s".into(),
            user: "u".into(),
            scopes: None,
            text: String::new(),
            expire_time: None,
            count: 0,
            last_used_time: None,
            customer: None,
        };
        let b = serde_json::to_value(k.body("http://x", t, KeySecret::Hide)).unwrap();
        assert_eq!(b["status"], "inactive");
        assert_eq!(b["expireTime"], Value::Null);
        assert_eq!(b["scopes"], serde_json::json!([]));
        assert_eq!(b["type"], "read-only");
        let future = t + chrono::Duration::days(1);
        let no_scopes = ApiKey {
            expire_time: Some(future),
            ..k.clone()
        };
        assert_eq!(
            no_scopes.body("http://x", t, KeySecret::Hide).status,
            "inactive"
        );
        let no_expiry = ApiKey {
            scopes: Some(vec!["read".into()]),
            ..k.clone()
        };
        assert_eq!(
            no_expiry.body("http://x", t, KeySecret::Hide).status,
            "inactive"
        );
        let active = ApiKey {
            scopes: Some(vec!["read".into()]),
            expire_time: Some(future),
            ..k
        };
        assert_eq!(active.body("http://x", t, KeySecret::Hide).status, "active");
    }

    #[test]
    fn scopes() {
        let s = vec!["write:alerts".to_string()];
        assert!(scope_allows(&s, "read:alerts"));
        assert!(scope_allows(&s, "write:alerts"));
        assert!(!scope_allows(&s, "write:heartbeats"));
        assert!(scope_allows(&["admin".to_string()], "admin:keys"));
        assert!(!scope_allows(&["read".to_string()], "write:alerts"));
    }
}
