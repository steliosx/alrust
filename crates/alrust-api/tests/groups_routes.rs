//! User groups through the router, against PostgreSQL (reference
//! `views/groups.py`; `spec/security-fixes.md` SF-GROUPS): the vector table,
//! membership, and the list.
//!
//! Each test creates its own database `<prefix>r<n>_<µs>` and drops it when
//! it ends, also on panic. Without `DATABASE_URL` the tests are skipped,
//! except under CI (`CI` set), where a missing database is a failure. Keys
//! and users are synthetic.
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
const GROUPS: &str = "synthetic-groups-key-00000000000000000000";

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
        "{prefix}r{}_{}",
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

async fn setup() -> Option<(TestDb, axum::Router)> {
    let db = test_db().await?;
    let store = Store::new(db.pool.clone(), StoreConfig::default());
    store.prepare().await.unwrap();
    for (id, key, scopes) in [
        ("k-admin", ADMIN, vec!["admin"]),
        ("k-groups", GROUPS, vec!["admin:groups"]),
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
    db.pool
        .execute(
            "INSERT INTO users (id, name, email, login, password, status, roles, attributes, create_time)
             VALUES ('u-ana', 'Ana', 'ana@example.test', 'ana@example.test', 'synthetic', 'active',
                     ARRAY[]::text[], '{}', timezone('UTC', now())),
                    ('u-bo', 'Bo', 'bo@example.test', '', 'synthetic', 'inactive',
                     ARRAY[]::text[], '{}', timezone('UTC', now()))",
        )
        .await
        .unwrap();
    let app = router(Arc::new(AppState {
        store,
        cfg: ApiConfig {
            auth_required: true,
            ..ApiConfig::default()
        },
    }));
    Some((db, app))
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

/// SF-GROUPS vectors: method, path (`{ops}` and `{adm}` are group ids),
/// key, body, status, message (`""`: no message checked).
const VECTORS: [(&str, &str, &str, &str, u16, &str); 13] = [
    (
        "POST",
        "/group",
        GROUPS,
        r#"{"name": "admin"}"#,
        403,
        "Requested scope 'admin' not in existing scopes: admin:groups",
    ),
    (
        "POST",
        "/group",
        GROUPS,
        r#"{"name": "user"}"#,
        403,
        "Requested scope 'read' not in existing scopes: admin:groups",
    ),
    (
        "POST",
        "/group",
        GROUPS,
        r#"{"name": ""}"#,
        400,
        "Missing mandatory value for name",
    ),
    (
        "POST",
        "/group",
        GROUPS,
        r#"{"text": "no name"}"#,
        400,
        "Missing mandatory value for name",
    ),
    (
        "POST",
        "/group",
        GROUPS,
        r#"["not", "an", "object"]"#,
        400,
        "group must be a JSON object",
    ),
    (
        "PUT",
        "/group/{ops}",
        GROUPS,
        r#"{"name": "admin"}"#,
        403,
        "Requested scope 'admin' not in existing scopes: admin:groups",
    ),
    (
        "PUT",
        "/group/{ops}",
        GROUPS,
        r#"{"name": ""}"#,
        400,
        "Missing mandatory value for name",
    ),
    (
        "PUT",
        "/group/{ops}",
        GROUPS,
        r#"{"text": 5}"#,
        400,
        "text must be a string",
    ),
    (
        "PUT",
        "/group/{ops}",
        GROUPS,
        "{}",
        400,
        "nothing to change",
    ),
    (
        "PUT",
        "/group/missing",
        GROUPS,
        r#"{"text": "x"}"#,
        404,
        "not found",
    ),
    (
        "PUT",
        "/group/{adm}/user/u-ana",
        GROUPS,
        "null",
        403,
        "Requested scope 'admin' not in existing scopes: admin:groups",
    ),
    (
        "PUT",
        "/group/{ops}/user/u-none",
        GROUPS,
        "null",
        400,
        "invalid user",
    ),
    (
        "PUT",
        "/group/missing/user/u-ana",
        GROUPS,
        "null",
        404,
        "not found",
    ),
];

async fn create(app: &axum::Router, key: &str, body: Value) -> String {
    let (status, answer) = call(app, "POST", "/group", key, Some(body)).await;
    assert_eq!(status, StatusCode::CREATED, "{answer}");
    answer["id"].as_str().unwrap().to_owned()
}

/// The SF-GROUPS vectors; refusals change nothing.
#[tokio::test]
async fn sf_groups_vectors() {
    let Some((_db, app)) = setup().await else {
        return;
    };
    let ops = create(
        &app,
        GROUPS,
        json!({"id": "g-ops", "name": "ops", "text": "on call"}),
    )
    .await;
    let adm = create(&app, ADMIN, json!({"id": "g-adm", "name": "admin"})).await;
    for (method, uri, key, body, status, message) in VECTORS {
        let uri = uri.replace("{ops}", &ops).replace("{adm}", &adm);
        let body: Value = serde_json::from_str(body).unwrap();
        let body = (!body.is_null()).then_some(body);
        let (got, answer) = call(&app, method, &uri, key, body).await;
        assert_eq!(got.as_u16(), status, "{method} {uri} {answer}");
        assert_eq!(answer["message"], json!(message), "{method} {uri}");
    }
    let (_, ops_now) = call(&app, "GET", "/group/g-ops", ADMIN, None).await;
    assert_eq!(
        ops_now,
        json!({"status": "ok", "total": 1, "group": {
            "id": "g-ops", "href": "http://localhost:8080/group/g-ops",
            "name": "ops", "text": "on call", "count": 0,
        }})
    );
    let (_, adm_users) = call(&app, "GET", "/group/g-adm/users", ADMIN, None).await;
    assert_eq!(adm_users["total"], json!(0));
    // `text` alone needs no ceiling, even on a privileged group (SF-GROUPS).
    let (status, body) = call(
        &app,
        "PUT",
        "/group/g-adm",
        GROUPS,
        Some(json!({"text": "admins"})),
    )
    .await;
    assert_eq!(
        (status, body["group"]["text"].clone()),
        (StatusCode::OK, json!("admins"))
    );
    // Within the ceiling: text, a harmless rename, a member.
    let (status, body) = call(
        &app,
        "PUT",
        "/group/g-ops",
        GROUPS,
        Some(json!({"name": "oncall", "text": "pager"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        (body["group"]["name"].clone(), body["group"]["text"].clone()),
        (json!("oncall"), json!("pager"))
    );
    let (status, _) = call(&app, "PUT", "/group/g-ops/user/u-ana", GROUPS, None).await;
    assert_eq!(status, StatusCode::OK);
    let (status, body) = call(
        &app,
        "POST",
        "/group",
        ADMIN,
        Some(json!({"name": "oncall"})),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::INTERNAL_SERVER_ERROR,
        "duplicate name: {body}"
    );
}

/// Members, their order and the list's filters.
#[tokio::test]
async fn members_and_the_list() {
    let Some((_db, app)) = setup().await else {
        return;
    };
    create(&app, ADMIN, json!({"id": "g-a", "name": "alpha"})).await;
    create(
        &app,
        ADMIN,
        json!({"id": "g-b", "name": "bravo", "text": "second"}),
    )
    .await;
    for user in ["u-bo", "u-ana", "u-bo"] {
        let (status, _) = call(&app, "PUT", &format!("/group/g-a/user/{user}"), ADMIN, None).await;
        assert_eq!(status, StatusCode::OK);
    }
    let (_, body) = call(&app, "GET", "/group/g-a/users", ADMIN, None).await;
    assert_eq!(body["total"], json!(2));
    let mut logins: Vec<Value> = body["users"]
        .as_array()
        .unwrap()
        .iter()
        .map(|u| u["login"].clone())
        .collect();
    logins.sort_by_key(ToString::to_string);
    assert_eq!(
        logins,
        [json!("ana@example.test"), json!("bo@example.test")]
    );
    let names = |body: &Value| -> Vec<String> {
        body["groups"]
            .as_array()
            .unwrap()
            .iter()
            .map(|g| g["name"].as_str().unwrap().to_owned())
            .collect()
    };
    for (uri, want) in [
        ("/groups", vec!["alpha", "bravo"]),
        ("/groups?sort-by=-count", vec!["alpha", "bravo"]),
        ("/groups?sort-by=count", vec!["bravo", "alpha"]),
        ("/groups?count=2", vec!["alpha"]),
        ("/groups?name=~rav", vec!["bravo"]),
        ("/groups?text=second", vec!["bravo"]),
    ] {
        let (status, body) = call(&app, "GET", uri, ADMIN, None).await;
        assert_eq!(status, StatusCode::OK, "{uri} {body}");
        assert_eq!(names(&body), want, "{uri}");
    }
    let (_, body) = call(&app, "GET", "/groups?name=none", ADMIN, None).await;
    assert_eq!(
        (body["message"].clone(), body["total"].clone()),
        (json!("not found"), json!(0))
    );
    let (status, body) = call(&app, "GET", "/groups?users=x", ADMIN, None).await;
    assert_eq!(
        (status, body["message"].clone()),
        (
            StatusCode::BAD_REQUEST,
            json!("Invalid filter parameter: users")
        )
    );
    // Removal, then deletion.
    let (status, _) = call(&app, "DELETE", "/group/g-a/user/u-bo", ADMIN, None).await;
    assert_eq!(status, StatusCode::OK);
    let (_, body) = call(&app, "GET", "/group/g-a", ADMIN, None).await;
    assert_eq!(body["group"]["count"], json!(1));
    let (status, _) = call(&app, "DELETE", "/group/g-a", ADMIN, None).await;
    assert_eq!(status, StatusCode::OK);
    let (status, _) = call(&app, "DELETE", "/group/g-a", ADMIN, None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}
