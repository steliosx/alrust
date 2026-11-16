//! ADR-0005: the reference layout, its three start-up states, the 0002
//! conversion and its refusals, the NULL rules and the session pinning.
//!
//! Each test creates its own database named `<prefix><n>_<µs>`, where the
//! prefix is `ALRUST_TEST_DB_PREFIX` (set by `scripts/store-db-run.sh`) or
//! `alrust_t_<pid>_`. A guard drops it when the test ends, also when the
//! test panics, and fails the test if the drop fails or the database is
//! still there. The wrapper additionally drops anything left under its
//! prefix (a killed process) and checks that none remain.
//!
//! Without `DATABASE_URL` the tests are skipped, except under CI (`CI` set),
//! where a missing database is a failure. Test sessions use alrust's
//! pinned search path but are moved to a far-from-UTC time zone after
//! connecting (`odd_zone_pool`) on purpose: nothing may depend on it.
#![expect(
    clippy::unwrap_used,
    clippy::print_stderr,
    clippy::too_many_lines,
    clippy::items_after_statements,
    reason = "integration test: helpers unwrap, skips are printed, and each scenario stays in one function"
)]

use alrust_store_pg::catalog;
use alrust_store_pg::{
    ALERT_BY_EXACT_ID, ALERT_BY_EXACT_ID_FOR_UPDATE, AlertCounts, AlertQuery, NewAlert, Outcome,
    ReceiveKind, StartupError, Store, StoreConfig, StoreError, session_options, value_text,
};
use chrono::{TimeZone, Utc};
use sqlx::postgres::{PgConnectOptions, PgPoolOptions};
use sqlx::{Executor, PgPool, Row};
use std::fmt::Write as _;
use std::sync::atomic::{AtomicU32, Ordering};

const SESSION_TIME_ZONE: &str = "Pacific/Chatham";
static COUNTER: AtomicU32 = AtomicU32::new(0);

const VERIFY_ALERTS: &str = include_str!("../src/verify/alerts.sql");
const VERIFY_HISTORY: &str = include_str!("../src/verify/history.sql");
const VERIFY_HEARTBEATS: &str = include_str!("../src/verify/heartbeats.sql");
const VERIFY_KEYS: &str = include_str!("../src/verify/keys.sql");
const ALPHA_SHAPE: &str = include_str!("../src/verify/alpha_shape.sql");
const ALPHA_SHAPE_EXPECTED: &str = include_str!("../src/verify/alpha_shape.expected");
const MIGRATION_0002: &str = include_str!("../migrations/0002_reference_layout.sql");
/// The header of 0002's verification blocks (other DO blocks declare other
/// variables), used to cut 0002 into the sections that precede them.
const VERIFY_BLOCK: &str = "DO $alrust$\nDECLARE\n    n bigint;";

/// An owned test database; dropped by `Drop`, whatever happens.
struct TestDb {
    base: PgConnectOptions,
    name: String,
    pool: PgPool,
}

impl TestDb {
    /// Another pool on this database, with the given session settings.
    async fn pool_with(&self, opts: PgConnectOptions) -> PgPool {
        PgPoolOptions::new()
            .max_connections(2)
            .connect_with(opts.database(&self.name))
            .await
            .expect("connect to test database")
    }

    /// Like `pool_with`, with every session moved to the odd time zone.
    async fn odd_zone_pool_with(&self, opts: PgConnectOptions) -> PgPool {
        odd_zone_pool(2)
            .connect_with(opts.database(&self.name))
            .await
            .expect("connect to test database")
    }
}

impl Drop for TestDb {
    fn drop(&mut self) {
        let admin = self.base.clone();
        let name = self.name.clone();
        // A fresh runtime on its own thread: Drop cannot await, and this
        // also runs while a failed test unwinds.
        let outcome = std::thread::spawn(move || -> Result<(), String> {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .map_err(|e| e.to_string())?;
            rt.block_on(async move {
                // Bounded: connecting, each statement and each lock wait.
                let pool = PgPoolOptions::new()
                    .max_connections(1)
                    .acquire_timeout(std::time::Duration::from_secs(10))
                    .connect_with(
                        admin.options([("statement_timeout", "20s"), ("lock_timeout", "10s")]),
                    )
                    .await
                    .map_err(|e| e.to_string())?;
                sqlx::query(&format!("DROP DATABASE IF EXISTS \"{name}\" WITH (FORCE)"))
                    .execute(&pool)
                    .await
                    .map_err(|e| e.to_string())?;
                let left: bool = sqlx::query_scalar(
                    "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = $1)",
                )
                .bind(&name)
                .fetch_one(&pool)
                .await
                .map_err(|e| e.to_string())?;
                pool.close().await;
                if left {
                    Err(format!("database {name} still exists after DROP"))
                } else {
                    Ok(())
                }
            })
        })
        .join()
        .unwrap_or_else(|_| Err("cleanup thread panicked".into()));
        if let Err(e) = outcome {
            let msg = format!("cleanup of test database {} failed: {e}", self.name);
            if std::thread::panicking() {
                eprintln!("{msg}");
            } else {
                panic!("{msg}");
            }
        }
    }
}

fn database_url() -> Option<String> {
    match std::env::var("DATABASE_URL") {
        Ok(u) if !u.is_empty() => Some(u),
        _ if std::env::var_os("CI").is_some() => panic!("DATABASE_URL is required under CI"),
        _ => {
            eprintln!("skipped: DATABASE_URL is not set");
            None
        }
    }
}

fn db_prefix() -> String {
    let p = std::env::var("ALRUST_TEST_DB_PREFIX")
        .unwrap_or_else(|_| format!("alrust_t_{}_", std::process::id()));
    assert!(
        p.starts_with("alrust_t_")
            && p.chars()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_'),
        "ALRUST_TEST_DB_PREFIX must be alrust_t_[a-z0-9_]*, not {p:?}"
    );
    p
}

/// The pinned test session: alrust's search path. (The odd time zone is set
/// after connecting, by `odd_zone_pool`.)
fn test_session(base: &PgConnectOptions) -> PgConnectOptions {
    base.clone()
        .options([("search_path", alrust_store_pg::SEARCH_PATH)])
}

/// Pool options whose sessions run `SET TIME ZONE` to the odd zone right
/// after connecting. A startup option cannot do it: `SQLx` always sends its
/// own `TimeZone=UTC` startup parameter, which wins (sqlx-postgres 0.8.6
/// connection/establish.rs; CI runs 38459899496 and 38461412602).
fn odd_zone_pool(max: u32) -> PgPoolOptions {
    PgPoolOptions::new()
        .max_connections(max)
        .after_connect(|conn, _meta| {
            Box::pin(async move {
                conn.execute(format!("SET TIME ZONE '{SESSION_TIME_ZONE}'").as_str())
                    .await?;
                Ok(())
            })
        })
}

async fn test_db() -> Option<TestDb> {
    let url = database_url()?;
    let base: PgConnectOptions = url.parse().expect("DATABASE_URL");
    let name = format!(
        "{}{}_{}",
        db_prefix(),
        COUNTER.fetch_add(1, Ordering::SeqCst),
        Utc::now().timestamp_micros()
    );
    {
        let admin = PgPoolOptions::new()
            .max_connections(1)
            .connect_with(base.clone())
            .await
            .expect("connect to DATABASE_URL");
        admin
            .execute(format!("CREATE DATABASE \"{name}\"").as_str())
            .await
            .expect("create test database");
        admin.close().await;
    }
    // From here on the guard owns the database, even if connecting fails.
    let pool = odd_zone_pool(4).connect_lazy_with(test_session(&base).database(&name));
    Some(TestDb { base, name, pool })
}

fn reference_schema() -> String {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../reference/alerta/alerta/sql/schema.sql"
    );
    std::fs::read_to_string(path).expect("reference/alerta checked out (git submodule)")
}

async fn apply_reference_schema(pool: &PgPool) {
    sqlx::raw_sql(&reference_schema())
        .execute(pool)
        .await
        .expect("reference schema.sql applies");
}

/// Apply only `0001` (the alpha schema), as an alpha database has it.
async fn apply_alpha(pool: &PgPool) {
    let all = sqlx::migrate!("./migrations");
    let alpha = sqlx::migrate::Migrator {
        migrations: std::borrow::Cow::Owned(all.migrations[..1].to_vec()),
        ..sqlx::migrate::Migrator::DEFAULT
    };
    alpha.run(pool).await.expect("0001 applies");
}

/// Alpha rows covering every field, NULLs, "", [], equal history times and
/// microsecond timestamps.
const ALPHA_ROWS: &str = r#"
INSERT INTO alerts (id, resource, event, environment, severity, correlate, status, service, "group",
    value, text, tags, attributes, origin, type, create_time, timeout, raw_data, customer,
    duplicate_count, repeat, previous_severity, trend_indication, receive_time, last_receive_id,
    last_receive_time, update_time, history)
VALUES
 ('a1', 'web01', 'HttpError', 'Production', 'major', '{HttpError,HttpOK}', 'ack', '{Web}', 'Web',
  '"500"', 'boom', '{}', '{"region": "eu", "n": 1}', 'smoke/host', 'exceptionAlert',
  '2026-10-13 12:00:00.123456+00', 86400, NULL, NULL, 5, true, 'minor', 'moreSevere',
  '2026-10-13 12:00:01.5+00', 'r5', '2026-10-13 12:05:00.25+00', '2026-10-13 12:06:00+00',
  '[{"id": "a1", "event": "HttpError", "severity": "major", "status": "ack", "value": "500",
     "text": "acked", "type": "ack", "updateTime": "2026-10-13T12:06:00.000Z", "user": "alice", "timeout": 7200},
    {"id": "a1", "event": "HttpError", "severity": "major", "status": "open", "value": null,
     "text": "", "type": "severity", "updateTime": "2026-10-13T12:01:00.000Z", "user": null, "timeout": 86400},
    {"id": "a1", "event": "HttpError", "severity": null, "status": "open", "value": "400",
     "text": null, "type": "new", "updateTime": "2026-10-13T12:01:00.000Z", "user": null}]'),
 ('a2', 'db01', 'Down', 'Development', 'critical', '{}', 'open', '{Db}', 'Db',
  'null', '', '{x,y}', '{}', 'o', 'exceptionAlert',
  '2026-10-13 00:00:00+00', NULL, 'raw', 'c1', 0, false, 'indeterminate', 'moreSevere',
  '2026-10-13 00:00:00+00', 'a2', '2026-10-13 00:00:00+00', '2026-10-13 00:00:00+00', '[]');
INSERT INTO heartbeats (id, origin, tags, attributes, type, create_time, timeout, receive_time, customer)
VALUES ('h1', 'origin-1', '{t}', '{"k": "v"}', 'Heartbeat', '2026-10-13 01:02:03.004+00', 60,
        '2026-10-13 01:02:04+00', NULL),
       ('h2', 'origin-2', '{}', '{}', 'Heartbeat', '2026-10-13 01:00:00+00', 0,
        '2026-10-13 01:00:00+00', 'c1');
INSERT INTO keys (id, key, "user", scopes, text, expire_time, count, last_used_time, customer)
VALUES ('k1', 'secret-1', 'admin', '{admin,read,write}', 'bootstrap', '2036-10-13 00:00:00+00', 3,
        '2026-10-13 02:00:00.5+00', NULL),
       ('k2', 'secret-2', 'bob', '{read}', '', '2027-01-01 00:00:00+00', 0, NULL, 'c1');
"#;

async fn alpha_db_with_rows() -> Option<TestDb> {
    let db = test_db().await?;
    apply_alpha(&db.pool).await;
    sqlx::raw_sql(ALPHA_ROWS)
        .execute(&db.pool)
        .await
        .expect("alpha rows");
    Some(db)
}

async fn catalog_of(pool: &PgPool) -> catalog::Catalog {
    let mut tx = pool.begin().await.unwrap();
    let c = catalog::read(&mut tx).await.unwrap();
    tx.rollback().await.unwrap();
    c
}

/// A checksum of every row of every listed table that exists.
async fn checksum(pool: &PgPool) -> Vec<(String, Option<String>)> {
    let mut out = Vec::new();
    for t in catalog::TABLES {
        let exists: bool = sqlx::query_scalar("SELECT to_regclass($1) IS NOT NULL")
            .bind(format!("public.{t}"))
            .fetch_one(pool)
            .await
            .unwrap();
        if exists {
            let sum: Option<String> = sqlx::query_scalar(&format!(
                "SELECT md5(string_agg(t::text, E'\\n' ORDER BY t::text)) FROM {t} t"
            ))
            .fetch_one(pool)
            .await
            .unwrap();
            out.push((t.to_string(), sum));
        }
    }
    out
}

async fn applied_versions(pool: &PgPool) -> Vec<(i64, String)> {
    sqlx::query_as("SELECT version, description FROM _sqlx_migrations ORDER BY version")
        .fetch_all(pool)
        .await
        .unwrap()
}

fn store(pool: &PgPool) -> Store {
    Store::new(pool.clone(), StoreConfig::default())
}

#[test]
fn migration_0002_embeds_the_reference_and_verification_sql_verbatim() {
    for q in [
        VERIFY_ALERTS,
        VERIFY_HISTORY,
        VERIFY_HEARTBEATS,
        VERIFY_KEYS,
    ] {
        assert!(
            MIGRATION_0002.contains(&format!("n := ({});", q.trim())),
            "verification query missing from 0002:\n{q}"
        );
    }
    assert!(MIGRATION_0002.contains(&format!("actual := ({});", ALPHA_SHAPE.trim())));
    // ADR-0005 decision 8 (b): the API's value expression, applied to the
    // alert value and to every history value.
    assert!(MIGRATION_0002.contains(&format!(
        "       {}, a.text, a.tags,",
        value_text("a.value")
    )));
    assert!(MIGRATION_0002.contains(&format!("{},", value_text("e->'value'"))));
    assert!(MIGRATION_0002.contains(&format!(
        "IS DISTINCT FROM $shape${}$shape$",
        ALPHA_SHAPE_EXPECTED.trim_end_matches('\n')
    )));
    let reference = reference_schema();
    let lines: Vec<&str> = reference.split('\n').collect();
    for (a, b) in [
        (2, 29),
        (32, 62),
        (65, 107),
        (109, 120),
        (136, 193),
        (195, 198),
    ] {
        let block = lines[a - 1..b].join("\n");
        assert!(
            MIGRATION_0002.contains(&block),
            "schema.sql lines {a}-{b} are not in 0002 verbatim"
        );
    }
}

#[tokio::test]
async fn reference_schema_matches_the_committed_catalog() {
    let Some(db) = test_db().await else { return };
    apply_reference_schema(&db.pool).await;
    apply_reference_schema(&db.pool).await; // idempotent, as the reference runs it at every start
    let actual = catalog_of(&db.pool).await;
    let d = catalog::diff(&catalog::expected(), &actual);
    assert!(
        d.is_empty(),
        "reference-catalog.json is stale: {d:#?}\nregenerate it from:\n{}",
        serde_json::to_string_pretty(&actual).unwrap()
    );
}

#[tokio::test]
async fn fresh_database_gets_the_reference_layout_and_repeat_start_is_a_no_op() {
    let Some(db) = test_db().await else { return };
    let s = store(&db.pool);
    let r = s.prepare().await.expect("fresh start");
    assert_eq!(r.outcome, Outcome::Fresh);
    assert_eq!(r.triggers, Vec::<String>::new());
    assert_eq!(
        catalog::diff(&catalog::expected(), &catalog_of(&db.pool).await),
        Vec::<String>::new()
    );
    let before: Vec<(i64, String, chrono::DateTime<Utc>)> = sqlx::query_as(
        "SELECT version, description, installed_on FROM _sqlx_migrations ORDER BY 1",
    )
    .fetch_all(&db.pool)
    .await
    .unwrap();
    assert_eq!(before.iter().map(|r| r.0).collect::<Vec<_>>(), [1, 2]);
    assert_eq!(s.prepare().await.unwrap().outcome, Outcome::Migrated);
    let after: Vec<(i64, String, chrono::DateTime<Utc>)> = sqlx::query_as(
        "SELECT version, description, installed_on FROM _sqlx_migrations ORDER BY 1",
    )
    .fetch_all(&db.pool)
    .await
    .unwrap();
    assert_eq!(before, after);
}

#[tokio::test]
async fn alpha_upgrade_converts_every_field_as_declared() {
    let Some(db) = alpha_db_with_rows().await else {
        return;
    };
    let s = store(&db.pool);
    assert_eq!(
        s.prepare().await.expect("upgrade").outcome,
        Outcome::Migrated
    );
    assert_eq!(
        catalog::diff(&catalog::expected(), &catalog_of(&db.pool).await),
        Vec::<String>::new()
    );

    // Independent expectations, written out by hand (not derived from 0002).
    let r = sqlx::query(
        "SELECT value, create_time::text AS ct, receive_time::text AS rt,
                last_receive_time::text AS lrt, update_time::text AS ut, timeout, duplicate_count,
                correlate, tags, attributes::text AS attrs, raw_data, customer, repeat
           FROM alerts WHERE id = 'a1'",
    )
    .fetch_one(&db.pool)
    .await
    .unwrap();
    assert_eq!(r.get::<Option<String>, _>("value").as_deref(), Some("500"));
    assert_eq!(r.get::<String, _>("ct"), "2026-10-13 12:00:00.123456");
    assert_eq!(r.get::<String, _>("rt"), "2026-10-13 12:00:01.5");
    assert_eq!(r.get::<String, _>("lrt"), "2026-10-13 12:05:00.25");
    assert_eq!(r.get::<String, _>("ut"), "2026-10-13 12:06:00");
    assert_eq!(r.get::<Option<i32>, _>("timeout"), Some(86400));
    assert_eq!(r.get::<Option<i32>, _>("duplicate_count"), Some(5));
    assert_eq!(
        r.get::<Vec<String>, _>("correlate"),
        ["HttpError", "HttpOK"]
    );
    assert_eq!(r.get::<Vec<String>, _>("tags"), Vec::<String>::new());
    assert_eq!(r.get::<String, _>("attrs"), r#"{"n": 1, "region": "eu"}"#);
    assert_eq!(r.get::<Option<String>, _>("raw_data"), None);
    assert_eq!(r.get::<Option<String>, _>("customer"), None);
    assert!(r.get::<bool, _>("repeat"));

    type H = (
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<i32>,
        i64,
    );
    let h: Vec<H> = sqlx::query_as(
        "SELECT h.id, h.event, h.severity, h.status, h.value, h.text, h.type,
                h.update_time::text, h.\"user\", h.timeout, h.n
           FROM alerts a, unnest(a.history) WITH ORDINALITY AS h(id, event, severity, status, value,
                text, type, update_time, \"user\", timeout, n)
          WHERE a.id = 'a1' ORDER BY h.n",
    )
    .fetch_all(&db.pool)
    .await
    .unwrap();
    let s_ = |v: &str| Some(v.to_string());
    assert_eq!(
        h,
        vec![
            (
                s_("a1"),
                s_("HttpError"),
                s_("major"),
                s_("ack"),
                s_("500"),
                s_("acked"),
                s_("ack"),
                s_("2026-10-13 12:06:00"),
                s_("alice"),
                Some(7200),
                1
            ),
            (
                s_("a1"),
                s_("HttpError"),
                s_("major"),
                s_("open"),
                None,
                s_(""),
                s_("severity"),
                s_("2026-10-13 12:01:00"),
                None,
                Some(86400),
                2
            ),
            (
                s_("a1"),
                s_("HttpError"),
                None,
                s_("open"),
                s_("400"),
                None,
                s_("new"),
                s_("2026-10-13 12:01:00"),
                None,
                None,
                3
            ),
        ]
    );

    let r = sqlx::query(
        "SELECT value, timeout, cardinality(history) AS n, raw_data, customer FROM alerts WHERE id = 'a2'",
    )
    .fetch_one(&db.pool)
    .await
    .unwrap();
    assert_eq!(r.get::<Option<String>, _>("value"), None);
    assert_eq!(r.get::<Option<i32>, _>("timeout"), None);
    assert_eq!(r.get::<i32, _>("n"), 0);
    assert_eq!(
        r.get::<Option<String>, _>("raw_data").as_deref(),
        Some("raw")
    );
    assert_eq!(
        r.get::<Option<String>, _>("customer").as_deref(),
        Some("c1")
    );

    let hb: Vec<(String, String, i32, String, String)> = sqlx::query_as(
        "SELECT id, create_time::text, timeout, receive_time::text, attributes::text
           FROM heartbeats ORDER BY id",
    )
    .fetch_all(&db.pool)
    .await
    .unwrap();
    assert_eq!(
        hb,
        vec![
            (
                "h1".into(),
                "2026-10-13 01:02:03.004".into(),
                60,
                "2026-10-13 01:02:04".into(),
                r#"{"k": "v"}"#.into()
            ),
            (
                "h2".into(),
                "2026-10-13 01:00:00".into(),
                0,
                "2026-10-13 01:00:00".into(),
                "{}".into()
            ),
        ]
    );
    let keys: Vec<(String, String, Option<String>, Option<i64>)> = sqlx::query_as(
        "SELECT id, expire_time::text, last_used_time::text, count FROM keys ORDER BY id",
    )
    .fetch_all(&db.pool)
    .await
    .unwrap();
    assert_eq!(
        keys,
        vec![
            (
                "k1".into(),
                "2036-10-13 00:00:00".into(),
                Some("2026-10-13 02:00:00.5".into()),
                Some(3)
            ),
            ("k2".into(), "2027-01-01 00:00:00".into(), None, Some(0)),
        ]
    );

    // The store reads the converted rows back; history keeps storage order.
    let a = s.get_alert("a1", None).await.unwrap();
    assert_eq!(a.history.len(), 3);
    assert_eq!(a.history[0].change_type, "ack");
    assert_eq!(a.history[2].change_type, "new");
    assert_eq!(
        a.create_time,
        Utc.with_ymd_and_hms(2026, 10, 13, 12, 0, 0).unwrap()
            + chrono::Duration::microseconds(123_456)
    );

    assert_eq!(s.prepare().await.unwrap().outcome, Outcome::Migrated);
}

/// Each fixture makes 0002 refuse. The alpha catalog, every row and the
/// migration history must be exactly as before.
#[tokio::test]
async fn alpha_upgrade_refusals_change_nothing() {
    let cases: &[(&str, &str)] = &[
        (
            "UPDATE alerts SET timeout = 3000000000 WHERE id = 'a1'",
            "outside integer range",
        ),
        (
            "UPDATE alerts SET duplicate_count = 3000000000 WHERE id = 'a1'",
            "outside integer range",
        ),
        (
            "UPDATE alerts SET history = jsonb_set(history, '{0,timeout}', '3000000000') WHERE id = 'a1'",
            "outside integer range",
        ),
        (
            "UPDATE heartbeats SET timeout = 3000000000 WHERE id = 'h1'",
            "heartbeats with timeout outside integer range",
        ),
        (
            "INSERT INTO alerts SELECT 'a3', resource, event, environment, 'minor', correlate, status, service,
                    \"group\", value, text, tags, attributes, origin, type, create_time, timeout, raw_data,
                    customer, duplicate_count, repeat, previous_severity, trend_indication, receive_time,
                    'a3', last_receive_time, update_time, history FROM alerts WHERE id = 'a1'",
            "alerts sharing (environment, resource, event, customer)",
        ),
        (
            "CREATE VIEW v AS SELECT id FROM alerts",
            "v depends on alerts",
        ),
        (
            "CREATE FUNCTION f() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END$$;
             CREATE TRIGGER t BEFORE UPDATE ON alerts FOR EACH ROW EXECUTE FUNCTION f()",
            "trigger t on alerts",
        ),
        (
            "GRANT SELECT ON alerts TO PUBLIC",
            "non-default privileges on alerts",
        ),
        (
            "CREATE TABLE notes (id text)",
            "notes already exists",
        ),
        (
            "ALTER TABLE alerts ADD COLUMN extra text; UPDATE alerts SET extra = 'kept data'",
            "the alpha tables differ from what 0001 created",
        ),
        (
            "ALTER TABLE heartbeats ADD COLUMN extra integer DEFAULT 1",
            "the alpha tables differ from what 0001 created",
        ),
        (
            "ALTER TABLE alerts ADD CONSTRAINT extra_check CHECK (duplicate_count >= 0)",
            "the alpha tables differ from what 0001 created",
        ),
        (
            "CREATE TABLE extra_customers (id text PRIMARY KEY); INSERT INTO extra_customers VALUES ('c1'); ALTER TABLE heartbeats ADD CONSTRAINT extra_fk FOREIGN KEY (customer) REFERENCES extra_customers (id)",
            "the alpha tables differ from what 0001 created",
        ),
        (
            "CREATE INDEX extra_idx ON keys (\"user\")",
            "the alpha tables differ from what 0001 created",
        ),
        (
            "CREATE RULE extra_rule AS ON UPDATE TO heartbeats DO ALSO NOTIFY alrust",
            "the alpha tables differ from what 0001 created",
        ),
        (
            "ALTER TABLE heartbeats SET UNLOGGED",
            "the alpha tables differ from what 0001 created",
        ),
        (
            "ALTER TABLE alerts SET (fillfactor = 50)",
            "the alpha tables differ from what 0001 created",
        ),
        (
            "ALTER TABLE keys ADD COLUMN extra text",
            "the alpha tables differ from what 0001 created",
        ),
        (
            "ALTER TABLE keys ALTER COLUMN text SET DEFAULT 'x'",
            "the alpha tables differ from what 0001 created",
        ),
        (
            "ALTER TABLE alerts ENABLE ROW LEVEL SECURITY",
            "row-level security on alerts",
        ),
        (
            "CREATE PUBLICATION extra_pub FOR TABLES IN SCHEMA public",
            "publication extra_pub includes schema public",
        ),
        (
            "UPDATE alerts SET history = jsonb_set(history, '{0,severity}', 'false') WHERE id = 'a1'",
            "a history entry field of the wrong JSON type",
        ),
        (
            "UPDATE alerts SET history = jsonb_set(history, '{0,event}', '5') WHERE id = 'a1'",
            "a history entry field of the wrong JSON type",
        ),
        (
            "UPDATE alerts SET history = jsonb_set(history, '{1,text}', '[]') WHERE id = 'a1'",
            "a history entry field of the wrong JSON type",
        ),
        (
            "UPDATE alerts SET history = jsonb_set(history, '{0,user}', '{\"a\": 1}') WHERE id = 'a1'",
            "a history entry field of the wrong JSON type",
        ),
        (
            "UPDATE alerts SET history = jsonb_set(history, '{0,timeout}', '\"7200\"') WHERE id = 'a1'",
            "a history entry field of the wrong JSON type",
        ),
        (
            "UPDATE alerts SET history = jsonb_set(history, '{0,timeout}', '1.5') WHERE id = 'a1'",
            "a history entry field of the wrong JSON type",
        ),
        (
            "UPDATE alerts SET history = jsonb_set(history, '{0,updateTime}', '5') WHERE id = 'a1'",
            "a history entry field of the wrong JSON type",
        ),
        (
            "UPDATE alerts SET history = history || '[{\"id\": \"a1\", \"bogus\": 1}]' WHERE id = 'a1'",
            "the history type cannot hold",
        ),
        (
            "UPDATE alerts SET history = '{\"not\": \"an array\"}' WHERE id = 'a2'",
            "the history type cannot hold",
        ),
    ];
    for &(setup, expected) in cases {
        let Some(db) = alpha_db_with_rows().await else {
            return;
        };
        if let Err(e) = sqlx::raw_sql(setup).execute(&db.pool).await {
            // Schema publications need superuser; CI's role has it.
            if setup.contains("PUBLICATION")
                && e.as_database_error()
                    .and_then(sqlx::error::DatabaseError::code)
                    .as_deref()
                    == Some("42501")
            {
                eprintln!("NOT_RUN (needs superuser): {setup}");
                continue;
            }
            panic!("{setup}: {e}");
        }
        let catalog_before = catalog_of(&db.pool).await;
        let rows_before = checksum(&db.pool).await;
        let err = store(&db.pool).prepare().await.expect_err(setup);
        assert!(
            matches!(err, StartupError::Migrate(_)) && err.to_string().contains(expected),
            "{setup}: {err}"
        );
        assert_eq!(catalog_of(&db.pool).await, catalog_before, "{setup}");
        assert_eq!(checksum(&db.pool).await, rows_before, "{setup}");
        let versions: Vec<i64> = applied_versions(&db.pool)
            .await
            .into_iter()
            .map(|v| v.0)
            .collect();
        assert_eq!(versions, [1], "{setup}");
    }
}

/// Everything PostgreSQL knows about one sequence, read without advancing
/// it: identity, owning column, parameters and state.
#[derive(Debug, PartialEq, sqlx::FromRow)]
struct SequenceFacts {
    oid: i64,
    owner: Option<String>,
    deptype: Option<String>,
    start: i64,
    increment: i64,
    max: i64,
    min: i64,
    cache: i64,
    cycle: bool,
    last_value: i64,
    is_called: bool,
}

async fn sequence_facts(pool: &PgPool, name: &str) -> Option<SequenceFacts> {
    let exists: bool = sqlx::query_scalar("SELECT to_regclass($1) IS NOT NULL")
        .bind(format!("public.{name}"))
        .fetch_one(pool)
        .await
        .unwrap();
    if !exists {
        return None;
    }
    Some(
        sqlx::query_as(&format!(
            "SELECT c.oid::bigint AS oid,
                    (SELECT d.refobjid::regclass::text || '.' || a.attname
                       FROM pg_depend d
                       JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
                      WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
                        AND d.refclassid = 'pg_class'::regclass AND d.refobjsubid > 0) AS owner,
                    (SELECT d.deptype::text FROM pg_depend d
                      WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
                        AND d.refclassid = 'pg_class'::regclass AND d.refobjsubid > 0) AS deptype,
                    s.seqstart AS start, s.seqincrement AS increment, s.seqmax AS max,
                    s.seqmin AS min, s.seqcache AS cache, s.seqcycle AS cycle,
                    v.last_value, v.is_called
               FROM pg_class c
               JOIN pg_sequence s ON s.seqrelid = c.oid
               CROSS JOIN public.{name} v
              WHERE c.oid = 'public.{name}'::regclass"
        ))
        .fetch_one(pool)
        .await
        .unwrap(),
    )
}

/// ADR5-OWNED-SEQ-1: a sequence owned by an alert or heartbeat column, with
/// no column default, would be dropped with the old table. The preflight
/// refuses it and leaves the sequence, the catalog, the rows and the
/// migration versions exactly as they were. An unowned sequence in `public`
/// is not alrust's business: the upgrade proceeds and leaves it untouched.
#[tokio::test]
async fn owned_sequences_are_refused_and_unowned_ones_survive() {
    for (table, column) in [("alerts", "timeout"), ("heartbeats", "timeout")] {
        let Some(db) = alpha_db_with_rows().await else {
            return;
        };
        sqlx::raw_sql(&format!(
            "CREATE SEQUENCE public.alpha_counter INCREMENT 3 MINVALUE 2 MAXVALUE 900 START 5 CACHE 4 CYCLE
                 OWNED BY public.{table}.{column};
             SELECT setval('public.alpha_counter', 123, true)"
        ))
        .execute(&db.pool)
        .await
        .unwrap();
        let seq_before = sequence_facts(&db.pool, "alpha_counter").await;
        assert_eq!(
            seq_before.as_ref().map(|f| (
                f.owner.as_deref(),
                f.deptype.as_deref(),
                f.last_value,
                f.is_called
            )),
            Some((
                Some(format!("{table}.{column}").as_str()),
                Some("a"),
                123,
                true
            )),
            "{table}: fixture"
        );
        let catalog_before = catalog_of(&db.pool).await;
        let rows_before = checksum(&db.pool).await;
        let err = store(&db.pool).prepare().await.expect_err(table);
        let expected = format!("owned sequence alpha_counter depends on {table}.{column}");
        assert!(
            matches!(err, StartupError::Migrate(_)) && err.to_string().contains(&expected),
            "{table}: {err}"
        );
        assert_eq!(
            sequence_facts(&db.pool, "alpha_counter").await,
            seq_before,
            "{table}: sequence"
        );
        assert_eq!(
            catalog_of(&db.pool).await,
            catalog_before,
            "{table}: catalog"
        );
        assert_eq!(checksum(&db.pool).await, rows_before, "{table}: rows");
        let versions: Vec<i64> = applied_versions(&db.pool)
            .await
            .into_iter()
            .map(|v| v.0)
            .collect();
        assert_eq!(versions, [1], "{table}: versions");
    }

    // Control: an unowned sequence is preserved through a successful upgrade.
    let Some(db) = alpha_db_with_rows().await else {
        return;
    };
    sqlx::raw_sql(
        "CREATE SEQUENCE public.free_counter INCREMENT 2 START 7;
         SELECT setval('public.free_counter', 777, false)",
    )
    .execute(&db.pool)
    .await
    .unwrap();
    let seq_before = sequence_facts(&db.pool, "free_counter").await;
    assert_eq!(
        seq_before
            .as_ref()
            .map(|f| (f.owner.clone(), f.last_value, f.is_called)),
        Some((None, 777, false))
    );
    let r = store(&db.pool)
        .prepare()
        .await
        .expect("upgrade with an unowned sequence");
    assert_eq!(r.outcome, Outcome::Migrated);
    assert_eq!(
        sequence_facts(&db.pool, "free_counter").await,
        seq_before,
        "unowned sequence"
    );
    let versions: Vec<i64> = applied_versions(&db.pool)
        .await
        .into_iter()
        .map(|v| v.0)
        .collect();
    assert_eq!(versions, [1, 2]);
}

/// ADR-0005 decision 8 (b): a float, array or object `value` becomes its
/// `jsonb` text, both for alpha rows and history values converted by 0002
/// and for an alert received through the store, and the two agree.
#[tokio::test]
async fn non_string_values_become_jsonb_text_the_same_way_everywhere() {
    let Some(db) = alpha_db_with_rows().await else {
        return;
    };
    sqlx::raw_sql(
        "UPDATE alerts SET value = '1.50' WHERE id = 'a1';
         UPDATE alerts SET history = jsonb_set(history, '{1,value}', '[1, 2]') WHERE id = 'a1';
         UPDATE alerts SET value = '{\"b\": 1, \"a\": [true, null, 2.5]}' WHERE id = 'a2'",
    )
    .execute(&db.pool)
    .await
    .unwrap();
    let report = store(&db.pool).prepare().await.expect("upgrade");
    assert_eq!(report.outcome, Outcome::Migrated);
    let a1: Option<String> = sqlx::query_scalar("SELECT value FROM alerts WHERE id = 'a1'")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert_eq!(a1.as_deref(), Some("1.50"));
    let history_value: Option<String> =
        sqlx::query_scalar("SELECT (history[2]).value FROM alerts WHERE id = 'a1'")
            .fetch_one(&db.pool)
            .await
            .unwrap();
    assert_eq!(history_value.as_deref(), Some("[1, 2]"));
    let a2: Option<String> = sqlx::query_scalar("SELECT value FROM alerts WHERE id = 'a2'")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert_eq!(a2.as_deref(), Some("{\"a\": [true, null, 2.5], \"b\": 1}"));

    // The API path: the same expression, on the receive transaction.
    let st = store(&db.pool);
    let mut object_alert = new_alert("v1", 300);
    object_alert.resource = "value-host".into();
    object_alert.value = Some(serde_json::json!({"b": 1, "a": [true, null, 2.5]}));
    let (alert, kind) = st.receive(object_alert.clone(), "alice").await.unwrap();
    assert_eq!(kind, ReceiveKind::Created);
    assert_eq!(
        alert.value.as_deref(),
        Some("{\"a\": [true, null, 2.5], \"b\": 1}")
    );
    assert_eq!(
        alert.history[0].value.as_deref(),
        Some("{\"a\": [true, null, 2.5], \"b\": 1}")
    );
    // Repeating it is a duplicate with an unchanged value, not a value change.
    let (alert, kind) = st.receive(object_alert, "alice").await.unwrap();
    assert_eq!(kind, ReceiveKind::Duplicate);
    assert_eq!(alert.history.len(), 1, "{:?}", alert.history);
    let mut float_alert = new_alert("v2", 300);
    float_alert.resource = "value-host".into();
    float_alert.value = Some(serde_json::json!(1.5));
    let (alert, _) = st.receive(float_alert, "alice").await.unwrap();
    assert_eq!(alert.value.as_deref(), Some("1.5"));
}

/// Runs 0002's conversion statements (taken from the migration file itself)
/// in a transaction, then shows that the verification queries count a
/// changed field and a swapped history ordinal.
#[tokio::test]
async fn copy_verification_detects_any_changed_field() {
    let Some(db) = alpha_db_with_rows().await else {
        return;
    };
    let section = |from: &str, to: &str| {
        let a = MIGRATION_0002.find(from).expect(from);
        let b = MIGRATION_0002[a..].find(to).expect(to) + a;
        MIGRATION_0002[a..b].to_string()
    };
    let mut tx = db.pool.begin().await.unwrap();
    sqlx::raw_sql(&section("-- 3.2:", "-- 3.3.4:"))
        .execute(&mut *tx)
        .await
        .unwrap();
    sqlx::raw_sql(&section("-- 3.4:", VERIFY_BLOCK))
        .execute(&mut *tx)
        .await
        .unwrap();
    sqlx::raw_sql(&section("-- 3.5:", VERIFY_BLOCK))
        .execute(&mut *tx)
        .await
        .unwrap();
    let count = |q: &'static str| q.trim().to_string();
    for q in [
        VERIFY_ALERTS,
        VERIFY_HISTORY,
        VERIFY_HEARTBEATS,
        VERIFY_KEYS,
    ] {
        let n: i64 = sqlx::query_scalar(&count(q))
            .fetch_one(&mut *tx)
            .await
            .unwrap();
        assert_eq!(n, 0, "faithful copy must verify:\n{q}");
    }
    let perturb: [(&str, &str); 7] = [
        (
            "UPDATE alerts SET text = 'changed' WHERE id = 'a1'",
            VERIFY_ALERTS,
        ),
        (
            "UPDATE alerts SET last_receive_time = last_receive_time + interval '1 microsecond' WHERE id = 'a2'",
            VERIFY_ALERTS,
        ),
        (
            "UPDATE alerts SET value = NULL WHERE id = 'a1'",
            VERIFY_ALERTS,
        ),
        (
            "UPDATE alerts SET history = ARRAY[history[2], history[1], history[3]] WHERE id = 'a1'",
            VERIFY_HISTORY,
        ),
        (
            "UPDATE alerts SET history = history[1:2] WHERE id = 'a1'",
            VERIFY_HISTORY,
        ),
        (
            "UPDATE heartbeats SET customer = 'x' WHERE id = 'h1'",
            VERIFY_HEARTBEATS,
        ),
        (
            "UPDATE keys SET count = count + 1 WHERE id = 'k2'",
            VERIFY_KEYS,
        ),
    ];
    for (change, q) in perturb {
        sqlx::query("SAVEPOINT p").execute(&mut *tx).await.unwrap();
        sqlx::raw_sql(change).execute(&mut *tx).await.expect(change);
        let n: i64 = sqlx::query_scalar(&count(q))
            .fetch_one(&mut *tx)
            .await
            .unwrap();
        assert!(n > 0, "{change} was not detected");
        sqlx::query("ROLLBACK TO SAVEPOINT p")
            .execute(&mut *tx)
            .await
            .unwrap();
    }
    tx.rollback().await.unwrap();
}

/// Reference-written rows, including NULLs the reference model fills in or
/// leaves NULL, and keys with a NULL expiry or NULL scopes.
const REFERENCE_ROWS: &str = r#"
INSERT INTO alerts (id, resource, event, environment, severity, correlate, status, service, "group",
    value, text, tags, attributes, origin, type, create_time, timeout, raw_data, customer,
    duplicate_count, repeat, previous_severity, trend_indication, receive_time, last_receive_id,
    last_receive_time, history, update_time)
VALUES ('r1', 'web01', 'HttpError', NULL, 'major', NULL, 'open', '{Web}', NULL, 'some text', NULL,
        NULL, NULL, 'alertad/host', 'exceptionAlert', '2026-10-13 12:00:00', NULL, NULL, 'c1', 0, false,
        'indeterminate', 'moreSevere', '2026-10-13 12:00:00', 'r1', '2026-10-13 12:00:00',
        ARRAY[ROW('r1', 'HttpError', 'major', 'open', 'some text', 'new alert', 'new',
                  '2026-10-13 12:00:00', NULL, NULL)::history],
        NULL);
INSERT INTO alerts (id, resource, event, environment, severity, correlate, status, service, "group",
    value, text, tags, attributes, origin, type, create_time, timeout, raw_data, customer,
    duplicate_count, repeat, previous_severity, trend_indication, receive_time, last_receive_id,
    last_receive_time, history, update_time)
VALUES ('r2', 'db01', 'Down', 'Production', '', '{}', '', '{Db}', '', NULL, '', '{}', '{}',
        'alertad/host', '', '2026-10-13 12:00:00', 86400, NULL, NULL, 0, false, 'indeterminate',
        'moreSevere', '2026-10-13 12:00:00', 'r2', '2026-10-13 12:00:00', '{}', '2026-10-13 12:00:00'),
       ('r3', 'mq01', 'QueueFull', 'Production', 'major', '{}', 'open', '{Mq}', 'Mq', NULL, '', '{}',
        '{}', 'alertad/host', 'exceptionAlert', '2026-10-13 12:00:00', 86400, NULL, NULL, NULL, NULL,
        NULL, NULL, '2026-10-13 12:00:00', NULL, NULL, '{}', NULL);
INSERT INTO heartbeats (id, origin, tags, type, create_time, timeout, receive_time, customer, attributes)
VALUES ('hb1', 'origin-1', NULL, NULL, '2026-10-13 12:00:00', NULL, '2026-10-13 12:00:00', NULL, NULL),
       ('hb2', 'origin-2', '{}', '', '2026-10-13 12:00:00', 60, '2026-10-13 12:00:00', NULL, '{}');
INSERT INTO keys (id, key, "user", scopes, text, expire_time, count, last_used_time, customer)
VALUES ('ok', 'secret-ok', 'alice', '{read}', 'k', '2099-01-01 00:00:00', 1, NULL, NULL),
       ('noexp', 'secret-noexp', 'alice', '{admin}', NULL, NULL, 2, NULL, NULL),
       ('noscope', 'secret-noscope', 'alice', NULL, NULL, '2099-01-01 00:00:00', NULL, NULL, NULL);
INSERT INTO notes (id, text, "user", attributes, type, create_time, update_time, alert, customer)
VALUES ('n1', 'a note', 'alice', '{}', 'alert', '2026-10-13 12:00:00', NULL, 'r1', NULL);
INSERT INTO users (id, name, login, email, password, status, roles, attributes, create_time,
    last_login, text, update_time, email_verified, hash)
VALUES ('u1', 'Alice', 'alice', 'alice@example.invalid', 'x', 'active', '{user}', '{}',
        '2026-10-13 12:00:00', NULL, '', NULL, true, NULL);
"#;

#[tokio::test]
async fn adoption_writes_nothing_and_changes_no_row() {
    let Some(db) = test_db().await else { return };
    apply_reference_schema(&db.pool).await;
    sqlx::raw_sql(REFERENCE_ROWS)
        .execute(&db.pool)
        .await
        .unwrap();
    let rows_before = checksum(&db.pool).await;
    assert_eq!(rows_before.len(), 10);

    // Positive proof of "no DML": every reference table rejects any write
    // while the two starts run (statement-level, so even zero-row writes).
    let mut deny = String::from(
        "CREATE FUNCTION deny_writes() RETURNS trigger LANGUAGE plpgsql AS $$
         BEGIN RAISE EXCEPTION 'adoption wrote to %', TG_TABLE_NAME; END $$;",
    );
    for t in catalog::TABLES {
        write!(
            deny,
            "CREATE TRIGGER deny_writes BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON {t}
             FOR EACH STATEMENT EXECUTE FUNCTION deny_writes();"
        )
        .unwrap();
    }
    sqlx::raw_sql(&deny).execute(&db.pool).await.unwrap();

    let s = store(&db.pool);
    let r = s.prepare().await.expect("adopt");
    assert_eq!(r.outcome, Outcome::Adopted);
    assert_eq!(r.inactive_keys, 2);
    assert_eq!(
        r.triggers.len(),
        10,
        "the deny triggers are listed, not refused"
    );
    assert_eq!(s.prepare().await.unwrap().outcome, Outcome::Adopted);
    let has_history: bool =
        sqlx::query_scalar("SELECT to_regclass('public._sqlx_migrations') IS NOT NULL")
            .fetch_one(&db.pool)
            .await
            .unwrap();
    assert!(!has_history, "adoption records no baseline");
    let mut allow = String::new();
    for t in catalog::TABLES {
        write!(allow, "DROP TRIGGER deny_writes ON {t};").unwrap();
    }
    sqlx::raw_sql(&allow).execute(&db.pool).await.unwrap();
    assert_eq!(checksum(&db.pool).await, rows_before);

    // Reads apply the reference model's constants, never invented values.
    let a = s.get_alert("r1", None).await.unwrap();
    assert_eq!(a.environment, "");
    assert_eq!(a.group, "Misc");
    assert_eq!(a.text, "");
    assert!(a.correlate.is_empty() && a.tags.is_empty() && a.attributes.is_empty());
    assert_eq!(a.update_time, None);
    assert_eq!(a.timeout, None);
    assert_eq!(a.value.as_deref(), Some("some text"));
    assert_eq!(a.history[0].user, None);
    let hb = s.get_heartbeat("hb1").await.unwrap();
    assert_eq!(hb.timeout, StoreConfig::default().alert_timeout);
    assert_eq!(hb.event_type, "Heartbeat");

    // Keys with a NULL expiry or NULL scopes never authenticate, and their
    // use is not recorded (ADR-0005 decision 7).
    assert!(s.use_key("secret-noexp").await.unwrap().is_none());
    assert!(s.use_key("secret-noscope").await.unwrap().is_none());
    let k = s.use_key("secret-ok").await.unwrap().expect("active key");
    assert_eq!(k.count, 2);
    let after: Vec<(String, Option<i64>, Option<chrono::NaiveDateTime>)> =
        sqlx::query_as("SELECT id, count, last_used_time FROM keys WHERE id <> 'ok' ORDER BY id")
            .fetch_all(&db.pool)
            .await
            .unwrap();
    assert_eq!(
        after,
        vec![
            ("noexp".into(), Some(2), None),
            ("noscope".into(), None, None)
        ]
    );
    let listed = s.list_keys(None).await.unwrap();
    let noexp = listed.iter().find(|k| k.id == "noexp").unwrap();
    assert_eq!(noexp.expire_time, None);
    let noscope = listed.iter().find(|k| k.id == "noscope").unwrap();
    assert_eq!(noscope.scopes, None);
}

#[tokio::test]
async fn adoption_refuses_a_different_catalog_without_writing() {
    for change in [
        "ALTER TABLE alerts ADD COLUMN extra text",
        "CREATE INDEX extra_idx ON alerts (status)",
        "ALTER TABLE keys ALTER COLUMN count TYPE integer",
        "DROP TABLE groups",
        "ALTER TABLE alerts ENABLE ROW LEVEL SECURITY; ALTER TABLE alerts FORCE ROW LEVEL SECURITY",
        "CREATE POLICY extra_policy ON keys USING (true)",
        "CREATE TABLE alerts_child () INHERITS (alerts)",
        "ALTER TABLE heartbeats SET UNLOGGED",
        "ALTER TABLE notes ALTER COLUMN text TYPE text COLLATE \"C\"",
    ] {
        let Some(db) = test_db().await else { return };
        apply_reference_schema(&db.pool).await;
        sqlx::raw_sql(change).execute(&db.pool).await.unwrap();
        let catalog_before = catalog_of(&db.pool).await;
        let err = store(&db.pool).prepare().await.expect_err(change);
        assert!(
            matches!(err, StartupError::AdoptRefused(_)),
            "{change}: {err}"
        );
        let has_history: bool =
            sqlx::query_scalar("SELECT to_regclass('public._sqlx_migrations') IS NOT NULL")
                .fetch_one(&db.pool)
                .await
                .unwrap();
        assert!(!has_history, "{change}: nothing may be written");
        assert_eq!(catalog_of(&db.pool).await, catalog_before, "{change}");
    }
}

#[tokio::test]
async fn unrecognised_states_are_refused_without_writing() {
    for setup in [
        "CREATE TABLE notes (id text)",
        "CREATE TYPE history AS (id text)",
        "CREATE TABLE _sqlx_migrations (version bigint)",
    ] {
        let Some(db) = test_db().await else { return };
        sqlx::raw_sql(setup).execute(&db.pool).await.unwrap();
        let before = catalog_of(&db.pool).await;
        let err = store(&db.pool).prepare().await.expect_err(setup);
        assert!(
            matches!(err, StartupError::Unrecognised(_)),
            "{setup}: {err}"
        );
        assert_eq!(catalog_of(&db.pool).await, before, "{setup}");
    }
}

/// `count_alerts` gives exactly the totals and grouped counts of
/// `find_alerts` for the same query: empty, matching, excluding,
/// customer-restricted and unrestricted selections.
#[tokio::test]
async fn count_alerts_matches_the_list_totals() {
    let Some(db) = test_db().await else { return };
    let s = store(&db.pool);
    s.prepare().await.expect("fresh start");
    for (id, resource, event, severity, customer) in [
        ("c1", "web01", "HttpError", "major", None),
        ("c2", "web02", "HttpError", "critical", None),
        ("c3", "db01", "DiskFull", "critical", Some("acme")),
        ("c4", "db02", "DiskFull", "minor", Some("other")),
    ] {
        let mut n = new_alert(id, 3600);
        n.resource = resource.into();
        n.event = event.into();
        n.severity = severity.into();
        n.customer = customer.map(Into::into);
        s.receive(n, "alice").await.expect("receive");
    }
    s.take_action("c2", None, "ack", "seen", None, "alice")
        .await
        .expect("ack");
    let queries = [
        AlertQuery::default(),
        AlertQuery {
            events: vec!["HttpError".into()],
            ..AlertQuery::default()
        },
        AlertQuery {
            not_events: vec!["HttpError".into()],
            ..AlertQuery::default()
        },
        AlertQuery {
            severities: vec!["critical".into()],
            not_statuses: vec!["ack".into()],
            ..AlertQuery::default()
        },
        AlertQuery {
            events: vec!["NoSuchEvent".into()],
            ..AlertQuery::default()
        },
        AlertQuery {
            customers: Some(vec!["acme".into()]),
            ..AlertQuery::default()
        },
        AlertQuery {
            customers: Some(vec![]),
            ..AlertQuery::default()
        },
    ];
    let mut totals = Vec::new();
    for q in &queries {
        let c = s.count_alerts(q).await.expect("count");
        let p = s
            .find_alerts(q, 1, 1, alrust_domain::HistoryOutput::Omit)
            .await
            .expect("list");
        assert_eq!(
            c,
            AlertCounts {
                total: p.total,
                severity_counts: p.severity_counts,
                status_counts: p.status_counts,
            },
            "{q:?}"
        );
        totals.push(c.total);
    }
    assert_eq!(totals, [4, 2, 2, 1, 0, 1, 0]);
    let all = s.count_alerts(&AlertQuery::default()).await.unwrap();
    assert_eq!(
        serde_json::Value::Object(all.severity_counts),
        serde_json::json!({"critical": 2, "major": 1, "minor": 1})
    );
    assert_eq!(
        serde_json::Value::Object(all.status_counts),
        serde_json::json!({"ack": 1, "open": 3})
    );
}

/// An exact id wins over a longer id that starts with it and over another
/// alert's `lastReceiveId`, on the read, lock (action) and delete paths;
/// prefixes and `lastReceiveId` still find their alert; the customer
/// restriction applies to both the exact and the prefix path.
#[tokio::test]
async fn id_lookup_takes_the_exact_id_first_and_keeps_the_prefix_match() {
    let Some(db) = test_db().await else { return };
    let s = store(&db.pool);
    s.prepare().await.expect("fresh start");
    let mk = |id: &str, resource: &str, customer: Option<&str>| {
        let mut n = new_alert(id, 3600);
        n.resource = resource.into();
        n.customer = customer.map(Into::into);
        n
    };
    // Inserted first, so an unordered prefix scan for "aa11" meets it first.
    s.receive(mk("aa1199", "r-long", None), "alice")
        .await
        .unwrap();
    s.receive(mk("aa11", "r-short", None), "alice")
        .await
        .unwrap();
    s.receive(mk("e1", "r-e", None), "alice").await.unwrap();
    s.receive(mk("q1", "r-q", None), "alice").await.unwrap();
    // A duplicate of e1 whose incoming id is q1: e1's lastReceiveId is q1.
    let (dup, kind) = s.receive(mk("q1", "r-e", None), "alice").await.unwrap();
    assert_eq!(kind, ReceiveKind::Duplicate);
    assert_eq!(
        (dup.id.as_str(), dup.last_receive_id.as_deref()),
        ("e1", Some("q1"))
    );
    s.receive(mk("c7", "r-c", Some("acme")), "alice")
        .await
        .unwrap();

    let id_of = |r: Result<alrust_domain::Alert, StoreError>| r.map(|a| a.id);
    assert_eq!(id_of(s.get_alert("aa11", None).await).unwrap(), "aa11");
    assert_eq!(id_of(s.get_alert("aa119", None).await).unwrap(), "aa1199");
    assert!(
        id_of(s.get_alert("aa1", None).await)
            .unwrap()
            .starts_with("aa11")
    );
    assert_eq!(id_of(s.get_alert("q1", None).await).unwrap(), "q1");

    // lastReceiveId only, full and prefix: a later duplicate of e1.
    s.receive(mk("zz9", "r-e", None), "alice").await.unwrap();
    assert_eq!(id_of(s.get_alert("zz9", None).await).unwrap(), "e1");
    assert_eq!(id_of(s.get_alert("zz", None).await).unwrap(), "e1");

    // Customers restrict the exact path too.
    let acme = ["acme".to_string()];
    let other = ["other".to_string()];
    assert_eq!(id_of(s.get_alert("c7", Some(&acme)).await).unwrap(), "c7");
    assert!(matches!(
        s.get_alert("c7", Some(&other)).await,
        Err(StoreError::NotFound)
    ));
    assert!(matches!(
        s.get_alert("nope", None).await,
        Err(StoreError::NotFound)
    ));

    // The lock path (an action) and delete act on the exact id.
    let acked = s
        .take_action("aa11", None, "ack", "seen", None, "alice")
        .await
        .unwrap();
    assert_eq!((acked.id.as_str(), acked.status.as_str()), ("aa11", "ack"));
    assert_eq!(s.get_alert("aa1199", None).await.unwrap().status, "open");
    s.delete_alert("aa11", None).await.unwrap();
    // With no exact id left, the prefix match finds the longer id.
    assert_eq!(id_of(s.get_alert("aa11", None).await).unwrap(), "aa1199");
    s.delete_alert("aa11", None).await.unwrap();
    assert!(matches!(
        s.get_alert("aa11", None).await,
        Err(StoreError::NotFound)
    ));
}

/// The exact-id statements can use the primary key: with sequential scans
/// off for one transaction, both plans name `alerts_pkey`.
#[tokio::test]
async fn the_exact_id_lookup_can_use_the_primary_key() {
    let Some(db) = test_db().await else { return };
    let s = store(&db.pool);
    s.prepare().await.expect("fresh start");
    let mut tx = db.pool.begin().await.unwrap();
    sqlx::query("SET LOCAL enable_seqscan = off")
        .execute(&mut *tx)
        .await
        .unwrap();
    for sql in [ALERT_BY_EXACT_ID, ALERT_BY_EXACT_ID_FOR_UPDATE] {
        let explain = format!("EXPLAIN {sql}");
        let plan: Vec<String> = sqlx::query_scalar(&explain)
            .bind("x")
            .bind(None::<Vec<String>>)
            .fetch_all(&mut *tx)
            .await
            .unwrap();
        assert!(plan.iter().any(|l| l.contains("alerts_pkey")), "{plan:?}");
    }
    tx.rollback().await.unwrap();
}

fn new_alert(id: &str, timeout: i64) -> NewAlert {
    NewAlert {
        id: id.into(),
        resource: "web01".into(),
        event: "HttpError".into(),
        environment: "Production".into(),
        severity: "major".into(),
        correlate: vec![],
        status: "open".into(),
        service: vec!["Web".into()],
        group: "Web".into(),
        value: Some("500".into()),
        text: String::new(),
        tags: vec![],
        attributes: serde_json::Map::default(),
        origin: "test".into(),
        event_type: "exceptionAlert".into(),
        create_time: Utc.with_ymd_and_hms(2026, 10, 13, 12, 0, 0).unwrap()
            + chrono::Duration::milliseconds(789),
        timeout,
        raw_data: None,
        customer: None,
    }
}

#[tokio::test]
async fn times_round_trip_exactly_whatever_the_session_time_zone() {
    let Some(db) = test_db().await else { return };
    let tz: String = sqlx::query_scalar("SHOW TimeZone")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert_eq!(tz, SESSION_TIME_ZONE, "the test session must not be UTC");
    let s = store(&db.pool);
    s.prepare().await.unwrap();
    let (a, kind) = s.receive(new_alert("t1", 1), "alice").await.unwrap();
    assert_eq!(kind, ReceiveKind::Created);
    let back = s.get_alert("t1", None).await.unwrap();
    assert_eq!(back.create_time, a.create_time);
    assert_eq!(back.receive_time, a.receive_time);
    assert_eq!(back.update_time, a.update_time);
    assert_eq!(back.history[0].update_time, a.create_time);
    let stored: String = sqlx::query_scalar("SELECT create_time::text FROM alerts WHERE id = 't1'")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert_eq!(stored, "2026-10-13 12:00:00.789");
    // Housekeeping compares with a bound UTC time, not the session's now().
    sqlx::query(
        "UPDATE alerts SET last_receive_time = last_receive_time - interval '10 seconds' WHERE id = 't1'",
    )
    .execute(&db.pool)
    .await
    .unwrap();
    assert_eq!(s.housekeeping(0, 0).await.unwrap(), ["t1"]);
}

/// Two rows match the correlate lookup: `y1` has the same event with another
/// severity, `x1` lists the event in its correlate list. The same-event row
/// must win whatever the scan order; rewriting `x1`'s event would violate the
/// unique index. `y1` is moved after `x1` in the heap so that a plain
/// sequential scan meets `x1` first.
#[tokio::test]
async fn correlate_prefers_the_same_event_row() {
    let Some(db) = test_db().await else { return };
    let s = store(&db.pool);
    s.prepare().await.unwrap();
    let mut same_event = new_alert("y1", 60);
    same_event.event = "C".into();
    same_event.severity = "minor".into();
    s.receive(same_event, "alice").await.unwrap();
    let mut listing = new_alert("x1", 60);
    listing.event = "A".into();
    listing.correlate = vec!["A".into(), "C".into()];
    let (_, kind) = s.receive(listing, "alice").await.unwrap();
    assert_eq!(kind, ReceiveKind::Created);
    sqlx::query("UPDATE alerts SET text = text || '' WHERE id = 'y1'")
        .execute(&db.pool)
        .await
        .unwrap();
    let mut incoming = new_alert("c1", 60);
    incoming.event = "C".into();
    incoming.severity = "major".into();
    let (correlated, kind) = s.receive(incoming, "alice").await.unwrap();
    assert_eq!(kind, ReceiveKind::Correlated);
    assert_eq!(correlated.id, "y1");
    let x_event: String = sqlx::query_scalar("SELECT event FROM alerts WHERE id = 'x1'")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert_eq!(x_event, "A");
}

/// `ensure_key` keeps one row per id: a new value under the same id replaces
/// the old one, which then no longer authenticates, and a row already
/// holding the value under another id is adopted under this id.
#[tokio::test]
async fn ensure_key_replaces_the_value_under_its_id() {
    let Some(db) = test_db().await else { return };
    let s = store(&db.pool);
    s.prepare().await.unwrap();
    let key = |id: &str, value: &str| alrust_domain::ApiKey {
        id: id.into(),
        key: value.into(),
        user: "admin".into(),
        scopes: Some(vec!["admin".into()]),
        text: "Bootstrap admin key".into(),
        expire_time: Some(Utc::now() + chrono::Duration::days(1)),
        count: 0,
        last_used_time: None,
        customer: None,
    };
    let rows = |pool: PgPool| async move {
        sqlx::query_as::<_, (String, String)>("SELECT id, key FROM keys ORDER BY id")
            .fetch_all(&pool)
            .await
            .unwrap()
    };
    s.ensure_key(&key("fixed", "value-a-0123456789"))
        .await
        .unwrap();
    s.ensure_key(&key("fixed", "value-a-0123456789"))
        .await
        .unwrap();
    assert_eq!(
        rows(db.pool.clone()).await,
        [("fixed".into(), "value-a-0123456789".into())]
    );
    s.ensure_key(&key("fixed", "value-b-0123456789"))
        .await
        .unwrap();
    assert_eq!(
        rows(db.pool.clone()).await,
        [("fixed".into(), "value-b-0123456789".into())]
    );
    // An older row holding the next value under a random id is adopted.
    s.ensure_key(&key("legacy-random-id", "value-c-0123456789"))
        .await
        .unwrap();
    s.ensure_key(&key("fixed", "value-c-0123456789"))
        .await
        .unwrap();
    assert_eq!(
        rows(db.pool.clone()).await,
        [("fixed".into(), "value-c-0123456789".into())]
    );
}

/// Housekeeping deletes exactly the closed or expired alerts older than the
/// expired threshold and the informational alerts older than the info
/// threshold (spec K1), and nothing else.
#[tokio::test]
async fn housekeeping_deletes_only_old_closed_and_old_informational_alerts() {
    let Some(db) = test_db().await else { return };
    let s = store(&db.pool);
    s.prepare().await.unwrap();
    for (id, severity) in [
        ("hk-old-closed", "major"),
        ("hk-new-closed", "major"),
        ("hk-old-open", "major"),
        ("hk-old-info", "informational"),
        ("hk-new-info", "informational"),
    ] {
        let mut alert = new_alert(id, 86400);
        alert.resource = id.into();
        alert.severity = severity.into();
        s.receive(alert, "alice").await.unwrap();
    }
    for (id, status, age) in [
        ("hk-old-closed", "closed", "3 hours"),
        ("hk-new-closed", "closed", "10 minutes"),
        ("hk-old-open", "open", "3 hours"),
        ("hk-old-info", "open", "13 hours"),
        ("hk-new-info", "open", "1 hour"),
    ] {
        sqlx::query(
            "UPDATE alerts SET status = $2,
               last_receive_time = timezone('UTC', now()) - $3::interval WHERE id = $1",
        )
        .bind(id)
        .bind(status)
        .bind(age)
        .execute(&db.pool)
        .await
        .unwrap();
    }
    let expired = s.housekeeping(2 * 3600, 12 * 3600).await.unwrap();
    assert!(expired.is_empty(), "{expired:?}");
    let left: Vec<String> = sqlx::query_scalar("SELECT id FROM alerts ORDER BY id")
        .fetch_all(&db.pool)
        .await
        .unwrap();
    assert_eq!(left, ["hk-new-closed", "hk-new-info", "hk-old-open"]);
}

#[tokio::test]
async fn duplicate_count_at_the_integer_maximum_rolls_back() {
    let Some(db) = test_db().await else { return };
    let s = store(&db.pool);
    s.prepare().await.unwrap();
    s.receive(new_alert("d1", 60), "alice").await.unwrap();
    sqlx::query("UPDATE alerts SET duplicate_count = 2147483647 WHERE id = 'd1'")
        .execute(&db.pool)
        .await
        .unwrap();
    let before = checksum(&db.pool).await;
    let err = s
        .receive(new_alert("d2", 60), "alice")
        .await
        .expect_err("overflow");
    assert!(
        matches!(err, StoreError::OutOfRange("duplicateCount")),
        "{err}"
    );
    assert_eq!(checksum(&db.pool).await, before);
}

/// Reference rows with empty strings and with NULL metadata read as the
/// reference's model reads them, and the store keeps working on them.
#[tokio::test]
async fn adopted_empty_strings_and_null_metadata_read_like_the_reference() {
    let Some(db) = test_db().await else { return };
    apply_reference_schema(&db.pool).await;
    sqlx::raw_sql(REFERENCE_ROWS)
        .execute(&db.pool)
        .await
        .unwrap();
    let s = store(&db.pool);
    assert_eq!(s.prepare().await.unwrap().outcome, Outcome::Adopted);

    // Python's `or`: empty strings become the model's constants (EMPTY-1).
    let a = s.get_alert("r2", None).await.unwrap();
    assert_eq!(
        (
            a.severity.as_str(),
            a.status.as_str(),
            a.group.as_str(),
            a.event_type.as_str()
        ),
        ("normal", "open", "Misc", "exceptionAlert")
    );
    let acked = s
        .take_action("r2", None, "ack", "seen", None, "alice")
        .await
        .expect("an action on an adopted empty-severity alert");
    assert_eq!(acked.status, "ack");
    let hb = s.get_heartbeat("hb2").await.unwrap();
    assert_eq!(hb.event_type, "Heartbeat");

    // NULL metadata stays NULL, as the reference keeps None (NULL-1).
    let a = s.get_alert("r3", None).await.unwrap();
    assert_eq!(a.duplicate_count, None);
    assert_eq!(a.repeat, None);
    assert_eq!(a.previous_severity, None);
    assert_eq!(a.trend_indication, None);
    assert_eq!(a.last_receive_id, None);
    assert_eq!(a.last_receive_time, None);
    let page = s
        .find_alerts(
            &AlertQuery::default(),
            1,
            100,
            alrust_domain::HistoryOutput::Include,
        )
        .await
        .expect("listing includes NULL-metadata rows");
    assert!(page.alerts.iter().any(|a| a.id == "r3"));
    // A duplicate keeps a NULL count NULL (`duplicate_count + 1`), and sets
    // the rest, as the reference's dedup UPDATE does.
    let mut n = new_alert("r3-dup", 60);
    n.resource = "mq01".into();
    n.event = "QueueFull".into();
    n.service = vec!["Mq".into()];
    n.group = "Mq".into();
    let (a, kind) = s.receive(n, "alice").await.unwrap();
    assert_eq!(kind, ReceiveKind::Duplicate);
    assert_eq!(a.id, "r3");
    let (count, repeat, last_id): (Option<i32>, Option<bool>, Option<String>) = sqlx::query_as(
        "SELECT duplicate_count, repeat, last_receive_id FROM alerts WHERE id = 'r3'",
    )
    .fetch_one(&db.pool)
    .await
    .unwrap();
    assert_eq!(
        (count, repeat, last_id.as_deref()),
        (None, Some(true), Some("r3-dup"))
    );
}

/// The stored row with the given columns removed, as text, to compare
/// everything an operation must not touch (NULL and "" stay distinct).
async fn row_without(pool: &PgPool, id: &str, cols: &[&str]) -> String {
    sqlx::query_scalar("SELECT (to_jsonb(a) - $2::text[])::text FROM alerts a WHERE id = $1")
        .bind(id)
        .bind(cols)
        .fetch_one(pool)
        .await
        .unwrap()
}

/// Each operation writes only the reference's own columns for it, so stored
/// NULLs and empty strings that read hydrated as defaults are never written
/// back by an unrelated edit (ADR5-EDIT-PRESERVE-1).
#[tokio::test]
async fn edits_write_only_their_own_columns() {
    let Some(db) = test_db().await else { return };
    apply_reference_schema(&db.pool).await;
    sqlx::raw_sql(REFERENCE_ROWS)
        .execute(&db.pool)
        .await
        .unwrap();
    let s = store(&db.pool);
    assert_eq!(s.prepare().await.unwrap().outcome, Outcome::Adopted);
    use alrust_store_pg::AlertEdit;
    let cases: Vec<(&str, AlertEdit, &[&str])> = vec![
        ("tag", AlertEdit::Tag(vec!["x".into()]), &["tags"]),
        ("untag", AlertEdit::Untag(vec!["x".into()]), &["tags"]),
        (
            "attributes",
            AlertEdit::Attributes(serde_json::from_str(r#"{"k": "v"}"#).unwrap()),
            &["attributes"],
        ),
    ];
    for (name, edit, cols) in cases {
        let before = row_without(&db.pool, "r2", cols).await;
        s.edit_alert("r2", None, edit).await.expect(name);
        assert_eq!(row_without(&db.pool, "r2", cols).await, before, "{name}");
    }
    let raw: (String, String, String, String) =
        sqlx::query_as("SELECT severity, status, \"group\", type FROM alerts WHERE id = 'r2'")
            .fetch_one(&db.pool)
            .await
            .unwrap();
    assert_eq!(
        raw,
        (String::new(), String::new(), String::new(), String::new()),
        "stored empties kept"
    );

    let status_cols: &[&str] = &["status", "timeout", "update_time", "history"];
    let before = row_without(&db.pool, "r2", status_cols).await;
    s.set_status("r2", None, "assign", "mine", None, "alice")
        .await
        .unwrap();
    assert_eq!(
        row_without(&db.pool, "r2", status_cols).await,
        before,
        "set_status"
    );

    let action_cols: &[&str] = &[
        "severity",
        "status",
        "tags",
        "attributes",
        "timeout",
        "previous_severity",
        "update_time",
        "history",
    ];
    let before = row_without(&db.pool, "r2", action_cols).await;
    s.take_action("r2", None, "ack", "seen", None, "alice")
        .await
        .unwrap();
    assert_eq!(
        row_without(&db.pool, "r2", action_cols).await,
        before,
        "action"
    );

    let dedup_cols: &[&str] = &[
        "status",
        "service",
        "value",
        "text",
        "timeout",
        "raw_data",
        "repeat",
        "last_receive_id",
        "last_receive_time",
        "tags",
        "attributes",
        "duplicate_count",
        "update_time",
        "history",
    ];
    let before = row_without(&db.pool, "r3", dedup_cols).await;
    let mut n = new_alert("r3-dup", 60);
    n.resource = "mq01".into();
    n.event = "QueueFull".into();
    n.service = vec!["Mq".into()];
    n.group = "Mq".into();
    assert_eq!(
        s.receive(n, "alice").await.unwrap().1,
        ReceiveKind::Duplicate
    );
    assert_eq!(
        row_without(&db.pool, "r3", dedup_cols).await,
        before,
        "duplicate"
    );
}

/// An invalid same-definition unique index (a failed `CREATE UNIQUE INDEX
/// CONCURRENTLY`) is refused, and adoption writes nothing (INDEX-1).
#[tokio::test]
async fn adoption_refuses_an_invalid_identity_index() {
    let Some(db) = test_db().await else { return };
    apply_reference_schema(&db.pool).await;
    sqlx::raw_sql(
        "DROP INDEX env_res_evt_cust_key;
         INSERT INTO alerts (id, resource, event, environment, customer) VALUES
             ('x1', 'r', 'e', 'Production', NULL), ('x2', 'r', 'e', 'Production', NULL);",
    )
    .execute(&db.pool)
    .await
    .unwrap();
    let failed = sqlx::raw_sql(
        "CREATE UNIQUE INDEX CONCURRENTLY env_res_evt_cust_key ON alerts USING btree \
         (environment, resource, event, (COALESCE(customer, ''::text)))",
    )
    .execute(&db.pool)
    .await;
    assert!(failed.is_err(), "duplicates must make the build fail");
    let valid: bool = sqlx::query_scalar(
        "SELECT indisvalid FROM pg_index WHERE indexrelid = 'env_res_evt_cust_key'::regclass",
    )
    .fetch_one(&db.pool)
    .await
    .unwrap();
    assert!(!valid, "the failed build leaves an invalid index behind");
    let err = store(&db.pool).prepare().await.expect_err("invalid index");
    assert!(
        matches!(&err, StartupError::AdoptRefused(d) if d.iter().any(|l| l.contains("env_res_evt_cust_key"))),
        "{err}"
    );
    let has_history: bool =
        sqlx::query_scalar("SELECT to_regclass('public._sqlx_migrations') IS NOT NULL")
            .fetch_one(&db.pool)
            .await
            .unwrap();
    assert!(!has_history);
}

/// DDL that commits while adoption waits for its locks is seen by the
/// comparison, so adoption refuses and writes nothing (ADOPT-1).
#[tokio::test]
async fn adoption_waits_for_concurrent_ddl_and_then_refuses() {
    let Some(db) = test_db().await else { return };
    apply_reference_schema(&db.pool).await;
    let mut other = db.pool.begin().await.unwrap();
    sqlx::query("ALTER TABLE alerts ADD COLUMN racing text")
        .execute(&mut *other)
        .await
        .unwrap();
    let s = store(&db.pool);
    let adopting = tokio::spawn(async move { s.prepare().await });
    // Wait until adoption is blocked on a lock held by the open DDL.
    let mut waiting = false;
    for _ in 0..200 {
        waiting = sqlx::query_scalar(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted AND relation = 'public.alerts'::regclass)",
        )
        .fetch_one(&db.pool)
        .await
        .unwrap();
        if waiting {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert!(waiting, "adoption should wait for the DDL's lock");
    other.commit().await.unwrap();
    let err = adopting.await.unwrap().expect_err("refused after the DDL");
    assert!(
        matches!(&err, StartupError::AdoptRefused(d) if d.iter().any(|l| l.contains("racing"))),
        "{err}"
    );
    let has_history: bool =
        sqlx::query_scalar("SELECT to_regclass('public._sqlx_migrations') IS NOT NULL")
            .fetch_one(&db.pool)
            .await
            .unwrap();
    assert!(!has_history);
}

/// Positive proof of "no DDL": an event trigger rejects every DDL command
/// while adoption runs, twice. Needs superuser (event triggers); `NOT_RUN`
/// otherwise, run under CI.
#[tokio::test]
async fn adoption_runs_no_ddl() {
    let Some(db) = test_db().await else { return };
    apply_reference_schema(&db.pool).await;
    let setup = sqlx::raw_sql(
        r"CREATE FUNCTION deny_ddl() RETURNS event_trigger LANGUAGE plpgsql AS $$
           BEGIN RAISE EXCEPTION 'adoption ran DDL: %', TG_TAG; END $$;
           CREATE EVENT TRIGGER deny_ddl ON ddl_command_start EXECUTE FUNCTION deny_ddl();",
    )
    .execute(&db.pool)
    .await;
    if let Err(e) = setup {
        if e.as_database_error()
            .and_then(sqlx::error::DatabaseError::code)
            .as_deref()
            == Some("42501")
        {
            eprintln!("NOT_RUN (needs superuser): event-trigger DDL denial");
            return;
        }
        panic!("{e}");
    }
    let s = store(&db.pool);
    assert_eq!(s.prepare().await.expect("no DDL").outcome, Outcome::Adopted);
    assert_eq!(s.prepare().await.expect("no DDL").outcome, Outcome::Adopted);
    let n: i64 =
        sqlx::query_scalar("SELECT count(*) FROM pg_event_trigger WHERE evtname = 'deny_ddl'")
            .fetch_one(&db.pool)
            .await
            .unwrap();
    assert_eq!(n, 1, "the denial was in force throughout");
    sqlx::raw_sql("DROP EVENT TRIGGER deny_ddl")
        .execute(&db.pool)
        .await
        .unwrap();
}

/// The server's own session options: search path pinned, `TimeZone` UTC, and
/// start-up accepts them.
#[tokio::test]
async fn session_options_pin_the_search_path_and_utc() {
    let Some(db) = test_db().await else { return };
    let pool = db.pool_with(session_options(db.base.clone())).await;
    let (tz, path, order): (String, String, Vec<String>) = sqlx::query_as(
        "SELECT current_setting('TimeZone'), current_setting('search_path'), current_schemas(true)::text[]",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(tz, "UTC");
    assert_eq!(path, alrust_store_pg::SEARCH_PATH);
    assert_eq!(order, ["pg_catalog", "public"]);
    assert_eq!(
        store(&pool).prepare().await.unwrap().outcome,
        Outcome::Fresh
    );
    pool.close().await;
}

/// Sessions are pinned to `public`: a role schema with shadow tables is
/// never used, and an unpinned pool is refused at start-up (PATH-1).
#[tokio::test]
async fn sessions_are_pinned_to_public_and_shadow_keys_never_authenticate() {
    let Some(db) = test_db().await else { return };
    assert_eq!(
        store(&db.pool).prepare().await.unwrap().outcome,
        Outcome::Fresh
    );
    sqlx::raw_sql(
        r#"DO $$
           BEGIN
               EXECUTE format('CREATE SCHEMA %I', current_user);
               EXECUTE format('CREATE TABLE %I.keys (LIKE public.keys INCLUDING ALL)', current_user);
               EXECUTE format($q$INSERT INTO %I.keys (id, key, "user", scopes, text, expire_time, count)
                                VALUES ('shadow', 'shadow-secret', 'mallory', '{admin}', '',
                                        '2099-01-01', 0)$q$, current_user);
               EXECUTE format('ALTER DATABASE %I SET search_path = "$user", public', current_database());
           END $$;"#,
    )
    .execute(&db.pool)
    .await
    .unwrap();

    // A pool that does not pin the search path would resolve `keys` to the
    // shadow table; start-up refuses it.
    let unpinned = db.pool_with(db.base.clone()).await;
    let shadow_seen: i64 = sqlx::query_scalar("SELECT count(*) FROM keys WHERE id = 'shadow'")
        .fetch_one(&unpinned)
        .await
        .unwrap();
    assert_eq!(shadow_seen, 1, "the fixture must really shadow public.keys");
    let err = store(&unpinned).prepare().await.expect_err("unpinned pool");
    assert!(matches!(err, StartupError::SearchPath { .. }), "{err}");

    // The pinned pool never sees the shadow table.
    let pinned = db.odd_zone_pool_with(test_session(&db.base)).await;
    let s = store(&pinned);
    assert_eq!(s.prepare().await.unwrap().outcome, Outcome::Migrated);
    assert!(s.use_key("shadow-secret").await.unwrap().is_none());
    let tz: String = sqlx::query_scalar("SHOW TimeZone")
        .fetch_one(&pinned)
        .await
        .unwrap();
    assert_eq!(tz, SESSION_TIME_ZONE);
    let shadow_count: Option<i64> = sqlx::query_scalar(
        "SELECT count FROM keys WHERE id = 'shadow'", // resolves to public.keys: no row
    )
    .fetch_optional(&pinned)
    .await
    .unwrap();
    assert_eq!(shadow_count, None);
    let untouched: (i64, Option<chrono::NaiveDateTime>) = sqlx::query_as(&format!(
        "SELECT count, last_used_time FROM {}.keys WHERE id = 'shadow'",
        sqlx::query_scalar::<_, String>("SELECT quote_ident(current_user)")
            .fetch_one(&pinned)
            .await
            .unwrap()
    ))
    .fetch_one(&pinned)
    .await
    .unwrap();
    assert_eq!(untouched, (0, None), "no shadow write");
}

/// Injects a copy mismatch into the real migration: 0002 with one
/// conversion altered runs through `SQLx`, raises, and the whole transaction
/// rolls back, leaving the alpha catalog, rows and history as they were
/// (ROLLBACK-1).
#[tokio::test]
async fn an_injected_copy_mismatch_rolls_the_real_migration_back() {
    let cases: [(&str, &str, &str); 4] = [
        (
            "(a.value)::text END, a.text, a.tags,",
            "(a.value)::text END, a.text || 'x', a.tags,",
            "alert rows differ after the copy",
        ),
        (
            "WITH ORDINALITY AS h(e, n) ORDER BY h.n),",
            "WITH ORDINALITY AS h(e, n) ORDER BY h.n DESC),",
            "history entries differ after the copy",
        ),
        (
            "h.receive_time AT TIME ZONE 'UTC', h.customer, h.attributes",
            "h.receive_time AT TIME ZONE 'UTC', upper(h.customer), h.attributes",
            "heartbeat rows differ after the copy",
        ),
        (
            "USING expire_time AT TIME ZONE 'UTC'",
            "USING (expire_time + interval '1 second') AT TIME ZONE 'UTC'",
            "key rows differ after the conversion",
        ),
    ];
    for (from, to, expected) in cases {
        assert_eq!(MIGRATION_0002.matches(from).count(), 1, "{from}");
        let Some(db) = alpha_db_with_rows().await else {
            return;
        };
        let catalog_before = catalog_of(&db.pool).await;
        let rows_before = checksum(&db.pool).await;
        let all = sqlx::migrate!("./migrations");
        let real = &all.migrations[1];
        let broken = sqlx::migrate::Migration::new(
            real.version,
            real.description.clone(),
            real.migration_type,
            std::borrow::Cow::Owned(MIGRATION_0002.replace(from, to)),
            false,
        );
        let migrator = sqlx::migrate::Migrator {
            migrations: std::borrow::Cow::Owned(vec![all.migrations[0].clone(), broken]),
            ..sqlx::migrate::Migrator::DEFAULT
        };
        let err = migrator.run(&db.pool).await.expect_err(to);
        assert!(err.to_string().contains(expected), "{to}: {err}");
        assert_eq!(catalog_of(&db.pool).await, catalog_before, "{to}");
        assert_eq!(checksum(&db.pool).await, rows_before, "{to}");
        let versions: Vec<i64> = applied_versions(&db.pool)
            .await
            .into_iter()
            .map(|v| v.0)
            .collect();
        assert_eq!(versions, [1], "{to}");
    }
}

// ---- generation (scripts/store-db-run.sh generate); ignored otherwise ----

/// Writes `reference-catalog.json` from the pinned reference schema.
#[tokio::test]
#[ignore = "generation step; run through scripts/store-db-run.sh generate"]
async fn generate_reference_catalog() {
    let db = test_db()
        .await
        .expect("DATABASE_URL is required to generate");
    apply_reference_schema(&db.pool).await;
    apply_reference_schema(&db.pool).await;
    let c = catalog_of(&db.pool).await;
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/reference-catalog.json");
    std::fs::write(
        path,
        format!("{}\n", serde_json::to_string_pretty(&c).unwrap()),
    )
    .unwrap();
    eprintln!("wrote {path}");
}

/// Writes `src/verify/alpha_shape.expected` from a database holding only 0001.
#[tokio::test]
#[ignore = "generation step; run through scripts/store-db-run.sh generate"]
async fn generate_alpha_shape() {
    let db = test_db()
        .await
        .expect("DATABASE_URL is required to generate");
    apply_alpha(&db.pool).await;
    let shape: String = sqlx::query_scalar(ALPHA_SHAPE)
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert!(!shape.contains("$shape$"));
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/src/verify/alpha_shape.expected"
    );
    std::fs::write(path, format!("{shape}\n")).unwrap();
    eprintln!("wrote {path}");
}

/// Measured on PostgreSQL 18.6 (CI run 38458334716): a standalone `ALTER
/// TYPE history ADD ATTRIBUTE` does NOT wait for a lock on `alerts`. This
/// is why adoption does not rely on locks for safety: it writes nothing at
/// all (ADR-0005 decision 4). The test pins the measured fact; it covers
/// ADD ATTRIBUTE only and is a characterisation, not a safety test.
#[tokio::test]
async fn alter_type_history_does_not_wait_for_the_alerts_lock() {
    let Some(db) = test_db().await else { return };
    apply_reference_schema(&db.pool).await;
    let mut holder = db.pool.begin().await.unwrap();
    sqlx::query("LOCK TABLE public.alerts IN ACCESS EXCLUSIVE MODE")
        .execute(&mut *holder)
        .await
        .unwrap();
    let mut other = db.pool.begin().await.unwrap();
    sqlx::query("SET LOCAL lock_timeout = '2s'")
        .execute(&mut *other)
        .await
        .unwrap();
    let altered = sqlx::query("ALTER TYPE public.history ADD ATTRIBUTE probe text")
        .execute(&mut *other)
        .await;
    other.rollback().await.ok();
    holder.rollback().await.unwrap();
    assert!(
        altered.is_ok(),
        "ALTER TYPE history now waits for (or fails on) the alerts lock; revisit ADR-0005 adoption P2: {altered:?}"
    );
}

/// DECISIONS Y68: a key is verified with a read on every request, and its use
/// is written by the flush, or at once with `key_usage_immediate`.
#[tokio::test]
async fn key_use_is_tallied_and_flushed() {
    let Some(db) = test_db().await else { return };
    let s = store(&db.pool);
    s.prepare().await.expect("fresh start");
    db.pool
        .execute(
            "INSERT INTO keys (id, key, \"user\", scopes, text, expire_time, count, last_used_time, customer)
             VALUES ('ok', 'secret-ok', 'alice', '{read}', 'k', '2099-01-01 00:00:00', 1, NULL, NULL),
                    ('nullcount', 'secret-nc', 'alice', '{read}', NULL, '2099-01-01 00:00:00', NULL, NULL, NULL),
                    ('old', 'secret-old', 'alice', '{read}', NULL, '2000-01-01 00:00:00', 0, NULL, NULL),
                    ('noexp', 'secret-noexp', 'alice', '{admin}', NULL, NULL, 2, NULL, NULL),
                    ('noscope', 'secret-noscope', 'alice', NULL, NULL, '2099-01-01 00:00:00', NULL, NULL, NULL)",
        )
        .await
        .unwrap();
    let row = |id: &'static str| {
        let pool = db.pool.clone();
        async move {
            sqlx::query_as::<_, (Option<i64>, Option<chrono::NaiveDateTime>)>(
                "SELECT count, last_used_time FROM keys WHERE id = $1",
            )
            .bind(id)
            .fetch_one(&pool)
            .await
            .unwrap()
        }
    };

    // The read matches exactly the keys use_key matches, and writes nothing.
    for refused in ["secret-old", "secret-noexp", "secret-noscope", "unknown"] {
        assert!(s.verify_key(refused).await.unwrap().is_none(), "{refused}");
        assert!(
            s.authenticate_key(refused).await.unwrap().is_none(),
            "{refused}"
        );
    }
    assert_eq!(s.verify_key("secret-ok").await.unwrap().unwrap().count, 1);
    assert_eq!(s.flush_key_usage().await.unwrap(), 0, "nothing tallied yet");

    // Two uses and one on a NULL count: nothing written until the flush.
    let before = Utc::now().naive_utc();
    assert_eq!(
        s.authenticate_key("secret-ok").await.unwrap().unwrap().id,
        "ok"
    );
    assert!(s.authenticate_key("secret-ok").await.unwrap().is_some());
    assert!(s.authenticate_key("secret-nc").await.unwrap().is_some());
    assert_eq!(row("ok").await, (Some(1), None));
    // A clone shares the tally, as the API's state and the flush loop do.
    assert_eq!(s.clone().flush_key_usage().await.unwrap(), 2);
    let (count, used) = row("ok").await;
    assert_eq!(count, Some(3));
    assert!(used.unwrap() >= before);
    // A NULL count stays NULL, as the reference's count + 1 leaves it.
    let (count, used) = row("nullcount").await;
    assert_eq!(count, None);
    assert!(used.is_some());
    assert_eq!(
        s.flush_key_usage().await.unwrap(),
        0,
        "the flush emptied the tally"
    );
    assert_eq!(row("ok").await.0, Some(3));

    // A key deleted between its use and the flush is harmless.
    assert!(s.authenticate_key("secret-ok").await.unwrap().is_some());
    db.pool
        .execute("DELETE FROM keys WHERE id = 'ok'")
        .await
        .unwrap();
    assert_eq!(s.flush_key_usage().await.unwrap(), 0);
    assert!(
        s.authenticate_key("secret-ok").await.unwrap().is_none(),
        "never cached"
    );

    // The parity switch: the reference's write on every request.
    db.pool
        .execute(
            "INSERT INTO keys (id, key, \"user\", scopes, text, expire_time, count, last_used_time, customer)
             VALUES ('imm', 'secret-imm', 'alice', '{read}', NULL, '2099-01-01 00:00:00', 5, NULL, NULL)",
        )
        .await
        .unwrap();
    let immediate = Store::new(
        db.pool.clone(),
        StoreConfig {
            key_usage_immediate: true,
            ..StoreConfig::default()
        },
    );
    assert_eq!(
        immediate
            .authenticate_key("secret-imm")
            .await
            .unwrap()
            .unwrap()
            .count,
        6
    );
    let (count, used) = row("imm").await;
    assert_eq!(count, Some(6), "written before the flush");
    assert!(used.is_some());
    assert!(
        immediate
            .authenticate_key("secret-noexp")
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(
        immediate.flush_key_usage().await.unwrap(),
        0,
        "nothing tallied"
    );
    assert_eq!(row("imm").await.0, Some(6));
}

/// cdx 56ffa31d R1 to R3 (DECISIONS Y70): a write blocked by a row lock is
/// refused within the attempt timeout and its uses stay pending while
/// authentication keeps working; a cancelled write leaves them pending too;
/// a key recreated under the same id with a new secret gets none of the old
/// uses; uses not written within the bound are dropped and reported.
#[tokio::test]
async fn key_use_survives_blocked_writes_and_is_bounded() {
    use alrust_store_pg::key_usage::KeyUsageLosses;
    use std::time::Duration;

    let Some(db) = test_db().await else { return };
    let cfg = StoreConfig {
        key_usage_attempt_timeout: Duration::from_millis(300),
        ..StoreConfig::default()
    };
    let s = Store::new(db.pool.clone(), cfg.clone());
    s.prepare().await.expect("fresh start");
    db.pool
        .execute(
            "INSERT INTO keys (id, key, \"user\", scopes, text, expire_time, count, last_used_time, customer)
             VALUES ('a', 'secret-a', 'alice', '{read}', NULL, '2099-01-01 00:00:00', 0, NULL, NULL),
                    ('b', 'secret-b', 'alice', '{read}', NULL, '2099-01-01 00:00:00', 0, NULL, NULL)",
        )
        .await
        .unwrap();
    let count = |id: &'static str| {
        let pool = db.pool.clone();
        async move {
            sqlx::query_scalar::<_, Option<i64>>("SELECT count FROM keys WHERE id = $1")
                .bind(id)
                .fetch_one(&pool)
                .await
                .unwrap()
        }
    };
    // Another session holds the row lock, as a slow writer would.
    let locker = db.pool_with(test_session(&db.base)).await;

    // Blocked: each write is refused within the attempt timeout, nothing is
    // written, the uses stay pending, and authentication still succeeds.
    let mut lock = locker.begin().await.unwrap();
    sqlx::query("SELECT 1 FROM keys WHERE id = 'a' FOR UPDATE")
        .execute(&mut *lock)
        .await
        .unwrap();
    for _ in 0..3 {
        assert!(s.authenticate_key("secret-a").await.unwrap().is_some());
    }
    for _ in 0..2 {
        let err = s.flush_key_usage().await.expect_err("the row is locked");
        assert!(
            matches!(&err, StoreError::Db(sqlx::Error::Database(_))),
            "{err}"
        );
        assert!(s.authenticate_key("secret-a").await.unwrap().is_some());
    }
    assert_eq!(s.key_usage_pending(), (1, 5));
    // A write cancelled before COMMIT (its future dropped) changes nothing,
    // and its uses stay pending. It has its own pool: the cancelled UPDATE
    // still waits on the server, so that backend is ended before the lock is
    // released, which rolls its transaction back.
    let slow = Store::new(
        db.pool_with(test_session(&db.base)).await,
        StoreConfig {
            key_usage_attempt_timeout: Duration::from_secs(20),
            ..cfg.clone()
        },
    );
    assert!(slow.authenticate_key("secret-a").await.unwrap().is_some());
    assert!(
        tokio::time::timeout(Duration::from_millis(500), slow.flush_key_usage())
            .await
            .is_err(),
        "the write waits for the lock"
    );
    assert_eq!(slow.key_usage_pending(), (1, 1));
    assert_eq!(s.take_key_usage_losses(), KeyUsageLosses::default());
    assert_eq!(slow.take_key_usage_losses(), KeyUsageLosses::default());
    let ended: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM (SELECT pg_terminate_backend(pid) FROM pg_stat_activity
          WHERE datname = current_database() AND pid <> pg_backend_pid()
            AND state = 'active' AND query LIKE 'UPDATE keys AS k%') t",
    )
    .fetch_one(&mut *lock)
    .await
    .unwrap();
    assert_eq!(ended, 1, "the cancelled write was still waiting");
    lock.rollback().await.unwrap();
    assert_eq!(count("a").await, Some(0));
    assert_eq!(s.flush_key_usage().await.unwrap(), 1);
    assert_eq!(count("a").await, Some(5), "every use once, none twice");
    assert_eq!(s.key_usage_pending(), (0, 0));
    assert_eq!(
        slow.key_usage_pending(),
        (1, 1),
        "never written, never lost silently"
    );

    // The same id with a new secret is a new credential.
    assert!(s.authenticate_key("secret-b").await.unwrap().is_some());
    db.pool
        .execute("DELETE FROM keys WHERE id = 'b'")
        .await
        .unwrap();
    db.pool
        .execute(
            "INSERT INTO keys (id, key, \"user\", scopes, text, expire_time, count, last_used_time, customer)
             VALUES ('b', 'secret-b2', 'alice', '{read}', NULL, '2099-01-01 00:00:00', 0, NULL, NULL)",
        )
        .await
        .unwrap();
    assert_eq!(s.flush_key_usage().await.unwrap(), 0);
    assert_eq!(count("b").await, Some(0));
    assert_eq!(s.key_usage_pending(), (0, 0));
    assert_eq!(
        s.take_key_usage_losses(),
        KeyUsageLosses {
            unmatched_keys: 1,
            unmatched_uses: 1,
            ..KeyUsageLosses::default()
        },
        "a committed write that matched no row is a definite, separate loss"
    );

    // Uses recorded while a write is in progress stay for the next write;
    // a second write does not start meanwhile.
    let tallying = Store::new(
        db.pool.clone(),
        StoreConfig {
            key_usage_attempt_timeout: Duration::from_secs(10),
            ..cfg.clone()
        },
    );
    assert!(
        tallying
            .authenticate_key("secret-a")
            .await
            .unwrap()
            .is_some()
    );
    let mut lock = locker.begin().await.unwrap();
    sqlx::query("SELECT 1 FROM keys WHERE id = 'a' FOR UPDATE")
        .execute(&mut *lock)
        .await
        .unwrap();
    let in_progress = {
        let c = tallying.clone();
        tokio::spawn(async move { c.flush_key_usage().await })
    };
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert!(
        tallying
            .authenticate_key("secret-a")
            .await
            .unwrap()
            .is_some()
    );
    assert!(
        tallying
            .authenticate_key("secret-a")
            .await
            .unwrap()
            .is_some()
    );
    assert_eq!(
        tallying.flush_key_usage().await.unwrap(),
        0,
        "no second write while one is in progress"
    );
    lock.rollback().await.unwrap();
    assert_eq!(in_progress.await.unwrap().unwrap(), 1);
    assert_eq!(count("a").await, Some(6), "the snapshot's one use");
    assert_eq!(tallying.key_usage_pending(), (1, 2));
    assert_eq!(tallying.flush_key_usage().await.unwrap(), 1);
    assert_eq!(count("a").await, Some(8));
    assert_eq!(tallying.take_key_usage_losses(), KeyUsageLosses::default());

    // The bound: uses that wait longer are dropped at the next write, and
    // reported once.
    let short = Store::new(
        db.pool.clone(),
        StoreConfig {
            key_usage_max_pending: Duration::from_millis(400),
            ..cfg
        },
    );
    let mut lock = locker.begin().await.unwrap();
    sqlx::query("SELECT 1 FROM keys WHERE id = 'a' FOR UPDATE")
        .execute(&mut *lock)
        .await
        .unwrap();
    assert!(short.authenticate_key("secret-a").await.unwrap().is_some());
    assert!(short.authenticate_key("secret-a").await.unwrap().is_some());
    assert!(short.flush_key_usage().await.is_err());
    tokio::time::sleep(Duration::from_millis(500)).await;
    assert_eq!(short.flush_key_usage().await.unwrap(), 0);
    assert_eq!(
        short.take_key_usage_losses(),
        KeyUsageLosses {
            expired_keys: 1,
            expired_uses: 2,
            ..KeyUsageLosses::default()
        }
    );
    assert_eq!(short.take_key_usage_losses(), KeyUsageLosses::default());
    lock.rollback().await.unwrap();
    assert_eq!(count("a").await, Some(8));
}

/// cdx 0e4cb36f R3a: a write that starts just before the bound gets only the
/// time left (PostgreSQL ends it then, not after a full attempt), and its
/// uses expire at the next write. Nothing is written after the deadline.
#[tokio::test]
async fn key_use_writes_stop_at_the_deadline() {
    use alrust_store_pg::key_usage::KeyUsageLosses;
    use std::time::{Duration, Instant};

    let Some(db) = test_db().await else { return };
    let s = Store::new(
        db.pool.clone(),
        StoreConfig {
            key_usage_max_pending: Duration::from_millis(1500),
            key_usage_attempt_timeout: Duration::from_secs(10),
            ..StoreConfig::default()
        },
    );
    s.prepare().await.expect("fresh start");
    db.pool
        .execute(
            "INSERT INTO keys (id, key, \"user\", scopes, text, expire_time, count, last_used_time, customer)
             VALUES ('a', 'secret-a', 'alice', '{read}', NULL, '2099-01-01 00:00:00', 0, NULL, NULL)",
        )
        .await
        .unwrap();
    let locker = db.pool_with(test_session(&db.base)).await;
    let mut lock = locker.begin().await.unwrap();
    sqlx::query("SELECT 1 FROM keys WHERE id = 'a' FOR UPDATE")
        .execute(&mut *lock)
        .await
        .unwrap();
    assert!(s.authenticate_key("secret-a").await.unwrap().is_some());
    tokio::time::sleep(Duration::from_millis(1000)).await;

    let started = Instant::now();
    let err = s.flush_key_usage().await.expect_err("the row is locked");
    let took = started.elapsed();
    assert!(
        matches!(&err, StoreError::Db(sqlx::Error::Database(_))),
        "{err}"
    );
    assert!(
        took < Duration::from_millis(1000),
        "the write had only the time left, not the 10 s attempt: {took:?}"
    );

    tokio::time::sleep(Duration::from_millis(600)).await;
    s.expire_key_usage();
    assert_eq!(s.key_usage_pending(), (0, 0));
    assert_eq!(
        s.take_key_usage_losses(),
        KeyUsageLosses {
            expired_keys: 1,
            expired_uses: 1,
            ..KeyUsageLosses::default()
        }
    );
    lock.rollback().await.unwrap();
    let count: Option<i64> = sqlx::query_scalar("SELECT count FROM keys WHERE id = 'a'")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert_eq!(count, Some(0), "nothing was written after the deadline");
}

/// cdx 766fb5e4: a write that waited for a connection past the deadline
/// sends nothing (the uses stay pending, then expire), and a backlog of
/// several batches drains in one write.
#[tokio::test]
async fn key_use_writes_check_the_deadline_after_waiting_and_drain_in_batches() {
    use alrust_store_pg::key_usage::KeyUsageLosses;
    use std::time::Duration;

    let Some(db) = test_db().await else { return };
    store(&db.pool).prepare().await.expect("fresh start");
    db.pool
        .execute(
            "INSERT INTO keys (id, key, \"user\", scopes, text, expire_time, count, last_used_time, customer)
             SELECT 'k' || i, 'secret-' || i, 'alice', '{read}', NULL, '2099-01-01 00:00:00', 0, NULL, NULL
               FROM generate_series(1, 5) AS i",
        )
        .await
        .unwrap();
    let total = || {
        let pool = db.pool.clone();
        async move {
            sqlx::query_scalar::<_, Option<i64>>("SELECT sum(count)::bigint FROM keys")
                .fetch_one(&pool)
                .await
                .unwrap()
        }
    };

    // Every connection of a small pool is held while a write waits for one,
    // until after the deadline.
    let small = db.pool_with(test_session(&db.base)).await; // 2 connections
    let s = Store::new(
        small.clone(),
        StoreConfig {
            key_usage_max_pending: Duration::from_millis(1500),
            key_usage_attempt_timeout: Duration::from_secs(10),
            ..StoreConfig::default()
        },
    );
    assert!(s.authenticate_key("secret-1").await.unwrap().is_some());
    let held = (
        small.acquire().await.unwrap(),
        small.acquire().await.unwrap(),
    );
    let waiting = {
        let w = s.clone();
        tokio::spawn(async move { w.flush_key_usage().await })
    };
    tokio::time::sleep(Duration::from_millis(1700)).await;
    drop(held);
    assert_eq!(
        waiting.await.unwrap().unwrap(),
        0,
        "nothing is sent once the deadline has passed"
    );
    assert_eq!(total().await, Some(0));
    assert_eq!(s.key_usage_pending(), (1, 1));
    s.expire_key_usage();
    assert_eq!(
        s.take_key_usage_losses(),
        KeyUsageLosses {
            expired_keys: 1,
            expired_uses: 1,
            ..KeyUsageLosses::default()
        }
    );

    // A backlog of three batches of two drains in one write.
    let batched = Store::new(
        db.pool.clone(),
        StoreConfig {
            key_usage_batch: 2,
            ..StoreConfig::default()
        },
    );
    for i in 1..=5 {
        let key = format!("secret-{i}");
        assert!(batched.authenticate_key(&key).await.unwrap().is_some());
    }
    assert_eq!(batched.key_usage_pending(), (5, 5));
    assert_eq!(batched.flush_key_usage().await.unwrap(), 5);
    assert_eq!(batched.key_usage_pending(), (0, 0));
    assert_eq!(total().await, Some(5));
    assert_eq!(batched.take_key_usage_losses(), KeyUsageLosses::default());
}
