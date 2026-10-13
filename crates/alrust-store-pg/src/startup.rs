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
    // resolve through the search path; it must be exactly the pinned one.
    let setting: String = sqlx::query_scalar("SELECT current_setting('search_path')")
        .fetch_one(&mut *conn)
        .await?;
    if setting != crate::SEARCH_PATH {
        return Err(StartupError::SearchPath {
            expected: crate::SEARCH_PATH.to_string(),
            found: setting,
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

fn present(b: bool) -> &'static str {
    if b { "present" } else { "absent" }
}

/// Adopt an existing reference database: compare its catalog read-only, and
/// only if it is exact record the SQLx baseline. On any difference nothing
/// is written, not even `_sqlx_migrations`.
///
/// The reference tables that exist are locked before the comparison and
/// stay locked until the baseline commits, so no DDL (including `ALTER TYPE
/// history`, which must lock `alerts`) can land between the two. Writers
/// that are not alrust wait, or the lock times out and nothing is written.
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
    tx.commit().await?;
    Ok(())
}
