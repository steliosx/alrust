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
//!   one second late. Requests never wait for a write: they only update the
//!   in-memory tally under a short lock.
//! - A tally is bound to the credential, not only to its id: the key's id and
//!   a SHA-256 of its secret, computed by PostgreSQL from the same row the
//!   request was verified against, and matched the same way by the write.
//!   The pending tallies hold no raw secret. A key deleted, or recreated under
//!   the same id with a new secret, or given a new id by `ensure_key`, matches
//!   no row: its uses are dropped and reported as unmatched. The same id with
//!   the same secret is the same credential.
//! - Only a failure before `COMMIT` is sent proves that nothing was written:
//!   those uses stay pending and the next write retries them. Any error from
//!   `COMMIT`, or a write abandoned while committing, has an unknown outcome:
//!   its uses are never retried, so a use is never counted twice, and they
//!   are reported as possibly unrecorded.
//! - Uses still pending `StoreConfig::key_usage_max_pending` (30 seconds)
//!   after the oldest of them was recorded are dropped at the next write and
//!   reported. A credential's tally ages from its oldest pending use, so newer
//!   uses merged into it are dropped with it; after a successful write, the
//!   uses recorded meanwhile are dated from that write's snapshot.
//! - No write runs past the oldest pending use's 30-second deadline: the
//!   writer's timeout and PostgreSQL's `lock_timeout` and `statement_timeout`
//!   for the write are all cut to the time left before it (and to
//!   `key_usage_attempt_timeout` at most). The one exception is a `COMMIT`
//!   already sent when the deadline passes: PostgreSQL may still apply it,
//!   and its uses settle as unknown (possibly unrecorded, never retried).
//! - Memory: only uses of a key that verified against a `keys` row are
//!   recorded, so the tally holds at most one entry per distinct key used in
//!   the last 30 seconds, never more than the rows in `keys`, at roughly 200
//!   bytes each. A write takes at most `StoreConfig::key_usage_batch` (1,000)
//!   of the oldest entries, so one statement's arrays and `RETURNING` rows
//!   are bounded; the others wait for the next write under the same 30-second
//!   rule. Taking a snapshot copies the tally under its lock (one entry per
//!   key); sorting and the database work happen outside it. More distinct
//!   keys than one batch per write under lasting trouble would see expiry,
//!   which is Y70's rule, not another one.
//! - Reported separately and disjointly: expired (dropped after the bound),
//!   unmatched (written, but no row matched), unknown (possibly unrecorded),
//!   and, at exit, whatever is still pending (the writer logs it as lost).
//!   A crash loses the uses not yet written. Alerts are never involved.
//!
//! `StoreConfig::key_usage_immediate` restores the reference's per-request
//! write.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicUsize, Ordering};
use std::sync::{Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant};

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
    /// Credentials and uses whose write committed but matched no row (the
    /// key was deleted, or its id or secret changed).
    pub unmatched_keys: usize,
    pub unmatched_uses: i64,
    /// Credentials and uses whose write had an unknown outcome; they are
    /// never retried, so they may be missing from the database.
    pub unknown_keys: usize,
    pub unknown_uses: i64,
}

/// How a write attempt ended, for the uses it carried.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Outcome {
    /// Committed: the uses are written.
    Written,
    /// Failed or abandoned before `COMMIT` was sent: nothing changed.
    NotCommitted,
    /// `COMMIT` was sent and failed, or its answer was lost.
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
    unmatched_keys: AtomicUsize,
    unmatched_uses: AtomicI64,
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
    fn expire(&self, max_age: Duration, now: Instant) {
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

    /// A copy of the oldest `limit` pending tallies; nothing is removed
    /// until it is written. The lock is held only to copy.
    fn snapshot(&self, limit: usize) -> Vec<(Credential, Pending)> {
        let mut all: Vec<(Credential, Pending)> =
            self.map().iter().map(|(c, p)| (c.clone(), *p)).collect();
        if all.len() > limit {
            all.sort_by_key(|(_, p)| p.since);
            all.truncate(limit);
        }
        all
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

    fn note_unmatched(&self, batch: &[(Credential, Pending)], matched: &HashSet<Credential>) {
        let mut keys = 0;
        let mut uses = 0;
        for (c, p) in batch {
            if !matched.contains(c) {
                keys += 1;
                uses += p.uses;
            }
        }
        if keys > 0 {
            self.unmatched_keys.fetch_add(keys, Ordering::Relaxed);
            self.unmatched_uses.fetch_add(uses, Ordering::Relaxed);
        }
    }

    fn take_losses(&self) -> KeyUsageLosses {
        KeyUsageLosses {
            expired_keys: self.expired_keys.swap(0, Ordering::Relaxed),
            expired_uses: self.expired_uses.swap(0, Ordering::Relaxed),
            unmatched_keys: self.unmatched_keys.swap(0, Ordering::Relaxed),
            unmatched_uses: self.unmatched_uses.swap(0, Ordering::Relaxed),
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
    /// `key_usage_attempt_timeout`. Returns the number of credentials
    /// written; a write already in progress makes this call return 0 at
    /// once.
    ///
    /// See the module documentation for what each failure does to the uses,
    /// and [`Store::take_key_usage_losses`] for what was dropped.
    ///
    /// # Errors
    ///
    /// The database error. The uses stay pending when it came before
    /// `COMMIT`; an error from `COMMIT` has an unknown outcome, and those uses
    /// are reported, not retried.
    pub async fn flush_key_usage(&self) -> Result<u64, StoreError> {
        let usage: &KeyUsage = &self.usage;
        if usage.writing.swap(true, Ordering::Acquire) {
            return Ok(0);
        }
        let taken = Instant::now();
        usage.expire(self.cfg.key_usage_max_pending, taken);
        let mut attempt = Attempt {
            usage,
            batch: usage.snapshot(self.cfg.key_usage_batch),
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
        // The time left before the oldest use in the batch reaches the bound,
        // and one attempt at most: PostgreSQL ends the write by then.
        let oldest = attempt.batch.iter().map(|(_, p)| p.since).min();
        let left = oldest.map_or(self.cfg.key_usage_max_pending, |since| {
            self.cfg
                .key_usage_max_pending
                .saturating_sub(taken.saturating_duration_since(since))
        });
        let limit = format!(
            "{}ms",
            left.min(self.cfg.key_usage_attempt_timeout)
                .as_millis()
                .max(1)
        );
        let mut tx = self.pool.begin().await?;
        sqlx::query(
            "SELECT set_config('lock_timeout', $1, true), set_config('statement_timeout', $1, true)",
        )
        .bind(&limit)
        .execute(&mut *tx)
        .await?;
        let rows = sqlx::query(
            "UPDATE keys AS k
                SET count = k.count + u.n,
                    last_used_time = GREATEST(k.last_used_time, u.t)
               FROM unnest($1::text[], $2::text[], $3::bigint[], $4::timestamp[]) AS u(id, h, n, t)
              WHERE k.id = u.id AND encode(sha256(convert_to(k.key, 'UTF8')), 'hex') = u.h
          RETURNING u.id, u.h",
        )
        .bind(&ids)
        .bind(&hashes)
        .bind(&uses)
        .bind(&times)
        .fetch_all(&mut *tx)
        .await?;
        let mut matched = HashSet::with_capacity(rows.len());
        for r in &rows {
            matched.insert(Credential {
                id: r.try_get("id")?,
                key_hash: r.try_get("h")?,
            });
        }
        // From here on `COMMIT` may reach the server: any failure or
        // cancellation has an unknown outcome.
        attempt.committing = true;
        let committed = tx.commit().await;
        match committed {
            Ok(()) => {
                usage.note_unmatched(&attempt.batch, &matched);
                attempt.finish(Outcome::Written);
                Ok(u64::try_from(matched.len()).unwrap_or(u64::MAX))
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

    /// Drops the uses pending longer than `key_usage_max_pending` and counts
    /// them as expired; [`Store::flush_key_usage`] also does this first.
    pub fn expire_key_usage(&self) {
        self.usage
            .expire(self.cfg.key_usage_max_pending, Instant::now());
    }

    /// How long the oldest pending use has waited, if any is pending.
    #[must_use]
    pub fn key_usage_oldest_age(&self) -> Option<Duration> {
        let now = Instant::now();
        self.usage
            .map()
            .values()
            .map(|p| now.saturating_duration_since(p.since))
            .max()
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
    use super::{Attempt, Credential, KeyUsage, KeyUsageLosses, Outcome, Pending};
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
        let batch: Vec<(Credential, Pending)> = usage.snapshot(usize::MAX);
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
        let batch = usage.snapshot(usize::MAX);
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

    /// cdx 56ffa31d item 6: a write abandoned while committing is never
    /// retried (so never counted twice) and is reported; one abandoned
    /// before `COMMIT` leaves its uses pending.
    #[test]
    fn an_abandoned_write_is_settled_by_how_far_it_got() {
        let usage = KeyUsage::default();
        let at = NaiveDateTime::MIN;
        let start = Instant::now();
        usage.record(cred("a"), at, start);
        usage.record(cred("a"), at, start);
        for committing in [false, true] {
            usage
                .writing
                .store(true, std::sync::atomic::Ordering::Release);
            drop(Attempt {
                usage: &usage,
                batch: usage.snapshot(usize::MAX),
                taken: start,
                committing,
                settled: false,
            });
            assert!(!usage.writing.load(std::sync::atomic::Ordering::Acquire));
            if committing {
                assert_eq!(total(&usage), 0);
                assert_eq!(
                    usage.take_losses(),
                    KeyUsageLosses {
                        unknown_keys: 1,
                        unknown_uses: 2,
                        ..KeyUsageLosses::default()
                    }
                );
            } else {
                assert_eq!(total(&usage), 2);
                assert_eq!(usage.take_losses(), KeyUsageLosses::default());
            }
        }
    }

    /// cdx 0e4cb36f R3b: a write carries at most a batch of the oldest
    /// tallies; the rest stay for the next write.
    #[test]
    fn a_write_takes_at_most_a_batch_of_the_oldest() {
        let usage = KeyUsage::default();
        let at = NaiveDateTime::MIN;
        let start = Instant::now();
        for i in 0..2500_u64 {
            usage.record(
                cred(&format!("k{i:04}")),
                at,
                start + Duration::from_millis(i),
            );
        }
        let batch = usage.snapshot(1000);
        assert_eq!(batch.len(), 1000);
        let newest_taken = batch.iter().map(|(_, p)| p.since).max();
        assert_eq!(newest_taken, Some(start + Duration::from_millis(999)));
        assert_eq!(usage.map().len(), 2500, "a snapshot removes nothing");
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
