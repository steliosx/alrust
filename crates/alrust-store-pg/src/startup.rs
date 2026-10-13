//! Start-up: decide the database's state, then migrate, adopt or refuse
//! (ADR-0005 decision 4).
//!
//! Everything runs on one dedicated connection holding SQLx's own migration
//! lock, so a second alrust (or any SQLx migrator on this database) waits;
//! the migrator itself then runs with locking off on that same connection.
//! The connection is closed, not returned to the pool, so the session lock
//! cannot leak.

use crate::catalog;
use sqlx::migrate::{Migrate, MigrateError, Migrator};
use sqlx::{Acquire, PgConnection, PgPool, Row};

pub(crate) static MIGRATOR: Migrator = sqlx::migrate!("./migrations");

/// What start-up found and did.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    /// An empty database: `0001` and `0002` applied.
    Fresh,
    /// An alrust database: pending migrations applied (none on a repeat start).
    Migrated,
    /// An exact reference database adopted: baseline recorded, nothing else written.
    Adopted,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Report {
    pub outcome: Outcome,
    /// Non-internal triggers on the reference tables (allowed; logged).
    pub triggers: Vec<String>,
    /// Keys that can never authenticate because their expiry or scopes are NULL.
    pub inactive_keys: i64,
}

#[derive(Debug, thiserror::Error)]
pub enum StartupError {
    #[error("database error: {0}")]
    Db(#[from] sqlx::Error),
    #[error("migration failed: {0}")]
    Migrate(#[from] MigrateError),
    #[error("refusing to adopt the existing database; nothing was written. Differences from the reference schema: {}", .0.join("; "))]
    AdoptRefused(Vec<String>),
    #[error("refusing an unrecognised database state; nothing was written: {0}")]
    Unrecognised(String),
    #[error("the database does not match the reference schema after start-up: {}", .0.join("; "))]
    CatalogMismatch(Vec<String>),
    #[error(
        "database sessions must use search_path {expected:?} (see session_options); found {found:?}"
    )]
    SearchPath { expected: String, found: String },
}

const ADOPT_DESCRIPTION: &str = "baseline: adopted reference schema";

struct State {
    has_migrations: bool,
    has_alerts: bool,
    has_history_type: bool,
    reference_tables: Vec<String>,
}

async fn detect(conn: &mut PgConnection) -> Result<State, sqlx::Error> {
    let row = sqlx::query(
        "SELECT to_regclass('public._sqlx_migrations') IS NOT NULL AS m,
                EXISTS (SELECT 1 FROM pg_class WHERE relnamespace = 'public'::regnamespace
                          AND relname = 'alerts' AND relkind = 'r') AS a,
                EXISTS (SELECT 1 FROM pg_type WHERE typnamespace = 'public'::regnamespace
                          AND typname = 'history') AS h,
                ARRAY(SELECT relname::text FROM pg_class WHERE relnamespace = 'public'::regnamespace
                        AND relname = ANY($1) ORDER BY relname) AS t",
    )
    .bind(&catalog::TABLES[..])
    .fetch_one(&mut *conn)
    .await?;
    Ok(State {
        has_migrations: row.try_get("m")?,
        has_alerts: row.try_get("a")?,
        has_history_type: row.try_get("h")?,
        reference_tables: row.try_get("t")?,
    })
}

/// Prepare the database for serving. See the module docs.
pub async fn prepare(pool: &PgPool) -> Result<Report, StartupError> {
    let mut conn = pool.acquire().await?;
    conn.close_on_drop();
    conn.lock().await?;
    let result = prepare_locked(&mut conn).await;
    // Release explicitly; if that fails the connection is closed on drop,
    // which releases the session lock too.
    let _ = conn.unlock().await;
    result
}

async fn prepare_locked(conn: &mut PgConnection) -> Result<Report, StartupError> {
    // Every name below, SQLx's bookkeeping and the store's runtime queries
    // resolve through the search path, and unqualified DDL creates in its
    // first schema: the effective order must be exactly pg_catalog, public,
    // then (only if this session has one) its own temporary schema.
    let (setting, creation, order): (String, Option<String>, Vec<String>) = sqlx::query_as(
        "SELECT current_setting('search_path'), current_schema()::text, current_schemas(true)::text[]",
    )
    .fetch_one(&mut *conn)
    .await?;
    if !search_order_is_pinned(creation.as_deref(), &order) {
        return Err(StartupError::SearchPath {
            expected: format!("{} (pg_catalog, public, pg_temp)", crate::SEARCH_PATH),
            found: format!("{setting} ({})", order.join(", ")),
        });
    }
    let mut migrator = Migrator {
        migrations: MIGRATOR.migrations.clone(),
        ..Migrator::DEFAULT
    };
    migrator.set_locking(false);

    let state = detect(conn).await?;
    let outcome = match (
        state.has_migrations,
        state.has_alerts,
        state.has_history_type,
    ) {
        (false, false, false) if state.reference_tables.is_empty() => {
            migrator.run_direct(conn).await?;
            Outcome::Fresh
        }
        (true, true, _) => {
            migrator.run_direct(conn).await?;
            Outcome::Migrated
        }
        (false, true, true) => {
            adopt(conn, &state.reference_tables).await?;
            // Validates the baseline against the embedded checksums; applies nothing.
            migrator.run_direct(conn).await?;
            Outcome::Adopted
        }
        _ => {
            return Err(StartupError::Unrecognised(format!(
                "_sqlx_migrations {}, alerts table {}, history type {}, reference tables present: [{}]",
                present(state.has_migrations),
                present(state.has_alerts),
                present(state.has_history_type),
                state.reference_tables.join(", ")
            )));
        }
    };

    let mut tx = conn.begin().await?;
    let actual = catalog::read(&mut tx).await?;
    let differences = catalog::diff(&catalog::expected(), &actual);
    let triggers = catalog::triggers(&mut tx).await?;
    let inactive_keys: i64 =
        sqlx::query_scalar("SELECT count(*) FROM keys WHERE expire_time IS NULL OR scopes IS NULL")
            .fetch_one(&mut *tx)
            .await?;
    tx.rollback().await?;
    if !differences.is_empty() {
        return Err(StartupError::CatalogMismatch(differences));
    }
    Ok(Report {
        outcome,
        triggers,
        inactive_keys,
    })
}

/// `current_schema()` must be `public`, and `current_schemas(true)` must be
/// `pg_catalog, public` plus, at the end only, this session's temp schema.
fn search_order_is_pinned(creation: Option<&str>, order: &[String]) -> bool {
    let fixed = order.len() >= 2 && order[0] == "pg_catalog" && order[1] == "public";
    let rest_ok = match &order[2.min(order.len())..] {
        [] => true,
        [temp] => temp.starts_with("pg_temp_"),
        _ => false,
    };
    creation == Some("public") && fixed && rest_ok
}

fn present(b: bool) -> &'static str {
    if b { "present" } else { "absent" }
}

/// Adopt an existing reference database: compare its catalog read-only, and
/// only if it is exact record the SQLx baseline. On any difference nothing
/// is written, not even `_sqlx_migrations`.
///
/// The reference tables that exist are locked before the comparison and
/// stay locked until the baseline commits, so no table DDL can land between
/// the two. Writers that are not alrust wait, or the lock times out and
/// nothing is written. A standalone `ALTER TYPE history` locks only the
/// type, not the tables: the second comparison below narrows that window
/// but does not close it (a type change committed after that read and
/// before the baseline commit is caught only by the post-commit catalog
/// check, with the baseline already written). Closing it is an open design
/// question (ADR-0005).
async fn adopt(conn: &mut PgConnection, existing: &[String]) -> Result<(), StartupError> {
    let mut tx = conn.begin().await?;
    sqlx::query("SET LOCAL lock_timeout = '30s'")
        .execute(&mut *tx)
        .await?;
    let tables: Vec<&str> = catalog::TABLES
        .into_iter()
        .filter(|t| existing.iter().any(|e| e == t))
        .collect();
    sqlx::query(&format!(
        "LOCK TABLE {} IN ACCESS EXCLUSIVE MODE",
        tables
            .iter()
            .map(|t| format!("public.{t}"))
            .collect::<Vec<_>>()
            .join(", ")
    ))
    .execute(&mut *tx)
    .await?;
    let actual = catalog::read(&mut tx).await?;
    let differences = catalog::diff(&catalog::expected(), &actual);
    if !differences.is_empty() {
        tx.rollback().await?;
        return Err(StartupError::AdoptRefused(differences));
    }
    tx.ensure_migrations_table().await?;
    for m in MIGRATOR.iter() {
        sqlx::query(
            "INSERT INTO _sqlx_migrations (version, description, success, checksum, execution_time)
             VALUES ($1, $2, TRUE, $3, -1)",
        )
        .bind(m.version)
        .bind(format!("{ADOPT_DESCRIPTION} ({})", m.description))
        .bind(&*m.checksum)
        .execute(&mut *tx)
        .await?;
    }
    // The table locks do not cover a standalone `ALTER TYPE history`, which
    // locks only the type. Compare once more inside this transaction, just
    // before committing, so a type change that committed while the baseline
    // was being written is refused with nothing written.
    let differences = catalog::diff(&catalog::expected(), &catalog::read(&mut tx).await?);
    if !differences.is_empty() {
        tx.rollback().await?;
        return Err(StartupError::AdoptRefused(differences));
    }
    tx.commit().await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::search_order_is_pinned;

    fn v(s: &[&str]) -> Vec<String> {
        s.iter().map(|x| x.to_string()).collect()
    }

    #[test]
    fn only_the_pinned_search_order_is_accepted() {
        assert!(search_order_is_pinned(
            Some("public"),
            &v(&["pg_catalog", "public"])
        ));
        assert!(search_order_is_pinned(
            Some("public"),
            &v(&["pg_catalog", "public", "pg_temp_3"])
        ));
        // A role schema first, a temp schema first, or creation elsewhere.
        assert!(!search_order_is_pinned(
            Some("alrust"),
            &v(&["pg_catalog", "alrust", "public"])
        ));
        assert!(!search_order_is_pinned(
            Some("public"),
            &v(&["pg_temp_3", "pg_catalog", "public"])
        ));
        assert!(!search_order_is_pinned(
            Some("pg_catalog"),
            &v(&["pg_catalog", "public"])
        ));
        assert!(!search_order_is_pinned(
            Some("public"),
            &v(&["pg_catalog", "public", "other"])
        ));
        assert!(!search_order_is_pinned(None, &v(&["pg_catalog"])));
    }
}
