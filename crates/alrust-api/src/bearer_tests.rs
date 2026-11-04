//! Operator Bearer tokens in the request path (spec/oidc-caller-facts.md
//! sections 3, 5 and 6), without a database: the store is a lazy pool that
//! these paths never use. Tokens are signed here from a synthetic key over
//! their literal encoded segments. Rows that need a stored API key run in
//! `tests/bearer_routes.rs` against PostgreSQL.

use std::sync::Arc;

use alrust_store_pg::hooks::zabalack::{
    Bypass, Capture, Policy, PolicyInput, authorize_actor, human_actor,
};
use alrust_store_pg::{CallerFacts, Store, StoreConfig};
use axum::http::{HeaderMap, HeaderValue, StatusCode};
use axum::response::IntoResponse as _;
use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use hmac::{Hmac, Mac};
use serde_json::{Value, json};
use sha2::Sha256;

use super::{
    ApiConfig, ApiError, AppState, BearerConfig, Caller, Credential, authenticate, bearer_token,
    select_credential,
};

const KEY: &[u8] = b"synthetic-api-test-key-0123456789";
const AUD: &str = "http://alrust.test";
const NOW: i64 = 1_800_000_000;
const PROVIDER: &str = "keycloak-test";

fn fixed_now() -> i64 {
    NOW
}

fn bearer_cfg() -> BearerConfig {
    BearerConfig {
        key: KEY.to_vec(),
        audience: AUD.into(),
        provider: PROVIDER.into(),
        now: fixed_now,
    }
}

fn mint(claims: &Value) -> String {
    let input = format!(
        "{}.{}",
        URL_SAFE_NO_PAD.encode(r#"{"alg":"HS256","typ":"JWT"}"#),
        URL_SAFE_NO_PAD.encode(claims.to_string())
    );
    let mut mac = Hmac::<Sha256>::new_from_slice(KEY).expect("any key length");
    mac.update(input.as_bytes());
    format!(
        "{input}.{}",
        URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes())
    )
}

fn claims() -> Value {
    json!({
        "sub": "user-1",
        "preferred_username": "ops@example.test",
        "provider": PROVIDER,
        "scope": "read write:alerts",
        "aud": AUD,
        "exp": NOW + 3600,
        "alrust_hook_customers": ["acme"],
    })
}

fn with(key: &str, value: Value) -> Value {
    let mut c = claims();
    c[key] = value;
    c
}

fn without(key: &str) -> Value {
    let mut c = claims();
    c.as_object_mut().expect("object").remove(key);
    c
}

/// `AUTH_REQUIRED`, as a value rather than a bool parameter.
#[derive(Debug, Clone, Copy)]
enum Auth {
    Required,
    Off,
}

impl Auth {
    fn required(self) -> bool {
        matches!(self, Auth::Required)
    }
}

fn state(auth: Auth, bearer: Option<BearerConfig>) -> AppState {
    let pool = sqlx::postgres::PgPoolOptions::new()
        .connect_lazy("postgres://unused@127.0.0.1:1/unused")
        .expect("lazy pool");
    AppState {
        store: Store::new(pool, StoreConfig::default()),
        cfg: ApiConfig {
            auth_required: auth.required(),
            bearer,
            ..ApiConfig::default()
        },
    }
}

fn headers(pairs: &[(&str, &[u8])]) -> HeaderMap {
    let mut h = HeaderMap::new();
    for (name, value) in pairs {
        h.append(
            axum::http::HeaderName::from_bytes(name.as_bytes()).expect("name"),
            HeaderValue::from_bytes(value).expect("value"),
        );
    }
    h
}

fn bearer(token: &str) -> Vec<u8> {
    format!("Bearer {token}").into_bytes()
}

async fn auth(st: &AppState, h: &HeaderMap) -> Result<Caller, ApiError> {
    authenticate(st, h, &[]).await
}

fn message(e: ApiError) -> (StatusCode, Value) {
    use std::pin::pin;
    use std::task::{Context, Poll, Waker};
    let r = e.into_response();
    let code = r.status();
    // An in-memory body is ready on the first poll.
    let mut fut = pin!(axum::body::to_bytes(r.into_body(), usize::MAX));
    let Poll::Ready(bytes) = fut.as_mut().poll(&mut Context::from_waker(Waker::noop())) else {
        panic!("body not ready");
    };
    (
        code,
        serde_json::from_slice(&bytes.expect("body")).expect("json"),
    )
}

#[test]
fn bearer_header_recognition_matches_the_reference() {
    let recognised: &[(&[u8], &str)] = &[
        (b"Bearer abc", "abc"),
        (b"Bearer abc def", "abc"),
        (b"Bearer abc\tdef", "abc"),
        (b"Bearer a.b.c", "a.b.c"),
        // Latin-1 decoding: a non-ASCII byte is part of the run ...
        (b"Bearer ab\xe9c", "ab\u{e9}c"),
        // ... but U+00A0 and U+0085 end it, as `\s` does in Python.
        (b"Bearer ab\xa0cd", "ab"),
        (b"Bearer ab\x85cd", "ab"),
    ];
    for (value, token) in recognised {
        let h = headers(&[("authorization", value)]);
        assert_eq!(bearer_token(&h).as_deref(), Some(*token), "{value:?}");
    }
    let unrecognised: &[&[u8]] = &[
        b"Bearer",
        b"Bearer ",
        b"Bearer  abc",
        b"Bearer \tabc",
        b"bearer abc",
        b"BEARER abc",
        b"Bearerabc",
        b"Basic dXNlcjpwYXNz",
        b"Key abc",
        b"Token abc",
    ];
    for value in unrecognised {
        let h = headers(&[("authorization", value)]);
        assert_eq!(bearer_token(&h), None, "{value:?}");
    }
    assert_eq!(bearer_token(&HeaderMap::new()), None);
}

#[test]
fn credential_selection_follows_section_3() {
    let cfg = bearer_cfg();
    let some = Some(&cfg);
    let token = bearer("t");
    let key = |h: &HeaderMap, p: &[(String, String)], b| match select_credential(h, p, b) {
        Credential::Key(k) => Some(k),
        _ => None,
    };
    let is_bearer =
        |h: &HeaderMap, b| matches!(select_credential(h, &[], b), Credential::Bearer(..));
    let is_absent = |h: &HeaderMap, b| matches!(select_credential(h, &[], b), Credential::Absent);
    let param = |v: &str| vec![("api-key".to_owned(), v.to_owned())];

    // Authorization: Key, with or without other carriers.
    let h = headers(&[("authorization", b"Key k1"), ("x-api-key", b"k2")]);
    assert_eq!(key(&h, &param("k3"), some).as_deref(), Some("k1"));
    // `Key ` with nothing after it, as received: the key path, empty.
    let h = headers(&[("authorization", b"Key "), ("x-api-key", b"k2")]);
    assert_eq!(key(&h, &[], some).as_deref(), Some(""));
    assert_eq!(key(&h, &[], None).as_deref(), Some(""));
    // A key carrier wins over a Bearer header, also when it is empty.
    let h = headers(&[("authorization", &token), ("x-api-key", b"k2")]);
    assert_eq!(key(&h, &[], some).as_deref(), Some("k2"));
    let h = headers(&[("authorization", &token), ("x-api-key", b"")]);
    assert_eq!(key(&h, &[], some).as_deref(), Some(""));
    let h = headers(&[("authorization", &token)]);
    assert_eq!(key(&h, &param("k3"), some).as_deref(), Some("k3"));
    assert_eq!(key(&h, &param(""), some).as_deref(), Some(""));
    // A recognised Bearer header alone, with and without a verifier.
    assert!(is_bearer(&h, some));
    assert!(is_absent(&h, None));
    // Unrecognised forms are no credential either way.
    for value in [
        &b"Bearer"[..],
        b"Bearer ",
        b"Bearer  t",
        b"bearer t",
        b"Basic eDp5",
    ] {
        let h = headers(&[("authorization", value)]);
        assert!(is_absent(&h, some), "{value:?}");
        assert!(is_absent(&h, None), "{value:?}");
    }
    assert!(is_absent(&HeaderMap::new(), some));
}

#[tokio::test]
async fn a_valid_bearer_token_is_the_operator_in_both_auth_settings() {
    for mode in [Auth::Required, Auth::Off] {
        let st = state(mode, Some(bearer_cfg()));
        let h = headers(&[("authorization", &bearer(&mint(&claims())))]);
        let caller = auth(&st, &h).await.expect("operator");
        assert_eq!(caller.login.as_deref(), Some("ops@example.test"));
        assert_eq!(
            caller.scopes,
            vec!["read".to_owned(), "write:alerts".to_owned()]
        );
        assert_eq!(caller.customer, None);
        assert!(!caller.auth_off);
        let facts = caller.facts(&st);
        assert_eq!(
            facts,
            CallerFacts {
                login: Some("ops@example.test".into()),
                user_id: Some("user-1".into()),
                auth_type: Some("bearer:keycloak-test".into()),
                scopes: vec!["read".into(), "write:alerts".into()],
                customers: Some(vec!["acme".into()]),
                auth_required: mode.required(),
                request_id: None,
                from_request: true,
            }
        );
    }
}

#[tokio::test]
async fn an_invalid_recognised_token_is_401_in_both_auth_settings() {
    let mut no_exp_wrong_aud = without("exp");
    no_exp_wrong_aud["aud"] = json!("someone-else");
    let mut expired_wrong_aud = with("exp", json!(NOW));
    expired_wrong_aud["aud"] = json!("someone-else");
    let cases = [
        ("garbage".to_owned(), "Token is invalid"),
        (format!("{}x", mint(&claims())), "Token is invalid"),
        (mint(&with("exp", json!(NOW))), "Token has expired"),
        (
            mint(&with("aud", json!("someone-else"))),
            "Invalid audience",
        ),
        (mint(&without("aud")), "Invalid audience"),
        (mint(&without("exp")), "Token is invalid"),
        (
            mint(&with("preferred_username", json!(5))),
            "Token is invalid",
        ),
        (mint(&no_exp_wrong_aud), "Token is invalid"),
        (mint(&expired_wrong_aud), "Token has expired"),
        // Non-ASCII in the recognised run: outside the decoding profile.
        (format!("{}\u{e9}", mint(&claims())), "Token is invalid"),
    ];
    for mode in [Auth::Required, Auth::Off] {
        let st = state(mode, Some(bearer_cfg()));
        for (token, expected) in &cases {
            let value: Vec<u8> = format!("Bearer {token}")
                .chars()
                .map(|c| u8::try_from(u32::from(c)).expect("latin-1"))
                .collect();
            let h = headers(&[("authorization", &value)]);
            let e = auth(&st, &h).await.expect_err("refused");
            let (code, body) = message(e);
            assert_eq!(code, StatusCode::UNAUTHORIZED, "{token}");
            assert_eq!(
                body,
                json!({"status": "error", "message": expected, "code": 401, "errors": null}),
                "{mode:?} {token}"
            );
        }
    }
}

#[tokio::test]
async fn without_a_verifier_or_a_recognised_header_nothing_changes() {
    let token = bearer(&mint(&claims()));
    let unrecognised = b"bearer x".to_vec();
    for (cfg, value) in [(None, &token), (Some(bearer_cfg()), &unrecognised)] {
        let on = state(Auth::Required, cfg.clone());
        let h = headers(&[("authorization", value)]);
        let (code, body) = message(auth(&on, &h).await.expect_err("401"));
        assert_eq!(code, StatusCode::UNAUTHORIZED);
        assert_eq!(
            body["message"],
            "Missing authorization API Key or Bearer Token"
        );
        let off = state(Auth::Off, cfg);
        let caller = auth(&off, &h).await.expect("auth off");
        assert!(caller.auth_off);
        assert_eq!(caller.login, None);
        assert!(caller.scopes.is_empty());
        assert_eq!(caller.facts(&off), auth_off_facts(Auth::Off));
    }
}

fn auth_off_facts(auth: Auth) -> CallerFacts {
    CallerFacts {
        login: None,
        user_id: None,
        auth_type: None,
        scopes: vec![],
        customers: None,
        auth_required: auth.required(),
        request_id: None,
        from_request: true,
    }
}

#[tokio::test]
async fn api_key_callers_keep_their_facts() {
    // The Caller the key path builds, for an ordinary, an admin and a
    // person-looking key: facts as before, with no operator facts.
    let st = state(Auth::Required, Some(bearer_cfg()));
    for (user, scopes, customer) in [
        (
            "zbx-1",
            vec!["write:alerts".to_owned()],
            Some("acme".to_owned()),
        ),
        ("admin", vec!["admin".to_owned()], None),
        (
            "jane.doe@example.test",
            vec!["read".to_owned()],
            Some("acme".to_owned()),
        ),
    ] {
        let caller = Caller {
            login: Some(user.to_owned()),
            scopes: scopes.clone(),
            customer: customer.clone(),
            auth_off: false,
            operator: None,
        };
        assert_eq!(
            caller.facts(&st),
            CallerFacts {
                login: Some(user.to_owned()),
                user_id: None,
                auth_type: None,
                scopes,
                customers: customer.map(|c| vec![c]),
                auth_required: true,
                request_id: None,
                from_request: true,
            }
        );
    }
    // Basic credentials are not read: no credential.
    let off = state(Auth::Off, Some(bearer_cfg()));
    let h = headers(&[("authorization", b"Basic dXNlcjpwYXNz")]);
    assert_eq!(
        auth(&off, &h).await.expect("off").facts(&off),
        auth_off_facts(Auth::Off)
    );
}

#[tokio::test]
async fn failed_hook_checks_keep_the_global_caller() {
    let st = state(Auth::Required, Some(bearer_cfg()));
    for c in [
        without("sub"),
        with("sub", json!("")),
        with("preferred_username", json!("")),
        without("preferred_username"),
        with("preferred_username", Value::Null),
        without("provider"),
        with("provider", json!("basic")),
        without("alrust_hook_customers"),
        with("alrust_hook_customers", json!([""])),
        with("alrust_hook_customers", json!("acme")),
    ] {
        let h = headers(&[("authorization", &bearer(&mint(&c)))]);
        let caller = auth(&st, &h).await.expect("valid token");
        assert_eq!(caller.customer, None, "{c}");
        assert!(!caller.auth_off, "{c}");
        assert_eq!(
            caller.scopes,
            vec!["read".to_owned(), "write:alerts".to_owned()]
        );
        let facts = caller.facts(&st);
        assert_eq!(facts.user_id, None, "{c}");
        assert_eq!(facts.auth_type, None, "{c}");
        assert_eq!(facts.customers, None, "{c}");
        assert_eq!(facts.login, caller.login, "{c}");
    }
    // An absent or null login is no login at all, in the caller and facts.
    for c in [
        without("preferred_username"),
        with("preferred_username", Value::Null),
    ] {
        let h = headers(&[("authorization", &bearer(&mint(&c)))]);
        let caller = auth(&st, &h).await.expect("valid token");
        assert_eq!(caller.login, None);
        assert_eq!(caller.facts(&st).login, None);
    }
}

#[tokio::test]
async fn hook_customers_are_sorted_and_never_widened() {
    let st = state(Auth::Required, Some(bearer_cfg()));
    let facts = |customers: Value| {
        let st = &st;
        async move {
            let h = headers(&[(
                "authorization",
                &bearer(&mint(&with("alrust_hook_customers", customers))),
            )]);
            auth(st, &h).await.expect("valid").facts(st).customers
        }
    };
    assert_eq!(
        facts(json!(["b", "a", "a"])).await,
        Some(vec!["a".to_owned(), "b".to_owned()])
    );
    assert_eq!(facts(json!([])).await, Some(vec![]));
}

#[tokio::test]
async fn bearer_operators_have_global_semantics_without_customer_views() {
    let st = state(Auth::Required, Some(bearer_cfg()));
    let h = headers(&[(
        "authorization",
        &bearer(&mint(&with("scope", json!("write:keys  read")))),
    )]);
    let caller = auth(&st, &h).await.expect("operator");
    // Empty scope items are kept, as the reference's `split(' ')` keeps
    // them; create_key's 403 joins them with commas.
    assert_eq!(caller.scopes.join(","), "write:keys,,read");
    assert!(caller.require("admin").is_err());
    // No customer filter, and no customer to assign without an override.
    assert_eq!(caller.customers(), None);
    assert_eq!(
        caller.assign_customer(None, "admin:alerts").expect("none"),
        None
    );
    let e = caller
        .assign_customer(Some("acme".into()), "admin:alerts")
        .expect_err("refused");
    assert_eq!(
        message(e).1["message"],
        "not allowed to set customer to 'acme'"
    );
}

#[tokio::test]
async fn identity_sent_outside_the_token_is_ignored() {
    let st = state(Auth::Required, Some(bearer_cfg()));
    let mut h = headers(&[("authorization", &bearer(&mint(&claims())))]);
    for name in [
        "x-user-id",
        "x-login",
        "x-customer",
        "x-provider",
        "x-forwarded-user",
    ] {
        h.append(name, HeaderValue::from_static("mallory"));
    }
    let params: Vec<(String, String)> = ["user_id", "login", "customer", "customers", "provider"]
        .iter()
        .map(|k| ((*k).to_owned(), "mallory".to_owned()))
        .collect();
    let caller = authenticate(&st, &h, &params).await.expect("operator");
    let facts = caller.facts(&st);
    assert_eq!(facts.user_id.as_deref(), Some("user-1"));
    assert_eq!(facts.login.as_deref(), Some("ops@example.test"));
    assert_eq!(facts.customers, Some(vec!["acme".to_owned()]));
}

fn policy(admin_exceptions: Vec<String>) -> Policy {
    let cohort = |customer: &str, allow_admin: bool| {
        json!({
            "customer": customer, "zabbix_instance": "zbx-1", "zabbix_trigger_id": "77",
            "host_ids": ["1"], "group_ids": ["2"], "revision": "r1", "policy_revision": "1",
            "enabled": true, "one_to_one": true, "comment_enabled": false, "allow_admin": allow_admin
        })
    };
    Policy::new(PolicyInput {
        capture: Capture::Enabled,
        bypass: Bypass::Off,
        config_revision: "cfg-1".into(),
        ingestion_mappings: json!({"zbx-ingest": {"customer": "acme", "instance": "zbx-1"}})
            .as_object()
            .expect("object")
            .clone(),
        admin_exceptions,
        cohorts: vec![cohort("acme", false), cohort("globex", true)],
    })
    .expect("policy")
}

#[tokio::test]
async fn operator_facts_drive_the_actor_checks() {
    let st = state(Auth::Required, Some(bearer_cfg()));
    let facts = |c: Value| {
        let st = &st;
        async move {
            let h = headers(&[("authorization", &bearer(&mint(&c)))]);
            auth(st, &h).await.expect("valid").facts(st)
        }
    };
    let p = policy(vec!["chief@example.test".into()]);
    let acme = p
        .select_cohort("acme", "zbx-1", "77")
        .expect("acme")
        .clone();
    let globex = p
        .select_cohort("globex", "zbx-1", "77")
        .expect("globex")
        .clone();

    // A customer-A operator: A only, never B or an empty customer.
    let actor = human_actor(&p, &facts(claims()).await).expect("actor");
    assert!(authorize_actor(&actor, "acme", &acme));
    assert!(!authorize_actor(&actor, "globex", &globex));
    assert!(!authorize_actor(&actor, "", &acme));
    // An empty customer list authorises nothing for a non-admin.
    let none =
        human_actor(&p, &facts(with("alrust_hook_customers", json!([]))).await).expect("actor");
    assert!(!authorize_actor(&none, "acme", &acme));
    // Exact admin scopes need the exception, then the cohort's allow_admin.
    for scope in ["admin", "admin:alerts"] {
        let mut c = with("scope", json!(scope));
        assert!(
            human_actor(&p, &facts(c.clone()).await).is_none(),
            "{scope}"
        );
        c["preferred_username"] = json!("chief@example.test");
        let admin = human_actor(&p, &facts(c).await).expect("excepted admin");
        assert!(admin.admin);
        assert!(!authorize_actor(&admin, "acme", &acme));
        assert!(authorize_actor(&admin, "globex", &globex));
    }
    // Near matches are not admin.
    for scope in ["Admin", "admin:alert", "admin:alerts:x"] {
        let actor = human_actor(&p, &facts(with("scope", json!(scope))).await).expect("actor");
        assert!(!actor.admin, "{scope}");
    }
    // An ingestion-mapped login is never an actor.
    assert!(
        human_actor(
            &p,
            &facts(with("preferred_username", json!("zbx-ingest"))).await
        )
        .is_none()
    );
}

#[tokio::test]
async fn debug_never_prints_the_key() {
    let cfg = ApiConfig {
        bearer: Some(BearerConfig {
            key: b"synthetic-secret-key-bytes-zz".to_vec(),
            ..bearer_cfg()
        }),
        ..ApiConfig::default()
    };
    let shown = format!("{cfg:?}");
    assert!(!shown.contains("synthetic-secret-key-bytes-zz"));
    assert!(shown.contains("[REDACTED]"));
    let shown = format!("{:?}", cfg.bearer);
    assert!(!shown.contains("synthetic-secret"));
    let mut st = state(Auth::Required, None);
    st.cfg = cfg;
    let st = Arc::new(st);
    assert!(!format!("{st:?}").contains("synthetic-secret"));
}
