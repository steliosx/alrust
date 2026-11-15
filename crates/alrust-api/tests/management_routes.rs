//! `GET /management/manifest` and `/management/status` through the router,
//! against PostgreSQL (reference `management/views.py:67-79`, `:228-242`).
//!
//! Each test creates its own database `<prefix>m<n>_<µs>` and drops it when
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

const ADMIN_KEY: &str = "synthetic-admin-key-000000000000000000000";
const READER_KEY: &str = "synthetic-reader-key-00000000000000000000";
const ALERTS_KEY: &str = "synthetic-alertsread-key-0000000000000000";

async fn app() -> Option<(TestDb, axum::Router)> {
    let db = test_db().await?;
    let store = Store::new(db.pool.clone(), StoreConfig::default());
    store.prepare().await.unwrap();
    let keys: [(&str, &str, &str, &str, Option<&str>); 3] = [
        ("k-admin", ADMIN_KEY, "admin", "admin", None),
        ("k-reader", READER_KEY, "reader", "read", None),
        ("k-alerts", ALERTS_KEY, "alerts-reader", "read:alerts", None),
    ];
    for (id, key, user, scope, customer) in keys {
        store
            .ensure_key(&ApiKey {
                id: id.into(),
                key: key.into(),
                user: user.into(),
                scopes: Some(vec![scope.to_owned()]),
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

#[tokio::test]
async fn the_manifest_names_alrust_and_its_build() {
    let Some((_db, app)) = app().await else {
        return;
    };
    let (code, _, out) = send(&app, "GET", "/management/manifest", READER_KEY, None).await;
    assert_eq!(code, StatusCode::OK, "{out}");
    let mut keys: Vec<&String> = out.as_object().unwrap().keys().collect();
    keys.sort();
    assert_eq!(keys, ["build", "date", "release", "revision"]);
    assert_eq!(out["release"], env!("CARGO_PKG_VERSION"));
    assert_eq!(
        out["build"],
        option_env!("ALRUST_BUILD_NUMBER").unwrap_or("DEV")
    );
    assert_eq!(
        out["revision"],
        option_env!("ALRUST_VCS_REVISION").unwrap_or("HEAD")
    );
    let date = out["date"].as_str().unwrap();
    assert!(
        chrono::NaiveDateTime::parse_from_str(date, "%Y-%m-%dT%H:%M:%SZ").is_ok(),
        "{date}"
    );
    let (code, _, _) = send(&app, "GET", "/management/manifest", ALERTS_KEY, None).await;
    assert_eq!(code, StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn the_status_counts_alerts_and_reports_the_switches() {
    let Some((_db, app)) = app().await else {
        return;
    };
    for resource in ["m1", "m2"] {
        let (code, _, out) = send(
            &app,
            "POST",
            "/alert",
            ADMIN_KEY,
            Some(
                json!({"resource": resource, "event": "E", "environment": "Production",
                        "service": ["svc"], "severity": "major"}),
            ),
        )
        .await;
        assert_eq!(code, StatusCode::CREATED, "{out}");
    }
    let (code, _, out) = send(&app, "GET", "/management/status", READER_KEY, None).await;
    assert_eq!(code, StatusCode::OK, "{out}");
    assert_eq!(out["application"], "alrust");
    assert_eq!(out["version"], env!("CARGO_PKG_VERSION"));
    let time = out["time"].as_i64().unwrap();
    let uptime = out["uptime"].as_i64().unwrap();
    assert!(uptime >= 0 && time >= uptime, "{out}");
    assert!(
        (Utc::now().timestamp_millis() - time).abs() < 60_000,
        "{out}"
    );
    let metrics = out["metrics"].as_array().unwrap();
    assert_eq!(metrics.len(), 3, "{out}");
    assert_eq!(
        metrics[0],
        json!({"group": "alerts", "name": "total", "title": "Total alerts",
               "description": "Total number of alerts in the database", "type": "gauge", "value": 2})
    );
    let switches: Vec<(Value, Value)> = metrics[1..]
        .iter()
        .map(|m| (m["name"].clone(), m["value"].clone()))
        .collect();
    assert_eq!(
        switches,
        vec![
            (json!("auto-refresh-allow"), json!("ON")),
            (json!("sender-api-allow"), json!("ON"))
        ]
    );
    let (code, _, _) = send(&app, "GET", "/management/status", ALERTS_KEY, None).await;
    assert_eq!(code, StatusCode::FORBIDDEN);
}
