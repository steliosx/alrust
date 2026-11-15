//! Blackout periods: `POST /blackout`, `GET`/`PUT`/`DELETE /blackout/{id}`
//! and `GET /blackouts` (reference `views/blackouts.py`, `models/blackout.py`,
//! `postgres/base.py:743-850`, `postgres/utils.py:201-268`; `spec/openapi.yaml`
//! Blackout), with `spec/security-fixes.md` SF-BLACKOUTS.
//!
//! As in the reference, every read rebuilds the blackout the way
//! `Blackout.__init__` does: `duration` is `endTime - startTime` in whole
//! seconds and `priority` is computed from the fields (the stored columns
//! are written at creation and never refreshed). `status` uses the
//! application clock. `remaining` is the integer duration on creation and,
//! on every other read, the database's `numeric` `GREATEST(EXTRACT(EPOCH
//! FROM ...), 0)` as its text, which is how the reference's JSON encoder
//! writes the `Decimal` it gets (a string).
//!
//! Alert suppression during a blackout is not part of this module.

use chrono::{DateTime, NaiveDateTime, Utc};
use serde_json::{Value, json};
use sqlx::postgres::PgRow;
use sqlx::{Postgres, QueryBuilder, Row};

use crate::{Store, StoreError, list, naive, opt_time, utc};

/// `BLACKOUT_DURATION` (`settings.py:281`).
pub const BLACKOUT_DURATION: i64 = 3600;

/// `remaining` on reads (`postgres/base.py:758`): a `numeric`.
const REMAINING_EXPR: &str =
    "GREATEST(EXTRACT(EPOCH FROM (end_time - GREATEST(start_time, NOW() AT TIME ZONE 'utc'))), 0)";

/// [`REMAINING_EXPR`] as text, the form the response carries.
fn remaining_column() -> String {
    format!("({REMAINING_EXPR})::text AS remaining")
}

/// A stored blackout as the reference's model holds it after a read.
#[derive(Debug, Clone, PartialEq)]
pub struct Blackout {
    pub id: String,
    pub environment: String,
    pub service: Vec<String>,
    pub resource: Option<String>,
    pub event: Option<String>,
    pub group: Option<String>,
    pub tags: Vec<String>,
    pub origin: Option<String>,
    pub customer: Option<String>,
    pub start_time: DateTime<Utc>,
    pub end_time: DateTime<Utc>,
    /// A number on creation, the database's text on other reads.
    pub remaining: Value,
    pub user: Option<String>,
    pub create_time: Option<DateTime<Utc>>,
    pub text: Option<String>,
}

/// `Blackout.__init__`'s priority ladder (`models/blackout.py:63-80`).
pub fn priority(
    resource: Option<&str>,
    event: Option<&str>,
    service: &[String],
    group: Option<&str>,
    tags: &[String],
    origin: Option<&str>,
) -> i32 {
    let set = |v: Option<&str>| v.is_some_and(|s| !s.is_empty());
    let (resource, event, group, origin) = (set(resource), set(event), set(group), set(origin));
    if origin {
        return 8;
    }
    if resource && !event {
        2
    } else if !service.is_empty() {
        3
    } else if event && !resource {
        4
    } else if group {
        5
    } else if resource && event {
        6
    } else if !tags.is_empty() {
        7
    } else {
        0
    }
}

impl Blackout {
    /// `int((end_time - start_time).total_seconds())`.
    pub fn duration(&self) -> i64 {
        (self.end_time - self.start_time).num_seconds()
    }

    pub fn priority(&self) -> i32 {
        priority(
            self.resource.as_deref(),
            self.event.as_deref(),
            &self.service,
            self.group.as_deref(),
            &self.tags,
            self.origin.as_deref(),
        )
    }

    /// `Blackout.status` on the given (application) clock.
    pub fn status(&self, now: DateTime<Utc>) -> &'static str {
        if self.start_time <= now && now < self.end_time {
            "active"
        } else if self.start_time > now {
            "pending"
        } else {
            "expired"
        }
    }

    /// `Blackout.serialize` (`models/blackout.py:115-137`).
    pub fn body(&self, base_url: &str, now: DateTime<Utc>) -> Value {
        let iso = alrust_domain::iso8601;
        // Two json! calls: one call this size nears the macro's recursion
        // limit.
        let mut body = json!({
            "id": self.id,
            "href": format!("{base_url}/blackout/{}", alrust_domain::path_segment(&self.id)),
            "priority": self.priority(),
            "environment": self.environment,
            "service": self.service,
            "resource": self.resource,
            "event": self.event,
            "group": self.group,
            "tags": self.tags,
            "origin": self.origin,
        });
        let rest = json!({
            "customer": self.customer,
            "startTime": iso(&self.start_time),
            "endTime": iso(&self.end_time),
            "duration": self.duration(),
            "status": self.status(now),
            "remaining": self.remaining,
            "user": self.user,
            "createTime": self.create_time.as_ref().map(iso),
            "text": self.text,
        });
        if let (Value::Object(top), Value::Object(rest)) = (&mut body, rest) {
            top.extend(rest);
        }
        body
    }
}

/// `from_record`: an empty `resource`, `event`, `group` or `origin` reads
/// as none.
fn blank(row: &PgRow, column: &str) -> Result<Option<String>, sqlx::Error> {
    Ok(row
        .try_get::<Option<String>, _>(column)?
        .filter(|v| !v.is_empty()))
}

/// Which `remaining` a row carries.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Read {
    /// `RETURNING *, duration AS remaining`: the stored duration.
    Created,
    /// The [`REMAINING_EXPR`] text.
    Stored,
}

fn row_to_blackout(row: &PgRow, read: Read) -> Result<Blackout, sqlx::Error> {
    let remaining = if read == Read::Created {
        row.try_get::<Option<i32>, _>("duration")?
            .map_or(Value::Null, Value::from)
    } else {
        row.try_get::<Option<String>, _>("remaining")?
            .map_or(Value::Null, Value::String)
    };
    Ok(Blackout {
        id: row.try_get("id")?,
        environment: row.try_get("environment")?,
        service: list(row, "service")?,
        resource: blank(row, "resource")?,
        event: blank(row, "event")?,
        group: blank(row, "group")?,
        tags: list(row, "tags")?,
        origin: blank(row, "origin")?,
        customer: row.try_get("customer")?,
        start_time: utc(row.try_get::<NaiveDateTime, _>("start_time")?),
        end_time: utc(row.try_get::<NaiveDateTime, _>("end_time")?),
        remaining,
        user: row.try_get("user")?,
        create_time: opt_time(row, "create_time")?,
        text: row.try_get("text")?,
    })
}

/// A blackout to insert, already resolved as `Blackout.__init__` resolves
/// it (times, duration, user, customer).
#[derive(Debug, Clone)]
pub struct NewBlackout {
    pub id: String,
    pub environment: String,
    pub service: Vec<String>,
    pub resource: Option<String>,
    pub event: Option<String>,
    pub group: Option<String>,
    pub tags: Vec<String>,
    pub origin: Option<String>,
    pub customer: Option<String>,
    pub start_time: DateTime<Utc>,
    pub end_time: DateTime<Utc>,
    pub duration: i64,
    pub user: Option<String>,
    pub create_time: DateTime<Utc>,
    pub text: Option<String>,
}

/// Whether an update writes a column, and the value (which may be NULL).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub enum Change<T> {
    #[default]
    Keep,
    Set(T),
}

/// The columns `PUT /blackout/{id}` writes (`postgres/base.py:803-839`).
#[derive(Debug, Clone, Default)]
pub struct BlackoutUpdate {
    pub environment: Option<String>,
    pub service: Option<Vec<String>>,
    pub resource: Change<Option<String>>,
    pub event: Change<Option<String>>,
    pub group: Change<Option<String>>,
    pub tags: Option<Vec<String>>,
    pub origin: Change<Option<String>>,
    /// SF-BLACKOUTS: only when the body names `customer`.
    pub customer: Change<Option<String>>,
    pub start_time: Option<DateTime<Utc>>,
    pub end_time: Option<DateTime<Utc>>,
    pub duration: Change<Option<i32>>,
    pub text: Change<Option<String>>,
    pub user: Option<String>,
}

/// How a filter column compares (`postgres/utils.py:55-104`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ColumnKind {
    Text,
    Integer,
    Timestamp,
    /// `service`: array overlap.
    Array,
}

/// One `GET /blackouts` filter.
#[derive(Debug, Clone)]
pub struct BlackoutFilter {
    /// The quoted column.
    pub column: &'static str,
    pub kind: ColumnKind,
    pub negated: bool,
    pub values: Vec<String>,
}

/// `GET /blackouts`' filters and order.
#[derive(Debug, Clone, Default)]
pub struct BlackoutQuery {
    /// `None` means no customer restriction.
    pub customers: Option<Vec<String>>,
    /// Any of `active`, `pending`, `expired`; empty means all.
    pub statuses: Vec<String>,
    pub filters: Vec<BlackoutFilter>,
    /// `(column, descending)`; empty means the default order.
    pub sort: Vec<(&'static str, bool)>,
}

fn push_where(qb: &mut QueryBuilder<'_, Postgres>, q: &BlackoutQuery) {
    qb.push(" WHERE true");
    if let Some(c) = &q.customers {
        qb.push(" AND customer = ANY(")
            .push_bind(c.clone())
            .push(")");
    }
    let mut states = Vec::new();
    for s in &q.statuses {
        match s.as_str() {
            "active" => {
                states.push("(start_time <= NOW() AT TIME ZONE 'utc' AND end_time > NOW())");
            }
            "pending" => states.push("start_time > NOW() AT TIME ZONE 'utc'"),
            "expired" => states.push("end_time <= NOW() AT TIME ZONE 'utc'"),
            _ => {}
        }
    }
    if !states.is_empty() {
        qb.push(format!(" AND ({})", states.join(" OR ")));
    }
    for f in &q.filters {
        push_filter(qb, f);
    }
}

/// One filter, as `filter_query` writes it: one value compares with `=`
/// (`!=` negated) or, with a leading `~`, `ILIKE '%v%'`; several compare
/// with `= ANY` (`NOT ... = ANY`) or, when any starts with `~`, one
/// case-insensitive regular expression of all of them joined by `|`.
fn push_filter(qb: &mut QueryBuilder<'_, Postgres>, f: &BlackoutFilter) {
    let col = f.column;
    if f.kind == ColumnKind::Array {
        qb.push(format!(" AND {col} && "))
            .push_bind(f.values.clone());
        return;
    }
    let cast = match f.kind {
        ColumnKind::Integer => "::integer",
        ColumnKind::Timestamp => "::timestamp",
        ColumnKind::Text | ColumnKind::Array => "",
    };
    let regex = f.values.iter().any(|v| v.starts_with('~'));
    if let [one] = f.values.as_slice() {
        if let Some(pattern) = one.strip_prefix('~') {
            let op = if f.negated { "NOT ILIKE" } else { "ILIKE" };
            qb.push(format!(" AND {col} {op} "))
                .push_bind(format!("%{pattern}%"));
        } else {
            let op = if f.negated { "!=" } else { "=" };
            qb.push(format!(" AND {col} {op} "))
                .push_bind(one.clone())
                .push(cast);
        }
    } else if regex {
        let op = if f.negated { "!~*" } else { "~*" };
        let joined = f
            .values
            .iter()
            .map(|v| v.trim_start_matches('~'))
            .collect::<Vec<_>>()
            .join("|");
        qb.push(format!(" AND {col} {op} ")).push_bind(joined);
    } else {
        let not = if f.negated { "NOT " } else { "" };
        let close = if cast.is_empty() {
            ")".to_owned()
        } else {
            format!("{cast}[])")
        };
        qb.push(format!(" AND {not}{col} = ANY("))
            .push_bind(f.values.clone())
            .push(close);
    }
}

impl Store {
    /// `POST /blackout`: insert and return the created blackout.
    ///
    /// # Errors
    ///
    /// The database error (for example a duplicate id).
    pub async fn create_blackout(&self, b: &NewBlackout) -> Result<Blackout, StoreError> {
        let prio = priority(
            b.resource.as_deref(),
            b.event.as_deref(),
            &b.service,
            b.group.as_deref(),
            &b.tags,
            b.origin.as_deref(),
        );
        let duration = i32::try_from(b.duration).map_err(|_| StoreError::OutOfRange("duration"))?;
        let row = sqlx::query(
            r#"INSERT INTO blackouts (id, priority, environment, service, resource, event, "group", tags,
                   origin, customer, start_time, end_time, duration, "user", create_time, text)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
               RETURNING *"#,
        )
        .bind(&b.id)
        .bind(prio)
        .bind(&b.environment)
        .bind(&b.service)
        .bind(&b.resource)
        .bind(&b.event)
        .bind(&b.group)
        .bind(&b.tags)
        .bind(&b.origin)
        .bind(&b.customer)
        .bind(naive(b.start_time))
        .bind(naive(b.end_time))
        .bind(duration)
        .bind(&b.user)
        .bind(naive(b.create_time))
        .bind(&b.text)
        .fetch_one(&self.pool)
        .await?;
        Ok(row_to_blackout(&row, Read::Created)?)
    }

    /// One blackout by exact id, optionally limited to `customers`.
    ///
    /// # Errors
    ///
    /// `NotFound`, otherwise the database error.
    pub async fn get_blackout(
        &self,
        id: &str,
        customers: Option<&[String]>,
    ) -> Result<Blackout, StoreError> {
        let remaining = remaining_column();
        let row = sqlx::query(&format!(
            "SELECT *, {remaining} FROM blackouts WHERE id = $1 \
             AND ($2::text[] IS NULL OR customer = ANY($2))"
        ))
        .bind(id)
        .bind(customers)
        .fetch_optional(&self.pool)
        .await?
        .ok_or(StoreError::NotFound)?;
        Ok(row_to_blackout(&row, Read::Stored)?)
    }

    /// `GET /blackouts` total.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn count_blackouts(&self, q: &BlackoutQuery) -> Result<i64, StoreError> {
        let mut qb: QueryBuilder<'_, Postgres> =
            QueryBuilder::new("SELECT COUNT(1) AS n FROM blackouts");
        push_where(&mut qb, q);
        let row = qb.build().fetch_one(&self.pool).await?;
        Ok(row.try_get("n")?)
    }

    /// `GET /blackouts` page. Without `sort-by` the reference has no order;
    /// alrust orders by creation time, then id.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn list_blackouts(
        &self,
        q: &BlackoutQuery,
        page: i64,
        page_size: i64,
    ) -> Result<Vec<Blackout>, StoreError> {
        let remaining = remaining_column();
        let mut qb: QueryBuilder<'_, Postgres> =
            QueryBuilder::new(format!("SELECT *, {remaining} FROM blackouts"));
        push_where(&mut qb, q);
        // `remaining` sorts as the number, not as its text.
        let mut order: Vec<String> = q
            .sort
            .iter()
            .map(|(col, desc)| {
                let col = if *col == "remaining" {
                    REMAINING_EXPR
                } else {
                    *col
                };
                format!("{col} {}", if *desc { "DESC" } else { "ASC" })
            })
            .collect();
        if order.is_empty() {
            order.push("create_time ASC".into());
        }
        order.push("id ASC".into());
        qb.push(format!(" ORDER BY {} LIMIT ", order.join(", ")))
            .push_bind(page_size)
            .push(" OFFSET ")
            .push_bind((page - 1).saturating_mul(page_size));
        let rows = qb.build().fetch_all(&self.pool).await?;
        Ok(rows
            .iter()
            .map(|r| row_to_blackout(r, Read::Stored))
            .collect::<Result<Vec<_>, sqlx::Error>>()?)
    }

    /// `PUT /blackout/{id}`: write the given columns of the blackout
    /// visible to `customers` (`None`: any).
    ///
    /// # Errors
    ///
    /// `NotFound`, otherwise the database error.
    pub async fn update_blackout(
        &self,
        id: &str,
        customers: Option<&[String]>,
        u: BlackoutUpdate,
    ) -> Result<Blackout, StoreError> {
        self.get_blackout(id, customers).await?;
        let remaining = remaining_column();
        let mut qb: QueryBuilder<'_, Postgres> = QueryBuilder::new("UPDATE blackouts SET ");
        if let Some(v) = u.environment {
            qb.push("environment = ").push_bind(v).push(", ");
        }
        if let Some(v) = u.service {
            qb.push("service = ").push_bind(v).push(", ");
        }
        for (col, v) in [
            ("resource", u.resource),
            ("event", u.event),
            ("\"group\"", u.group),
            ("origin", u.origin),
            ("customer", u.customer),
            ("text", u.text),
        ] {
            if let Change::Set(v) = v {
                qb.push(format!("{col} = ")).push_bind(v).push(", ");
            }
        }
        if let Some(v) = u.tags {
            qb.push("tags = ").push_bind(v).push(", ");
        }
        if let Some(t) = u.start_time {
            qb.push("start_time = ").push_bind(naive(t)).push(", ");
        }
        if let Some(t) = u.end_time {
            qb.push("end_time = ").push_bind(naive(t)).push(", ");
        }
        if let Change::Set(d) = u.duration {
            qb.push("duration = ").push_bind(d).push(", ");
        }
        qb.push("\"user\" = COALESCE(")
            .push_bind(u.user)
            .push(", \"user\") WHERE id = ")
            .push_bind(id.to_owned())
            .push(format!(" RETURNING *, {remaining}"));
        let row = qb
            .build()
            .fetch_optional(&self.pool)
            .await?
            .ok_or(StoreError::NotFound)?;
        Ok(row_to_blackout(&row, Read::Stored)?)
    }

    /// `DELETE /blackout/{id}`, with no customer filter (customer-mapping
    /// C19, the reference contract).
    ///
    /// # Errors
    ///
    /// `NotFound`, otherwise the database error.
    pub async fn delete_blackout(&self, id: &str) -> Result<(), StoreError> {
        sqlx::query("DELETE FROM blackouts WHERE id = $1 RETURNING id")
            .bind(id)
            .fetch_optional(&self.pool)
            .await?
            .ok_or(StoreError::NotFound)?;
        Ok(())
    }
}
