//! Alert notes: `PUT /alert/{id}/note`, `GET /alert/{id}/notes`, and `PUT`
//! and `DELETE /alert/{id}/note/{note_id}` (reference `views/alerts.py:596-723`,
//! `models/note.py`, `models/alert.py:572-606`, `postgres/base.py:1371-1436`),
//! with `spec/security-fixes.md` SF-NOTES: a note is changed or deleted only
//! through its own alert.
//!
//! "Its own alert" means the stored alert that received the note, not only
//! an alert with the same id. An alert's id can be set by the client, and a
//! deleted alert leaves its notes behind (as in the reference), so a later
//! alert can have the id of an earlier one. A note is changed or deleted
//! only when it is bound to the alert ([`owns_note`], DECISIONS Y77).
//!
//! The binding is a row of `alrust_note_bindings` (migration `0003`): the
//! note's id, the id of the alert that received it, and the `xmin` of the
//! note's stored row. [`Store::add_note`] writes it in the transaction that
//! stores the note.
//! - *The alert's lifetime.* Triggers on `alerts` remove an alert's
//!   bindings when any program deletes the alert, stores an alert with that
//!   id, changes an alert's id or truncates the table. So a binding lasts as
//!   long as its alert, whatever the history keeps, and a later alert with
//!   the same id owns none of the earlier notes.
//! - *The note's lifetime.* A binding counts only while the note's row
//!   still has the bound `xmin`, which every insert and update by any
//!   program replaces. A note that another program deleted and stored
//!   again, or changed, is therefore no longer bound. alrust's own update
//!   binds the new row version in the same transaction.
//!
//! A note with no valid binding is protected: it is listed, and it is not
//! changed or deleted (the same 404). That is every note another program
//! wrote or changed, every note of a restored dump, and every note stored
//! while the bindings were not in force. They are in force only when the
//! table, the function and the four triggers are exactly what `0003`
//! creates and the triggers are enabled ([`bindings_state`], read in the
//! same transaction as each write). An adopted reference database has none
//! of them (alrust writes no DDL there, ADR-0005 decision 4), so notes are
//! added and listed there and never changed or deleted.
//!
//! This holds for programs that write rows. It does not hold against a
//! role that changes the schema or bypasses triggers: a trigger that was
//! disabled and enabled again, or `session_replication_role = replica`,
//! can leave a binding whose alert was replaced, and alrust cannot see
//! that afterwards. After such a gap an operator empties
//! `alrust_note_bindings`; every note is then protected.
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
use sqlx::{PgConnection, Postgres, QueryBuilder, Row};

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

static MIGRATION_0003: &str = include_str!("../migrations/0003_note_bindings.sql");

/// The body of `alrust_note_bindings_clear()` as `0003` creates it: the
/// text between the two `$alrust$` marks.
fn clear_function_body() -> &'static str {
    MIGRATION_0003.split("$alrust$").nth(1).unwrap_or_default()
}

/// What the database has of the note bindings.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NoteBindings {
    /// The table, the function and the four triggers are what `0003`
    /// creates, and the triggers are enabled.
    InForce,
    /// None of them exists (an adopted reference database).
    Absent,
    /// Some exist and something is missing, different or disabled. Each
    /// line names one difference.
    Malformed(Vec<String>),
}

/// The four triggers: name, `pg_trigger.tgtype` (row 1, insert 4, delete 8,
/// update 16, truncate 32; no "before" bit) and whether a `WHEN` is needed.
const BINDINGS_STATE: &str = r"
WITH want(tgname, tgtype, has_when) AS (VALUES
    ('alrust_note_bindings_delete'::text, 9, false),
    ('alrust_note_bindings_insert', 5, false),
    ('alrust_note_bindings_truncate', 32, false),
    ('alrust_note_bindings_update', 17, true)),
tbl AS (
    SELECT c.oid, c.relkind::text AS relkind, c.relpersistence::text AS persistence
      FROM pg_class c WHERE c.oid = to_regclass('public.alrust_note_bindings')),
cols AS (
    SELECT coalesce(string_agg(a.attname || ' ' || format_type(a.atttypid, a.atttypmod)
                               || CASE WHEN a.attnotnull THEN ' not null' ELSE '' END,
                               ', ' ORDER BY a.attnum), '') AS found
      FROM tbl JOIN pg_attribute a ON a.attrelid = tbl.oid AND a.attnum > 0 AND NOT a.attisdropped),
fn AS (
    SELECT p.oid, p.prosecdef, p.prosrc, p.proconfig, p.prorettype::regtype::text AS returns
      FROM pg_proc p WHERE p.oid = to_regproc('public.alrust_note_bindings_clear')),
trg AS (
    SELECT t.tgname::text AS tgname, t.tgtype::int AS tgtype, t.tgenabled::text AS enabled,
           t.tgfoid, t.tgqual IS NOT NULL AS has_when, pg_get_triggerdef(t.oid) AS def
      FROM pg_trigger t
     WHERE t.tgrelid = to_regclass('public.alerts')::oid AND NOT t.tgisinternal
       AND starts_with(t.tgname::text, 'alrust_note_bindings_'))
SELECT (EXISTS (SELECT 1 FROM tbl) OR EXISTS (SELECT 1 FROM fn) OR EXISTS (SELECT 1 FROM trg)) AS present,
       ARRAY(
           SELECT 'the table public.alrust_note_bindings is missing'
            WHERE NOT EXISTS (SELECT 1 FROM tbl)
           UNION ALL
           SELECT 'public.alrust_note_bindings is not an ordinary permanent table'
             FROM tbl WHERE relkind <> 'r' OR persistence <> 'p'
           UNION ALL
           SELECT 'the columns of public.alrust_note_bindings are: ' || cols.found
             FROM cols WHERE EXISTS (SELECT 1 FROM tbl)
              AND cols.found <> 'note text not null, alert text not null, note_xmin xid not null'
           UNION ALL
           SELECT 'public.alrust_note_bindings has no valid primary key on (note)'
             FROM tbl WHERE NOT EXISTS (
                 SELECT 1 FROM pg_index i
                   JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attname = 'note'
                  WHERE i.indrelid = tbl.oid AND i.indisprimary AND i.indisvalid
                    AND i.indnatts = 1 AND i.indkey[0] = a.attnum)
           UNION ALL
           SELECT 'the function public.alrust_note_bindings_clear() is missing'
            WHERE NOT EXISTS (SELECT 1 FROM fn)
           UNION ALL
           SELECT 'the function public.alrust_note_bindings_clear() is not the one of migration 0003 (settings: '
                  || coalesce(fn.proconfig::text, 'none') || ')'
             FROM fn WHERE NOT fn.prosecdef OR fn.returns <> 'trigger' OR fn.prosrc <> $1
               OR fn.proconfig IS DISTINCT FROM ARRAY['search_path=pg_catalog, pg_temp']
           UNION ALL
           SELECT 'the trigger ' || w.tgname || ' on public.alerts is missing'
             FROM want w WHERE NOT EXISTS (SELECT 1 FROM trg WHERE trg.tgname = w.tgname)
           UNION ALL
           SELECT 'the trigger ' || w.tgname || ' on public.alerts is not the one of migration 0003, or is disabled (enabled '
                  || trg.enabled || '): ' || trg.def
             FROM want w JOIN trg ON trg.tgname = w.tgname
            WHERE trg.tgtype <> w.tgtype OR trg.enabled NOT IN ('O', 'A')
               OR trg.tgfoid IS DISTINCT FROM (SELECT fn.oid FROM fn)
               OR trg.has_when <> w.has_when
               OR (w.has_when AND position('WHEN ((old.id IS DISTINCT FROM new.id))' IN trg.def) = 0)
           UNION ALL
           SELECT 'unknown trigger ' || trg.tgname || ' on public.alerts'
             FROM trg WHERE NOT EXISTS (SELECT 1 FROM want w WHERE w.tgname = trg.tgname)
       ) AS problems";

/// What the database has of the note bindings, from the catalogs. See the
/// module text for what `InForce` cannot show.
///
/// # Errors
///
/// The database error.
pub async fn bindings_state(conn: &mut PgConnection) -> Result<NoteBindings, sqlx::Error> {
    let (present, problems): (bool, Vec<String>) = sqlx::query_as(BINDINGS_STATE)
        .bind(clear_function_body())
        .fetch_one(conn)
        .await?;
    Ok(if problems.is_empty() {
        NoteBindings::InForce
    } else if present {
        NoteBindings::Malformed(problems)
    } else {
        NoteBindings::Absent
    })
}

async fn bindings_in_force(conn: &mut PgConnection) -> Result<bool, sqlx::Error> {
    Ok(bindings_state(conn).await? == NoteBindings::InForce)
}

/// Whether the stored alert `alert_id` received the note `note_id` and the
/// note's row is still the bound one: the bindings are in force and have
/// that pair with the row's `xmin`. With `lock`, the note's row is locked
/// for the write that follows; the caller holds the alert's row lock.
async fn owns_note(
    conn: &mut PgConnection,
    alert_id: &str,
    note_id: &str,
    lock: bool,
) -> Result<bool, sqlx::Error> {
    if !bindings_in_force(&mut *conn).await? {
        return Ok(false);
    }
    let mut sql = String::from(
        "SELECT 1 FROM alrust_note_bindings b JOIN notes n ON n.id = b.note
          WHERE b.note = $1 AND b.alert = $2 AND n.alert = b.alert AND n.xmin = b.note_xmin",
    );
    if lock {
        sql.push_str(" FOR UPDATE OF n");
    }
    Ok(sqlx::query(&sql)
        .bind(note_id)
        .bind(alert_id)
        .fetch_optional(conn)
        .await?
        .is_some())
}

/// Bind the note to its alert at the note row's present `xmin`. The caller
/// has just written that row in this transaction.
async fn bind_note(conn: &mut PgConnection, note_id: &str) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO alrust_note_bindings (note, alert, note_xmin)
         SELECT n.id, n.alert, n.xmin FROM notes n WHERE n.id = $1
         ON CONFLICT (note) DO UPDATE SET alert = EXCLUDED.alert, note_xmin = EXCLUDED.note_xmin",
    )
    .bind(note_id)
    .execute(conn)
    .await?;
    Ok(())
}

impl Store {
    /// Add a note to the alert `id` (the usual lookup and customer filter)
    /// with the given new note id. The note is bound to the alert when the
    /// bindings are in force; otherwise it is stored without a binding and
    /// stays protected.
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
        if bindings_in_force(&mut tx).await? {
            // A binding left for this id by a note that another program
            // removed is replaced.
            bind_note(&mut tx, note_id).await?;
        }
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

    /// The reference's two lookups before a note update, with SF-NOTES:
    /// the alert `id` is visible and `note_id` is one of its notes. Reads
    /// only. The API calls it before a refusal that the reference makes
    /// after these lookups, so that a missing alert or note stays 404.
    ///
    /// # Errors
    ///
    /// `NotFound` for a missing alert or note, otherwise the database error.
    pub async fn check_note(
        &self,
        id: &str,
        customers: Option<&[String]>,
        note_id: &str,
    ) -> Result<(), StoreError> {
        let a = self.get_alert(id, customers).await?;
        let mut conn = self.pool.acquire().await?;
        if !owns_note(&mut conn, &a.id, note_id, false).await? {
            return Err(StoreError::NotFound);
        }
        drop(conn);
        sqlx::query("SELECT 1 FROM notes WHERE id = $1 AND alert = $2")
            .bind(note_id)
            .bind(&a.id)
            .fetch_optional(&self.pool)
            .await?
            .map(|_| ())
            .ok_or(StoreError::NotFound)
    }

    /// Update note `note_id` of the alert `id`. SF-NOTES: a note of another
    /// alert, or of an earlier alert with the same id, is `NotFound`, like a
    /// missing one, and nothing changes. The alert's row is locked from the
    /// lookup to the commit, so the alert cannot be deleted and stored again
    /// between the check and the write.
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
        let mut tx = self.pool.begin().await?;
        let a = Self::lock_alert(&mut tx, id, customers).await?;
        if !owns_note(&mut tx, &a.id, note_id, true).await? {
            tx.rollback().await?;
            return Err(StoreError::NotFound);
        }
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
        let Some(row) = qb.build().fetch_optional(&mut *tx).await? else {
            tx.rollback().await?;
            return Err(StoreError::NotFound);
        };
        let note = row_to_note(&row)?;
        // The update gave the row a new xmin; bind that version.
        bind_note(&mut tx, note_id).await?;
        tx.commit().await?;
        Ok(note)
    }

    /// Delete note `note_id` of the alert `id`: `Ok(false)` when the alert
    /// has no such note (SF-NOTES: a note of another alert, or of an earlier
    /// alert with the same id, counts as missing), with nothing changed.
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
        if !owns_note(&mut tx, &a.id, note_id, true).await? {
            tx.rollback().await?;
            return Ok(false);
        }
        let deleted = sqlx::query("DELETE FROM notes WHERE id = $1 AND alert = $2 RETURNING id")
            .bind(note_id)
            .bind(&a.id)
            .fetch_optional(&mut *tx)
            .await?;
        if deleted.is_none() {
            tx.rollback().await?;
            return Ok(false);
        }
        sqlx::query("DELETE FROM alrust_note_bindings WHERE note = $1")
            .bind(note_id)
            .execute(&mut *tx)
            .await?;
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
