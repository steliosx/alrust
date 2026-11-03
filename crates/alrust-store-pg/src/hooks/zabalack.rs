//! Zabalack's receive-time path, ported for its future adapter
//! (`spec/integration-hooks.md` 4.3 and 4.3.1; `zab-al-ack@eaf2acf`
//! `src/zabalack/normalizer.py`, `policy.py`, `config.py`). Pure functions:
//! no file, environment, key, clock or database. Nothing registers them yet.

use std::collections::HashSet;

use chrono::{DateTime, Utc};
use serde_json::{Map, Value};

use alrust_domain::Alert;

use super::{CallerFacts, HookError};
use crate::NewAlert;

/// Case-sensitive key prefixes (`normalizer.py:16`); `zabbix_ack_sync`
/// has no trailing underscore.
pub const RESERVED_PREFIXES: &[&str] = &["zabbix_ack_sync", "zabalack_", "ackline_"];

/// Exact keys (`normalizer.py:17`).
pub const RESERVED_FIELDS: &[&str] = &[
    "zabbix_ack_intent",
    "zabbix_ack_proof",
    "ack_intent",
    "ack_proof",
];

/// Removes reserved top-level keys; other keys and every value,
/// including nested ones, are unchanged (`strip_reserved`,
/// `normalizer.py:34-36`).
pub fn strip_reserved(attributes: &mut Map<String, Value>) {
    attributes.retain(|k, _| {
        !RESERVED_FIELDS.contains(&k.as_str())
            && !RESERVED_PREFIXES.iter().any(|p| k.starts_with(p))
    });
}

/// The settings fields the receive path (4.3.1) and the actor policy (4.1.1)
/// use (`config.py:15-62`), built only through [`Policy::new`], which applies
/// the pinned checks for them.
/// This is the subset of `validate_settings` these fields need, not full
/// configuration validation, and it has not passed Zabalack's own loader.
#[derive(Debug, Clone)]
pub struct Policy {
    capture: Capture,
    bypass: Bypass,
    config_revision: String,
    /// Login to (customer, instance).
    mappings: Vec<(String, String, String)>,
    admin_exceptions: Vec<String>,
    cohorts: Vec<Cohort>,
}

/// Zabalack's `settings.enabled`: capture enablement, not registration.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Capture {
    Enabled,
    Disabled,
}

/// Zabalack's `capture_bypass`, with its change reference.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Bypass {
    Off,
    On { change_ref: String },
}

/// The receive policy's raw inputs, as a configuration would hold them.
#[derive(Debug, Clone)]
pub struct PolicyInput {
    pub capture: Capture,
    pub bypass: Bypass,
    pub config_revision: String,
    /// `ingestion_mappings`: login to `{"customer": .., "instance": ..}`.
    pub ingestion_mappings: Map<String, Value>,
    /// `admin_exceptions`: admin logins that may still be actors.
    pub admin_exceptions: Vec<String>,
    /// `cohorts`, each a JSON object.
    pub cohorts: Vec<Value>,
}

/// One validated cohort (`policy.py:63-85`), as `select_cohort` returns it.
#[derive(Debug, Clone)]
pub struct Cohort {
    customer: String,
    instance: String,
    trigger: String,
    enabled: bool,
    one_to_one: bool,
    allow_admin: bool,
}

/// A receive policy refused when built, with Zabalack's reason string.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PolicyBuildError(pub &'static str);

impl Policy {
    /// # Errors
    ///
    /// `PolicyBuildError` with the pinned reason: a bypass without a change
    /// reference (`config.py:145-146`), an invalid ingestion mapping
    /// (`153-157`), an empty admin exception (`158-159`), an invalid cohort
    /// (`policy.py:63-85`) or a duplicate one (`config.py:162-168`).
    pub fn new(input: PolicyInput) -> Result<Self, PolicyBuildError> {
        if matches!(&input.bypass, Bypass::On { change_ref } if change_ref.is_empty()) {
            return Err(PolicyBuildError("bypass_requires_change_reference"));
        }
        let mut mappings = Vec::new();
        for (login, mapping) in &input.ingestion_mappings {
            let Some(m) = mapping.as_object() else {
                return Err(PolicyBuildError("ingestion_mapping_invalid"));
            };
            let text = |k: &str| m.get(k).and_then(Value::as_str).filter(|s| !s.is_empty());
            match (
                login.is_empty(),
                m.len(),
                text("customer"),
                text("instance"),
            ) {
                (false, 2, Some(c), Some(i)) => {
                    mappings.push((login.clone(), c.to_owned(), i.to_owned()));
                }
                _ => return Err(PolicyBuildError("ingestion_mapping_invalid")),
            }
        }
        if input.admin_exceptions.iter().any(String::is_empty) {
            return Err(PolicyBuildError("admin_exception_invalid"));
        }
        let mut cohorts = Vec::new();
        let mut seen = HashSet::new();
        for c in &input.cohorts {
            let cohort = validate_cohort(c)?;
            let ident = (
                cohort.customer.clone(),
                cohort.instance.clone(),
                cohort.trigger.clone(),
            );
            if !seen.insert(ident) {
                return Err(PolicyBuildError("duplicate_cohort"));
            }
            cohorts.push(cohort);
        }
        Ok(Policy {
            capture: input.capture,
            bypass: input.bypass,
            config_revision: input.config_revision,
            mappings,
            admin_exceptions: input.admin_exceptions,
            cohorts,
        })
    }

    /// `select_cohort` (`policy.py:87-94`): the first enabled cohort for this
    /// customer, instance and trigger; none for an empty customer.
    pub fn select_cohort(&self, customer: &str, instance: &str, trigger: &str) -> Option<&Cohort> {
        if customer.is_empty() {
            return None;
        }
        self.cohorts.iter().find(|c| {
            c.customer == customer && c.instance == instance && c.trigger == trigger && c.enabled
        })
    }

    fn mapping(&self, login: Option<&str>) -> Option<(&str, &str)> {
        let login = login?;
        self.mappings
            .iter()
            .find(|(l, _, _)| l == login)
            .map(|(_, c, i)| (c.as_str(), i.as_str()))
    }
}

const COHORT_REQUIRED: &[&str] = &[
    "customer",
    "zabbix_instance",
    "zabbix_trigger_id",
    "host_ids",
    "group_ids",
    "revision",
    "policy_revision",
    "enabled",
    "one_to_one",
    "comment_enabled",
    "allow_admin",
];
const COHORT_OPTIONAL: &[&str] = &["host_groups", "host_names", "change_ref", "host_drift"];

/// `validate_cohort` (`policy.py:63-85`).
fn validate_cohort(v: &Value) -> Result<Cohort, PolicyBuildError> {
    let Some(c) = v.as_object() else {
        return Err(PolicyBuildError("cohort_shape"));
    };
    if COHORT_REQUIRED.iter().any(|k| !c.contains_key(*k))
        || c.keys().any(|k| {
            !COHORT_REQUIRED.contains(&k.as_str()) && !COHORT_OPTIONAL.contains(&k.as_str())
        })
    {
        return Err(PolicyBuildError("cohort_shape"));
    }
    for k in ["customer", "revision", "policy_revision"] {
        let ok = c[k]
            .as_str()
            .is_some_and(|s| (1..=128).contains(&s.chars().count()));
        if !ok {
            return Err(PolicyBuildError("cohort_identity"));
        }
    }
    let instance = c["zabbix_instance"].as_str().filter(|s| instance_ok(s));
    let Some(instance) = instance else {
        return Err(PolicyBuildError("cohort_instance"));
    };
    let Some(trigger) = numeric_id(&c["zabbix_trigger_id"]) else {
        return Err(PolicyBuildError("invalid_source_id"));
    };
    let flag = |k: &str| c[k].as_bool();
    let (Some(enabled), Some(one_to_one), Some(_), Some(allow_admin)) = (
        flag("enabled"),
        flag("one_to_one"),
        flag("comment_enabled"),
        flag("allow_admin"),
    ) else {
        return Err(PolicyBuildError("cohort_boolean"));
    };
    if c.get("host_drift").is_some_and(|d| !d.is_boolean()) {
        return Err(PolicyBuildError("cohort_drift_boolean"));
    }
    for k in ["host_ids", "group_ids"] {
        let Some(values) = c[k].as_array() else {
            return Err(PolicyBuildError("cohort_host_group_bound"));
        };
        let distinct: HashSet<String> = values.iter().map(Value::to_string).collect();
        if !(1..=100).contains(&values.len()) || distinct.len() != values.len() {
            return Err(PolicyBuildError("cohort_host_group_bound"));
        }
        if values.iter().any(|x| numeric_id(x).is_none()) {
            return Err(PolicyBuildError("invalid_source_id"));
        }
    }
    Ok(Cohort {
        customer: c["customer"].as_str().unwrap_or_default().to_owned(),
        instance: instance.to_owned(),
        trigger: trigger.to_owned(),
        enabled,
        one_to_one,
        allow_admin,
    })
}

/// `INSTANCE_RE` (`policy.py:28`): `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`.
fn instance_ok(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty()
        && b.len() <= 128
        && b[0].is_ascii_alphanumeric()
        && b[1..]
            .iter()
            .all(|&c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'.' | b':' | b'-'))
}

/// `numeric_id` without the nullable form (`policy.py:33-38`): a string of
/// `[1-9][0-9]{0,19}` whose value fits in 64 unsigned bits.
fn numeric_id(v: &Value) -> Option<&str> {
    let s = v.as_str()?;
    let b = s.as_bytes();
    let shape = (1..=20).contains(&b.len())
        && matches!(b[0], b'1'..=b'9')
        && b.iter().all(u8::is_ascii_digit);
    (shape && s.parse::<u64>().is_ok()).then_some(s)
}

/// Python's zero sentinels for a nullable id (`policy.py:34`):
/// `value in (None, "", "0", 0)`, where `0` also equals `False` and any
/// number equal to zero.
fn zero_sentinel(v: &Value) -> bool {
    match v {
        Value::Null => true,
        Value::String(s) => s.is_empty() || s == "0",
        Value::Bool(b) => !b,
        Value::Number(n) => n.as_f64() == Some(0.0),
        Value::Array(_) | Value::Object(_) => false,
    }
}

/// Python truthiness of a JSON value, for `not alert.attributes.get(key)`.
fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

const SOURCE_CONTRACT_KEYS: &[&str] = &[
    "source_system",
    "zabbix_instance",
    "zabbix_event_id",
    "zabbix_trigger_id",
    "zabbix_cause_event_id",
    "zabbix_recovery_event_id",
    "source_contract_version",
];

/// `validate_source` (`policy.py:49-61`): the normalised cause and recovery
/// ids, or `None` for any failure (all of which the normaliser treats as
/// `legacy_missing_mapping`).
fn validate_source(a: &Map<String, Value>) -> Option<(Option<String>, Option<String>)> {
    if SOURCE_CONTRACT_KEYS.iter().any(|k| !a.contains_key(*k)) {
        return None;
    }
    if a["source_system"].as_str() != Some("zabbix")
        || a["source_contract_version"].as_str() != Some("1")
    {
        return None;
    }
    if !a["zabbix_instance"].as_str().is_some_and(instance_ok) {
        return None;
    }
    let event = numeric_id(&a["zabbix_event_id"])?;
    numeric_id(&a["zabbix_trigger_id"])?;
    let nullable = |k: &str| -> Option<Option<String>> {
        let v = &a[k];
        if zero_sentinel(v) {
            Some(None)
        } else {
            numeric_id(v).map(|s| Some(s.to_owned()))
        }
    };
    let cause = nullable("zabbix_cause_event_id")?;
    let recovery = nullable("zabbix_recovery_event_id")?;
    if recovery.as_deref() == Some(event) {
        return None;
    }
    Some((cause, recovery))
}

/// `project_skip` (`normalizer.py:39-47`), with the injected `now`.
fn project_skip(a: &mut Map<String, Value>, reason: &str, policy: &Policy, now: DateTime<Utc>) {
    a.remove("zabbix_ack_sync_intent");
    a.insert("zabbix_ack_sync".into(), Value::String("skipped".into()));
    a.insert(
        "zabbix_ack_sync_reason".into(),
        Value::String(reason.into()),
    );
    a.insert(
        "zabbix_ack_sync_config_revision".into(),
        Value::String(policy.config_revision.clone()),
    );
    a.insert(
        "zabbix_ack_sync_local_at".into(),
        Value::String(now.format("%Y-%m-%dT%H:%M:%S%.6f+00:00").to_string()),
    );
}

fn reject(reason: &str) -> HookError {
    HookError::new(403, reason)
}

/// The normaliser's `pre_receive` (`normalizer.py:57-100`), as spec 4.3.1
/// orders it. `Ok` means the receive proceeds, possibly with a skip record;
/// `Err` is a 403 refusal with Zabalack's reason string.
///
/// # Errors
///
/// A `HookError` with status 403 for each pinned refusal.
pub fn pre_receive(
    policy: &Policy,
    caller: &CallerFacts,
    incoming: &mut NewAlert,
    now: DateTime<Utc>,
) -> Result<(), HookError> {
    let a = &mut incoming.attributes;
    strip_reserved(a);
    if a.get("source_system").and_then(Value::as_str) != Some("zabbix") {
        return Ok(());
    }
    if policy.capture == Capture::Disabled {
        project_skip(a, "skipped_disabled", policy, now);
        return Ok(());
    }
    if !caller.from_request {
        return Err(reject("zabbix_ingestion_requires_authenticated_context"));
    }
    if !caller.auth_required {
        return Err(reject("zabbix_ingestion_tenant_gate"));
    }
    let admin = caller
        .scopes
        .iter()
        .any(|s| s == "admin" || s == "admin:alerts");
    let Some((map_customer, map_instance)) = policy.mapping(caller.login.as_deref()) else {
        return Err(reject("zabbix_ingestion_identity_policy"));
    };
    if caller.user_id.is_some() || admin {
        return Err(reject("zabbix_ingestion_identity_policy"));
    }
    let customer = incoming.customer.as_deref().unwrap_or("");
    let bound = caller
        .customers
        .as_deref()
        .unwrap_or(&[])
        .iter()
        .any(|c| c == customer);
    if customer.is_empty() || customer != map_customer || !bound {
        return Err(reject("zabbix_ingestion_customer_mismatch"));
    }
    let a = &mut incoming.attributes;
    let required = [
        "source_contract_version",
        "zabbix_instance",
        "zabbix_event_id",
        "zabbix_trigger_id",
    ];
    if required.iter().any(|k| !truthy(a.get(*k))) {
        project_skip(a, "legacy_missing_mapping", policy, now);
        return Ok(());
    }
    if a.get("zabbix_instance").and_then(Value::as_str) != Some(map_instance) {
        return Err(reject("zabbix_ingestion_instance_mismatch"));
    }
    let Some((cause, recovery)) = validate_source(a) else {
        project_skip(a, "legacy_missing_mapping", policy, now);
        return Ok(());
    };
    a.insert(
        "zabbix_cause_event_id".into(),
        cause.map_or(Value::Null, Value::String),
    );
    a.insert(
        "zabbix_recovery_event_id".into(),
        recovery.map_or(Value::Null, Value::String),
    );
    let instance = a
        .get("zabbix_instance")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let trigger = a
        .get("zabbix_trigger_id")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let Some(cohort) = policy.select_cohort(customer, instance, trigger) else {
        project_skip(a, "skipped_cohort_policy", policy, now);
        return Ok(());
    };
    let ambiguous = ["zabbix_event_ids", "zabbix_trigger_ids", "source_members"]
        .iter()
        .any(|k| a.contains_key(*k));
    if !cohort.one_to_one || ambiguous {
        project_skip(a, "skipped_ambiguous_source", policy, now);
        return Ok(());
    }
    if let Bypass::On { change_ref } = &policy.bypass {
        project_skip(a, "skipped_capture_bypass", policy, now);
        a.insert(
            "zabbix_ack_sync_change_reference".into(),
            Value::String(change_ref.clone()),
        );
    }
    Ok(())
}

/// A human actor (`_human_actor`, `plugins.py:269-281`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Actor {
    pub login: String,
    /// Sorted and de-duplicated. `String` ordering is byte order, which for
    /// UTF-8 is Python's code-point order.
    pub customers: Vec<String>,
    pub admin: bool,
}

/// `_human_actor` (`plugins.py:269-281`), from `CallerFacts` only (spec
/// 4.1.1). The facts must be upstream-verified human facts: populated fields
/// and `from_request` are not credential verification. Today the API gives
/// every caller no user id, so this returns `None` for every current caller.
pub fn human_actor(policy: &Policy, caller: &CallerFacts) -> Option<Actor> {
    if !caller.from_request || present(caller.user_id.as_deref()).is_none() {
        return None;
    }
    let login = present(caller.login.as_deref())?;
    if policy.mapping(Some(login)).is_some() {
        return None;
    }
    let admin = caller
        .scopes
        .iter()
        .any(|s| s == "admin" || s == "admin:alerts");
    if admin && !policy.admin_exceptions.iter().any(|e| e == login) {
        return None;
    }
    let mut customers = caller.customers.clone()?;
    customers.sort();
    customers.dedup();
    Some(Actor {
        login: login.to_owned(),
        customers,
        admin,
    })
}

/// A value present and non-empty, as Python's truthiness reads a string.
fn present(v: Option<&str>) -> Option<&str> {
    v.filter(|s| !s.is_empty())
}

/// `authorize_actor` (`policy.py:96-101`): an admin by the cohort's
/// `allow_admin` alone, anyone else by membership of the alert's customer.
pub fn authorize_actor(actor: &Actor, customer: &str, cohort: &Cohort) -> bool {
    if customer.is_empty() || actor.login.is_empty() {
        return false;
    }
    if actor.admin {
        cohort.allow_admin
    } else {
        actor.customers.iter().any(|c| c == customer)
    }
}

/// The source identity an eligible alert carries (`make_snapshot`,
/// `policy.py:40-47`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SourceSnapshot {
    pub instance: String,
    pub event_id: String,
    pub trigger_id: String,
}

/// The source and cohort prechecks' outcome (spec 4.1.2).
#[derive(Debug, Clone)]
pub enum Precheck<'p> {
    /// Eligible: the snapshot and the selected cohort, for `actor_gate`.
    Eligible {
        source: SourceSnapshot,
        cohort: &'p Cohort,
    },
    /// Skipped: these attribute writes, carried as `Gate::Skip`.
    Skip(Map<String, Value>),
}

/// Python's `str(UUID(id)) == id` (`plugins.py:439-440`): 32 lowercase
/// hexadecimal digits grouped 8-4-4-4-12 with hyphens.
fn canonical_uuid(id: &str) -> bool {
    let b = id.as_bytes();
    b.len() == 36
        && b.iter().enumerate().all(|(i, c)| match i {
            8 | 13 | 18 | 23 => *c == b'-',
            _ => c.is_ascii_digit() || (b'a'..=b'f').contains(c),
        })
}

/// The acknowledgement gate's source and cohort prechecks
/// (`plugins.py:438-451`, spec 4.1.2), in order: canonical id and source
/// contract, cohort, ambiguity. Reads the stored alert only; writes nothing
/// back on success. Every failure is a skip, never a refusal.
pub fn ack_prechecks<'p>(policy: &'p Policy, alert: &Alert, now: DateTime<Utc>) -> Precheck<'p> {
    let a = &alert.attributes;
    if !canonical_uuid(&alert.id) || validate_source(a).is_none() {
        return Precheck::Skip(skip_writes("legacy_missing_mapping", policy, now));
    }
    let text = |k: &str| {
        a.get(k)
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned()
    };
    let source = SourceSnapshot {
        instance: text("zabbix_instance"),
        event_id: text("zabbix_event_id"),
        trigger_id: text("zabbix_trigger_id"),
    };
    let customer = alert.customer.as_deref().unwrap_or("");
    let Some(cohort) = policy.select_cohort(customer, &source.instance, &source.trigger_id) else {
        return Precheck::Skip(skip_writes("skipped_cohort_policy", policy, now));
    };
    let ambiguous = ["zabbix_event_ids", "zabbix_trigger_ids", "source_members"]
        .iter()
        .any(|k| a.contains_key(*k));
    if !cohort.one_to_one || ambiguous {
        return Precheck::Skip(skip_writes("skipped_ambiguous_source", policy, now));
    }
    Precheck::Eligible { source, cohort }
}

/// The actor checks' outcome for the adapter's `gate` (`plugins.py:452-454`).
#[derive(Debug, Clone, PartialEq)]
pub enum ActorOutcome {
    Authorized(Actor),
    /// `skipped_actor_policy`: these attribute writes (a null removes the
    /// key), carried as `Gate::Skip`; the acknowledgement proceeds.
    Skip(Map<String, Value>),
}

/// Human-actor extraction then authorisation, for the cohort the caller
/// already selected (spec 4.1.1). Never a refusal.
pub fn actor_gate(
    policy: &Policy,
    caller: &CallerFacts,
    customer: &str,
    cohort: &Cohort,
    now: DateTime<Utc>,
) -> ActorOutcome {
    match human_actor(policy, caller) {
        Some(actor) if authorize_actor(&actor, customer, cohort) => ActorOutcome::Authorized(actor),
        _ => ActorOutcome::Skip(skip_writes("skipped_actor_policy", policy, now)),
    }
}

/// `project_skip`'s effect as attribute writes: the intent removed (null),
/// then the four record keys.
fn skip_writes(reason: &str, policy: &Policy, now: DateTime<Utc>) -> Map<String, Value> {
    let mut m = Map::new();
    m.insert("zabbix_ack_sync_intent".into(), Value::Null);
    project_skip(&mut m, reason, policy, now);
    m.insert("zabbix_ack_sync_intent".into(), Value::Null);
    m
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn stripped(v: Value) -> Value {
        let Value::Object(mut m) = v else {
            panic!("an object")
        };
        strip_reserved(&mut m);
        Value::Object(m)
    }

    // Codex's prepared cases (44d0cef0) for the pinned normaliser.
    #[test]
    fn reserved_keys_go_and_everything_else_stays() {
        assert_eq!(
            stripped(
                json!({"zabbix_ack_syncfoo": 1, "zabalack_any": 2, "ackline_legacy": 3,
                             "ack_intent": 4, "safe": {"keep": 1}})
            ),
            json!({"safe": {"keep": 1}})
        );
        // Case variants, near lookalikes, suffixed exact fields and
        // nested keys are not reserved.
        let kept = json!({"ZABBIX_ACK_SYNC_intent": 1, "Zabalack_x": 2, "zabalack": 3,
                          "ackline": 4, "zabbix_ack_proofsuffix": 5,
                          "ordinary": {"ack_intent": 6}});
        assert_eq!(stripped(kept.clone()), kept);
        assert_eq!(
            stripped(json!({"source_system": "other", "ackline_legacy": "x",
                             "safe": [1, null, {"zabalack_nested": 2}]})),
            json!({"source_system": "other", "safe": [1, null, {"zabalack_nested": 2}]})
        );
        // Every exact field and every prefix.
        assert_eq!(
            stripped(
                json!({"zabbix_ack_intent": 1, "zabbix_ack_proof": 2, "ack_proof": 3,
                             "zabbix_ack_sync": 4, "zabbix_ack_sync_intent": 5, "z": 6})
            ),
            json!({"z": 6})
        );
    }
}

#[cfg(test)]
#[expect(
    clippy::many_single_char_names,
    reason = "short names for the input, policy, result and attributes of each case"
)]
mod policy_tests {
    use chrono::TimeZone as _;
    use serde_json::json;

    use super::*;

    fn now() -> DateTime<Utc> {
        Utc.with_ymd_and_hms(2026, 11, 3, 1, 52, 32).unwrap()
            + chrono::Duration::microseconds(123_456)
    }

    const LOCAL_AT: &str = "2026-11-03T01:52:32.123456+00:00";

    fn cohort() -> Value {
        json!({
            "customer": "acme", "zabbix_instance": "zbx-1", "zabbix_trigger_id": "77",
            "host_ids": ["1"], "group_ids": ["2"], "revision": "r1", "policy_revision": "1",
            "enabled": true, "one_to_one": true, "comment_enabled": false, "allow_admin": false
        })
    }

    fn input() -> PolicyInput {
        PolicyInput {
            capture: Capture::Enabled,
            bypass: Bypass::Off,
            config_revision: "cfg-7".into(),
            ingestion_mappings: json!({"zbx-ingest": {"customer": "acme", "instance": "zbx-1"}})
                .as_object()
                .unwrap()
                .clone(),
            admin_exceptions: vec![],
            cohorts: vec![cohort()],
        }
    }

    fn policy() -> Policy {
        Policy::new(input()).unwrap()
    }

    fn caller() -> CallerFacts {
        CallerFacts {
            login: Some("zbx-ingest".into()),
            scopes: vec!["write:alerts".into()],
            customers: Some(vec!["acme".into()]),
            auth_required: true,
            from_request: true,
            ..CallerFacts::default()
        }
    }

    fn source() -> Map<String, Value> {
        json!({
            "source_system": "zabbix", "source_contract_version": "1",
            "zabbix_instance": "zbx-1", "zabbix_event_id": "1001", "zabbix_trigger_id": "77",
            "zabbix_cause_event_id": "", "zabbix_recovery_event_id": "0",
            "region": "eu"
        })
        .as_object()
        .unwrap()
        .clone()
    }

    fn alert(attributes: Map<String, Value>) -> NewAlert {
        NewAlert {
            id: "a".into(),
            resource: "r".into(),
            event: "e".into(),
            environment: "Production".into(),
            severity: "major".into(),
            correlate: vec![],
            status: "open".into(),
            service: vec!["Web".into()],
            group: "Misc".into(),
            value: None,
            text: String::new(),
            tags: vec![],
            attributes,
            origin: "zbx".into(),
            event_type: "exceptionAlert".into(),
            create_time: now(),
            timeout: 86_400,
            raw_data: None,
            customer: Some("acme".into()),
        }
    }

    /// Runs the policy on these attributes; the result and the attributes after.
    fn run_with(
        p: &Policy,
        c: &CallerFacts,
        attributes: Map<String, Value>,
        customer: Option<&str>,
    ) -> (Result<(), HookError>, Map<String, Value>) {
        let mut n = alert(attributes);
        n.customer = customer.map(str::to_owned);
        let r = pre_receive(p, c, &mut n, now());
        (r, n.attributes)
    }

    fn run(attributes: Map<String, Value>) -> (Result<(), HookError>, Map<String, Value>) {
        run_with(&policy(), &caller(), attributes, Some("acme"))
    }

    fn with(mut m: Map<String, Value>, k: &str, v: Value) -> Map<String, Value> {
        m.insert(k.into(), v);
        m
    }

    fn without(mut m: Map<String, Value>, k: &str) -> Map<String, Value> {
        m.remove(k);
        m
    }

    fn refused(r: &Result<(), HookError>, reason: &str) -> bool {
        matches!(r, Err(e) if e.status == 403 && e.message == reason)
    }

    /// The skip record for `reason`, exactly as `project_skip` writes it.
    fn skipped(a: &Map<String, Value>, reason: &str) -> bool {
        a.get("zabbix_ack_sync") == Some(&json!("skipped"))
            && a.get("zabbix_ack_sync_reason") == Some(&json!(reason))
            && a.get("zabbix_ack_sync_config_revision") == Some(&json!("cfg-7"))
            && a.get("zabbix_ack_sync_local_at") == Some(&json!(LOCAL_AT))
            && !a.contains_key("zabbix_ack_sync_intent")
    }

    fn no_record(a: &Map<String, Value>) -> bool {
        !a.keys().any(|k| k.starts_with("zabbix_ack_sync"))
    }

    #[test]
    fn a_passing_alert_is_normalised_with_no_skip_record() {
        let (r, a) = run(source());
        assert!(r.is_ok(), "{r:?}");
        assert!(no_record(&a), "{a:?}");
        assert_eq!(a["zabbix_cause_event_id"], Value::Null);
        assert_eq!(a["zabbix_recovery_event_id"], Value::Null);
        assert_eq!(a["region"], "eu");
        let (_, a) = run(with(
            with(source(), "zabbix_cause_event_id", json!("5")),
            "zabbix_recovery_event_id",
            json!("6"),
        ));
        assert_eq!(
            (
                a["zabbix_cause_event_id"].clone(),
                a["zabbix_recovery_event_id"].clone()
            ),
            (json!("5"), json!("6"))
        );
    }

    #[test]
    fn non_zabbix_alerts_are_only_stripped() {
        let mut m = without(source(), "source_system");
        m.insert("zabalack_forged".into(), json!(1));
        let (r, a) = run(m.clone());
        assert!(r.is_ok());
        m.remove("zabalack_forged");
        assert_eq!(a, m);
        // Even with every caller fact missing: the source check comes first.
        let (r, _) = run_with(
            &policy(),
            &CallerFacts::default(),
            with(source(), "source_system", json!("nagios")),
            None,
        );
        assert!(r.is_ok());
    }

    #[test]
    fn capture_disabled_strips_and_records_the_skip() {
        let mut i = input();
        i.capture = Capture::Disabled;
        let p = Policy::new(i).unwrap();
        let m = with(
            with(source(), "zabbix_ack_sync_intent", json!({"forged": 1})),
            "zabalack_x",
            json!(2),
        );
        // Before any caller check: even facts not from a request.
        let (r, a) = run_with(&p, &CallerFacts::default(), m, None);
        assert!(r.is_ok());
        assert!(skipped(&a, "skipped_disabled"), "{a:?}");
        assert!(!a.contains_key("zabalack_x"));
        assert_eq!(
            a.keys()
                .filter(|k| k.starts_with("zabbix_ack_sync"))
                .count(),
            4
        );
    }

    #[test]
    fn each_refusal_is_a_403_with_zabalacks_reason() {
        let p = policy();
        let mut c = caller();
        c.from_request = false;
        assert!(refused(
            &run_with(&p, &c, source(), Some("acme")).0,
            "zabbix_ingestion_requires_authenticated_context"
        ));
        let mut c = caller();
        c.auth_required = false;
        assert!(refused(
            &run_with(&p, &c, source(), Some("acme")).0,
            "zabbix_ingestion_tenant_gate"
        ));
        for c in [
            CallerFacts {
                login: Some("other".into()),
                ..caller()
            },
            CallerFacts {
                login: None,
                ..caller()
            },
            CallerFacts {
                user_id: Some("u1".into()),
                ..caller()
            },
            CallerFacts {
                scopes: vec!["admin".into()],
                ..caller()
            },
            CallerFacts {
                scopes: vec!["write:alerts".into(), "admin:alerts".into()],
                ..caller()
            },
        ] {
            assert!(
                refused(
                    &run_with(&p, &c, source(), Some("acme")).0,
                    "zabbix_ingestion_identity_policy"
                ),
                "{c:?}"
            );
        }
        for (c, customer) in [
            (caller(), None),
            (caller(), Some("")),
            (caller(), Some("other")),
            (
                CallerFacts {
                    customers: None,
                    ..caller()
                },
                Some("acme"),
            ),
            (
                CallerFacts {
                    customers: Some(vec!["other".into()]),
                    ..caller()
                },
                Some("acme"),
            ),
        ] {
            assert!(
                refused(
                    &run_with(&p, &c, source(), customer).0,
                    "zabbix_ingestion_customer_mismatch"
                ),
                "{c:?} {customer:?}"
            );
        }
        assert!(refused(
            &run(with(source(), "zabbix_instance", json!("zbx-2"))).0,
            "zabbix_ingestion_instance_mismatch"
        ));
    }

    #[test]
    fn a_falsy_required_key_is_a_legacy_skip() {
        for k in [
            "source_contract_version",
            "zabbix_instance",
            "zabbix_event_id",
            "zabbix_trigger_id",
        ] {
            for v in [
                Value::Null,
                json!(""),
                json!(0),
                json!(false),
                json!([]),
                json!({}),
            ] {
                let (r, a) = run(with(source(), k, v.clone()));
                assert!(
                    r.is_ok() && skipped(&a, "legacy_missing_mapping"),
                    "{k}={v}"
                );
            }
            let (r, a) = run(without(source(), k));
            assert!(
                r.is_ok() && skipped(&a, "legacy_missing_mapping"),
                "{k} missing"
            );
        }
    }

    #[test]
    fn source_contract_failures_are_a_legacy_skip() {
        for m in [
            without(source(), "zabbix_cause_event_id"),
            without(source(), "zabbix_recovery_event_id"),
            with(source(), "source_contract_version", json!(1)),
            with(source(), "source_contract_version", json!("2")),
            with(source(), "zabbix_event_id", json!(1001)),
            with(source(), "zabbix_event_id", json!("0123")),
            with(source(), "zabbix_event_id", json!("18446744073709551616")),
            with(source(), "zabbix_trigger_id", json!("x")),
            with(source(), "zabbix_recovery_event_id", json!("1001")),
            with(source(), "zabbix_cause_event_id", json!(true)),
            with(source(), "zabbix_cause_event_id", json!("0.0")),
            with(source(), "zabbix_cause_event_id", json!(5)),
        ] {
            let (r, a) = run(m.clone());
            assert!(r.is_ok() && skipped(&a, "legacy_missing_mapping"), "{m:?}");
        }
        // The largest 64-bit id passes.
        let (r, a) = run(with(
            source(),
            "zabbix_event_id",
            json!("18446744073709551615"),
        ));
        assert!(r.is_ok() && no_record(&a));
    }

    #[test]
    fn present_zero_sentinels_become_null() {
        for v in [
            Value::Null,
            json!(""),
            json!("0"),
            json!(0),
            json!(0.0),
            json!(-0.0),
            json!(false),
        ] {
            let (r, a) = run(with(source(), "zabbix_cause_event_id", v.clone()));
            assert!(r.is_ok() && no_record(&a), "{v}");
            assert_eq!(a["zabbix_cause_event_id"], Value::Null, "{v}");
        }
    }

    #[test]
    fn cohort_ambiguity_and_bypass_skips() {
        let mut i = input();
        i.cohorts = vec![with_v(cohort(), "enabled", json!(false))];
        let (r, a) = run_with(&Policy::new(i).unwrap(), &caller(), source(), Some("acme"));
        assert!(r.is_ok() && skipped(&a, "skipped_cohort_policy"));
        let (r, a) = run(with(source(), "zabbix_trigger_id", json!("78")));
        assert!(r.is_ok() && skipped(&a, "skipped_cohort_policy"));

        let mut i = input();
        i.cohorts = vec![with_v(cohort(), "one_to_one", json!(false))];
        let (r, a) = run_with(&Policy::new(i).unwrap(), &caller(), source(), Some("acme"));
        assert!(r.is_ok() && skipped(&a, "skipped_ambiguous_source"));
        for k in ["zabbix_event_ids", "zabbix_trigger_ids", "source_members"] {
            let (r, a) = run(with(source(), k, json!([])));
            assert!(r.is_ok() && skipped(&a, "skipped_ambiguous_source"), "{k}");
        }

        let mut i = input();
        i.bypass = Bypass::On {
            change_ref: "CHG-1".into(),
        };
        let (r, a) = run_with(&Policy::new(i).unwrap(), &caller(), source(), Some("acme"));
        assert!(r.is_ok() && skipped(&a, "skipped_capture_bypass"));
        assert_eq!(a["zabbix_ack_sync_change_reference"], "CHG-1");
    }

    #[test]
    fn producer_reserved_keys_are_stripped_even_when_the_alert_is_skipped() {
        let m = with(
            with(source(), "zabbix_ack_sync_reason", json!("forged")),
            "ackline_x",
            json!(1),
        );
        let (r, a) = run(with(m, "zabbix_trigger_id", json!("78")));
        assert!(r.is_ok() && skipped(&a, "skipped_cohort_policy"));
        assert!(!a.contains_key("ackline_x"));
        let (_, a) = run(with(source(), "zabbix_ack_sync_reason", json!("forged")));
        assert!(
            no_record(&a),
            "a passing alert keeps no producer-supplied record: {a:?}"
        );
    }

    fn with_v(v: Value, k: &str, x: Value) -> Value {
        let Value::Object(m) = v else {
            panic!("object")
        };
        Value::Object(with(m, k, x))
    }

    #[test]
    fn invalid_policies_are_refused_when_built() {
        let refuse = |i: PolicyInput, reason: &str| {
            assert_eq!(
                Policy::new(i).unwrap_err(),
                PolicyBuildError(match reason {
                    "bypass" => "bypass_requires_change_reference",
                    "mapping" => "ingestion_mapping_invalid",
                    other => Box::leak(other.to_owned().into_boxed_str()),
                })
            );
        };
        let mut i = input();
        i.bypass = Bypass::On {
            change_ref: String::new(),
        };
        refuse(i, "bypass");
        for m in [
            json!({"": {"customer": "acme", "instance": "zbx-1"}}),
            json!({"l": {"customer": "acme", "instance": "zbx-1", "extra": "x"}}),
            json!({"l": {"customer": "acme"}}),
            json!({"l": {"customer": 1, "instance": "zbx-1"}}),
            json!({"l": {"customer": "", "instance": "zbx-1"}}),
            json!({"l": "acme"}),
        ] {
            let mut i = input();
            i.ingestion_mappings = m.as_object().unwrap().clone();
            refuse(i, "mapping");
        }
        for (c, reason) in [
            (json!("not an object"), "cohort_shape"),
            (with_v(cohort(), "unknown", json!(1)), "cohort_shape"),
            (without_v(cohort(), "allow_admin"), "cohort_shape"),
            (with_v(cohort(), "customer", json!("")), "cohort_identity"),
            (
                with_v(cohort(), "revision", json!("x".repeat(129))),
                "cohort_identity",
            ),
            (
                with_v(cohort(), "zabbix_instance", json!("-bad")),
                "cohort_instance",
            ),
            (
                with_v(cohort(), "zabbix_trigger_id", json!("0")),
                "invalid_source_id",
            ),
            (with_v(cohort(), "enabled", json!("true")), "cohort_boolean"),
            (
                with_v(cohort(), "host_drift", json!(1)),
                "cohort_drift_boolean",
            ),
            (
                with_v(cohort(), "host_ids", json!([])),
                "cohort_host_group_bound",
            ),
            (
                with_v(cohort(), "group_ids", json!(["2", "2"])),
                "cohort_host_group_bound",
            ),
            (
                with_v(cohort(), "host_ids", json!(["x"])),
                "invalid_source_id",
            ),
        ] {
            let mut i = input();
            i.cohorts = vec![c.clone()];
            refuse(i, reason);
        }
        let mut i = input();
        i.cohorts = vec![cohort(), with_v(cohort(), "revision", json!("r2"))];
        refuse(i, "duplicate_cohort");
    }

    // ---- actor policy (spec 4.1.1)

    fn human() -> CallerFacts {
        CallerFacts {
            login: Some("op".into()),
            user_id: Some("u-1".into()),
            auth_type: Some("bearer:test".into()),
            scopes: vec!["write:alerts".into()],
            customers: Some(vec!["acme".into()]),
            auth_required: true,
            from_request: true,
            ..CallerFacts::default()
        }
    }

    fn policy_with(admin_exceptions: &[&str], allow_admin: Value) -> Policy {
        let mut i = input();
        i.admin_exceptions = admin_exceptions.iter().map(|s| (*s).to_owned()).collect();
        i.cohorts = vec![with_v(cohort(), "allow_admin", allow_admin)];
        Policy::new(i).unwrap()
    }

    fn the_cohort(p: &Policy) -> &Cohort {
        p.select_cohort("acme", "zbx-1", "77").unwrap()
    }

    fn gate(p: &Policy, c: &CallerFacts, customer: &str) -> ActorOutcome {
        actor_gate(p, c, customer, the_cohort(p), now())
    }

    fn is_skip(o: &ActorOutcome) -> bool {
        matches!(o, ActorOutcome::Skip(m) if m == &skip_writes_expected())
    }

    fn skip_writes_expected() -> Map<String, Value> {
        json!({
            "zabbix_ack_sync_intent": null,
            "zabbix_ack_sync": "skipped",
            "zabbix_ack_sync_reason": "skipped_actor_policy",
            "zabbix_ack_sync_config_revision": "cfg-7",
            "zabbix_ack_sync_local_at": LOCAL_AT
        })
        .as_object()
        .unwrap()
        .clone()
    }

    #[test]
    fn an_ordinary_actor_in_the_customer_list_is_authorised() {
        let p = policy_with(&[], json!(false));
        let o = gate(&p, &human(), "acme");
        assert_eq!(
            o,
            ActorOutcome::Authorized(Actor {
                login: "op".into(),
                customers: vec!["acme".into()],
                admin: false
            })
        );
        // Not in the list, or an empty alert customer: skipped.
        assert!(is_skip(&gate(&p, &human(), "other")));
        assert!(is_skip(&gate(&p, &human(), "")));
        let c = CallerFacts {
            customers: Some(vec!["other".into()]),
            ..human()
        };
        assert!(is_skip(&gate(&p, &c, "acme")));
    }

    #[test]
    fn extraction_needs_request_facts_and_a_non_ingestion_login() {
        let p = policy_with(&[], json!(false));
        for c in [
            CallerFacts {
                from_request: false,
                ..human()
            },
            CallerFacts {
                user_id: None,
                ..human()
            },
            CallerFacts {
                user_id: Some(String::new()),
                ..human()
            },
            CallerFacts {
                login: None,
                ..human()
            },
            CallerFacts {
                login: Some(String::new()),
                ..human()
            },
            CallerFacts {
                login: Some("zbx-ingest".into()),
                ..human()
            },
            CallerFacts {
                customers: None,
                ..human()
            },
        ] {
            assert_eq!(human_actor(&p, &c), None, "{c:?}");
            assert!(is_skip(&gate(&p, &c, "acme")), "{c:?}");
        }
        // Today's API-key caller: no user id, so no actor.
        let api_key = CallerFacts {
            user_id: None,
            auth_type: None,
            ..human()
        };
        assert!(is_skip(&gate(&p, &api_key, "acme")));
    }

    #[test]
    fn admin_is_an_exact_scope_and_needs_an_exception() {
        let none = policy_with(&[], json!(true));
        let excepted = policy_with(&["op"], json!(true));
        for s in ["admin", "admin:alerts"] {
            let c = CallerFacts {
                scopes: vec!["read".into(), s.into()],
                ..human()
            };
            assert_eq!(human_actor(&none, &c), None, "{s} without an exception");
            assert!(
                human_actor(&excepted, &c).is_some_and(|a| a.admin),
                "{s} with one"
            );
        }
        for s in [
            "admin:alert",
            "Admin",
            "admin:alerts:x",
            "ADMIN:ALERTS",
            "xadmin",
        ] {
            let c = CallerFacts {
                scopes: vec![s.into()],
                ..human()
            };
            assert!(
                human_actor(&none, &c).is_some_and(|a| !a.admin),
                "{s} is not admin"
            );
        }
    }

    #[test]
    fn an_admin_is_authorised_by_allow_admin_alone() {
        let c = CallerFacts {
            scopes: vec!["admin".into()],
            customers: Some(vec![]),
            ..human()
        };
        // allow_admin true: authorised even with an empty customer list.
        let yes = policy_with(&["op"], json!(true));
        assert!(matches!(gate(&yes, &c, "acme"), ActorOutcome::Authorized(a) if a.admin));
        // allow_admin false: skipped even when the customer is listed.
        let no = policy_with(&["op"], json!(false));
        let listed = CallerFacts {
            customers: Some(vec!["acme".into()]),
            ..c.clone()
        };
        assert!(is_skip(&gate(&no, &listed, "acme")));
        // An exception alone does not authorise; an empty alert customer neither.
        assert!(is_skip(&gate(&no, &c, "acme")));
        assert!(is_skip(&gate(&yes, &c, "")));
    }

    #[test]
    fn an_empty_customer_list_is_an_actor_but_not_authorised() {
        let p = policy_with(&[], json!(false));
        let c = CallerFacts {
            customers: Some(vec![]),
            ..human()
        };
        assert_eq!(
            human_actor(&p, &c),
            Some(Actor {
                login: "op".into(),
                customers: vec![],
                admin: false
            })
        );
        assert!(is_skip(&gate(&p, &c, "acme")));
    }

    #[test]
    fn actor_customers_are_sorted_and_deduplicated() {
        let p = policy_with(&[], json!(false));
        let c = CallerFacts {
            customers: Some(vec![
                "b".into(),
                "acme".into(),
                "b".into(),
                "Z".into(),
                "é".into(),
            ]),
            ..human()
        };
        let a = human_actor(&p, &c).unwrap();
        assert_eq!(a.customers, ["Z", "acme", "b", "é"]);
    }

    #[test]
    fn select_cohort_needs_a_customer_and_an_enabled_match() {
        let p = policy();
        assert!(p.select_cohort("acme", "zbx-1", "77").is_some());
        assert!(p.select_cohort("", "zbx-1", "77").is_none());
        assert!(p.select_cohort("acme", "zbx-2", "77").is_none());
        assert!(p.select_cohort("acme", "zbx-1", "78").is_none());
        let mut i = input();
        i.cohorts = vec![with_v(cohort(), "enabled", json!(false))];
        assert!(
            Policy::new(i)
                .unwrap()
                .select_cohort("acme", "zbx-1", "77")
                .is_none()
        );
    }

    #[test]
    fn an_empty_admin_exception_is_refused() {
        let mut i = input();
        i.admin_exceptions = vec!["op".into(), String::new()];
        assert_eq!(
            Policy::new(i).unwrap_err(),
            PolicyBuildError("admin_exception_invalid")
        );
    }

    // ---- acknowledgement source and cohort prechecks (spec 4.1.2)

    const UUID: &str = "0f8fad5b-d9cb-469f-a165-70867728950e";

    fn stored(
        id: &str,
        customer: Option<&str>,
        attributes: Map<String, Value>,
    ) -> alrust_domain::Alert {
        alrust_domain::Alert {
            id: id.into(),
            resource: "r".into(),
            event: "e".into(),
            environment: "Production".into(),
            severity: "major".into(),
            correlate: vec![],
            status: "open".into(),
            service: vec![],
            group: "Misc".into(),
            value: None,
            text: String::new(),
            tags: vec![],
            attributes,
            origin: "zbx".into(),
            event_type: "exceptionAlert".into(),
            create_time: now(),
            timeout: Some(86_400),
            raw_data: None,
            customer: customer.map(str::to_owned),
            duplicate_count: Some(0),
            repeat: Some(false),
            previous_severity: None,
            trend_indication: None,
            receive_time: now(),
            last_receive_id: None,
            last_receive_time: None,
            update_time: Some(now()),
            history: vec![],
        }
    }

    fn pre(p: &Policy, a: &alrust_domain::Alert) -> Option<String> {
        match ack_prechecks(p, a, now()) {
            Precheck::Eligible { .. } => None,
            Precheck::Skip(m) => {
                assert_eq!(m.len(), 5, "{m:?}");
                assert_eq!(m["zabbix_ack_sync_intent"], Value::Null);
                assert_eq!(m["zabbix_ack_sync"], "skipped");
                assert_eq!(m["zabbix_ack_sync_config_revision"], "cfg-7");
                assert_eq!(m["zabbix_ack_sync_local_at"], LOCAL_AT);
                Some(m["zabbix_ack_sync_reason"].as_str().unwrap().to_owned())
            }
        }
    }

    fn reason(
        p: &Policy,
        id: &str,
        customer: Option<&str>,
        attributes: Map<String, Value>,
    ) -> Option<String> {
        pre(p, &stored(id, customer, attributes))
    }

    #[test]
    fn an_eligible_alert_yields_its_snapshot_and_cohort_and_is_not_changed() {
        let p = policy();
        let a = stored(UUID, Some("acme"), source());
        let before = a.attributes.clone();
        match ack_prechecks(&p, &a, now()) {
            Precheck::Eligible { source, cohort } => {
                assert_eq!(
                    source,
                    SourceSnapshot {
                        instance: "zbx-1".into(),
                        event_id: "1001".into(),
                        trigger_id: "77".into()
                    }
                );
                // The selected cohort is the one actor_gate takes.
                let actor = CallerFacts {
                    login: Some("op".into()),
                    user_id: Some("u".into()),
                    customers: Some(vec!["acme".into()]),
                    from_request: true,
                    ..CallerFacts::default()
                };
                assert!(matches!(
                    actor_gate(&p, &actor, "acme", cohort, now()),
                    ActorOutcome::Authorized(_)
                ));
            }
            Precheck::Skip(m) => panic!("skipped: {m:?}"),
        }
        // Nothing written back: the sentinels "" and "0" stay as they were.
        assert_eq!(a.attributes, before);
        assert_eq!(a.attributes["zabbix_cause_event_id"], "");
        assert_eq!(a.attributes["zabbix_recovery_event_id"], "0");
    }

    #[test]
    fn a_non_canonical_id_is_a_legacy_skip() {
        let p = policy();
        for id in [
            "0F8FAD5B-D9CB-469F-A165-70867728950E",
            "0f8fad5bd9cb469fa16570867728950e",
            "{0f8fad5b-d9cb-469f-a165-70867728950e}",
            "urn:uuid:0f8fad5b-d9cb-469f-a165-70867728950e",
            "0f8fad5b-d9cb-469f-a165-70867728950",
            "0f8fad5b-d9cb-469f-a165-70867728950e0",
            "0f8fad5b-d9cb-469f-a165_70867728950e",
            "0g8fad5b-d9cb-469f-a165-70867728950e",
            "not-a-uuid",
            "",
        ] {
            assert_eq!(
                reason(&p, id, Some("acme"), source()).as_deref(),
                Some("legacy_missing_mapping"),
                "{id}"
            );
        }
        assert_eq!(reason(&p, UUID, Some("acme"), source()), None);
    }

    #[test]
    fn a_source_contract_failure_is_a_legacy_skip() {
        let p = policy();
        for m in [
            without(source(), "zabbix_cause_event_id"),
            without(source(), "source_system"),
            with(source(), "source_system", json!("nagios")),
            with(source(), "source_contract_version", json!(1)),
            with(source(), "zabbix_instance", json!("-bad")),
            with(source(), "zabbix_event_id", json!("18446744073709551616")),
            with(source(), "zabbix_trigger_id", json!("0")),
            with(source(), "zabbix_recovery_event_id", json!("1001")),
            with(source(), "zabbix_cause_event_id", json!(true)),
        ] {
            assert_eq!(
                reason(&p, UUID, Some("acme"), m.clone()).as_deref(),
                Some("legacy_missing_mapping"),
                "{m:?}"
            );
        }
        for v in [
            Value::Null,
            json!(""),
            json!("0"),
            json!(0),
            json!(0.0),
            json!(false),
            json!("5"),
        ] {
            assert_eq!(
                reason(
                    &p,
                    UUID,
                    Some("acme"),
                    with(source(), "zabbix_cause_event_id", v.clone())
                ),
                None,
                "{v}"
            );
        }
    }

    #[test]
    fn no_enabled_matching_cohort_is_a_cohort_skip() {
        let p = policy();
        for (customer, m) in [
            (None, source()),
            (Some(""), source()),
            (Some("other"), source()),
            (
                Some("acme"),
                with(source(), "zabbix_trigger_id", json!("78")),
            ),
        ] {
            assert_eq!(
                reason(&p, UUID, customer, m).as_deref(),
                Some("skipped_cohort_policy"),
                "{customer:?}"
            );
        }
        let mut i = input();
        i.cohorts = vec![with_v(cohort(), "enabled", json!(false))];
        assert_eq!(
            reason(&Policy::new(i).unwrap(), UUID, Some("acme"), source()).as_deref(),
            Some("skipped_cohort_policy")
        );
    }

    #[test]
    fn ambiguity_is_a_skip_whatever_the_key_holds() {
        let p = policy();
        for k in ["zabbix_event_ids", "zabbix_trigger_ids", "source_members"] {
            for v in [Value::Null, json!([]), json!(""), json!(["1"])] {
                assert_eq!(
                    reason(&p, UUID, Some("acme"), with(source(), k, v.clone())).as_deref(),
                    Some("skipped_ambiguous_source"),
                    "{k}={v}"
                );
            }
        }
        let mut i = input();
        i.cohorts = vec![with_v(cohort(), "one_to_one", json!(false))];
        assert_eq!(
            reason(&Policy::new(i).unwrap(), UUID, Some("acme"), source()).as_deref(),
            Some("skipped_ambiguous_source")
        );
    }

    #[test]
    fn competing_failures_resolve_source_then_cohort_then_ambiguity() {
        let p = policy();
        let ambiguous = with(source(), "source_members", json!([]));
        // Bad id + no cohort + ambiguous: the source check wins.
        assert_eq!(
            reason(&p, "BAD", Some("other"), ambiguous.clone()).as_deref(),
            Some("legacy_missing_mapping")
        );
        // Bad source + no cohort: source.
        assert_eq!(
            reason(
                &p,
                UUID,
                Some("other"),
                with(ambiguous.clone(), "source_contract_version", json!("2"))
            )
            .as_deref(),
            Some("legacy_missing_mapping")
        );
        // No cohort + ambiguous: cohort.
        assert_eq!(
            reason(&p, UUID, Some("other"), ambiguous.clone()).as_deref(),
            Some("skipped_cohort_policy")
        );
        // Cohort found + ambiguous: ambiguity.
        assert_eq!(
            reason(&p, UUID, Some("acme"), ambiguous).as_deref(),
            Some("skipped_ambiguous_source")
        );
    }

    fn without_v(v: Value, k: &str) -> Value {
        let Value::Object(m) = v else {
            panic!("object")
        };
        Value::Object(without(m, k))
    }
}
