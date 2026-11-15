//! `q.mode=lucene` through the router, against PostgreSQL
//! (`spec/search-lucene-mode.md`): every vector of the spec on the `QL`
//! store, each in the default mode (#245, unchanged) and in the opt-in
//! mode.
//!
//! Each test creates its own database `<prefix>l<n>_<µs>` and drops it when
//! it ends, also on panic. Without `DATABASE_URL` the tests are skipped,
//! except under CI (`CI` set), where a missing database is a failure. Keys
//! and alerts are synthetic.
#![expect(
    clippy::unwrap_used,
    clippy::print_stderr,
    reason = "integration test: helpers unwrap, skips are printed, and each scenario stays in one function"
)]

use std::sync::Arc;
use std::sync::atomic::{AtomicU32, Ordering};

use alrust_api::{ApiConfig, AppState, router};
use alrust_domain::ApiKey;
use alrust_store_pg::{Store, StoreConfig};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use chrono::Utc;
use serde_json::Value;
use sqlx::Executor as _;
use sqlx::postgres::{PgConnectOptions, PgPool, PgPoolOptions};
use tower::ServiceExt as _;

static COUNTER: AtomicU32 = AtomicU32::new(0);

const READER_KEY: &str = "synthetic-reader-key-00000000000000000000";

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
        "{prefix}l{}_{}",
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

/// The `QL` store: SF-QUERY's `QF` (`q1` … `q6`) and `q7` … `q9`, seeded
/// directly and completely, `T0` = 2026-11-01 00:00. `group` and `value`
/// are NULL everywhere, so a negation must not turn NULL into "unknown".
const SEED: &str = r#"
INSERT INTO alerts (id, resource, event, environment, severity, status, previous_severity,
    trend_indication, type, origin, value, repeat, timeout, correlate, tags, service, customer,
    text, attributes, duplicate_count, last_receive_id, create_time, receive_time, update_time,
    last_receive_time, history)
SELECT v.id, v.resource, v.event, v.environment, 'major', 'open', 'indeterminate',
    'moreSevere', 'exceptionAlert', 'fixture', NULL, false, 86400, ARRAY[]::text[],
    v.tags, v.service, NULL, v.text, v.attributes::jsonb, v.dup, v.id,
    TIMESTAMP '2026-11-01 00:00:00', TIMESTAMP '2026-11-01 00:00:00',
    TIMESTAMP '2026-11-01 00:00:00', v.lrt, ARRAY[]::history[]
FROM (VALUES
    ('q1', 'res-alpha',   'ev-1', 'Production',   'disk',                    '{"region": "b"}',   ARRAY['svc'], ARRAY[]::text[], 0, TIMESTAMP '2026-11-01 00:00:00'),
    ('q2', 'res-bravo',   'ev-2', 'Production',   'full',                    '{"region": "a"}',   ARRAY['svc'], ARRAY[]::text[], 0, TIMESTAMP '2026-11-01 00:00:00'),
    ('q3', 'res-charlie', 'ev-3', 'Production',   'server',                  '{"reg''ion": "z"}', ARRAY['svc'], ARRAY[]::text[], 0, TIMESTAMP '2026-11-01 00:00:00'),
    ('q4', 'res-delta',   'ev-4', 'Production',   'disk full',               '{}',                ARRAY['svc'], ARRAY[]::text[], 0, TIMESTAMP '2026-11-01 00:00:00'),
    ('q5', 'res-echo',    'ev-5', 'Production',   'disk full server',        '{}',                ARRAY['svc'], ARRAY[]::text[], 0, TIMESTAMP '2026-11-01 00:00:00'),
    ('q6', 'res-foxtrot', 'ev-6', 'Prod''uction', 'other',                   '{}',                ARRAY['svc'], ARRAY[]::text[], 0, TIMESTAMP '2026-11-01 00:00:00'),
    ('q7', 'web-01',      'ev-7', 'Production',   'Disk usage 100% on /var', '{}',                ARRAY['Web'], ARRAY['Linux'],    3, TIMESTAMP '2026-11-01 01:00:00'),
    ('q8', 'db-02',       'ev-8', 'Production',   'dsk error on a_b',        '{}',                ARRAY['svc'], ARRAY[]::text[], 0, TIMESTAMP '2026-11-01 00:00:00'),
    ('q9', 'cache-03',    'ev-9', 'Production',   '100 items in axb',        '{}',                ARRAY['svc'], ARRAY[]::text[], 0, TIMESTAMP '2026-11-01 00:00:00')
) AS v(id, resource, event, environment, text, attributes, service, tags, dup, lrt)
"#;

async fn seeded() -> Option<(TestDb, axum::Router)> {
    let db = test_db().await?;
    let store = Store::new(db.pool.clone(), StoreConfig::default());
    store.prepare().await.unwrap();
    store
        .ensure_key(&ApiKey {
            id: "k-reader".into(),
            key: READER_KEY.into(),
            user: "reader".into(),
            scopes: Some(vec!["read".to_owned()]),
            text: "synthetic".into(),
            expire_time: Some(Utc::now() + chrono::Duration::days(1)),
            count: 0,
            last_used_time: None,
            customer: None,
        })
        .await
        .unwrap();
    db.pool.execute(SEED).await.unwrap();
    let app = router(Arc::new(AppState {
        store,
        cfg: ApiConfig {
            auth_required: true,
            ..ApiConfig::default()
        },
    }));
    Some((db, app))
}

/// Percent-encodes every byte but the unreserved ones.
fn enc(s: &str) -> String {
    s.bytes()
        .map(|b| {
            if b.is_ascii_alphanumeric() || b"-._~".contains(&b) {
                char::from(b).to_string()
            } else {
                format!("%{b:02X}")
            }
        })
        .collect()
}

/// What a read gives: the sorted ids, or the status and message.
#[derive(Debug, PartialEq, Eq)]
enum Outcome {
    Ids(Vec<String>),
    Refused(String),
    Failed,
}

async fn read(app: &axum::Router, params: &[(&str, &str)]) -> Outcome {
    let query: Vec<String> = params
        .iter()
        .map(|(k, v)| format!("{}={}", enc(k), enc(v)))
        .collect();
    let req = Request::builder()
        .method("GET")
        .uri(format!("/alerts?{}", query.join("&")))
        .header("Authorization", format!("Key {READER_KEY}"))
        .body(Body::empty())
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    let body: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    match status {
        StatusCode::OK => {
            let mut ids: Vec<String> = body["alerts"]
                .as_array()
                .unwrap()
                .iter()
                .map(|a| a["id"].as_str().unwrap().to_owned())
                .collect();
            ids.sort();
            Outcome::Ids(ids)
        }
        StatusCode::BAD_REQUEST => Outcome::Refused(body["message"].as_str().unwrap().to_owned()),
        StatusCode::INTERNAL_SERVER_ERROR => Outcome::Failed,
        other => panic!("{params:?}: {other} {body}"),
    }
}

fn ids(list: &str) -> Outcome {
    let mut v: Vec<String> = list.split_whitespace().map(str::to_owned).collect();
    v.sort();
    Outcome::Ids(v)
}

fn refused(m: &str) -> Outcome {
    Outcome::Refused(m.to_owned())
}

const PARSE: &str = "Failed to parse query string.";
const RESTRICTED: &str = "Query selector is not permitted on restricted attributes";
const ALL: &str = "q1 q2 q3 q4 q5 q6 q7 q8 q9";

/// The spec's vector table: `q`, the default mode's outcome, the opt-in
/// mode's outcome (with `q.op=AND`, the default).
#[tokio::test]
async fn every_vector_in_both_modes() {
    let Some((_db, app)) = seeded().await else {
        return;
    };
    let not_disk = "q2 q3 q6 q8 q9";
    for (q, default, lucene) in [
        ("disk full", ids("q1 q2 q4 q5 q7"), ids("q4 q5")),
        ("-disk", Outcome::Failed, ids(not_disk)),
        ("NOT disk", ids(not_disk), ids(not_disk)),
        ("+disk -server", Outcome::Failed, ids("q1 q4 q7")),
        ("Dis*", ids(ALL), ids("q1 q4 q5 q7")),
        ("d?sk", ids("q1 q4 q5 q7 q8"), ids("q1 q4 q5 q7")),
        ("100%", ids("q7 q9"), ids("q7")),
        ("a_b", ids("q8 q9"), ids("q8")),
        ("\"disk.full\"", ids("q4 q5"), ids("")),
        ("disk )", ids("q1 q4 q5 q7"), refused(PARSE)),
        ("web\\-01", ids(""), ids("q7")),
        ("res-alpha", ids(""), ids("q1")),
        ("on", ids("q7 q8"), ids("q7 q8")),
        ("isk", ids("q1 q4 q5 q7"), ids("q1 q4 q5 q7")),
        ("isk*", ids(""), ids("")),
        ("service:SVC", ids(""), ids("q1 q2 q3 q4 q5 q6 q8 q9")),
        (
            "duplicateCount:>1",
            refused("Search field 'duplicateCount' not supported."),
            ids("q7"),
        ),
        ("duplicate_count:[1 TO 5]", ids("q7"), ids("q7")),
        (
            "lastReceiveTime:>\"2026-11-01T00:30:00\"",
            refused(PARSE),
            ids("q7"),
        ),
        (
            "disk~2",
            ids("q1 q4 q5 q7"),
            refused("Query operator '~' not supported."),
        ),
        (
            "history:ack",
            Outcome::Failed,
            refused("Search field 'history' not supported."),
        ),
        ("disk AND", ids("q1 q4 q5 q7"), refused(PARSE)),
        ("disk and full", ids("q1 q2 q4 q5 q7"), ids("")),
        ("/^disk$/", ids("q1"), ids("q1")),
        ("disk OR -server", Outcome::Failed, ids("q1 q4 q7")),
        ("disk OR NOT server", ids("q1 q4 q7"), ids("q1 q4 q7")),
        ("resource:(alpha OR bravo)", ids("q1 q2"), ids("q1 q2")),
        ("_.region:a", ids("q2"), ids("q2")),
        ("_exists_:region", ids("q1 q2"), ids("q1 q2")),
        (
            "_exists_:zabbix_ack_sync_intent",
            refused(RESTRICTED),
            refused(RESTRICTED),
        ),
        (
            "attributes.zabbix_ack_sync_inten\\t:x",
            ids(""),
            refused(RESTRICTED),
        ),
    ] {
        assert_eq!(read(&app, &[("q", q)]).await, default, "default mode: {q}");
        assert_eq!(
            read(&app, &[("q", q), ("q.mode", "lucene")]).await,
            lucene,
            "q.mode=lucene: {q}"
        );
    }
}

/// `q.op`, `q.df` and the surface of the mode parameters.
#[tokio::test]
async fn operator_default_field_and_surface() {
    let Some((_db, app)) = seeded().await else {
        return;
    };
    let lucene = |q: &'static str, extra: &'static [(&'static str, &'static str)]| {
        let mut p = vec![("q", q), ("q.mode", "lucene")];
        p.extend_from_slice(extra);
        p
    };
    for (params, want) in [
        (
            lucene("disk full", &[("q.op", "OR")]),
            ids("q1 q2 q4 q5 q7"),
        ),
        (
            lucene("full +disk", &[("q.op", "OR")]),
            ids("q1 q2 q4 q5 q7"),
        ),
        (lucene("disk full", &[("q.op", "AND")]), ids("q4 q5")),
        (lucene("full -disk", &[("q.op", "OR")]), ids("q2")),
        (lucene("-disk -full", &[("q.op", "OR")]), ids("q3 q6 q8 q9")),
        (
            lucene("disk", &[("q.op", "XOR")]),
            refused("Default operator 'XOR' not supported."),
        ),
        (
            lucene("res", &[("q.df", "resource")]),
            ids("q1 q2 q3 q4 q5 q6"),
        ),
        (lucene("res", &[("q.df", "text")]), ids("")),
        (
            lucene("x", &[("q.df", "bogus")]),
            refused("Default field 'bogus' not supported."),
        ),
        (
            vec![("q", "disk"), ("q.mode", "bogus")],
            refused("Query mode 'bogus' not supported."),
        ),
        (vec![("q", "disk"), ("q.op", "XOR")], ids("q1 q4 q5 q7")),
        (vec![("q.mode", "bogus")], ids(ALL)),
        (
            vec![("q", "disk full"), ("q.mode", "alerta")],
            ids("q1 q2 q4 q5 q7"),
        ),
        (
            vec![("q", "disk full"), ("q.mode", "")],
            ids("q1 q2 q4 q5 q7"),
        ),
        (
            vec![
                ("q", "disk full"),
                ("q.mode", "lucene"),
                ("q.mode", "alerta"),
            ],
            ids("q4 q5"),
        ),
    ] {
        assert_eq!(read(&app, &params).await, want, "{params:?}");
    }
}
