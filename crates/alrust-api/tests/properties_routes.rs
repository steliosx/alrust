//! `GET /management/properties` through the router, against PostgreSQL
//! (`spec/security-fixes.md` SF-MGMT vectors 1, 8, 9 and 10): the scope,
//! the credential rules, the content type, and the page passed through
//! unchanged. What the page holds is tested where it is made
//! (`properties.rs`, and the server's `properties_page.rs`).
//!
//! Each test creates its own database `<prefix>p<n>_<µs>` and drops it when
//! it ends, also on panic. Without `DATABASE_URL` the tests are skipped,
//! except under CI (`CI` set), where a missing database is a failure. Keys
//! are synthetic.
#![expect(
    clippy::unwrap_used,
    clippy::print_stderr,
    reason = "integration test: helpers unwrap, skips are printed, and each scenario stays in one function"
)]

use std::sync::Arc;
use std::sync::atomic::{AtomicU32, Ordering};

use alrust_api::properties::{Setting, SettingValue, render};
use alrust_api::{ApiConfig, AppState, router};
use alrust_domain::ApiKey;
use alrust_store_pg::{Store, StoreConfig};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use chrono::Utc;
use sqlx::Executor as _;
use sqlx::postgres::{PgConnectOptions, PgPool, PgPoolOptions};
use tower::ServiceExt as _;

static COUNTER: AtomicU32 = AtomicU32::new(0);

const M1_KEY: &str = "synthetic-m1-key-000000000000000000000000";
const M2_KEY: &str = "synthetic-m2-key-000000000000000000000000";
const SENTINEL: &str = "SENTINEL-route-secret-0001";

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
        "{prefix}p{}_{}",
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

fn page() -> String {
    render(&[
        Setting::secret("SECRET_KEY", vec![SENTINEL.to_owned()]),
        Setting::public("AUTH_REQUIRED", SettingValue::Bool(true)),
        Setting::public("ALERT_TIMEOUT", SettingValue::Int(86_400)),
    ])
}

#[derive(Debug, Clone, Copy)]
enum AuthRequired {
    Yes,
    No,
}

async fn app(db: &TestDb, auth_required: AuthRequired) -> axum::Router {
    let store = Store::new(db.pool.clone(), StoreConfig::default());
    store.prepare().await.unwrap();
    for (id, key, user, scope) in [
        ("k-m1", M1_KEY, "m1", "admin:management"),
        ("k-m2", M2_KEY, "m2", "read"),
    ] {
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
                customer: None,
            })
            .await
            .unwrap();
    }
    router(Arc::new(AppState {
        store,
        cfg: ApiConfig {
            auth_required: matches!(auth_required, AuthRequired::Yes),
            properties: page(),
            ..ApiConfig::default()
        },
    }))
}

/// Status, `Content-Type` and body text.
async fn get(app: &axum::Router, key: Option<&str>) -> (StatusCode, String, String) {
    let mut req = Request::builder()
        .method("GET")
        .uri("/management/properties");
    if let Some(k) = key {
        req = req.header("Authorization", format!("Key {k}"));
    }
    let resp = app
        .clone()
        .oneshot(req.body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = resp.status();
    let content_type = resp
        .headers()
        .get("content-type")
        .map_or_else(String::new, |v| v.to_str().unwrap().to_owned());
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    (
        status,
        content_type,
        String::from_utf8(bytes.to_vec()).unwrap(),
    )
}

/// Vectors 1, 8 and 9: `admin:management` reads the page as rendered,
/// `text/plain`, with no secret and not the caller's own key; `read` is
/// refused with the reference's 403; no credential is 401.
#[tokio::test]
async fn the_page_needs_admin_management() {
    let Some(db) = test_db().await else {
        return;
    };
    let app = app(&db, AuthRequired::Yes).await;
    let (status, content_type, body) = get(&app, Some(M1_KEY)).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(content_type, "text/plain");
    assert_eq!(body, page());
    assert_eq!(
        body,
        "ALERT_TIMEOUT: 86400\nAUTH_REQUIRED: true\nSECRET_KEY: [REDACTED]\n"
    );
    assert!(!body.contains(SENTINEL) && !body.contains(M1_KEY));
    let (status, _, body) = get(&app, Some(M2_KEY)).await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    let body: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(
        body["message"],
        serde_json::json!("Missing required scope: admin:management")
    );
    let (status, _, _) = get(&app, None).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
}

/// Vector 10: with `AUTH_REQUIRED` off, a caller with no credential reads
/// the same redacted page.
#[tokio::test]
async fn auth_off_shows_the_same_redacted_page() {
    let Some(db) = test_db().await else {
        return;
    };
    let app = app(&db, AuthRequired::No).await;
    let (status, content_type, body) = get(&app, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(content_type, "text/plain");
    assert_eq!(body, page());
}
