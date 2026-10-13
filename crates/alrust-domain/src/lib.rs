//! Alert domain types shared across alrust crates.
//!
//! Field names and JSON shapes follow the reference Alerta v9.1.0 API
//! (`alerta/models/alert.py` `serialize`, `history.py`, `heartbeat.py`,
//! `key.py`), so existing Alerta clients can talk to alrust unchanged.

use chrono::{DateTime, SecondsFormat, Utc};
use serde::{Deserialize, Serialize, Serializer};
use serde_json::{Map, Value};

/// Severity levels of the default `ALERTA` alarm model (`sm.py:7-58`).
/// A lower level is more severe.
/// `id` as one URI path segment for an `href`: every byte outside RFC 3986's
/// unreserved set is percent-encoded, so a caller-supplied id containing `/`,
/// `?`, `#`, `%` or non-ASCII still names exactly one resource. Generated
/// UUIDs are unchanged.
pub fn path_segment(id: &str) -> String {
    let mut out = String::with_capacity(id.len());
    for b in id.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'.' | b'_' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

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

/// History change types (`enums.py` ChangeType).
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

fn ser_time<S: Serializer>(t: &DateTime<Utc>, s: S) -> Result<S::Ok, S::Error> {
    s.serialize_str(&iso8601(t))
}

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
    pub value: Value,
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
    pub value: Value,
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
    pub duplicate_count: i64,
    pub repeat: bool,
    pub previous_severity: String,
    pub trend_indication: String,
    pub receive_time: DateTime<Utc>,
    pub last_receive_id: String,
    pub last_receive_time: DateTime<Utc>,
    pub update_time: DateTime<Utc>,
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
    pub value: &'a Value,
    pub text: &'a str,
    pub tags: &'a [String],
    pub attributes: &'a Map<String, Value>,
    pub origin: &'a str,
    #[serde(rename = "type")]
    pub event_type: &'a str,
    #[serde(serialize_with = "ser_time")]
    pub create_time: DateTime<Utc>,
    pub timeout: i64,
    pub raw_data: Option<&'a str>,
    pub customer: Option<&'a str>,
    pub duplicate_count: i64,
    pub repeat: bool,
    pub previous_severity: &'a str,
    pub trend_indication: &'a str,
    #[serde(serialize_with = "ser_time")]
    pub receive_time: DateTime<Utc>,
    pub last_receive_id: &'a str,
    #[serde(serialize_with = "ser_time")]
    pub last_receive_time: DateTime<Utc>,
    #[serde(serialize_with = "ser_time")]
    pub update_time: DateTime<Utc>,
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
    pub value: &'a Value,
    pub text: Option<&'a str>,
    #[serde(rename = "type")]
    pub change_type: &'a str,
    #[serde(serialize_with = "ser_time")]
    pub update_time: DateTime<Utc>,
    pub user: Option<&'a str>,
    pub timeout: Option<i64>,
}

impl Alert {
    /// Serialize like `Alert.serialize`: history sorted ascending by
    /// `updateTime`, stable, so equal times keep storage order (spec R7.3b).
    pub fn body<'a>(
        &'a self,
        base_url: &str,
        alert_timeout: i64,
        with_history: bool,
    ) -> AlertBody<'a> {
        let history = if with_history {
            let mut h: Vec<&HistoryEntry> = self.history.iter().collect();
            h.sort_by_key(|e| e.update_time);
            h.into_iter()
                .map(|e| HistoryBody {
                    id: &e.id,
                    href: format!("{base_url}/alert/{}", path_segment(&e.id)),
                    event: &e.event,
                    severity: e.severity.as_deref(),
                    status: e.status.as_deref(),
                    value: &e.value,
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
            value: &self.value,
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
            previous_severity: &self.previous_severity,
            trend_indication: &self.trend_indication,
            receive_time: self.receive_time,
            last_receive_id: &self.last_receive_id,
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
            .map(i128::from)
            .unwrap_or_else(|| i128::from(elapsed.num_seconds()) * 1_000_000);
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
    pub scopes: Vec<String>,
    pub text: String,
    pub expire_time: DateTime<Utc>,
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
    #[serde(serialize_with = "ser_time")]
    pub expire_time: DateTime<Utc>,
    pub count: i64,
    #[serde(serialize_with = "ser_opt_time")]
    pub last_used_time: Option<DateTime<Utc>>,
    pub customer: Option<&'a str>,
}

impl ApiKey {
    /// Serialize a key. `with_secret` is true only for the creation response.
    pub fn body<'a>(
        &'a self,
        base_url: &str,
        now: DateTime<Utc>,
        with_secret: bool,
    ) -> ApiKeyBody<'a> {
        let key_type = if self
            .scopes
            .iter()
            .any(|s| s.starts_with("write") || s.starts_with("admin"))
        {
            "read-write"
        } else {
            "read-only"
        };
        ApiKeyBody {
            id: &self.id,
            key: with_secret.then_some(self.key.as_str()),
            status: if self.expire_time < now {
                "expired"
            } else {
                "active"
            },
            href: format!("{base_url}/key/{}", path_segment(&self.id)),
            user: &self.user,
            scopes: &self.scopes,
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
        scope_allows(&self.scopes, wanted)
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
            ("admin", _) | ("write", "write") | ("write", "read") | ("read", "read")
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
            scopes: vec!["read".into()],
            text: String::new(),
            expire_time: t,
            count: 0,
            last_used_time: None,
            customer: None,
        };
        let listed = serde_json::to_value(k.body("http://x", t, false)).unwrap();
        assert!(listed.get("key").is_none());
        assert_eq!(listed["href"], "http://x/key/pub-id");
        let odd = ApiKey {
            id: "a/b?c#d%e é".into(),
            ..k.clone()
        };
        let b = serde_json::to_value(odd.body("http://x", t, false)).unwrap();
        assert_eq!(b["href"], "http://x/key/a%2Fb%3Fc%23d%25e%20%C3%A9");
        let created = serde_json::to_value(k.body("http://x", t, true)).unwrap();
        assert_eq!(created["key"], "secret");
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
