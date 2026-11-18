//! HTTP API for alrust (axum), Alerta-compatible surface plus SSE.
//!
//! The alpha covers the core Alerta routes: receive, get, query, delete,
//! status, action, heartbeats, API keys, health and housekeeping. Paths,
//! status codes and JSON shapes follow the reference v9.1.0 views.

use std::fmt;
use std::sync::Arc;

use alrust_domain::bearer::{OperatorFacts, VerifiedToken, verify_bearer};
use alrust_domain::{
    Alert, ApiKey, HistoryOutput, KeySecret, REDACTED_ERROR, RESTRICTED_ATTRIBUTES,
    contains_evidence, is_restricted_attribute, iso8601, parse_time, redact_diagnostic,
    scope_allows, truncate_ms,
};
use alrust_store_pg::aggregates::CubeCounts;
use alrust_store_pg::blackouts::Change;
use alrust_store_pg::topn::{GroupColumn, TopnKind};
use alrust_store_pg::{AlertEdit, AlertQuery, CallerFacts, NewAlert, Store, StoreError};
use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post, put};
use axum::{Json, Router};
use chrono::{Duration, Utc};
use serde_json::{Map, Value, json};

/// Server configuration (environment-driven; see `alrust-server`).
#[derive(Debug, Clone)]
pub struct ApiConfig {
    /// Absolute base URL used in `href` fields.
    pub base_url: String,
    /// Require an API key on every route except health checks.
    pub auth_required: bool,
    /// `ALLOWED_ENVIRONMENTS`: exact (full-match) environment names.
    pub allowed_environments: Vec<String>,
    /// `HEARTBEAT_MAX_LATENCY` in ms.
    pub heartbeat_max_latency_ms: i64,
    /// `HEARTBEAT_EVENTS`.
    pub heartbeat_events: Vec<String>,
    /// `DEFAULT_PAGE_SIZE`.
    pub default_page_size: i64,
    /// Default origin for alerts/heartbeats that omit it.
    pub default_origin: String,
    /// `DELETE_EXPIRED_AFTER` / `DELETE_INFO_AFTER`, in seconds.
    pub delete_expired_after_s: i64,
    pub delete_info_after_s: i64,
    /// Operator Bearer verification (spec/oidc-caller-facts.md sections 3
    /// to 6). `None`, the default, is a deliberately incomplete staging seam:
    /// a Bearer header is then ignored exactly as before this field existed.
    /// That is not the settled credential order of section 3 (O1), which
    /// applies once a verifier is configured; configuring one from the
    /// server's settings is a separate, later slice.
    pub bearer: Option<BearerConfig>,
    /// `OAUTH2_CLIENT_ID` as configured (empty when unset), for `GET /config`.
    pub oauth2_client_id: String,
}

/// The verifier inputs for operator Bearer tokens: the signing key, the
/// expected audience, the configured OIDC provider name and the clock, all
/// injected. The key is never printed.
#[derive(Clone)]
pub struct BearerConfig {
    pub key: Vec<u8>,
    pub audience: String,
    pub provider: String,
    /// Whole seconds since the epoch, rounded down.
    pub now: fn() -> i64,
}

impl fmt::Debug for BearerConfig {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("BearerConfig")
            .field("key", &"[REDACTED]")
            .field("audience", &self.audience)
            .field("provider", &self.provider)
            .finish_non_exhaustive()
    }
}

/// The system clock for [`BearerConfig::now`].
pub fn system_now() -> i64 {
    Utc::now().timestamp()
}

impl Default for ApiConfig {
    fn default() -> Self {
        ApiConfig {
            base_url: "http://localhost:8080".into(),
            auth_required: true,
            allowed_environments: vec!["Production".into(), "Development".into()],
            heartbeat_max_latency_ms: 2000,
            heartbeat_events: vec!["Heartbeat".into()],
            default_page_size: 1000,
            default_origin: "alrust".into(),
            delete_expired_after_s: 7200,
            delete_info_after_s: 43200,
            bearer: None,
            oauth2_client_id: String::new(),
        }
    }
}

#[derive(Debug)]
pub struct AppState {
    pub store: Store,
    pub cfg: ApiConfig,
}

type Shared = Arc<AppState>;

/// An error in the reference's JSON shape.
#[derive(Debug)]
pub struct ApiError {
    code: StatusCode,
    message: String,
}

impl ApiError {
    pub fn new(code: StatusCode, message: impl Into<String>) -> Self {
        ApiError {
            code,
            message: message.into(),
        }
    }
    fn bad(message: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_REQUEST, message)
    }
}

impl IntoResponse for ApiError {
    /// The message is the only caller- or database-derived string in an
    /// error body; one that may carry restricted evidence becomes
    /// `REDACTED_ERROR` (hook spec section 4.2), the status code unchanged.
    fn into_response(self) -> Response {
        let message = if contains_evidence(&self.message) {
            REDACTED_ERROR.to_owned()
        } else {
            self.message
        };
        let body = json!({"status": "error", "message": message, "code": self.code.as_u16(), "errors": null});
        (self.code, Json(body)).into_response()
    }
}

impl From<StoreError> for ApiError {
    fn from(e: StoreError) -> Self {
        match e {
            StoreError::NotFound => ApiError::new(StatusCode::NOT_FOUND, "not found"),
            StoreError::Transition(alrust_engine::TransitionError::InvalidAction(m)) => {
                ApiError::new(StatusCode::CONFLICT, m)
            }
            StoreError::Transition(t) => {
                ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, t.to_string())
            }
            StoreError::Db(e) => {
                tracing::error!(error = %redact_diagnostic(e.to_string()), "database error");
                ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "database error")
            }
            // Input ranges are refused with 400 before the store; this is a
            // stored value that cannot be written back, such as a duplicate
            // count at the integer maximum. Nothing was written.
            // A hook's own status and message (spec/integration-hooks.md
            // sections 3 and 5): a refusal before COMMIT, or an unknown
            // outcome after it; the response layer still redacts evidence.
            StoreError::Hook(h) | StoreError::OutcomeUnknown(h) => ApiError::new(
                StatusCode::from_u16(h.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
                h.message,
            ),
            StoreError::OutOfRange(what) => {
                tracing::error!(field = what, "value outside the reference integer range");
                ApiError::new(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    format!("{what} is outside the integer range of the database"),
                )
            }
        }
    }
}

type ApiResult<T> = Result<T, ApiError>;

/// The authenticated caller.
#[derive(Debug, Clone)]
struct Caller {
    /// `g.login`; `None` for an unauthenticated request with auth disabled.
    login: Option<String>,
    scopes: Vec<String>,
    customer: Option<String>,
    /// `AUTH_REQUIRED=False` with no credential: the route permission check
    /// is skipped, but the caller holds **no** scopes (`auth/decorators.py:156-161`).
    auth_off: bool,
    /// Hook-only facts of a verified operator; `None` for every API-key and
    /// auth-off caller, and for an operator whose token fails a section 6
    /// check. Only [`Caller::from_bearer`] sets it.
    operator: Option<OperatorFacts>,
}

impl Caller {
    /// A verified operator (spec section 5): the token's login and scopes,
    /// no customer (no customer views, so nothing is filtered), and the
    /// hook-only facts for the configured provider (section 6).
    fn from_bearer(token: &VerifiedToken, provider: &str) -> Caller {
        Caller {
            login: token.login().map(str::to_owned),
            scopes: token.scopes(),
            customer: None,
            auth_off: false,
            operator: token.operator_facts(provider),
        }
    }

    fn require(&self, scope: &str) -> ApiResult<()> {
        if self.auth_off || scope_allows(&self.scopes, scope) {
            Ok(())
        } else {
            Err(ApiError::new(
                StatusCode::FORBIDDEN,
                format!("Missing required scope: {scope}"),
            ))
        }
    }

    /// The facts the integration hooks receive. API keys carry no user id
    /// and no verified auth type; a verified operator whose token passes
    /// every section 6 check carries its hook-only facts, and any other
    /// caller gets the API-key shape.
    fn facts(&self, st: &AppState) -> CallerFacts {
        let mut facts = CallerFacts {
            login: self.login.clone(),
            user_id: None,
            auth_type: None,
            scopes: self.scopes.clone(),
            customers: self.customers(),
            auth_required: st.cfg.auth_required,
            request_id: None,
            from_request: true,
        };
        // An operator's login is already `self.login`: both are the token's
        // `preferred_username`.
        if let Some(op) = &self.operator {
            facts.user_id = Some(op.user_id().to_owned());
            facts.auth_type = Some(op.auth_type().to_owned());
            facts.customers = Some(op.customers().to_vec());
        }
        facts
    }

    /// `g.customers`: the key's customer, if any.
    fn customers(&self) -> Option<Vec<String>> {
        self.customer.as_ref().map(|c| vec![c.clone()])
    }

    fn login(&self) -> &str {
        self.login.as_deref().unwrap_or("")
    }

    /// `assign_customer(wanted, permission)` (`utils/api.py:14-28`,
    /// customer-mapping C5-C11). The override test is exact list membership
    /// of `admin` or `permission` (C6), not the scope hierarchy.
    fn assign_customer(
        &self,
        wanted: Option<String>,
        permission: &str,
    ) -> ApiResult<Option<String>> {
        let customers: Vec<String> = self.customer.iter().cloned().collect();
        match wanted {
            Some(w) => {
                if self.scopes.iter().any(|s| s == "admin" || s == permission)
                    || customers.contains(&w)
                {
                    Ok(Some(w))
                } else {
                    Err(ApiError::bad(format!(
                        "not allowed to set customer to '{w}'"
                    )))
                }
            }
            None => Ok(customers.into_iter().next()),
        }
    }
}

fn key_from_request(headers: &HeaderMap, params: &[(String, String)]) -> Option<String> {
    if let Some(v) = headers.get("authorization").and_then(|v| v.to_str().ok())
        && let Some(k) = v.strip_prefix("Key ")
    {
        return Some(k.trim().to_string());
    }
    if let Some(v) = headers.get("x-api-key").and_then(|v| v.to_str().ok()) {
        return Some(v.trim().to_string());
    }
    params
        .iter()
        .find(|(k, _)| k == "api-key")
        .map(|(_, v)| v.clone())
}

/// The credential a request presents, in the reference's order
/// (`auth/decorators.py:30-91`; spec section 3).
/// No `Debug`: the Bearer variant holds a raw token.
enum Credential<'a> {
    /// An API key from any of its three carriers, as before.
    Key(String),
    /// A recognised Bearer token, only when a verifier is configured.
    Bearer(String, &'a BearerConfig),
    /// Neither.
    Absent,
}

/// API-key carriers first, exactly as before; then, only with a configured
/// verifier, a recognised Bearer header.
fn select_credential<'a>(
    headers: &HeaderMap,
    params: &[(String, String)],
    bearer: Option<&'a BearerConfig>,
) -> Credential<'a> {
    if let Some(k) = key_from_request(headers, params) {
        return Credential::Key(k);
    }
    match (bearer, bearer_token(headers)) {
        (Some(cfg), Some(token)) => Credential::Bearer(token, cfg),
        _ => Credential::Absent,
    }
}

/// The reference's `re.match(r'Bearer (\S+)', auth_header)`
/// (`auth/decorators.py:70-72`): the `Authorization` value starts with
/// exactly `Bearer` and one space, then at least one non-whitespace
/// character; the token is that first run, and the rest is ignored. The
/// value is read as Latin-1, as the reference's server decodes header bytes,
/// so a run may hold non-ASCII characters (which the decoding profile then
/// refuses); `\s` there also matches U+0085 and U+00A0.
fn bearer_token(headers: &HeaderMap) -> Option<String> {
    let value = headers.get("authorization")?.as_bytes();
    let rest = value.strip_prefix(b"Bearer ")?;
    let token: String = rest
        .iter()
        .map(|&b| char::from(b))
        .take_while(|c| !c.is_whitespace())
        .collect();
    (!token.is_empty()).then_some(token)
}

/// A presented key is checked before `AUTH_REQUIRED` (C1); only the secret
/// authenticates (SF-MGMT S4), never the public id. A recognised Bearer
/// token is verified before `AUTH_REQUIRED` too, so an invalid one is a 401
/// either way (spec section 3).
async fn authenticate(
    st: &AppState,
    headers: &HeaderMap,
    params: &[(String, String)],
) -> ApiResult<Caller> {
    match select_credential(headers, params, st.cfg.bearer.as_ref()) {
        Credential::Key(k) => match st.store.authenticate_key(&k).await? {
            Some(key) => Ok(Caller {
                login: Some(key.user),
                // authenticate_key matches only keys with scopes. The use is
                // recorded before any scope check, so a 403 still counts, as
                // in the reference's verify_key.
                scopes: key.scopes.unwrap_or_default(),
                customer: key.customer,
                auth_off: false,
                operator: None,
            }),
            None => Err(ApiError::new(
                StatusCode::UNAUTHORIZED,
                format!("API key parameter '{k}' is invalid"),
            )),
        },
        Credential::Bearer(token, cfg) => bearer_caller(cfg, &token),
        Credential::Absent => no_credential(&st.cfg),
    }
}

/// A recognised Bearer token: the verified operator, or a 401 with one of
/// the three fixed messages (spec section 4; DECISIONS Y50, Y51).
fn bearer_caller(cfg: &BearerConfig, token: &str) -> ApiResult<Caller> {
    verify_bearer(token, &cfg.key, &cfg.audience, (cfg.now)())
        .map(|t| Caller::from_bearer(&t, &cfg.provider))
        .map_err(|e| ApiError::new(StatusCode::UNAUTHORIZED, e.message()))
}

/// No credential: the auth-off caller, or the reference's 401.
fn no_credential(cfg: &ApiConfig) -> ApiResult<Caller> {
    if cfg.auth_required {
        Err(ApiError::new(
            StatusCode::UNAUTHORIZED,
            "Missing authorization API Key or Bearer Token",
        ))
    } else {
        Ok(Caller {
            login: None,
            scopes: Vec::new(),
            customer: None,
            auth_off: true,
            operator: None,
        })
    }
}

/// The `customer` field of a submitted object: absent/null → `None`;
/// `""` → 400 (C5); any other string → `Some`.
fn wanted_customer(obj: &Map<String, Value>) -> ApiResult<Option<String>> {
    match obj.get("customer") {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) if s.is_empty() => {
            Err(ApiError::bad("customer must not be an empty string"))
        }
        Some(Value::String(s)) => Ok(Some(s.clone())),
        Some(_) => Err(ApiError::bad("customer must be a string")),
    }
}

/// Error bodies are read whole up to this size to be checked; a larger one is
/// replaced, since it cannot be checked.
const ERROR_BODY_LIMIT: usize = 1 << 20;

/// Every error response, including the plain-text rejections axum writes
/// itself outside `ApiError` (for example a JSON body it cannot parse, whose
/// message can quote the offending key): one that may carry restricted
/// evidence becomes the `REDACTED_ERROR` error body with the same status code
/// (hook spec section 4.2, after Zabalack's `redact_error_response`,
/// `zab-al-ack@eaf2acf:src/zabalack/plugins.py:140-146`). Other responses
/// pass unchanged.
async fn redact_error_body(r: Response) -> Response {
    let status = r.status();
    if !(status.is_client_error() || status.is_server_error()) {
        return r;
    }
    let (parts, body) = r.into_parts();
    match axum::body::to_bytes(body, ERROR_BODY_LIMIT).await {
        Ok(bytes) if !contains_evidence(&String::from_utf8_lossy(&bytes)) => {
            Response::from_parts(parts, axum::body::Body::from(bytes))
        }
        _ => ApiError::new(status, REDACTED_ERROR).into_response(),
    }
}

pub fn router(state: Shared) -> Router {
    Router::new()
        .route("/", get(index))
        .route("/config", get(client_config))
        .route("/alert", post(receive))
        .route("/alert/{id}", get(get_alert).delete(delete_alert))
        .route("/alert/{id}/status", put(set_status))
        .route("/alert/{id}/action", put(take_action))
        .route("/alert/{id}/tag", put(tag_alert))
        .route("/alert/{id}/untag", put(untag_alert))
        .route("/alert/{id}/attributes", put(update_attributes))
        .route("/alerts", get(search_alerts))
        .route("/alerts/count", get(count_alerts))
        .route("/webhooks/{webhook}", get(webhook).post(webhook))
        .route("/webhooks/{webhook}/{*path}", get(webhook).post(webhook))
        .route("/heartbeat", post(create_heartbeat))
        .route(
            "/heartbeat/{id}",
            get(get_heartbeat).delete(delete_heartbeat),
        )
        .route("/heartbeats", get(list_heartbeats))
        .route("/blackout", post(create_blackout))
        .route(
            "/blackout/{id}",
            get(get_blackout)
                .put(update_blackout)
                .delete(delete_blackout),
        )
        .route("/blackouts", get(list_blackouts))
        .route("/key", post(create_key))
        .route("/keys", get(list_keys))
        .route("/key/{id}", axum::routing::delete(delete_key))
        .route("/management/gtg", get(gtg))
        .route("/management/healthcheck", get(healthcheck))
        .route(
            "/management/housekeeping",
            get(housekeeping).post(housekeeping),
        )
        .route("/environments", get(list_environments))
        .route("/services", get(list_services))
        .route("/alerts/groups", get(list_alert_groups))
        .route("/alerts/tags", get(list_alert_tags))
        .route("/alerts/top10/count", get(topn_count))
        .route("/alerts/topn/count", get(topn_count))
        .route("/alerts/top10/flapping", get(topn_flapping))
        .route("/alerts/topn/flapping", get(topn_flapping))
        .route("/alerts/top10/standing", get(topn_standing))
        .route("/alerts/topn/standing", get(topn_standing))
        .layer(axum::middleware::map_response(redact_error_body))
        .with_state(state)
}

type Params = Query<Vec<(String, String)>>;

async fn index(State(st): State<Shared>) -> Json<Value> {
    Json(json!({
        "status": "ok",
        "name": "alrust",
        "version": env!("CARGO_PKG_VERSION"),
        "uri": st.cfg.base_url,
    }))
}

/// The `GET /config` values alrust reads no setting for: the reference
/// defaults (`settings.py`). Built in parts, because one `json!` call this
/// size exceeds the macro's recursion limit.
fn config_reference_defaults() -> Map<String, Value> {
    let dates = json!({
        "shortTime": "HH:mm",
        "mediumDate": "ddd D MMM HH:mm",
        "longDate": "ddd D MMM, YYYY HH:mm:ss.SSS Z",
    });
    let font = json!({
        "font-family": "\"Sintony\", Arial, sans-serif",
        "font-size": "13px",
        "font-weight": 500,
    });
    let columns = json!([
        "severity",
        "status",
        "lastReceiveTime",
        "timeoutLeft",
        "duplicateCount",
        "customer",
        "environment",
        "service",
        "resource",
        "event",
        "value",
        "text",
    ]);
    let indicators = json!({
        "severity": ["critical", "major", "minor", "warning", "indeterminate", "informational"],
        "queries": [
            {"text": "Production", "query": [["environment", "Production"]]},
            {"text": "Development", "query": [["environment", "Development"]]},
            {"text": "Heartbeats", "query": {"q": "event:Heartbeat"}},
            {"text": "Misc.", "query": "group=Misc"},
        ],
    });
    let providers = json!({
        "azure_tenant": "common",
        "aws_region": "us-east-1",
        "cognito_domain": null,
        "github_url": "https://github.com",
        "gitlab_url": "https://gitlab.com",
        "keycloak_url": null,
        "keycloak_realm": null,
        "oidc_auth_url": null,
        "site_logo_url": "",
    });
    let mut out = match providers {
        Value::Object(m) => m,
        _ => Map::new(),
    };
    for (key, value) in [
        (
            "blackouts",
            json!({"duration": alrust_store_pg::blackouts::BLACKOUT_DURATION}),
        ),
        ("dates", dates),
        ("font", font),
        ("audio", json!({"new": null})),
        ("columns", columns),
        ("sort_by", json!(["severity", "lastReceiveTime"])),
        ("filter", json!({"status": ["open", "ack"]})),
        ("indicators", indicators),
        ("actions", json!([])),
        ("tracking_id", Value::Null),
        ("refresh_interval", json!(5000)),
        ("clipboard_template", json!("")),
    ] {
        out.insert(key.to_owned(), value);
    }
    out
}

/// `GET /config` (`views/config.py:9-74`; `spec/openapi.yaml` `/config`):
/// the web UI's bootstrap settings, a bare object with the reference's 36
/// keys. No permission check: credentials are not read, so an invalid key
/// is not refused.
///
/// - alrust's own settings: `endpoint`, `auth_required`, `environments`,
///   `client_id`, `provider` (when set) and the timeouts (`heartbeat` is the
///   alert timeout, which alrust applies to a heartbeat without one).
/// - What alrust does not implement, reported as off: `debug`,
///   `allow_readonly`, `customer_views` and `signup_enabled` (signup is
///   out of scope).
/// - Everything else is the reference's default (`settings.py`), which
///   alrust does not read from its own settings.
async fn client_config(State(st): State<Shared>) -> Json<Value> {
    use alrust_domain::alarm_model;
    let store = st.store.config();
    let severity = alarm_model::severity_map();
    let colors = alarm_model::colors();
    // An empty AUTH_PROVIDER is alrust's default; the reference's is basic.
    let provider = st
        .cfg
        .bearer
        .as_ref()
        .map(|b| b.provider.as_str())
        .filter(|p| !p.is_empty())
        .unwrap_or("basic");
    let alarm_model = json!({
        "name": alarm_model::NAME,
        "severity": severity,
        "colors": colors,
        "status": alarm_model::status_map(),
        "defaults": {
            "status": alrust_domain::status::OPEN,
            "normal_severity": alrust_domain::NORMAL_SEVERITY,
            "previous_severity": alrust_domain::DEFAULT_PREVIOUS_SEVERITY,
        },
    });
    let mut body = json!({
        "debug": false,
        "endpoint": st.cfg.base_url.trim_end_matches('/'),
        "alarm_model": alarm_model,
        "auth_required": st.cfg.auth_required,
        "allow_readonly": false,
        "readonly_scopes": ["read"],
        "provider": provider,
        "customer_views": false,
        "signup_enabled": false,
        "email_verification": false,
        "client_id": st.cfg.oauth2_client_id,
        "severity": severity,
        "colors": colors,
        "timeouts": {
            "alert": store.alert_timeout,
            "heartbeat": store.alert_timeout,
            "ack": store.ack_timeout,
            "shelve": store.shelve_timeout,
        },
        "environments": st.cfg.allowed_environments,
    });
    if let Value::Object(top) = &mut body {
        top.extend(config_reference_defaults());
    }
    Json(body)
}

// ---------------------------------------------------------------- alerts

fn str_list(v: Option<&Value>, name: &str) -> ApiResult<Vec<String>> {
    match v {
        None | Some(Value::Null) => Ok(Vec::new()),
        Some(Value::Array(a)) => a
            .iter()
            .map(|x| match x {
                Value::String(s) => Ok(s.clone()),
                other => Ok(other.to_string()),
            })
            .collect(),
        Some(_) => Err(ApiError::bad(format!("{name} must be a list"))),
    }
}

fn opt_str(v: Option<&Value>) -> Option<String> {
    match v {
        Some(Value::String(s)) if !s.is_empty() => Some(s.clone()),
        _ => None,
    }
}

/// The largest timeout the reference's `integer` columns hold.
#[expect(
    clippy::as_conversions,
    reason = "widening i32::MAX to i64 loses nothing"
)]
pub const MAX_TIMEOUT: i64 = i32::MAX as i64;

/// `timeout` parsing (spec R0.4): null/absent → default; bool → 1/0;
/// negative → 400; strings and floats → 400. Above `MAX_TIMEOUT` → 400, a
/// documented divergence (ADR-0005 decision 6).
fn parse_timeout(v: Option<&Value>, default: i64) -> ApiResult<i64> {
    match v {
        None | Some(Value::Null) => Ok(default),
        Some(Value::Bool(b)) => Ok(i64::from(*b)),
        Some(Value::Number(n)) => match n.as_i64() {
            Some(t) if t > MAX_TIMEOUT => Err(ApiError::bad(format!(
                "timeout must be an integer less than or equal to {MAX_TIMEOUT}"
            ))),
            Some(t) if t >= 0 => Ok(t),
            Some(_) => Err(ApiError::bad(
                "timeout must be an integer greater than or equal to 0",
            )),
            None if n.is_u64() => Err(ApiError::bad(format!(
                "timeout must be an integer less than or equal to {MAX_TIMEOUT}"
            ))),
            None => Err(ApiError::bad("timeout must be an integer")),
        },
        Some(_) => Err(ApiError::bad("timeout must be an integer")),
    }
}

fn parse_attributes(v: Option<&Value>) -> ApiResult<Map<String, Value>> {
    match v {
        None | Some(Value::Null) => Ok(Map::new()),
        Some(Value::Object(m)) => {
            if m.keys().any(|k| k.contains('.') || k.contains('$')) {
                return Err(ApiError::bad(
                    "Attribute keys must not contain \".\" or \"$\"",
                ));
            }
            Ok(m.clone())
        }
        Some(_) => Err(ApiError::bad("attributes must be a JSON object")),
    }
}

fn parse_alert(body: &Value, st: &AppState, alert_timeout: i64) -> ApiResult<NewAlert> {
    let Some(obj) = body.as_object() else {
        return Err(ApiError::bad("alert must be a JSON object"));
    };
    let resource = opt_str(obj.get("resource"))
        .ok_or_else(|| ApiError::bad("Missing mandatory value for 'resource'"))?;
    let event = opt_str(obj.get("event"))
        .ok_or_else(|| ApiError::bad("Missing mandatory value for 'event'"))?;
    let attributes = parse_attributes(obj.get("attributes"))?;
    let timeout = parse_timeout(obj.get("timeout"), alert_timeout)?;
    let mut correlate = str_list(obj.get("correlate"), "correlate")?;
    if !correlate.is_empty() && !correlate.contains(&event) {
        correlate.push(event.clone());
    }
    // An integer value is stored as a string; JSON booleans as "True"/"False"
    // (spec R0.3). A float, array or object is kept as JSON here and the
    // store writes its jsonb text form (ADR-0005 decision 8 (b), Y38).
    let value = match obj.get("value") {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) => Some(Value::String(s.clone())),
        Some(Value::Bool(b)) => Some(Value::String(if *b { "True" } else { "False" }.to_string())),
        Some(Value::Number(n)) if n.is_i64() || n.is_u64() => Some(Value::String(n.to_string())),
        Some(other) => Some(other.clone()),
    };
    let create_time = match obj.get("createTime") {
        Some(Value::String(s)) => truncate_ms(parse_time(s).ok_or_else(|| {
            ApiError::bad("dates must be ISO 8601 date format YYYY-MM-DDThh:mm:ss.sssZ")
        })?),
        _ => truncate_ms(Utc::now()),
    };
    let raw_data = match obj.get("rawData") {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) => Some(s.clone()),
        Some(other) => Some(other.to_string()),
    };
    Ok(NewAlert {
        id: opt_str(obj.get("id")).unwrap_or_else(|| uuid::Uuid::new_v4().to_string()),
        resource,
        event,
        environment: opt_str(obj.get("environment")).unwrap_or_default(),
        severity: opt_str(obj.get("severity")).unwrap_or_else(|| "normal".into()),
        correlate,
        status: opt_str(obj.get("status")).unwrap_or_else(|| "open".into()),
        service: str_list(obj.get("service"), "service")?,
        group: opt_str(obj.get("group")).unwrap_or_else(|| "Misc".into()),
        value,
        text: opt_str(obj.get("text")).unwrap_or_default(),
        tags: str_list(obj.get("tags"), "tags")?,
        attributes,
        origin: opt_str(obj.get("origin")).unwrap_or_else(|| st.cfg.default_origin.clone()),
        event_type: opt_str(obj.get("type")).unwrap_or_else(|| "exceptionAlert".into()),
        create_time,
        timeout,
        raw_data,
        customer: wanted_customer(obj)?,
    })
}

fn alert_json(st: &AppState, a: &Alert) -> Value {
    serde_json::to_value(a.body(
        &st.cfg.base_url,
        st.store.config().alert_timeout,
        HistoryOutput::Include,
    ))
    .unwrap_or(Value::Null)
}

async fn receive(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Json(body): Json<Value>,
) -> ApiResult<Response> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("write:alerts")?;
    let n = parse_alert(&body, &st, st.store.config().alert_timeout)?;
    match process_alert(&st, &caller, n).await? {
        Received::Alert(alert) => Ok((
            StatusCode::CREATED,
            Json(json!({"status": "ok", "id": alert.id, "alert": alert_json(&st, &alert)})),
        )
            .into_response()),
        Received::Heartbeat(id) => Ok(heartbeat_accepted(&id)),
    }
}

/// What the receive path did with one incoming alert.
enum Received {
    Alert(Box<Alert>),
    /// A heartbeat event became this heartbeat (spec HB3.1).
    Heartbeat(String),
}

/// The 202 answer for an alert converted to a heartbeat.
const HEARTBEAT_MESSAGE: &str = "Alert converted to Heartbeat";

fn heartbeat_accepted(id: &str) -> Response {
    let msg = HEARTBEAT_MESSAGE;
    (
        StatusCode::ACCEPTED,
        Json(json!({"status": "ok", "message": msg, "id": id})),
    )
        .into_response()
}

/// The receive path after parsing, shared by `POST /alert` and the
/// webhooks (the reference's `assign_customer` and `process_alert`).
async fn process_alert(st: &AppState, caller: &Caller, mut n: NewAlert) -> ApiResult<Received> {
    n.customer = caller.assign_customer(n.customer.take(), "admin:alerts")?;

    // Reject policy (spec R1.2), unless the alert arrives already in blackout.
    if n.status != "blackout" {
        if !st
            .cfg
            .allowed_environments
            .iter()
            .any(|e| e == &n.environment)
        {
            return Err(ApiError::new(
                StatusCode::FORBIDDEN,
                format!(
                    "[POLICY] Alert environment does not match one of {}",
                    st.cfg.allowed_environments.join(", ")
                ),
            ));
        }
        if n.service.is_empty() {
            return Err(ApiError::new(
                StatusCode::FORBIDDEN,
                "[POLICY] Alert must define a service",
            ));
        }
        // Heartbeat alerts become heartbeats (spec HB3.1).
        if st.cfg.heartbeat_events.contains(&n.event) {
            let mut attrs = Map::new();
            attrs.insert("environment".into(), Value::String(n.environment.clone()));
            attrs.insert("severity".into(), Value::String(n.severity.clone()));
            attrs.insert("service".into(), json!(n.service));
            attrs.insert("group".into(), Value::String(n.group.clone()));
            let hb = st
                .store
                .upsert_heartbeat(
                    &uuid::Uuid::new_v4().to_string(),
                    &n.origin,
                    &n.tags,
                    &attrs,
                    "Heartbeat",
                    truncate_ms(Utc::now()),
                    n.timeout,
                    n.customer.as_deref(),
                )
                .await?;
            return Ok(Received::Heartbeat(hb.id));
        }
    }

    let (alert, _kind) = st.store.receive_with(n, &caller.facts(st)).await?;
    Ok(Received::Alert(Box::new(alert)))
}

/// The reference's `DEFAULT_ENVIRONMENT` (`settings.py:298`), used by the
/// webhooks when a source names no environment.
const WEBHOOK_DEFAULT_ENVIRONMENT: &str = "Production";

/// One webhook item's outcome (SF-WEBHOOKS alrust item 2, `results`).
struct ItemOutcome {
    index: usize,
    input_id: Option<String>,
    resource_id: Option<String>,
    outcome: &'static str,
    code: StatusCode,
    message: Option<String>,
    /// The reference's response for this item alone.
    response: Value,
    alert: Option<Alert>,
}

impl ItemOutcome {
    fn invalid(index: usize) -> Self {
        let message = format!("Invalid alert at index {index} in webhook payload.");
        ItemOutcome {
            index,
            input_id: None,
            resource_id: None,
            outcome: "invalid",
            code: StatusCode::BAD_REQUEST,
            response: error_body(StatusCode::BAD_REQUEST, &message),
            message: Some(message),
            alert: None,
        }
    }

    fn result(&self) -> Value {
        json!({
            "index": self.index,
            "inputId": self.input_id,
            "resourceId": self.resource_id,
            "outcome": self.outcome,
            "code": self.code.as_u16(),
            "message": self.message,
        })
    }
}

/// The API's error body, with the same redaction as `ApiError`.
fn error_body(code: StatusCode, message: &str) -> Value {
    let message = if contains_evidence(message) {
        REDACTED_ERROR
    } else {
        message
    };
    json!({"status": "error", "message": message, "code": code.as_u16(), "errors": null})
}

/// `POST /webhooks/<webhook>[/<path>]` (reference `webhooks/custom.py`, with
/// SF-WEBHOOKS): only `prometheus` is implemented (DECISIONS Y27). Every
/// item is mapped and processed on its own (Y10); see `webhook_response`
/// for the answer.
async fn webhook(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(p): Path<std::collections::HashMap<String, String>>,
    body: axum::body::Bytes,
) -> ApiResult<Response> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("write:webhooks")?;
    let name = p.get("webhook").map_or("", String::as_str);
    let items = match name {
        "prometheus" => alrust_domain::prometheus::parse(&body, WEBHOOK_DEFAULT_ENVIRONMENT)
            .map_err(|e| ApiError::bad(e.message))?,
        _ => {
            return Err(ApiError::new(
                StatusCode::NOT_FOUND,
                format!("Custom webhook '{name}' not found."),
            ));
        }
    };
    let alert_timeout = st.store.config().alert_timeout;
    let mut outcomes = Vec::with_capacity(items.len());
    for (index, item) in items.into_iter().enumerate() {
        let parsed = match item {
            alrust_domain::prometheus::Item::Mapped(o) => {
                parse_alert(&Value::Object(o), &st, alert_timeout).ok()
            }
            alrust_domain::prometheus::Item::Invalid => None,
        };
        let Some(n) = parsed else {
            outcomes.push(ItemOutcome::invalid(index));
            continue;
        };
        let input_id = Some(n.id.clone());
        outcomes.push(match process_alert(&st, &caller, n).await {
            Ok(Received::Alert(a)) => ItemOutcome {
                index,
                input_id,
                resource_id: Some(a.id.clone()),
                outcome: "processed",
                code: StatusCode::CREATED,
                message: None,
                response: json!({"status": "ok", "id": a.id, "alert": alert_json(&st, &a)}),
                alert: Some(*a),
            },
            Ok(Received::Heartbeat(id)) => ItemOutcome {
                index,
                input_id,
                response: json!({"status": "ok", "message": HEARTBEAT_MESSAGE, "id": id}),
                resource_id: Some(id),
                outcome: "heartbeat",
                code: StatusCode::ACCEPTED,
                message: Some(HEARTBEAT_MESSAGE.into()),
                alert: None,
            },
            Err(e) => ItemOutcome {
                index,
                input_id,
                resource_id: None,
                outcome: if e.code == StatusCode::FORBIDDEN {
                    "rejected"
                } else {
                    "error"
                },
                code: e.code,
                response: error_body(e.code, &e.message),
                message: Some(e.message),
                alert: None,
            },
        });
    }
    Ok(webhook_response(&st, &outcomes))
}

/// SF-WEBHOOKS alrust item 2, "Response": the reference's answer when every
/// item was processed, or for a batch of one; otherwise the reference's
/// answer for the first item not processed, plus `results`.
fn webhook_response(st: &AppState, outcomes: &[ItemOutcome]) -> Response {
    let first_failed = outcomes.iter().position(|o| o.outcome != "processed");
    let (code, mut body) = match (outcomes, first_failed) {
        ([one], None) => (
            StatusCode::CREATED,
            json!({"status": "ok", "id": one.resource_id, "alert": one.alert.as_ref().map(|a| alert_json(st, a))}),
        ),
        (_, None) => (
            StatusCode::CREATED,
            json!({"status": "ok", "ids": outcomes.iter().map(|o| o.resource_id.clone()).collect::<Vec<_>>()}),
        ),
        ([one], Some(_)) => (one.code, one.response.clone()),
        (all, Some(i)) => (all[i].code, all[i].response.clone()),
    };
    if outcomes.len() > 1
        && first_failed.is_some()
        && let Some(obj) = body.as_object_mut()
    {
        obj.insert(
            "results".into(),
            Value::Array(outcomes.iter().map(ItemOutcome::result).collect()),
        );
    }
    (code, Json(body)).into_response()
}

async fn get_alert(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("read:alerts")?;
    let a = st
        .store
        .get_alert(&id, caller.customers().as_deref())
        .await?;
    Ok(Json(
        json!({"status": "ok", "total": 1, "alert": alert_json(&st, &a)}),
    ))
}

async fn delete_alert(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("write:alerts")?;
    st.store
        .delete_alert(&id, caller.customers().as_deref())
        .await?;
    Ok(Json(json!({"status": "ok"})))
}

async fn set_status(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
    Json(body): Json<Value>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("write:alerts")?;
    let status = opt_str(body.get("status"))
        .ok_or_else(|| ApiError::bad("must supply 'status' as json data"))?;
    let text = body.get("text").and_then(Value::as_str).unwrap_or("");
    let timeout = match body.get("timeout") {
        None | Some(Value::Null) => None,
        v => Some(parse_timeout(v, 0)?),
    };
    st.store
        .set_status(
            &id,
            caller.customers().as_deref(),
            &status,
            text,
            timeout,
            caller.login(),
        )
        .await?;
    Ok(Json(json!({"status": "ok"})))
}

/// `PUT /alert/<id>/tag` and `/untag` (`views/alerts.py:194-245`): a
/// non-empty `tags` list is required (400 otherwise, the reference's
/// message); the alert is found by id or id prefix within the caller's
/// customers (404 otherwise).
async fn edit_tags(
    st: &AppState,
    headers: &HeaderMap,
    params: &[(String, String)],
    id: &str,
    body: &Value,
    edit: fn(Vec<String>) -> AlertEdit,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(st, headers, params).await?;
    caller.require("write:alerts")?;
    // Only a non-empty list of strings: a non-list, an empty list or a
    // non-string element is 400. The reference rejects only an absent or
    // empty value before SQL; other values reach its SQL, with outcomes that
    // vary by type.
    let tags: Vec<String> = match body.get("tags") {
        Some(Value::Array(a)) if !a.is_empty() && a.iter().all(Value::is_string) => a
            .iter()
            .filter_map(|v| v.as_str().map(str::to_string))
            .collect(),
        _ => return Err(ApiError::bad("must supply 'tags' as json list")),
    };
    st.store
        .edit_alert(id, caller.customers().as_deref(), edit(tags))
        .await?;
    Ok(Json(json!({"status": "ok"})))
}

async fn tag_alert(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
    Json(body): Json<Value>,
) -> ApiResult<Json<Value>> {
    edit_tags(&st, &headers, &params, &id, &body, AlertEdit::Tag).await
}

async fn untag_alert(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
    Json(body): Json<Value>,
) -> ApiResult<Json<Value>> {
    edit_tags(&st, &headers, &params, &id, &body, AlertEdit::Untag).await
}

/// `PUT /alert/<id>/attributes` (`views/alerts.py:248-272`): a non-empty
/// `attributes` object is required (400 otherwise, including the list of
/// pairs the reference's `dict.update` would accept); keys are merged into
/// the stored attributes and a null value removes its key. A successful
/// update that leaves no attributes is 200; the reference stores it and then
/// answers 500 "failed to update attributes" (DECISIONS Y34).
async fn update_attributes(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
    Json(body): Json<Value>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("write:alerts")?;
    let attrs = match body.get("attributes") {
        Some(Value::Object(m)) if !m.is_empty() => m.clone(),
        _ => return Err(ApiError::bad("must supply 'attributes' as json data")),
    };
    st.store
        .edit_alert(
            &id,
            caller.customers().as_deref(),
            AlertEdit::Attributes(attrs),
        )
        .await?;
    Ok(Json(json!({"status": "ok"})))
}

async fn take_action(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
    Json(body): Json<Value>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("write:alerts")?;
    let action = opt_str(body.get("action"))
        .ok_or_else(|| ApiError::bad("must supply 'action' as json data"))?;
    let default_text = format!("{action} operator action");
    let text = body
        .get("text")
        .and_then(Value::as_str)
        .unwrap_or(&default_text);
    let timeout = match body.get("timeout") {
        None | Some(Value::Null) => None,
        v => Some(parse_timeout(v, 0)?),
    };
    st.store
        .take_action_with(
            &id,
            caller.customers().as_deref(),
            &action,
            text,
            timeout,
            &caller.facts(&st),
        )
        .await?;
    Ok(Json(json!({"status": "ok"})))
}

/// Query parameters shared by `/alerts` and `/alerts/count`.
struct Search {
    q: AlertQuery,
    page: i64,
    page_size: i64,
    show_history: HistoryOutput,
}

/// Whether `attributes.<key>` names a restricted attribute.
fn restricted_path(path: &str) -> bool {
    path.strip_prefix("attributes.")
        .is_some_and(is_restricted_attribute)
}

/// A query parameter that could select, sort or group by a restricted
/// attribute (hook spec section 4.2, after Zabalack's query guard,
/// `zab-al-ack@eaf2acf:src/zabalack/query_guard.py:1-50`): a direct
/// `attributes.<key>` filter, negated or not; `sort-by` on it, either
/// direction; `group-by` the whole `attributes` object; `q.df` defaulting to
/// `attributes` or to the key; and any `q` that mentions the key. alrust
/// parses none of `q`, `q.df`, `sort-by`, `group-by` or `attributes.*` filters
/// yet (they are ignored like other unknown parameters), so today this only
/// refuses; it is in place before any of them is supported.
fn restricted_selector(key: &str, value: &str) -> bool {
    match key {
        "sort-by" => restricted_path(value.strip_prefix('-').unwrap_or(value)),
        "group-by" => value == "attributes",
        "q.df" => value == "attributes" || restricted_path(value),
        "q" => RESTRICTED_ATTRIBUTES.iter().any(|r| value.contains(r)),
        _ => restricted_path(key.strip_suffix('!').unwrap_or(key)),
    }
}

/// Refuses a search that has any restricted selector, for every caller,
/// with a stable, content-free 400.
fn refuse_restricted_selectors(params: &[(String, String)]) -> ApiResult<()> {
    if params.iter().any(|(k, v)| restricted_selector(k, v)) {
        return Err(ApiError::bad(
            "Query selector is not permitted on restricted attributes",
        ));
    }
    Ok(())
}

fn parse_search(st: &AppState, caller: &Caller, params: &[(String, String)]) -> ApiResult<Search> {
    // Before anything else is parsed.
    refuse_restricted_selectors(params)?;
    let mut q = AlertQuery {
        customers: caller.customers(),
        ..AlertQuery::default()
    };
    let mut page = 1i64;
    let mut page_size = st.cfg.default_page_size;
    let mut show_history = HistoryOutput::Omit;
    for (k, v) in params {
        match k.as_str() {
            "id" => q.ids.push(v.clone()),
            "resource" => q.resources.push(v.clone()),
            "event" => q.events.push(v.clone()),
            "environment" => q.environments.push(v.clone()),
            "severity" => q.severities.push(v.clone()),
            "status" => q.statuses.push(v.clone()),
            "service" => q.services.push(v.clone()),
            "group" => q.groups.push(v.clone()),
            "origin" => q.origins.push(v.clone()),
            "tag" | "tags" => q.tags.push(v.clone()),
            "id!" => q.not_ids.push(v.clone()),
            "resource!" => q.not_resources.push(v.clone()),
            "event!" => q.not_events.push(v.clone()),
            "environment!" => q.not_environments.push(v.clone()),
            "severity!" => q.not_severities.push(v.clone()),
            "status!" => q.not_statuses.push(v.clone()),
            "group!" => q.not_groups.push(v.clone()),
            "origin!" => q.not_origins.push(v.clone()),
            "page" => {
                page = v
                    .parse()
                    .map_err(|_| ApiError::bad("page must be an integer"))?;
            }
            "page-size" => {
                page_size = v
                    .parse()
                    .map_err(|_| ApiError::bad("page-size must be an integer"))?;
            }
            "show-history" => {
                show_history = if matches!(
                    v.to_lowercase().as_str(),
                    "true" | "t" | "1" | "yes" | "y" | "on"
                ) {
                    HistoryOutput::Include
                } else {
                    HistoryOutput::Omit
                };
            }
            _ => {}
        }
    }
    if page < 1 {
        return Err(ApiError::bad("page must be greater than 0"));
    }
    if page_size < 1 {
        return Err(ApiError::bad("page-size must be greater than 0"));
    }
    // The store's OFFSET is (page - 1) * page-size; it must not overflow.
    if (page - 1).checked_mul(page_size).is_none() {
        return Err(ApiError::bad("page is out of range for this page-size"));
    }
    Ok(Search {
        q,
        page,
        page_size,
        show_history,
    })
}

async fn count_alerts(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("read:alerts")?;
    let s = parse_search(&st, &caller, &params)?;
    let r = st.store.count_alerts(&s.q).await?;
    Ok(Json(json!({
        "status": "ok",
        "total": r.total,
        "severityCounts": r.severity_counts,
        "statusCounts": r.status_counts,
        "autoRefresh": true,
    })))
}

async fn search_alerts(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("read:alerts")?;
    let Search {
        q,
        page,
        page_size,
        show_history,
    } = parse_search(&st, &caller, &params)?;
    let r = st
        .store
        .find_alerts(&q, page, page_size, show_history)
        .await?;
    // Rounded up without `total + page_size - 1`, which overflows.
    let pages = r.total / page_size + i64::from(r.total % page_size != 0);
    let alert_timeout = st.store.config().alert_timeout;
    let alerts: Vec<Value> = r
        .alerts
        .iter()
        .map(|a| {
            serde_json::to_value(a.body(&st.cfg.base_url, alert_timeout, show_history))
                .unwrap_or(Value::Null)
        })
        .collect();
    let last_time = r
        .alerts
        .iter()
        .filter_map(|a| a.last_receive_time)
        .max()
        .unwrap_or_else(Utc::now);
    let mut body = json!({
        "status": "ok",
        "page": page,
        "pageSize": page_size,
        "pages": if alerts.is_empty() { 0 } else { pages },
        "more": page < pages,
        "alerts": alerts,
        "total": if r.alerts.is_empty() { 0 } else { r.total },
        "statusCounts": r.status_counts,
        "severityCounts": r.severity_counts,
        "lastTime": iso8601(&last_time),
        "autoRefresh": true,
    });
    if r.alerts.is_empty() {
        body["message"] = json!("not found");
    }
    Ok(Json(body))
}

// ------------------------------------------------------ grouped alert reads

/// A [`CubeCounts`] as the reference's item fields.
fn cube_fields(item: &mut Map<String, Value>, c: &CubeCounts) {
    item.insert(
        "severityCounts".into(),
        Value::Object(c.severity_counts.clone()),
    );
    item.insert(
        "statusCounts".into(),
        Value::Object(c.status_counts.clone()),
    );
    item.insert("count".into(), json!(c.count));
}

/// The reference's list answer: `status`, the items and `total` (their
/// number), with `message: not found` when there are none
/// (`views/alerts.py:529-542`, `:554-567`, `:579-593`).
fn listed(key: &str, items: Vec<Value>) -> Json<Value> {
    let mut body = json!({"status": "ok", "total": items.len()});
    if items.is_empty() {
        body["message"] = json!("not found");
    }
    body[key] = Value::Array(items);
    Json(body)
}

/// `GET /environments` (`views/alerts.py:486-517`). The names are not
/// filtered (customer-mapping C20); `total` is the filtered number of
/// distinct environments.
async fn list_environments(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("read:alerts")?;
    let s = parse_search(&st, &caller, &params)?;
    let total = st.store.environment_total(&s.q).await?;
    // `Page` (`utils/paging.py`): no pages when nothing matches, and 416
    // for a page after the last.
    let pages = if total == 0 {
        0
    } else {
        (total - 1) / s.page_size + 1
    };
    if total > 0 && s.page > pages {
        return Err(ApiError::new(
            StatusCode::RANGE_NOT_SATISFIABLE,
            format!("page out of range: 1-{pages}"),
        ));
    }
    let environments: Vec<Value> = st
        .store
        .environments(&s.q, s.page, s.page_size)
        .await?
        .iter()
        .map(|e| {
            let mut item = Map::new();
            item.insert("environment".into(), json!(e.environment));
            cube_fields(&mut item, &e.counts);
            Value::Object(item)
        })
        .collect();
    let found = !environments.is_empty();
    let mut body = json!({
        "status": "ok",
        "environments": environments,
        "page": s.page,
        "pageSize": s.page_size,
        "pages": pages,
        "more": s.page < pages,
        "total": if found { total } else { 0 },
    });
    if !found {
        body["message"] = json!("not found");
    }
    Ok(Json(body))
}

/// `GET /services` (`views/alerts.py:521-542`). The pairs are not filtered
/// (customer-mapping C20).
async fn list_services(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("read:alerts")?;
    let s = parse_search(&st, &caller, &params)?;
    let services = st
        .store
        .services(&s.q)
        .await?
        .iter()
        .map(|v| {
            let mut item = Map::new();
            item.insert("environment".into(), json!(v.environment));
            item.insert("service".into(), json!(v.service));
            cube_fields(&mut item, &v.counts);
            Value::Object(item)
        })
        .collect();
    Ok(listed("services", services))
}

/// `GET /alerts/groups` (`views/alerts.py:546-567`).
async fn list_alert_groups(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("read:alerts")?;
    let s = parse_search(&st, &caller, &params)?;
    let groups = st
        .store
        .alert_groups(&s.q)
        .await?
        .iter()
        .map(|g| json!({"environment": g.environment, "group": g.group, "count": g.count}))
        .collect();
    Ok(listed("groups", groups))
}

/// `GET /alerts/tags` (`views/alerts.py:571-593`).
async fn list_alert_tags(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("read:alerts")?;
    let s = parse_search(&st, &caller, &params)?;
    let tags = st
        .store
        .alert_tags(&s.q)
        .await?
        .iter()
        .map(|t| json!({"environment": t.environment, "tag": t.tag, "count": t.count}))
        .collect();
    Ok(listed("tags", tags))
}

// ------------------------------------------------------------------ top-N

/// `GET /alerts/top10/…` and `/alerts/topn/…` (`views/alerts.py:405-483`;
/// SF-QUERY alrust item 2). Order of checks: the `GET /alerts` filters, then
/// the reference's single page (`Page.from_params(args, 1)`: a page after
/// the first is 416), then `group-by`.
async fn topn_route(
    st: &AppState,
    headers: &HeaderMap,
    params: &[(String, String)],
    kind: TopnKind,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(st, headers, params).await?;
    caller.require("read:alerts")?;
    let s = parse_search(st, &caller, params)?;
    if s.page > 1 {
        return Err(ApiError::new(
            StatusCode::RANGE_NOT_SATISFIABLE,
            "page out of range: 1-1",
        ));
    }
    // The first `group-by` value, default `event`, matched exactly.
    let wanted = params
        .iter()
        .find(|(k, _)| k == "group-by")
        .map_or("event", |(_, v)| v.as_str());
    let group = GroupColumn::parse(wanted)
        .ok_or_else(|| ApiError::bad(format!("Grouping by '{wanted}' field not supported.")))?;
    let items = st.store.topn(&s.q, kind, group, s.page_size).await?;
    let top = items
        .iter()
        .map(|t| {
            let mut m = alrust_store_pg::topn::base_fields(t, group);
            let resources: Vec<Value> = t
                .resources
                .iter()
                .map(|(id, resource)| {
                    json!({
                        "id": id,
                        "resource": resource,
                        "href": format!("{}/alert/{}", st.cfg.base_url, alrust_domain::path_segment(id)),
                    })
                })
                .collect();
            m.insert("resources".into(), Value::Array(resources));
            Value::Object(m)
        })
        .collect();
    Ok(listed("top10", top))
}

async fn topn_count(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
) -> ApiResult<Json<Value>> {
    topn_route(&st, &headers, &params, TopnKind::Count).await
}

async fn topn_flapping(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
) -> ApiResult<Json<Value>> {
    topn_route(&st, &headers, &params, TopnKind::Flapping).await
}

async fn topn_standing(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
) -> ApiResult<Json<Value>> {
    topn_route(&st, &headers, &params, TopnKind::Standing).await
}

// ---------------------------------------------------------------- heartbeats

async fn create_heartbeat(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Json(body): Json<Value>,
) -> ApiResult<Response> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("write:heartbeats")?;
    let obj = body
        .as_object()
        .ok_or_else(|| ApiError::bad("heartbeat must be a JSON object"))?;
    let timeout = parse_timeout(obj.get("timeout"), st.store.config().alert_timeout)?;
    let attributes = parse_attributes(obj.get("attributes"))?;
    let create_time = match obj.get("createTime") {
        Some(Value::String(s)) => truncate_ms(parse_time(s).ok_or_else(|| {
            ApiError::bad("dates must be ISO 8601 date format YYYY-MM-DDThh:mm:ss.sssZ")
        })?),
        _ => truncate_ms(Utc::now()),
    };
    let customer = caller.assign_customer(wanted_customer(obj)?, "admin:heartbeats")?;
    let hb = st
        .store
        .upsert_heartbeat(
            &opt_str(obj.get("id")).unwrap_or_else(|| uuid::Uuid::new_v4().to_string()),
            &opt_str(obj.get("origin")).unwrap_or_else(|| st.cfg.default_origin.clone()),
            &str_list(obj.get("tags"), "tags")?,
            &attributes,
            &opt_str(obj.get("type")).unwrap_or_else(|| "Heartbeat".into()),
            create_time,
            timeout,
            customer.as_deref(),
        )
        .await?;
    let body = hb.body(
        &st.cfg.base_url,
        st.cfg.heartbeat_max_latency_ms,
        Utc::now(),
    );
    Ok((
        StatusCode::CREATED,
        Json(json!({"status": "ok", "id": hb.id, "heartbeat": body})),
    )
        .into_response())
}

async fn get_heartbeat(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("read:heartbeats")?;
    let hb = st.store.get_heartbeat(&id).await?;
    let body = hb.body(
        &st.cfg.base_url,
        st.cfg.heartbeat_max_latency_ms,
        Utc::now(),
    );
    Ok(Json(json!({"status": "ok", "total": 1, "heartbeat": body})))
}

async fn list_heartbeats(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("read:heartbeats")?;
    // Only these three values filter, case-sensitively; the reference ignores
    // any other `status` value (`postgres/base.py:886-907`).
    let wanted: Vec<&str> = params
        .iter()
        .filter(|(k, v)| k == "status" && matches!(v.as_str(), "ok" | "slow" | "expired"))
        .map(|(_, v)| v.as_str())
        .collect();
    let all = st
        .store
        .list_heartbeats(caller.customers().as_deref())
        .await?;
    let now = Utc::now();
    let total = all.len();
    let heartbeats: Vec<Value> = all
        .iter()
        .map(|h| h.body(&st.cfg.base_url, st.cfg.heartbeat_max_latency_ms, now))
        .filter(|b| wanted.is_empty() || wanted.contains(&b.status))
        .map(|b| serde_json::to_value(b).unwrap_or(Value::Null))
        .collect();
    if heartbeats.is_empty() {
        return Ok(Json(
            json!({"status": "ok", "message": "not found", "heartbeats": [], "total": 0}),
        ));
    }
    Ok(Json(
        json!({"status": "ok", "heartbeats": heartbeats, "total": total}),
    ))
}

async fn delete_heartbeat(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("write:heartbeats")?;
    st.store.delete_heartbeat(&id).await?;
    Ok(Json(json!({"status": "ok"})))
}

// ---------------------------------------------------------------- blackouts

/// `admin` or `admin:blackouts` by exact list membership, as the
/// reference's views test it (`Scope.admin in g.scopes or ...`).
fn admin_blackouts(caller: &Caller) -> bool {
    caller
        .scopes
        .iter()
        .any(|s| s == "admin" || s == "admin:blackouts")
}

const DATES: &str = "dates must be ISO 8601 date format YYYY-MM-DDThh:mm:ss.sssZ";

/// Python's falsiness of a JSON value.
fn py_falsy(v: &Value) -> bool {
    match v {
        Value::Null | Value::Bool(false) => true,
        Value::Number(n) => n.as_f64() == Some(0.0),
        Value::String(s) => s.is_empty(),
        Value::Array(a) => a.is_empty(),
        Value::Object(o) => o.is_empty(),
        Value::Bool(true) => false,
    }
}

/// A list of strings (`Blackout.parse`: `service must be a list`). A list
/// with a non-string is refused too (the reference's outcome is not
/// established).
fn string_list(v: Option<&Value>, key: &str) -> ApiResult<Vec<String>> {
    match v {
        None => Ok(Vec::new()),
        Some(Value::Array(items)) => items
            .iter()
            .map(|i| i.as_str().map(str::to_owned))
            .collect::<Option<Vec<_>>>()
            .ok_or_else(|| ApiError::bad(format!("{key} must be a list of strings"))),
        Some(_) => Err(ApiError::bad(format!("{key} must be a list"))),
    }
}

/// An optional string: absent or null is none; another type is refused
/// (not established in the reference).
fn opt_string(v: Option<&Value>, key: &str) -> ApiResult<Option<String>> {
    match v {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) => Ok(Some(s.clone())),
        Some(_) => Err(ApiError::bad(format!("{key} must be a string"))),
    }
}

/// `customer` in a blackout body, before `assign_customer`. A Python-falsy
/// value of any type (null, `""`, `0`, `false`, `[]`, `{}`) is "no customer
/// wanted", as in the reference. A truthy non-string is 400: for a caller
/// without `admin` or `admin:blackouts` the reference refuses it too (it is
/// in no customer list; the reference's text carries Python's rendering of
/// the value), and for the others the reference gives it to the database
/// adapter (outcome not established). It is never read as "absent".
/// Unlike `wanted_customer` (C5), an empty string is not refused here: the
/// blackout routes keep the reference's reading of it.
fn blackout_customer(v: Option<&Value>) -> ApiResult<Option<String>> {
    match v {
        Some(Value::String(s)) if !s.is_empty() => Ok(Some(s.clone())),
        Some(v) if !py_falsy(v) => Err(ApiError::bad("customer must be a string")),
        _ => Ok(None),
    }
}

/// `DateTime.parse` on create: a malformed string is the reference's 400;
/// a non-string or null is none.
fn create_time_field(v: Option<&Value>) -> ApiResult<Option<chrono::DateTime<Utc>>> {
    match v {
        Some(Value::String(s)) => parse_time(s).map(Some).ok_or_else(|| ApiError::bad(DATES)),
        _ => Ok(None),
    }
}

/// `kwargs.get('duration') or BLACKOUT_DURATION`; a truthy non-integer is
/// refused (the reference fails in `timedelta` or stores it unrounded).
fn create_duration(v: Option<&Value>) -> ApiResult<i64> {
    let Some(v) = v.filter(|v| !py_falsy(v)) else {
        return Ok(alrust_store_pg::blackouts::BLACKOUT_DURATION);
    };
    match v {
        Value::Number(n) if n.is_i64() => n
            .as_i64()
            .ok_or_else(|| ApiError::bad("duration must be an integer")),
        _ => Err(ApiError::bad("duration must be an integer")),
    }
}

/// `Blackout.parse` and `Blackout.__init__` (`models/blackout.py:25-112`),
/// in their order of checks, then the view's user and customer rules
/// (`views/blackouts.py:17-45`).
fn parse_new_blackout(
    body: &Value,
    caller: &Caller,
) -> ApiResult<alrust_store_pg::blackouts::NewBlackout> {
    let Value::Object(obj) = body else {
        return Err(ApiError::bad("blackout must be a JSON object"));
    };
    let service = string_list(obj.get("service"), "service")?;
    let tags = string_list(obj.get("tags"), "tags")?;
    // `json['environment']`: a missing key is Python's KeyError text.
    let environment = obj
        .get("environment")
        .ok_or_else(|| ApiError::bad("'environment'"))?;
    let start = create_time_field(obj.get("startTime"))?;
    let end = create_time_field(obj.get("endTime"))?;
    let environment = match environment {
        v if py_falsy(v) => {
            return Err(ApiError::bad("Missing mandatory value for \"environment\""));
        }
        Value::String(s) => s.clone(),
        _ => return Err(ApiError::bad("environment must be a string")),
    };
    let field = |key: &str| -> ApiResult<Option<String>> {
        let v = opt_string(obj.get(key), key)?;
        if v.as_deref() == Some("") {
            return Err(ApiError::bad(format!("{key} can not be an empty string")));
        }
        Ok(v)
    };
    let resource = field("resource")?;
    let event = field("event")?;
    let group = field("group")?;
    let origin = field("origin")?;
    let now = truncate_ms(Utc::now());
    let start_time = start.unwrap_or(now);
    let (end_time, duration) = if let Some(end) = end {
        (end, (end - start_time).num_seconds())
    } else {
        let d = create_duration(obj.get("duration"))?;
        let end = Duration::try_seconds(d)
            .and_then(|span| start_time.checked_add_signed(span))
            .ok_or_else(|| ApiError::bad("duration is out of range"))?;
        (end, d)
    };
    let id = supplied(obj, "id")?.unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    let user = if admin_blackouts(caller) {
        supplied(obj, "user")?.or_else(|| caller.login.clone())
    } else {
        caller.login.clone()
    };
    let customer =
        caller.assign_customer(blackout_customer(obj.get("customer"))?, "admin:blackouts")?;
    Ok(alrust_store_pg::blackouts::NewBlackout {
        id,
        environment,
        service,
        resource,
        event,
        group,
        tags,
        origin,
        customer,
        start_time,
        end_time,
        duration,
        user,
        create_time: now,
        text: opt_string(obj.get("text"), "text")?,
    })
}

/// `startTime`/`endTime` on update (`models/blackout.py:237-240`): only a
/// truthy value is parsed; a truthy non-string parses to none and is
/// skipped. A malformed string (a 500 in the reference) and a falsy
/// non-null value (written raw there, outcome not established) are 400.
fn update_time_field(
    obj: &Map<String, Value>,
    key: &str,
) -> ApiResult<Option<chrono::DateTime<Utc>>> {
    match obj.get(key) {
        Some(Value::String(s)) if !s.is_empty() => {
            parse_time(s).map(Some).ok_or_else(|| ApiError::bad(DATES))
        }
        Some(v) if !v.is_null() && py_falsy(v) => Err(ApiError::bad(DATES)),
        _ => Ok(None),
    }
}

/// The body of `PUT /blackout/{id}` as `update_blackout` writes it
/// (`postgres/base.py:803-839`), with SF-BLACKOUTS for `customer`. The
/// caller has already refused a falsy body and found the blackout; the
/// checks here keep the reference's order: the customer first
/// (`views/blackouts.py:111`), then the fields.
fn parse_blackout_update(
    body: &Value,
    caller: &Caller,
) -> ApiResult<alrust_store_pg::blackouts::BlackoutUpdate> {
    // A truthy body that is not an object fails in the reference at
    // `update['user'] = ...` (an inferred TypeError, a 500).
    let Value::Object(obj) = body else {
        return Err(ApiError::bad("blackout must be a JSON object"));
    };
    let customer = match obj.get("customer") {
        None => Change::Keep,
        v => Change::Set(caller.assign_customer(blackout_customer(v)?, "admin:blackouts")?),
    };
    // These keys collide with arguments of the reference's update calls
    // (`self` at `Blackout.update`, `id` at `db.update_blackout`): an
    // inferred TypeError, a 500 there.
    for key in ["id", "self"] {
        if obj.contains_key(key) {
            return Err(ApiError::bad(format!("{key} cannot be changed")));
        }
    }
    let present = |key: &str| -> ApiResult<Change<Option<String>>> {
        match obj.get(key) {
            None => Ok(Change::Keep),
            v => opt_string(v, key).map(Change::Set),
        }
    };
    // `None` becomes `[]` (`models/blackout.py:242-245`).
    let list_field = |key: &str| -> ApiResult<Option<Vec<String>>> {
        match obj.get(key) {
            None => Ok(None),
            Some(Value::Null) => Ok(Some(Vec::new())),
            v => string_list(v, key).map(Some),
        }
    };
    let duration = match obj.get("duration") {
        None => Change::Keep,
        Some(Value::Null) => Change::Set(None),
        Some(v) => Change::Set(Some(
            v.as_i64()
                .and_then(|d| i32::try_from(d).ok())
                .ok_or_else(|| ApiError::bad("duration must be an integer"))?,
        )),
    };
    // The reference writes an empty `environment` and then fails to
    // rebuild the blackout (`Blackout.__init__` refuses it), on this and
    // on every later read of the row. alrust refuses before it writes.
    let environment = opt_string(obj.get("environment"), "environment")?;
    if environment.as_deref() == Some("") {
        return Err(ApiError::bad("Missing mandatory value for \"environment\""));
    }
    Ok(alrust_store_pg::blackouts::BlackoutUpdate {
        environment,
        service: list_field("service")?,
        resource: present("resource")?,
        event: present("event")?,
        group: present("group")?,
        tags: list_field("tags")?,
        origin: present("origin")?,
        customer,
        start_time: update_time_field(obj, "startTime")?,
        end_time: update_time_field(obj, "endTime")?,
        duration,
        text: present("text")?,
        user: caller.login.clone(),
    })
}

/// Parameters `filter_query` skips (`postgres/utils.py:19-22`).
const NOT_FILTERS: [&str; 16] = [
    "_",
    "callback",
    "token",
    "api-key",
    "q",
    "q.df",
    "id",
    "from-date",
    "to-date",
    "sort-by",
    "group-by",
    "page",
    "page-size",
    "limit",
    "show-raw-data",
    "show-history",
];

/// A `Blackouts.VALID_PARAMS` filter name.
enum BlackoutFilterName {
    Column(&'static str, alrust_store_pg::blackouts::ColumnKind),
    /// A valid name in the reference whose SQL fails (inferred from the
    /// source): `remaining` is a SELECT alias and `status` (reached only
    /// as `status!` or `status.x`) is no column. A 400 here.
    Refused,
}

/// `Blackouts.VALID_PARAMS` filter columns; `None` for an unknown name.
fn blackout_filter_column(name: &str) -> Option<BlackoutFilterName> {
    use alrust_store_pg::blackouts::ColumnKind::{Array, Integer, Text, Timestamp};
    let (column, kind) = match name {
        "priority" => ("priority", Integer),
        "environment" => ("environment", Text),
        "service" => ("service", Array),
        "resource" => ("resource", Text),
        "event" => ("event", Text),
        "group" => ("\"group\"", Text),
        "customer" => ("customer", Text),
        "startTime" => ("start_time", Timestamp),
        "endTime" => ("end_time", Timestamp),
        "duration" => ("duration", Integer),
        "user" => ("\"user\"", Text),
        "createTime" => ("create_time", Timestamp),
        "text" => ("text", Text),
        "tag" => ("tags", Array),
        "remaining" | "status" => return Some(BlackoutFilterName::Refused),
        _ => return None,
    };
    Some(BlackoutFilterName::Column(column, kind))
}

/// `Blackouts.VALID_PARAMS` sort columns with their default direction
/// (`1` ascending). `status` is listed there but is no column (a 500 in
/// the reference); alrust refuses it like an unknown name.
fn blackout_sort_column(name: &str) -> Option<(&'static str, i8)> {
    Some(match name {
        "priority" => ("priority", 1),
        "environment" => ("environment", 1),
        "service" => ("service", 1),
        "resource" => ("resource", 1),
        "event" => ("event", 1),
        "group" => ("\"group\"", 1),
        "tags" => ("tags", 1),
        "customer" => ("customer", 1),
        "startTime" => ("start_time", -1),
        "endTime" => ("end_time", -1),
        "duration" => ("duration", 1),
        "remaining" => ("remaining", -1),
        "user" => ("\"user\"", 1),
        "createTime" => ("create_time", -1),
        "text" => ("text", 1),
        _ => return None,
    })
}

/// `Blackouts.from_params` (`postgres/utils.py:226-268`): customers,
/// `status`, the filters in order of first appearance, then `sort-by`.
fn parse_blackout_query(
    caller: &Caller,
    params: &[(String, String)],
) -> ApiResult<alrust_store_pg::blackouts::BlackoutQuery> {
    use alrust_store_pg::blackouts::{BlackoutFilter, ColumnKind};
    let mut q = alrust_store_pg::blackouts::BlackoutQuery {
        customers: caller.customers(),
        ..Default::default()
    };
    // The reference parses a non-empty `q` with its query parser. alrust
    // has no such parser yet (alpha.md). It refuses the request, because a
    // 200 with the list that `q` did not narrow would look like the answer.
    if params
        .iter()
        .find(|(k, _)| k == "q")
        .is_some_and(|(_, v)| !v.is_empty())
    {
        return Err(ApiError::bad("q queries are not supported yet"));
    }
    let mut seen: Vec<&str> = Vec::new();
    for (key, value) in params {
        if key == "status" {
            q.statuses.push(value.clone());
            continue;
        }
        // The exclusion applies to the normalized name, so `id!` and
        // `page.x` are skipped like `id` and `page`.
        let base = key.replace('!', "");
        let base = base.split('.').next().unwrap_or_default();
        if NOT_FILTERS.contains(&base) || seen.contains(&key.as_str()) {
            continue;
        }
        seen.push(key);
        let refuse = || ApiError::bad(format!("Filtering by '{key}' field not supported."));
        let (column, kind) = match blackout_filter_column(base) {
            Some(BlackoutFilterName::Column(column, kind)) => (column, kind),
            Some(BlackoutFilterName::Refused) => return Err(refuse()),
            None => return Err(ApiError::bad(format!("Invalid filter parameter: {key}"))),
        };
        let values: Vec<String> = params
            .iter()
            .filter(|(k, _)| k == key)
            .map(|(_, v)| v.clone())
            .collect();
        let negated = key.ends_with('!');
        // Only the exact key `service` is an overlap, with any values. Every
        // other form on an array column (`tag`, `tag!`, `service!`) is the
        // scalar comparison of the reference: one value, read as an array
        // literal, with `=` or `!=`.
        let kind = match kind {
            ColumnKind::Array if key != "service" => ColumnKind::ArrayLiteral,
            k => k,
        };
        // Errors in the reference, inferred from its source: `~` on a
        // column that is not text, several values on an array column
        // outside the overlap, and an empty value among several (`v[0]`).
        let unsupported = kind != ColumnKind::Array
            && ((kind != ColumnKind::Text && values.iter().any(|v| v.starts_with('~')))
                || (kind == ColumnKind::ArrayLiteral && values.len() > 1)
                || (values.len() > 1 && values.iter().any(String::is_empty)));
        if unsupported {
            return Err(refuse());
        }
        q.filters.push(BlackoutFilter {
            column,
            kind,
            negated,
            values,
        });
    }
    // `if params.get('sort-by')`: an empty first value means no order was
    // asked for, whatever follows it.
    let sorts: Vec<&String> = params
        .iter()
        .filter(|(k, _)| k == "sort-by")
        .map(|(_, v)| v)
        .collect();
    let sorts = if sorts.first().is_some_and(|v| v.is_empty()) {
        Vec::new()
    } else {
        sorts
    };
    for value in sorts {
        let (reverse, name) = match value.strip_prefix('-') {
            Some(n) => (-1_i8, n),
            None => (1_i8, value.as_str()),
        };
        let name = if name.starts_with("attributes.") {
            "attributes"
        } else {
            name
        };
        let (column, direction) = blackout_sort_column(name)
            .ok_or_else(|| ApiError::bad(format!("Sorting by '{name}' field not supported.")))?;
        q.sort.push((column, direction * reverse != 1));
    }
    Ok(q)
}

/// `Page.from_params` (`utils/paging.py`): the first `page`, `limit` and
/// `page-size`; a value that is not an integer gives the default (page 1,
/// limit 0, page-size = limit), and a page size of 0 gives the configured
/// default. A negative page size is 400 (in the reference it reaches
/// `LIMIT`, an SQL error inferred from the source). The range of `page`
/// is checked by the caller, after the count.
fn blackout_paging(st: &AppState, params: &[(String, String)]) -> ApiResult<(i64, i64)> {
    let int = |key: &str, default: i64| -> i64 {
        params
            .iter()
            .find(|(k, _)| k == key)
            .and_then(|(_, v)| v.trim().parse().ok())
            .unwrap_or(default)
    };
    let page = int("page", 1);
    let limit = int("limit", 0);
    let page_size = match int("page-size", limit) {
        0 => st.cfg.default_page_size,
        s => s,
    };
    if page_size < 1 {
        return Err(ApiError::bad("page-size must be greater than 0"));
    }
    Ok((page, page_size))
}

/// `POST /blackout` (`views/blackouts.py:17-45`).
async fn create_blackout(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Json(body): Json<Value>,
) -> ApiResult<Response> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("write:blackouts")?;
    let new = parse_new_blackout(&body, &caller)?;
    let b = st.store.create_blackout(&new).await?;
    let location = format!(
        "{}/blackout/{}",
        st.cfg.base_url,
        alrust_domain::path_segment(&b.id)
    );
    Ok((
        StatusCode::CREATED,
        [(axum::http::header::LOCATION, location)],
        Json(json!({"status": "ok", "id": b.id, "blackout": b.body(&st.cfg.base_url, Utc::now())})),
    )
        .into_response())
}

/// `GET /blackout/{id}`: no customer filter (customer-mapping C19, the
/// reference contract).
async fn get_blackout(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("read:blackouts")?;
    let b = st.store.get_blackout(&id, None).await?;
    Ok(Json(
        json!({"status": "ok", "total": 1, "blackout": b.body(&st.cfg.base_url, Utc::now())}),
    ))
}

/// `GET /blackouts` (`views/blackouts.py:61-91`).
async fn list_blackouts(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("read:blackouts")?;
    let q = parse_blackout_query(&caller, &params)?;
    let (page, page_size) = blackout_paging(&st, &params)?;
    if !st.store.blackout_filter_values_valid(&q).await? {
        return Err(ApiError::bad(
            "a filter value is not valid for the type of its field",
        ));
    }
    let total = st.store.count_blackouts(&q).await?;
    let pages = if total == 0 {
        0
    } else {
        (total - 1) / page_size + 1
    };
    // `if items and self.page > self.pages or self.page < 1` (416).
    if (total > 0 && page > pages) || page < 1 {
        return Err(ApiError::new(
            StatusCode::RANGE_NOT_SATISFIABLE,
            format!("page out of range: 1-{pages}"),
        ));
    }
    if (page - 1).checked_mul(page_size).is_none() {
        return Err(ApiError::bad("page is out of range for this page-size"));
    }
    let now = Utc::now();
    let blackouts: Vec<Value> = st
        .store
        .list_blackouts(&q, page, page_size)
        .await?
        .iter()
        .map(|b| b.body(&st.cfg.base_url, now))
        .collect();
    let found = !blackouts.is_empty();
    let mut body = json!({
        "status": "ok",
        "page": page,
        "pageSize": page_size,
        "pages": pages,
        "more": page < pages,
        "blackouts": blackouts,
        "total": if found { total } else { 0 },
    });
    if !found {
        body["message"] = json!("not found");
    }
    Ok(Json(body))
}

/// `PUT /blackout/{id}` (`views/blackouts.py:94-124`; SF-BLACKOUTS). The
/// lookup is unfiltered without `auth_required` or for `admin` and
/// `admin:blackouts`, else limited to the caller's customers.
///
/// The order is the reference's: a falsy body is 400, then a blackout
/// the caller cannot see is 404, and only then is the body checked. The
/// write carries the same customer rule again (the store's `UPDATE`), so
/// a blackout that leaves the caller's customers between the lookup and
/// the write is 404 and is not changed.
async fn update_blackout(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
    Json(body): Json<Value>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("write:blackouts")?;
    if py_falsy(&body) {
        return Err(ApiError::bad("nothing to change"));
    }
    let customers = if !st.cfg.auth_required || admin_blackouts(&caller) {
        None
    } else {
        caller.customers()
    };
    st.store.get_blackout(&id, customers.as_deref()).await?;
    let update = parse_blackout_update(&body, &caller)?;
    let b = st
        .store
        .update_blackout(&id, customers.as_deref(), update)
        .await?;
    Ok(Json(
        json!({"status": "ok", "blackout": b.body(&st.cfg.base_url, Utc::now())}),
    ))
}

/// `DELETE /blackout/{id}`: no customer filter (customer-mapping C19).
async fn delete_blackout(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("write:blackouts")?;
    st.store.delete_blackout(&id).await?;
    Ok(Json(json!({"status": "ok"})))
}

// ---------------------------------------------------------------- keys

/// A caller-supplied `id` or `key` on `POST /key` (`models/key.py:50-56`).
/// The reference keeps the value only when it is truthy
/// (`kwargs.get('key', None) or key_helper.generate()`), so every Python-falsy
/// value (absent, null, false, 0, 0.0, "", [], {}) generates one here too.
/// A truthy non-string is rejected with 400: an intentional divergence, by
/// the owner's ruling (KEYSUP-AUTH-1, DECISIONS Y33). The reference passes such values
/// unchanged to the database adapter; type-specific outcomes have not been
/// qualified.
fn supplied(obj: &serde_json::Map<String, Value>, field: &str) -> Result<Option<String>, ApiError> {
    match obj.get(field) {
        None | Some(Value::Null | Value::Bool(false)) => Ok(None),
        Some(Value::Number(n)) if n.as_f64() == Some(0.0) => Ok(None),
        Some(Value::String(s)) if s.is_empty() => Ok(None),
        Some(Value::Array(a)) if a.is_empty() => Ok(None),
        Some(Value::Object(o)) if o.is_empty() => Ok(None),
        Some(Value::String(s)) => Ok(Some(s.clone())),
        Some(_) => Err(ApiError::bad(format!("{field} must be a string"))),
    }
}

pub fn new_key_value() -> String {
    let a = uuid::Uuid::new_v4().simple().to_string();
    let b = uuid::Uuid::new_v4().simple().to_string();
    format!("{a}{b}")[..40].to_string()
}

async fn create_key(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Json(body): Json<Value>,
) -> ApiResult<Response> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("write:keys")?;
    let obj = body
        .as_object()
        .ok_or_else(|| ApiError::bad("key must be a JSON object"))?;
    let scopes = match obj.get("scopes") {
        None | Some(Value::Null) => vec!["read".into(), "write".into()],
        v => str_list(v, "scopes")?,
    };
    // Every resulting scope must be within the caller's own (views/keys.py:38-41).
    for want in &scopes {
        if caller.auth_off || !scope_allows(&caller.scopes, want) {
            return Err(ApiError::new(
                StatusCode::FORBIDDEN,
                format!(
                    "Requested scope '{want}' not in existing scopes: {}",
                    caller.scopes.join(",")
                ),
            ));
        }
    }
    let can_set_user = caller
        .scopes
        .iter()
        .any(|s| s == "admin" || s == "admin:keys");
    let user = if can_set_user {
        opt_str(obj.get("user")).or_else(|| caller.login.clone())
    } else {
        caller.login.clone()
    };
    let Some(user) = user.filter(|u| !u.is_empty()) else {
        return Err(ApiError::bad(
            "An API key must be associated with a 'user'. Retry with user credentials.",
        ));
    };
    let customer = caller.assign_customer(wanted_customer(obj)?, "admin:keys")?;
    let expire_time = match obj.get("expireTime") {
        Some(Value::String(s)) => {
            parse_time(s).ok_or_else(|| ApiError::bad("invalid expireTime"))?
        }
        _ => Utc::now() + Duration::days(365),
    };
    let key = ApiKey {
        id: supplied(obj, "id")?.unwrap_or_else(|| uuid::Uuid::new_v4().to_string()),
        key: supplied(obj, "key")?.unwrap_or_else(new_key_value),
        user,
        scopes: Some(scopes),
        text: opt_str(obj.get("text")).unwrap_or_else(|| "API Key for alrust".into()),
        expire_time: Some(truncate_ms(expire_time)),
        count: 0,
        last_used_time: None,
        customer,
    };
    let k = st.store.create_key(&key).await?;
    // The secret is shown once, here (owner decision Y6, SF-KEYS S1).
    let data = serde_json::to_value(k.body(&st.cfg.base_url, Utc::now(), KeySecret::Reveal))
        .unwrap_or(Value::Null);
    Ok((
        StatusCode::CREATED,
        Json(json!({"status": "ok", "key": k.key, "data": data})),
    )
        .into_response())
}

async fn list_keys(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("read:keys")?;
    let now = Utc::now();
    // views/keys.py:84-91: every key only with auth disabled or an exact
    // admin/admin:keys scope; otherwise the caller's own keys.
    let all = !st.cfg.auth_required
        || caller
            .scopes
            .iter()
            .any(|s| s == "admin" || s == "admin:keys");
    let rows = if all {
        st.store.list_keys(caller.customers().as_deref()).await?
    } else {
        let Some(login) = caller.login.as_deref().filter(|l| !l.is_empty()) else {
            return Err(ApiError::bad("Must define 'user' to list user keys"));
        };
        st.store.list_keys_by_user(login).await?
    };
    let keys: Vec<Value> = rows
        .iter()
        .map(|k| {
            serde_json::to_value(k.body(&st.cfg.base_url, now, KeySecret::Hide))
                .unwrap_or(Value::Null)
        })
        .collect();
    let total = keys.len();
    Ok(Json(json!({"status": "ok", "keys": keys, "total": total})))
}

/// Key management addresses keys by public id only (SF-KEYS S3).
async fn delete_key(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("admin:keys")?;
    st.store.delete_key(&id).await?;
    Ok(Json(json!({"status": "ok"})))
}

// ---------------------------------------------------------------- management

/// `GET /management/gtg`: `OK` when the database answers, else 503 `FAILED`
/// (the reference's two answers, `management/views.py:127-134`). The cause is
/// logged, never returned.
async fn gtg(State(st): State<Shared>) -> Response {
    match st.store.ping().await {
        Ok(()) => "OK".into_response(),
        Err(e) => {
            tracing::warn!(error = %redact_diagnostic(e.to_string()), "gtg: the database did not answer");
            (StatusCode::SERVICE_UNAVAILABLE, "FAILED").into_response()
        }
    }
}

/// `GET /management/healthcheck` (spec HB5). Reproduces the reference's use
/// of the seconds *component* of the heartbeat age (HB5.2).
async fn healthcheck(State(st): State<Shared>) -> Response {
    let Ok(hbs) = st.store.list_heartbeats(None).await else {
        return (StatusCode::SERVICE_UNAVAILABLE, "HEALTH_CHECK_FAILED").into_response();
    };
    let now = Utc::now();
    for h in hbs {
        let seconds_component = (now - h.receive_time).num_seconds().rem_euclid(86400);
        if seconds_component > 4 * h.timeout {
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                format!("HEARTBEAT_STALE: {}", h.origin),
            )
                .into_response();
        }
    }
    "OK".into_response()
}

async fn housekeeping(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("admin:management")?;
    let hours = |name: &str| {
        params
            .iter()
            .find(|(k, _)| k == name)
            .and_then(|(_, v)| v.parse::<i64>().ok())
    };
    // Hours to seconds. An unchecked multiply wraps in a release build, and a
    // wrapped threshold of a few seconds would delete recent closed alerts.
    let seconds = |name: &str, default: i64| match hours(name).filter(|h| *h != 0) {
        None => Ok(default),
        Some(h) => h
            .checked_mul(3600)
            .ok_or_else(|| ApiError::bad(format!("'{name}' is out of range"))),
    };
    let expired_s = seconds("expired", st.cfg.delete_expired_after_s)?;
    let info_s = seconds("info", st.cfg.delete_info_after_s)?;
    let expired = st.store.housekeeping(expired_s, info_s).await?;
    let count = expired.len();
    Ok(Json(
        json!({"status": "ok", "expired": expired, "unshelve": [], "unack": [], "count": count}),
    ))
}

#[cfg(test)]
mod bearer_tests;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn restricted_selectors_are_refused() {
        let refused = [
            ("attributes.zabbix_ack_sync_intent", "x"),
            ("attributes.zabbix_ack_sync_intent!", "x"),
            ("sort-by", "attributes.zabbix_ack_sync_intent"),
            ("sort-by", "-attributes.zabbix_ack_sync_intent"),
            ("group-by", "attributes"),
            ("q.df", "attributes"),
            ("q.df", "attributes.zabbix_ack_sync_intent"),
            ("q", "_exists_:attributes.zabbix_ack_sync_intent"),
            ("q", "attributes.zabbix_ack_sync_intent:*"),
        ];
        for (k, v) in refused {
            assert!(restricted_selector(k, v), "{k}={v}");
        }
    }

    #[test]
    fn a_search_with_a_restricted_selector_is_refused_without_content() {
        let params = |k: &str, v: &str| {
            vec![
                ("status".to_owned(), "open".to_owned()),
                (k.to_owned(), v.to_owned()),
            ]
        };
        let e = refuse_restricted_selectors(&params("attributes.zabbix_ack_sync_intent", "s3cr3t"))
            .unwrap_err();
        assert_eq!(e.code, StatusCode::BAD_REQUEST);
        assert_eq!(
            e.message,
            "Query selector is not permitted on restricted attributes"
        );
        assert!(refuse_restricted_selectors(&params("attributes.region", "eu")).is_ok());
        assert!(refuse_restricted_selectors(&[]).is_ok());
    }

    #[test]
    fn an_error_message_carrying_evidence_is_redacted() {
        let body = |e: ApiError| {
            let r = e.into_response();
            let code = r.status();
            let bytes = futures_body(r);
            (code, serde_json::from_slice::<Value>(&bytes).unwrap())
        };
        let (code, v) = body(ApiError::new(
            StatusCode::UNAUTHORIZED,
            "API key parameter '[zabalack:v1 action=a kid=k mac=00]' is invalid",
        ));
        assert_eq!(code, StatusCode::UNAUTHORIZED);
        assert_eq!(v["message"], REDACTED_ERROR);
        assert_eq!(v["code"], 401);
        assert!(!v.to_string().contains("mac=00"));
        let (code, v) = body(ApiError::bad("not allowed to set customer to 'x'"));
        assert_eq!(code, StatusCode::BAD_REQUEST);
        assert_eq!(v["message"], "not allowed to set customer to 'x'");
    }

    /// A future's output, without an async runtime: everything polled here
    /// works on in-memory bodies and is ready on the first poll.
    fn ready<F: std::future::Future>(fut: F) -> F::Output {
        use std::pin::pin;
        use std::task::{Context, Poll, Waker};
        let mut fut = pin!(fut);
        match fut.as_mut().poll(&mut Context::from_waker(Waker::noop())) {
            Poll::Ready(v) => v,
            Poll::Pending => panic!("future not ready"),
        }
    }

    fn futures_body(r: Response) -> Vec<u8> {
        ready(axum::body::to_bytes(r.into_body(), usize::MAX))
            .unwrap()
            .to_vec()
    }

    /// axum's own rejection of a JSON body, as the router would send it.
    fn json_rejection(body: &str) -> Response {
        use axum::extract::FromRequest as _;
        let req = axum::http::Request::builder()
            .method("POST")
            .uri("/alert")
            .header(axum::http::header::CONTENT_TYPE, "application/json")
            .body(axum::body::Body::from(body.to_owned()))
            .unwrap();
        match ready(Json::<Value>::from_request(req, &())) {
            Ok(_) => panic!("{body} parsed"),
            Err(rejection) => rejection.into_response(),
        }
    }

    // Codex's PR176-FRAMEWORK-ERROR-SCOPE-1 counterexample: axum's rejection
    // text quotes the key path, outside ApiError.
    #[test]
    fn a_framework_rejection_carrying_evidence_is_redacted() {
        for key in [
            "[zabalack:v1 action=a kid=k mac=00]",
            "[ZaBaLaCk:v1 x]",
            "[ackline:x]",
        ] {
            let raw = json_rejection(&format!("{{\"{key}\": invalid}}"));
            let status = raw.status();
            let raw_text = String::from_utf8(futures_body(raw)).unwrap();
            assert!(raw_text.contains(key), "unredacted rejection: {raw_text}");
            let r = ready(redact_error_body(json_rejection(&format!(
                "{{\"{key}\": invalid}}"
            ))));
            assert_eq!(r.status(), status);
            let v: Value = serde_json::from_slice(&futures_body(r)).unwrap();
            assert_eq!(v["message"], REDACTED_ERROR);
            assert_eq!(v["code"], status.as_u16());
        }
        let plain = json_rejection("{\"plain\": invalid}");
        let (status, text) = (
            plain.status(),
            String::from_utf8(futures_body(plain)).unwrap(),
        );
        let r = ready(redact_error_body(json_rejection("{\"plain\": invalid}")));
        assert_eq!(r.status(), status);
        assert_eq!(String::from_utf8(futures_body(r)).unwrap(), text);
    }

    #[test]
    fn successful_responses_pass_unchanged() {
        let ok = (StatusCode::OK, "[zabalack:v1 marker in history]").into_response();
        let r = ready(redact_error_body(ok));
        assert_eq!(r.status(), StatusCode::OK);
        assert_eq!(futures_body(r), b"[zabalack:v1 marker in history]");
    }

    #[test]
    fn other_selectors_are_not_refused() {
        let allowed = [
            ("attributes.zabbix_ack_sync", "pending"),
            ("attributes.zabbix_ack_sync_reason!", "x"),
            ("attributes.source_system", "zabbix"),
            ("sort-by", "attributes.zabbix_ack_sync"),
            ("sort-by", "lastReceiveTime"),
            ("group-by", "attributes.region"),
            ("group-by", "environment"),
            ("q.df", "text"),
            ("q", "attributes.zabbix_ack_sync:pending"),
            ("status", "zabbix_ack_sync_intent"),
            ("resource", "attributes.zabbix_ack_sync_intent"),
            ("attributes.Zabbix_ack_sync_intent", "x"),
        ];
        for (k, v) in allowed {
            assert!(!restricted_selector(k, v), "{k}={v}");
        }
    }
}
