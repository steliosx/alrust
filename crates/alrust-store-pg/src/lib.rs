//! PostgreSQL persistence for alrust (sqlx).
//!
//! The receive, status and action paths run inside one transaction with the
//! matched row locked (`FOR UPDATE`), and call `alrust-engine` for every
//! state decision. Behaviour follows `spec/lifecycle.md`; the known alpha
//! simplifications are listed in the crate README section of the top-level
//! README ("Alpha limitations").

pub mod catalog;
pub mod hooks;
pub mod search;
pub mod startup;

use std::sync::Arc;

use alrust_domain::{
    Alert, ApiKey, DEFAULT_PREVIOUS_SEVERITY, Heartbeat, HistoryEntry, HistoryOutput,
    INFORM_SEVERITY, NORMAL_SEVERITY, change_type, status, trend, truncate_ms,
};
use alrust_engine::{
    HistInfo, Timeouts, Transition, TransitionError, TransitionInput, action_timeout, hist_info,
    transition,
};
use chrono::{DateTime, NaiveDateTime, Utc};
use serde_json::{Map, Value};
use sqlx::postgres::{PgConnectOptions, PgPool, PgRow};
use sqlx::{Postgres, QueryBuilder, Row, Transaction};

pub use hooks::{AckIntegration, CallerFacts, HookError};
use hooks::{Gate, MULTIPLE_OWNERS, apply_writes, settle_commit};
pub use startup::{Outcome, Report as StartupReport, StartupError};

/// The search path every alrust session uses. PostgreSQL searches
/// `pg_catalog` first whenever the path does not name it, so the effective
/// order is `pg_catalog, public, pg_temp`: built-ins cannot be shadowed,
/// unqualified objects (`SQLx`'s `_sqlx_migrations`, 0001, the reference DDL)
/// are created in `public`, no other schema (a role's `"$user"` schema, say)
/// is searched, and a temporary table comes last. No spaces: `SQLx` passes
/// the value as a raw `-c search_path=...` startup option.
pub const SEARCH_PATH: &str = "public,pg_temp";

/// Pin the session settings alrust relies on: `search_path` (see
/// [`SEARCH_PATH`]) and `TimeZone=UTC`. [`Store::prepare`] refuses a
/// connection whose effective search order is not the pinned one; every
/// connection in the pool must be made with these options.
pub fn session_options(opts: PgConnectOptions) -> PgConnectOptions {
    opts.options([("search_path", SEARCH_PATH), ("TimeZone", "UTC")])
}

#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error(transparent)]
    Db(#[from] sqlx::Error),
    #[error(transparent)]
    Transition(#[from] TransitionError),
    #[error("not found")]
    NotFound,
    /// A value outside the reference column's `integer` range. The API
    /// refuses such input first; this guards the store boundary.
    #[error("{0} is outside the integer range of the reference schema")]
    OutOfRange(&'static str),
    /// An acknowledgement hook refused before `COMMIT`
    /// (spec/integration-hooks.md section 3): nothing was committed.
    #[error("{0}")]
    Hook(HookError),
    /// `COMMIT` failed with no server verdict and the owning integration's
    /// reconciliation (`commit_outcome`) failed too: the acknowledgement
    /// may or may not have been saved. Unknown, never "not saved", and
    /// never retried (section 5, commit outcome).
    #[error("{0}")]
    OutcomeUnknown(HookError),
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
/// The one SQL expression that turns a JSON `value` into the reference's
/// `text` column: strings as they are, JSON null as SQL NULL, and any other
/// JSON (a float, array or object) as PostgreSQL's `jsonb` text form
/// (ADR-0005 decision 8, option (b), DECISIONS Y38). `0002` applies the
/// same text to alpha rows and history values; a test checks the migration
/// embeds it verbatim, so the stored text is the same however it got there.
pub const VALUE_TEXT: &str = "CASE jsonb_typeof({v}) WHEN 'string' THEN ({v}) #>> '{}' WHEN 'null' THEN NULL ELSE ({v})::text END";

/// [`VALUE_TEXT`] applied to one SQL operand.
#[must_use]
pub fn value_text(operand: &str) -> String {
    VALUE_TEXT.replace("{v}", operand)
}

/// The text to store for a received `value`: strings and null need no
/// query; anything else goes through [`VALUE_TEXT`] on the same connection,
/// so the API path and `0002` produce identical text.
async fn value_to_text(
    tx: &mut Transaction<'_, Postgres>,
    v: Option<Value>,
) -> Result<Option<String>, StoreError> {
    match v {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) => Ok(Some(s)),
        Some(other) => Ok(
            sqlx::query_scalar(&format!("SELECT {}", value_text("$1::jsonb")))
                .bind(other)
                .fetch_one(&mut **tx)
                .await?,
        ),
    }
}

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
    /// As sent, after spec R0.3 (integers and booleans are already strings).
    /// A float, array or object is converted to its `jsonb` text form by
    /// [`VALUE_TEXT`] before it is written (ADR-0005 decision 8, Y38).
    pub value: Option<Value>,
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

/// How [`Store::explain_alert_reads`] explains (measurement only).
#[doc(hidden)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Explain {
    /// `EXPLAIN (ANALYZE, BUFFERS)`: the statements run.
    Analyze,
    /// `EXPLAIN (COSTS OFF)` with `enable_seqscan` off, in a transaction
    /// that is rolled back: whether any index can serve the statement.
    PlanWithoutSeqScan,
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
    /// A checked `q=` search (`search::compile`), the first condition of
    /// the reference's `WHERE` clause (`postgres/utils.py:144-155`).
    pub search: Option<search::SearchSql>,
}

/// The grouped counts of the alerts a query selects (`/alerts/count`, and
/// the totals of `/alerts`).
#[derive(Debug, Clone, PartialEq)]
pub struct AlertCounts {
    pub total: i64,
    pub severity_counts: Map<String, Value>,
    pub status_counts: Map<String, Value>,
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
    /// Acknowledgement integrations, in call order; empty unless registered.
    hooks: Vec<Arc<dyn AckIntegration>>,
}

/// An action's decisions, made before anything is written.
struct ActionPlan {
    now: DateTime<Utc>,
    t: Transition,
    hist_timeout: i64,
    kind: String,
}

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

/// The reference's composite `history` type, field for field in its
/// positional order (`schema.sql:4-15`).
#[derive(Debug, Clone, sqlx::Type)]
#[sqlx(type_name = "history")]
struct PgHistory {
    id: Option<String>,
    event: Option<String>,
    severity: Option<String>,
    status: Option<String>,
    value: Option<String>,
    text: Option<String>,
    change_type: Option<String>,
    update_time: Option<NaiveDateTime>,
    user: Option<String>,
    timeout: Option<i32>,
}

/// Columns are `timestamp without time zone` holding UTC (ADR-0005 decision 5).
fn naive(t: DateTime<Utc>) -> NaiveDateTime {
    t.naive_utc()
}

fn utc(t: NaiveDateTime) -> DateTime<Utc> {
    t.and_utc()
}

/// Checked narrowing to the reference's `integer` (ADR-0005 decision 6).
fn int(v: i64, what: &'static str) -> Result<i32, StoreError> {
    i32::try_from(v).map_err(|_| StoreError::OutOfRange(what))
}

fn null_column(table: &str, id: &str, column: &str) -> sqlx::Error {
    sqlx::Error::Decode(format!("{table} {id}: column {column} is NULL").into())
}

/// A column the reference always writes itself; a NULL there (from another
/// writer) makes the row unreadable rather than inventing a value
/// (ADR-0005 decision 7).
fn required<'r, T>(row: &'r PgRow, table: &str, id: &str, column: &str) -> Result<T, sqlx::Error>
where
    T: sqlx::Decode<'r, sqlx::Postgres> + sqlx::Type<sqlx::Postgres>,
{
    row.try_get::<Option<T>, _>(column)?
        .ok_or_else(|| null_column(table, id, column))
}

/// A column the reference's model fills with a constant through Python's
/// `or` (`models/alert.py:52-67`, `models/heartbeat.py`): NULL *and* the
/// empty string read as the constant.
fn or_default(row: &PgRow, column: &str, default: &str) -> Result<String, sqlx::Error> {
    Ok(row
        .try_get::<Option<String>, _>(column)?
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| default.to_string()))
}

fn opt_time(row: &PgRow, column: &str) -> Result<Option<DateTime<Utc>>, sqlx::Error> {
    Ok(row.try_get::<Option<NaiveDateTime>, _>(column)?.map(utc))
}

fn list(row: &PgRow, column: &str) -> Result<Vec<String>, sqlx::Error> {
    Ok(row
        .try_get::<Option<Vec<String>>, _>(column)?
        .unwrap_or_default())
}

fn object(row: &PgRow, column: &str) -> Result<Map<String, Value>, sqlx::Error> {
    Ok(match row.try_get::<Option<Value>, _>(column)? {
        Some(Value::Object(m)) => m,
        _ => Map::new(),
    })
}

fn history_from_pg(alert_id: &str, h: PgHistory) -> Result<HistoryEntry, sqlx::Error> {
    let need = |v: Option<String>, field: &str| {
        v.ok_or_else(|| null_column("alert", alert_id, &format!("history.{field}")))
    };
    Ok(HistoryEntry {
        id: need(h.id, "id")?,
        event: need(h.event, "event")?,
        severity: h.severity,
        status: h.status,
        value: h.value,
        text: h.text,
        change_type: need(h.change_type, "type")?,
        update_time: utc(h
            .update_time
            .ok_or_else(|| null_column("alert", alert_id, "history.update_time"))?),
        user: h.user,
        timeout: h.timeout.map(i64::from),
    })
}

fn history_to_pg(e: &HistoryEntry) -> Result<PgHistory, StoreError> {
    Ok(PgHistory {
        id: Some(e.id.clone()),
        event: Some(e.event.clone()),
        severity: e.severity.clone(),
        status: e.status.clone(),
        value: e.value.clone(),
        text: e.text.clone(),
        change_type: Some(e.change_type.clone()),
        update_time: Some(naive(e.update_time)),
        user: e.user.clone(),
        timeout: e.timeout.map(|t| int(t, "history timeout")).transpose()?,
    })
}

fn row_to_alert(row: &PgRow) -> Result<Alert, sqlx::Error> {
    let id: String = row.try_get("id")?;
    let history = row
        .try_get::<Option<Vec<PgHistory>>, _>("history")?
        .unwrap_or_default()
        .into_iter()
        .map(|h| history_from_pg(&id, h))
        .collect::<Result<Vec<_>, _>>()?;
    let time = |column: &str| required::<NaiveDateTime>(row, "alert", &id, column).map(utc);
    let text = |column: &str| required::<String>(row, "alert", &id, column);
    Ok(Alert {
        resource: row.try_get("resource")?,
        event: row.try_get("event")?,
        environment: or_default(row, "environment", "")?,
        severity: or_default(row, "severity", NORMAL_SEVERITY)?,
        correlate: list(row, "correlate")?,
        status: or_default(row, "status", status::OPEN)?,
        service: list(row, "service")?,
        group: or_default(row, "group", "Misc")?,
        value: row.try_get("value")?,
        text: or_default(row, "text", "")?,
        tags: list(row, "tags")?,
        attributes: object(row, "attributes")?,
        origin: text("origin")?,
        event_type: or_default(row, "type", "exceptionAlert")?,
        create_time: time("create_time")?,
        timeout: row.try_get::<Option<i32>, _>("timeout")?.map(i64::from),
        raw_data: row.try_get("raw_data")?,
        customer: row.try_get("customer")?,
        duplicate_count: row
            .try_get::<Option<i32>, _>("duplicate_count")?
            .map(i64::from),
        repeat: row.try_get("repeat")?,
        previous_severity: row.try_get("previous_severity")?,
        trend_indication: row.try_get("trend_indication")?,
        receive_time: time("receive_time")?,
        last_receive_id: row.try_get("last_receive_id")?,
        last_receive_time: opt_time(row, "last_receive_time")?,
        update_time: opt_time(row, "update_time")?,
        history,
        id,
    })
}

/// `default_timeout` replaces a NULL `timeout`, as the reference's model
/// does with `HEARTBEAT_TIMEOUT`.
fn row_to_heartbeat(row: &PgRow, default_timeout: i64) -> Result<Heartbeat, sqlx::Error> {
    let id: String = row.try_get("id")?;
    let time = |column: &str| required::<NaiveDateTime>(row, "heartbeat", &id, column).map(utc);
    Ok(Heartbeat {
        origin: row.try_get("origin")?,
        tags: list(row, "tags")?,
        attributes: object(row, "attributes")?,
        event_type: or_default(row, "type", "Heartbeat")?,
        create_time: time("create_time")?,
        timeout: row
            .try_get::<Option<i32>, _>("timeout")?
            .map_or(default_timeout, i64::from),
        receive_time: time("receive_time")?,
        customer: row.try_get("customer")?,
        id,
    })
}

/// NULL `expire_time` or `scopes` stay `None`: such a key is inactive and
/// shown as such, never given a computed default (ADR-0005 decision 7).
fn row_to_key(row: &PgRow) -> Result<ApiKey, sqlx::Error> {
    Ok(ApiKey {
        id: row.try_get("id")?,
        key: row.try_get("key")?,
        user: row.try_get("user")?,
        scopes: row.try_get("scopes")?,
        text: or_default(row, "text", "")?,
        expire_time: row
            .try_get::<Option<NaiveDateTime>, _>("expire_time")?
            .map(utc),
        count: row.try_get::<Option<i64>, _>("count")?.unwrap_or(0),
        last_used_time: row
            .try_get::<Option<NaiveDateTime>, _>("last_used_time")?
            .map(utc),
        customer: row.try_get("customer")?,
    })
}

/// The alert columns an operation may write (see `Store::update_alert`).
#[derive(Debug, Clone, Copy)]
enum Col {
    Event,
    Severity,
    Status,
    Service,
    Value,
    Text,
    CreateTime,
    Timeout,
    RawData,
    DuplicateCount,
    Repeat,
    PreviousSeverity,
    TrendIndication,
    ReceiveTime,
    LastReceiveId,
    LastReceiveTime,
    Tags,
    Attributes,
    UpdateTime,
    History,
}

impl Col {
    fn name(self) -> &'static str {
        match self {
            Col::Event => "event",
            Col::Severity => "severity",
            Col::Status => "status",
            Col::Service => "service",
            Col::Value => "value",
            Col::Text => "text",
            Col::CreateTime => "create_time",
            Col::Timeout => "timeout",
            Col::RawData => "raw_data",
            Col::DuplicateCount => "duplicate_count",
            Col::Repeat => "repeat",
            Col::PreviousSeverity => "previous_severity",
            Col::TrendIndication => "trend_indication",
            Col::ReceiveTime => "receive_time",
            Col::LastReceiveId => "last_receive_id",
            Col::LastReceiveTime => "last_receive_time",
            Col::Tags => "tags",
            Col::Attributes => "attributes",
            Col::UpdateTime => "update_time",
            Col::History => "history",
        }
    }
}

/// `dedup_alert` (`postgres/base.py:218-226`).
/// Every `alerts` column of the reference layout but `history`, which a list
/// without `show-history` would read and then discard.
/// One alert whose `id` equals `$1` exactly, within the customers `$2`
/// (`NULL` for no restriction). Served by the primary key `alerts_pkey`.
/// Public so that tests can check the plan of this exact text.
pub const ALERT_BY_EXACT_ID: &str =
    "SELECT * FROM alerts WHERE id = $1 AND ($2::text[] IS NULL OR customer = ANY($2))";

/// [`ALERT_BY_EXACT_ID`], locking the row.
pub const ALERT_BY_EXACT_ID_FOR_UPDATE: &str =
    "SELECT * FROM alerts WHERE id = $1 AND ($2::text[] IS NULL OR customer = ANY($2)) FOR UPDATE";

/// The reference-compatible match: an `id` or `last_receive_id` that starts
/// with `$1` (a `like_prefix` pattern), within the customers `$2`. A plain
/// B-tree cannot serve it under a non-`C` collation, so it scans `alerts`.
const ALERT_BY_PREFIX: &str = "SELECT * FROM alerts WHERE (id LIKE $1 OR last_receive_id LIKE $1)
               AND ($2::text[] IS NULL OR customer = ANY($2)) LIMIT 1";

const ALERT_BY_PREFIX_FOR_UPDATE: &str =
    "SELECT * FROM alerts WHERE (id LIKE $1 OR last_receive_id LIKE $1)
               AND ($2::text[] IS NULL OR customer = ANY($2)) LIMIT 1 FOR UPDATE";

const ALERTS_WITHOUT_HISTORY: &str = "SELECT id, resource, event, environment, severity,
    correlate, status, service, \"group\", value, text, tags, attributes, origin, type,
    create_time, timeout, raw_data, customer, duplicate_count, repeat, previous_severity,
    trend_indication, receive_time, last_receive_id, last_receive_time, update_time,
    NULL::history[] AS history FROM alerts";

const DEDUP_COLS: &[Col] = &[
    Col::Status,
    Col::Service,
    Col::Value,
    Col::Text,
    Col::Timeout,
    Col::RawData,
    Col::Repeat,
    Col::LastReceiveId,
    Col::LastReceiveTime,
    Col::Tags,
    Col::Attributes,
    Col::DuplicateCount,
    Col::UpdateTime,
    Col::History,
];

/// `correlate_alert` (`postgres/base.py:244-252`).
const CORRELATE_COLS: &[Col] = &[
    Col::Event,
    Col::Severity,
    Col::Status,
    Col::Service,
    Col::Value,
    Col::Text,
    Col::CreateTime,
    Col::Timeout,
    Col::RawData,
    Col::DuplicateCount,
    Col::Repeat,
    Col::PreviousSeverity,
    Col::TrendIndication,
    Col::ReceiveTime,
    Col::LastReceiveId,
    Col::LastReceiveTime,
    Col::Tags,
    Col::Attributes,
    Col::UpdateTime,
    Col::History,
];

/// `set_alert`, used by actions (`postgres/base.py:279-289`).
const ACTION_COLS: &[Col] = &[
    Col::Severity,
    Col::Status,
    Col::Tags,
    Col::Attributes,
    Col::Timeout,
    Col::PreviousSeverity,
    Col::UpdateTime,
    Col::History,
];

/// `set_status` (`postgres/base.py:303-310`).
const STATUS_COLS: &[Col] = &[Col::Status, Col::Timeout, Col::UpdateTime, Col::History];

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
        Store {
            pool,
            cfg,
            hooks: Vec::new(),
        }
    }

    /// This store with these acknowledgement integrations, called in this
    /// order (spec/integration-hooks.md section 3).
    #[must_use]
    pub fn with_integrations(mut self, hooks: Vec<Arc<dyn AckIntegration>>) -> Self {
        self.hooks = hooks;
        self
    }

    pub fn config(&self) -> &StoreConfig {
        &self.cfg
    }

    /// Migrate, adopt or refuse the database (ADR-0005 decision 4).
    ///
    /// # Errors
    ///
    /// A refused layout, a failed migration or copy check, or the database
    /// error, as `StartupError` says.
    pub async fn prepare(&self) -> Result<StartupReport, StartupError> {
        startup::prepare(&self.pool).await
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

    async fn insert_alert(tx: &mut Transaction<'_, Postgres>, a: &Alert) -> Result<(), StoreError> {
        let history = a
            .history
            .iter()
            .map(history_to_pg)
            .collect::<Result<Vec<_>, _>>()?;
        let timeout = a.timeout.map(|t| int(t, "timeout")).transpose()?;
        let duplicate_count = a
            .duplicate_count
            .map(|c| int(c, "duplicateCount"))
            .transpose()?;
        sqlx::query(
            r#"INSERT INTO alerts (id, resource, event, environment, severity, correlate, status, service, "group",
                value, text, tags, attributes, origin, type, create_time, timeout, raw_data, customer,
                duplicate_count, repeat, previous_severity, trend_indication, receive_time, last_receive_id,
                last_receive_time, update_time, history)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28)"#,
        )
        .bind(&a.id)
        .bind(&a.resource)
        .bind(&a.event)
        .bind(&a.environment)
        .bind(&a.severity)
        .bind(&a.correlate)
        .bind(&a.status)
        .bind(&a.service)
        .bind(&a.group)
        .bind(&a.value)
        .bind(&a.text)
        .bind(&a.tags)
        .bind(Value::Object(a.attributes.clone()))
        .bind(&a.origin)
        .bind(&a.event_type)
        .bind(naive(a.create_time))
        .bind(timeout)
        .bind(&a.raw_data)
        .bind(&a.customer)
        .bind(duplicate_count)
        .bind(a.repeat)
        .bind(&a.previous_severity)
        .bind(&a.trend_indication)
        .bind(naive(a.receive_time))
        .bind(&a.last_receive_id)
        .bind(a.last_receive_time.map(naive))
        .bind(a.update_time.map(naive))
        .bind(history)
        .execute(&mut **tx)
        .await?;
        Ok(())
    }

    /// Update only the given columns of a locked alert, as the reference's
    /// operation-specific UPDATEs do (`postgres/base.py`: `dedup_alert`,
    /// `correlate_alert`, `set_alert`, `set_status`, `tag_alert`,
    /// `update_tags`, `update_attributes`). Every other column keeps its
    /// stored value, including a NULL or empty string that reads hydrated as
    /// a default (ADR5-EDIT-PRESERVE-1).
    async fn update_alert(
        tx: &mut Transaction<'_, Postgres>,
        a: &Alert,
        cols: &[Col],
    ) -> Result<(), StoreError> {
        let mut qb: QueryBuilder<'_, Postgres> = QueryBuilder::new("UPDATE alerts SET ");
        for (i, col) in cols.iter().enumerate() {
            if i > 0 {
                qb.push(", ");
            }
            qb.push(col.name()).push(" = ");
            match col {
                Col::Event => qb.push_bind(a.event.clone()),
                Col::Severity => qb.push_bind(a.severity.clone()),
                Col::Status => qb.push_bind(a.status.clone()),
                Col::Service => qb.push_bind(a.service.clone()),
                Col::Value => qb.push_bind(a.value.clone()),
                Col::Text => qb.push_bind(a.text.clone()),
                Col::CreateTime => qb.push_bind(naive(a.create_time)),
                Col::Timeout => qb.push_bind(a.timeout.map(|t| int(t, "timeout")).transpose()?),
                Col::RawData => qb.push_bind(a.raw_data.clone()),
                // A duplicate past the integer maximum fails here, before any
                // write, and the caller's transaction rolls back (decision 6).
                Col::DuplicateCount => qb.push_bind(
                    a.duplicate_count
                        .map(|c| int(c, "duplicateCount"))
                        .transpose()?,
                ),
                Col::Repeat => qb.push_bind(a.repeat),
                Col::PreviousSeverity => qb.push_bind(a.previous_severity.clone()),
                Col::TrendIndication => qb.push_bind(a.trend_indication.clone()),
                Col::ReceiveTime => qb.push_bind(naive(a.receive_time)),
                Col::LastReceiveId => qb.push_bind(a.last_receive_id.clone()),
                Col::LastReceiveTime => qb.push_bind(a.last_receive_time.map(naive)),
                Col::Tags => qb.push_bind(a.tags.clone()),
                Col::Attributes => qb.push_bind(Value::Object(a.attributes.clone())),
                Col::UpdateTime => qb.push_bind(a.update_time.map(naive)),
                Col::History => qb.push_bind(
                    a.history
                        .iter()
                        .map(history_to_pg)
                        .collect::<Result<Vec<_>, _>>()?,
                ),
            };
        }
        qb.push(" WHERE id = ").push_bind(a.id.clone());
        qb.build().execute(&mut **tx).await?;
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
    pub async fn receive(
        &self,
        n: NewAlert,
        login: &str,
    ) -> Result<(Alert, ReceiveKind), StoreError> {
        let caller = CallerFacts {
            login: user_of(login),
            ..CallerFacts::default()
        };
        self.receive_with(n, &caller).await
    }

    /// `receive` with the caller's facts. Each registered integration's
    /// `pre_receive` runs first, in order, inside the transaction and before
    /// anything is looked up or written (spec/integration-hooks.md 4.3);
    /// without one, this is exactly the unhooked path.
    ///
    /// # Errors
    ///
    /// As `receive`, and `StoreError::Hook` when a hook refuses; then
    /// nothing is written.
    #[expect(
        clippy::too_many_lines,
        reason = "the three receive outcomes share one locked transaction, kept in one place"
    )]
    pub async fn receive_with(
        &self,
        n: NewAlert,
        caller: &CallerFacts,
    ) -> Result<(Alert, ReceiveKind), StoreError> {
        let login = caller.login.as_deref().unwrap_or("");
        let mut tx = self.pool.begin().await?;
        let mut n = n;
        for h in &self.hooks {
            h.pre_receive(&mut tx, caller, &mut n)
                .await
                .map_err(StoreError::Hook)?;
        }
        let value = value_to_text(&mut tx, n.value.take()).await?;
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
                a.update_time = Some(now);
                Some(Self::receive_entry(
                    &n,
                    value.as_deref(),
                    &t.status,
                    change_type::STATUS,
                    login,
                ))
            } else if self.cfg.history_on_value_change
                && Some(&value) != info.current_value.as_ref()
            {
                Some(Self::receive_entry(
                    &n,
                    value.as_deref(),
                    &current,
                    change_type::VALUE,
                    login,
                ))
            } else {
                None
            };
            a.status = t.status;
            a.service = n.service;
            a.value = value;
            a.text = n.text;
            a.timeout = Some(n.timeout);
            a.raw_data = n.raw_data;
            a.tags = union_tags(&a.tags, &n.tags);
            a.attributes = merge_attributes(&a.attributes, &n.attributes);
            // The reference's `duplicate_count + 1` leaves a NULL count NULL.
            a.duplicate_count = a.duplicate_count.map(|c| c + 1);
            a.repeat = Some(true);
            a.last_receive_id = Some(n.id);
            a.last_receive_time = Some(now);
            if let Some(e) = entry {
                self.push_history(&mut a.history, e);
            }
            Self::update_alert(&mut tx, &a, DEDUP_COLS).await?;
            tx.commit().await?;
            return Ok((a, ReceiveKind::Duplicate));
        }

        // When a same-event row and a row correlating this event both match,
        // the same-event row wins: rewriting the other row's event to this
        // one would violate the unique index (ADR-0005, R2.3). The reference
        // fails in that case; alrust picks one row deterministically.
        let corr = sqlx::query(
            "SELECT * FROM alerts WHERE environment=$1 AND resource=$2
               AND ((event=$3 AND severity<>$4) OR (event<>$3 AND $3=ANY(correlate)))
               AND customer IS NOT DISTINCT FROM $5
               ORDER BY (event=$3) DESC LIMIT 1 FOR UPDATE",
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
                a.update_time = Some(now);
            }
            let entry = Self::receive_entry(
                &n,
                value.as_deref(),
                &t.status,
                change_type::SEVERITY,
                login,
            );
            a.trend_indication = Some(trend(&previous_severity, &n.severity).to_string());
            a.previous_severity = Some(previous_severity);
            a.event = n.event;
            a.severity = n.severity;
            a.status = t.status;
            a.service = n.service;
            a.value = value;
            a.text = n.text;
            a.timeout = Some(n.timeout);
            a.raw_data = n.raw_data;
            a.create_time = n.create_time;
            a.duplicate_count = Some(0);
            a.repeat = Some(false);
            a.receive_time = now;
            a.last_receive_id = Some(n.id);
            a.last_receive_time = Some(now);
            a.tags = union_tags(&a.tags, &n.tags);
            a.attributes = merge_attributes(&a.attributes, &n.attributes);
            self.push_history(&mut a.history, entry);
            Self::update_alert(&mut tx, &a, CORRELATE_COLS).await?;
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
        let entry = Self::receive_entry(&n, value.as_deref(), &t.status, change_type::NEW, login);
        let mut attributes = n.attributes.clone();
        attributes.retain(|_, v| !v.is_null());
        let a = Alert {
            trend_indication: Some(trend(DEFAULT_PREVIOUS_SEVERITY, &n.severity).to_string()),
            previous_severity: Some(DEFAULT_PREVIOUS_SEVERITY.to_string()),
            status: t.status,
            duplicate_count: Some(0),
            repeat: Some(false),
            receive_time: now,
            last_receive_id: Some(n.id.clone()),
            last_receive_time: Some(now),
            update_time: Some(now),
            history: vec![entry],
            id: n.id,
            resource: n.resource,
            event: n.event,
            environment: n.environment,
            severity: n.severity,
            correlate: n.correlate,
            service: n.service,
            group: n.group,
            value,
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
        Self::insert_alert(&mut tx, &a).await?;
        tx.commit().await?;
        Ok((a, ReceiveKind::Created))
    }

    fn receive_entry(
        n: &NewAlert,
        value: Option<&str>,
        status: &str,
        kind: &str,
        login: &str,
    ) -> HistoryEntry {
        HistoryEntry {
            id: n.id.clone(),
            event: n.event.clone(),
            severity: Some(n.severity.clone()),
            status: Some(status.to_string()),
            value: value.map(str::to_string),
            text: Some(n.text.clone()),
            change_type: kind.to_string(),
            // Receive entries carry the client's createTime (spec R4.2, R5.4, R6.4).
            update_time: n.create_time,
            user: user_of(login),
            timeout: Some(n.timeout),
        }
    }

    /// Locks the alert `id` names: the row whose `id` equals it exactly when
    /// there is one (an index lookup), else the reference's prefix match on
    /// `id` or `last_receive_id`. The reference returns any one matching row
    /// (no `ORDER BY`), and an exact match is always one of them, so taking
    /// it first stays within the reference's results.
    async fn lock_alert(
        tx: &mut Transaction<'_, Postgres>,
        id: &str,
        customers: Option<&[String]>,
    ) -> Result<Alert, StoreError> {
        let exact = sqlx::query(ALERT_BY_EXACT_ID_FOR_UPDATE)
            .bind(id)
            .bind(customers)
            .fetch_optional(&mut **tx)
            .await?;
        let row = match exact {
            Some(row) => row,
            None => sqlx::query(ALERT_BY_PREFIX_FOR_UPDATE)
                .bind(like_prefix(id))
                .bind(customers)
                .fetch_optional(&mut **tx)
                .await?
                .ok_or(StoreError::NotFound)?,
        };
        Ok(row_to_alert(&row)?)
    }

    /// Look an alert up by id or id prefix, or by `lastReceiveId` (spec C17).
    /// An exact id is looked up first (see `lock_alert`).
    ///
    /// # Errors
    ///
    /// `StoreError::NotFound` when nothing matches, otherwise the database error.
    pub async fn get_alert(
        &self,
        id: &str,
        customers: Option<&[String]>,
    ) -> Result<Alert, StoreError> {
        let exact = sqlx::query(ALERT_BY_EXACT_ID)
            .bind(id)
            .bind(customers)
            .fetch_optional(&self.pool)
            .await?;
        let row = match exact {
            Some(row) => row,
            None => sqlx::query(ALERT_BY_PREFIX)
                .bind(like_prefix(id))
                .bind(customers)
                .fetch_optional(&self.pool)
                .await?
                .ok_or(StoreError::NotFound)?,
        };
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
        a.update_time = Some(now);
        a.timeout = timeout;
        self.push_history(&mut a.history, entry);
        Self::update_alert(&mut tx, &a, STATUS_COLS).await?;
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
        // Only the edited column is written (`tag_alert`, `update_tags`,
        // `update_attributes`), so no other stored value changes.
        let col = match &edit {
            AlertEdit::Tag(_) | AlertEdit::Untag(_) => Col::Tags,
            AlertEdit::Attributes(_) => Col::Attributes,
        };
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
        Self::update_alert(&mut tx, &a, &[col]).await?;
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
        let caller = CallerFacts {
            login: user_of(login),
            customers: customers.map(<[String]>::to_vec),
            ..CallerFacts::default()
        };
        self.take_action_with(id, customers, act, text, timeout, &caller)
            .await
    }

    /// `take_action` with the caller's facts, which the acknowledgement
    /// hooks receive (spec/integration-hooks.md sections 2 and 3). The alert
    /// lookup uses `customers`, the caller's own customer filter, never the
    /// facts' customer list: for an operator that list is the token's
    /// hook-only claim (spec/oidc-caller-facts.md section 5). Without a
    /// registered integration, or for any action but `ack`, this is exactly
    /// the unhooked path.
    ///
    /// # Errors
    ///
    /// As `take_action`; `StoreError::Hook` when a hook refuses before
    /// `COMMIT` (then nothing is committed); and, when `COMMIT` fails with
    /// no server verdict, whatever the owner's `commit_outcome` settles:
    /// success, the original database error, or `StoreError::OutcomeUnknown`.
    /// A database error from such a `COMMIT` leaves the outcome unknown, as
    /// it always has.
    pub async fn take_action_with(
        &self,
        id: &str,
        customers: Option<&[String]>,
        act: &str,
        text: &str,
        timeout: Option<i64>,
        caller: &CallerFacts,
    ) -> Result<Alert, StoreError> {
        let login = caller.login.as_deref().unwrap_or("");
        let mut tx = self.pool.begin().await?;
        if act != "ack" || self.hooks.is_empty() {
            let a = Self::lock_alert(&mut tx, id, customers).await?;
            let a = self
                .apply_action(&mut tx, a, act, text, timeout, login)
                .await?;
            tx.commit().await?;
            return Ok(a);
        }
        // Section 2, prerequisite 2: one READ COMMITTED transaction, the
        // alert locked before every hook call but pre_lookup.
        sqlx::query("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            .execute(&mut *tx)
            .await?;
        for h in &self.hooks {
            h.pre_lookup(&mut tx, caller)
                .await
                .map_err(StoreError::Hook)?;
        }
        let a = Self::lock_alert(&mut tx, id, customers).await?;
        let mut owner: Option<&Arc<dyn AckIntegration>> = None;
        for h in &self.hooks {
            if h.claims(&mut tx, &a).await.map_err(StoreError::Hook)? {
                if owner.is_some() {
                    return Err(StoreError::Hook(HookError::new(503, MULTIPLE_OWNERS)));
                }
                owner = Some(h);
            }
        }
        let Some(owner) = owner else {
            // Unclaimed: exactly as without hooks.
            let a = self
                .apply_action(&mut tx, a, act, text, timeout, login)
                .await?;
            tx.commit().await?;
            return Ok(a);
        };
        let plan = self.plan_action(&a, act, timeout)?;
        let mut a = a;
        let mut text = text.to_owned();
        match owner
            .gate(&mut tx, &a, caller, &plan.t)
            .await
            .map_err(StoreError::Hook)?
        {
            Gate::Skip { attributes } => apply_writes(&mut a.attributes, &attributes),
            Gate::Proceed => {
                let staged = owner
                    .stage(&mut tx, &a, caller, &text)
                    .await
                    .map_err(StoreError::Hook)?;
                // Staging SQL may itself have changed the row's attributes:
                // start from the locked row's, not the in-memory copy.
                a.attributes = Self::locked_attributes(&mut tx, &a.id).await?;
                apply_writes(&mut a.attributes, &staged.attributes);
                if let Some(replacement) = staged.text {
                    text = replacement;
                }
            }
        }
        let (a, entry) = self.finish_action(&mut tx, a, plan, &text, login).await?;
        owner
            .pre_commit(&mut tx, &a, &entry)
            .await
            .map_err(StoreError::Hook)?;
        let commit = tx.commit().await;
        settle_commit(commit, &**owner, &self.pool, a, &entry).await
    }

    /// The locked row's attributes, read again inside the transaction.
    async fn locked_attributes(
        tx: &mut Transaction<'_, Postgres>,
        id: &str,
    ) -> Result<Map<String, Value>, StoreError> {
        let row = sqlx::query("SELECT attributes FROM alerts WHERE id = $1")
            .bind(id)
            .fetch_one(&mut **tx)
            .await?;
        Ok(object(&row, "attributes")?)
    }

    async fn apply_action(
        &self,
        tx: &mut Transaction<'_, Postgres>,
        a: Alert,
        act: &str,
        text: &str,
        timeout: Option<i64>,
        login: &str,
    ) -> Result<Alert, StoreError> {
        let plan = self.plan_action(&a, act, timeout)?;
        Ok(self.finish_action(tx, a, plan, text, login).await?.0)
    }

    fn plan_action(
        &self,
        a: &Alert,
        act: &str,
        timeout: Option<i64>,
    ) -> Result<ActionPlan, StoreError> {
        let now = truncate_ms(Utc::now());
        let info: HistInfo = hist_info(&identity_history(a, &a.event), Some(act));
        let t = transition(TransitionInput {
            severity: &a.severity,
            alert_status: &a.status,
            // NULL is the engine's default (`indeterminate`), as in the reference.
            previous_severity: a.previous_severity.as_deref(),
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
        Ok(ActionPlan {
            now,
            t,
            hist_timeout,
            kind: kind.to_string(),
        })
    }

    /// Writes the planned action: one history entry and one `UPDATE`.
    async fn finish_action(
        &self,
        tx: &mut Transaction<'_, Postgres>,
        mut a: Alert,
        plan: ActionPlan,
        text: &str,
        login: &str,
    ) -> Result<(Alert, HistoryEntry), StoreError> {
        let ActionPlan {
            now,
            t,
            hist_timeout,
            kind,
        } = plan;
        let entry = HistoryEntry {
            id: a.id.clone(),
            event: a.event.clone(),
            severity: Some(t.severity.clone()),
            status: Some(t.status.clone()),
            value: a.value.clone(),
            text: Some(text.to_string()),
            change_type: kind,
            update_time: now,
            user: user_of(login),
            timeout: Some(hist_timeout),
        };
        if t.severity != a.severity {
            a.previous_severity = Some(std::mem::replace(&mut a.severity, t.severity));
        }
        a.status = t.status;
        a.update_time = Some(now);
        // The row keeps the hydrated model timeout: NULL becomes ALERT_TIMEOUT (spec A4.1).
        a.timeout = Some(a.timeout.unwrap_or(self.cfg.alert_timeout));
        a.attributes.retain(|_, v| !v.is_null());
        self.push_history(&mut a.history, entry.clone());
        Self::update_alert(tx, &a, ACTION_COLS).await?;
        Ok((a, entry))
    }

    /// `DELETE /alert/<id>`: one alert by id or id prefix, or by `lastReceiveId`.
    /// An exact id is deleted first (see `lock_alert`).
    ///
    /// # Errors
    ///
    /// `StoreError::NotFound` when nothing matches, otherwise the database error.
    pub async fn delete_alert(
        &self,
        id: &str,
        customers: Option<&[String]>,
    ) -> Result<(), StoreError> {
        let exact = sqlx::query(
            "DELETE FROM alerts WHERE id = $1 AND ($2::text[] IS NULL OR customer = ANY($2))",
        )
        .bind(id)
        .bind(customers)
        .execute(&self.pool)
        .await?;
        if exact.rows_affected() > 0 {
            return Ok(());
        }
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
        if let Some(search) = &q.search {
            qb.push(" AND (");
            search.push_to(qb);
            qb.push(")");
        }
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

    /// `GET /alerts`: newest `lastReceiveTime` first. With
    /// `HistoryOutput::Omit` the `history` column is not read, and every
    /// returned alert has an empty history.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn find_alerts(
        &self,
        q: &AlertQuery,
        page: i64,
        page_size: i64,
        history: HistoryOutput,
    ) -> Result<AlertPage, StoreError> {
        let AlertCounts {
            total,
            severity_counts,
            status_counts,
        } = self.count_alerts(q).await?;
        let mut sel = Self::page_query("", q, history, page, page_size);
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

    /// The page statement of `find_alerts`, after `prefix` (empty, or an
    /// `EXPLAIN` for [`Store::explain_alert_reads`]).
    fn page_query<'q>(
        prefix: &str,
        q: &'q AlertQuery,
        history: HistoryOutput,
        page: i64,
        page_size: i64,
    ) -> QueryBuilder<'q, Postgres> {
        let mut sel = QueryBuilder::new(format!(
            "{prefix}{}",
            match history {
                HistoryOutput::Include => "SELECT * FROM alerts",
                HistoryOutput::Omit => ALERTS_WITHOUT_HISTORY,
            }
        ));
        Self::push_filters(&mut sel, q);
        sel.push(" ORDER BY last_receive_time DESC, id LIMIT ")
            .push_bind(page_size)
            .push(" OFFSET ")
            .push_bind((page - 1) * page_size);
        sel
    }

    /// The one grouped-count statement of a query, with the same filters as
    /// the alert list.
    fn counts_query(q: &AlertQuery) -> QueryBuilder<'_, Postgres> {
        Self::counts_query_after("", q)
    }

    fn counts_query_after<'q>(prefix: &str, q: &'q AlertQuery) -> QueryBuilder<'q, Postgres> {
        let mut counts = QueryBuilder::new(format!(
            "{prefix}SELECT severity, status, count(*) AS n FROM alerts"
        ));
        Self::push_filters(&mut counts, q);
        counts.push(" GROUP BY severity, status");
        counts
    }

    /// Measurement only (`tests/search_measure.rs`; not routed): the plans of
    /// the two statements `find_alerts` runs for page 1 without history, the
    /// grouped counts and then the page, with the same filters and binds.
    ///
    /// # Errors
    ///
    /// The database error.
    #[doc(hidden)]
    pub async fn explain_alert_reads(
        &self,
        q: &AlertQuery,
        page_size: i64,
        mode: Explain,
    ) -> Result<[String; 2], StoreError> {
        let prefix = match mode {
            Explain::Analyze => "EXPLAIN (ANALYZE, BUFFERS) ",
            Explain::PlanWithoutSeqScan => "EXPLAIN (COSTS OFF) ",
        };
        let mut tx = self.pool.begin().await?;
        if mode == Explain::PlanWithoutSeqScan {
            sqlx::query("SET LOCAL enable_seqscan = off")
                .execute(&mut *tx)
                .await?;
        }
        let mut out = [String::new(), String::new()];
        let mut counts = Self::counts_query_after(prefix, q);
        let mut page = Self::page_query(prefix, q, HistoryOutput::Omit, 1, page_size);
        for (slot, qb) in out.iter_mut().zip([&mut counts, &mut page]) {
            let rows = qb.build().fetch_all(&mut *tx).await?;
            let lines = rows
                .iter()
                .map(|r| r.try_get::<String, _>(0))
                .collect::<Result<Vec<_>, _>>()?;
            *slot = lines.join("\n");
        }
        tx.rollback().await?;
        Ok(out)
    }

    /// `GET /alerts/count`: the total and the per-severity and per-status
    /// counts of the alerts `q` selects. One statement; no alert row is read.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn count_alerts(&self, q: &AlertQuery) -> Result<AlertCounts, StoreError> {
        let rows = Self::counts_query(q).build().fetch_all(&self.pool).await?;
        let mut severity_counts = Map::new();
        let mut status_counts = Map::new();
        let mut total = 0i64;
        for r in rows {
            let sev = or_default(&r, "severity", NORMAL_SEVERITY)?;
            let st = or_default(&r, "status", status::OPEN)?;
            let n: i64 = r.try_get("n")?;
            total += n;
            let e = severity_counts.entry(sev).or_insert(Value::from(0));
            *e = Value::from(e.as_i64().unwrap_or(0) + n);
            let e = status_counts.entry(st).or_insert(Value::from(0));
            *e = Value::from(e.as_i64().unwrap_or(0) + n);
        }
        Ok(AlertCounts {
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
        .bind(naive(create_time))
        .bind(int(timeout, "timeout")?)
        .bind(naive(now))
        .bind(customer)
        .fetch_one(&self.pool)
        .await?;
        Ok(row_to_heartbeat(&row, self.cfg.alert_timeout)?)
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
        Ok(row_to_heartbeat(&row, self.cfg.alert_timeout)?)
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
            .map(|r| row_to_heartbeat(r, self.cfg.alert_timeout))
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
        .bind(k.expire_time.map(naive))
        .bind(&k.customer)
        .fetch_one(&self.pool)
        .await?;
        Ok(row_to_key(&row)?)
    }

    /// Ensure the key with this id holds exactly this value, scopes and
    /// expiry. A different value stored under the same id is replaced, so a
    /// changed bootstrap value revokes the previous one. A row that already
    /// holds this value under another id is adopted under this id.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn ensure_key(&self, k: &ApiKey) -> Result<(), StoreError> {
        let mut tx = self.pool.begin().await?;
        sqlx::query("DELETE FROM keys WHERE id = $1 AND key <> $2")
            .bind(&k.id)
            .bind(&k.key)
            .execute(&mut *tx)
            .await?;
        sqlx::query(
            r#"INSERT INTO keys (id, key, "user", scopes, text, expire_time, count, last_used_time, customer)
               VALUES ($1,$2,$3,$4,$5,$6,0,NULL,$7)
               ON CONFLICT (key) DO UPDATE SET id=EXCLUDED.id, scopes=EXCLUDED.scopes,
                 expire_time=EXCLUDED.expire_time"#,
        )
        .bind(&k.id)
        .bind(&k.key)
        .bind(&k.user)
        .bind(&k.scopes)
        .bind(&k.text)
        .bind(k.expire_time.map(naive))
        .bind(&k.customer)
        .execute(&mut *tx)
        .await?;
        tx.commit().await?;
        Ok(())
    }

    /// Look up an active key and record its use. A key with a NULL expiry
    /// or NULL scopes never matches, so its count and last use are unchanged
    /// (ADR-0005 decision 7).
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn use_key(&self, key: &str) -> Result<Option<ApiKey>, StoreError> {
        let row = sqlx::query(
            "UPDATE keys SET count = count + 1, last_used_time = $2
              WHERE key = $1 AND expire_time > $2 AND scopes IS NOT NULL RETURNING *",
        )
        .bind(key)
        .bind(naive(Utc::now()))
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
        // with the raw timeout. A NULL timeout never expires (K2.2), and 0
        // never expires (K2.3).
        const EXPIRED: &str = "status <> 'expired' AND COALESCE(timeout, $1) <> 0
               AND EXTRACT(EPOCH FROM ($2 - last_receive_time)) > timeout";
        // `now` is bound, never SQL now(): the columns hold UTC without a
        // time zone, and now() would be converted by the session TimeZone.
        let now = naive(Utc::now());
        #[expect(
            clippy::as_conversions,
            clippy::cast_precision_loss,
            reason = "make_interval(secs => ...) takes double precision; a retention period near 2^53 s is not a real setting"
        )]
        let (expired_secs, info_secs) = (expired_after_s as f64, info_after_s as f64);
        if expired_after_s > 0 {
            sqlx::query(
                "DELETE FROM alerts WHERE status IN ('closed','expired')
                   AND last_receive_time < $2 - make_interval(secs => $1)",
            )
            .bind(expired_secs)
            .bind(now)
            .execute(&self.pool)
            .await?;
        }
        if info_after_s > 0 {
            sqlx::query("DELETE FROM alerts WHERE severity = $1 AND last_receive_time < $3 - make_interval(secs => $2)")
                .bind(INFORM_SEVERITY)
                .bind(info_secs)
                .bind(now)
                .execute(&self.pool)
                .await?;
        }
        let ids: Vec<String> =
            sqlx::query_scalar(&format!("SELECT id FROM alerts WHERE {EXPIRED}"))
                .bind(self.cfg.alert_timeout)
                .bind(now)
                .fetch_all(&self.pool)
                .await?;
        for id in &ids {
            let mut tx = self.pool.begin().await?;
            // Re-check under the row lock: a receive or action since the
            // SELECT may have refreshed or changed the alert.
            let row = sqlx::query(&format!(
                "SELECT * FROM alerts WHERE id = $3 AND {EXPIRED} FOR UPDATE"
            ))
            .bind(self.cfg.alert_timeout)
            .bind(now)
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

    /// `/alerts/count` runs only the grouped count: one statement, the list's
    /// filters, no row fetch, ordering or paging.
    #[test]
    fn the_count_path_is_one_grouped_statement_with_the_list_filters() {
        let q = AlertQuery {
            events: vec!["HttpError".into()],
            not_statuses: vec!["closed".into()],
            tags: vec!["t".into()],
            customers: Some(vec!["acme".into()]),
            ..AlertQuery::default()
        };
        let mut list = QueryBuilder::new("");
        Store::push_filters(&mut list, &q);
        let counts = Store::counts_query(&q);
        assert_eq!(
            counts.sql(),
            format!(
                "SELECT severity, status, count(*) AS n FROM alerts{} GROUP BY severity, status",
                list.sql()
            )
        );
        assert_eq!(
            list.sql(),
            " WHERE true AND event = ANY($1) AND NOT (status = ANY($2)) AND tags @> $3 AND customer = ANY($4)"
        );
        for absent in ["ORDER BY", "LIMIT", "OFFSET", "SELECT *"] {
            assert!(!counts.sql().contains(absent), "{absent}");
        }
    }

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
