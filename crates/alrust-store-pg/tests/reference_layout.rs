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
//! where a missing database is a failure. Test sessions are pinned with
//! `session_options` but use a far-from-UTC time zone on purpose: nothing
//! may depend on it.

use alrust_store_pg::catalog;
use alrust_store_pg::{
    NewAlert, Outcome, ReceiveKind, StartupError, Store, StoreConfig, StoreError, session_options,
};
use chrono::{TimeZone, Utc};
use sqlx::postgres::{PgConnectOptions, PgPoolOptions};
use sqlx::{Executor, PgPool, Row};
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

/// The pinned test session: `session_options`, then the odd time zone.
fn test_session(base: &PgConnectOptions) -> PgConnectOptions {
    session_options(base.clone()).options([("TimeZone", SESSION_TIME_ZONE)])
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
    let pool = PgPoolOptions::new()
        .max_connections(4)
        .connect_lazy_with(test_session(&base).database(&name));
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
    assert!(r.triggers.is_empty());
    assert!(catalog::diff(&catalog::expected(), &catalog_of(&db.pool).await).is_empty());
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
    assert!(catalog::diff(&catalog::expected(), &catalog_of(&db.pool).await).is_empty());

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
    assert!(r.get::<Vec<String>, _>("tags").is_empty());
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
            "UPDATE alerts SET value = '1.5' WHERE id = 'a1'",
            "decision 8 pending",
        ),
        (
            "UPDATE alerts SET history = jsonb_set(history, '{1,value}', '[1, 2]') WHERE id = 'a1'",
            "decision 8 pending",
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
                && e.as_database_error().and_then(|d| d.code()).as_deref() == Some("42501")
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
    sqlx::raw_sql(&section("-- 3.4:", "DO $alrust$"))
        .execute(&mut *tx)
        .await
        .unwrap();
    sqlx::raw_sql(&section("-- 3.5:", "DO $alrust$"))
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
async fn adoption_records_a_baseline_and_changes_no_row() {
    let Some(db) = test_db().await else { return };
    apply_reference_schema(&db.pool).await;
    sqlx::raw_sql(REFERENCE_ROWS)
        .execute(&db.pool)
        .await
        .unwrap();
    let rows_before = checksum(&db.pool).await;
    assert_eq!(rows_before.len(), 10);

    let s = store(&db.pool);
    let r = s.prepare().await.expect("adopt");
    assert_eq!(r.outcome, Outcome::Adopted);
    assert_eq!(r.inactive_keys, 2);
    assert_eq!(checksum(&db.pool).await, rows_before);
    let v = applied_versions(&db.pool).await;
    assert_eq!(v.iter().map(|v| v.0).collect::<Vec<_>>(), [1, 2]);
    assert!(
        v.iter()
            .all(|v| v.1.starts_with("baseline: adopted reference schema"))
    );
    assert_eq!(s.prepare().await.unwrap().outcome, Outcome::Migrated);
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
        attributes: Default::default(),
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
        .find_alerts(&Default::default(), 1, 100)
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

/// A history-type change committed during adoption is caught by the second
/// in-transaction comparison: an event trigger alters the type when
/// adoption creates `_sqlx_migrations`, inside adoption's own transaction.
/// Needs superuser (event triggers); NOT_RUN otherwise, run under CI.
#[tokio::test]
async fn adoption_rechecks_the_catalog_before_committing_the_baseline() {
    let Some(db) = test_db().await else { return };
    apply_reference_schema(&db.pool).await;
    let setup = sqlx::raw_sql(
        r#"CREATE FUNCTION racing_type_change() RETURNS event_trigger LANGUAGE plpgsql AS $$
           BEGIN ALTER TYPE public.history ADD ATTRIBUTE racing text; END $$;
           CREATE EVENT TRIGGER racing_type_change ON ddl_command_end WHEN TAG IN ('CREATE TABLE')
               EXECUTE FUNCTION racing_type_change();"#,
    )
    .execute(&db.pool)
    .await;
    if let Err(e) = setup {
        if e.as_database_error().and_then(|d| d.code()).as_deref() == Some("42501") {
            eprintln!("NOT_RUN (needs superuser): event-trigger type race");
            return;
        }
        panic!("{e}");
    }
    let err = store(&db.pool)
        .prepare()
        .await
        .expect_err("type changed mid-adoption");
    assert!(
        matches!(&err, StartupError::AdoptRefused(d) if d.iter().any(|l| l.starts_with("type history"))),
        "{err}"
    );
    let has_history: bool =
        sqlx::query_scalar("SELECT to_regclass('public._sqlx_migrations') IS NOT NULL")
            .fetch_one(&db.pool)
            .await
            .unwrap();
    assert!(!has_history, "the rollback must remove the baseline");
    sqlx::raw_sql("DROP EVENT TRIGGER racing_type_change")
        .execute(&db.pool)
        .await
        .unwrap();
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
    let pinned = db.pool_with(test_session(&db.base)).await;
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
/// conversion altered runs through SQLx, raises, and the whole transaction
/// rolls back, leaving the alpha catalog, rows and history as they were
/// (ROLLBACK-1).
#[tokio::test]
async fn an_injected_copy_mismatch_rolls_the_real_migration_back() {
    let cases: [(&str, &str, &str); 4] = [
        (
            "a.value #>> '{}', a.text, a.tags,",
            "a.value #>> '{}', a.text || 'x', a.tags,",
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
    std::fs::write(path, serde_json::to_string_pretty(&c).unwrap() + "\n").unwrap();
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
    std::fs::write(path, shape + "\n").unwrap();
    eprintln!("wrote {path}");
}
