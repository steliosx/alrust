//! Customer mappings: `POST /customer`, `GET`/`PUT`/`DELETE
//! /customer/{id}` and `GET /customers` (reference `views/customers.py`,
//! `models/customer.py`, `postgres/base.py` `create_customer` to
//! `delete_customer`, `postgres/utils.py:442-462`). A mapping says which
//! customer a login, group or e-mail domain (`match`) belongs to. alrust
//! reads no mapping yet (it has no login), so these rows are stored and
//! served only.

use serde_json::{Value, json};
use sqlx::postgres::PgRow;
use sqlx::{Postgres, QueryBuilder, Row};

use crate::{Store, StoreError};

/// One row of the `customers` table (`sql/schema.sql:100-104`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CustomerMapping {
    pub id: String,
    /// The `match` column.
    pub pattern: String,
    pub customer: Option<String>,
}

impl CustomerMapping {
    /// `Customer.serialize`.
    pub fn body(&self, base_url: &str) -> Value {
        json!({
            "id": self.id,
            "href": format!("{base_url}/customer/{}", alrust_domain::path_segment(&self.id)),
            "match": self.pattern,
            "customer": self.customer,
        })
    }
}

fn row_to_mapping(row: &PgRow) -> Result<CustomerMapping, sqlx::Error> {
    Ok(CustomerMapping {
        id: row.try_get("id")?,
        pattern: row.try_get("match")?,
        customer: row.try_get("customer")?,
    })
}

/// Whether `PUT` writes the `customer` column, and the value (which may
/// be NULL).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub enum SetCustomer {
    #[default]
    Keep,
    To(Option<String>),
}

/// One text filter, as `filter_query` writes it for `match` or `customer`.
#[derive(Debug, Clone)]
pub struct MappingFilter {
    /// `match` or `customer`.
    pub column: &'static str,
    pub negated: bool,
    pub values: Vec<String>,
}

/// `GET /customers`' filters and order.
#[derive(Debug, Clone, Default)]
pub struct MappingQuery {
    pub filters: Vec<MappingFilter>,
    /// `(column, descending)`; empty means the default order.
    pub sort: Vec<(&'static str, bool)>,
}

/// The same text-filter rules as the blackout list (#241): one value
/// compares with `=`/`!=` or, after `~`, `ILIKE '%v%'`; several with
/// `= ANY`/`NOT ... = ANY` or, when any starts with `~`, one joined
/// case-insensitive regular expression.
fn push_where(qb: &mut QueryBuilder<'_, Postgres>, q: &MappingQuery) {
    qb.push(" WHERE true");
    for f in &q.filters {
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
}

impl Store {
    /// `POST /customer`.
    ///
    /// # Errors
    ///
    /// The database error (for example a duplicate id).
    pub async fn create_mapping(&self, m: &CustomerMapping) -> Result<CustomerMapping, StoreError> {
        let row = sqlx::query(
            "INSERT INTO customers (id, match, customer) VALUES ($1, $2, $3) RETURNING *",
        )
        .bind(&m.id)
        .bind(&m.pattern)
        .bind(&m.customer)
        .fetch_one(&self.pool)
        .await?;
        Ok(row_to_mapping(&row)?)
    }

    /// One mapping by exact id.
    ///
    /// # Errors
    ///
    /// `NotFound`, otherwise the database error.
    pub async fn get_mapping(&self, id: &str) -> Result<CustomerMapping, StoreError> {
        let row = sqlx::query("SELECT * FROM customers WHERE id = $1")
            .bind(id)
            .fetch_optional(&self.pool)
            .await?
            .ok_or(StoreError::NotFound)?;
        Ok(row_to_mapping(&row)?)
    }

    /// `GET /customers` total (before the view's visibility filter).
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn count_mappings(&self, q: &MappingQuery) -> Result<i64, StoreError> {
        let mut qb: QueryBuilder<'_, Postgres> =
            QueryBuilder::new("SELECT COUNT(1) AS n FROM customers");
        push_where(&mut qb, q);
        let row = qb.build().fetch_one(&self.pool).await?;
        Ok(row.try_get("n")?)
    }

    /// `GET /customers` page. Without `sort-by` the reference has no
    /// order; alrust orders by id.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn list_mappings(
        &self,
        q: &MappingQuery,
        page: i64,
        page_size: i64,
    ) -> Result<Vec<CustomerMapping>, StoreError> {
        let mut qb: QueryBuilder<'_, Postgres> = QueryBuilder::new("SELECT * FROM customers");
        push_where(&mut qb, q);
        let mut order: Vec<String> = q
            .sort
            .iter()
            .map(|(col, desc)| format!("{col} {}", if *desc { "DESC" } else { "ASC" }))
            .collect();
        order.push("id ASC".into());
        qb.push(format!(" ORDER BY {} LIMIT ", order.join(", ")))
            .push_bind(page_size)
            .push(" OFFSET ")
            .push_bind((page - 1).saturating_mul(page_size));
        let rows = qb.build().fetch_all(&self.pool).await?;
        Ok(rows
            .iter()
            .map(row_to_mapping)
            .collect::<Result<Vec<_>, sqlx::Error>>()?)
    }

    /// `PUT /customer/{id}` (`update_customer`): writes `match` and
    /// `customer` when given; with neither, the reference's `SET id = id`
    /// changes nothing and returns the row.
    ///
    /// # Errors
    ///
    /// `NotFound`, otherwise the database error.
    pub async fn update_mapping(
        &self,
        id: &str,
        pattern: Option<String>,
        customer: SetCustomer,
    ) -> Result<CustomerMapping, StoreError> {
        let mut qb: QueryBuilder<'_, Postgres> = QueryBuilder::new("UPDATE customers SET ");
        if let Some(p) = pattern {
            qb.push("match = ").push_bind(p).push(", ");
        }
        if let SetCustomer::To(c) = customer {
            qb.push("customer = ").push_bind(c).push(", ");
        }
        qb.push("id = id WHERE id = ")
            .push_bind(id.to_owned())
            .push(" RETURNING *");
        let row = qb
            .build()
            .fetch_optional(&self.pool)
            .await?
            .ok_or(StoreError::NotFound)?;
        Ok(row_to_mapping(&row)?)
    }

    /// `DELETE /customer/{id}`.
    ///
    /// # Errors
    ///
    /// `NotFound`, otherwise the database error.
    pub async fn delete_mapping(&self, id: &str) -> Result<(), StoreError> {
        sqlx::query("DELETE FROM customers WHERE id = $1 RETURNING id")
            .bind(id)
            .fetch_optional(&self.pool)
            .await?
            .ok_or(StoreError::NotFound)?;
        Ok(())
    }
}
