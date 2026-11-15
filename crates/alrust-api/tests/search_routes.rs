//! `q=` search through the router, against PostgreSQL: `GET /alerts` and
//! `/alerts/count` (reference `postgres/queryparser.py`,
//! `postgres/utils.py:144-155`; `spec/search-q.md`; `spec/security-fixes.md`
//! SF-QUERY items 3, 4, 6 and 7, vectors 14 to 23 and 26 to 30).
//!
//! The store is SF-QUERY's `QF` with two changes, so that the typed range
//! bounds have a discriminating hit: `q1` has `timeout 30` (QF: 86400) and
//! `q2` has `duplicate_count 3` (QF: 0).
//!
//! Each test creates its own database `<prefix>s<n>_<µs>` and drops it when
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
use serde_json::{Value, json};
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
        "{prefix}s{}_{}",
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

/// The `QF` alerts, seeded directly and completely (SF-QUERY fixtures),
/// with `T0` = 2026-11-01 00:00.
const SEED: &str = r#"
INSERT INTO alerts (id, resource, event, environment, severity, status, previous_severity,
    trend_indication, type, origin, value, repeat, timeout, correlate, tags, service, customer,
    text, attributes, duplicate_count, last_receive_id, create_time, receive_time, update_time,
    last_receive_time, history)
SELECT v.id, v.resource, v.event, v.environment, 'major', 'open', 'indeterminate',
    'moreSevere', 'exceptionAlert', 'fixture', NULL, false, v.timeout, ARRAY[]::text[],
    ARRAY[]::text[], ARRAY['svc'], NULL, v.text, v.attributes::jsonb, v.dup, v.id,
    TIMESTAMP '2026-11-01 00:00:00', TIMESTAMP '2026-11-01 00:00:00',
    TIMESTAMP '2026-11-01 00:00:00', TIMESTAMP '2026-11-01 00:00:00', ARRAY[]::history[]
FROM (VALUES
    ('q1', 'res-alpha',   'ev-1', 'Production',   'disk',             '{"region": "b"}',   30,    0),
    ('q2', 'res-bravo',   'ev-2', 'Production',   'full',             '{"region": "a"}',   86400, 3),
    ('q3', 'res-charlie', 'ev-3', 'Production',   'server',           '{"reg''ion": "z"}', 86400, 0),
    ('q4', 'res-delta',   'ev-4', 'Production',   'disk full',        '{}',                86400, 0),
    ('q5', 'res-echo',    'ev-5', 'Production',   'disk full server', '{}',                86400, 0),
    ('q6', 'res-foxtrot', 'ev-6', 'Prod''uction', 'other',            '{}',                86400, 0)
) AS v(id, resource, event, environment, text, attributes, timeout, dup)
"#;

const ALL: [&str; 6] = [
    "res-alpha",
    "res-bravo",
    "res-charlie",
    "res-delta",
    "res-echo",
    "res-foxtrot",
];

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

/// Percent-encodes every byte but the unreserved ones; `+` in particular,
/// which a query string decodes as a space.
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

fn uri(path: &str, params: &[(&str, &str)]) -> String {
    let query: Vec<String> = params
        .iter()
        .map(|(k, v)| format!("{}={}", enc(k), enc(v)))
        .collect();
    format!("{path}?{}", query.join("&"))
}

async fn get(app: &axum::Router, uri: &str) -> (StatusCode, Value) {
    let req = Request::builder()
        .method("GET")
        .uri(uri)
        .header("Authorization", format!("Key {READER_KEY}"))
        .body(Body::empty())
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

/// The resources `GET /alerts` returns for `params`, sorted.
async fn found(app: &axum::Router, params: &[(&str, &str)]) -> Vec<String> {
    let (status, body) = get(app, &uri("/alerts", params)).await;
    assert_eq!(status, StatusCode::OK, "{params:?}: {body}");
    let mut out: Vec<String> = body["alerts"]
        .as_array()
        .unwrap()
        .iter()
        .map(|a| a["resource"].as_str().unwrap().to_owned())
        .collect();
    out.sort();
    assert_eq!(body["total"], json!(out.len()), "{params:?}");
    out
}

/// The 400 message `GET /alerts` gives for `params`.
async fn refused(app: &axum::Router, params: &[(&str, &str)]) -> String {
    let (status, body) = get(app, &uri("/alerts", params)).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{params:?}: {body}");
    body["message"].as_str().unwrap().to_owned()
}

fn set(v: &[&str]) -> Vec<String> {
    let mut out: Vec<String> = v.iter().map(|s| (*s).to_owned()).collect();
    out.sort();
    out
}

#[tokio::test]
async fn sf_query_search_vectors() {
    let Some((_db, app)) = seeded().await else {
        return;
    };
    // 14, 18, 19, 21, 23.
    for (params, want) in [
        (
            &[("q", "alpha"), ("q.df", "resource")][..],
            &["res-alpha"][..],
        ),
        (&[("q.df", "anything")][..], &ALL[..]),
        (&[("q", "resource:alpha")][..], &["res-alpha"][..]),
        (&[("q", "_.region:a")][..], &["res-bravo"][..]),
        (
            &[("q", "_exists_:region")][..],
            &["res-alpha", "res-bravo"][..],
        ),
    ] {
        assert_eq!(found(&app, params).await, set(want), "{params:?}");
    }
    // 15, 16, 17, 20, 22.
    for (params, message) in [
        (
            &[("q", "disk"), ("q.df", "resource\" IS NOT NULL OR \"text")][..],
            "Default field 'resource\" IS NOT NULL OR \"text' not supported.",
        ),
        (
            &[("q", "alpha"), ("q.df", "Resource")][..],
            "Default field 'Resource' not supported.",
        ),
        (
            &[("q", "resource:alpha"), ("q.df", "bogus")][..],
            "Default field 'bogus' not supported.",
        ),
        (
            &[("q", "unknownfield:disk")][..],
            "Search field 'unknownfield' not supported.",
        ),
        (
            &[("q", "raw_data.x:y")][..],
            "Search field 'raw_data.x' not supported.",
        ),
    ] {
        assert_eq!(refused(&app, params).await, message, "{params:?}");
    }
}

/// Vectors 26 to 30: chains fold to the left (SF-QUERY item 6).
#[tokio::test]
async fn chains_keep_every_operand() {
    let Some((_db, app)) = seeded().await else {
        return;
    };
    for (q, want) in [
        (
            "disk full server",
            &[
                "res-alpha",
                "res-bravo",
                "res-charlie",
                "res-delta",
                "res-echo",
            ][..],
        ),
        ("disk AND full AND server", &["res-echo"][..]),
        (
            "disk OR full OR NOT server",
            &["res-alpha", "res-bravo", "res-delta"][..],
        ),
        (
            "resource:(alpha OR bravo OR charlie)",
            &["res-alpha", "res-bravo", "res-charlie"][..],
        ),
        ("disk AND full", &["res-delta", "res-echo"][..]),
        (
            "disk full",
            &["res-alpha", "res-bravo", "res-delta", "res-echo"][..],
        ),
        ("disk AND (full AND server)", &["res-echo"][..]),
    ] {
        assert_eq!(found(&app, &[("q", q)]).await, set(want), "{q}");
    }
}

/// The translations reach PostgreSQL as the reference's: typed range
/// bounds (cast from the bound text), `?`, `=ANY`, `~*` and `NOT`; a
/// non-ASCII character ends a word. Two listed forms keep the reference's
/// expected database error: `+` emits a literal sign (F5) and `ILIKE` on
/// `history` (F16).
#[tokio::test]
async fn translations_run_in_postgresql() {
    let Some((_db, app)) = seeded().await else {
        return;
    };
    for (q, want) in [
        ("timeout:<60", &["res-alpha"][..]),
        ("duplicate_count:[2 TO 5]", &["res-bravo"][..]),
        ("duplicate_count:{0 TO *]", &["res-bravo"][..]),
        ("create_time:>=2026-11-01", &ALL[..]),
        ("create_time:<2026-10-31", &[][..]),
        ("service:svc", &ALL[..]),
        ("\"disk full\"", &["res-delta", "res-echo"][..]),
        ("dis*", &["res-alpha", "res-delta", "res-echo"][..]),
        ("/^disk$/", &["res-alpha"][..]),
        ("NOT disk", &["res-bravo", "res-charlie", "res-foxtrot"][..]),
        ("résumé", &["res-charlie", "res-echo", "res-foxtrot"][..]),
        ("environment:\"Prod'uction\"", &["res-foxtrot"][..]),
    ] {
        assert_eq!(found(&app, &[("q", q)]).await, set(want), "{q}");
    }
    for q in ["+disk", "history:ack"] {
        let (status, _) = get(&app, &uri("/alerts", &[("q", q)])).await;
        assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR, "{q}");
    }
}

/// SF-QUERY item 7 and the first-occurrence rule; the hook 4.2 guard runs
/// before any of them.
#[tokio::test]
async fn checks_run_in_order_on_the_first_q() {
    let Some((_db, app)) = seeded().await else {
        return;
    };
    for (params, message) in [
        (
            &[("q", "(disk"), ("q.df", "bogus")][..],
            "Failed to parse query string.",
        ),
        (
            &[("q", "nosuch:x"), ("q.df", "bogus")][..],
            "Default field 'bogus' not supported.",
        ),
        (
            &[("q", "nosuch:x"), ("page", "0")][..],
            "Search field 'nosuch' not supported.",
        ),
        (
            &[("q", "x"), ("q.df", "attributes")][..],
            "Query selector is not permitted on restricted attributes",
        ),
        (
            &[("q", "_exists_:zabbix_ack_sync_intent")][..],
            "Query selector is not permitted on restricted attributes",
        ),
        (&[("q", "\"\\0\"")][..], "Failed to parse query string."),
    ] {
        assert_eq!(refused(&app, params).await, message, "{params:?}");
    }
    let deep = format!("{}a{}", "(".repeat(32), ")".repeat(32));
    assert_eq!(
        refused(&app, &[("q", deep.as_str())]).await,
        "Failed to parse query string."
    );
    let shallow = format!("{}disk{}", "(".repeat(31), ")".repeat(31));
    assert_eq!(
        found(&app, &[("q", shallow.as_str())]).await,
        set(&["res-alpha", "res-delta", "res-echo"])
    );
    assert_eq!(found(&app, &[("q", ""), ("q", "disk")]).await, set(&ALL));
    assert_eq!(
        found(&app, &[("q", "full"), ("q", "disk")]).await,
        set(&["res-bravo", "res-delta", "res-echo"])
    );
    assert_eq!(
        found(&app, &[("q", "alpha"), ("q.df", ""), ("q.df", "resource")]).await,
        Vec::<String>::new()
    );
}

/// The search is part of every alert query: the totals and the severity and
/// status counts follow it.
#[tokio::test]
async fn counts_follow_the_search() {
    let Some((_db, app)) = seeded().await else {
        return;
    };
    let (status, body) = get(&app, &uri("/alerts/count", &[("q", "disk")])).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["total"], json!(3));
    assert_eq!(body["severityCounts"], json!({"major": 3}));
    assert_eq!(body["statusCounts"], json!({"open": 3}));
    let (status, body) = get(&app, &uri("/alerts/count", &[("q", "nosuch:x")])).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(
        body["message"],
        json!("Search field 'nosuch' not supported.")
    );
}
