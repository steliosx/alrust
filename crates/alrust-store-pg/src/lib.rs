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
    /// `None` means no customer restriction.
    pub customers: Option<Vec<String>>,
}

pub struct AlertPage {
    pub alerts: Vec<Alert>,
    pub total: i64,
    pub severity_counts: Map<String, Value>,
    pub status_counts: Map<String, Value>,
}

#[derive(Clone)]
pub struct Store {
    pool: PgPool,
    cfg: StoreConfig,
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

    pub async fn migrate(&self) -> Result<(), MigrateError> {
        MIGRATOR.run(&self.pool).await
    }

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
        insert: bool,
    ) -> Result<(), sqlx::Error> {
        let sql = if insert {
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
    pub async fn receive(
        &self,
        n: NewAlert,
        login: &str,
    ) -> Result<(Alert, ReceiveKind), StoreError> {
        let mut tx = self.pool.begin().await?;
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
                Some(self.receive_entry(&n, &t.status, change_type::STATUS, login))
            } else if self.cfg.history_on_value_change
                && Some(&n.value) != info.current_value.as_ref()
            {
                Some(self.receive_entry(&n, &current, change_type::VALUE, login))
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
            Self::write_alert(&mut tx, &a, false).await?;
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
            let entry = self.receive_entry(&n, &t.status, change_type::SEVERITY, login);
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
            Self::write_alert(&mut tx, &a, false).await?;
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
        let entry = self.receive_entry(&n, &t.status, change_type::NEW, login);
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
        Self::write_alert(&mut tx, &a, true).await?;
        tx.commit().await?;
        Ok((a, ReceiveKind::Created))
    }

    fn receive_entry(&self, n: &NewAlert, status: &str, kind: &str, login: &str) -> HistoryEntry {
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
            user: Some(login.to_string()),
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
            user: Some(login.to_string()),
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
        Self::write_alert(&mut tx, &a, false).await?;
        tx.commit().await?;
        Ok(a)
    }

    /// `PUT /alert/<id>/action` (spec A2-A5), also used by housekeeping.
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
            user: Some(login.to_string()),
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
        Self::write_alert(tx, &a, false).await?;
        Ok(a)
    }

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

    #[allow(clippy::too_many_arguments)]
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

    pub async fn get_heartbeat(&self, id: &str) -> Result<Heartbeat, StoreError> {
        let row = sqlx::query("SELECT * FROM heartbeats WHERE id LIKE $1 LIMIT 1")
            .bind(like_prefix(id))
            .fetch_optional(&self.pool)
            .await?
            .ok_or(StoreError::NotFound)?;
        Ok(row_to_heartbeat(&row)?)
    }

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

    pub async fn list_keys(&self) -> Result<Vec<ApiKey>, StoreError> {
        let rows = sqlx::query("SELECT * FROM keys ORDER BY expire_time DESC")
            .fetch_all(&self.pool)
            .await?;
        Ok(rows.iter().map(row_to_key).collect::<Result<Vec<_>, _>>()?)
    }

    pub async fn delete_key(&self, key_or_id: &str) -> Result<(), StoreError> {
        let r = sqlx::query("DELETE FROM keys WHERE key = $1 OR id = $1")
            .bind(key_or_id)
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
    pub async fn housekeeping(
        &self,
        expired_after_s: i64,
        info_after_s: i64,
    ) -> Result<Vec<String>, StoreError> {
        if expired_after_s > 0 {
            sqlx::query(
                "DELETE FROM alerts WHERE status IN ('closed','expired')
                   AND last_receive_time < now() - make_interval(secs => $1)",
            )
            .bind(expired_after_s as f64)
            .execute(&self.pool)
            .await?;
        }
        if info_after_s > 0 {
            sqlx::query("DELETE FROM alerts WHERE severity = $1 AND last_receive_time < now() - make_interval(secs => $2)")
                .bind(INFORM_SEVERITY)
                .bind(info_after_s as f64)
                .execute(&self.pool)
                .await?;
        }
        let ids: Vec<String> = sqlx::query_scalar(
            "SELECT id FROM alerts WHERE status <> 'expired' AND COALESCE(timeout, $1) <> 0
               AND last_receive_time + make_interval(secs => timeout) < now()",
        )
        .bind(self.cfg.alert_timeout)
        .fetch_all(&self.pool)
        .await?;
        for id in &ids {
            let mut tx = self.pool.begin().await?;
            let a = Self::lock_alert(&mut tx, id, None).await?;
            self.apply_action(&mut tx, a, "expired", "alert timeout", None, "housekeeping")
                .await?;
            tx.commit().await?;
        }
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
    fn like_prefix_escapes() {
        assert_eq!(like_prefix("ab_%"), "ab\\_\\%%");
    }
}
