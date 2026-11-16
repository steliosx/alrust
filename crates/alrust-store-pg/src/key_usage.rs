//! API-key use, recorded off the request path (owner decision 2026-11-15,
//! DECISIONS Y68).
//!
//! The reference writes a key's `count` and `last_used_time` on every request
//! (`update_key_last_used`): a committed write on one shared row, even for a
//! read. Here a request verifies its key with a read only, under the same
//! rule as before (ADR-0005 decision 7: a NULL expiry or NULL scopes never
//! matches), and adds one to an in-memory tally. The server writes the tallies
//! about once a second, one statement for all keys. A deleted or expired key
//! still fails on its next request: the verification is never cached.
//!
//! Stated divergence: `GET /keys` shows a key's count and last use up to the
//! flush interval late, and a crash loses at most that interval's increments,
//! never an alert. `StoreConfig::key_usage_immediate` restores the reference's
//! per-request write.

use std::collections::HashMap;
use std::sync::{Mutex, PoisonError};

use alrust_domain::ApiKey;
use chrono::{NaiveDateTime, Utc};

use crate::{Store, StoreError, naive, row_to_key};

/// Uses not yet written, per key id: (uses, latest use).
#[derive(Debug, Default)]
pub struct KeyUsage {
    pending: Mutex<HashMap<String, (i64, NaiveDateTime)>>,
}

impl KeyUsage {
    fn record(&self, id: &str, at: NaiveDateTime) {
        let mut pending = self.pending.lock().unwrap_or_else(PoisonError::into_inner);
        let entry = pending.entry(id.to_owned()).or_insert((0, at));
        entry.0 += 1;
        entry.1 = entry.1.max(at);
    }

    fn drain(&self) -> HashMap<String, (i64, NaiveDateTime)> {
        std::mem::take(&mut *self.pending.lock().unwrap_or_else(PoisonError::into_inner))
    }

    /// Puts back what a failed flush took, merged with uses recorded since.
    fn restore(&self, drained: HashMap<String, (i64, NaiveDateTime)>) {
        let mut pending = self.pending.lock().unwrap_or_else(PoisonError::into_inner);
        for (id, (n, at)) in drained {
            let entry = pending.entry(id).or_insert((0, at));
            entry.0 += n;
            entry.1 = entry.1.max(at);
        }
    }
}

impl Store {
    /// The active key with this value, read only: an unexpired key with
    /// scopes (the same rule as [`Store::use_key`]).
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn verify_key(&self, key: &str) -> Result<Option<ApiKey>, StoreError> {
        let row = sqlx::query(
            "SELECT * FROM keys WHERE key = $1 AND expire_time > $2 AND scopes IS NOT NULL",
        )
        .bind(key)
        .bind(naive(Utc::now()))
        .fetch_optional(&self.pool)
        .await?;
        Ok(row.as_ref().map(row_to_key).transpose()?)
    }

    /// The key a request presents: verified, and its use recorded. With
    /// `key_usage_immediate` this is [`Store::use_key`] (the reference's
    /// per-request write); otherwise a read and an in-memory tally that
    /// [`Store::flush_key_usage`] writes.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn authenticate_key(&self, key: &str) -> Result<Option<ApiKey>, StoreError> {
        if self.cfg.key_usage_immediate {
            return self.use_key(key).await;
        }
        let found = self.verify_key(key).await?;
        if let Some(k) = &found {
            self.usage.record(&k.id, naive(Utc::now()));
        }
        Ok(found)
    }

    /// Writes every pending key use: `count` grows by the uses, and
    /// `last_used_time` becomes the latest use (`GREATEST` ignores NULL). A
    /// NULL `count` stays NULL, as the reference's `count + 1` leaves it, and
    /// a key deleted meanwhile matches no row. On a database error the uses
    /// are kept for the next flush. Returns the number of keys written.
    ///
    /// # Errors
    ///
    /// The database error.
    pub async fn flush_key_usage(&self) -> Result<u64, StoreError> {
        let drained = self.usage.drain();
        if drained.is_empty() {
            return Ok(0);
        }
        let mut ids = Vec::with_capacity(drained.len());
        let mut uses = Vec::with_capacity(drained.len());
        let mut times = Vec::with_capacity(drained.len());
        for (id, (n, at)) in &drained {
            ids.push(id.clone());
            uses.push(*n);
            times.push(*at);
        }
        let result = sqlx::query(
            "UPDATE keys AS k
                SET count = k.count + u.n,
                    last_used_time = GREATEST(k.last_used_time, u.t)
               FROM unnest($1::text[], $2::bigint[], $3::timestamp[]) AS u(id, n, t)
              WHERE k.id = u.id",
        )
        .bind(&ids)
        .bind(&uses)
        .bind(&times)
        .execute(&self.pool)
        .await;
        match result {
            Ok(done) => Ok(done.rows_affected()),
            Err(e) => {
                self.usage.restore(drained);
                Err(e.into())
            }
        }
    }
}
