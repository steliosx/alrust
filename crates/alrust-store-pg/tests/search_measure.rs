//! Search and alert-list measurement on PostgreSQL 18.6
//! (the search v2 design, `docs/performance/search-v2.md`, which this
//! measurement's first results come with). Not a test of behaviour: it is
//! ignored by `cargo test` and run only by
//! `.github/workflows/search-measure.yml`.
//!
//! For each size in `ALRUST_MEASURE_ROWS` (default `10000,100000`), it seeds
//! synthetic alerts with one `generate_series` statement, then, for each
//! candidate index (none first), measures:
//! - the dashboard's hot reads (`find_alerts` page 1 and its counts) and
//!   every `q=` form of the corpus, through the store's own statements;
//! - the plans (`EXPLAIN (ANALYZE, BUFFERS)`), and whether an index can
//!   serve the statement at all (`enable_seqscan` off);
//! - after every read, the receive path's duplicate update, which rewrites
//!   `text` and `last_receive_time`: time and WAL bytes per update.
//!
//! **The invariant is the point:** every candidate must select exactly
//! what the unindexed baseline selects (the total, the severity and status
//! counts and the first page's ids). A difference fails the run. Timings
//! are a hosted runner's: relative numbers and plan shapes only.
//!
//! The summary goes to stdout and, with `ALRUST_MEASURE_OUT`, to
//! `summary.md` and one plan file per case in that directory.
#![expect(
    clippy::unwrap_used,
    clippy::print_stderr,
    reason = "measurement: helpers unwrap, skips are printed"
)]

use std::fmt::Write as _;
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::{Duration, Instant};

use alrust_domain::HistoryOutput;
use alrust_store_pg::search::compile;
use alrust_store_pg::{AlertQuery, Explain, NewAlert, ReceiveKind, Store, StoreConfig};
use chrono::{TimeZone, Utc};
use sqlx::Executor as _;
use sqlx::postgres::{PgConnectOptions, PgPool, PgPoolOptions};

static COUNTER: AtomicU32 = AtomicU32::new(0);

struct TestDb {
    base: PgConnectOptions,
    name: String,
    pool: PgPool,
}

impl Drop for TestDb {
    fn drop(&mut self) {
        let admin = self.base.clone();
        let name = self.name.clone();
        let outcome = std::thread::spawn(move || -> Result<(), String> {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .map_err(|e| e.to_string())?;
            rt.block_on(async move {
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
                pool.close().await;
                Ok(())
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

async fn test_db() -> Option<TestDb> {
    let url = match std::env::var("DATABASE_URL") {
        Ok(u) if !u.is_empty() => u,
        _ if std::env::var_os("CI").is_some() => panic!("DATABASE_URL is required under CI"),
        _ => {
            eprintln!("skipped: DATABASE_URL is not set");
            return None;
        }
    };
    let base: PgConnectOptions = url.parse().expect("DATABASE_URL");
    let prefix = std::env::var("ALRUST_TEST_DB_PREFIX")
        .unwrap_or_else(|_| format!("alrust_t_{}_", std::process::id()));
    assert!(
        prefix.starts_with("alrust_t_"),
        "bad ALRUST_TEST_DB_PREFIX {prefix:?}"
    );
    let name = format!(
        "{prefix}m{}_{}",
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
    let pool = PgPoolOptions::new().max_connections(4).connect_lazy_with(
        base.clone()
            .options([("search_path", alrust_store_pg::SEARCH_PATH)])
            .database(&name),
    );
    Some(TestDb { base, name, pool })
}

const EVENTS: [&str; 8] = [
    "NodeDown",
    "HttpError",
    "DiskFull",
    "CpuHigh",
    "MemoryLow",
    "LinkDown",
    "PingLoss",
    "ServiceDown",
];
const ENVIRONMENTS: [&str; 3] = ["Production", "Development", "Staging"];
const SEVERITIES: [&str; 6] = ["critical", "major", "minor", "warning", "normal", "ok"];
const STATUSES: [&str; 5] = ["open", "ack", "closed", "shelved", "expired"];

/// `n` synthetic alerts `m-1` … `m-n`. Each value is a fixed function of
/// the row number, so [`identity`] can name an existing row; `(environment,
/// resource, event)` is unique (`resource` is `host-<g/8>`, `event` cycles
/// through 8). `text` draws three words from a 16-word vocabulary.
fn seed_sql(n: u32) -> String {
    format!(
        r#"
INSERT INTO alerts (id, resource, event, environment, severity, correlate, status, service,
    "group", value, text, tags, attributes, origin, type, create_time, timeout, raw_data,
    customer, duplicate_count, repeat, previous_severity, trend_indication, receive_time,
    last_receive_id, last_receive_time, history, update_time)
SELECT 'm-' || g, 'host-' || (g / 8),
    (ARRAY['NodeDown','HttpError','DiskFull','CpuHigh','MemoryLow','LinkDown','PingLoss','ServiceDown'])[1 + g % 8],
    (ARRAY['Production','Development','Staging'])[1 + (g / 7) % 3],
    (ARRAY['critical','major','minor','warning','normal','ok'])[1 + (g / 3) % 6],
    ARRAY[]::text[],
    (ARRAY['open','ack','closed','shelved','expired'])[1 + (g / 11) % 5],
    ARRAY[(ARRAY['Web','Db','Cache','Queue'])[1 + g % 4]],
    'grp-' || (g % 20), NULL,
    w.a || ' ' || w.b || ' ' || w.c || ' on host-' || (g / 8),
    ARRAY['dc' || (g % 3)],
    jsonb_build_object('region', (ARRAY['eu','us','ap'])[1 + g % 3]),
    'measure', 'exceptionAlert',
    TIMESTAMP '2026-10-01 00:00:00' + make_interval(secs => g),
    CASE WHEN g % 97 = 0 THEN 30 ELSE 86400 END, NULL, NULL, g % 10, false,
    'indeterminate', 'moreSevere',
    TIMESTAMP '2026-10-01 00:00:00' + make_interval(secs => g), 'm-' || g,
    TIMESTAMP '2026-10-01 00:00:00' + make_interval(secs => g),
    ARRAY[]::history[],
    TIMESTAMP '2026-10-01 00:00:00' + make_interval(secs => g)
FROM generate_series(1, {n}) AS g,
LATERAL (SELECT
    (ARRAY['disk','full','server','timeout','connection','refused','latency','high',
           'memory','swap','link','down','packet','loss','service','restart'])[1 + g % 16] AS a,
    (ARRAY['disk','full','server','timeout','connection','refused','latency','high',
           'memory','swap','link','down','packet','loss','service','restart'])[1 + (g * 7) % 16] AS b,
    (ARRAY['disk','full','server','timeout','connection','refused','latency','high',
           'memory','swap','link','down','packet','loss','service','restart'])[1 + (g * 11 + 3) % 16] AS c
) AS w
"#
    )
}

/// The receive identity of seeded row `g`, as [`seed_sql`] wrote it.
fn identity(g: u32, text: String) -> NewAlert {
    let at = |list: &[&str], i: u32| list[usize::try_from(i).unwrap() % list.len()].to_owned();
    NewAlert {
        id: format!("dup-{g}-{}", COUNTER.fetch_add(1, Ordering::SeqCst)),
        resource: format!("host-{}", g / 8),
        event: at(&EVENTS, g),
        environment: at(&ENVIRONMENTS, g / 7),
        severity: at(&SEVERITIES, g / 3),
        correlate: vec![],
        status: at(&STATUSES, g / 11),
        service: vec![at(&["Web", "Db", "Cache", "Queue"], g)],
        group: format!("grp-{}", g % 20),
        value: None,
        text,
        tags: vec![],
        attributes: serde_json::Map::default(),
        origin: "measure".into(),
        event_type: "exceptionAlert".into(),
        create_time: Utc.with_ymd_and_hms(2026, 11, 1, 0, 0, 0).unwrap(),
        timeout: 86400,
        raw_data: None,
        customer: None,
    }
}

/// A candidate: its name and the statements that create it (dropped by
/// name afterwards).
struct Candidate {
    name: &'static str,
    create: &'static [&'static str],
    drop: &'static [&'static str],
}

const CANDIDATES: [Candidate; 8] = [
    Candidate {
        name: "none",
        create: &[],
        drop: &[],
    },
    Candidate {
        name: "btree last_receive_time",
        create: &["CREATE INDEX m_lrt ON alerts (last_receive_time DESC, id)"],
        drop: &["DROP INDEX m_lrt"],
    },
    Candidate {
        name: "btree status, environment, last_receive_time",
        create: &["CREATE INDEX m_sel ON alerts (status, environment, last_receive_time DESC, id)"],
        drop: &["DROP INDEX m_sel"],
    },
    Candidate {
        name: "btree severity, status",
        create: &["CREATE INDEX m_sevst ON alerts (severity, status)"],
        drop: &["DROP INDEX m_sevst"],
    },
    Candidate {
        name: "btree environment, resource",
        create: &["CREATE INDEX m_envres ON alerts (environment, resource)"],
        drop: &["DROP INDEX m_envres"],
    },
    Candidate {
        name: "pg_trgm GIN text",
        create: &["CREATE INDEX m_trgm_gin ON alerts USING gin (text gin_trgm_ops)"],
        drop: &["DROP INDEX m_trgm_gin"],
    },
    Candidate {
        name: "pg_trgm GiST text",
        create: &["CREATE INDEX m_trgm_gist ON alerts USING gist (text gist_trgm_ops)"],
        drop: &["DROP INDEX m_trgm_gist"],
    },
    Candidate {
        name: "pg_trgm GIN resource, event",
        create: &[
            "CREATE INDEX m_trgm_res ON alerts USING gin (resource gin_trgm_ops)",
            "CREATE INDEX m_trgm_evt ON alerts USING gin (event gin_trgm_ops)",
        ],
        drop: &["DROP INDEX m_trgm_res", "DROP INDEX m_trgm_evt"],
    },
];

/// The measured reads: the dashboard's hot path first, then `q=` forms.
fn cases() -> Vec<(String, AlertQuery)> {
    let mut out = vec![
        ("all alerts".to_owned(), AlertQuery::default()),
        (
            "status=open&environment=Production".to_owned(),
            AlertQuery {
                statuses: vec!["open".into()],
                environments: vec!["Production".into()],
                ..AlertQuery::default()
            },
        ),
        (
            "resource=host-42".to_owned(),
            AlertQuery {
                resources: vec!["host-42".into()],
                ..AlertQuery::default()
            },
        ),
    ];
    for q in [
        "disk",
        "\"disk full\"",
        "dis*",
        "/^disk/",
        "resource:host*",
        "resource:(host-1 OR host-2)",
        "event:DiskFull",
        "_.region:eu",
        "_exists_:region",
        "service:Web",
        "timeout:<60",
        "NOT disk",
        "disk AND full AND server",
        "disk swap packet",
        "di",
        "Dis*",
    ] {
        out.push((
            format!("q={q}"),
            AlertQuery {
                search: Some(compile(q, None).unwrap()),
                ..AlertQuery::default()
            },
        ));
    }
    out
}

/// What a read selects: the total, both count maps and the first page.
#[derive(Debug, Clone, PartialEq)]
struct Selected {
    total: i64,
    counts: String,
    first_page: Vec<String>,
}

async fn select(store: &Store, q: &AlertQuery) -> (Selected, Duration) {
    let start = Instant::now();
    let page = store
        .find_alerts(q, 1, 50, HistoryOutput::Omit)
        .await
        .unwrap();
    let took = start.elapsed();
    let selected = Selected {
        total: page.total,
        counts: format!(
            "{} {}",
            serde_json::Value::Object(page.severity_counts),
            serde_json::Value::Object(page.status_counts)
        ),
        first_page: page.alerts.into_iter().map(|a| a.id).collect(),
    };
    (selected, took)
}

/// One warm-up, then [`RUNS`] timed reads: the selection and the times.
async fn timed(store: &Store, q: &AlertQuery) -> (Selected, Vec<Duration>) {
    let _ = select(store, q).await;
    let mut times = Vec::with_capacity(RUNS);
    let mut got = None;
    for _ in 0..RUNS {
        let (s, t) = select(store, q).await;
        times.push(t);
        got = Some(s);
    }
    (got.unwrap(), times)
}

fn median(mut v: Vec<Duration>) -> Duration {
    v.sort();
    v[v.len() / 2]
}

fn ms(d: Duration) -> String {
    format!("{:.2}", d.as_secs_f64() * 1000.0)
}

/// The first plan line that names one of the candidate's indexes, or the
/// plan's top node.
fn plan_node(plan: &str, indexes: &[&str]) -> String {
    plan.lines()
        .find(|l| indexes.iter().any(|i| l.contains(i)))
        .or_else(|| plan.lines().next())
        .unwrap_or("")
        .trim()
        .chars()
        .take(90)
        .collect()
}

fn index_names(c: &Candidate) -> Vec<&'static str> {
    c.drop
        .iter()
        .map(|d| d.trim_start_matches("DROP INDEX "))
        .collect()
}

async fn wal_lsn(pool: &PgPool) -> String {
    sqlx::query_scalar("SELECT pg_current_wal_lsn()::text")
        .fetch_one(pool)
        .await
        .unwrap()
}

async fn wal_bytes(pool: &PgPool, from: &str, to: &str) -> i64 {
    sqlx::query_scalar("SELECT pg_wal_lsn_diff($2::pg_lsn, $1::pg_lsn)::bigint")
        .bind(from)
        .bind(to)
        .fetch_one(pool)
        .await
        .unwrap()
}

const RUNS: usize = 5;
const DUPLICATES: u32 = 200;

/// The first `count` seeded rows from `start` on whose status is `open`
/// (`(g / 11) % 5 == 0` in [`seed_sql`]): 11 in every 55, so 200 fit in
/// 1,100 rows, and eight candidates fit in the 10,000-row size.
fn open_rows(start: u32, count: u32) -> Vec<u32> {
    (start..)
        .filter(|g| (g / 11) % 5 == 0)
        .take(usize::try_from(count).unwrap())
        .collect()
}

#[tokio::test]
#[ignore = "measurement: run by .github/workflows/search-measure.yml"]
#[expect(
    clippy::too_many_lines,
    reason = "one measurement loop: sizes, candidates, reads, writes"
)]
async fn measure_search_and_alert_reads() {
    let sizes = std::env::var("ALRUST_MEASURE_ROWS").unwrap_or_else(|_| "10000,100000".into());
    let out_dir = std::env::var_os("ALRUST_MEASURE_OUT").map(std::path::PathBuf::from);
    if let Some(d) = &out_dir {
        std::fs::create_dir_all(d).unwrap();
    }
    let mut summary = String::from(
        "# Search measurement (hosted runner, PostgreSQL 18.6: relative numbers and plan shapes only)\n",
    );
    let mut mismatches = Vec::new();
    for n in sizes.split(',').map(|s| s.trim().parse::<u32>().unwrap()) {
        let Some(db) = test_db().await else {
            return;
        };
        let store = Store::new(db.pool.clone(), StoreConfig::default());
        store.prepare().await.unwrap();
        // The same statements on sessions that always plan with the bound
        // values: sqlx prepares every statement, and after five executions
        // PostgreSQL may switch to a generic plan that ignores them.
        let custom = Store::new(
            PgPoolOptions::new().max_connections(4).connect_lazy_with(
                db.base
                    .clone()
                    .options([
                        ("search_path", alrust_store_pg::SEARCH_PATH),
                        ("plan_cache_mode", "force_custom_plan"),
                    ])
                    .database(&db.name),
            ),
            StoreConfig::default(),
        );
        let start = Instant::now();
        db.pool.execute(seed_sql(n).as_str()).await.unwrap();
        // VACUUM sets the visibility map, as autovacuum would, so an
        // index-only scan is possible.
        db.pool.execute("VACUUM (ANALYZE) alerts").await.unwrap();
        let trgm = db
            .pool
            .execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            .await;
        let version: String = sqlx::query_scalar("SELECT version()")
            .fetch_one(&db.pool)
            .await
            .unwrap();
        writeln!(
            summary,
            "\n## {n} alerts\n\n{version}\n\nseeded and analysed in {} ms; pg_trgm: {}\n",
            ms(start.elapsed()),
            trgm.as_ref()
                .map_or_else(|e| format!("unavailable ({e})"), |_| "installed".into())
        )
        .unwrap();
        writeln!(
            summary,
            "| candidate | index KB | build ms | read | median ms | median ms, custom plans | plan node | servable by an index |\n|---|---|---|---|---|---|---|---|"
        )
        .unwrap();
        let cases = cases();
        let mut baseline: Vec<Selected> = Vec::new();
        let mut writes = String::from(
            "\n| candidate | duplicate updates | ms each | WAL bytes each |\n|---|---|---|---|\n",
        );
        for (ci, cand) in CANDIDATES.iter().enumerate() {
            if cand.name.starts_with("pg_trgm") && trgm.is_err() {
                continue;
            }
            let start = Instant::now();
            for stmt in cand.create {
                db.pool.execute(*stmt).await.unwrap();
            }
            db.pool.execute("ANALYZE alerts").await.unwrap();
            let build = start.elapsed();
            let names = index_names(cand);
            let mut size_bytes = 0i64;
            for i in &names {
                let s: i64 = sqlx::query_scalar("SELECT pg_relation_size($1::regclass)")
                    .bind(*i)
                    .fetch_one(&db.pool)
                    .await
                    .unwrap();
                size_bytes += s;
            }
            let size_kb = size_bytes / 1024;
            for (qi, (label, q)) in cases.iter().enumerate() {
                let (got, times) = timed(&store, q).await;
                let (got_custom, times_custom) = timed(&custom, q).await;
                if got_custom != got {
                    mismatches.push(format!("{n} alerts, {}, {label}, custom plans", cand.name));
                }
                if ci == 0 {
                    baseline.push(got);
                } else if baseline[qi] != got {
                    mismatches.push(format!("{n} alerts, {}, {label}", cand.name));
                }
                let analyzed = store
                    .explain_alert_reads(q, 50, Explain::Analyze)
                    .await
                    .unwrap();
                let servable = if names.is_empty() {
                    "-".to_owned()
                } else {
                    let forced = store
                        .explain_alert_reads(q, 50, Explain::PlanWithoutSeqScan)
                        .await
                        .unwrap();
                    let uses = |p: &str| names.iter().any(|i| p.contains(i));
                    format!(
                        "counts {}, page {}",
                        if uses(&forced[0]) { "yes" } else { "no" },
                        if uses(&forced[1]) { "yes" } else { "no" }
                    )
                };
                writeln!(
                    summary,
                    "| {} | {size_kb} | {} | `{}` | {} | {} | {} | {servable} |",
                    cand.name,
                    ms(build),
                    label.replace('|', "\\|"),
                    ms(median(times)),
                    ms(median(times_custom)),
                    plan_node(&analyzed[0], &names).replace('|', "\\|"),
                )
                .unwrap();
                if let Some(d) = &out_dir {
                    let file = d.join(format!("plan-{n}-{ci}-{qi}.txt"));
                    std::fs::write(
                        file,
                        format!(
                            "{n} alerts; {}; {label}\n\n-- counts\n{}\n\n-- page\n{}\n",
                            cand.name, analyzed[0], analyzed[1]
                        ),
                    )
                    .unwrap();
                }
            }
            for stmt in cand.drop {
                db.pool.execute(*stmt).await.unwrap();
            }
        }
        // Pass B, after every read: the receive path's duplicate update
        // rewrites `text` and `last_receive_time`, so it would change what
        // later reads select. One disjoint set of open seeded rows per
        // candidate, each received once more as an open duplicate.
        for (ci, cand) in CANDIDATES.iter().enumerate() {
            if cand.name.starts_with("pg_trgm") && trgm.is_err() {
                continue;
            }
            for stmt in cand.create {
                db.pool.execute(*stmt).await.unwrap();
            }
            let rows = open_rows(1 + u32::try_from(ci).unwrap() * 1100, DUPLICATES);
            let before = wal_lsn(&db.pool).await;
            let start = Instant::now();
            for g in &rows {
                let (_, kind) = store
                    .receive(
                        identity(*g, format!("updated text {g} disk full")),
                        "measure",
                    )
                    .await
                    .unwrap();
                assert_eq!(kind, ReceiveKind::Duplicate, "row m-{g}");
            }
            let took = start.elapsed();
            let after = wal_lsn(&db.pool).await;
            let wal = wal_bytes(&db.pool, &before, &after).await;
            writeln!(
                writes,
                "| {} | {DUPLICATES} | {} | {} |",
                cand.name,
                ms(took / DUPLICATES),
                wal / i64::from(DUPLICATES)
            )
            .unwrap();
            for stmt in cand.drop {
                db.pool.execute(*stmt).await.unwrap();
            }
        }
        summary.push_str(&writes);
    }
    writeln!(
        summary,
        "\nSelection differences from the unindexed baseline: {}",
        if mismatches.is_empty() {
            "none".to_owned()
        } else {
            mismatches.join("; ")
        }
    )
    .unwrap();
    println!("{summary}");
    if let Some(d) = &out_dir {
        std::fs::write(d.join("summary.md"), &summary).unwrap();
    }
    assert!(mismatches.is_empty(), "{mismatches:?}");
}
