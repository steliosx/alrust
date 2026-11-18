//! The bulk alert routes through the router, against PostgreSQL (reference
//! `views/bulk.py`; `spec/security-fixes.md` SF-BULK): status, tag, untag
//! and attributes on the two-customer store `BK` of the SF-BULK vectors.
//!
//! Each test creates its own database `<prefix>k<n>_<µs>` and drops it when
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

const ADMIN: &str = "synthetic-admin-key-000000000000000000000";
/// `KW` of the vectors: `write:alerts`, no customer.
const KW: &str = "synthetic-bulk-kw-key-0000000000000000000";
/// `KA`: `write:alerts`, customer `A`.
const KA: &str = "synthetic-bulk-ka-key-0000000000000000000";
const READ: &str = "synthetic-bulk-read-key-00000000000000000";

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
        "{prefix}k{}_{}",
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

/// The store `BK`: `b1` (`major`) and `b2` (`critical`) of customer `A`,
/// `b3` (`critical`) of customer `B`, all `open` in `Production`. Returns
/// the router and the three alert ids.
async fn setup() -> Option<(TestDb, axum::Router, [String; 3])> {
    let db = test_db().await?;
    let store = Store::new(db.pool.clone(), StoreConfig::default());
    store.prepare().await.unwrap();
    for (id, key, scopes, customer) in [
        ("k-admin", ADMIN, vec!["admin"], None),
        ("k-kw", KW, vec!["write:alerts"], None),
        ("k-ka", KA, vec!["write:alerts"], Some("A")),
        ("k-read", READ, vec!["read"], None),
    ] {
        store
            .ensure_key(&ApiKey {
                id: id.into(),
                key: key.into(),
                user: id.into(),
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
    let mut ids = Vec::new();
    for (resource, severity, customer) in [
        ("r1", "major", "A"),
        ("r2", "critical", "A"),
        ("r3", "critical", "B"),
    ] {
        let body = json!({
            "resource": resource, "event": "HttpError", "environment": "Production",
            "service": ["Web"], "severity": severity, "customer": customer,
        });
        let (status, answer) = call(&app, "POST", "/alert", ADMIN, Some(body)).await;
        assert_eq!(status, StatusCode::CREATED, "{answer}");
        ids.push(answer["id"].as_str().unwrap().to_owned());
    }
    Some((db, app, ids.try_into().unwrap()))
}

async fn call(
    app: &axum::Router,
    method: &str,
    uri: &str,
    key: &str,
    body: Option<Value>,
) -> (StatusCode, Value) {
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
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

/// One alert as `GET /alert/{id}` shows it to the admin key.
async fn alert(app: &axum::Router, id: &str) -> Value {
    let (status, body) = call(app, "GET", &format!("/alert/{id}"), ADMIN, None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    body["alert"].clone()
}

/// A JSON list of ids, sorted.
fn set(list: &Value) -> Value {
    let mut ids = list.as_array().cloned().unwrap_or_default();
    ids.sort_by_key(ToString::to_string);
    json!(ids)
}

fn sorted(ids: &[&String]) -> Value {
    let mut v: Vec<&String> = ids.to_vec();
    v.sort();
    json!(v)
}

/// SF-BULK vectors 1, 4, 6 and 7.
#[tokio::test]
async fn sf_bulk_vectors() {
    let Some((_db, app, [b1, b2, b3])) = setup().await else {
        return;
    };
    // 1: KA tags its own two alerts only.
    let (status, body) = call(
        &app,
        "PUT",
        "/_bulk/alerts/tag?environment=Production",
        KA,
        Some(json!({"tags": ["t"]})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        (body["updated"].clone(), body["count"].clone()),
        (sorted(&[&b1, &b2]), json!(2))
    );
    assert_eq!(alert(&app, &b3).await["tags"], json!([]));
    // 4: KW sets one alert's status.
    let (status, body) = call(
        &app,
        "PUT",
        &format!("/_bulk/alerts/status?id={b1}"),
        KW,
        Some(json!({"status": "ack"})),
    )
    .await;
    assert_eq!(
        (status, body["updated"].clone()),
        (StatusCode::OK, json!([b1]))
    );
    let a1 = alert(&app, &b1).await;
    assert_eq!(a1["status"], json!("ack"));
    assert!(
        a1["history"]
            .as_array()
            .unwrap()
            .iter()
            .any(|h| h["text"] == json!("bulk status update") && h["status"] == json!("ack")),
        "{a1}"
    );
    // 6: no tags.
    let (status, body) = call(
        &app,
        "PUT",
        &format!("/_bulk/alerts/tag?id={b1}"),
        KW,
        Some(json!({})),
    )
    .await;
    assert_eq!(
        (status, body["message"].clone()),
        (
            StatusCode::BAD_REQUEST,
            json!("must supply 'tags' as json list")
        )
    );
    // 7: attributes merge.
    let (status, body) = call(
        &app,
        "PUT",
        &format!("/_bulk/alerts/attributes?id={b1}"),
        KW,
        Some(json!({"attributes": {"k": "v"}})),
    )
    .await;
    assert_eq!(
        (status, body["updated"].clone()),
        (StatusCode::OK, json!([b1]))
    );
    assert_eq!(alert(&app, &b1).await["attributes"], json!({"k": "v"}));
}

/// The customer restriction on every route: a customer-limited key never
/// selects another customer's alert.
#[tokio::test]
async fn customer_restriction() {
    let Some((_db, app, [b1, b2, b3])) = setup().await else {
        return;
    };
    let (status, body) = call(
        &app,
        "PUT",
        "/_bulk/alerts/status?status=open",
        KA,
        Some(json!({"status": "closed"})),
    )
    .await;
    // The status route answers in its processing order, newest first.
    assert_eq!(
        (status, set(&body["updated"])),
        (StatusCode::OK, sorted(&[&b1, &b2]))
    );
    assert_eq!(alert(&app, &b3).await["status"], json!("open"));
    let (status, body) = call(
        &app,
        "PUT",
        &format!("/_bulk/alerts/status?id={b3}"),
        KA,
        Some(json!({"status": "ack"})),
    )
    .await;
    assert_eq!(
        (status, body["message"].clone()),
        (StatusCode::NOT_FOUND, json!("not found"))
    );
    for (uri, body) in [
        (
            format!("/_bulk/alerts/untag?id={b3}"),
            json!({"tags": ["t"]}),
        ),
        (
            format!("/_bulk/alerts/attributes?id={b3}"),
            json!({"attributes": {"k": "v"}}),
        ),
        (format!("/_bulk/alerts/tag?id={b3}"), json!({"tags": ["x"]})),
    ] {
        let (status, answer) = call(&app, "PUT", &uri, KA, Some(body)).await;
        assert_eq!(
            (status, answer["updated"].clone(), answer["count"].clone()),
            (StatusCode::OK, json!([]), json!(0)),
            "{uri}"
        );
    }
    let a3 = alert(&app, &b3).await;
    assert_eq!(
        (a3["tags"].clone(), a3["attributes"].clone()),
        (json!([]), json!({}))
    );
}

/// The single routes' edits, in bulk: first-seen tag order, an empty list
/// after untagging every tag, and null removes an attribute.
#[tokio::test]
async fn edits_and_checks() {
    let Some((_db, app, [b1, b2, b3])) = setup().await else {
        return;
    };
    let (_, body) = call(
        &app,
        "PUT",
        "/_bulk/alerts/tag?severity=critical",
        KW,
        Some(json!({"tags": ["z", "a", "z"]})),
    )
    .await;
    assert_eq!(body["updated"], sorted(&[&b2, &b3]));
    let (_, body) = call(
        &app,
        "PUT",
        &format!("/_bulk/alerts/tag?id={b2}"),
        KW,
        Some(json!({"tags": ["m", "a"]})),
    )
    .await;
    assert_eq!(body["count"], json!(1));
    assert_eq!(alert(&app, &b2).await["tags"], json!(["z", "a", "m"]));
    let (_, body) = call(
        &app,
        "PUT",
        "/_bulk/alerts/untag?environment=Production",
        KW,
        Some(json!({"tags": ["z", "a", "m"]})),
    )
    .await;
    assert_eq!(body["updated"], sorted(&[&b1, &b2, &b3]));
    assert_eq!(alert(&app, &b2).await["tags"], json!([]));
    for attributes in [
        json!({"k": "v", "j": 1}),
        json!({"k": null, "n": {"x": null}}),
    ] {
        let (status, _) = call(
            &app,
            "PUT",
            &format!("/_bulk/alerts/attributes?id={b1}"),
            KW,
            Some(json!({"attributes": attributes})),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
    }
    assert_eq!(
        alert(&app, &b1).await["attributes"],
        json!({"j": 1, "n": {"x": null}})
    );
}

/// The checks, and the parameters the bulk routes do not read.
#[tokio::test]
async fn checks_and_unread_parameters() {
    let Some((_db, app, [b1, _, b3])) = setup().await else {
        return;
    };
    // page, page-size, sort-by, group-by and show-history are not read.
    let (status, body) = call(
        &app,
        "PUT",
        &format!("/_bulk/alerts/tag?id={b1}&page=x&page-size=0&sort-by=nonsense&group-by=x"),
        KW,
        Some(json!({"tags": ["p"]})),
    )
    .await;
    assert_eq!(
        (status, body["updated"].clone()),
        (StatusCode::OK, json!([b1]))
    );
    for (uri, key, body, status, message) in [
        (
            "/_bulk/alerts/status",
            KW,
            json!({"text": "x"}),
            400,
            "must supply 'status' as json data",
        ),
        (
            "/_bulk/alerts/status?status=nosuch",
            KW,
            json!({"status": "ack"}),
            404,
            "not found",
        ),
        (
            "/_bulk/alerts/untag",
            KW,
            json!({"tags": []}),
            400,
            "must supply 'tags' as json list",
        ),
        (
            "/_bulk/alerts/tag",
            KW,
            json!({"tags": ["a", 5]}),
            400,
            "must supply 'tags' as json list",
        ),
        (
            "/_bulk/alerts/attributes",
            KW,
            json!({"attributes": {}}),
            400,
            "must supply 'attributes' as json data",
        ),
        (
            "/_bulk/alerts/tag",
            READ,
            json!({"tags": ["a"]}),
            403,
            "Missing required scope: write:alerts",
        ),
        (
            "/_bulk/alerts/tag?sort-by=attributes.zabbix_ack_sync_intent",
            KW,
            json!({"tags": ["a"]}),
            400,
            "Query selector is not permitted on restricted attributes",
        ),
    ] {
        let (got, answer) = call(&app, "PUT", uri, key, Some(body)).await;
        assert_eq!(
            (got.as_u16(), answer["message"].clone()),
            (status, json!(message)),
            "{uri}"
        );
    }
    // Nothing above changed b3's status.
    assert_eq!(alert(&app, &b3).await["status"], json!("open"));
}
