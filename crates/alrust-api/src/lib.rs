//! HTTP API for alrust (axum), Alerta-compatible surface plus SSE.
//!
//! The alpha covers the core Alerta routes: receive, get, query, delete,
//! status, action, heartbeats, API keys, health and housekeeping. Paths,
//! status codes and JSON shapes follow the reference v9.1.0 views.

use std::sync::Arc;

use alrust_domain::{Alert, ApiKey, iso8601, parse_time, scope_allows, truncate_ms};
use alrust_store_pg::{AlertQuery, NewAlert, Store, StoreError};
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
        }
    }
}

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
    fn into_response(self) -> Response {
        let body = json!({"status": "error", "message": self.message, "code": self.code.as_u16(), "errors": null});
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
                tracing::error!(error = %e, "database error");
                ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "database error")
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
}

impl Caller {
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

/// A presented key is checked before `AUTH_REQUIRED` (C1); only the secret
/// authenticates (SF-KEYS S4), never the public id.
async fn authenticate(
    st: &AppState,
    headers: &HeaderMap,
    params: &[(String, String)],
) -> ApiResult<Caller> {
    match key_from_request(headers, params) {
        Some(k) => match st.store.use_key(&k).await? {
            Some(key) => Ok(Caller {
                login: Some(key.user),
                scopes: key.scopes,
                customer: key.customer,
                auth_off: false,
            }),
            None => Err(ApiError::new(
                StatusCode::UNAUTHORIZED,
                format!("API key parameter '{k}' is invalid"),
            )),
        },
        None if !st.cfg.auth_required => Ok(Caller {
            login: None,
            scopes: Vec::new(),
            customer: None,
            auth_off: true,
        }),
        None => Err(ApiError::new(
            StatusCode::UNAUTHORIZED,
            "Missing authorization API Key or Bearer Token",
        )),
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

pub fn router(state: Shared) -> Router {
    Router::new()
        .route("/", get(index))
        .route("/alert", post(receive))
        .route("/alert/{id}", get(get_alert).delete(delete_alert))
        .route("/alert/{id}/status", put(set_status))
        .route("/alert/{id}/action", put(take_action))
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

/// `timeout` parsing (spec R0.4): null/absent → default; bool → 1/0;
/// negative → 400; strings and floats → 400.
fn parse_timeout(v: Option<&Value>, default: i64) -> ApiResult<i64> {
    match v {
        None | Some(Value::Null) => Ok(default),
        Some(Value::Bool(b)) => Ok(i64::from(*b)),
        Some(Value::Number(n)) => match n.as_i64() {
            Some(t) if t >= 0 => Ok(t),
            Some(_) => Err(ApiError::bad(
                "timeout must be an integer greater than or equal to 0",
            )),
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
    // An integer value is stored as a string; JSON booleans as "True"/"False" (spec R0.3).
    let value = match obj.get("value") {
        Some(Value::Bool(b)) => Value::String(if *b { "True" } else { "False" }.into()),
        Some(Value::Number(n)) if n.is_i64() || n.is_u64() => Value::String(n.to_string()),
        Some(v) => v.clone(),
        None => Value::Null,
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
    serde_json::to_value(a.body(&st.cfg.base_url, st.store.config().alert_timeout, true))
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

    let (alert, _kind) = st.store.receive(n, caller.login()).await?;
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
        .take_action(
            &id,
            caller.customers().as_deref(),
            &action,
            text,
            timeout,
            caller.login(),
        )
        .await?;
    Ok(Json(json!({"status": "ok"})))
}

/// Query parameters shared by `/alerts` and `/alerts/count`.
struct Search {
    q: AlertQuery,
    page: i64,
    page_size: i64,
    show_history: bool,
}

fn parse_search(st: &AppState, caller: &Caller, params: &[(String, String)]) -> ApiResult<Search> {
    let mut q = AlertQuery {
        customers: caller.customers(),
        ..AlertQuery::default()
    };
    let mut page = 1i64;
    let mut page_size = st.cfg.default_page_size;
    let mut show_history = false;
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
            "page" => {
                page = v
                    .parse()
                    .map_err(|_| ApiError::bad("page must be an integer"))?
            }
            "page-size" => {
                page_size = v
                    .parse()
                    .map_err(|_| ApiError::bad("page-size must be an integer"))?
            }
            "show-history" => {
                show_history = matches!(
                    v.to_lowercase().as_str(),
                    "true" | "t" | "1" | "yes" | "y" | "on"
                )
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
    let r = st.store.find_alerts(&s.q, 1, 1).await?;
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
    let r = st.store.find_alerts(&q, page, page_size).await?;
    let pages = (r.total + page_size - 1) / page_size;
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
        .map(|a| a.last_receive_time)
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
    let wanted: Vec<&str> = params
        .iter()
        .filter(|(k, _)| k == "status")
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
        None | Some(Value::Null) | Some(Value::Bool(false)) => Ok(None),
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
        scopes,
        text: opt_str(obj.get("text")).unwrap_or_else(|| "API Key for alrust".into()),
        expire_time: truncate_ms(expire_time),
        count: 0,
        last_used_time: None,
        customer,
    };
    let k = st.store.create_key(&key).await?;
    // The secret is shown once, here (owner decision Y6, SF-KEYS S1).
    let data =
        serde_json::to_value(k.body(&st.cfg.base_url, Utc::now(), true)).unwrap_or(Value::Null);
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
        .map(|k| serde_json::to_value(k.body(&st.cfg.base_url, now, false)).unwrap_or(Value::Null))
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

async fn gtg() -> &'static str {
    "OK"
}

/// `GET /management/healthcheck` (spec HB5). Reproduces the reference's use
/// of the seconds *component* of the heartbeat age (HB5.2).
async fn healthcheck(State(st): State<Shared>) -> Response {
    let hbs = match st.store.list_heartbeats(None).await {
        Ok(h) => h,
        Err(_) => return (StatusCode::SERVICE_UNAVAILABLE, "HEALTH_CHECK_FAILED").into_response(),
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
    let expired_s = hours("expired")
        .filter(|h| *h != 0)
        .map(|h| h * 3600)
        .unwrap_or(st.cfg.delete_expired_after_s);
    let info_s = hours("info")
        .filter(|h| *h != 0)
        .map(|h| h * 3600)
        .unwrap_or(st.cfg.delete_info_after_s);
    let expired = st.store.housekeeping(expired_s, info_s).await?;
    let count = expired.len();
    Ok(Json(
        json!({"status": "ok", "expired": expired, "unshelve": [], "unack": [], "count": count}),
    ))
}
