//! Users (reference `views/users.py`, `models/user.py`) with
//! `spec/security-fixes.md` SF-USERS, update half.
//!
//! `POST /user` is not served: a new user needs a password hash, and alrust
//! has no password hashing yet (R-NOTICE-2), so SF-USERS' create half waits
//! with it. For the same reason a `password` member on an update is refused.
//! `/user/me` is the caller's own user: the `sub` of a verified operator
//! token whose provider is the configured one (A1). An API key has no user,
//! so `/user/me` is 404 for it, as in the reference. No audit events (alrust
//! has no audit trail).

use alrust_store_pg::list_query::FilterKind;
use alrust_store_pg::users::UserChanges;
use axum::Json;
use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use serde_json::{Map, Value, json};

use super::list_params::{self, Direction, ListSpec};
use super::perms::hold_to_ceiling;
use super::{ApiError, ApiResult, AppState, Caller, Params, Shared, authenticate};

/// `Users.VALID_PARAMS` (`postgres/utils.py:360-378`). `role` overlaps the
/// roles (the reference compares a `text[]` column with text, a database
/// error); the times and `email_verified` compare as their types (the
/// reference fails on several values). `domain` is no column, so it is
/// refused here (a database error in the reference); `roles` and `id` do
/// not filter, as in the reference.
const USERS: ListSpec = ListSpec {
    filters: &[
        ("name", "\"name\"", FilterKind::Text),
        ("login", "\"login\"", FilterKind::Text),
        ("email", "\"email\"", FilterKind::Text),
        ("status", "\"status\"", FilterKind::Text),
        ("role", "\"roles\"", FilterKind::Array),
        ("attributes", "\"attributes\"", FilterKind::Contains),
        (
            "createTime",
            "\"create_time\"",
            FilterKind::Typed("timestamp"),
        ),
        (
            "lastLogin",
            "\"last_login\"",
            FilterKind::Typed("timestamp"),
        ),
        ("text", "\"text\"", FilterKind::Text),
        (
            "updateTime",
            "\"update_time\"",
            FilterKind::Typed("timestamp"),
        ),
        (
            "email_verified",
            "\"email_verified\"",
            FilterKind::Typed("boolean"),
        ),
    ],
    sorts: &[
        ("name", "\"name\"", Direction::Asc),
        ("login", "\"login\"", Direction::Asc),
        ("email", "\"email\"", Direction::Asc),
        ("status", "\"status\"", Direction::Asc),
        ("roles", "\"roles\"", Direction::Asc),
        ("attributes", "\"attributes\"", Direction::Asc),
        ("createTime", "\"create_time\"", Direction::Desc),
        ("lastLogin", "\"last_login\"", Direction::Desc),
        ("text", "\"text\"", Direction::Asc),
        ("updateTime", "\"update_time\"", Direction::Desc),
        ("email_verified", "\"email_verified\"", Direction::Asc),
    ],
};

const NOTHING: &str = "nothing to change";
const PASSWORD_GATE: &str = "password cannot be changed: alrust has no password hashing yet";
const NO_ATTRIBUTES: &str = "must supply 'attributes' as json data";
/// `ALLOWED_SELF_UPDATE_FIELDS`.
const SELF_FIELDS: [&str; 4] = ["email", "name", "password", "text"];

/// A JSON object with at least one member, else 400 `nothing to change`.
fn changes_object(body: &Value) -> ApiResult<&Map<String, Value>> {
    match body.as_object() {
        Some(o) if !o.is_empty() => Ok(o),
        _ => Err(ApiError::bad(NOTHING)),
    }
}

/// The caller's own user id, else 404 (the reference's `g.user_id` of
/// `None` finds no user).
fn own_id(caller: &Caller) -> ApiResult<String> {
    caller
        .operator
        .as_ref()
        .map(|op| op.user_id().to_owned())
        .ok_or_else(|| ApiError::new(StatusCode::NOT_FOUND, "not found"))
}

/// 409 when another user has e-mail `email`.
async fn email_free(st: &AppState, email: Option<&str>, id: &str) -> ApiResult<()> {
    let Some(email) = email else {
        return Ok(());
    };
    match st.store.user_id_by_email(email).await? {
        Some(other) if other != id => Err(ApiError::new(
            StatusCode::CONFLICT,
            "user with that email already exists",
        )),
        _ => Ok(()),
    }
}

/// SF-USERS update steps 1 to 3: the effective new roles, `None` when the
/// body sets none.
fn new_roles(obj: &Map<String, Value>) -> ApiResult<Option<Vec<String>>> {
    let roles = match obj.get("roles") {
        None | Some(Value::Null) => None,
        Some(Value::Array(items)) => Some(
            items
                .iter()
                .map(|v| v.as_str().map(str::to_owned))
                .collect::<Option<Vec<_>>>()
                .ok_or_else(|| ApiError::bad("roles must be a list of strings"))?,
        ),
        Some(_) => return Err(ApiError::bad("roles must be a list of strings")),
    };
    let role = match obj.get("role") {
        None => None,
        Some(Value::String(s)) if !s.is_empty() => Some(s.clone()),
        Some(_) => return Err(ApiError::bad("role must be a non-empty string")),
    };
    match (role, roles) {
        (Some(_), Some(_)) => Err(ApiError::bad("role and roles are mutually exclusive")),
        (Some(role), None) => Ok(Some(vec![role])),
        (None, roles) => Ok(roles),
    }
}

/// `User.update`'s checks on the members it writes (null leaves a member
/// as it is): a given e-mail needs an `@` (the reference's uncaught
/// `ValueError`, a 500) and resets `email_verified` unless the body sets
/// it; a password is refused (no hashing yet); the other members must have
/// their column's type.
fn changes(obj: &Map<String, Value>, roles: Option<Vec<String>>) -> ApiResult<UserChanges> {
    let text = |field: &str| -> ApiResult<Option<String>> {
        match obj.get(field) {
            None | Some(Value::Null) => Ok(None),
            Some(Value::String(s)) => Ok(Some(s.clone())),
            Some(_) => Err(ApiError::bad(format!("{field} must be a string"))),
        }
    };
    let email = text("email")?;
    if let Some(e) = email.as_deref()
        && !e.contains('@')
    {
        return Err(ApiError::bad(format!("Value for \"email\" not valid: {e}")));
    }
    if !matches!(obj.get("password"), None | Some(Value::Null)) {
        return Err(ApiError::bad(PASSWORD_GATE));
    }
    let email_verified = match obj.get("email_verified") {
        None => email.as_ref().map(|_| false),
        Some(Value::Null) => None,
        Some(Value::Bool(b)) => Some(*b),
        Some(_) => return Err(ApiError::bad("email_verified must be a boolean")),
    };
    let attributes = match obj.get("attributes") {
        None | Some(Value::Null) => None,
        Some(v @ Value::Object(_)) => Some(v.clone()),
        Some(_) => return Err(ApiError::bad("attributes must be an object")),
    };
    Ok(UserChanges {
        name: text("name")?,
        login: text("login")?,
        email,
        status: text("status")?,
        roles,
        attributes,
        text: text("text")?,
        email_verified,
    })
}

/// `utils.collections.merge`: nested objects merge, anything else replaces.
fn merge(into: &mut Map<String, Value>, from: &Map<String, Value>) {
    for (key, value) in from {
        if let (Some(Value::Object(a)), Value::Object(b)) = (into.get_mut(key), value) {
            merge(a, b);
        } else {
            into.insert(key.clone(), value.clone());
        }
    }
}

/// `update_user_attributes`: a non-empty object `attributes` (else 400; a
/// non-object is the reference's 500), the user (404), the deep merge, and
/// the top-level members that are null removed.
async fn merge_attributes(st: &AppState, id: &str, body: &Value) -> ApiResult<Json<Value>> {
    let new = match body.get("attributes") {
        Some(Value::Object(o)) if !o.is_empty() => o,
        _ => return Err(ApiError::bad(NO_ATTRIBUTES)),
    };
    let user = st.store.get_user(id).await?;
    let mut merged = user.attributes.as_object().cloned().unwrap_or_default();
    merge(&mut merged, new);
    merged.retain(|_, v| !v.is_null());
    st.store
        .set_user_attributes(id, &Value::Object(merged))
        .await?;
    Ok(Json(json!({"status": "ok"})))
}

/// `GET /user/{id}`.
pub(crate) async fn get_user(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("admin:users")?;
    let user = st.store.get_user(&id).await?;
    Ok(Json(json!({
        "status": "ok",
        "total": 1,
        "user": user.body(&st.cfg.base_url),
    })))
}

/// `GET /user/{id}/groups`.
pub(crate) async fn get_user_groups(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("admin:users")?;
    st.store.get_user(&id).await?;
    let groups: Vec<Value> = st
        .store
        .groups_of_user(&id)
        .await?
        .iter()
        .map(|g| g.body(&st.cfg.base_url))
        .collect();
    let mut body = json!({"status": "ok", "total": groups.len(), "groups": groups});
    if groups.is_empty() {
        body["message"] = json!("not found");
    }
    Ok(Json(body))
}

/// `GET /user/me`.
pub(crate) async fn get_me(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    let user = st.store.get_user(&own_id(&caller)?).await?;
    Ok(Json(json!({
        "status": "ok",
        "total": 1,
        "user": user.body(&st.cfg.base_url),
    })))
}

/// `GET /user/me/attributes`.
pub(crate) async fn get_me_attributes(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    let user = st.store.get_user(&own_id(&caller)?).await?;
    Ok(Json(json!({
        "status": "ok",
        "total": 1,
        "attributes": user.attributes,
    })))
}

/// `GET /users`. `domains` is the reference's default
/// `ALLOWED_EMAIL_DOMAINS` (alrust has no such setting).
pub(crate) async fn list_users(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("admin:users")?;
    if let Some((key, _)) = params.iter().find(|(k, _)| k.replace('!', "") == "domain") {
        return Err(ApiError::bad(format!(
            "Filtering by '{key}' field not supported."
        )));
    }
    let q = list_params::parse(&params, &USERS)?;
    let total = st.store.count_users(&q).await?;
    let page = list_params::page(&params, st.cfg.default_page_size, total)?;
    let users: Vec<Value> = st
        .store
        .list_users(&q, page.page, page.page_size)
        .await?
        .iter()
        .map(|u| u.body(&st.cfg.base_url))
        .collect();
    let found = !users.is_empty();
    let mut body = json!({
        "status": "ok",
        "page": page.page,
        "pageSize": page.page_size,
        "pages": page.pages,
        "more": page.more(),
        "users": users,
        "domains": ["*"],
        "total": if found { total } else { 0 },
    });
    if !found {
        body["message"] = json!("not found");
    }
    Ok(Json(body))
}

/// `PUT /user/{id}`: an empty body (400), the user (404), a truthy e-mail
/// another user has (409), SF-USERS steps 1 to 4, the model's checks, then
/// the write. All refusals come before any change.
pub(crate) async fn update_user(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
    Json(body): Json<Value>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("admin:users")?;
    let obj = changes_object(&body)?;
    st.store.get_user(&id).await?;
    let email = match obj.get("email") {
        Some(Value::String(s)) if !s.is_empty() => Some(s.as_str()),
        _ => None,
    };
    email_free(&st, email, &id).await?;
    let roles = new_roles(obj)?;
    if let Some(r) = roles.as_ref().filter(|r| !r.is_empty()) {
        hold_to_ceiling(&caller, &st.store.scopes_by_match("", r).await?)?;
    }
    let user = st.store.update_user(&id, &changes(obj, roles)?).await?;
    Ok(Json(json!({
        "status": "ok",
        "user": user.body(&st.cfg.base_url),
    })))
}

/// `PUT /user/me`: an empty body (400), members other than `name`,
/// `email`, `password` and `text` (400), the caller's user (404), an
/// e-mail that is present and another user's (409), the model's checks.
pub(crate) async fn update_me(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Json(body): Json<Value>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    let obj = changes_object(&body)?;
    let mut refused: Vec<&str> = obj
        .keys()
        .map(String::as_str)
        .filter(|k| !SELF_FIELDS.contains(k))
        .collect();
    if !refused.is_empty() {
        refused.sort_unstable();
        return Err(ApiError::bad(format!(
            "not allowed to update: {}",
            refused.join(", ")
        )));
    }
    let id = own_id(&caller)?;
    st.store.get_user(&id).await?;
    email_free(&st, obj.get("email").and_then(Value::as_str), &id).await?;
    let user = st.store.update_user(&id, &changes(obj, None)?).await?;
    Ok(Json(json!({
        "status": "ok",
        "user": user.body(&st.cfg.base_url),
    })))
}

/// `PUT /user/{id}/attributes`.
pub(crate) async fn update_user_attributes(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
    Json(body): Json<Value>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("admin:users")?;
    merge_attributes(&st, &id, &body).await
}

/// `PUT /user/me/attributes`.
pub(crate) async fn update_me_attributes(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Json(body): Json<Value>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    if !matches!(body.get("attributes"), Some(Value::Object(o)) if !o.is_empty()) {
        return Err(ApiError::bad(NO_ATTRIBUTES));
    }
    merge_attributes(&st, &own_id(&caller)?, &body).await
}

/// `DELETE /user/{id}`.
pub(crate) async fn delete_user(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("admin:users")?;
    st.store.delete_user(&id).await?;
    Ok(Json(json!({"status": "ok"})))
}
