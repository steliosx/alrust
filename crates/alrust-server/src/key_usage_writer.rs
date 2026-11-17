//! The API-key use writer (DECISIONS Y68, Y70). One task writes the store's
//! tallied key uses about once a second, so two writes never overlap. At
//! shutdown it lets a write in progress finish, then writes once more, after
//! the HTTP server has stopped taking requests.

use std::future::Future;
use std::time::Duration;

use alrust_domain::redact_diagnostic;
use alrust_store_pg::Store;
use tokio::sync::watch;

/// How often the tallies are written.
pub(crate) const INTERVAL: Duration = Duration::from_secs(1);

/// Which write this is: a periodic one, or the last one at shutdown.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum WriteKind {
    Periodic,
    Final,
}

/// Calls `write(Periodic)` every `every`, one call at a time, until `stop`
/// changes or its sender is dropped; then calls `write(Final)` once. `stop` is
/// only looked at between calls, so a call in progress always finishes first,
/// and it wins over a tick that is due at the same time.
pub(crate) async fn run<F, Fut>(mut write: F, mut stop: watch::Receiver<bool>, every: Duration)
where
    F: FnMut(WriteKind) -> Fut,
    Fut: Future<Output = ()>,
{
    let mut tick = tokio::time::interval(every);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        // Biased, stop first: once `stop` has changed, no further periodic
        // write starts, even when a tick is also due.
        tokio::select! {
            biased;
            _ = stop.changed() => break,
            _ = tick.tick() => write(WriteKind::Periodic).await,
        }
    }
    write(WriteKind::Final).await;
}

/// The time one write may take: what is left before the oldest pending use
/// reaches `max_pending`. `None` when nothing is pending or nothing is left
/// (expiry then drops those uses). Never extended to a full attempt.
pub(crate) fn budget(oldest_age: Option<Duration>, max_pending: Duration) -> Option<Duration> {
    let left = max_pending.saturating_sub(oldest_age?);
    (!left.is_zero()).then_some(left)
}

/// One write of `store`'s tallies, then [`report_losses`]. Uses past the
/// bound are expired first, so the budget is that of the oldest use still
/// pending. PostgreSQL bounds the write with the same budget
/// (`lock_timeout`, `statement_timeout`); this function's own timeout also
/// bounds getting a connection and a hung one. A write cancelled by it
/// settles by how far it got: before `COMMIT`, its uses stay pending (and
/// expire at the next write if their time is up); while committing, they
/// are reported as possibly unrecorded.
pub(crate) async fn write_once(store: Store, kind: WriteKind) {
    store.expire_key_usage();
    let what = match kind {
        WriteKind::Final => "final API-key use write",
        WriteKind::Periodic => "API-key use write",
    };
    if let Some(limit) = budget(
        store.key_usage_oldest_age(),
        store.config().key_usage_max_pending,
    ) {
        match tokio::time::timeout(limit, store.flush_key_usage()).await {
            Ok(Ok(_)) => {}
            Ok(Err(e)) => {
                tracing::warn!(error = %redact_diagnostic(e.to_string()), "{what} failed");
            }
            Err(_) => tracing::warn!("{what} did not finish within {limit:?}"),
        }
    }
    report_losses(&store, kind);
}

/// Logs each kind of loss since the last report, separately: expired,
/// unmatched and possibly unrecorded; and, at exit, what is still pending.
/// The writer calls it after every write, and the server after stopping a
/// writer that did not finish, so no count goes unreported.
pub(crate) fn report_losses(store: &Store, kind: WriteKind) {
    let dropped = store.take_key_usage_losses();
    if dropped.expired_uses > 0 {
        tracing::warn!(
            keys = dropped.expired_keys,
            uses = dropped.expired_uses,
            "API-key uses dropped: not written within {:?}",
            store.config().key_usage_max_pending
        );
    }
    if dropped.unmatched_uses > 0 {
        tracing::warn!(
            keys = dropped.unmatched_keys,
            uses = dropped.unmatched_uses,
            "API-key uses dropped: their key was deleted or its id or secret changed"
        );
    }
    if dropped.unknown_uses > 0 {
        tracing::warn!(
            keys = dropped.unknown_keys,
            uses = dropped.unknown_uses,
            "API-key uses may be unrecorded: the write's outcome is unknown"
        );
    }
    if kind == WriteKind::Final {
        let (keys, uses) = store.key_usage_pending();
        if uses > 0 {
            tracing::error!(keys, uses, "API-key uses still pending at exit are lost");
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    use tokio::sync::{Notify, watch};

    use super::{WriteKind, budget, run};

    /// cdx 0e4cb36f R3a: a write gets only the time left before the oldest
    /// use's bound, never a full attempt more.
    #[test]
    fn the_budget_is_the_time_left_and_never_more() {
        let bound = Duration::from_secs(30);
        assert_eq!(budget(None, bound), None, "nothing pending");
        assert_eq!(
            budget(Some(Duration::from_secs(29)), bound),
            Some(Duration::from_secs(1))
        );
        assert_eq!(
            budget(Some(Duration::from_millis(29_900)), bound),
            Some(Duration::from_millis(100))
        );
        assert_eq!(budget(Some(bound), bound), None, "expired");
        assert_eq!(budget(Some(Duration::from_secs(31)), bound), None);
        assert_eq!(budget(Some(Duration::ZERO), bound), Some(bound));
    }

    /// cdx 56ffa31d R1: a write in progress when shutdown starts finishes
    /// before the final write, nothing else starts meanwhile, and two writes
    /// never overlap.
    #[tokio::test]
    async fn shutdown_lets_the_write_in_progress_finish_then_writes_once_more() {
        let events = Arc::new(Mutex::new(Vec::<String>::new()));
        let calls = Arc::new(AtomicUsize::new(0));
        let active = Arc::new(AtomicUsize::new(0));
        let overlaps = Arc::new(AtomicUsize::new(0));
        let started = Arc::new(Notify::new());
        let release = Arc::new(Notify::new());
        let (stop, stopped) = watch::channel(false);

        let writer = {
            let (events, calls, active, overlaps, started, release) = (
                Arc::clone(&events),
                Arc::clone(&calls),
                Arc::clone(&active),
                Arc::clone(&overlaps),
                Arc::clone(&started),
                Arc::clone(&release),
            );
            tokio::spawn(run(
                move |kind: WriteKind| {
                    let (events, active, overlaps, started, release) = (
                        Arc::clone(&events),
                        Arc::clone(&active),
                        Arc::clone(&overlaps),
                        Arc::clone(&started),
                        Arc::clone(&release),
                    );
                    let first = calls.fetch_add(1, Ordering::SeqCst) == 0;
                    async move {
                        if active.fetch_add(1, Ordering::SeqCst) > 0 {
                            overlaps.fetch_add(1, Ordering::SeqCst);
                        }
                        events
                            .lock()
                            .expect("events")
                            .push(format!("start {kind:?}"));
                        if first {
                            // The first periodic write blocks until released.
                            started.notify_one();
                            release.notified().await;
                        }
                        events.lock().expect("events").push(format!("end {kind:?}"));
                        active.fetch_sub(1, Ordering::SeqCst);
                    }
                },
                stopped,
                Duration::from_millis(10),
            ))
        };

        started.notified().await;
        stop.send(true).expect("the writer is listening");
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert_eq!(
            *events.lock().expect("events"),
            ["start Periodic"],
            "nothing else runs while a write is in progress"
        );
        release.notify_one();
        tokio::time::timeout(Duration::from_secs(5), writer)
            .await
            .expect("the writer finishes")
            .expect("the writer does not panic");
        assert_eq!(
            *events.lock().expect("events"),
            ["start Periodic", "end Periodic", "start Final", "end Final"]
        );
        assert_eq!(overlaps.load(Ordering::SeqCst), 0);
    }
}
