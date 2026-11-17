//! `GET /config` through the router (reference `views/config.py:9-74`;
//! `spec/openapi.yaml` `/config`). The route reads no database, so the
//! store's pool is lazy and never connects; these tests need no
//! `DATABASE_URL`.
#![expect(
    clippy::unwrap_used,
    reason = "integration test: helpers unwrap and each scenario stays in one function"
)]

use std::sync::Arc;

use alrust_api::{ApiConfig, AppState, BearerConfig, router};
use alrust_store_pg::{Store, StoreConfig};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{Value, json};
use sqlx::postgres::PgPoolOptions;
use tower::ServiceExt as _;

/// The 36 keys `spec/openapi.yaml` requires, in its order.
const TOP_KEYS: [&str; 36] = [
    "debug",
    "endpoint",
    "alarm_model",
    "auth_required",
    "allow_readonly",
    "readonly_scopes",
    "provider",
    "customer_views",
    "signup_enabled",
    "email_verification",
    "client_id",
    "azure_tenant",
    "aws_region",
    "cognito_domain",
    "github_url",
    "gitlab_url",
    "keycloak_url",
    "keycloak_realm",
    "oidc_auth_url",
    "site_logo_url",
    "severity",
    "colors",
    "timeouts",
    "blackouts",
    "dates",
    "font",
    "audio",
    "columns",
    "sort_by",
    "filter",
    "indicators",
    "actions",
    "tracking_id",
    "refresh_interval",
    "environments",
    "clipboard_template",
];

fn app(cfg: ApiConfig, store_cfg: StoreConfig) -> axum::Router {
    let pool = PgPoolOptions::new()
        .connect_lazy("postgres://unused@127.0.0.1:1/unused")
        .unwrap();
    router(Arc::new(AppState {
        store: Store::new(pool, store_cfg),
        cfg,
    }))
}

async fn get(app: &axum::Router, uri: &str, key: Option<&str>) -> (StatusCode, Value) {
    let mut req = Request::builder().method("GET").uri(uri);
    if let Some(key) = key {
        req = req.header("Authorization", format!("Key {key}"));
    }
    let resp = app
        .clone()
        .oneshot(req.body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

fn keys(v: &Value) -> Vec<String> {
    let mut k: Vec<String> = v.as_object().unwrap().keys().cloned().collect();
    k.sort();
    k
}

fn sorted(names: &[&str]) -> Vec<String> {
    let mut k: Vec<String> = names.iter().map(|s| (*s).to_owned()).collect();
    k.sort();
    k
}

#[tokio::test]
async fn the_keys_and_the_alarm_model_are_the_references() {
    let app = app(ApiConfig::default(), StoreConfig::default());
    let (code, body) = get(&app, "/config", None).await;
    assert_eq!(code, StatusCode::OK, "{body}");
    assert_eq!(keys(&body), sorted(&TOP_KEYS));
    for (key, nested) in [
        (
            "alarm_model",
            &["name", "severity", "colors", "status", "defaults"][..],
        ),
        ("timeouts", &["alert", "heartbeat", "ack", "shelve"][..]),
        ("blackouts", &["duration"][..]),
        ("dates", &["shortTime", "mediumDate", "longDate"][..]),
        ("audio", &["new"][..]),
        ("indicators", &["severity", "queries"][..]),
    ] {
        assert_eq!(keys(&body[key]), sorted(nested), "{key}");
    }
    let model = &body["alarm_model"];
    assert_eq!(model["name"], "Alerta 9.1.0");
    assert_eq!(
        model["defaults"],
        json!({"status": "open", "normal_severity": "normal", "previous_severity": "indeterminate"})
    );
    assert_eq!(
        model["status"],
        json!({"open": "A", "assign": "B", "ack": "C", "shelved": "D",
               "blackout": "E", "closed": "F", "expired": "G", "unknown": "H"})
    );
    assert_eq!(model["severity"].as_object().unwrap().len(), 13);
    assert_eq!(
        (
            model["severity"]["security"].clone(),
            model["severity"]["ok"].clone(),
            model["severity"]["unknown"].clone()
        ),
        (json!(0), json!(7), json!(10))
    );
    assert_eq!(model["colors"]["severity"]["critical"], "red");
    assert_eq!(model["colors"]["text"], "black");
    // The top-level aliases are the model's objects (`config.py:42-43`).
    assert_eq!(body["severity"], model["severity"]);
    assert_eq!(body["colors"], model["colors"]);
    // Defaults, as the conformance session checks them on the reference.
    for key in [
        "debug",
        "customer_views",
        "allow_readonly",
        "signup_enabled",
    ] {
        assert_eq!(body[key], false, "{key}");
    }
    assert_eq!(body["auth_required"], true);
    assert_eq!(body["provider"], "basic");
    assert_eq!(body["client_id"], "");
    assert_eq!(
        body["timeouts"],
        json!({"alert": 86400, "heartbeat": 86400, "ack": 0, "shelve": 7200})
    );
    assert_eq!(body["font"]["font-weight"], 500);
    assert_eq!(body["refresh_interval"], 5000);
    assert_eq!(body["environments"], json!(["Production", "Development"]));
}

#[tokio::test]
async fn alrusts_own_settings_are_reported() {
    let cfg = ApiConfig {
        base_url: "http://alrust.test:9000//".into(),
        auth_required: false,
        allowed_environments: vec!["Prod".into()],
        oauth2_client_id: "web-ui".into(),
        bearer: Some(BearerConfig {
            key: b"synthetic-config-test-key-0123456789".to_vec(),
            audience: "web-ui".into(),
            provider: "keycloak".into(),
            now: || 0,
        }),
        ..ApiConfig::default()
    };
    let store_cfg = StoreConfig {
        alert_timeout: 600,
        ack_timeout: 60,
        shelve_timeout: 120,
        ..StoreConfig::default()
    };
    let app = app(cfg, store_cfg);
    let (_, body) = get(&app, "/config", None).await;
    // `absolute_url().rstrip('/')`: every trailing slash goes.
    assert_eq!(body["endpoint"], "http://alrust.test:9000");
    assert_eq!(body["auth_required"], false);
    assert_eq!(body["environments"], json!(["Prod"]));
    assert_eq!(body["client_id"], "web-ui");
    assert_eq!(body["provider"], "keycloak");
    assert_eq!(
        body["timeouts"],
        json!({"alert": 600, "heartbeat": 600, "ack": 60, "shelve": 120})
    );
}

/// No permission decorator: credentials are not read, so neither a missing
/// nor an invalid key is refused, even with `auth_required`.
#[tokio::test]
async fn credentials_are_ignored() {
    let app = app(ApiConfig::default(), StoreConfig::default());
    let (code, _) = get(&app, "/config", Some("not-a-real-key")).await;
    assert_eq!(code, StatusCode::OK);
    let (code, body) = get(&app, "/config?api-key=not-a-real-key", None).await;
    assert_eq!(code, StatusCode::OK);
    assert_eq!(keys(&body).len(), 36);
}
