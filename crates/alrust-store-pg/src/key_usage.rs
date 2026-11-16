//! API-key use, recorded off the request path (owner decisions 2026-11-15
//! and 2026-11-16, DECISIONS Y68 and Y70).
//!
//! The reference writes a key's `count` and `last_used_time` on every request
//! (`update_key_last_used`): a committed write on one shared row, even for a
//! read. Here a request verifies its key with a read only, under the same
//! rule as before (ADR-0005 decision 7: a NULL expiry or NULL scopes never
//! matches), and adds one to an in-memory tally. The server's writer writes
//! the tallies about once a second, in one statement for all keys. A deleted
//! or expired key still fails on its next request: the verification is never
//! cached.
//!
//! The contract (Y68, with Y70's failure rule):
//! - In normal operation `GET /keys` shows a key's count and last use about
//!   one second late.
//! - A tally is bound to the credential, not only to its id: the key's id and
//!   a SHA-256 of its secret, computed by PostgreSQL. A key deleted and
//!   recreated under the same id with a new secret gets none of the old uses.
//!   No secret is kept in memory.
//! - A write that is refused or never reaches `COMMIT` changes nothing and
//!   its uses stay pending, so the next write retries them. Uses still
//!   unwritten after `StoreConfig::key_usage_max_pending` (30 seconds) are
//!   dropped and reported. A write is bounded by
//!   `StoreConfig::key_usage_attempt_timeout`, so requests never wait for it.
//! - A use is never counted twice. When the outcome of a `COMMIT` is unknown
//!   (the connection failed, or the write was abandoned while committing),
//!   its uses are treated as written and reported as possibly unrecorded.
//! - A crash loses the uses not yet written. Alerts are never involved.
//!
//! `StoreConfig::key_usage_immediate` restores the reference's per-request
//! write.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicUsize, Ordering};
use std::sync::{Mutex, MutexGuard, PoisonError};
use std::time::Instant;

use alrust_domain::ApiKey;
use chrono::{NaiveDateTime, Utc};
use sqlx::Row;

use crate::{Store, StoreError, naive, row_to_key};

/// One credential: the key's id and the hex SHA-256 of its secret.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct Credential {
    id: String,
    key_hash: String,
}

/// Uses not yet written for one credential.
#[derive(Debug, Clone, Copy)]
struct Pending {
    uses: i64,
    last: NaiveDateTime,
    /// When the oldest of these uses was recorded.
    since: Instant,
}

/// Uses dropped since the last report.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct KeyUsageLosses {
    /// Credentials and uses dropped after waiting longer than the bound.
    pub expired_keys: usize,
    pub expired_uses: i64,
    /// Credentials and uses whose write had an unknown outcome; they are
    /// treated as written, so they may be missing from the database.
    pub unknown_keys: usize,
    pub unknown_uses: i64,
}

/// How a write attempt ended, for the uses it carried.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Outcome {
    /// Committed: the uses are written.
    Written,
    /// Refused, failed or abandoned before `COMMIT`: nothing changed.
    NotCommitted,
    /// `COMMIT` was sent and its answer was lost.
    Unknown,
}

/// The tallies, shared by every clone of a store.
#[derive(Debug, Default)]
pub struct KeyUsage {
    pending: Mutex<HashMap<Credential, Pending>>,
    /// Set while a write is in progress: writes never overlap.
    writing: AtomicBool,
    expired_keys: AtomicUsize,
    expired_uses: AtomicI64,
    unknown_keys: AtomicUsize,
    unknown_uses: AtomicI64,
}

impl KeyUsage {
    fn map(&self) -> MutexGuard<'_, HashMap<Credential, Pending>> {
        self.pending.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn record(&self, credential: Credential, at: NaiveDateTime, now: Instant) {
        let mut map = self.map();
        let entry = map.entry(credential).or_insert(Pending {
            uses: 0,
            last: at,
            since: now,
        });
        entry.uses += 1;
        entry.last = entry.last.max(at);
    }

    /// Drops the uses pending for `max_age` or longer.
    fn expire(&self, max_age: std::time::Duration, now: Instant) {
        let mut map = self.map();
        let before = map.len();
        let mut uses = 0;
        map.retain(|_, p| {
            let keep = now.saturating_duration_since(p.since) < max_age;
            if !keep {
                uses += p.uses;
            }
            keep
        });
        let keys = before - map.len();
        if keys > 0 {
            self.expired_keys.fetch_add(keys, Ordering::Relaxed);
            self.expired_uses.fetch_add(uses, Ordering::Relaxed);
        }
    }

    /// A copy of what is pending; nothing is removed until it is written.
    fn snapshot(&self) -> Vec<(Credential, Pending)> {
        self.map().iter().map(|(c, p)| (c.clone(), *p)).collect()
    }

    /// Settles a write attempt. Written or unknown: the batch's uses leave
    /// the tally (uses recorded since the snapshot stay, dated from it).
    /// Not committed: the tally is unchanged and the next write retries.
    fn settle(&self, batch: &[(Credential, Pending)], taken: Instant, outcome: Outcome) {
        if outcome == Outcome::NotCommitted {
            return;
        }
        let mut map = self.map();
        for (credential, written) in batch {
            let emptied = match map.get_mut(credential) {
                Some(p) => {
                    p.uses -= written.uses;
                    if p.uses > 0 {
                        p.since = taken;
                    }
                    p.uses <= 0
                }
                None => false,
            };
            if emptied {
                map.remove(credential);
            }
        }
        if outcome == Outcome::Unknown {
            self.unknown_keys.fetch_add(batch.len(), Ordering::Relaxed);
            self.unknown_uses
                .fetch_add(batch.iter().map(|(_, p)| p.uses).sum(), Ordering::Relaxed);
        }
    }

    fn take_losses(&self) -> KeyUsageLosses {
        KeyUsageLosses {
            expired_keys: self.expired_keys.swap(0, Ordering::Relaxed),
            expired_uses: self.expired_uses.swap(0, Ordering::Relaxed),
            unknown_keys: self.unknown_keys.swap(0, Ordering::Relaxed),
            unknown_uses: self.unknown_uses.swap(0, Ordering::Relaxed),
        }
    }
}

/// A write in progress. If its future is dropped (cancelled by a timeout or
/// at shutdown), the drop settles the batch by how far the write got.
struct Attempt<'a> {
    usage: &'a KeyUsage,
    batch: Vec<(Credential, Pending)>,
    taken: Instant,
    committing: bool,
    settled: bool,
}

impl Attempt<'_> {
    fn finish(&mut self, outcome: Outcome) {
        self.usage.settle(&self.batch, self.taken, outcome);
        self.settled = true;
    }
}

impl Drop for Attempt<'_> {
    fn drop(&mut self) {
        if !self.settled {
            let outcome = if self.committing {
                Outcome::Unknown
            } else {
                Outcome::NotCommitted
            };
            self.usage.settle(&self.batch, self.taken, outcome);
        }
        self.usage.writing.store(false, Ordering::Release);
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
        Ok(self.verified_key(key).await?.map(|(k, _)| k))
    }

    /// [`Store::verify_key`], with the hex SHA-256 of the key's secret.
    async fn verified_key(&self, key: &str) -> Result<Option<(ApiKey, String)>, StoreError> {
        let row = sqlx::query(
            "SELECT *, encode(sha256(convert_to(key, 'UTF8')), 'hex') AS key_hash
               FROM keys WHERE key = $1 AND expire_time > $2 AND scopes IS NOT NULL",
        )
        .bind(key)
        .bind(naive(Utc::now()))
        .fetch_optional(&self.pool)
        .await?;
        match row {
            Some(r) => Ok(Some((row_to_key(&r)?, r.try_get::<String, _>("key_hash")?))),
            None => Ok(None),
        }
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
        let found = self.verified_key(key).await?;
        Ok(found.map(|(k, key_hash)| {
            let credential = Credential {
                id: k.id.clone(),
                key_hash,
            };
            self.usage
                .record(credential, naive(Utc::now()), Instant::now());
            k
        }))
    }

    /// Writes the pending key uses: `count` grows by the uses and
    /// `last_used_time` becomes the latest use (`GREATEST` ignores NULL), for
    /// the row with the same id and the same secret. A NULL `count` stays
    /// NULL, as the reference's `count + 1` leaves it; a key deleted or
    /// recreated meanwhile matches no row. Uses pending longer than
    /// `key_usage_max_pending` are dropped first. The write runs in one
    /// transaction with `lock_timeout` and `statement_timeout` set to
    /// `key_usage_attempt_timeout`. Returns the number of rows written; a
    /// write already in progress makes this call return 0 at once.
    ///
    /// See the module documentation for what each failure does to the uses,
    /// and [`Store::take_key_usage_losses`] for what was dropped.
    ///
    /// # Errors
    ///
    /// The database error; the uses stay pending unless the error came from
    /// `COMMIT` with an unknown outcome.
    pub async fn flush_key_usage(&self) -> Result<u64, StoreError> {
        let usage: &KeyUsage = &self.usage;
        if usage.writing.swap(true, Ordering::Acquire) {
            return Ok(0);
        }
        let taken = Instant::now();
        usage.expire(self.cfg.key_usage_max_pending, taken);
        let mut attempt = Attempt {
            usage,
            batch: usage.snapshot(),
            taken,
            committing: false,
            settled: false,
        };
        if attempt.batch.is_empty() {
            attempt.settled = true;
            return Ok(0);
        }
        let mut ids = Vec::with_capacity(attempt.batch.len());
        let mut hashes = Vec::with_capacity(attempt.batch.len());
        let mut uses = Vec::with_capacity(attempt.batch.len());
        let mut times = Vec::with_capacity(attempt.batch.len());
        for (c, p) in &attempt.batch {
            ids.push(c.id.clone());
            hashes.push(c.key_hash.clone());
            uses.push(p.uses);
            times.push(p.last);
        }
        let limit = format!(
            "{}ms",
            self.cfg.key_usage_attempt_timeout.as_millis().max(1)
        );
        let mut tx = self.pool.begin().await?;
        sqlx::query(
            "SELECT set_config('lock_timeout', $1, true), set_config('statement_timeout', $1, true)",
        )
        .bind(&limit)
        .execute(&mut *tx)
        .await?;
        let done = sqlx::query(
            "UPDATE keys AS k
                SET count = k.count + u.n,
                    last_used_time = GREATEST(k.last_used_time, u.t)
               FROM unnest($1::text[], $2::text[], $3::bigint[], $4::timestamp[]) AS u(id, h, n, t)
              WHERE k.id = u.id AND encode(sha256(convert_to(k.key, 'UTF8')), 'hex') = u.h",
        )
        .bind(&ids)
        .bind(&hashes)
        .bind(&uses)
        .bind(&times)
        .execute(&mut *tx)
        .await?;
        attempt.committing = true;
        match tx.commit().await {
            Ok(()) => {
                attempt.finish(Outcome::Written);
                Ok(done.rows_affected())
            }
            // PostgreSQL answered: the transaction did not commit.
            Err(e @ sqlx::Error::Database(_)) => {
                attempt.finish(Outcome::NotCommitted);
                Err(e.into())
            }
            Err(e) => {
                attempt.finish(Outcome::Unknown);
                Err(e.into())
            }
        }
    }

    /// The credentials and uses waiting to be written.
    #[must_use]
    pub fn key_usage_pending(&self) -> (usize, i64) {
        let map = self.usage.map();
        (map.len(), map.values().map(|p| p.uses).sum())
    }

    /// The uses dropped or possibly unrecorded since the last call; the
    /// counters restart at zero.
    #[must_use]
    pub fn take_key_usage_losses(&self) -> KeyUsageLosses {
        self.usage.take_losses()
    }
}

#[cfg(test)]
mod tests {
    use super::{Credential, KeyUsage, KeyUsageLosses, Outcome, Pending};
    use chrono::NaiveDateTime;
    use std::time::{Duration, Instant};

    fn cred(id: &str) -> Credential {
        Credential {
            id: id.to_owned(),
            key_hash: format!("hash-of-{id}"),
        }
    }

    fn total(usage: &KeyUsage) -> i64 {
        usage.map().values().map(|p| p.uses).sum()
    }

    #[test]
    fn each_outcome_settles_the_batch_as_documented() {
        let usage = KeyUsage::default();
        let at = NaiveDateTime::MIN;
        let start = Instant::now();
        for _ in 0..3 {
            usage.record(cred("a"), at, start);
        }
        let batch: Vec<(Credential, Pending)> = usage.snapshot();
        usage.record(cred("a"), at, start);

        // Not committed: everything stays for the next write.
        usage.settle(&batch, start, Outcome::NotCommitted);
        assert_eq!(total(&usage), 4);

        // Written: the batch's 3 uses leave; the later one stays, dated
        // from the snapshot.
        let later = start + Duration::from_millis(5);
        usage.settle(&batch, later, Outcome::Written);
        assert_eq!(total(&usage), 1);
        assert_eq!(usage.map().get(&cred("a")).map(|p| p.since), Some(later));
        assert_eq!(usage.take_losses(), KeyUsageLosses::default());

        // Unknown: treated as written (never retried, so never counted
        // twice) and reported.
        let batch = usage.snapshot();
        usage.settle(&batch, later, Outcome::Unknown);
        assert_eq!(total(&usage), 0);
        assert_eq!(
            usage.take_losses(),
            KeyUsageLosses {
                unknown_keys: 1,
                unknown_uses: 1,
                ..KeyUsageLosses::default()
            }
        );
        assert_eq!(usage.take_losses(), KeyUsageLosses::default());
    }

    #[test]
    fn uses_past_the_bound_are_dropped_and_reported() {
        let usage = KeyUsage::default();
        let at = NaiveDateTime::MIN;
        let start = Instant::now();
        usage.record(cred("old"), at, start);
        usage.record(cred("old"), at, start);
        usage.record(cred("new"), at, start + Duration::from_secs(20));
        usage.expire(Duration::from_secs(30), start + Duration::from_secs(30));
        assert_eq!(total(&usage), 1);
        assert!(usage.map().contains_key(&cred("new")));
        assert_eq!(
            usage.take_losses(),
            KeyUsageLosses {
                expired_keys: 1,
                expired_uses: 2,
                ..KeyUsageLosses::default()
            }
        );
    }

    #[test]
    fn the_same_id_with_another_secret_is_another_credential() {
        let usage = KeyUsage::default();
        let at = NaiveDateTime::MIN;
        let now = Instant::now();
        usage.record(cred("a"), at, now);
        usage.record(
            Credential {
                id: "a".to_owned(),
                key_hash: "another".to_owned(),
            },
            at,
            now,
        );
        assert_eq!(usage.map().len(), 2);
    }
}
