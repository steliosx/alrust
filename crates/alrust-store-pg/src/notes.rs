//! Alert notes: `PUT /alert/{id}/note`, `GET /alert/{id}/notes`, and `PUT`
//! and `DELETE /alert/{id}/note/{note_id}` (reference `views/alerts.py:596-723`,
//! `models/note.py`, `models/alert.py:572-606`, `postgres/base.py:1371-1436`),
//! with `spec/security-fixes.md` SF-NOTES: a note is changed or deleted only
//! through its own alert.
//!
//! As in the reference:
//! - a new note copies the alert's resource, event, environment, severity
//!   and status into its `attributes`, and adds a `note` history entry
//!   whose id is the note's id; a deleted note adds a `dismiss` entry
//!   (`note dismissed`);
//! - the list selects `alert ~* <the alert's id>`, an unanchored,
//!   case-insensitive regular expression (customer-mapping C21, kept as the
//!   reference contract), at most 100 notes;
//! - an update writes the given text, merges the given attributes with
//!   `attributes || …`, sets the user and the update time, and adds no
//!   history entry.
//!
//! The history entries are written to the matched alert's row only, as for
//! every other alrust write (the reference also updates alerts whose id
//! starts with that id; `docs/alpha.md`, id lookup). The list adds an order
//! (creation time, then id); the reference has none.

use alrust_domain::{HistoryEntry, change_type, truncate_ms};
use chrono::{DateTime, NaiveDateTime, Utc};
use serde_json::{Map, Value, json};
use sqlx::postgres::PgRow;
use sqlx::{Postgres, QueryBuilder, Row};

use crate::{Col, Store, StoreError, naive, object, opt_time, user_of, utc};

/// `NoteType.alert` (`models/enums.py`).
pub const ALERT_NOTE_TYPE: &str = "alert";

/// The reference's fixed page of notes (`Alert.get_alert_notes`).
pub const NOTES_LIMIT: i64 = 100;

/// A stored note (the `notes` table, `sql/schema.sql:65-75`).
#[derive(Debug, Clone, PartialEq)]
pub struct Note {
    pub id: String,
    pub text: Option<String>,
    pub user: Option<String>,
    pub attributes: Map<String, Value>,
    /// The `type` column.
    pub kind: String,
    pub create_time: DateTime<Utc>,
    pub update_time: Option<DateTime<Utc>>,
    pub alert: Option<String>,
    pub customer: Option<String>,
}

impl Note {
    /// `Note.serialize` (`models/note.py:45-65`).
    pub fn body(&self, base_url: &str) -> Value {
        let segment = alrust_domain::path_segment;
        let mut body = json!({
            "id": self.id,
            "href": format!("{base_url}/note/{}", segment(&self.id)),
            "text": self.text,
            "user": self.user,
            "attributes": self.attributes,
            "type": self.kind,
            "createTime": alrust_domain::iso8601(&self.create_time),
            "updateTime": self.update_time.as_ref().map(alrust_domain::iso8601),
            "_links": {},
            "customer": self.customer,
        });
        if let Some(alert) = &self.alert {
            body["related"] = json!({"alert": alert});
            body["_links"] = json!({"alert": format!("{base_url}/alert/{}", segment(alert))});
        }
        body
    }
}

fn row_to_note(row: &PgRow) -> Result<Note, sqlx::Error> {
    Ok(Note {
        id: row.try_get("id")?,
        text: row.try_get("text")?,
        user: row.try_get("user")?,
        attributes: object(row, "attributes")?,
        kind: row.try_get("type")?,
        create_time: utc(row.try_get::<NaiveDateTime, _>("create_time")?),
        update_time: opt_time(row, "update_time")?,
        alert: row.try_get("alert")?,
        customer: row.try_get("customer")?,
    })
}

/// The changes a note update may carry (`update_note`).
#[derive(Debug, Clone, Default)]
pub struct NoteUpdate {
    pub text: Option<String>,
    pub attributes: Option<Map<String, Value>>,
}

impl Store {
    /// Add a note to the alert `id` (the usual lookup and customer filter)
    /// with the given new note id.
    ///
    /// # Errors
    ///
    /// `NotFound` when no visible alert matches, otherwise the database
    /// error.
    pub async fn add_note(
        &self,
        id: &str,
        customers: Option<&[String]>,
        note_id: &str,
        text: &str,
        login: &str,
    ) -> Result<(String, Note), StoreError> {
        let mut tx = self.pool.begin().await?;
        let mut a = Self::lock_alert(&mut tx, id, customers).await?;
        let now = truncate_ms(Utc::now());
        let attributes = json!({
            "resource": a.resource,
            "event": a.event,
            "environment": a.environment,
            "severity": a.severity,
            "status": a.status,
        });
        let row = sqlx::query(
            r#"INSERT INTO notes (id, text, "user", attributes, type, create_time, update_time, alert, customer)
               VALUES ($1, $2, $3, $4, $5, $6, NULL, $7, $8) RETURNING *"#,
        )
        .bind(note_id)
        .bind(text)
        .bind(user_of(login))
        .bind(attributes)
        .bind(ALERT_NOTE_TYPE)
        .bind(naive(now))
        .bind(&a.id)
        .bind(&a.customer)
        .fetch_one(&mut *tx)
        .await?;
        let note = row_to_note(&row)?;
        let entry = HistoryEntry {
            id: note_id.to_owned(),
            event: a.event.clone(),
            severity: Some(a.severity.clone()),
            status: Some(a.status.clone()),
            value: a.value.clone(),
            text: Some(text.to_owned()),
            change_type: change_type::NOTE.to_owned(),
            update_time: now,
            user: user_of(login),
            timeout: None,
        };
        self.push_history(&mut a.history, entry);
        Self::update_alert(&mut tx, &a, &[Col::History]).await?;
        tx.commit().await?;
        Ok((a.id, note))
    }

    /// The notes of the alert `id` (the usual lookup and customer filter),
    /// selected as `alert ~* <its id>`.
    ///
    /// # Errors
    ///
    /// `NotFound` when no visible alert matches, otherwise the database
    /// error (also for an alert id that is not a valid regular expression,
    /// as in the reference).
    pub async fn alert_notes(
        &self,
        id: &str,
        customers: Option<&[String]>,
    ) -> Result<Vec<Note>, StoreError> {
        let a = self.get_alert(id, customers).await?;
        let rows =
            sqlx::query("SELECT * FROM notes WHERE alert ~* $1 ORDER BY create_time, id LIMIT $2")
                .bind(&a.id)
                .bind(NOTES_LIMIT)
                .fetch_all(&self.pool)
                .await?;
        Ok(rows
            .iter()
            .map(row_to_note)
            .collect::<Result<Vec<_>, sqlx::Error>>()?)
    }

    /// Update note `note_id` of the alert `id`. SF-NOTES: a note of another
    /// alert is `NotFound`, like a missing one, and nothing changes.
    ///
    /// # Errors
    ///
    /// `NotFound` for a missing alert or note, otherwise the database error.
    pub async fn update_note(
        &self,
        id: &str,
        customers: Option<&[String]>,
        note_id: &str,
        update: NoteUpdate,
        login: &str,
    ) -> Result<Note, StoreError> {
        let a = self.get_alert(id, customers).await?;
        let mut qb: QueryBuilder<'_, Postgres> = QueryBuilder::new("UPDATE notes SET ");
        if let Some(text) = update.text {
            qb.push("text = ").push_bind(text).push(", ");
        }
        if let Some(attributes) = update.attributes {
            qb.push("attributes = attributes || ")
                .push_bind(Value::Object(attributes))
                .push(", ");
        }
        qb.push("\"user\" = COALESCE(")
            .push_bind(user_of(login))
            .push(", \"user\"), update_time = NOW() AT TIME ZONE 'utc' WHERE id = ")
            .push_bind(note_id.to_owned())
            .push(" AND alert = ")
            .push_bind(a.id)
            .push(" RETURNING *");
        let row = qb
            .build()
            .fetch_optional(&self.pool)
            .await?
            .ok_or(StoreError::NotFound)?;
        Ok(row_to_note(&row)?)
    }

    /// Delete note `note_id` of the alert `id`: `Ok(false)` when the alert
    /// has no such note (SF-NOTES: a note of another alert counts as
    /// missing), with nothing changed.
    ///
    /// # Errors
    ///
    /// `NotFound` for a missing alert, otherwise the database error.
    pub async fn delete_note(
        &self,
        id: &str,
        customers: Option<&[String]>,
        note_id: &str,
        login: &str,
    ) -> Result<bool, StoreError> {
        let mut tx = self.pool.begin().await?;
        let mut a = Self::lock_alert(&mut tx, id, customers).await?;
        let deleted = sqlx::query("DELETE FROM notes WHERE id = $1 AND alert = $2 RETURNING id")
            .bind(note_id)
            .bind(&a.id)
            .fetch_optional(&mut *tx)
            .await?;
        if deleted.is_none() {
            tx.rollback().await?;
            return Ok(false);
        }
        let entry = HistoryEntry {
            id: note_id.to_owned(),
            event: a.event.clone(),
            severity: Some(a.severity.clone()),
            status: Some(a.status.clone()),
            value: a.value.clone(),
            text: Some("note dismissed".to_owned()),
            change_type: change_type::DISMISS.to_owned(),
            update_time: truncate_ms(Utc::now()),
            user: user_of(login),
            timeout: None,
        };
        self.push_history(&mut a.history, entry);
        Self::update_alert(&mut tx, &a, &[Col::History]).await?;
        tx.commit().await?;
        Ok(true)
    }
}
