//! Permissions (reference `views/permissions.py`, `models/permission.py`)
//! with `spec/security-fixes.md` SF-PERMS: a permission edit holds the same
//! checks as a permission creation.
//!
//! Permission rows map role names to scopes. They have no effect on
//! authentication today: API keys carry their scopes, Bearer tokens their
//! `scope` claim, and roles count only at a password or OIDC login, which
//! alrust does not have yet. The reference's `admin_audit_trail` events are
//! not sent: alrust has no audit trail.

use alrust_domain::roles::{
    ADMIN_ROLE, ADMIN_SCOPES, GUEST_DEFAULT_SCOPES, GUEST_ROLE, KNOWN_SCOPES, RESERVED_ROLES,
    USER_DEFAULT_SCOPES, USER_ROLE, is_known_scope,
};
use alrust_domain::scope_allows;
use alrust_store_pg::list_query::FilterKind;
use alrust_store_pg::perms::Perm;
use axum::Json;
use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use serde_json::{Map, Value, json};

use super::list_params::{self, Direction, ListSpec};
use super::{ApiError, ApiResult, Caller, Params, Shared, authenticate, supplied};

/// `Permissions.VALID_PARAMS` (`postgres/utils.py:418-426`). `id` is
/// excluded from filtering; `scopes` has no filter column, so `?scopes=` is
/// 400 `Invalid filter parameter: scopes`, as in the reference. `scope`
/// filters by overlap; the reference compares the array with one text
/// value, a database error.
const PERMS: ListSpec = ListSpec {
    filters: &[
        ("match", "\"match\"", FilterKind::Text),
        ("scope", "\"scopes\"", FilterKind::Array),
    ],
    sorts: &[
        ("match", "\"match\"", Direction::Asc),
        ("scopes", "\"scopes\"", Direction::Asc),
    ],
};

/// The role ceiling (`views/permissions.py:33-36`; the same check as key
/// creation): every scope must be within the caller's own, with alrust's
/// `scope_allows`. That does not derive `delete:x` from `write:x`, which the
/// reference does while `DELETE_SCOPES` is empty. With authentication off
/// and no credential the caller holds no scope, so any scope is refused.
pub(crate) fn hold_to_ceiling(caller: &Caller, wanted: &[String]) -> ApiResult<()> {
    for want in wanted {
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
    Ok(())
}

/// `scopes` (SF-PERMS input contract): `None` when absent; when present, a
/// JSON array of strings.
fn scopes_member(obj: &Map<String, Value>) -> ApiResult<Option<Vec<String>>> {
    match obj.get("scopes") {
        None => Ok(None),
        Some(Value::Array(items)) => items
            .iter()
            .map(|v| match v {
                Value::String(s) => Ok(s.clone()),
                other => Err(ApiError::bad(format!("'{other}' is not a valid Scope"))),
            })
            .collect::<ApiResult<Vec<_>>>()
            .map(Some),
        Some(_) => Err(ApiError::bad("scopes must be a list")),
    }
}

/// SF-PERMS step 2: every scope is a known one.
fn known(scopes: &[String]) -> ApiResult<()> {
    match scopes.iter().find(|s| !is_known_scope(s)) {
        Some(s) => Err(ApiError::bad(format!("'{s}' is not a valid Scope"))),
        None => Ok(()),
    }
}

/// SF-PERMS step 3: the built-in role names are not rows.
fn not_reserved(role: &str) -> ApiResult<()> {
    if RESERVED_ROLES.contains(&role) {
        Err(ApiError::new(
            StatusCode::CONFLICT,
            format!("{role} role already exists"),
        ))
    } else {
        Ok(())
    }
}

/// The three built-in roles as `GET /perms` lists them, each with a fresh
/// id, as the reference builds them on every request.
fn system_roles() -> [Perm; 3] {
    let perm = |role: &str, scopes: &[&str]| Perm {
        id: uuid::Uuid::new_v4().to_string(),
        role: role.to_owned(),
        scopes: scopes.iter().map(|s| (*s).to_owned()).collect(),
    };
    [
        perm(ADMIN_ROLE, &ADMIN_SCOPES),
        perm(USER_ROLE, &USER_DEFAULT_SCOPES),
        perm(GUEST_ROLE, &GUEST_DEFAULT_SCOPES),
    ]
}

/// `POST /perm`: the input contract (`scopes`, then `match` and `id`), known
/// scopes, reserved names, the ceiling, then the insert. A missing or
/// duplicate `match` is the database's 500, as in the reference.
pub(crate) async fn create_perm(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Json(body): Json<Value>,
) -> ApiResult<(StatusCode, Json<Value>)> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("admin:perms")?;
    let obj = body
        .as_object()
        .ok_or_else(|| ApiError::bad("permission must be a JSON object"))?;
    let scopes = scopes_member(obj)?.unwrap_or_default();
    let role = match obj.get("match") {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) => Some(s.as_str()),
        Some(_) => return Err(ApiError::bad("match must be a string")),
    };
    let id = supplied(obj, "id")?.unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    known(&scopes)?;
    if let Some(r) = role {
        not_reserved(r)?;
    }
    hold_to_ceiling(&caller, &scopes)?;
    let perm = st.store.create_perm(&id, role, &scopes).await?;
    Ok((
        StatusCode::CREATED,
        Json(json!({
            "status": "ok",
            "id": perm.id,
            "permission": perm.body(&st.cfg.base_url),
        })),
    ))
}

/// `GET /perm/{id}`.
pub(crate) async fn get_perm(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("read:perms")?;
    let perm = st.store.get_perm(&id).await?;
    Ok(Json(json!({
        "status": "ok",
        "total": 1,
        "permission": perm.body(&st.cfg.base_url),
    })))
}

/// `GET /perms`: the three built-in roles first on every page, counted in
/// `total`, then one page of the rows (offset by the page, not by the
/// roles), as in the reference.
pub(crate) async fn list_perms(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("read:perms")?;
    let q = list_params::parse(&params, &PERMS)?;
    let total = st.store.count_perms(&q).await? + 3;
    let page = list_params::page(&params, st.cfg.default_page_size, total)?;
    let base = &st.cfg.base_url;
    let mut permissions: Vec<Value> = system_roles().iter().map(|p| p.body(base)).collect();
    for p in st.store.list_perms(&q, page.page, page.page_size).await? {
        permissions.push(p.body(base));
    }
    Ok(Json(json!({
        "status": "ok",
        "page": page.page,
        "pageSize": page.page_size,
        "pages": page.pages,
        "more": page.more(),
        "permissions": permissions,
        "total": total,
    })))
}

/// `PUT /perm/{id}` (SF-PERMS): an empty body, then the input contract,
/// known scopes, the row (404, where the reference looks it up), a reserved
/// new name, and the ceiling on the scopes the row will grant: the new
/// `scopes` when given, else the row's own when `match` changes.
pub(crate) async fn update_perm(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
    Json(body): Json<Value>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("admin:perms")?;
    let obj = match body.as_object() {
        Some(o) if !o.is_empty() => o,
        _ => return Err(ApiError::bad("nothing to change")),
    };
    let scopes = scopes_member(obj)?;
    let role = match obj.get("match") {
        None => None,
        Some(Value::String(s)) if !s.is_empty() => Some(s.as_str()),
        Some(_) => return Err(ApiError::bad("match must be a non-empty string")),
    };
    if let Some(s) = &scopes {
        known(s)?;
    }
    let current = st.store.get_perm(&id).await?;
    if let Some(r) = role {
        not_reserved(r)?;
    }
    let granted: &[String] = match (&scopes, role) {
        (Some(s), _) => s,
        (None, Some(r)) if r != current.role => &current.scopes,
        _ => &[],
    };
    hold_to_ceiling(&caller, granted)?;
    let perm = st.store.update_perm(&id, role, scopes.as_deref()).await?;
    Ok(Json(json!({
        "status": "ok",
        "permission": perm.body(&st.cfg.base_url),
    })))
}

/// `DELETE /perm/{id}`.
pub(crate) async fn delete_perm(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
    Path(id): Path<String>,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("admin:perms")?;
    st.store.delete_perm(&id).await?;
    Ok(Json(json!({"status": "ok"})))
}

/// `GET /scopes`: the known scopes, in the reference's order.
pub(crate) async fn list_scopes(
    State(st): State<Shared>,
    headers: HeaderMap,
    Query(params): Params,
) -> ApiResult<Json<Value>> {
    let caller = authenticate(&st, &headers, &params).await?;
    caller.require("read:perms")?;
    Ok(Json(json!({
        "status": "ok",
        "scopes": KNOWN_SCOPES,
        "total": KNOWN_SCOPES.len(),
    })))
}
