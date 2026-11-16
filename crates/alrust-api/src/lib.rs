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
        Credential::Key(k) => match st.store.use_key(&k).await? {
            Some(key) => Ok(Caller {
                login: Some(key.user),
                // use_key matches only keys with scopes.
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
        .route("/alert", post(receive))
        .route("/alert/{id}", get(get_alert).delete(delete_alert))
        .route("/alert/{id}/status", put(set_status))
        .route("/alert/{id}/action", put(take_action))
        .route("/alert/{id}/tag", put(tag_alert))
        .route("/alert/{id}/untag", put(untag_alert))
        .route("/alert/{id}/attributes", put(update_attributes))
        .route("/alerts", get(search_alerts))
        .route("/alerts/count", get(count_alerts))
        .route("/heartbeat", post(create_heartbeat))
        .route(
            "/heartbeat/{id}",
            get(get_heartbeat).delete(delete_heartbeat),
        )
        .route("/heartbeats", get(list_heartbeats))
        .route("/key", post(create_key))
        .route("/keys", get(list_keys))
        .route("/key/{id}", axum::routing::delete(delete_key))
        .route("/management/gtg", get(gtg))
        .route("/management/healthcheck", get(healthcheck))
        .route(
            "/management/housekeeping",
            get(housekeeping).post(housekeeping),
        )
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
    let mut n = parse_alert(&body, &st, st.store.config().alert_timeout)?;
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
            let msg = "Alert converted to Heartbeat";
            return Ok((
                StatusCode::ACCEPTED,
                Json(json!({"status": "ok", "message": msg, "id": hb.id})),
            )
                .into_response());
        }
    }

    let (alert, _kind) = st.store.receive_with(n, &caller.facts(&st)).await?;
    Ok((
        StatusCode::CREATED,
        Json(json!({"status": "ok", "id": alert.id, "alert": alert_json(&st, &alert)})),
    )
        .into_response())
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
