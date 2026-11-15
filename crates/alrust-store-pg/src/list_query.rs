//! The admin lists' filters and order: one port of the reference's
//! `QueryBuilder.filter_query` and `sort_by_columns`
//! (`postgres/utils.py:24-104`). The blackout (#241) and customer (#243)
//! lists carry their own copies until they move to this one.

use serde_json::{Map, Value};
use sqlx::{Postgres, QueryBuilder};

/// How a filter compares.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FilterKind {
    /// A text column. One value compares with `=` or, after `~`,
    /// `ILIKE '%value%'`; several with `= ANY` or, when any starts with `~`,
    /// one joined case-insensitive regular expression. `!` negates.
    Text,
    /// A text-array column: it overlaps the values (`&&`); `!` negates.
    /// `~` has no meaning here and is literal.
    Array,
    /// A non-text column: equality with the values cast to this type
    /// (`= CAST($n AS <type>)`, `= ANY(CAST($n AS <type>[]))`); `!` negates.
    Typed(&'static str),
    /// A `jsonb` column: `attributes.<key>=<value>` contains
    /// `{<key>: <first value>}`, the reference's own branch.
    Contains,
}

/// One filter: a fixed, quoted column and the values given for it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ListFilter {
    /// Quoted SQL identifier, from a fixed table.
    pub column: &'static str,
    pub kind: FilterKind,
    pub negated: bool,
    pub values: Vec<String>,
    /// The `jsonb` key of a [`FilterKind::Contains`] filter.
    pub key: Option<String>,
}

/// A list's filters and order.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ListQuery {
    pub filters: Vec<ListFilter>,
    /// `(quoted column, descending)`, in the order given.
    pub sort: Vec<(&'static str, bool)>,
}

impl ListQuery {
    /// ` WHERE true AND …`, every value bound.
    pub(crate) fn push_where(&self, qb: &mut QueryBuilder<'_, Postgres>) {
        qb.push(" WHERE true");
        for f in &self.filters {
            let col = f.column;
            match f.kind {
                FilterKind::Array => {
                    let not = if f.negated { "NOT " } else { "" };
                    qb.push(format!(" AND {not}({col} && "))
                        .push_bind(f.values.clone())
                        .push(")");
                }
                FilterKind::Text => push_text(qb, f),
                FilterKind::Typed(cast) => push_typed(qb, f, cast),
                FilterKind::Contains => {
                    let mut want = Map::new();
                    want.insert(
                        f.key.clone().unwrap_or_default(),
                        Value::String(f.values.first().cloned().unwrap_or_default()),
                    );
                    qb.push(format!(" AND {col} @> "))
                        .push_bind(Value::Object(want));
                }
            }
        }
    }

    /// ` ORDER BY …`, then `tiebreak` (the reference's `(select 1)` is the
    /// table's physical order; alrust adds a stable last key).
    pub(crate) fn push_order(&self, qb: &mut QueryBuilder<'_, Postgres>, tiebreak: &str) {
        let mut order: Vec<String> = self
            .sort
            .iter()
            .map(|(col, desc)| format!("{col} {}", if *desc { "DESC" } else { "ASC" }))
            .collect();
        order.push(tiebreak.to_owned());
        qb.push(format!(" ORDER BY {}", order.join(", ")));
    }
}

fn push_typed(qb: &mut QueryBuilder<'_, Postgres>, f: &ListFilter, cast: &str) {
    let col = f.column;
    if let [one] = f.values.as_slice() {
        let op = if f.negated { "!=" } else { "=" };
        qb.push(format!(" AND {col} {op} CAST("))
            .push_bind(one.clone())
            .push(format!(" AS {cast})"));
    } else {
        let not = if f.negated { "NOT " } else { "" };
        qb.push(format!(" AND {not}{col} = ANY(CAST("))
            .push_bind(f.values.clone())
            .push(format!(" AS {cast}[]))"));
    }
}

fn push_text(qb: &mut QueryBuilder<'_, Postgres>, f: &ListFilter) {
    let col = f.column;
    if let [one] = f.values.as_slice() {
        if let Some(pattern) = one.strip_prefix('~') {
            let op = if f.negated { "NOT ILIKE" } else { "ILIKE" };
            qb.push(format!(" AND {col} {op} "))
                .push_bind(format!("%{pattern}%"));
        } else {
            let op = if f.negated { "!=" } else { "=" };
            qb.push(format!(" AND {col} {op} ")).push_bind(one.clone());
        }
    } else if f.values.iter().any(|v| v.starts_with('~')) {
        let op = if f.negated { "!~*" } else { "~*" };
        let joined = f
            .values
            .iter()
            .map(|v| v.trim_start_matches('~'))
            .collect::<Vec<_>>()
            .join("|");
        qb.push(format!(" AND {col} {op} ")).push_bind(joined);
    } else {
        let not = if f.negated { "NOT " } else { "" };
        qb.push(format!(" AND {not}{col} = ANY("))
            .push_bind(f.values.clone())
            .push(")");
    }
}
