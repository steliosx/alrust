//! User groups (`groups`, `sql/schema.sql:184-192`; reference
//! `postgres/base.py:1132-1220`): a name, a text and the ids of the member
//! users. A group's name also acts as a role at a password login
//! (`auth/basic.py:52-53`), which alrust does not have yet.

use alrust_domain::path_segment;
use chrono::Utc;
use serde_json::{Value, json};
use sqlx::postgres::PgRow;
use sqlx::{Postgres, QueryBuilder, Row};

use crate::list_query::ListQuery;
use crate::{Store, StoreError, naive};

/// The member count, as the reference computes it.
pub const COUNT: &str = "COALESCE(CARDINALITY(\"users\"), 0)";

/// A group row with its member count.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Group {
    pub id: String,
    pub name: String,
    /// NULL reads as empty, as the reference's constructor does.
    pub text: String,
    pub count: i64,
}

impl Group {
    /// `Group.serialize` (`models/group.py`).
    pub fn body(&self, base_url: &str) -> Value {
        json!({
            "id": self.id,
            "href": format!("{base_url}/group/{}", path_segment(&self.id)),
            "name": self.name,
            "text": self.text,
            "count": self.count,
        })
    }
}

/// A member, as `GET /group/{id}/users` lists it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GroupUser {
    pub id: String,
    /// `login`, else `email`, with Python's `or` (`GroupUser.from_record`):
    /// an empty login falls back too.
    pub login: Option<String>,
    pub name: Option<String>,
    pub status: Option<String>,
}

impl GroupUser {
    /// `GroupUser.serialize`.
    pub fn body(&self, base_url: &str) -> Value {
        json!({
            "id": self.id,
            "href": format!("{base_url}/user/{}", path_segment(&self.id)),
            "login": self.login,
            "name": self.name,
            "status": self.status,
        })
    }
}

fn row_to_group(row: &PgRow) -> Result<Group, sqlx::Error> {
    Ok(Group {
        id: row.try_get("id")?,
        name: row.try_get("name")?,
        text: row
            .try_get::<Option<String>, _>("text")?
            .unwrap_or_default(),
        count: i64::from(row.try_get::<i32, _>("count")?),
    })
}

impl Store {
    /// `POST /group`.
    ///
    /// # Errors
    ///
    /// The database error (a duplicate name or id).
    pub async fn create_group(
        &self,
        id: &str,
        name: &str,
        text: &str,
    ) -> Result<Group, StoreError> {
        let row = sqlx::query(
            "INSERT INTO groups (id, name, text) VALUES ($1, $2, $3) RETURNING *, 0 AS count",
        )
        .bind(id)
        .bind(name)
        .bind(text)
        .fetch_one(&self.pool)
        .await?;
        Ok(row_to_group(&row)?)
    }

    /// `GET /group/{id}`.
    ///
    /// # Errors
    ///
    /// `NotFound`, or the database error.
    pub async fn get_group(&self, id: &str) -> Result<Group, StoreError> {
        let row = sqlx::query(&format!(
            "SELECT *, {COUNT} AS count FROM groups WHERE id = $1"
        ))
        .bind(id)
        .fetch_optional(&self.pool)
        .await?
        .ok_or(StoreError::NotFound)?;
        Ok(row_to_group(&row)?)
    }

    /// The number of groups `q` selects.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn count_groups(&self, q: &ListQuery) -> Result<i64, StoreError> {
        let mut qb: QueryBuilder<'_, Postgres> = QueryBuilder::new("SELECT COUNT(1) FROM groups");
        q.push_where(&mut qb);
        Ok(qb.build_query_scalar().fetch_one(&self.pool).await?)
    }

    /// One page of the groups `q` selects, in its order, then by id.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn list_groups(
        &self,
        q: &ListQuery,
        page: i64,
        page_size: i64,
    ) -> Result<Vec<Group>, StoreError> {
        let mut qb: QueryBuilder<'_, Postgres> =
            QueryBuilder::new(format!("SELECT *, {COUNT} AS count FROM groups"));
        q.push_where(&mut qb);
        q.push_order(&mut qb, "id ASC");
        qb.push(" LIMIT ")
            .push_bind(page_size)
            .push(" OFFSET ")
            .push_bind((page - 1).saturating_mul(page_size));
        let rows = qb.build().fetch_all(&self.pool).await?;
        Ok(rows
            .iter()
            .map(row_to_group)
            .collect::<Result<Vec<_>, sqlx::Error>>()?)
    }

    /// `PUT /group/{id}` (`update_group`): writes `name` and `text` when
    /// given, and the update time.
    ///
    /// # Errors
    ///
    /// `NotFound`, or the database error (a duplicate name).
    pub async fn update_group(
        &self,
        id: &str,
        name: Option<&str>,
        text: Option<&str>,
    ) -> Result<Group, StoreError> {
        let mut qb: QueryBuilder<'_, Postgres> = QueryBuilder::new("UPDATE groups SET ");
        if let Some(n) = name {
            qb.push("name = ").push_bind(n.to_owned()).push(", ");
        }
        if let Some(t) = text {
            qb.push("text = ").push_bind(t.to_owned()).push(", ");
        }
        qb.push("update_time = ")
            .push_bind(naive(Utc::now()))
            .push(" WHERE id = ")
            .push_bind(id.to_owned())
            .push(format!(" RETURNING *, {COUNT} AS count"));
        let row = qb
            .build()
            .fetch_optional(&self.pool)
            .await?
            .ok_or(StoreError::NotFound)?;
        Ok(row_to_group(&row)?)
    }

    /// The members of group `id`, in the order the group lists them.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn group_users(&self, id: &str) -> Result<Vec<GroupUser>, StoreError> {
        let rows = sqlx::query(
            "SELECT u.id, COALESCE(NULLIF(u.login, ''), u.email) AS login, u.name, u.status
               FROM groups g, UNNEST(g.users) WITH ORDINALITY AS m(uid, n)
               JOIN users u ON u.id = m.uid
              WHERE g.id = $1
              ORDER BY m.n",
        )
        .bind(id)
        .fetch_all(&self.pool)
        .await?;
        Ok(rows
            .iter()
            .map(|r| {
                Ok(GroupUser {
                    id: r.try_get("id")?,
                    login: r.try_get("login")?,
                    name: r.try_get("name")?,
                    status: r.try_get("status")?,
                })
            })
            .collect::<Result<Vec<_>, sqlx::Error>>()?)
    }

    /// Whether a user row `id` exists.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn user_exists(&self, id: &str) -> Result<bool, StoreError> {
        Ok(sqlx::query("SELECT 1 FROM users WHERE id = $1")
            .bind(id)
            .fetch_optional(&self.pool)
            .await?
            .is_some())
    }

    /// `add_user_to_group`: the user id joins the group's members once.
    ///
    /// # Errors
    ///
    /// `NotFound`, or the database error.
    pub async fn add_user_to_group(&self, group: &str, user: &str) -> Result<(), StoreError> {
        let row = sqlx::query(
            "UPDATE groups SET users = ARRAY(SELECT DISTINCT UNNEST(users || ARRAY[$2]))
              WHERE id = $1 RETURNING id",
        )
        .bind(group)
        .bind(user)
        .fetch_optional(&self.pool)
        .await?;
        row.map(|_| ()).ok_or(StoreError::NotFound)
    }

    /// `remove_user_from_group`.
    ///
    /// # Errors
    ///
    /// `NotFound`, or the database error.
    pub async fn remove_user_from_group(&self, group: &str, user: &str) -> Result<(), StoreError> {
        let row = sqlx::query(
            "UPDATE groups SET users = (SELECT array_agg(u) FROM unnest(users) AS u WHERE NOT u = $2)
              WHERE id = $1 RETURNING id",
        )
        .bind(group)
        .bind(user)
        .fetch_optional(&self.pool)
        .await?;
        row.map(|_| ()).ok_or(StoreError::NotFound)
    }

    /// The groups that list user `user` among their members
    /// (`get_groups_by_user`), by id.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn groups_of_user(&self, user: &str) -> Result<Vec<Group>, StoreError> {
        let rows = sqlx::query(&format!(
            "SELECT *, {COUNT} AS count FROM groups WHERE $1 = ANY(users) ORDER BY id"
        ))
        .bind(user)
        .fetch_all(&self.pool)
        .await?;
        Ok(rows
            .iter()
            .map(row_to_group)
            .collect::<Result<Vec<_>, sqlx::Error>>()?)
    }

    /// `DELETE /group/{id}`.
    ///
    /// # Errors
    ///
    /// `NotFound`, or the database error.
    pub async fn delete_group(&self, id: &str) -> Result<(), StoreError> {
        let deleted = sqlx::query("DELETE FROM groups WHERE id = $1 RETURNING id")
            .bind(id)
            .fetch_optional(&self.pool)
            .await?;
        deleted.map(|_| ()).ok_or(StoreError::NotFound)
    }
}
