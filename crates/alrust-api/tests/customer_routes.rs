//! Customer mappings through the router, against PostgreSQL: `POST
//! /customer`, `GET`/`PUT`/`DELETE /customer/{id}`, `GET /customers`
//! (reference `views/customers.py`; SF-CUSTOMERS with `CUSTOMER_VIEWS` off).
//!
//! Each test creates its own database `<prefix>c<n>_<µs>` and drops it when
//! it ends, also on panic. Without `DATABASE_URL` the tests are skipped,
//! except under CI (`CI` set), where a missing database is a failure. Keys
//! and mappings are synthetic.
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
        "{prefix}c{}_{}",
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
const CUST_ADMIN_KEY: &str = "synthetic-custadmin-key-00000000000000000";
const ACME_READER_KEY: &str = "synthetic-acmereader-key-0000000000000000";
const READER_KEY: &str = "synthetic-reader-key-00000000000000000000";

async fn app() -> Option<(TestDb, axum::Router)> {
    let db = test_db().await?;
    let store = Store::new(db.pool.clone(), StoreConfig::default());
    store.prepare().await.unwrap();
    let keys: [(&str, &str, &str, &str, Option<&str>); 4] = [
        ("k-admin", ADMIN_KEY, "admin", "admin", None),
        (
            "k-cust",
            CUST_ADMIN_KEY,
            "cust-admin",
            "admin:customers",
            Some("acme"),
        ),
        (
            "k-acme",
            ACME_READER_KEY,
            "acme-reader",
            "read",
            Some("acme"),
        ),
        ("k-reader", READER_KEY, "reader", "read", None),
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

async fn mapping(app: &axum::Router, pattern: &str, customer: Option<&str>) -> String {
    let (code, _, out) = send(
        app,
        "POST",
        "/customer",
        ADMIN_KEY,
        Some(json!({"match": pattern, "customer": customer})),
    )
    .await;
    assert_eq!(code, StatusCode::CREATED, "{out}");
    out["id"].as_str().unwrap().to_owned()
}

#[tokio::test]
async fn a_mapping_is_created() {
    let Some((_db, app)) = app().await else {
        return;
    };
    let (code, location, out) = send(
        &app,
        "POST",
        "/customer",
        ADMIN_KEY,
        Some(json!({"match": "ops@example.test", "customer": "acme"})),
    )
    .await;
    assert_eq!(code, StatusCode::CREATED, "{out}");
    assert_eq!(location, None, "no Location header, as in the reference");
    let id = out["id"].as_str().unwrap();
    assert_eq!(
        out["customer"],
        json!({"id": id, "href": format!("http://localhost:8080/customer/{id}"),
               "match": "ops@example.test", "customer": "acme"})
    );
    // SF-CUSTOMERS with CUSTOMER_VIEWS off: a customer-scoped caller maps
    // to any name, `*` included.
    for customer in ["other", "*"] {
        let (code, _, out) = send(
            &app,
            "POST",
            "/customer",
            CUST_ADMIN_KEY,
            Some(json!({"match": "m", "customer": customer})),
        )
        .await;
        assert_eq!(code, StatusCode::CREATED, "{customer}: {out}");
    }
    for (body, message) in [
        (json!({"customer": "acme"}), "match is required"),
        (json!({"match": 5}), "match must be a string"),
        (json!([]), "customer must be a JSON object"),
    ] {
        let (code, _, out) = send(&app, "POST", "/customer", ADMIN_KEY, Some(body.clone())).await;
        assert_eq!(
            (code, out["message"].clone()),
            (StatusCode::BAD_REQUEST, json!(message)),
            "{body}"
        );
    }
    let (code, _, _) = send(
        &app,
        "POST",
        "/customer",
        READER_KEY,
        Some(json!({"match": "m"})),
    )
    .await;
    assert_eq!(code, StatusCode::FORBIDDEN);
}

/// `admin`, `admin:customers`, or the caller's own customer.
#[tokio::test]
async fn a_mapping_is_visible_to_admins_and_its_own_customer() {
    let Some((_db, app)) = app().await else {
        return;
    };
    let acme = mapping(&app, "acme-ops", Some("acme")).await;
    let none = mapping(&app, "nobody", None).await;
    for (id, key, visible) in [
        (&acme, ADMIN_KEY, true),
        (&acme, CUST_ADMIN_KEY, true),
        (&acme, ACME_READER_KEY, true),
        (&acme, READER_KEY, false),
        (&none, ADMIN_KEY, true),
        (&none, CUST_ADMIN_KEY, true),
        (&none, ACME_READER_KEY, false),
        (&none, READER_KEY, false),
    ] {
        let (code, _, out) = send(&app, "GET", &format!("/customer/{id}"), key, None).await;
        let want = if visible {
            StatusCode::OK
        } else {
            StatusCode::NOT_FOUND
        };
        assert_eq!(code, want, "{id} {key}: {out}");
    }
    // The reference answers 500 for a missing id; alrust 404.
    let (code, _, out) = send(&app, "GET", "/customer/nosuch", ADMIN_KEY, None).await;
    assert_eq!(
        (code, out["message"].clone()),
        (StatusCode::NOT_FOUND, json!("not found"))
    );
}

#[tokio::test]
async fn the_list_counts_before_it_filters_visibility() {
    let Some((_db, app)) = app().await else {
        return;
    };
    mapping(&app, "ops", Some("acme")).await;
    mapping(&app, "dev", Some("other")).await;
    mapping(&app, "misc", None).await;
    let (code, _, out) = send(&app, "GET", "/customers", ADMIN_KEY, None).await;
    assert_eq!(
        (code, out["total"].clone()),
        (StatusCode::OK, json!(3)),
        "{out}"
    );
    let (_, _, out) = send(&app, "GET", "/customers", ACME_READER_KEY, None).await;
    assert_eq!(out["total"], 3, "the count is before the visibility filter");
    assert_eq!(out["customers"].as_array().unwrap().len(), 1);
    let (_, _, out) = send(&app, "GET", "/customers", READER_KEY, None).await;
    assert_eq!(
        (
            out["message"].clone(),
            out["total"].clone(),
            out["pages"].clone()
        ),
        (json!("not found"), json!(0), json!(1))
    );
    for (uri, total) in [
        ("/customers?match=ops", 1),
        ("/customers?customer!=acme", 1),
        ("/customers?match=~OP", 1),
        ("/customers?customer=acme&customer=other", 2),
    ] {
        let (code, _, out) = send(&app, "GET", uri, ADMIN_KEY, None).await;
        assert_eq!(
            (code, out["total"].clone()),
            (StatusCode::OK, json!(total)),
            "{uri}: {out}"
        );
    }
    let (_, _, out) = send(&app, "GET", "/customers?sort-by=-match", ADMIN_KEY, None).await;
    let names: Vec<Value> = out["customers"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c["match"].clone())
        .collect();
    assert_eq!(names, vec![json!("ops"), json!("misc"), json!("dev")]);
    for (uri, code, message) in [
        (
            "/customers?bogus=1",
            StatusCode::BAD_REQUEST,
            "Invalid filter parameter: bogus",
        ),
        (
            "/customers?sort-by=id",
            StatusCode::BAD_REQUEST,
            "Sorting by 'id' field not supported.",
        ),
        (
            "/customers?page-size=1&page=4",
            StatusCode::RANGE_NOT_SATISFIABLE,
            "page out of range: 1-3",
        ),
    ] {
        let (got, _, out) = send(&app, "GET", uri, ADMIN_KEY, None).await;
        assert_eq!(
            (got, out["message"].clone()),
            (code, json!(message)),
            "{uri}"
        );
    }
}

#[tokio::test]
async fn a_mapping_is_updated_and_deleted() {
    let Some((_db, app)) = app().await else {
        return;
    };
    let id = mapping(&app, "ops", Some("acme")).await;
    let put = |body: Value| {
        let app = app.clone();
        let uri = format!("/customer/{id}");
        async move { send(&app, "PUT", &uri, CUST_ADMIN_KEY, Some(body)).await }
    };
    let (code, _, out) = put(json!({"customer": "other"})).await;
    assert_eq!(
        (code, out["customer"]["customer"].clone()),
        (StatusCode::OK, json!("other"))
    );
    let (_, _, out) = put(json!({"match": "new"})).await;
    assert_eq!(out["customer"]["match"], "new");
    let (_, _, out) = put(json!({"customer": null})).await;
    assert_eq!(out["customer"]["customer"], Value::Null);
    // Only unknown keys: the reference's `SET id = id`, nothing changes.
    let (code, _, out) = put(json!({"foo": 1})).await;
    assert_eq!(
        (code, out["customer"]["match"].clone()),
        (StatusCode::OK, json!("new"))
    );
    for (body, message) in [
        (json!({}), "nothing to change"),
        (json!({"id": "x"}), "id cannot be changed"),
        (json!({"match": null}), "match is required"),
    ] {
        let (code, _, out) = put(body.clone()).await;
        assert_eq!(
            (code, out["message"].clone()),
            (StatusCode::BAD_REQUEST, json!(message)),
            "{body}"
        );
    }
    let (code, _, _) = send(
        &app,
        "PUT",
        "/customer/nosuch",
        ADMIN_KEY,
        Some(json!({"match": "x"})),
    )
    .await;
    assert_eq!(code, StatusCode::NOT_FOUND);
    let (code, _, _) = send(
        &app,
        "PUT",
        &format!("/customer/{id}"),
        READER_KEY,
        Some(json!({"match": "x"})),
    )
    .await;
    assert_eq!(code, StatusCode::FORBIDDEN);
    let (code, _, out) = send(&app, "DELETE", &format!("/customer/{id}"), ADMIN_KEY, None).await;
    assert_eq!((code, out), (StatusCode::OK, json!({"status": "ok"})));
    let (code, _, _) = send(&app, "DELETE", &format!("/customer/{id}"), ADMIN_KEY, None).await;
    assert_eq!(code, StatusCode::NOT_FOUND);
}
