//! The bulk alert routes' store side (`spec/security-fixes.md` SF-BULK):
//! the ids a selection picks, and the single-alert tag, untag and attribute
//! edits applied to every selected alert in one statement (reference
//! `postgres/base.py:361-388`).

use sqlx::{Postgres, QueryBuilder};

use crate::{AlertEdit, AlertQuery, Store, StoreError};

impl Store {
    /// The ids of the alerts `q` selects, in the order `GET /alerts` lists
    /// them (newest `lastReceiveTime` first, then id), at most `limit`.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn alert_ids(&self, q: &AlertQuery, limit: i64) -> Result<Vec<String>, StoreError> {
        let mut qb: QueryBuilder<'_, Postgres> = QueryBuilder::new("SELECT id FROM alerts");
        Self::push_filters(&mut qb, q);
        qb.push(" ORDER BY last_receive_time DESC, id LIMIT ")
            .push_bind(limit);
        Ok(qb.build_query_scalar().fetch_all(&self.pool).await?)
    }

    /// `tag_alerts`, `untag_alerts` and `update_attributes_by_query`: the
    /// edit `PUT /alert/{id}/tag`, `/untag` or `/attributes` makes, on every
    /// alert `q` selects, in one statement. As on the single route, added
    /// tags keep their first-seen order, untagging every tag leaves an empty
    /// list, an attribute set to null is removed, and nothing else changes
    /// (no history, no update time). Returns the edited ids, sorted.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn edit_alerts(
        &self,
        q: &AlertQuery,
        edit: &AlertEdit,
    ) -> Result<Vec<String>, StoreError> {
        let mut qb: QueryBuilder<'_, Postgres> = QueryBuilder::new("UPDATE alerts SET ");
        match edit {
            AlertEdit::Tag(tags) => {
                qb.push("tags = ARRAY(SELECT t FROM unnest(COALESCE(tags, '{}') || ")
                    .push_bind(tags.clone())
                    .push("::text[]) WITH ORDINALITY AS u(t, n) GROUP BY t ORDER BY min(n))");
            }
            AlertEdit::Untag(tags) => {
                qb.push(
                    "tags = COALESCE((SELECT array_agg(t ORDER BY n) FROM unnest(tags) \
                     WITH ORDINALITY AS u(t, n) WHERE NOT t = ANY(",
                )
                .push_bind(tags.clone())
                .push(")), '{}')");
            }
            AlertEdit::Attributes(attrs) => {
                qb.push(
                    "attributes = (SELECT COALESCE(jsonb_object_agg(k, v), '{}'::jsonb) \
                     FROM jsonb_each(COALESCE(attributes, '{}'::jsonb) || ",
                )
                .push_bind(serde_json::Value::Object(attrs.clone()))
                .push(") AS e(k, v) WHERE jsonb_typeof(v) <> 'null')");
            }
        }
        Self::push_filters(&mut qb, q);
        qb.push(" RETURNING id");
        let mut ids: Vec<String> = qb.build_query_scalar().fetch_all(&self.pool).await?;
        ids.sort();
        Ok(ids)
    }
}
