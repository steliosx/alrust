//! Blackout suppression on `POST /alert`, against PostgreSQL (reference
//! `plugins/blackout.py` with `NOTIFICATION_BLACKOUT = False`,
//! `postgres/base.py:784-801`; `spec/lifecycle.md` R1 step 3). Blackouts are
//! seeded directly, so these tests need no blackout route.
//!
//! Each test creates its own database `<prefix>s<n>_<µs>` and drops it when
//! it ends, also on panic. Without `DATABASE_URL` the tests are skipped,
//! except under CI (`CI` set), where a missing database is a failure. Keys,
//! alerts and blackouts are synthetic.
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

const ADMIN_KEY: &str = "synthetic-admin-key-000000000000000000000";
const ACME_KEY: &str = "synthetic-acme-key-0000000000000000000000";
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

async fn app() -> Option<(TestDb, axum::Router)> {
    let db = test_db().await?;
    let store = Store::new(db.pool.clone(), StoreConfig::default());
    store.prepare().await.unwrap();
    for (id, key, user, scopes, customer) in [
        ("k-admin", ADMIN_KEY, "admin", vec!["admin"], None),
        (
            "k-acme",
            ACME_KEY,
            "acme-writer",
            vec!["write:alerts"],
            Some("acme"),
        ),
        ("k-reader", READER_KEY, "reader", vec!["read"], None),
    ] {
        store
            .ensure_key(&ApiKey {
                id: id.into(),
                key: key.into(),
                user: user.into(),
                scopes: Some(scopes.into_iter().map(str::to_owned).collect()),
                text: "synthetic".into(),
                expire_time: Some(Utc::now() + chrono::Duration::days(1)),
                count: 0,
                last_used_time: None,
                customer: customer.map(str::to_owned),
            })
            .await
            .unwrap();
    }
    let app = router(Arc::new(AppState {
        store,
        cfg: ApiConfig {
            auth_required: true,
            ..ApiConfig::default()
        },
    }));
    Some((db, app))
}

async fn send(
    app: &axum::Router,
    method: &str,
    uri: &str,
    key: &str,
    body: Option<Value>,
) -> (StatusCode, Option<String>, Value) {
    let req = Request::builder()
        .method(method)
        .uri(uri)
        .header("Authorization", format!("Key {key}"));
    let req = match body {
        Some(b) => req
            .header("content-type", "application/json")
            .body(Body::from(b.to_string())),
        None => req.body(Body::empty()),
    }
    .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let location = resp
        .headers()
        .get("location")
        .map(|v| v.to_str().unwrap().to_owned());
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    (
        status,
        location,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

/// One blackout row; `cols` and `vals` add columns to the environment and
/// the window.
async fn blackout(db: &TestDb, id: &str, window: (&str, &str), cols: &str, vals: &str) {
    let sql = format!(
        "INSERT INTO blackouts (id, priority, environment, start_time, end_time, duration{cols}) \
         VALUES ('{id}', 0, 'Production', '{}', '{}', 0{vals})",
        window.0, window.1
    );
    db.pool.execute(sql.as_str()).await.unwrap();
}

const NOW_WINDOW: (&str, &str) = ("2020-01-01 00:00:00", "2099-01-01 00:00:00");

/// `POST /alert` with `extra` fields over a Production alert; returns the
/// status and body.
async fn post(app: &axum::Router, resource: &str, extra: Value) -> (StatusCode, Value) {
    let mut body = json!({
        "resource": resource, "event": "Down", "environment": "Production",
        "service": ["svc"], "severity": "major"
    });
    if let Value::Object(map) = extra {
        body.as_object_mut().unwrap().extend(map);
    }
    let (code, _, out) = send(app, "POST", "/alert", ADMIN_KEY, Some(body)).await;
    (code, out)
}

fn suppressed(code: StatusCode, out: &Value) -> bool {
    code == StatusCode::ACCEPTED && out["message"] == "Suppressed alert during blackout period"
}

#[tokio::test]
async fn an_alert_in_a_blackout_is_dropped_with_202() {
    let Some((db, app)) = app().await else {
        return;
    };
    blackout(&db, "b-env", NOW_WINDOW, "", "").await;
    let (code, out) = post(&app, "r1", json!({"id": "dropped-1"})).await;
    assert!(suppressed(code, &out), "{code} {out}");
    assert_eq!(out["id"], "dropped-1");
    let (code, _, _) = send(&app, "GET", "/alert/dropped-1", ADMIN_KEY, None).await;
    assert_eq!(code, StatusCode::NOT_FOUND, "never stored");
    // Another environment is not covered.
    let (code, _) = post(&app, "r1", json!({"environment": "Development"})).await;
    assert_eq!(code, StatusCode::CREATED);
    // The reject policy and the heartbeat diversion come first.
    let (code, _) = post(&app, "r1", json!({"environment": "Nowhere"})).await;
    assert_eq!(code, StatusCode::FORBIDDEN);
    let (code, out) = post(&app, "r1", json!({"event": "Heartbeat"})).await;
    assert_eq!(
        (code, out["message"].clone()),
        (StatusCode::ACCEPTED, json!("Alert converted to Heartbeat"))
    );
    // An alert that arrives with status blackout skips the plugins (R1 step 1).
    let (code, _) = post(&app, "r-blk", json!({"status": "blackout"})).await;
    assert_eq!(code, StatusCode::CREATED);
}

#[tokio::test]
async fn the_match_follows_is_blackout_period() {
    let Some((db, app)) = app().await else {
        return;
    };
    blackout(&db, "b-res", NOW_WINDOW, ", resource", ", 'web01'").await;
    blackout(&db, "b-svc", NOW_WINDOW, ", service", ", '{a}'").await;
    blackout(&db, "b-tags", NOW_WINDOW, ", tags", ", '{x}'").await;
    blackout(
        &db,
        "b-ev",
        NOW_WINDOW,
        ", event, \"group\"",
        ", 'Disk', 'Storage'",
    )
    .await;
    blackout(
        &db,
        "b-org",
        NOW_WINDOW,
        ", origin, resource",
        ", 'probe/1', 'db01'",
    )
    .await;
    for (resource, extra, dropped) in [
        ("web01", json!({}), true),
        ("web02", json!({}), false),
        // The blackout's service must be contained in the alert's.
        ("app1", json!({"service": ["a", "b"]}), true),
        ("app2", json!({"service": ["b"]}), false),
        ("app3", json!({"tags": ["x", "y"]}), true),
        ("app4", json!({"tags": ["y"]}), false),
        ("app5", json!({"event": "Disk", "group": "Storage"}), true),
        ("app6", json!({"event": "Disk"}), false),
        ("db01", json!({"origin": "probe/1"}), true),
        ("db01", json!({"origin": "probe/2"}), false),
    ] {
        let (code, out) = post(&app, resource, extra.clone()).await;
        assert_eq!(
            suppressed(code, &out),
            dropped,
            "{resource} {extra}: {code} {out}"
        );
    }
}

#[tokio::test]
async fn the_window_uses_the_alerts_create_time() {
    let Some((db, app)) = app().await else {
        return;
    };
    blackout(
        &db,
        "b-past",
        ("2020-01-01 00:00:00", "2021-01-01 00:00:00"),
        "",
        "",
    )
    .await;
    blackout(
        &db,
        "b-future",
        ("2098-01-01 00:00:00", "2099-01-01 00:00:00"),
        "",
        "",
    )
    .await;
    let (code, _) = post(&app, "now", json!({})).await;
    assert_eq!(code, StatusCode::CREATED, "no blackout covers now");
    let (code, out) = post(
        &app,
        "then",
        json!({"createTime": "2020-06-01T00:00:00.000Z"}),
    )
    .await;
    assert!(suppressed(code, &out), "{code} {out}");
    // end_time is exclusive, start_time inclusive.
    let (code, _) = post(
        &app,
        "end",
        json!({"createTime": "2021-01-01T00:00:00.000Z"}),
    )
    .await;
    assert_eq!(code, StatusCode::CREATED);
    let (code, out) = post(
        &app,
        "start",
        json!({"createTime": "2020-01-01T00:00:00.000Z"}),
    )
    .await;
    assert!(suppressed(code, &out), "{code} {out}");
}

/// `CUSTOMER_VIEWS` is off: as in the reference, a blackout of one customer
/// suppresses another customer's matching alert.
#[tokio::test]
async fn no_customer_predicate_applies() {
    let Some((db, app)) = app().await else {
        return;
    };
    blackout(
        &db,
        "b-other",
        NOW_WINDOW,
        ", customer, resource",
        ", 'other', 'shared'",
    )
    .await;
    let (code, out) = post(&app, "shared", json!({"customer": "acme"})).await;
    assert!(suppressed(code, &out), "{code} {out}");
}
