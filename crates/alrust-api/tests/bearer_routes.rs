//! Operator Bearer tokens through the router, against PostgreSQL
//! (spec/oidc-caller-facts.md sections 3 and 5): API-key carriers win over a
//! Bearer header, also when empty; an operator reads every customer's
//! alerts, cannot assign a customer without an override, and gets the
//! reference's 403 text with empty scope items kept; the three 401 messages;
//! and, with no verifier configured, a Bearer header changes nothing.
//!
//! Each test creates its own database `<prefix>b<n>_<µs>` and drops it when
//! it ends, also on panic. Without `DATABASE_URL` the tests are skipped,
//! except under CI (`CI` set), where a missing database is a failure. Keys
//! and tokens are synthetic.
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

const SIGNING_KEY: &[u8] = b"synthetic-route-test-key-0123456789";
const AUD: &str = "http://alrust.test";
const PROVIDER: &str = "keycloak-test";
const ADMIN_KEY: &str = "synthetic-admin-key-000000000000000000000";
const ACME_KEY: &str = "synthetic-acme-key-0000000000000000000000";

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
        "{prefix}b{}_{}",
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

/// A fixed verifier clock (Codex 74bae416: deterministic). Keys are stored
/// with real expiry times, which the database compares with its own clock.
const NOW: i64 = 1_800_000_000;

fn fixed_now() -> i64 {
    NOW
}

fn bearer_cfg() -> BearerConfig {
    BearerConfig {
        key: SIGNING_KEY.to_vec(),
        audience: AUD.into(),
        provider: PROVIDER.into(),
        now: fixed_now,
    }
}

fn mint(claims: &Value) -> String {
    signed(&URL_SAFE_NO_PAD.encode(claims.to_string()))
}

/// A token over this literal payload segment, signed as written.
fn signed(payload_segment: &str) -> String {
    let input = format!(
        "{}.{payload_segment}",
        URL_SAFE_NO_PAD.encode(r#"{"alg":"HS256","typ":"JWT"}"#)
    );
    let mut mac = Hmac::<Sha256>::new_from_slice(SIGNING_KEY).unwrap();
    mac.update(input.as_bytes());
    format!(
        "{input}.{}",
        URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes())
    )
}

fn operator(scope: &str) -> String {
    mint(&json!({
        "sub": "user-1",
        "preferred_username": "ops@example.test",
        "provider": PROVIDER,
        "scope": scope,
        "aud": AUD,
        "exp": NOW + 3600,
        "alrust_hook_customers": ["acme"],
    }))
}

async fn store(db: &TestDb) -> Store {
    let store = Store::new(db.pool.clone(), StoreConfig::default());
    store.prepare().await.unwrap();
    for (key, user, scopes, customer) in [
        (ADMIN_KEY, "admin", vec!["admin", "read", "write"], None),
        (
            ACME_KEY,
            "zbx-acme",
            vec!["read", "write:alerts"],
            Some("acme"),
        ),
    ] {
        store
            .ensure_key(&ApiKey {
                id: uuid::Uuid::new_v4().to_string(),
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
    store
}

fn app(store: Store, auth_required: AuthRequired, bearer: Option<BearerConfig>) -> axum::Router {
    router(Arc::new(AppState {
        store,
        cfg: ApiConfig {
            auth_required: matches!(auth_required, AuthRequired::Yes),
            bearer,
            ..ApiConfig::default()
        },
    }))
}

#[derive(Debug, Clone, Copy)]
enum AuthRequired {
    Yes,
    No,
}

async fn call(
    app: &axum::Router,
    method: &str,
    uri: &str,
    headers: &[(&str, &str)],
    body: Option<Value>,
) -> (StatusCode, Value) {
    let mut req = Request::builder().method(method).uri(uri);
    for (k, v) in headers {
        req = req.header(*k, *v);
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

fn alert(resource: &str, customer: &str) -> Value {
    json!({
        "resource": resource, "event": "HttpError", "environment": "Production",
        "service": ["Web"], "severity": "major", "customer": customer
    })
}

fn customers_seen(body: &Value) -> Vec<String> {
    let mut c: Vec<String> = body["alerts"]
        .as_array()
        .unwrap()
        .iter()
        .map(|a| a["customer"].as_str().unwrap_or("").to_owned())
        .collect();
    c.sort();
    c
}

#[tokio::test]
async fn bearer_operators_through_the_router() {
    let Some(db) = test_db().await else { return };
    let app = app(store(&db).await, AuthRequired::Yes, Some(bearer_cfg()));
    let admin = format!("Key {ADMIN_KEY}");
    for (resource, customer) in [("r-acme", "acme"), ("r-globex", "globex")] {
        let (code, _) = call(
            &app,
            "POST",
            "/alert",
            &[("authorization", &admin)],
            Some(alert(resource, customer)),
        )
        .await;
        assert_eq!(code, StatusCode::CREATED, "{resource}");
    }

    // A Bearer operator reads every customer's alerts (no customer views).
    let op = format!("Bearer {}", operator("read write:alerts"));
    let (code, body) = call(&app, "GET", "/alerts", &[("authorization", &op)], None).await;
    assert_eq!(code, StatusCode::OK);
    assert_eq!(customers_seen(&body), ["acme", "globex"]);

    // Key carriers win over a Bearer header: the acme key sees only acme.
    let (code, body) = call(
        &app,
        "GET",
        "/alerts",
        &[("authorization", &op), ("x-api-key", ACME_KEY)],
        None,
    )
    .await;
    assert_eq!(code, StatusCode::OK);
    assert_eq!(customers_seen(&body), ["acme"]);
    let (code, body) = call(
        &app,
        "GET",
        &format!("/alerts?api-key={ACME_KEY}"),
        &[("authorization", &op)],
        None,
    )
    .await;
    assert_eq!(code, StatusCode::OK);
    assert_eq!(customers_seen(&body), ["acme"]);
    // ... also when empty.
    for (headers, uri) in [
        (
            vec![("authorization", op.as_str()), ("x-api-key", "")],
            "/alerts",
        ),
        (vec![("authorization", op.as_str())], "/alerts?api-key="),
    ] {
        let (code, body) = call(&app, "GET", uri, &headers, None).await;
        assert_eq!(code, StatusCode::UNAUTHORIZED);
        assert_eq!(body["message"], "API key parameter '' is invalid");
    }

    // No customer to assign without an override.
    let (code, body) = call(
        &app,
        "POST",
        "/alert",
        &[("authorization", &op)],
        Some(alert("r-op", "acme")),
    )
    .await;
    assert_eq!(code, StatusCode::BAD_REQUEST);
    assert_eq!(body["message"], "not allowed to set customer to 'acme'");

    // The reference's 403 text, empty scope items kept.
    let op_keys = format!("Bearer {}", operator("write:keys  read"));
    let (code, body) = call(
        &app,
        "POST",
        "/key",
        &[("authorization", &op_keys)],
        Some(json!({"scopes": ["admin"]})),
    )
    .await;
    assert_eq!(code, StatusCode::FORBIDDEN);
    assert_eq!(
        body["message"],
        "Requested scope 'admin' not in existing scopes: write:keys,,read"
    );

    // The three 401 messages, and an unrecognised header.
    let expired = mint(&json!({"sub": "u", "aud": AUD, "exp": NOW}));
    let wrong_aud = mint(&json!({"sub": "u", "aud": "other", "exp": NOW + 60}));
    for (value, expected) in [
        ("Bearer garbage".to_owned(), "Token is invalid"),
        (format!("Bearer {expired}"), "Token has expired"),
        (format!("Bearer {wrong_aud}"), "Invalid audience"),
        (
            format!("bearer {}", operator("read")),
            "Missing authorization API Key or Bearer Token",
        ),
    ] {
        let (code, body) = call(&app, "GET", "/alerts", &[("authorization", &value)], None).await;
        assert_eq!(code, StatusCode::UNAUTHORIZED, "{value}");
        assert_eq!(
            body,
            json!({"status": "error", "message": expected, "code": 401, "errors": null})
        );
    }
}

fn claims_for(changes: &[(&str, Value)], removed: &[&str]) -> Value {
    let mut c = json!({
        "sub": "user-1", "preferred_username": "ops@example.test", "provider": PROVIDER,
        "scope": "read", "aud": AUD, "exp": NOW + 3600, "alrust_hook_customers": ["acme"],
    });
    for (k, v) in changes {
        c[*k] = v.clone();
    }
    for k in removed {
        c.as_object_mut().unwrap().remove(*k);
    }
    c
}

/// Recognised but invalid tokens and their exact 401 messages: the three
/// errors, mixed-failure precedence, and type and decoding-profile refusals.
fn invalid_tokens() -> Vec<(String, &'static str)> {
    let valid = operator("read");
    let bad_signature = format!("{}AA", &valid[..valid.len() - 2]);
    let nan_payload = {
        let text = claims_for(&[], &[]).to_string();
        let body = text.strip_suffix('}').unwrap();
        signed(&URL_SAFE_NO_PAD.encode(format!("{body},\"x\":NaN}}")))
    };
    vec![
        ("garbage".to_owned(), "Token is invalid"),
        (bad_signature, "Token is invalid"),
        (
            mint(&claims_for(&[("exp", json!(NOW))], &[])),
            "Token has expired",
        ),
        (
            mint(&claims_for(&[("aud", json!("other"))], &[])),
            "Invalid audience",
        ),
        (
            mint(&claims_for(&[("aud", json!("other"))], &["exp"])),
            "Token is invalid",
        ),
        (
            mint(&claims_for(
                &[("aud", json!("other")), ("exp", json!(NOW))],
                &[],
            )),
            "Token has expired",
        ),
        (
            mint(&claims_for(
                &[("aud", json!("other")), ("sub", json!(5))],
                &[],
            )),
            "Invalid audience",
        ),
        (
            mint(&claims_for(&[("preferred_username", json!(5))], &[])),
            "Token is invalid",
        ),
        (nan_payload, "Token is invalid"),
    ]
}

/// The table itself, checked without a database: each token is refused
/// by the verifier with exactly the message the router test expects, and
/// the valid operator token is accepted.
#[test]
fn the_invalid_token_table_matches_the_verifier() {
    use alrust_domain::bearer::verify_bearer;
    for (token, expected) in invalid_tokens() {
        let e = verify_bearer(&token, SIGNING_KEY, AUD, NOW).expect_err(expected);
        assert_eq!(e.message(), expected, "{token}");
    }
    assert!(verify_bearer(&operator("read"), SIGNING_KEY, AUD, NOW).is_ok());
}

/// Spec section 3 and the section 4 errors through the real router, with a
/// verifier configured, in both `AUTH_REQUIRED` settings: a recognised
/// invalid token is always a 401 with the exact envelope, never the auth-off
/// caller; key carriers still win; an unrecognised header is no credential.
#[tokio::test]
async fn the_credential_matrix_through_the_router_in_both_auth_settings() {
    let Some(db) = test_db().await else { return };
    let store = store(&db).await;
    let valid = operator("read");
    let invalid = invalid_tokens();
    for (mode, auth_off_status) in [
        (AuthRequired::Yes, StatusCode::UNAUTHORIZED),
        (AuthRequired::No, StatusCode::OK),
    ] {
        let app = app(
            Store::new(db.pool.clone(), StoreConfig::default()),
            mode,
            Some(bearer_cfg()),
        );
        for (token, expected) in &invalid {
            let value = format!("Bearer {token}");
            let (code, body) =
                call(&app, "GET", "/alerts", &[("authorization", &value)], None).await;
            assert_eq!(code, StatusCode::UNAUTHORIZED, "{mode:?} {expected}");
            assert_eq!(
                body,
                json!({"status": "error", "message": expected, "code": 401, "errors": null}),
                "{mode:?} {token}"
            );
        }
        // A valid token is the operator in both settings.
        let value = format!("Bearer {valid}");
        let (code, _) = call(&app, "GET", "/alerts", &[("authorization", &value)], None).await;
        assert_eq!(code, StatusCode::OK, "{mode:?}");
        // Key carriers win over an invalid Bearer header, also when empty.
        let (code, _) = call(
            &app,
            "GET",
            "/alerts",
            &[("authorization", "Bearer garbage"), ("x-api-key", ACME_KEY)],
            None,
        )
        .await;
        assert_eq!(code, StatusCode::OK, "{mode:?}");
        let (code, body) = call(
            &app,
            "GET",
            "/alerts",
            &[("authorization", "Bearer garbage"), ("x-api-key", "")],
            None,
        )
        .await;
        assert_eq!(code, StatusCode::UNAUTHORIZED, "{mode:?}");
        assert_eq!(body["message"], "API key parameter '' is invalid");
        // An unrecognised header is no credential: 401 Missing, or auth-off.
        let (code, body) = call(
            &app,
            "GET",
            "/alerts",
            &[("authorization", "bearer x")],
            None,
        )
        .await;
        assert_eq!(code, auth_off_status, "{mode:?}");
        if code == StatusCode::UNAUTHORIZED {
            assert_eq!(
                body["message"],
                "Missing authorization API Key or Bearer Token"
            );
        }
    }
    drop(store);
}

#[tokio::test]
async fn auth_off_and_an_unconfigured_verifier() {
    let Some(db) = test_db().await else { return };
    let off = app(store(&db).await, AuthRequired::No, Some(bearer_cfg()));
    // With auth off, an invalid recognised token is still a 401 ...
    let (code, body) = call(
        &off,
        "GET",
        "/alerts",
        &[("authorization", "Bearer garbage")],
        None,
    )
    .await;
    assert_eq!(code, StatusCode::UNAUTHORIZED);
    assert_eq!(body["message"], "Token is invalid");
    // ... while an unrecognised header is the auth-off caller.
    let (code, _) = call(
        &off,
        "GET",
        "/alerts",
        &[("authorization", "bearer x")],
        None,
    )
    .await;
    assert_eq!(code, StatusCode::OK);

    // With no verifier configured, a valid Bearer header changes nothing.
    let unconfigured = app(
        Store::new(db.pool.clone(), StoreConfig::default()),
        AuthRequired::Yes,
        None,
    );
    let op = format!("Bearer {}", operator("read"));
    let (code, body) = call(
        &unconfigured,
        "GET",
        "/alerts",
        &[("authorization", &op)],
        None,
    )
    .await;
    assert_eq!(code, StatusCode::UNAUTHORIZED);
    assert_eq!(
        body["message"],
        "Missing authorization API Key or Bearer Token"
    );
}
