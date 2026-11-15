//! Permission rows (`perms`, `sql/schema.sql:151-155`; reference
//! `postgres/base.py:1223-1293`): a role name (`match`) and the scopes it
//! grants.
//!
//! A `scopes` value that is NULL refuses the row with the database error
//! (ADR-0005 decision 5): the reference iterates it on every read
//! (`models/permission.py`, `from_record`; `base.py:1292`) and fails
//! uncaught. Its own writer never stores NULL, so only an adopted database
//! can hold one.

use alrust_domain::path_segment;
use alrust_domain::roles::{
    ADMIN_ROLE, ADMIN_SCOPES, GUEST_DEFAULT_SCOPES, GUEST_ROLE, USER_DEFAULT_SCOPES, USER_ROLE,
};
use serde_json::{Value, json};
use sqlx::postgres::PgRow;
use sqlx::{Postgres, QueryBuilder, Row};

use crate::list_query::ListQuery;
use crate::{Store, StoreError};

/// A permission row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Perm {
    pub id: String,
    /// The `match` column: the role name.
    pub role: String,
    pub scopes: Vec<String>,
}

impl Perm {
    /// `Permission.serialize` (`models/permission.py`).
    pub fn body(&self, base_url: &str) -> Value {
        json!({
            "id": self.id,
            "href": format!("{base_url}/perm/{}", path_segment(&self.id)),
            "match": self.role,
            "scopes": self.scopes,
        })
    }
}

fn row_to_perm(row: &PgRow) -> Result<Perm, sqlx::Error> {
    Ok(Perm {
        id: row.try_get("id")?,
        role: row.try_get("match")?,
        scopes: row.try_get("scopes")?,
    })
}

impl Store {
    /// `POST /perm` (`create_perm`). A missing `role` reaches the column's
    /// `NOT NULL`, as in the reference.
    ///
    /// # Errors
    ///
    /// The database error (a missing or duplicate role, a duplicate id).
    pub async fn create_perm(
        &self,
        id: &str,
        role: Option<&str>,
        scopes: &[String],
    ) -> Result<Perm, StoreError> {
        let row =
            sqlx::query("INSERT INTO perms (id, match, scopes) VALUES ($1, $2, $3) RETURNING *")
                .bind(id)
                .bind(role)
                .bind(scopes)
                .fetch_one(&self.pool)
                .await?;
        Ok(row_to_perm(&row)?)
    }

    /// `GET /perm/{id}`.
    ///
    /// # Errors
    ///
    /// `NotFound`, or the database error.
    pub async fn get_perm(&self, id: &str) -> Result<Perm, StoreError> {
        let row = sqlx::query("SELECT * FROM perms WHERE id = $1")
            .bind(id)
            .fetch_optional(&self.pool)
            .await?
            .ok_or(StoreError::NotFound)?;
        Ok(row_to_perm(&row)?)
    }

    /// The number of rows `q` selects.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn count_perms(&self, q: &ListQuery) -> Result<i64, StoreError> {
        let mut qb: QueryBuilder<'_, Postgres> = QueryBuilder::new("SELECT COUNT(1) FROM perms");
        q.push_where(&mut qb);
        Ok(qb.build_query_scalar().fetch_one(&self.pool).await?)
    }

    /// One page of the rows `q` selects, in its order, then by id.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn list_perms(
        &self,
        q: &ListQuery,
        page: i64,
        page_size: i64,
    ) -> Result<Vec<Perm>, StoreError> {
        let mut qb: QueryBuilder<'_, Postgres> = QueryBuilder::new("SELECT * FROM perms");
        q.push_where(&mut qb);
        q.push_order(&mut qb, "id ASC");
        qb.push(" LIMIT ")
            .push_bind(page_size)
            .push(" OFFSET ")
            .push_bind((page - 1).saturating_mul(page_size));
        let rows = qb.build().fetch_all(&self.pool).await?;
        Ok(rows
            .iter()
            .map(row_to_perm)
            .collect::<Result<Vec<_>, sqlx::Error>>()?)
    }

    /// `PUT /perm/{id}` (`update_perm`): writes `role` and `scopes` when
    /// given.
    ///
    /// # Errors
    ///
    /// `NotFound`, or the database error (a duplicate role).
    pub async fn update_perm(
        &self,
        id: &str,
        role: Option<&str>,
        scopes: Option<&[String]>,
    ) -> Result<Perm, StoreError> {
        let mut qb: QueryBuilder<'_, Postgres> = QueryBuilder::new("UPDATE perms SET ");
        if let Some(r) = role {
            qb.push("match = ").push_bind(r.to_owned()).push(", ");
        }
        if let Some(s) = scopes {
            qb.push("scopes = ").push_bind(s.to_vec()).push(", ");
        }
        qb.push("id = id WHERE id = ")
            .push_bind(id.to_owned())
            .push(" RETURNING *");
        let row = qb
            .build()
            .fetch_optional(&self.pool)
            .await?
            .ok_or(StoreError::NotFound)?;
        Ok(row_to_perm(&row)?)
    }

    /// `DELETE /perm/{id}`.
    ///
    /// # Errors
    ///
    /// `NotFound`, or the database error.
    pub async fn delete_perm(&self, id: &str) -> Result<(), StoreError> {
        let deleted = sqlx::query("DELETE FROM perms WHERE id = $1 RETURNING id")
            .bind(id)
            .fetch_optional(&self.pool)
            .await?;
        deleted.map(|_| ()).ok_or(StoreError::NotFound)
    }

    /// `Permission.lookup` (`get_scopes_by_match`, `base.py:1277-1293`): the
    /// scopes `roles` grant, sorted and without repeats. An admin role
    /// grants `ADMIN_SCOPES` at once; the user and guest roles add their
    /// defaults; every role adds its permission row's scopes. alrust has no
    /// `ADMIN_USERS`, so `login` grants nothing by itself.
    ///
    /// # Errors
    ///
    /// The database error, also for a row whose scopes are NULL.
    pub async fn scopes_by_match(
        &self,
        _login: &str,
        roles: &[String],
    ) -> Result<Vec<String>, StoreError> {
        let mut scopes: Vec<String> = Vec::new();
        for role in roles {
            if role == ADMIN_ROLE {
                return Ok(ADMIN_SCOPES.iter().map(|s| (*s).to_owned()).collect());
            }
            if role == USER_ROLE {
                scopes.extend(USER_DEFAULT_SCOPES.iter().map(|s| (*s).to_owned()));
            }
            if role == GUEST_ROLE {
                scopes.extend(GUEST_DEFAULT_SCOPES.iter().map(|s| (*s).to_owned()));
            }
            let row = sqlx::query("SELECT scopes FROM perms WHERE match = $1")
                .bind(role)
                .fetch_optional(&self.pool)
                .await?;
            if let Some(row) = row {
                scopes.extend(row.try_get::<Vec<String>, _>("scopes")?);
            }
        }
        scopes.sort();
        scopes.dedup();
        Ok(scopes)
    }
}
