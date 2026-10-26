//! PostgreSQL persistence for alrust (sqlx).
//!
//! The receive, status and action paths run inside one transaction with the
//! matched row locked (`FOR UPDATE`), and call `alrust-engine` for every
//! state decision. Behaviour follows `spec/lifecycle.md`; the known alpha
//! simplifications are listed in the crate README section of the top-level
//! README ("Alpha limitations").

use alrust_domain::{
    Alert, ApiKey, DEFAULT_PREVIOUS_SEVERITY, Heartbeat, HistoryEntry, INFORM_SEVERITY,
    change_type, status, trend, truncate_ms,
};
use alrust_engine::{
    HistInfo, Timeouts, TransitionError, TransitionInput, action_timeout, hist_info, transition,
};
use chrono::{DateTime, Utc};
use serde_json::{Map, Value};
use sqlx::postgres::{PgPool, PgRow};
use sqlx::{Postgres, QueryBuilder, Row, Transaction};

pub use sqlx::migrate::MigrateError;

#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error(transparent)]
    Db(#[from] sqlx::Error),
    #[error(transparent)]
    Transition(#[from] TransitionError),
    #[error("not found")]
    NotFound,
}

/// Behaviour knobs, defaulting to the reference settings.
#[derive(Debug, Clone)]
pub struct StoreConfig {
    pub alert_timeout: i64,
    pub ack_timeout: i64,
    pub shelve_timeout: i64,
    pub history_limit: usize,
    pub history_on_value_change: bool,
}

impl Default for StoreConfig {
    fn default() -> Self {
        StoreConfig {
            alert_timeout: 86400,
            ack_timeout: 0,
            shelve_timeout: 7200,
            history_limit: 100,
            history_on_value_change: true,
        }
    }
}

impl StoreConfig {
    fn timeouts(&self) -> Timeouts {
        Timeouts {
            alert: self.alert_timeout,
            ack: self.ack_timeout,
            shelve: self.shelve_timeout,
        }
    }
}

/// A parsed, validated incoming alert (spec R0), before matching.
#[derive(Debug, Clone)]
pub struct NewAlert {
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
    pub timeout: i64,
    pub raw_data: Option<String>,
    pub customer: Option<String>,
}

/// How a received alert was stored.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReceiveKind {
    Created,
    Duplicate,
    Correlated,
}

/// Query filters for `GET /alerts`. Each list is OR-ed within a field and
/// AND-ed across fields, as the reference query builder does.
#[derive(Debug, Clone, Default)]
pub struct AlertQuery {
    pub ids: Vec<String>,
    pub resources: Vec<String>,
    pub events: Vec<String>,
    pub environments: Vec<String>,
    pub severities: Vec<String>,
    pub statuses: Vec<String>,
    pub services: Vec<String>,
    pub groups: Vec<String>,
    pub origins: Vec<String>,
    pub tags: Vec<String>,
    /// `field!=value` exclusions (`postgres/utils.py` `filter_query`): the
    /// alert's column must equal none of the values.
    pub not_ids: Vec<String>,
    pub not_resources: Vec<String>,
    pub not_events: Vec<String>,
    pub not_environments: Vec<String>,
    pub not_severities: Vec<String>,
    pub not_statuses: Vec<String>,
    pub not_groups: Vec<String>,
    pub not_origins: Vec<String>,
    /// `None` means no customer restriction.
    pub customers: Option<Vec<String>>,
}

#[derive(Debug)]
pub struct AlertPage {
    pub alerts: Vec<Alert>,
    pub total: i64,
    pub severity_counts: Map<String, Value>,
    pub status_counts: Map<String, Value>,
}

/// A tags or attributes edit of one alert.
#[derive(Debug)]
pub enum AlertEdit {
    Tag(Vec<String>),
    Untag(Vec<String>),
    Attributes(serde_json::Map<String, serde_json::Value>),
}

#[derive(Debug, Clone)]
pub struct Store {
    pool: PgPool,
    cfg: StoreConfig,
}

/// Whether `write_alert` inserts a new row or updates the locked one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum WriteMode {
    Insert,
    Update,
}

static MIGRATOR: sqlx::migrate::Migrator = sqlx::migrate!("./migrations");

fn union_tags(stored: &[String], incoming: &[String]) -> Vec<String> {
    let mut out: Vec<String> = Vec::with_capacity(stored.len() + incoming.len());
    for t in stored.iter().chain(incoming) {
        if !out.contains(t) {
            out.push(t.clone());
        }
    }
    out
}

/// Shallow merge with incoming keys winning, then drop null-valued keys
/// (spec R5.2, R1.3).
fn merge_attributes(
    stored: &Map<String, Value>,
    incoming: &Map<String, Value>,
) -> Map<String, Value> {
    let mut out = stored.clone();
    for (k, v) in incoming {
        out.insert(k.clone(), v.clone());
    }
    out.retain(|_, v| !v.is_null());
    out
}

/// The advisory-lock key for an alert identity. A `None` customer and every
/// string customer map to distinct keys.
fn identity_lock_key(environment: &str, resource: &str, customer: Option<&str>) -> String {
    let customer = match customer {
        Some(c) => format!("c:{c}"),
        None => "n".to_string(),
    };
    format!("alrust-alert\u{1f}{environment}\u{1f}{resource}\u{1f}{customer}")
}

/// `g.login` is `None` for an unauthenticated request with auth disabled.
fn user_of(login: &str) -> Option<String> {
    (!login.is_empty()).then(|| login.to_string())
}

fn like_prefix(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 1);
    for c in s.chars() {
        if matches!(c, '%' | '_' | '\\') {
            out.push('\\');
        }
        out.push(c);
    }
    out.push('%');
    out
}

fn row_to_alert(row: &PgRow) -> Result<Alert, sqlx::Error> {
    let attributes: Value = row.try_get("attributes")?;
    let history: Value = row.try_get("history")?;
    let history: Vec<HistoryEntry> =
        serde_json::from_value(history).map_err(|e| sqlx::Error::Decode(Box::new(e)))?;
    Ok(Alert {
        id: row.try_get("id")?,
        resource: row.try_get("resource")?,
        event: row.try_get("event")?,
        environment: row.try_get("environment")?,
        severity: row.try_get("severity")?,
        correlate: row.try_get("correlate")?,
        status: row.try_get("status")?,
        service: row.try_get("service")?,
        group: row.try_get("group")?,
        value: row
            .try_get::<Option<Value>, _>("value")?
            .unwrap_or(Value::Null),
        text: row.try_get("text")?,
        tags: row.try_get("tags")?,
        attributes: match attributes {
            Value::Object(m) => m,
            _ => Map::new(),
        },
        origin: row.try_get("origin")?,
        event_type: row.try_get("type")?,
        create_time: row.try_get("create_time")?,
        timeout: row.try_get("timeout")?,
        raw_data: row.try_get("raw_data")?,
        customer: row.try_get("customer")?,
        duplicate_count: row.try_get("duplicate_count")?,
        repeat: row.try_get("repeat")?,
        previous_severity: row.try_get("previous_severity")?,
        trend_indication: row.try_get("trend_indication")?,
        receive_time: row.try_get("receive_time")?,
        last_receive_id: row.try_get("last_receive_id")?,
        last_receive_time: row.try_get("last_receive_time")?,
        update_time: row.try_get("update_time")?,
        history,
    })
}

fn row_to_heartbeat(row: &PgRow) -> Result<Heartbeat, sqlx::Error> {
    let attributes: Value = row.try_get("attributes")?;
    Ok(Heartbeat {
        id: row.try_get("id")?,
        origin: row.try_get("origin")?,
        tags: row.try_get("tags")?,
        attributes: match attributes {
            Value::Object(m) => m,
            _ => Map::new(),
        },
        event_type: row.try_get("type")?,
        create_time: row.try_get("create_time")?,
        timeout: row.try_get("timeout")?,
        receive_time: row.try_get("receive_time")?,
        customer: row.try_get("customer")?,
    })
}

fn row_to_key(row: &PgRow) -> Result<ApiKey, sqlx::Error> {
    Ok(ApiKey {
        id: row.try_get("id")?,
        key: row.try_get("key")?,
        user: row.try_get("user")?,
        scopes: row.try_get("scopes")?,
        text: row.try_get("text")?,
        expire_time: row.try_get("expire_time")?,
        count: row.try_get("count")?,
        last_used_time: row.try_get("last_used_time")?,
        customer: row.try_get("customer")?,
    })
}

/// History entries that count for an incoming event (spec R7.1, restricted
/// to the matched alert's own history in the alpha).
fn identity_history<'a>(alert: &'a Alert, event: &str) -> Vec<&'a HistoryEntry> {
    let correlated = alert.correlate.iter().any(|c| c == event);
    alert
        .history
        .iter()
        .filter(|h| correlated || h.event == event)
        .collect()
}

impl Store {
    pub fn new(pool: PgPool, cfg: StoreConfig) -> Self {
        Store { pool, cfg }
    }

    pub fn config(&self) -> &StoreConfig {
        &self.cfg
    }

    /// Apply the embedded migrations.
    ///
    /// # Errors
    ///
    /// The sqlx migration error.
    pub async fn migrate(&self) -> Result<(), MigrateError> {
        MIGRATOR.run(&self.pool).await
    }

    /// `SELECT 1`.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn ping(&self) -> Result<(), StoreError> {
        sqlx::query("SELECT 1").execute(&self.pool).await?;
        Ok(())
    }

    fn push_history(&self, history: &mut Vec<HistoryEntry>, entry: HistoryEntry) {
        history.insert(0, entry);
        history.truncate(self.cfg.history_limit);
    }

    async fn write_alert(
        tx: &mut Transaction<'_, Postgres>,
        a: &Alert,
        mode: WriteMode,
    ) -> Result<(), sqlx::Error> {
        let sql = if mode == WriteMode::Insert {
            r#"INSERT INTO alerts (id, resource, event, environment, severity, correlate, status, service, "group",
                value, text, tags, attributes, origin, type, create_time, timeout, raw_data, customer,
                duplicate_count, repeat, previous_severity, trend_indication, receive_time, last_receive_id,
                last_receive_time, update_time, history)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28)"#
        } else {
            r#"UPDATE alerts SET resource=$2, event=$3, environment=$4, severity=$5, correlate=$6, status=$7,
                service=$8, "group"=$9, value=$10, text=$11, tags=$12, attributes=$13, origin=$14, type=$15,
                create_time=$16, timeout=$17, raw_data=$18, customer=$19, duplicate_count=$20, repeat=$21,
                previous_severity=$22, trend_indication=$23, receive_time=$24, last_receive_id=$25,
                last_receive_time=$26, update_time=$27, history=$28
               WHERE id=$1"#
        };
        let history =
            serde_json::to_value(&a.history).map_err(|e| sqlx::Error::Encode(Box::new(e)))?;
        sqlx::query(sql)
            .bind(&a.id)
            .bind(&a.resource)
            .bind(&a.event)
            .bind(&a.environment)
            .bind(&a.severity)
            .bind(&a.correlate)
            .bind(&a.status)
            .bind(&a.service)
            .bind(&a.group)
            .bind(if a.value.is_null() {
                None
            } else {
                Some(&a.value)
            })
            .bind(&a.text)
            .bind(&a.tags)
            .bind(Value::Object(a.attributes.clone()))
            .bind(&a.origin)
            .bind(&a.event_type)
            .bind(a.create_time)
            .bind(a.timeout)
            .bind(&a.raw_data)
            .bind(&a.customer)
            .bind(a.duplicate_count)
            .bind(a.repeat)
            .bind(&a.previous_severity)
            .bind(&a.trend_indication)
            .bind(a.receive_time)
            .bind(&a.last_receive_id)
            .bind(a.last_receive_time)
            .bind(a.update_time)
            .bind(history)
            .execute(&mut **tx)
            .await?;
        Ok(())
    }

    /// The receive path: duplicate, else correlate, else create (spec R1 step 4).
    ///
    /// Both lookups and the insert run under a transaction-scoped advisory
    /// lock on the alert identity (environment, resource, customer), which
    /// covers every duplicate and correlation match. Two first-time alerts
    /// for the same identity therefore cannot both miss and insert twice.
    ///
    /// # Errors
    ///
    /// `StoreError::Transition` when the severity is unknown or the state
    /// machine refuses the change, otherwise the database error.
    #[expect(
        clippy::too_many_lines,
        reason = "the three receive outcomes share one locked transaction, kept in one place"
    )]
    pub async fn receive(
        &self,
        n: NewAlert,
        login: &str,
    ) -> Result<(Alert, ReceiveKind), StoreError> {
        let mut tx = self.pool.begin().await?;
        sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
            .bind(identity_lock_key(
                &n.environment,
                &n.resource,
                n.customer.as_deref(),
            ))
            .execute(&mut *tx)
            .await?;
        let now = truncate_ms(Utc::now());

        let dup = sqlx::query(
            "SELECT * FROM alerts WHERE environment=$1 AND resource=$2 AND event=$3 AND severity=$4
               AND customer IS NOT DISTINCT FROM $5 LIMIT 1 FOR UPDATE",
        )
        .bind(&n.environment)
        .bind(&n.resource)
        .bind(&n.event)
        .bind(&n.severity)
        .bind(&n.customer)
        .fetch_optional(&mut *tx)
        .await?;

        if let Some(row) = dup {
            let mut a = row_to_alert(&row)?;
            let info = hist_info(&identity_history(&a, &n.event), None);
            let t = transition(TransitionInput {
                severity: &n.severity,
                alert_status: &n.status,
                previous_severity: None,
                current_status: info.current_status.as_deref(),
                previous_status: info.previous_status.as_deref(),
                action: None,
            })?;
            let current = info
                .current_status
                .clone()
                .unwrap_or_else(|| status::OPEN.to_string());
            let entry = if t.status != current {
                a.update_time = now;
                Some(Self::receive_entry(
                    &n,
                    &t.status,
                    change_type::STATUS,
                    login,
                ))
            } else if self.cfg.history_on_value_change
                && Some(&n.value) != info.current_value.as_ref()
            {
                Some(Self::receive_entry(&n, &current, change_type::VALUE, login))
            } else {
                None
            };
            a.status = t.status;
            a.service = n.service;
            a.value = n.value;
            a.text = n.text;
            a.timeout = Some(n.timeout);
            a.raw_data = n.raw_data;
            a.tags = union_tags(&a.tags, &n.tags);
            a.attributes = merge_attributes(&a.attributes, &n.attributes);
            a.duplicate_count += 1;
            a.repeat = true;
            a.last_receive_id = n.id;
            a.last_receive_time = now;
            if let Some(e) = entry {
                self.push_history(&mut a.history, e);
            }
            Self::write_alert(&mut tx, &a, WriteMode::Update).await?;
            tx.commit().await?;
            return Ok((a, ReceiveKind::Duplicate));
        }

        let corr = sqlx::query(
            "SELECT * FROM alerts WHERE environment=$1 AND resource=$2
               AND ((event=$3 AND severity<>$4) OR (event<>$3 AND $3=ANY(correlate)))
               AND customer IS NOT DISTINCT FROM $5 LIMIT 1 FOR UPDATE",
        )
        .bind(&n.environment)
        .bind(&n.resource)
        .bind(&n.event)
        .bind(&n.severity)
        .bind(&n.customer)
        .fetch_optional(&mut *tx)
        .await?;

        if let Some(row) = corr {
            let mut a = row_to_alert(&row)?;
            let previous_severity = a.severity.clone();
            let info = hist_info(&identity_history(&a, &n.event), None);
            let t = transition(TransitionInput {
                severity: &n.severity,
                alert_status: &n.status,
                previous_severity: Some(&previous_severity),
                current_status: info.current_status.as_deref(),
                previous_status: info.previous_status.as_deref(),
                action: None,
            })?;
            let current = info
                .current_status
                .clone()
                .unwrap_or_else(|| status::OPEN.to_string());
            if t.status != current {
                a.update_time = now;
            }
            let entry = Self::receive_entry(&n, &t.status, change_type::SEVERITY, login);
            a.trend_indication = trend(&previous_severity, &n.severity).to_string();
            a.previous_severity = previous_severity;
            a.event = n.event;
            a.severity = n.severity;
            a.status = t.status;
            a.service = n.service;
            a.value = n.value;
            a.text = n.text;
            a.timeout = Some(n.timeout);
            a.raw_data = n.raw_data;
            a.create_time = n.create_time;
            a.duplicate_count = 0;
            a.repeat = false;
            a.receive_time = now;
            a.last_receive_id = n.id;
            a.last_receive_time = now;
            a.tags = union_tags(&a.tags, &n.tags);
            a.attributes = merge_attributes(&a.attributes, &n.attributes);
            self.push_history(&mut a.history, entry);
            Self::write_alert(&mut tx, &a, WriteMode::Update).await?;
            tx.commit().await?;
            return Ok((a, ReceiveKind::Correlated));
        }

        let t = transition(TransitionInput {
            severity: &n.severity,
            alert_status: &n.status,
            previous_severity: None,
            current_status: None,
            previous_status: None,
            action: None,
        })?;
        let entry = Self::receive_entry(&n, &t.status, change_type::NEW, login);
        let mut attributes = n.attributes.clone();
        attributes.retain(|_, v| !v.is_null());
        let a = Alert {
            trend_indication: trend(DEFAULT_PREVIOUS_SEVERITY, &n.severity).to_string(),
            previous_severity: DEFAULT_PREVIOUS_SEVERITY.to_string(),
            status: t.status,
            duplicate_count: 0,
            repeat: false,
            receive_time: now,
            last_receive_id: n.id.clone(),
            last_receive_time: now,
            update_time: now,
            history: vec![entry],
            id: n.id,
            resource: n.resource,
            event: n.event,
            environment: n.environment,
            severity: n.severity,
            correlate: n.correlate,
            service: n.service,
            group: n.group,
            value: n.value,
            text: n.text,
            tags: union_tags(&[], &n.tags),
            attributes,
            origin: n.origin,
            event_type: n.event_type,
            create_time: n.create_time,
            timeout: Some(n.timeout),
            raw_data: n.raw_data,
            customer: n.customer,
        };
        Self::write_alert(&mut tx, &a, WriteMode::Insert).await?;
        tx.commit().await?;
        Ok((a, ReceiveKind::Created))
    }

    fn receive_entry(n: &NewAlert, status: &str, kind: &str, login: &str) -> HistoryEntry {
        HistoryEntry {
            id: n.id.clone(),
            event: n.event.clone(),
            severity: Some(n.severity.clone()),
            status: Some(status.to_string()),
            value: n.value.clone(),
            text: Some(n.text.clone()),
            change_type: kind.to_string(),
            // Receive entries carry the client's createTime (spec R4.2, R5.4, R6.4).
            update_time: n.create_time,
            user: user_of(login),
            timeout: Some(n.timeout),
        }
    }

    async fn lock_alert(
        tx: &mut Transaction<'_, Postgres>,
        id: &str,
        customers: Option<&[String]>,
    ) -> Result<Alert, StoreError> {
        let row = sqlx::query(
            "SELECT * FROM alerts WHERE (id LIKE $1 OR last_receive_id LIKE $1)
               AND ($2::text[] IS NULL OR customer = ANY($2)) LIMIT 1 FOR UPDATE",
        )
        .bind(like_prefix(id))
        .bind(customers)
        .fetch_optional(&mut **tx)
        .await?
        .ok_or(StoreError::NotFound)?;
        Ok(row_to_alert(&row)?)
    }

    /// Look an alert up by id or id prefix, or by `lastReceiveId` (spec C17).
    ///
    /// # Errors
    ///
    /// `StoreError::NotFound` when nothing matches, otherwise the database error.
    pub async fn get_alert(
        &self,
        id: &str,
        customers: Option<&[String]>,
    ) -> Result<Alert, StoreError> {
        let row = sqlx::query(
            "SELECT * FROM alerts WHERE (id LIKE $1 OR last_receive_id LIKE $1)
               AND ($2::text[] IS NULL OR customer = ANY($2)) LIMIT 1",
        )
        .bind(like_prefix(id))
        .bind(customers)
        .fetch_optional(&self.pool)
        .await?
        .ok_or(StoreError::NotFound)?;
        Ok(row_to_alert(&row)?)
    }

    /// `PUT /alert/<id>/status` (spec A1): no state machine, raw timeout stored.
    ///
    /// # Errors
    ///
    /// `StoreError::NotFound` when nothing matches, otherwise the database error.
    pub async fn set_status(
        &self,
        id: &str,
        customers: Option<&[String]>,
        new_status: &str,
        text: &str,
        timeout: Option<i64>,
        login: &str,
    ) -> Result<Alert, StoreError> {
        let mut tx = self.pool.begin().await?;
        let mut a = Self::lock_alert(&mut tx, id, customers).await?;
        let now = truncate_ms(Utc::now());
        let entry = HistoryEntry {
            id: a.id.clone(),
            event: a.event.clone(),
            severity: Some(a.severity.clone()),
            status: Some(new_status.to_string()),
            value: a.value.clone(),
            text: Some(text.to_string()),
            change_type: change_type::STATUS.to_string(),
            update_time: now,
            user: user_of(login),
            timeout: Some(
                timeout
                    .filter(|t| *t != 0)
                    .unwrap_or(self.cfg.alert_timeout),
            ),
        };
        a.status = new_status.to_string();
        a.update_time = now;
        a.timeout = timeout;
        a.attributes.retain(|_, v| !v.is_null());
        self.push_history(&mut a.history, entry);
        Self::write_alert(&mut tx, &a, WriteMode::Update).await?;
        tx.commit().await?;
        Ok(a)
    }

    /// `PUT /alert/<id>/tag`, `/untag` and `/attributes` (`views/alerts.py:194-272`).
    /// The reference updates only `tags` or `attributes`: no history entry,
    /// no `update_time` change. Tag adds keep the first-seen order (the
    /// reference's `DISTINCT UNNEST` may reorder); untagging every tag leaves
    /// an empty list. An attribute set to null is removed
    /// (`postgres/base.py:339-349`).
    ///
    /// # Errors
    ///
    /// `StoreError::NotFound` when nothing matches, otherwise the database error.
    pub async fn edit_alert(
        &self,
        id: &str,
        customers: Option<&[String]>,
        edit: AlertEdit,
    ) -> Result<Alert, StoreError> {
        let mut tx = self.pool.begin().await?;
        let mut a = Self::lock_alert(&mut tx, id, customers).await?;
        match edit {
            AlertEdit::Tag(tags) => {
                for t in tags {
                    if !a.tags.contains(&t) {
                        a.tags.push(t);
                    }
                }
            }
            AlertEdit::Untag(tags) => a.tags.retain(|t| !tags.contains(t)),
            AlertEdit::Attributes(attrs) => {
                for (k, v) in attrs {
                    a.attributes.insert(k, v);
                }
                a.attributes.retain(|_, v| !v.is_null());
            }
        }
        Self::write_alert(&mut tx, &a, WriteMode::Update).await?;
        tx.commit().await?;
        Ok(a)
    }

    /// `PUT /alert/<id>/action` (spec A2-A5), also used by housekeeping.
    ///
    /// # Errors
    ///
    /// `StoreError::NotFound` when nothing matches, `StoreError::Transition`
    /// when the action is not allowed in the current status, otherwise the
    /// database error.
    pub async fn take_action(
        &self,
        id: &str,
        customers: Option<&[String]>,
        act: &str,
        text: &str,
        timeout: Option<i64>,
        login: &str,
    ) -> Result<Alert, StoreError> {
        let mut tx = self.pool.begin().await?;
        let a = Self::lock_alert(&mut tx, id, customers).await?;
        let a = self
            .apply_action(&mut tx, a, act, text, timeout, login)
            .await?;
        tx.commit().await?;
        Ok(a)
    }

    async fn apply_action(
        &self,
        tx: &mut Transaction<'_, Postgres>,
        mut a: Alert,
        act: &str,
        text: &str,
        timeout: Option<i64>,
        login: &str,
    ) -> Result<Alert, StoreError> {
        let now = truncate_ms(Utc::now());
        let info: HistInfo = hist_info(&identity_history(&a, &a.event), Some(act));
        let t = transition(TransitionInput {
            severity: &a.severity,
            alert_status: &a.status,
            previous_severity: Some(&a.previous_severity),
            current_status: info.current_status.as_deref(),
            previous_status: info.previous_status.as_deref(),
            action: Some(act),
        })?;
        let hist_timeout = action_timeout(
            act,
            timeout,
            info.previous_timeout,
            a.timeout,
            self.cfg.timeouts(),
        );
        let kind = if change_type::ALL.contains(&act) {
            act
        } else {
            change_type::ACTION
        };
        let entry = HistoryEntry {
            id: a.id.clone(),
            event: a.event.clone(),
            severity: Some(t.severity.clone()),
            status: Some(t.status.clone()),
            value: a.value.clone(),
            text: Some(text.to_string()),
            change_type: kind.to_string(),
            update_time: now,
            user: user_of(login),
            timeout: Some(hist_timeout),
        };
        if t.severity != a.severity {
            a.previous_severity = std::mem::replace(&mut a.severity, t.severity);
        }
        a.status = t.status;
        a.update_time = now;
        // The row keeps the hydrated model timeout: NULL becomes ALERT_TIMEOUT (spec A4.1).
        a.timeout = Some(a.timeout.unwrap_or(self.cfg.alert_timeout));
        a.attributes.retain(|_, v| !v.is_null());
        self.push_history(&mut a.history, entry);
        Self::write_alert(tx, &a, WriteMode::Update).await?;
        Ok(a)
    }

    /// `DELETE /alert/<id>`: one alert by id or id prefix, or by `lastReceiveId`.
    ///
    /// # Errors
    ///
    /// `StoreError::NotFound` when nothing matches, otherwise the database error.
    pub async fn delete_alert(
        &self,
        id: &str,
        customers: Option<&[String]>,
    ) -> Result<(), StoreError> {
        let r = sqlx::query(
            "DELETE FROM alerts WHERE id IN (SELECT id FROM alerts WHERE (id LIKE $1 OR last_receive_id LIKE $1)
               AND ($2::text[] IS NULL OR customer = ANY($2)) LIMIT 1)",
        )
        .bind(like_prefix(id))
        .bind(customers)
        .execute(&self.pool)
        .await?;
        if r.rows_affected() == 0 {
            return Err(StoreError::NotFound);
        }
        Ok(())
    }

    fn push_filters<'q>(qb: &mut QueryBuilder<'q, Postgres>, q: &'q AlertQuery) {
        qb.push(" WHERE true");
        let fields: [(&str, &Vec<String>); 8] = [
            ("environment", &q.environments),
            ("severity", &q.severities),
            ("status", &q.statuses),
            ("resource", &q.resources),
            ("event", &q.events),
            ("\"group\"", &q.groups),
            ("origin", &q.origins),
            ("id", &q.ids),
        ];
        for (col, vals) in fields {
            if !vals.is_empty() {
                qb.push(format!(" AND {col} = ANY("))
                    .push_bind(vals)
                    .push(")");
            }
        }
        // The reference writes `col != v` for one value and `NOT col = ANY(..)`
        // for several; both exclude NULL columns, as this does.
        let negated: [(&str, &Vec<String>); 8] = [
            ("environment", &q.not_environments),
            ("severity", &q.not_severities),
            ("status", &q.not_statuses),
            ("resource", &q.not_resources),
            ("event", &q.not_events),
            ("\"group\"", &q.not_groups),
            ("origin", &q.not_origins),
            ("id", &q.not_ids),
        ];
        for (col, vals) in negated {
            if !vals.is_empty() {
                qb.push(format!(" AND NOT ({col} = ANY("))
                    .push_bind(vals)
                    .push("))");
            }
        }
        if !q.services.is_empty() {
            qb.push(" AND service && ").push_bind(&q.services);
        }
        if !q.tags.is_empty() {
            qb.push(" AND tags @> ").push_bind(&q.tags);
        }
        if let Some(c) = &q.customers {
            qb.push(" AND customer = ANY(").push_bind(c).push(")");
        }
    }

    /// `GET /alerts`: newest `lastReceiveTime` first.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn find_alerts(
        &self,
        q: &AlertQuery,
        page: i64,
        page_size: i64,
    ) -> Result<AlertPage, StoreError> {
        let mut counts = QueryBuilder::new("SELECT severity, status, count(*) AS n FROM alerts");
        Self::push_filters(&mut counts, q);
        counts.push(" GROUP BY severity, status");
        let rows = counts.build().fetch_all(&self.pool).await?;
        let mut severity_counts = Map::new();
        let mut status_counts = Map::new();
        let mut total = 0i64;
        for r in rows {
            let sev: String = r.try_get("severity")?;
            let st: String = r.try_get("status")?;
            let n: i64 = r.try_get("n")?;
            total += n;
            let e = severity_counts.entry(sev).or_insert(Value::from(0));
            *e = Value::from(e.as_i64().unwrap_or(0) + n);
            let e = status_counts.entry(st).or_insert(Value::from(0));
            *e = Value::from(e.as_i64().unwrap_or(0) + n);
        }
        let mut sel = QueryBuilder::new("SELECT * FROM alerts");
        Self::push_filters(&mut sel, q);
        sel.push(" ORDER BY last_receive_time DESC, id LIMIT ")
            .push_bind(page_size)
            .push(" OFFSET ")
            .push_bind((page - 1) * page_size);
        let rows = sel.build().fetch_all(&self.pool).await?;
        let alerts = rows
            .iter()
            .map(row_to_alert)
            .collect::<Result<Vec<_>, _>>()?;
        Ok(AlertPage {
            alerts,
            total,
            severity_counts,
            status_counts,
        })
    }

    // ---- heartbeats (spec HB0-HB5) ----

    /// `POST /heartbeat`: insert, or refresh the row with the same origin and
    /// customer.
    ///
    /// # Errors
    ///
    /// The database error.
    #[expect(
        clippy::too_many_arguments,
        reason = "one parameter per heartbeat column; a struct would only move the list"
    )]
    pub async fn upsert_heartbeat(
        &self,
        id: &str,
        origin: &str,
        tags: &[String],
        attributes: &Map<String, Value>,
        event_type: &str,
        create_time: DateTime<Utc>,
        timeout: i64,
        customer: Option<&str>,
    ) -> Result<Heartbeat, StoreError> {
        let now = truncate_ms(Utc::now());
        let row = sqlx::query(
            "INSERT INTO heartbeats (id, origin, tags, attributes, type, create_time, timeout, receive_time, customer)
             VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
             ON CONFLICT (origin, COALESCE(customer, '')) DO UPDATE SET
               tags=EXCLUDED.tags, attributes=EXCLUDED.attributes, create_time=EXCLUDED.create_time,
               timeout=EXCLUDED.timeout, receive_time=EXCLUDED.receive_time
             RETURNING *",
        )
        .bind(id)
        .bind(origin)
        .bind(tags)
        .bind(Value::Object(attributes.clone()))
        .bind(event_type)
        .bind(create_time)
        .bind(timeout)
        .bind(now)
        .bind(customer)
        .fetch_one(&self.pool)
        .await?;
        Ok(row_to_heartbeat(&row)?)
    }

    /// One heartbeat by id or id prefix.
    ///
    /// # Errors
    ///
    /// `StoreError::NotFound` when nothing matches, otherwise the database error.
    pub async fn get_heartbeat(&self, id: &str) -> Result<Heartbeat, StoreError> {
        let row = sqlx::query("SELECT * FROM heartbeats WHERE id LIKE $1 LIMIT 1")
            .bind(like_prefix(id))
            .fetch_optional(&self.pool)
            .await?
            .ok_or(StoreError::NotFound)?;
        Ok(row_to_heartbeat(&row)?)
    }

    /// All heartbeats, newest first, optionally limited to some customers.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn list_heartbeats(
        &self,
        customers: Option<&[String]>,
    ) -> Result<Vec<Heartbeat>, StoreError> {
        let rows = sqlx::query(
            "SELECT * FROM heartbeats WHERE ($1::text[] IS NULL OR customer = ANY($1)) ORDER BY receive_time DESC",
        )
        .bind(customers)
        .fetch_all(&self.pool)
        .await?;
        Ok(rows
            .iter()
            .map(row_to_heartbeat)
            .collect::<Result<Vec<_>, _>>()?)
    }

    /// Delete one heartbeat by id or id prefix.
    ///
    /// # Errors
    ///
    /// `StoreError::NotFound` when nothing matches, otherwise the database error.
    pub async fn delete_heartbeat(&self, id: &str) -> Result<(), StoreError> {
        let r = sqlx::query("DELETE FROM heartbeats WHERE id IN (SELECT id FROM heartbeats WHERE id LIKE $1 LIMIT 1)")
            .bind(like_prefix(id))
            .execute(&self.pool)
            .await?;
        if r.rows_affected() == 0 {
            return Err(StoreError::NotFound);
        }
        Ok(())
    }

    // ---- API keys ----

    /// Insert a key and return the stored row.
    ///
    /// # Errors
    ///
    /// The database error, including a duplicate key value.
    pub async fn create_key(&self, k: &ApiKey) -> Result<ApiKey, StoreError> {
        let row = sqlx::query(
            r#"INSERT INTO keys (id, key, "user", scopes, text, expire_time, count, last_used_time, customer)
               VALUES ($1,$2,$3,$4,$5,$6,0,NULL,$7) RETURNING *"#,
        )
        .bind(&k.id)
        .bind(&k.key)
        .bind(&k.user)
        .bind(&k.scopes)
        .bind(&k.text)
        .bind(k.expire_time)
        .bind(&k.customer)
        .fetch_one(&self.pool)
        .await?;
        Ok(row_to_key(&row)?)
    }

    /// Ensure a bootstrap key exists with exactly these scopes.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn ensure_key(&self, k: &ApiKey) -> Result<(), StoreError> {
        sqlx::query(
            r#"INSERT INTO keys (id, key, "user", scopes, text, expire_time, count, last_used_time, customer)
               VALUES ($1,$2,$3,$4,$5,$6,0,NULL,$7)
               ON CONFLICT (key) DO UPDATE SET scopes=EXCLUDED.scopes, expire_time=EXCLUDED.expire_time"#,
        )
        .bind(&k.id)
        .bind(&k.key)
        .bind(&k.user)
        .bind(&k.scopes)
        .bind(&k.text)
        .bind(k.expire_time)
        .bind(&k.customer)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    /// Look up an active key and record its use.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn use_key(&self, key: &str) -> Result<Option<ApiKey>, StoreError> {
        let row = sqlx::query(
            "UPDATE keys SET count = count + 1, last_used_time = now()
              WHERE key = $1 AND expire_time > now() RETURNING *",
        )
        .bind(key)
        .fetch_optional(&self.pool)
        .await?;
        Ok(row.as_ref().map(row_to_key).transpose()?)
    }

    /// All keys, latest expiry first, optionally limited to some customers.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn list_keys(&self, customers: Option<&[String]>) -> Result<Vec<ApiKey>, StoreError> {
        let rows = sqlx::query(
            "SELECT * FROM keys WHERE ($1::text[] IS NULL OR customer = ANY($1)) ORDER BY expire_time DESC",
        )
        .bind(customers)
        .fetch_all(&self.pool)
        .await?;
        Ok(rows.iter().map(row_to_key).collect::<Result<Vec<_>, _>>()?)
    }

    /// One user's keys, latest expiry first.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn list_keys_by_user(&self, user: &str) -> Result<Vec<ApiKey>, StoreError> {
        let rows = sqlx::query(r#"SELECT * FROM keys WHERE "user" = $1 ORDER BY expire_time DESC"#)
            .bind(user)
            .fetch_all(&self.pool)
            .await?;
        Ok(rows.iter().map(row_to_key).collect::<Result<Vec<_>, _>>()?)
    }

    /// Delete one key by exact id.
    ///
    /// # Errors
    ///
    /// `StoreError::NotFound` when nothing matches, otherwise the database error.
    pub async fn delete_key(&self, id: &str) -> Result<(), StoreError> {
        let r = sqlx::query("DELETE FROM keys WHERE id = $1")
            .bind(id)
            .execute(&self.pool)
            .await?;
        if r.rows_affected() == 0 {
            return Err(StoreError::NotFound);
        }
        Ok(())
    }

    // ---- housekeeping (spec K1, K2; auto-unshelve/unack not in the alpha) ----

    /// Delete old closed/expired and informational alerts, then expire timed-out
    /// alerts. Returns the expired ids.
    ///
    /// # Errors
    ///
    /// The database error, or `StoreError::Transition` from expiring an alert.
    pub async fn housekeeping(
        &self,
        expired_after_s: i64,
        info_after_s: i64,
    ) -> Result<Vec<String>, StoreError> {
        // K2.1: elapsed seconds since last_receive_time, compared numerically
        // with the raw timeout. A NULL timeout never expires (K2.2), 0 never
        // expires (K2.3), and any stored bigint compares without overflow.
        const EXPIRED: &str = "status <> 'expired' AND COALESCE(timeout, $1) <> 0
               AND EXTRACT(EPOCH FROM (now() - last_receive_time)) > timeout";
        #[expect(
            clippy::as_conversions,
            clippy::cast_precision_loss,
            reason = "make_interval(secs => ...) takes double precision; a retention period near 2^53 s is not a real setting"
        )]
        let (expired_secs, info_secs) = (expired_after_s as f64, info_after_s as f64);
        if expired_after_s > 0 {
            sqlx::query(
                "DELETE FROM alerts WHERE status IN ('closed','expired')
                   AND last_receive_time < now() - make_interval(secs => $1)",
            )
            .bind(expired_secs)
            .execute(&self.pool)
            .await?;
        }
        if info_after_s > 0 {
            sqlx::query("DELETE FROM alerts WHERE severity = $1 AND last_receive_time < now() - make_interval(secs => $2)")
                .bind(INFORM_SEVERITY)
                .bind(info_secs)
                .execute(&self.pool)
                .await?;
        }
        let ids: Vec<String> =
            sqlx::query_scalar(&format!("SELECT id FROM alerts WHERE {EXPIRED}"))
                .bind(self.cfg.alert_timeout)
                .fetch_all(&self.pool)
                .await?;
        for id in &ids {
            let mut tx = self.pool.begin().await?;
            // Re-check under the row lock: a receive or action since the
            // SELECT may have refreshed or changed the alert.
            let row = sqlx::query(&format!(
                "SELECT * FROM alerts WHERE id = $2 AND {EXPIRED} FOR UPDATE"
            ))
            .bind(self.cfg.alert_timeout)
            .bind(id)
            .fetch_optional(&mut *tx)
            .await?;
            if let Some(row) = row {
                let a = row_to_alert(&row)?;
                self.apply_action(&mut tx, a, "expired", "alert timeout", None, "housekeeping")
                    .await?;
            }
            tx.commit().await?;
        }
        // The response lists the ids selected before processing (K4.1).
        Ok(ids)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tags_union_keeps_first_seen_order() {
        let s = vec!["a".to_string(), "b".to_string()];
        let i = vec!["b".to_string(), "c".to_string(), "c".to_string()];
        assert_eq!(union_tags(&s, &i), vec!["a", "b", "c"]);
    }

    #[test]
    fn attributes_merge_drops_nulls() {
        let s: Map<String, Value> = serde_json::from_str(r#"{"a":1,"b":2}"#).unwrap();
        let i: Map<String, Value> = serde_json::from_str(r#"{"b":3,"a":null,"c":4}"#).unwrap();
        let m = merge_attributes(&s, &i);
        assert_eq!(Value::Object(m), serde_json::json!({"b":3,"c":4}));
    }

    #[test]
    fn identity_lock_keys_distinguish_null_customer() {
        assert_ne!(
            identity_lock_key("P", "r", None),
            identity_lock_key("P", "r", Some("n"))
        );
        assert_ne!(
            identity_lock_key("P", "r", Some("")),
            identity_lock_key("P", "r", None)
        );
        assert_eq!(
            identity_lock_key("P", "r", Some("x")),
            identity_lock_key("P", "r", Some("x"))
        );
    }

    #[test]
    fn like_prefix_escapes() {
        assert_eq!(like_prefix("ab_%"), "ab\\_\\%%");
    }
}
