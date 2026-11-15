//! Permissions through the router, against PostgreSQL (reference
//! `views/permissions.py`; `spec/security-fixes.md` SF-PERMS): the vector
//! table, the built-in roles on every page, the filters, a NULL `scopes`
//! row, the role lookup, and the ceiling with authentication off.
//!
//! Each test creates its own database `<prefix>e<n>_<µs>` and drops it when
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
const PERMS: &str = "synthetic-perms-key-000000000000000000000";
const PERMS_READ: &str = "synthetic-permsread-key-0000000000000000";
const PERMS_WRITE_ALERTS: &str = "synthetic-permswa-key-00000000000000000000";

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
        "{prefix}e{}_{}",
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

#[derive(Debug, Clone, Copy)]
enum AuthRequired {
    Yes,
    No,
}

async fn setup(auth: AuthRequired) -> Option<(TestDb, Store, axum::Router)> {
    let db = test_db().await?;
    let store = Store::new(db.pool.clone(), StoreConfig::default());
    store.prepare().await.unwrap();
    for (id, key, scopes) in [
        ("k-admin", ADMIN, vec!["admin"]),
        ("k-perms", PERMS, vec!["admin:perms"]),
        ("k-perms-read", PERMS_READ, vec!["admin:perms", "read"]),
        (
            "k-perms-wa",
            PERMS_WRITE_ALERTS,
            vec!["admin:perms", "write:alerts"],
        ),
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
                customer: None,
            })
            .await
            .unwrap();
    }
    let app = router(Arc::new(AppState {
        store: store.clone(),
        cfg: ApiConfig {
            auth_required: matches!(auth, AuthRequired::Yes),
            ..ApiConfig::default()
        },
    }));
    Some((db, store, app))
}

async fn call(
    app: &axum::Router,
    method: &str,
    uri: &str,
    key: Option<&str>,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let mut req = Request::builder().method(method).uri(uri);
    if let Some(k) = key {
        req = req.header("Authorization", format!("Key {k}"));
    }
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

/// Creates a row with the id `p-<role>` as the `admin` key; its id.
async fn row(app: &axum::Router, role: &str, scopes: &[&str]) -> String {
    let (status, body) = call(
        app,
        "POST",
        "/perm",
        Some(ADMIN),
        Some(json!({"id": format!("p-{role}"), "match": role, "scopes": scopes})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    body["id"].as_str().unwrap().to_owned()
}

fn ceiling(want: &str, have: &str) -> Value {
    json!(format!(
        "Requested scope '{want}' not in existing scopes: {have}"
    ))
}

/// SF-PERMS vectors: method, path (`{ops}` and `{root}` are row ids),
/// key, body, status, message.
const VECTORS: [(&str, &str, &str, &str, u16, &str); 22] = [
    // PUT by a caller with only admin:perms.
    (
        "PUT",
        "/perm/{ops}",
        PERMS,
        r#"{"scopes": ["admin"]}"#,
        403,
        "Requested scope 'admin' not in existing scopes: admin:perms",
    ),
    (
        "PUT",
        "/perm/{ops}",
        PERMS,
        r#"{"match": "user", "scopes": []}"#,
        409,
        "user role already exists",
    ),
    (
        "PUT",
        "/perm/{root}",
        PERMS,
        r#"{"match": "root2"}"#,
        403,
        "Requested scope 'admin' not in existing scopes: admin:perms",
    ),
    (
        "PUT",
        "/perm/{ops}",
        PERMS,
        r#"{"scopes": "admin"}"#,
        400,
        "scopes must be a list",
    ),
    (
        "PUT",
        "/perm/{ops}",
        PERMS,
        r#"{"scopes": null}"#,
        400,
        "scopes must be a list",
    ),
    (
        "PUT",
        "/perm/{ops}",
        PERMS,
        r#"{"match": null}"#,
        400,
        "match must be a non-empty string",
    ),
    (
        "PUT",
        "/perm/{ops}",
        PERMS,
        r#"{"match": ""}"#,
        400,
        "match must be a non-empty string",
    ),
    (
        "PUT",
        "/perm/{ops}",
        PERMS,
        r#"{"scopes": ["read:nothing"]}"#,
        400,
        "'read:nothing' is not a valid Scope",
    ),
    ("PUT", "/perm/{ops}", PERMS, "{}", 400, "nothing to change"),
    (
        "PUT",
        "/perm/missing",
        PERMS,
        r#"{"scopes": []}"#,
        404,
        "not found",
    ),
    // POST.
    (
        "POST",
        "/perm",
        PERMS,
        r#"{"match": "a1", "scopes": [""]}"#,
        400,
        "'' is not a valid Scope",
    ),
    (
        "POST",
        "/perm",
        PERMS_READ,
        r#"{"match": "a2", "scopes": ["read:synthetic_unknown"]}"#,
        400,
        "'read:synthetic_unknown' is not a valid Scope",
    ),
    (
        "POST",
        "/perm",
        PERMS,
        r#"{"match": "a3", "scopes": ["read:synthetic_unknown"]}"#,
        400,
        "'read:synthetic_unknown' is not a valid Scope",
    ),
    (
        "POST",
        "/perm",
        PERMS,
        r#"{"match": "a4", "scopes": ["read:alerts"]}"#,
        403,
        "Requested scope 'read:alerts' not in existing scopes: admin:perms",
    ),
    (
        "POST",
        "/perm",
        ADMIN,
        r#"{"match": "user", "scopes": ["read"]}"#,
        409,
        "user role already exists",
    ),
    (
        "POST",
        "/perm",
        ADMIN,
        r#"{"match": "user", "scopes": [""]}"#,
        400,
        "'' is not a valid Scope",
    ),
    (
        "POST",
        "/perm",
        ADMIN,
        r#"{"match": "a5", "scopes": null}"#,
        400,
        "scopes must be a list",
    ),
    (
        "POST",
        "/perm",
        ADMIN,
        r#"{"match": "a6", "scopes": "admin"}"#,
        400,
        "scopes must be a list",
    ),
    (
        "POST",
        "/perm",
        ADMIN,
        r#"{"match": "a7", "scopes": [5]}"#,
        400,
        "'5' is not a valid Scope",
    ),
    (
        "POST",
        "/perm",
        ADMIN,
        r#"["not", "an", "object"]"#,
        400,
        "permission must be a JSON object",
    ),
    (
        "POST",
        "/perm",
        ADMIN,
        r#"{"match": 5}"#,
        400,
        "match must be a string",
    ),
    // A restrictive divergence: write:alerts does not grant delete:alerts here.
    (
        "POST",
        "/perm",
        PERMS_WRITE_ALERTS,
        r#"{"match": "a8", "scopes": ["delete:alerts"]}"#,
        403,
        "Requested scope 'delete:alerts' not in existing scopes: admin:perms,write:alerts",
    ),
];

/// SF-PERMS vectors.
#[tokio::test]
async fn sf_perms_vectors() {
    let Some((_db, _store, app)) = setup(AuthRequired::Yes).await else {
        return;
    };
    let ops = row(&app, "ops", &["read:alerts"]).await;
    let root = row(&app, "root", &["admin"]).await;
    let put = |id: &str| format!("/perm/{id}");
    for (method, uri, key, body, status, message) in VECTORS {
        let uri = uri.replace("{ops}", &ops).replace("{root}", &root);
        let body: Value = serde_json::from_str(body).unwrap();
        let (got, answer) = call(&app, method, &uri, Some(key), Some(body)).await;
        assert_eq!(got.as_u16(), status, "{method} {uri} {answer}");
        assert_eq!(answer["message"], json!(message), "{method} {uri}");
    }
    // Nothing above changed a row.
    let (_, ops_now) = call(&app, "GET", &put(&ops), Some(ADMIN), None).await;
    assert_eq!(ops_now["permission"]["match"], json!("ops"));
    assert_eq!(ops_now["permission"]["scopes"], json!(["read:alerts"]));
    assert_eq!(ops_now["total"], json!(1));
    // Within the ceiling: renaming with scopes [] and setting known scopes
    // the caller holds.
    let (status, body) = call(
        &app,
        "PUT",
        &put(&root),
        Some(PERMS),
        Some(json!({"match": "root2", "scopes": []})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["permission"]["scopes"], json!([]));
    let (status, body) = call(
        &app,
        "PUT",
        &put(&ops),
        Some(PERMS),
        Some(json!({"scopes": ["read:perms"]})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["permission"]["scopes"], json!(["read:perms"]));
    // A caller-supplied id, the reference's success shape, and the deletes.
    let (status, body) = call(
        &app,
        "POST",
        "/perm",
        Some(ADMIN),
        Some(json!({"id": "perm-x", "match": "x", "scopes": ["read"]})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    assert_eq!(body["id"], json!("perm-x"));
    assert_eq!(
        body["permission"]["href"],
        json!("http://localhost:8080/perm/perm-x")
    );
    let (status, _) = call(&app, "DELETE", "/perm/perm-x", Some(PERMS), None).await;
    assert_eq!(status, StatusCode::OK);
    let (status, body) = call(&app, "DELETE", "/perm/perm-x", Some(PERMS), None).await;
    assert_eq!(
        (status, body["message"].clone()),
        (StatusCode::NOT_FOUND, json!("not found"))
    );
    let (status, _) = call(&app, "GET", "/perm/perm-x", Some(ADMIN), None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

/// The built-in roles come first on every page and count in `total`; the
/// rows are paged by themselves.
#[tokio::test]
async fn built_in_roles_on_every_page() {
    let Some((_db, _store, app)) = setup(AuthRequired::Yes).await else {
        return;
    };
    row(&app, "ops", &["read:alerts"]).await;
    row(&app, "dev", &["read"]).await;
    let names = |body: &Value| -> Vec<String> {
        body["permissions"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| p["match"].as_str().unwrap().to_owned())
            .collect()
    };
    let (status, body) = call(&app, "GET", "/perms?page-size=1", Some(ADMIN), None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        (body["total"].clone(), body["pages"].clone()),
        (json!(5), json!(5))
    );
    assert_eq!(names(&body), ["admin", "user", "guest", "dev"]);
    assert_eq!(
        body["permissions"][0]["scopes"],
        json!(["admin", "read", "write"])
    );
    let (_, body) = call(&app, "GET", "/perms?page-size=1&page=4", Some(ADMIN), None).await;
    assert_eq!(names(&body), ["admin", "user", "guest"]);
    let (status, body) = call(&app, "GET", "/perms?page-size=1&page=6", Some(ADMIN), None).await;
    assert_eq!(status, StatusCode::RANGE_NOT_SATISFIABLE);
    assert_eq!(body["message"], json!("page out of range: 1-5"));
    // Filters and order.
    let (_, body) = call(&app, "GET", "/perms?scope=read:alerts", Some(ADMIN), None).await;
    assert_eq!(names(&body), ["admin", "user", "guest", "ops"]);
    let (_, body) = call(&app, "GET", "/perms?match=~o", Some(ADMIN), None).await;
    assert_eq!(names(&body), ["admin", "user", "guest", "ops"]);
    let (_, body) = call(&app, "GET", "/perms?sort-by=-match", Some(ADMIN), None).await;
    assert_eq!(names(&body), ["admin", "user", "guest", "ops", "dev"]);
    for (uri, message) in [
        ("/perms?scopes=read", "Invalid filter parameter: scopes"),
        ("/perms?bogus=1", "Invalid filter parameter: bogus"),
        (
            "/perms?sort-by=bogus",
            "Sorting by 'bogus' field not supported.",
        ),
        ("/perms?page=x", "page must be an integer"),
    ] {
        let (status, body) = call(&app, "GET", uri, Some(ADMIN), None).await;
        assert_eq!(
            (status, body["message"].clone()),
            (StatusCode::BAD_REQUEST, json!(message)),
            "{uri}"
        );
    }
    let (status, body) = call(&app, "GET", "/scopes", Some(ADMIN), None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["total"], json!(29));
    assert_eq!(body["scopes"][0], json!("read"));
    assert_eq!(body["scopes"][28], json!("read:userinfo"));
    let (status, _) = call(&app, "GET", "/perms", Some(PERMS), None).await;
    assert_eq!(status, StatusCode::OK, "admin:perms grants read:perms");
}

/// A NULL `scopes` row (an adopted database) refuses the row with 500
/// (ADR-0005 decision 5); the role lookup unions and short-circuits as the
/// reference's.
#[tokio::test]
async fn null_rows_and_the_role_lookup() {
    let Some((db, store, app)) = setup(AuthRequired::Yes).await else {
        return;
    };
    row(&app, "ops", &["read:alerts", "read:perms"]).await;
    db.pool
        .execute(
            "INSERT INTO perms (id, match, scopes) VALUES ('p-user', 'user', ARRAY['read:keys'])",
        )
        .await
        .unwrap();
    let roles = |r: &[&str]| r.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
    assert_eq!(
        store
            .scopes_by_match("", &roles(&["guest", "ops", "user"]))
            .await
            .unwrap(),
        ["read", "read:alerts", "read:keys", "read:perms", "write"]
    );
    assert_eq!(
        store
            .scopes_by_match("", &roles(&["ops", "admin", "nosuch"]))
            .await
            .unwrap(),
        ["admin", "read", "write"]
    );
    assert_eq!(
        store
            .scopes_by_match("", &roles(&["nosuch"]))
            .await
            .unwrap(),
        Vec::<String>::new()
    );
    db.pool
        .execute("INSERT INTO perms (id, match, scopes) VALUES ('p-null', 'nullrole', NULL)")
        .await
        .unwrap();
    assert!(
        store
            .scopes_by_match("", &roles(&["nullrole"]))
            .await
            .is_err()
    );
    let (status, _) = call(&app, "GET", "/perm/p-null", Some(ADMIN), None).await;
    assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR);
    let (status, _) = call(&app, "GET", "/perms", Some(ADMIN), None).await;
    assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR);
}

/// With authentication off and no credential the caller holds no scope:
/// every non-empty scope list is above the ceiling, as for key creation.
#[tokio::test]
async fn auth_off_ceiling() {
    let Some((_db, _store, app)) = setup(AuthRequired::No).await else {
        return;
    };
    let (status, body) = call(
        &app,
        "POST",
        "/perm",
        None,
        Some(json!({"match": "ops", "scopes": ["read:alerts"]})),
    )
    .await;
    assert_eq!(
        (status, body["message"].clone()),
        (StatusCode::FORBIDDEN, ceiling("read:alerts", ""))
    );
    let (status, body) = call(
        &app,
        "POST",
        "/perm",
        None,
        Some(json!({"match": "ops", "scopes": []})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
}
