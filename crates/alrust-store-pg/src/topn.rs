//! Top-N alert groups: `GET /alerts/top10/…` and `/alerts/topn/…` for
//! `count`, `flapping` and `standing` (reference `postgres/base.py:556-634`,
//! `views/alerts.py:405-483`), with `spec/security-fixes.md` SF-QUERY alrust
//! item 2: the `group-by` column comes from a fixed list and is emitted
//! quoted.
//!
//! As in the reference:
//! - every query joins `UNNEST(service)`, so an alert counts once per
//!   service, and an alert with a NULL or empty `service` counts nothing;
//! - `flapping` and `standing` also join `UNNEST(history)` filtered to
//!   `type = 'severity'`, so an alert counts once per severity entry;
//! - `count` and `flapping` order by that count, `standing` by the summed
//!   `last_receive_time - create_time`, all descending (NULL first, as
//!   PostgreSQL orders `DESC`). alrust adds the grouped column as a
//!   tiebreak, so equal values come in a fixed order.

use alrust_domain::iso8601;
use chrono::NaiveDateTime;
use serde_json::{Map, Value};
use sqlx::postgres::PgRow;
use sqlx::{Postgres, QueryBuilder, Row};

use crate::{AlertQuery, Store, StoreError, utc};

/// Which top-N list.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TopnKind {
    Count,
    Flapping,
    Standing,
}

/// How the reference's database adapter and JSON encoder write a column's
/// value (psycopg2, then `utils/format.py`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Render {
    /// Text, text arrays, integers, booleans and `jsonb`: as JSON.
    Json,
    /// `timestamp`: `DateTime.iso8601`, milliseconds and `Z`.
    Time,
    /// `history[]`: the registered composite, a list of 10-field lists.
    History,
}

/// SF-QUERY alrust item 2: the `alerts` columns (`sql/schema.sql:32-62`).
const GROUP_COLUMNS: [(&str, Render); 28] = [
    ("id", Render::Json),
    ("resource", Render::Json),
    ("event", Render::Json),
    ("environment", Render::Json),
    ("severity", Render::Json),
    ("correlate", Render::Json),
    ("status", Render::Json),
    ("service", Render::Json),
    ("group", Render::Json),
    ("value", Render::Json),
    ("text", Render::Json),
    ("tags", Render::Json),
    ("attributes", Render::Json),
    ("origin", Render::Json),
    ("type", Render::Json),
    ("create_time", Render::Time),
    ("timeout", Render::Json),
    ("raw_data", Render::Json),
    ("customer", Render::Json),
    ("duplicate_count", Render::Json),
    ("repeat", Render::Json),
    ("previous_severity", Render::Json),
    ("trend_indication", Render::Json),
    ("receive_time", Render::Time),
    ("last_receive_id", Render::Json),
    ("last_receive_time", Render::Time),
    ("history", Render::History),
    ("update_time", Render::Time),
];

/// The `history` type's attributes, in order (`sql/schema.sql:5-16`).
const HISTORY_FIELDS: [&str; 10] = [
    "id",
    "event",
    "severity",
    "status",
    "value",
    "text",
    "type",
    "update_time",
    "user",
    "timeout",
];

/// A `group-by` column that SF-QUERY allows.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct GroupColumn {
    name: &'static str,
    render: Render,
}

impl GroupColumn {
    /// The column whose name is exactly `name` (case-sensitive), if any.
    pub fn parse(name: &str) -> Option<Self> {
        GROUP_COLUMNS
            .iter()
            .find(|(n, _)| *n == name)
            .map(|&(name, render)| Self { name, render })
    }

    /// The column name, which is also the response key.
    pub fn name(&self) -> &'static str {
        self.name
    }
}

/// One top-N item.
#[derive(Debug, Clone, PartialEq)]
pub struct TopnItem {
    /// The grouped column's value, as the reference writes it.
    pub group: Value,
    pub count: i64,
    /// `SUM(duplicate_count)` over the joined rows; `None` when every row
    /// has NULL.
    pub duplicate_count: Option<i64>,
    pub environments: Vec<Option<String>>,
    pub services: Vec<Option<String>>,
    /// `(id, resource)` of each alert, in the order `array_agg(DISTINCT …)`
    /// gives.
    pub resources: Vec<(String, String)>,
}

fn decode_error(message: String) -> sqlx::Error {
    sqlx::Error::Decode(message.into())
}

/// A timestamp text from `to_jsonb` (`2026-11-01T00:00:00` with optional
/// fraction) as `DateTime.iso8601`.
fn iso_from_json_text(text: &str) -> Result<Value, sqlx::Error> {
    NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M:%S%.f")
        .map(|t| Value::String(iso8601(&utc(t))))
        .map_err(|e| decode_error(format!("history update_time {text:?}: {e}")))
}

/// `to_jsonb(history)` as psycopg2's named tuples: each entry a list of the
/// 10 attributes in order, `update_time` as `DateTime.iso8601`.
fn history_as_lists(v: Value) -> Result<Value, sqlx::Error> {
    let Value::Array(entries) = v else {
        return Ok(v);
    };
    let mut out = Vec::with_capacity(entries.len());
    for entry in entries {
        let Value::Object(fields) = entry else {
            out.push(entry);
            continue;
        };
        let mut list = Vec::with_capacity(HISTORY_FIELDS.len());
        for name in HISTORY_FIELDS {
            list.push(match (name, fields.get(name)) {
                ("update_time", Some(Value::String(text))) => iso_from_json_text(text)?,
                (_, Some(value)) => value.clone(),
                (_, None) => Value::Null,
            });
        }
        out.push(Value::Array(list));
    }
    Ok(Value::Array(out))
}

fn group_value(row: &PgRow, render: Render) -> Result<Value, sqlx::Error> {
    match render {
        Render::Json => Ok(row
            .try_get::<Option<Value>, _>("grp")?
            .unwrap_or(Value::Null)),
        Render::Time => Ok(row
            .try_get::<Option<NaiveDateTime>, _>("grp")?
            .map_or(Value::Null, |t| Value::String(iso8601(&utc(t))))),
        Render::History => row
            .try_get::<Option<Value>, _>("grp")?
            .map_or(Ok(Value::Null), history_as_lists),
    }
}

fn resources(row: &PgRow) -> Result<Vec<(String, String)>, sqlx::Error> {
    let v: Option<Value> = row.try_get("resources")?;
    match v {
        None => Ok(Vec::new()),
        Some(v) => serde_json::from_value(v).map_err(|e| decode_error(format!("resources: {e}"))),
    }
}

fn row_to_item(row: &PgRow, render: Render) -> Result<TopnItem, sqlx::Error> {
    Ok(TopnItem {
        group: group_value(row, render)?,
        count: row.try_get("n")?,
        duplicate_count: row.try_get("dup")?,
        environments: row
            .try_get::<Option<_>, _>("environments")?
            .unwrap_or_default(),
        services: row.try_get::<Option<_>, _>("services")?.unwrap_or_default(),
        resources: resources(row)?,
    })
}

impl Store {
    /// A top-N list over the alerts matching `q`, grouped by `group`, at
    /// most `limit` items.
    ///
    /// # Errors
    ///
    /// The database error, or a value that does not decode.
    pub async fn topn(
        &self,
        q: &AlertQuery,
        kind: TopnKind,
        group: GroupColumn,
        limit: i64,
    ) -> Result<Vec<TopnItem>, StoreError> {
        // The filters apply to the alerts before any join, as the
        // reference's `WITH topn AS (SELECT * FROM alerts WHERE …)`. Every
        // outer column is qualified: `history` has its own `id`, `event`,
        // `severity`, `status`, `value`, `text`, `type`, `update_time`,
        // `user` and `timeout`.
        let mut qb: QueryBuilder<'_, Postgres> =
            QueryBuilder::new("WITH topn AS (SELECT * FROM alerts");
        Self::push_filters(&mut qb, q);
        let col = format!("topn.\"{}\"", group.name);
        let grp = match group.render {
            Render::Json | Render::History => format!("to_jsonb({col})"),
            Render::Time => col.clone(),
        };
        qb.push(format!(
            ") SELECT {grp} AS grp, COUNT(1) AS n, SUM(topn.duplicate_count) AS dup, \
             array_agg(DISTINCT topn.environment) AS environments, \
             array_agg(DISTINCT svc) AS services, \
             to_jsonb(array_agg(DISTINCT ARRAY[topn.id, topn.resource])) AS resources"
        ));
        if kind == TopnKind::Standing {
            qb.push(", SUM(topn.last_receive_time - topn.create_time) AS life_time");
        }
        qb.push(" FROM topn, UNNEST(topn.service) AS svc");
        if kind != TopnKind::Count {
            qb.push(", UNNEST(topn.history) AS hist WHERE hist.type = 'severity'");
        }
        let order = if kind == TopnKind::Standing {
            "life_time"
        } else {
            "n"
        };
        qb.push(format!(
            " GROUP BY {col} ORDER BY {order} DESC, {col} LIMIT "
        ))
        .push_bind(limit);
        let rows = qb.build().fetch_all(&self.pool).await?;
        Ok(rows
            .iter()
            .map(|r| row_to_item(r, group.render))
            .collect::<Result<Vec<_>, sqlx::Error>>()?)
    }
}

/// A top-N item as the reference's `top10` entry, without the `resources`
/// hrefs (the API adds them).
pub fn base_fields(item: &TopnItem, group: GroupColumn) -> Map<String, Value> {
    let mut m = Map::new();
    m.insert("count".into(), Value::from(item.count));
    m.insert(
        "duplicateCount".into(),
        item.duplicate_count.map_or(Value::Null, Value::from),
    );
    m.insert(
        "environments".into(),
        Value::from(item.environments.clone()),
    );
    m.insert("services".into(), Value::from(item.services.clone()));
    m.insert(group.name.into(), item.group.clone());
    m
}
