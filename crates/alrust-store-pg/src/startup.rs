//! Start-up: decide the database's state, then migrate, serve an existing
//! reference database as it is, or refuse (ADR-0005 decision 4).
//!
//! Everything runs on one dedicated connection holding `SQLx`'s own migration
//! lock, so a second alrust (or any `SQLx` migrator on this database) waits;
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
    /// An empty database: every migration applied.
    Fresh,
    /// An alrust database: pending migrations applied (none on a repeat start).
    Migrated,
    /// An exact reference database, served as it is. Nothing is written,
    /// on the first start or any later one (ADR-0005 decision 4).
    Adopted,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Report {
    pub outcome: Outcome,
    /// Non-internal triggers on the reference tables (allowed; logged).
    pub triggers: Vec<String>,
    /// Keys that can never authenticate because their expiry or scopes are NULL.
    pub inactive_keys: i64,
    /// Whether the note bindings are in force (`notes::bindings_in_force`).
    /// Never on an adopted database unless an operator installed them:
    /// notes are then added and listed, and not changed or deleted.
    pub note_bindings: bool,
}

#[derive(Debug, thiserror::Error)]
pub enum StartupError {
    #[error("database error: {0}")]
    Db(#[from] sqlx::Error),
    #[error("migration failed: {0}")]
    Migrate(#[from] MigrateError),
    #[error("refusing to serve the existing reference database; nothing was written. Differences from the reference schema: {}", .0.join("; "))]
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
///
/// # Errors
///
/// A refused layout, a failed migration or copy check, or the database
/// error, as `StartupError` says.
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
        // An existing reference database: no migrator and no baseline. Only
        // the read-only catalog check below decides, on every start, so
        // adoption can never write anything (ADR-0005 decision 4).
        (false, true, true) => Outcome::Adopted,
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

    // The check reads several catalogs. ACCESS SHARE on the reference tables
    // conflicts only with ACCESS EXCLUSIVE, so it keeps the DDL that takes
    // it (most ALTER TABLE forms, DROP, TRUNCATE) out of the middle of the
    // read without blocking writers, and needs only SELECT. It does not
    // exclude all catalog DDL: CREATE INDEX, some ALTER TABLE forms and a
    // standalone ALTER TYPE take weaker or different locks. The type's
    // attributes come from one statement (one consistent snapshot); the
    // catalog is not frozen beyond that. Whatever lands is runtime drift,
    // refused at the next start, and alrust has written nothing either way.
    let mut tx = conn.begin().await?;
    sqlx::query("SET LOCAL lock_timeout = '30s'")
        .execute(&mut *tx)
        .await?;
    let present: Vec<String> = sqlx::query_scalar(
        "SELECT relname::text FROM pg_class WHERE relnamespace = 'public'::regnamespace
            AND relkind = 'r' AND relname = ANY($1) ORDER BY relname",
    )
    .bind(&catalog::TABLES[..])
    .fetch_all(&mut *tx)
    .await?;
    if !present.is_empty() {
        sqlx::query(&format!(
            "LOCK TABLE {} IN ACCESS SHARE MODE",
            present
                .iter()
                .map(|t| format!("public.{t}"))
                .collect::<Vec<_>>()
                .join(", ")
        ))
        .execute(&mut *tx)
        .await?;
    }
    let actual = catalog::read(&mut tx).await?;
    let differences = catalog::diff(&catalog::expected(), &actual);
    let triggers = catalog::triggers(&mut tx).await?;
    let inactive_keys: i64 =
        sqlx::query_scalar("SELECT count(*) FROM keys WHERE expire_time IS NULL OR scopes IS NULL")
            .fetch_one(&mut *tx)
            .await?;
    let note_bindings = crate::notes::bindings_in_force(&mut tx).await?;
    tx.rollback().await?;
    if !differences.is_empty() {
        return Err(if outcome == Outcome::Adopted {
            StartupError::AdoptRefused(differences)
        } else {
            StartupError::CatalogMismatch(differences)
        });
    }
    Ok(Report {
        outcome,
        triggers,
        inactive_keys,
        note_bindings,
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

#[expect(
    clippy::fn_params_excessive_bools,
    reason = "the bool is a catalog fact being rendered for the log, not a mode switch"
)]
fn present(b: bool) -> &'static str {
    if b { "present" } else { "absent" }
}

#[cfg(test)]
mod tests {
    use super::search_order_is_pinned;

    fn v(s: &[&str]) -> Vec<String> {
        s.iter().map(std::string::ToString::to_string).collect()
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
