//! The bulk alert routes that need no owner choice (reference
//! `views/bulk.py`; `spec/security-fixes.md` SF-BULK): status, tag, untag
//! and attributes. `DELETE /_bulk/alerts` waits on the owner's BULK-Q1, and
//! `PUT /_bulk/alerts/action` and `GET /_bulk/task/{id}` on BULK-Q2.
//!
//! Every route needs `write:alerts`, as the single routes do, and selects
//! with the selection of `GET /alerts` **and the caller's customer
//! restriction** (BULK-SEC-1 fixed). No audit events: alrust has no audit
//! trail (BULK-SEC-3).

use alrust_store_pg::{AlertEdit, AlertQuery};
use axum::Json;
use axum::extract::{Query, State};
use axum::http::{HeaderMap, StatusCode};
use serde_json::{Value, json};

use super::{
    ApiError, ApiResult, AppState, Caller, Params, Shared, authenticate, opt_str, parse_search,
    parse_timeout, refuse_restricted_selectors,
};

/// `Alert.find_all`'s default page size: the status route changes at most
/// this many alerts, the newest first.
const STATUS_LIMIT: i64 = 1000;

/// Parameters of `GET /alerts` that do not select; the bulk routes do not
/// read them, so their checks do not apply.
const NOT_READ: [&str; 5] = ["page", "page-size", "sort-by", "group-by", "show-history"];

/// SF-BULK item 1: the selection of `GET /alerts`, with the caller's
/// customers. The hook 4.2 guard sees every parameter, read or not.
fn selection(st: &AppState, caller: &Caller, params: &[(String, String)]) -> ApiResult<AlertQuery> {
    refuse_restricted_selectors(params)?;
    let read: Vec<(String, String)> = params
        .iter()
        .filter(|(k, _)| !NOT_READ.contains(&k.as_str()))
        .cloned()
        .collect();
    Ok(parse_search(st, caller, &read)?.q)
}

fn updated(ids: &[String]) -> Json<Value> {
    Json(json!({"status": "ok", "updated": ids, "count": ids.len()}))
}

/// `PUT /_bulk/alerts/status`: a `status` (400 first, as in the
/// reference), the selection, at most 1000 alerts (404 when none), then the
/// single route's `set_status` on each, in its own transaction. Per-alert
/// errors are collected and, after the others, answered with the
/// reference's 500 `failed to bulk set alert status` and their messages.
pub(crate) async fn set_status(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Json(body): Json<Value>,
) -> ApiResult<(StatusCode, Json<Value>)> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("write:alerts")?;
    let status = opt_str(body.get("status"))
        .ok_or_else(|| ApiError::bad("must supply 'status' as json data"))?;
    let text = body
        .get("text")
        .map_or("bulk status update", |v| v.as_str().unwrap_or(""));
    let timeout = match body.get("timeout") {
        None | Some(Value::Null) => None,
        v => Some(parse_timeout(v, 0)?),
    };
    let q = selection(&st, &caller, &params)?;
    let ids = st.store.alert_ids(&q, STATUS_LIMIT).await?;
    if ids.is_empty() {
        return Err(ApiError::new(StatusCode::NOT_FOUND, "not found"));
    }
    let customers = caller.customers();
    let mut done = Vec::new();
    let mut errors = Vec::new();
    for id in &ids {
        match st
            .store
            .set_status(
                id,
                customers.as_deref(),
                &status,
                text,
                timeout,
                caller.login(),
            )
            .await
        {
            Ok(_) => done.push(id.clone()),
            Err(e) => errors.push(ApiError::from(e).message),
        }
    }
    if errors.is_empty() {
        return Ok((StatusCode::OK, updated(&done)));
    }
    Ok((
        StatusCode::INTERNAL_SERVER_ERROR,
        Json(json!({
            "status": "error",
            "message": "failed to bulk set alert status",
            "code": 500,
            "errors": errors,
        })),
    ))
}

/// The single route's `tags` rule: a non-empty list of strings, else 400
/// (the reference's message; its bulk routes answer an uncoded 500).
fn tags(body: &Value) -> ApiResult<Vec<String>> {
    match body.get("tags") {
        Some(Value::Array(a)) if !a.is_empty() && a.iter().all(Value::is_string) => Ok(a
            .iter()
            .filter_map(|v| v.as_str().map(str::to_owned))
            .collect()),
        _ => Err(ApiError::bad("must supply 'tags' as json list")),
    }
}

/// The selection, then `edit` on every selected alert, in one statement and
/// without a limit, as in the reference.
async fn apply(
    st: &AppState,
    caller: &Caller,
    params: &[(String, String)],
    edit: &AlertEdit,
) -> ApiResult<Json<Value>> {
    let q = selection(st, caller, params)?;
    Ok(updated(&st.store.edit_alerts(&q, edit).await?))
}

/// `PUT /_bulk/alerts/tag`.
pub(crate) async fn tag(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Json(body): Json<Value>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("write:alerts")?;
    let edit = AlertEdit::Tag(tags(&body)?);
    apply(&st, &caller, &params, &edit).await
}

/// `PUT /_bulk/alerts/untag`.
pub(crate) async fn untag(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Json(body): Json<Value>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("write:alerts")?;
    let edit = AlertEdit::Untag(tags(&body)?);
    apply(&st, &caller, &params, &edit).await
}

/// `PUT /_bulk/alerts/attributes`: a non-empty `attributes` object (400
/// otherwise, as on the single route).
pub(crate) async fn attributes(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Json(body): Json<Value>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("write:alerts")?;
    let edit = match body.get("attributes") {
        Some(Value::Object(m)) if !m.is_empty() => AlertEdit::Attributes(m.clone()),
        _ => return Err(ApiError::bad("must supply 'attributes' as json data")),
    };
    apply(&st, &caller, &params, &edit).await
}
