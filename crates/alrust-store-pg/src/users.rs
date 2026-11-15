//! Users (`users`, `sql/schema.sql:158-182`; reference `postgres/base.py`
//! user methods, `models/user.py`). alrust reads, edits and deletes user
//! rows. It creates none until it can hash passwords (R-NOTICE-2).

use alrust_domain::{iso8601, path_segment};
use chrono::{NaiveDateTime, Utc};
use serde_json::{Value, json};
use sqlx::postgres::PgRow;
use sqlx::{Postgres, QueryBuilder, Row};

use crate::list_query::ListQuery;
use crate::{Store, StoreError, naive};

/// A user row. `attributes` that are NULL refuse the row with the database
/// error, where the reference's `dict(None)` fails uncaught (ADR-0005,
/// decision 5); alrust's own writer never stores NULL there.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct User {
    pub id: String,
    pub name: Option<String>,
    pub login: String,
    pub email: Option<String>,
    pub status: Option<String>,
    pub roles: Option<Vec<String>>,
    pub attributes: Value,
    pub create_time: NaiveDateTime,
    pub last_login: Option<NaiveDateTime>,
    pub text: Option<String>,
    pub update_time: Option<NaiveDateTime>,
    pub email_verified: Option<bool>,
}

impl User {
    /// `login or email or 'n/a'` (`User.from_record`).
    pub fn shown_login(&self) -> &str {
        if !self.login.is_empty() {
            &self.login
        } else {
            self.email
                .as_deref()
                .filter(|e| !e.is_empty())
                .unwrap_or("n/a")
        }
    }

    /// `User.domain`: the part of the login before a `\`, else the part of
    /// the e-mail after the first `@`, else none.
    pub fn domain(&self) -> Option<&str> {
        match self.shown_login().split_once('\\') {
            Some((domain, _)) => Some(domain),
            None => self.email.as_deref()?.split('@').nth(1),
        }
    }

    /// `User.serialize`, with the constructor's defaults: an empty name and
    /// text, status `active`, `email_verified` false, and a NULL update time
    /// read as now. alrust has no `ADMIN_USERS`, so the roles are as stored.
    pub fn body(&self, base_url: &str) -> Value {
        let time = |t: NaiveDateTime| iso8601(&t.and_utc());
        json!({
            "id": self.id,
            "href": format!("{base_url}/user/{}", path_segment(&self.id)),
            "name": self.name.clone().unwrap_or_default(),
            "login": self.shown_login(),
            "email": self.email,
            "domain": self.domain(),
            "status": self.status.as_deref().filter(|s| !s.is_empty()).unwrap_or("active"),
            "roles": self.roles,
            "attributes": self.attributes,
            "createTime": time(self.create_time),
            "lastLogin": self.last_login.map(time),
            "text": self.text.clone().unwrap_or_default(),
            "updateTime": iso8601(&self.update_time.map_or_else(Utc::now, |t| t.and_utc())),
            "email_verified": self.email_verified.unwrap_or(false),
        })
    }
}

/// The members `PUT /user/{id}` writes; `None` leaves a column as it is.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct UserChanges {
    pub name: Option<String>,
    pub login: Option<String>,
    pub email: Option<String>,
    pub status: Option<String>,
    pub roles: Option<Vec<String>>,
    /// Merged into the stored object at the top level (`||`).
    pub attributes: Option<Value>,
    pub text: Option<String>,
    pub email_verified: Option<bool>,
}

fn row_to_user(row: &PgRow) -> Result<User, sqlx::Error> {
    Ok(User {
        id: row.try_get("id")?,
        name: row.try_get("name")?,
        login: row.try_get("login")?,
        email: row.try_get("email")?,
        status: row.try_get("status")?,
        roles: row.try_get("roles")?,
        attributes: row.try_get("attributes")?,
        create_time: row.try_get("create_time")?,
        last_login: row.try_get("last_login")?,
        text: row.try_get("text")?,
        update_time: row.try_get("update_time")?,
        email_verified: row.try_get("email_verified")?,
    })
}

impl Store {
    /// `GET /user/{id}`.
    ///
    /// # Errors
    ///
    /// `NotFound`, or the database error.
    pub async fn get_user(&self, id: &str) -> Result<User, StoreError> {
        let row = sqlx::query("SELECT * FROM users WHERE id = $1")
            .bind(id)
            .fetch_optional(&self.pool)
            .await?
            .ok_or(StoreError::NotFound)?;
        Ok(row_to_user(&row)?)
    }

    /// The id of the user with e-mail `email` (`get_user_by_email`).
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn user_id_by_email(&self, email: &str) -> Result<Option<String>, StoreError> {
        Ok(sqlx::query_scalar("SELECT id FROM users WHERE email = $1")
            .bind(email)
            .fetch_optional(&self.pool)
            .await?)
    }

    /// The number of users `q` selects.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn count_users(&self, q: &ListQuery) -> Result<i64, StoreError> {
        let mut qb: QueryBuilder<'_, Postgres> = QueryBuilder::new("SELECT COUNT(1) FROM users");
        q.push_where(&mut qb);
        Ok(qb.build_query_scalar().fetch_one(&self.pool).await?)
    }

    /// One page of the users `q` selects, in its order, then by id.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn list_users(
        &self,
        q: &ListQuery,
        page: i64,
        page_size: i64,
    ) -> Result<Vec<User>, StoreError> {
        let mut qb: QueryBuilder<'_, Postgres> = QueryBuilder::new("SELECT * FROM users");
        q.push_where(&mut qb);
        q.push_order(&mut qb, "id ASC");
        qb.push(" LIMIT ")
            .push_bind(page_size)
            .push(" OFFSET ")
            .push_bind((page - 1).saturating_mul(page_size));
        let rows = qb.build().fetch_all(&self.pool).await?;
        Ok(rows
            .iter()
            .map(row_to_user)
            .collect::<Result<Vec<_>, sqlx::Error>>()?)
    }

    /// `update_user`: writes the given members and the update time.
    ///
    /// # Errors
    ///
    /// `NotFound`, or the database error (for example a duplicate login).
    pub async fn update_user(&self, id: &str, c: &UserChanges) -> Result<User, StoreError> {
        let mut qb: QueryBuilder<'_, Postgres> = QueryBuilder::new("UPDATE users SET ");
        for (column, value) in [
            ("name", &c.name),
            ("login", &c.login),
            ("email", &c.email),
            ("status", &c.status),
            ("text", &c.text),
        ] {
            if let Some(v) = value {
                qb.push(format!("{column} = "))
                    .push_bind(v.clone())
                    .push(", ");
            }
        }
        if let Some(roles) = &c.roles {
            qb.push("roles = ").push_bind(roles.clone()).push(", ");
        }
        if let Some(attributes) = &c.attributes {
            qb.push("attributes = attributes || ")
                .push_bind(attributes.clone())
                .push(", ");
        }
        if let Some(verified) = c.email_verified {
            qb.push("email_verified = ").push_bind(verified).push(", ");
        }
        qb.push("update_time = ")
            .push_bind(naive(Utc::now()))
            .push(" WHERE id = ")
            .push_bind(id.to_owned())
            .push(" RETURNING *");
        let row = qb
            .build()
            .fetch_optional(&self.pool)
            .await?
            .ok_or(StoreError::NotFound)?;
        Ok(row_to_user(&row)?)
    }

    /// `update_user_attributes`' write: the merged object replaces the
    /// stored one.
    ///
    /// # Errors
    ///
    /// `NotFound`, or the database error.
    pub async fn set_user_attributes(
        &self,
        id: &str,
        attributes: &Value,
    ) -> Result<(), StoreError> {
        let row = sqlx::query(
            "UPDATE users SET attributes = $2, update_time = $3 WHERE id = $1 RETURNING id",
        )
        .bind(id)
        .bind(attributes)
        .bind(naive(Utc::now()))
        .fetch_optional(&self.pool)
        .await?;
        row.map(|_| ()).ok_or(StoreError::NotFound)
    }

    /// `DELETE /user/{id}`. Groups keep the id among their members, as in
    /// the reference.
    ///
    /// # Errors
    ///
    /// `NotFound`, or the database error.
    pub async fn delete_user(&self, id: &str) -> Result<(), StoreError> {
        let deleted = sqlx::query("DELETE FROM users WHERE id = $1 RETURNING id")
            .bind(id)
            .fetch_optional(&self.pool)
            .await?;
        deleted.map(|_| ()).ok_or(StoreError::NotFound)
    }
}
