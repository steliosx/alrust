//! User groups (reference `views/groups.py`, `models/group.py`) with
//! `spec/security-fixes.md` SF-GROUPS: a group name that grants privileges
//! is held to the caller's scopes.
//!
//! A group's name acts as a role at a password login (G-SEC-1), so
//! creating, renaming or joining a group is held to the role ceiling with
//! `group_grants(name)`, the scopes that name grants (`scopes_by_match`).
//! Groups have no effect on authentication until alrust has a login. The
//! reference's audit events are not sent (alrust has no audit trail); the
//! auth-proxy defect G-DEF-1 does not arise (alrust has no auth proxy).

use alrust_store_pg::groups::COUNT;
use alrust_store_pg::list_query::FilterKind;
use axum::Json;
use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use serde_json::{Map, Value, json};

use super::list_params::{self, Direction, ListSpec};
use super::perms::hold_to_ceiling;
use super::{ApiError, ApiResult, AppState, Caller, Params, Shared, authenticate, supplied};

/// `Groups.VALID_PARAMS` (`postgres/utils.py:394-402`). `id` is excluded
/// from filtering. `count` is the member count, which the reference names
/// in its `WHERE` clause as a column it does not have (a database error);
/// here it compares the count's text.
const GROUPS: ListSpec = ListSpec {
    filters: &[
        ("name", "\"name\"", FilterKind::Text),
        ("text", "\"text\"", FilterKind::Text),
        (
            "count",
            "COALESCE(CARDINALITY(\"users\"), 0)::text",
            FilterKind::Text,
        ),
    ],
    sorts: &[
        ("name", "\"name\"", Direction::Asc),
        ("text", "\"text\"", Direction::Asc),
        ("count", COUNT, Direction::Asc),
    ],
};

const MISSING_NAME: &str = "Missing mandatory value for name";

/// SF-GROUPS: the scopes a group named `name` grants must be within the
/// caller's own.
async fn hold_group_name(st: &AppState, caller: &Caller, name: &str) -> ApiResult<()> {
    let grants = st.store.scopes_by_match("", &[name.to_owned()]).await?;
    hold_to_ceiling(caller, &grants)
}

/// `text`: absent or null is `None`; a string, else 400.
fn text_member(obj: &Map<String, Value>) -> ApiResult<Option<String>> {
    match obj.get("text") {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) => Ok(Some(s.clone())),
        Some(_) => Err(ApiError::bad("text must be a string")),
    }
}

/// `POST /group`: a non-empty `name` (the model's own 400), then the
/// ceiling on what the name grants, then the insert.
pub(crate) async fn create_group(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Json(body): Json<Value>,
) -> ApiResult<(StatusCode, Json<Value>)> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("admin:groups")?;
    let obj = body
        .as_object()
        .ok_or_else(|| ApiError::bad("group must be a JSON object"))?;
    let name = match obj.get("name") {
        Some(Value::String(s)) if !s.is_empty() => s.clone(),
        _ => return Err(ApiError::bad(MISSING_NAME)),
    };
    let text = text_member(obj)?.unwrap_or_default();
    let id = supplied(obj, "id")?.unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    hold_group_name(&st, &caller, &name).await?;
    let group = st.store.create_group(&id, &name, &text).await?;
    Ok((
        StatusCode::CREATED,
        Json(json!({
            "status": "ok",
            "id": group.id,
            "group": group.body(&st.cfg.base_url),
        })),
    ))
}

/// `GET /group/{id}`.
pub(crate) async fn get_group(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("read:groups")?;
    let group = st.store.get_group(&id).await?;
    Ok(Json(json!({
        "status": "ok",
        "total": 1,
        "group": group.body(&st.cfg.base_url),
    })))
}

/// `GET /group/{id}/users`.
pub(crate) async fn get_group_users(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("read:groups")?;
    st.store.get_group(&id).await?;
    let users: Vec<Value> = st
        .store
        .group_users(&id)
        .await?
        .iter()
        .map(|u| u.body(&st.cfg.base_url))
        .collect();
    Ok(Json(json!({
        "status": "ok",
        "total": users.len(),
        "users": users,
    })))
}

/// `GET /groups`.
pub(crate) async fn list_groups(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("read:groups")?;
    let q = list_params::parse(&params, &GROUPS)?;
    let total = st.store.count_groups(&q).await?;
    let page = list_params::page(&params, st.cfg.default_page_size, total)?;
    let groups: Vec<Value> = st
        .store
        .list_groups(&q, page.page, page.page_size)
        .await?
        .iter()
        .map(|g| g.body(&st.cfg.base_url))
        .collect();
    let found = !groups.is_empty();
    let mut body = json!({
        "status": "ok",
        "page": page.page,
        "pageSize": page.page_size,
        "pages": page.pages,
        "more": page.more(),
        "groups": groups,
        "total": if found { total } else { 0 },
    });
    if !found {
        body["message"] = json!("not found");
    }
    Ok(Json(body))
}

/// `PUT /group/{id}` (SF-GROUPS): an empty body, the group (404, where the
/// reference looks it up), a `name` that is a non-empty string when given
/// (GR-R1), a string `text`, and the ceiling on what a given name grants.
pub(crate) async fn update_group(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
    Json(body): Json<Value>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("admin:groups")?;
    let obj = match body.as_object() {
        Some(o) if !o.is_empty() => o,
        _ => return Err(ApiError::bad("nothing to change")),
    };
    st.store.get_group(&id).await?;
    let name = match obj.get("name") {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) if !s.is_empty() => Some(s.as_str()),
        Some(_) => return Err(ApiError::bad(MISSING_NAME)),
    };
    let text = text_member(obj)?;
    if let Some(n) = name {
        hold_group_name(&st, &caller, n).await?;
    }
    let group = st.store.update_group(&id, name, text.as_deref()).await?;
    Ok(Json(json!({
        "status": "ok",
        "group": group.body(&st.cfg.base_url),
    })))
}

/// The group (404) and the user (400 `invalid user`) of a membership change.
async fn membership(st: &AppState, group_id: &str, user_id: &str) -> ApiResult<String> {
    let group = st.store.get_group(group_id).await?;
    if !st.store.user_exists(user_id).await? {
        return Err(ApiError::bad("invalid user"));
    }
    Ok(group.name)
}

/// `PUT /group/{group_id}/user/{user_id}` (SF-GROUPS 3): held to what the
/// group's current name grants.
pub(crate) async fn add_user(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path((group_id, user_id)): Path<(String, String)>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("admin:groups")?;
    let name = membership(&st, &group_id, &user_id).await?;
    hold_group_name(&st, &caller, &name).await?;
    st.store.add_user_to_group(&group_id, &user_id).await?;
    Ok(Json(json!({"status": "ok"})))
}

/// `DELETE /group/{group_id}/user/{user_id}`.
pub(crate) async fn remove_user(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path((group_id, user_id)): Path<(String, String)>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("admin:groups")?;
    membership(&st, &group_id, &user_id).await?;
    st.store.remove_user_from_group(&group_id, &user_id).await?;
    Ok(Json(json!({"status": "ok"})))
}

/// `DELETE /group/{id}`.
pub(crate) async fn delete_group(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("admin:groups")?;
    st.store.delete_group(&id).await?;
    Ok(Json(json!({"status": "ok"})))
}
