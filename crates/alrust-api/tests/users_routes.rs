//! Users through the router, against PostgreSQL (reference
//! `views/users.py`; `spec/security-fixes.md` SF-USERS): the update vector
//! table, `/user/me` for an operator token and an API key, the attribute
//! merge, the serialized defaults, the list, a user's groups and deletion.
//!
//! Each test creates its own database `<prefix>u<n>_<µs>` and drops it when
//! it ends, also on panic. Without `DATABASE_URL` the tests are skipped,
//! except under CI (`CI` set), where a missing database is a failure. Keys,
//! tokens and users are synthetic.
#![expect(
    clippy::unwrap_used,
    clippy::print_stderr,
    reason = "integration test: helpers unwrap, skips are printed, and each scenario stays in one function"
)]

use std::sync::Arc;
use std::sync::atomic::{AtomicU32, Ordering};

use alrust_api::{ApiConfig, AppState, BearerConfig, router};
use alrust_domain::ApiKey;
use alrust_store_pg::{Store, StoreConfig};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use chrono::Utc;
use hmac::{Hmac, Mac};
use serde_json::{Value, json};
use sha2::Sha256;
use sqlx::Executor as _;
use sqlx::postgres::{PgConnectOptions, PgPool, PgPoolOptions};
use tower::ServiceExt as _;

static COUNTER: AtomicU32 = AtomicU32::new(0);

const ADMIN: &str = "synthetic-admin-key-000000000000000000000";
const USERS: &str = "synthetic-users-key-000000000000000000000";
const SIGNING_KEY: &[u8] = b"synthetic-route-test-key-0123456789";
const AUD: &str = "http://alrust.test";
const PROVIDER: &str = "keycloak-test";
/// A fixed verifier clock, as in `bearer_routes.rs`.
const NOW: i64 = 1_800_000_000;
const GATE: &str = "password cannot be changed: alrust has no password hashing yet";

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
        "{prefix}u{}_{}",
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

fn fixed_now() -> i64 {
    NOW
}

/// An HS256 operator token for user `sub` from `provider`.
fn token(sub: &str, provider: &str) -> String {
    let claims = json!({
        "sub": sub,
        "preferred_username": "ana@example.test",
        "provider": provider,
        "scope": "read",
        "aud": AUD,
        "exp": NOW + 3600,
        "alrust_hook_customers": [],
    });
    let input = format!(
        "{}.{}",
        URL_SAFE_NO_PAD.encode(r#"{"alg":"HS256","typ":"JWT"}"#),
        URL_SAFE_NO_PAD.encode(claims.to_string())
    );
    let mut mac = Hmac::<Sha256>::new_from_slice(SIGNING_KEY).unwrap();
    mac.update(input.as_bytes());
    let sig = URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes());
    format!("Bearer {input}.{sig}")
}

fn key(k: &str) -> String {
    format!("Key {k}")
}

async fn setup() -> Option<(TestDb, axum::Router)> {
    let db = test_db().await?;
    let store = Store::new(db.pool.clone(), StoreConfig::default());
    store.prepare().await.unwrap();
    for (id, k, scopes) in [
        ("k-admin", ADMIN, vec!["admin"]),
        ("k-users", USERS, vec!["admin:users"]),
    ] {
        store
            .ensure_key(&ApiKey {
                id: id.into(),
                key: k.into(),
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
            r#"INSERT INTO perms (id, match, scopes) VALUES ('p-helpdesk', 'helpdesk', ARRAY['admin:users']);
               INSERT INTO users (id, name, email, login, password, status, roles, attributes, create_time, email_verified)
               VALUES ('u-ana', 'Ana', 'ana@example.test', 'ana@example.test', 'synthetic', 'active',
                       ARRAY['ops', 'dev'], '{}', timezone('UTC', now()), true),
                      ('u-bo', 'Bo', 'bo@example.test', 'bo', 'synthetic', 'inactive',
                       ARRAY[]::text[], '{"team": "red"}', timezone('UTC', now()), NULL),
                      ('u-cy', NULL, NULL, 'CORP\cy', 'synthetic', NULL,
                       NULL, '{}', '2026-01-01 00:00:00', NULL)"#,
        )
        .await
        .unwrap();
    let app = router(Arc::new(AppState {
        store,
        cfg: ApiConfig {
            auth_required: true,
            bearer: Some(BearerConfig {
                key: SIGNING_KEY.to_vec(),
                audience: AUD.into(),
                provider: PROVIDER.into(),
                now: fixed_now,
            }),
            ..ApiConfig::default()
        },
    }));
    Some((db, app))
}

async fn call(
    app: &axum::Router,
    method: &str,
    uri: &str,
    auth: &str,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let req = Request::builder()
        .method(method)
        .uri(uri)
        .header("Authorization", auth);
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

const ABOVE: &str = "Requested scope 'admin' not in existing scopes: admin:users";
const EXCLUSIVE: &str = "role and roles are mutually exclusive";
const NOT_LIST: &str = "roles must be a list of strings";
const NOT_ROLE: &str = "role must be a non-empty string";

/// SF-USERS update vectors, in order on one user: path, key, body, status,
/// message (`""`: none). `helpdesk` grants `admin:users`, within the
/// `USERS` key's ceiling.
const VECTORS: [(&str, &str, &str, u16, &str); 22] = [
    ("/user/u-bo", USERS, r#"{"role": "admin"}"#, 403, ABOVE),
    ("/user/u-bo", USERS, r#"{"role": "helpdesk"}"#, 200, ""),
    ("/user/u-bo", USERS, r#"{"roles": ["admin"]}"#, 403, ABOVE),
    ("/user/u-bo", USERS, r#"{"roles": ["helpdesk"]}"#, 200, ""),
    ("/user/u-bo", USERS, r#"{"roles": []}"#, 200, ""),
    ("/user/u-bo", USERS, r#"{"text": "on call"}"#, 200, ""),
    (
        "/user/u-bo",
        USERS,
        r#"{"roles": ["admin"], "role": "helpdesk"}"#,
        400,
        EXCLUSIVE,
    ),
    (
        "/user/u-bo",
        USERS,
        r#"{"roles": [], "role": "admin"}"#,
        400,
        EXCLUSIVE,
    ),
    ("/user/u-bo", USERS, r#"{"roles": "admin"}"#, 400, NOT_LIST),
    (
        "/user/u-bo",
        USERS,
        r#"{"roles": ["ops", 5]}"#,
        400,
        NOT_LIST,
    ),
    ("/user/u-bo", USERS, r#"{"role": null}"#, 400, NOT_ROLE),
    (
        "/user/u-bo",
        USERS,
        r#"{"role": 5, "roles": ["admin"]}"#,
        400,
        NOT_ROLE,
    ),
    ("/user/u-bo", USERS, r#"{"role": ""}"#, 400, NOT_ROLE),
    ("/user/u-bo", USERS, "{}", 400, "nothing to change"),
    ("/user/u-none", USERS, r#"{"text": "x"}"#, 404, "not found"),
    (
        "/user/u-bo",
        USERS,
        r#"{"email": "ana@example.test", "roles": ["admin"]}"#,
        409,
        "user with that email already exists",
    ),
    (
        "/user/u-bo",
        USERS,
        r#"{"email": "nope"}"#,
        400,
        "Value for \"email\" not valid: nope",
    ),
    (
        "/user/u-bo",
        USERS,
        r#"{"password": "synthetic-new"}"#,
        400,
        GATE,
    ),
    (
        "/user/u-bo",
        USERS,
        r#"{"name": 5}"#,
        400,
        "name must be a string",
    ),
    (
        "/user/u-bo",
        USERS,
        r#"{"attributes": [1]}"#,
        400,
        "attributes must be an object",
    ),
    (
        "/user/u-bo",
        USERS,
        r#"{"email_verified": "yes"}"#,
        400,
        "email_verified must be a boolean",
    ),
    ("/user/u-bo", ADMIN, r#"{"role": "admin"}"#, 200, ""),
];

/// The SF-USERS update vectors; refusals change nothing.
#[tokio::test]
async fn sf_users_vectors() {
    let Some((_db, app)) = setup().await else {
        return;
    };
    for (uri, k, body, status, message) in VECTORS {
        let body: Value = serde_json::from_str(body).unwrap();
        let (got, answer) = call(&app, "PUT", uri, &key(k), Some(body.clone())).await;
        assert_eq!(got.as_u16(), status, "{uri} {body} {answer}");
        let want = if message.is_empty() {
            Value::Null
        } else {
            json!(message)
        };
        assert_eq!(answer["message"], want, "{uri} {body}");
    }
    let (_, body) = call(&app, "GET", "/user/u-bo", &key(USERS), None).await;
    let user = &body["user"];
    for (field, want) in [
        ("roles", json!(["admin"])),
        ("text", json!("on call")),
        ("name", json!("Bo")),
        ("email", json!("bo@example.test")),
        ("email_verified", json!(false)),
        ("login", json!("bo")),
        ("domain", json!("example.test")),
        ("status", json!("inactive")),
        ("attributes", json!({"team": "red"})),
        ("href", json!("http://localhost:8080/user/u-bo")),
    ] {
        assert_eq!(user[field], want, "{field}");
    }
    // A new e-mail resets email_verified unless the body sets it; members
    // that are null change nothing; attributes merge at the top level.
    for (body, verified) in [
        (
            json!({"email": "bo2@example.test", "email_verified": true}),
            true,
        ),
        (json!({"email": "bo3@example.test", "name": null}), false),
    ] {
        let (status, answer) = call(&app, "PUT", "/user/u-bo", &key(USERS), Some(body)).await;
        assert_eq!(status, StatusCode::OK, "{answer}");
        assert_eq!(answer["user"]["email_verified"], json!(verified));
        assert_eq!(answer["user"]["name"], json!("Bo"));
    }
    let (_, answer) = call(
        &app,
        "PUT",
        "/user/u-bo",
        &key(USERS),
        Some(json!({"attributes": {"zone": "eu"}})),
    )
    .await;
    assert_eq!(
        answer["user"]["attributes"],
        json!({"team": "red", "zone": "eu"})
    );
}

/// `/user/me`: the operator token's user, 404 for an API key and for a
/// token from another provider; the self-update limits.
#[tokio::test]
async fn me_routes() {
    let Some((_db, app)) = setup().await else {
        return;
    };
    let me = token("u-ana", PROVIDER);
    let (status, body) = call(&app, "GET", "/user/me", &me, None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        (body["user"]["id"].clone(), body["user"]["roles"].clone()),
        (json!("u-ana"), json!(["ops", "dev"]))
    );
    for auth in [key(ADMIN), token("u-ana", "other-provider")] {
        let (status, body) = call(&app, "GET", "/user/me", &auth, None).await;
        assert_eq!(
            (status, body["message"].clone()),
            (StatusCode::NOT_FOUND, json!("not found"))
        );
    }
    for (body, status, message) in [
        (
            json!({"roles": ["admin"], "status": "x"}),
            400,
            json!("not allowed to update: roles, status"),
        ),
        (json!({}), 400, json!("nothing to change")),
        (
            json!({"email": "bo@example.test"}),
            409,
            json!("user with that email already exists"),
        ),
        (json!({"password": "synthetic-new"}), 400, json!(GATE)),
        (json!({"name": "Ana B", "text": "lead"}), 200, Value::Null),
    ] {
        let (got, answer) = call(&app, "PUT", "/user/me", &me, Some(body.clone())).await;
        assert_eq!(
            (got.as_u16(), answer["message"].clone()),
            (status, message),
            "{body}"
        );
    }
    let (_, body) = call(
        &app,
        "PUT",
        "/user/me",
        &key(ADMIN),
        Some(json!({"name": "x"})),
    )
    .await;
    assert_eq!(body["message"], json!("not found"));
    let (_, body) = call(&app, "GET", "/user/me", &me, None).await;
    assert_eq!(
        (
            body["user"]["name"].clone(),
            body["user"]["email_verified"].clone()
        ),
        (json!("Ana B"), json!(true))
    );
}

/// The attribute routes: a deep merge, then the top-level nulls removed.
#[tokio::test]
async fn attribute_merge() {
    let Some((_db, app)) = setup().await else {
        return;
    };
    let me = token("u-ana", PROVIDER);
    for attributes in [
        json!({"a": {"x": 1}, "b": 2}),
        json!({"a": {"y": null}, "b": null}),
    ] {
        let (status, _) = call(
            &app,
            "PUT",
            "/user/me/attributes",
            &me,
            Some(json!({"attributes": attributes})),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
    }
    let (_, body) = call(&app, "GET", "/user/me/attributes", &me, None).await;
    assert_eq!(
        body,
        json!({"status": "ok", "total": 1, "attributes": {"a": {"x": 1, "y": null}}})
    );
    for (uri, auth, body, status) in [
        (
            "/user/me/attributes",
            key(ADMIN),
            json!({"attributes": {}}),
            400,
        ),
        (
            "/user/me/attributes",
            key(ADMIN),
            json!({"attributes": {"k": 1}}),
            404,
        ),
        (
            "/user/u-bo/attributes",
            key(USERS),
            json!({"attributes": "x"}),
            400,
        ),
        (
            "/user/u-none/attributes",
            key(USERS),
            json!({"attributes": {"k": 1}}),
            404,
        ),
        (
            "/user/u-bo/attributes",
            key(USERS),
            json!({"attributes": {"tier": 2}}),
            200,
        ),
    ] {
        let (got, answer) = call(&app, "PUT", uri, &auth, Some(body)).await;
        assert_eq!(got.as_u16(), status, "{uri} {answer}");
    }
    let (_, body) = call(&app, "GET", "/user/u-bo", &key(USERS), None).await;
    assert_eq!(
        body["user"]["attributes"],
        json!({"team": "red", "tier": 2})
    );
}

/// The serialized defaults and the list.
#[tokio::test]
async fn defaults_and_list() {
    let Some((_db, app)) = setup().await else {
        return;
    };
    let admin = key(ADMIN);
    let (_, body) = call(&app, "GET", "/user/u-cy", &admin, None).await;
    assert_eq!(
        body["user"],
        json!({
            "id": "u-cy", "href": "http://localhost:8080/user/u-cy", "name": "",
            "login": "CORP\\cy", "email": null, "domain": "CORP", "status": "active",
            "roles": null, "attributes": {}, "createTime": "2026-01-01T00:00:00.000Z",
            "lastLogin": null, "text": "", "updateTime": body["user"]["updateTime"],
            "email_verified": false,
        })
    );
    let ids = |body: &Value| -> Vec<String> {
        body["users"]
            .as_array()
            .unwrap()
            .iter()
            .map(|u| u["id"].as_str().unwrap().to_owned())
            .collect()
    };
    let (_, body) = call(&app, "GET", "/users", &key(USERS), None).await;
    assert_eq!(
        (ids(&body), body["domains"].clone(), body["total"].clone()),
        (
            vec!["u-ana".to_owned(), "u-bo".into(), "u-cy".into()],
            json!(["*"]),
            json!(3)
        )
    );
    for (uri, want) in [
        ("/users?role=ops", vec!["u-ana"]),
        ("/users?role=ops&role=none", vec!["u-ana"]),
        ("/users?attributes.team=red", vec!["u-bo"]),
        ("/users?email_verified=true", vec!["u-ana"]),
        ("/users?createTime=2026-01-01T00:00:00", vec!["u-cy"]),
        (
            "/users?createTime=2026-01-01T00:00:00&createTime=2025-06-01T00:00:00",
            vec!["u-cy"],
        ),
        ("/users?name=~o", vec!["u-bo"]),
        ("/users?status!=active", vec!["u-bo"]),
        ("/users?sort-by=createTime", vec!["u-ana", "u-bo", "u-cy"]),
        ("/users?sort-by=-createTime", vec!["u-cy", "u-ana", "u-bo"]),
    ] {
        let (status, body) = call(&app, "GET", uri, &admin, None).await;
        assert_eq!(status, StatusCode::OK, "{uri} {body}");
        assert_eq!(ids(&body), want, "{uri}");
    }
    for (uri, message) in [
        (
            "/users?domain=example.test",
            "Filtering by 'domain' field not supported.",
        ),
        (
            "/users?createTime=~2026",
            "Filtering by 'createTime' field not supported.",
        ),
        (
            "/users?attributes=x",
            "Filtering by 'attributes' field not supported.",
        ),
        (
            "/users?sort-by=domain",
            "Sorting by 'domain' field not supported.",
        ),
        ("/users?roles=ops", "Invalid filter parameter: roles"),
    ] {
        let (status, body) = call(&app, "GET", uri, &admin, None).await;
        assert_eq!(
            (status, body["message"].clone()),
            (StatusCode::BAD_REQUEST, json!(message)),
            "{uri}"
        );
    }
    let (_, body) = call(&app, "GET", "/users?login=none", &admin, None).await;
    assert_eq!(
        (body["message"].clone(), body["total"].clone()),
        (json!("not found"), json!(0))
    );
    let (status, _) = call(
        &app,
        "POST",
        "/user",
        &admin,
        Some(json!({"login": "new", "password": "synthetic"})),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "POST /user is not served");
}

/// A user's groups; deletion keeps the id among a group's members.
#[tokio::test]
async fn groups_and_delete() {
    let Some((_db, app)) = setup().await else {
        return;
    };
    let admin = key(ADMIN);
    call(
        &app,
        "POST",
        "/group",
        &admin,
        Some(json!({"id": "g-ops", "name": "ops"})),
    )
    .await;
    let (status, _) = call(&app, "PUT", "/group/g-ops/user/u-ana", &admin, None).await;
    assert_eq!(status, StatusCode::OK);
    let (_, body) = call(&app, "GET", "/user/u-ana/groups", &admin, None).await;
    assert_eq!(
        (body["total"].clone(), body["groups"][0]["id"].clone()),
        (json!(1), json!("g-ops"))
    );
    let (_, body) = call(&app, "GET", "/user/u-bo/groups", &admin, None).await;
    assert_eq!(
        (body["total"].clone(), body["message"].clone()),
        (json!(0), json!("not found"))
    );
    for (method, uri, status) in [
        ("GET", "/user/u-none/groups", 404),
        ("DELETE", "/user/u-ana", 200),
        ("GET", "/user/u-ana", 404),
        ("DELETE", "/user/u-ana", 404),
    ] {
        let (got, _) = call(&app, method, uri, &admin, None).await;
        assert_eq!(got.as_u16(), status, "{method} {uri}");
    }
    let (_, body) = call(&app, "GET", "/group/g-ops", &admin, None).await;
    assert_eq!(body["group"]["count"], json!(1));
    let (_, body) = call(&app, "GET", "/group/g-ops/users", &admin, None).await;
    assert_eq!(body["total"], json!(0));
}
