//! Grouped alert reads for the web UI: `GET /environments`, `/services`,
//! `/alerts/groups` and `/alerts/tags` (reference
//! `postgres/base.py:637-741`; `spec/openapi.yaml`).
//!
//! The filters are the ones `GET /alerts` uses ([`AlertQuery`]), including
//! the customer scope. As in the reference, the environment names of
//! `/environments` and the (environment, service) pairs of `/services` are
//! read from **all** alerts, without the filters or the customer scope;
//! only their counts are filtered (`spec/customer-mapping.md` C20). The
//! page of environment names is one bound array (SF-QUERY alrust item 5).
//!
//! Each grouped count reads at most [`GROUPED_ROW_LIMIT`] rows, as the
//! reference does; this module adds an `ORDER BY`, so the rows kept are
//! fixed where the reference's are not.

use std::collections::HashMap;

use alrust_domain::{NORMAL_SEVERITY, status};
use serde_json::{Map, Value};
use sqlx::postgres::PgRow;
use sqlx::{Postgres, QueryBuilder, Row};

use crate::{AlertQuery, Store, StoreError, or_default};

/// The reference's `limit=` on each grouped count (`base.py:650`, `:690`,
/// `:723`, `:739`).
pub const GROUPED_ROW_LIMIT: i64 = 1000;

/// Severity and status counts of one name, from `CUBE(severity, status)`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct CubeCounts {
    pub severity_counts: Map<String, Value>,
    pub status_counts: Map<String, Value>,
    pub count: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EnvironmentCounts {
    pub environment: Option<String>,
    pub counts: CubeCounts,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServiceCounts {
    pub environment: Option<String>,
    pub service: Option<String>,
    pub counts: CubeCounts,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GroupCount {
    pub environment: Option<String>,
    pub group: Option<String>,
    pub count: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TagCount {
    pub environment: Option<String>,
    pub tag: Option<String>,
    pub count: i64,
}

fn add(map: &mut Map<String, Value>, key: String, n: i64) {
    let e = map.entry(key).or_insert(Value::from(0));
    *e = Value::from(e.as_i64().unwrap_or(0) + n);
}

/// One `CUBE(severity, status)` row into `counts`. `GROUPING()` tells a
/// rolled-up column from a stored NULL. A stored NULL or empty severity or
/// status counts as `normal` or `open`, as in `GET /alerts/count`.
fn fold_cube_row(counts: &mut CubeCounts, row: &PgRow) -> Result<(), sqlx::Error> {
    let by_severity: i32 = row.try_get("g_severity")?;
    let by_status: i32 = row.try_get("g_status")?;
    let n: i64 = row.try_get("n")?;
    match (by_severity, by_status) {
        // Grouped by severity only.
        (0, 1) => add(
            &mut counts.severity_counts,
            or_default(row, "severity", NORMAL_SEVERITY)?,
            n,
        ),
        // Grouped by status only.
        (1, 0) => add(
            &mut counts.status_counts,
            or_default(row, "status", status::OPEN)?,
            n,
        ),
        // The name's total.
        (1, 1) => counts.count += n,
        // Severity and status together: the reference reads no such row.
        _ => {}
    }
    Ok(())
}

const CUBE_COLUMNS: &str = "severity, status, GROUPING(severity) AS g_severity, \
                            GROUPING(status) AS g_status, count(*) AS n";

fn push_limit(qb: &mut QueryBuilder<'_, Postgres>) {
    qb.push(" LIMIT ").push_bind(GROUPED_ROW_LIMIT);
}

impl Store {
    /// `GET /environments` `total`: the number of distinct environments of
    /// the matching alerts (`base.py:674-681`).
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn environment_total(&self, q: &AlertQuery) -> Result<i64, StoreError> {
        let mut qb = QueryBuilder::new("SELECT COUNT(DISTINCT environment) AS n FROM alerts");
        Self::push_filters(&mut qb, q);
        let row = qb.build().fetch_one(&self.pool).await?;
        Ok(row.try_get("n")?)
    }

    /// `GET /environments` items (`base.py:637-671`): one page of the
    /// environment names of all alerts, in ascending order, each with the
    /// counts of the matching alerts. A name with no matching alert has
    /// empty counts.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn environments(
        &self,
        q: &AlertQuery,
        page: i64,
        page_size: i64,
    ) -> Result<Vec<EnvironmentCounts>, StoreError> {
        // C20: the names ignore the filters and the customer scope.
        let names: Vec<Option<String>> = sqlx::query_scalar(
            "SELECT DISTINCT environment FROM alerts ORDER BY environment ASC LIMIT $1 OFFSET $2",
        )
        .bind(page_size)
        .bind((page - 1).saturating_mul(page_size))
        .fetch_all(&self.pool)
        .await?;
        let mut out: Vec<EnvironmentCounts> = names
            .into_iter()
            .map(|environment| EnvironmentCounts {
                environment,
                counts: CubeCounts::default(),
            })
            .collect();
        if out.is_empty() {
            return Ok(out);
        }
        let page_names: Vec<Option<String>> = out.iter().map(|e| e.environment.clone()).collect();
        let mut qb = QueryBuilder::new(format!("SELECT environment, {CUBE_COLUMNS} FROM alerts"));
        Self::push_filters(&mut qb, q);
        qb.push(" AND environment = ANY(")
            .push_bind(page_names)
            .push(")");
        qb.push(
            " GROUP BY environment, CUBE(severity, status) \
             ORDER BY environment, g_severity, g_status, severity, status",
        );
        push_limit(&mut qb);
        let index: HashMap<Option<String>, usize> = out
            .iter()
            .enumerate()
            .map(|(i, e)| (e.environment.clone(), i))
            .collect();
        for row in qb.build().fetch_all(&self.pool).await? {
            let env: Option<String> = row.try_get("environment")?;
            if let Some(e) = index.get(&env).and_then(|&i| out.get_mut(i)) {
                fold_cube_row(&mut e.counts, &row)?;
            }
        }
        Ok(out)
    }

    /// `GET /services` items (`base.py:684-715`): every (environment,
    /// service) pair of all alerts, each with the counts of the matching
    /// alerts.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn services(&self, q: &AlertQuery) -> Result<Vec<ServiceCounts>, StoreError> {
        let mut qb = QueryBuilder::new(format!(
            "SELECT environment, svc, {CUBE_COLUMNS} FROM alerts, UNNEST(service) svc"
        ));
        Self::push_filters(&mut qb, q);
        qb.push(
            " GROUP BY environment, svc, CUBE(severity, status) \
             ORDER BY environment, svc, g_severity, g_status, severity, status",
        );
        push_limit(&mut qb);
        let counted = qb.build().fetch_all(&self.pool).await?;
        // C20: the pairs ignore the filters and the customer scope.
        let pairs = sqlx::query(
            "SELECT DISTINCT environment, svc FROM alerts, UNNEST(service) svc \
             ORDER BY environment, svc",
        )
        .fetch_all(&self.pool)
        .await?;
        let mut out = pairs
            .iter()
            .map(|r| {
                Ok(ServiceCounts {
                    environment: r.try_get("environment")?,
                    service: r.try_get("svc")?,
                    counts: CubeCounts::default(),
                })
            })
            .collect::<Result<Vec<_>, sqlx::Error>>()?;
        let index: HashMap<(Option<String>, Option<String>), usize> = out
            .iter()
            .enumerate()
            .map(|(i, s)| ((s.environment.clone(), s.service.clone()), i))
            .collect();
        for row in &counted {
            let key: (Option<String>, Option<String>) =
                (row.try_get("environment")?, row.try_get("svc")?);
            if let Some(s) = index.get(&key).and_then(|&i| out.get_mut(i)) {
                fold_cube_row(&mut s.counts, row)?;
            }
        }
        Ok(out)
    }

    /// `GET /alerts/groups` items (`base.py:718-731`).
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn alert_groups(&self, q: &AlertQuery) -> Result<Vec<GroupCount>, StoreError> {
        let mut qb =
            QueryBuilder::new("SELECT environment, \"group\" AS grp, count(*) AS n FROM alerts");
        Self::push_filters(&mut qb, q);
        qb.push(" GROUP BY environment, \"group\" ORDER BY environment, \"group\"");
        push_limit(&mut qb);
        let rows = qb.build().fetch_all(&self.pool).await?;
        Ok(rows
            .iter()
            .map(|r| {
                Ok(GroupCount {
                    environment: r.try_get("environment")?,
                    group: r.try_get("grp")?,
                    count: r.try_get("n")?,
                })
            })
            .collect::<Result<Vec<_>, sqlx::Error>>()?)
    }

    /// `GET /alerts/tags` items (`base.py:734-741`). Alerts whose `tags` is
    /// NULL or empty add nothing.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn alert_tags(&self, q: &AlertQuery) -> Result<Vec<TagCount>, StoreError> {
        let mut qb = QueryBuilder::new(
            "SELECT environment, tag, count(*) AS n FROM alerts, UNNEST(tags) tag",
        );
        Self::push_filters(&mut qb, q);
        qb.push(" GROUP BY environment, tag ORDER BY environment, tag");
        push_limit(&mut qb);
        let rows = qb.build().fetch_all(&self.pool).await?;
        Ok(rows
            .iter()
            .map(|r| {
                Ok(TagCount {
                    environment: r.try_get("environment")?,
                    tag: r.try_get("tag")?,
                    count: r.try_get("n")?,
                })
            })
            .collect::<Result<Vec<_>, sqlx::Error>>()?)
    }
}
