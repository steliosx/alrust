//! List parameters for the admin lists: the reference's `filter_query` and
//! `sort_by_columns` (`postgres/utils.py:24-104`) and `Page.from_params`
//! (`utils/paging.py`), with the conventions `GET /customers` (#243) set:
//! - an unknown filter is 400 `Invalid filter parameter: <name>`, as in the
//!   reference; an empty value among several is 400 `Filtering by '<name>'
//!   field not supported.`;
//! - `sort-by` on an unsupported field is 400 `Sorting by '<name>' field
//!   not supported.`;
//! - `page` and `page-size` must be integers (the reference silently uses
//!   the default for a non-integer), at least 1, else 400; a page past the
//!   last is 416 `page out of range: 1-<pages>`. `limit`, the reference's
//!   older name for `page-size`, is not read by any alrust list.

use alrust_store_pg::list_query::{FilterKind, ListFilter, ListQuery};
use axum::http::StatusCode;

use super::{ApiError, ApiResult};

/// `EXCLUDE_FROM_QUERY` (`postgres/utils.py:19-22`): no filter reads these.
const EXCLUDED: [&str; 16] = [
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

/// A sort field's default direction (`VALID_PARAMS`' third member).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Direction {
    Asc,
    Desc,
}

/// A list's filters, `(name, quoted column, kind)`, and sort fields,
/// `(name, quoted column, default direction)`.
pub(crate) struct ListSpec {
    pub(crate) filters: &'static [(&'static str, &'static str, FilterKind)],
    pub(crate) sorts: &'static [(&'static str, &'static str, Direction)],
}

/// The filters and order `params` ask for.
pub(crate) fn parse(params: &[(String, String)], spec: &ListSpec) -> ApiResult<ListQuery> {
    let mut q = ListQuery::default();
    let mut seen: Vec<&str> = Vec::new();
    for (key, _) in params {
        if seen.contains(&key.as_str()) {
            continue;
        }
        seen.push(key);
        let base = key.replace('!', "");
        let name = base.split('.').next().unwrap_or_default();
        // The base name is checked (`utils.py:57-58`), so `id!` and `q.x`
        // are skipped as well.
        if EXCLUDED.contains(&name) {
            continue;
        }
        let Some(&(_, column, kind)) = spec.filters.iter().find(|(n, _, _)| *n == name) else {
            return Err(ApiError::bad(format!("Invalid filter parameter: {key}")));
        };
        let values: Vec<String> = params
            .iter()
            .filter(|(k, _)| k == key)
            .map(|(_, v)| v.clone())
            .collect();
        // An empty value among several fails `v[0]` in the reference.
        if values.len() > 1 && values.iter().any(String::is_empty) {
            return Err(ApiError::bad(format!(
                "Filtering by '{key}' field not supported."
            )));
        }
        q.filters.push(ListFilter {
            column,
            kind,
            negated: key.ends_with('!'),
            values,
        });
    }
    // No order at all when the first `sort-by` is empty (`utils.py:31`,
    // `params.get`); after a non-empty first one, an empty one is 400.
    let mut sorts = params
        .iter()
        .filter(|(k, _)| k == "sort-by")
        .map(|(_, v)| v)
        .peekable();
    if sorts.peek().is_some_and(|v| v.is_empty()) {
        return Ok(q);
    }
    for value in sorts {
        let (reversed, name) = match value.strip_prefix('-') {
            Some(n) => (true, n),
            None => (false, value.as_str()),
        };
        let name = if name.starts_with("attributes.") {
            "attributes"
        } else {
            name
        };
        let Some(&(_, column, direction)) = spec.sorts.iter().find(|(n, _, _)| *n == name) else {
            return Err(ApiError::bad(format!(
                "Sorting by '{name}' field not supported."
            )));
        };
        q.sort
            .push((column, (direction == Direction::Desc) != reversed));
    }
    Ok(q)
}

/// One page of a list of `total` items.
#[derive(Debug, Clone, Copy)]
pub(crate) struct Paging {
    pub(crate) page: i64,
    pub(crate) page_size: i64,
    pub(crate) pages: i64,
}

impl Paging {
    pub(crate) fn more(self) -> bool {
        self.page < self.pages
    }
}

/// `Page.from_params(params, total)` with the conventions above.
pub(crate) fn page(
    params: &[(String, String)],
    default_page_size: i64,
    total: i64,
) -> ApiResult<Paging> {
    let integer = |key: &str| -> ApiResult<Option<i64>> {
        params
            .iter()
            .find(|(k, _)| k == key)
            .map(|(_, v)| {
                v.parse()
                    .map_err(|_| ApiError::bad(format!("{key} must be an integer")))
            })
            .transpose()
    };
    let page = integer("page")?.unwrap_or(1);
    let page_size = integer("page-size")?.unwrap_or(default_page_size);
    if page < 1 || page_size < 1 || (page - 1).checked_mul(page_size).is_none() {
        return Err(ApiError::bad("page or page-size is out of range"));
    }
    let pages = if total == 0 {
        0
    } else {
        (total - 1) / page_size + 1
    };
    if total > 0 && page > pages {
        return Err(ApiError::new(
            StatusCode::RANGE_NOT_SATISFIABLE,
            format!("page out of range: 1-{pages}"),
        ));
    }
    Ok(Paging {
        page,
        page_size,
        pages,
    })
}
