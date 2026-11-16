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

/// Calls `write(false)` every `every`, one call at a time, until `stop`
/// changes or its sender is dropped; then calls `write(true)` once. `stop` is
/// only looked at between calls, so a call in progress always finishes first,
/// and it wins over a tick that is due at the same time.
pub(crate) async fn run<F, Fut>(mut write: F, mut stop: watch::Receiver<bool>, every: Duration)
where
    F: FnMut(bool) -> Fut,
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
            _ = tick.tick() => write(false).await,
        }
    }
    write(true).await;
}

/// One write of `store`'s tallies, with what was lost logged. After the
/// final write, uses still pending are logged as lost. PostgreSQL bounds a
/// write normally (`lock_timeout` and `statement_timeout`, set per write);
/// this function's own timeout, the retention bound itself, is a backstop
/// for a hung connection, so no write outlives `key_usage_max_pending`.
pub(crate) fn write_once(store: Store, last: bool) -> impl Future<Output = ()> + Send {
    async move { write_logged(&store, last).await }
}

async fn write_logged(store: &Store, last: bool) {
    let limit = store.config().key_usage_max_pending;
    let what = if last {
        "final API-key use write"
    } else {
        "API-key use write"
    };
    match tokio::time::timeout(limit, store.flush_key_usage()).await {
        Ok(Ok(_)) => {}
        Ok(Err(e)) => {
            tracing::warn!(error = %redact_diagnostic(e.to_string()), "{what} failed");
        }
        Err(_) => tracing::warn!("{what} did not finish within {limit:?}"),
    }
    let lost = store.take_key_usage_losses();
    if lost.expired_uses > 0 {
        tracing::warn!(
            keys = lost.expired_keys,
            uses = lost.expired_uses,
            "API-key uses dropped: not written within {:?}",
            store.config().key_usage_max_pending
        );
    }
    if lost.unknown_uses > 0 {
        tracing::warn!(
            keys = lost.unknown_keys,
            uses = lost.unknown_uses,
            "API-key uses may be unrecorded: the write's outcome is unknown"
        );
    }
    if last {
        let (keys, uses) = store.key_usage_pending();
        if uses > 0 {
            tracing::error!(keys, uses, "API-key uses not written before exit are lost");
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    use tokio::sync::{Notify, watch};

    use super::run;

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
                move |last| {
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
                        events.lock().expect("events").push(format!("start {last}"));
                        if first {
                            // The first periodic write blocks until released.
                            started.notify_one();
                            release.notified().await;
                        }
                        events.lock().expect("events").push(format!("end {last}"));
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
            ["start false"],
            "nothing else runs while a write is in progress"
        );
        release.notify_one();
        tokio::time::timeout(Duration::from_secs(5), writer)
            .await
            .expect("the writer finishes")
            .expect("the writer does not panic");
        assert_eq!(
            *events.lock().expect("events"),
            ["start false", "end false", "start true", "end true"]
        );
        assert_eq!(overlaps.load(Ordering::SeqCst), 0);
    }
}
