//! Acknowledgement integration hooks (`spec/integration-hooks.md` section 3).
//!
//! An integration that acts on an acknowledgement implements
//! [`AckIntegration`]; the store calls its phases inside the acknowledgement's
//! own transaction, in this order, each at most once per request:
//!
//! 1. `pre_lookup(caller)` for every integration, before the alert is looked
//!    up (refusals that must not depend on the alert);
//! 2. `claims(alert)` for every integration, after the alert row is locked;
//!    more than one claimant refuses the acknowledgement;
//! 3. for the owner only: `gate`, then `stage` unless the gate declined,
//!    then alrust re-reads the locked row's attributes and applies its own
//!    changes on top, writes the row, and calls `pre_commit`;
//! 4. `commit_outcome`, for the owner only, when `COMMIT` fails with no
//!    server verdict.
//!
//! Separately, on receive (`POST /alert`), `pre_receive` runs for every
//! registered integration, in order, on the incoming alert before anything
//! is looked up or written (section 4.3); an error refuses the alert.
//!
//! The acknowledgement phases run only for `PUT /alert/<id>/action` with
//! `action = "ack"`, and
//! only when at least one integration is registered; otherwise the
//! acknowledgement runs exactly as before. An error from any phase before
//! `COMMIT` refuses the acknowledgement and the transaction rolls back. An
//! error from `commit_outcome` is different: `COMMIT` has already been
//! attempted, so the outcome is unknown (`StoreError::OutcomeUnknown`).
//!
//! No integration is registered yet: the adapters are later work.

use std::fmt;
use std::future::Future;
use std::pin::Pin;

use alrust_domain::{Alert, HistoryEntry};

use crate::{NewAlert, StoreError};
use alrust_engine::Transition;
use serde_json::{Map, Value};
use sqlx::{PgConnection, PgPool};

/// The future a hook phase returns.
pub type HookFuture<'a, T> = Pin<Box<dyn Future<Output = Result<T, HookError>> + Send + 'a>>;

/// An error from a hook: the request fails with this HTTP status and
/// message. Before `COMMIT` nothing is committed; from `commit_outcome` the
/// outcome is unknown.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HookError {
    pub status: u16,
    pub message: String,
}

impl HookError {
    pub fn new(status: u16, message: impl Into<String>) -> Self {
        HookError {
            status,
            message: message.into(),
        }
    }
}

impl fmt::Display for HookError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} {}", self.status, self.message)
    }
}

/// What alrust knows about the caller, for the hooks (spec section 2,
/// prerequisite 1). API-key callers have no user id and no verified auth
/// type; human operators (OIDC, DECISIONS Y44) are not implemented yet.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct CallerFacts {
    /// `g.login`; `None` without a credential when auth is off.
    pub login: Option<String>,
    pub user_id: Option<String>,
    /// For example `bearer:<provider>`; `None` for an API key.
    pub auth_type: Option<String>,
    pub scopes: Vec<String>,
    /// `None`: every customer.
    pub customers: Option<Vec<String>>,
    pub auth_required: bool,
    pub request_id: Option<String>,
}

/// The gate's decision for the owning integration.
#[derive(Debug, Clone, PartialEq)]
pub enum Gate {
    /// Stage next.
    Proceed,
    /// No capture: the acknowledgement proceeds without `stage`, carrying
    /// these attribute writes (a null value removes the key).
    Skip { attributes: Map<String, Value> },
}

/// What `stage` returns.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Staged {
    /// Replacement text for the history entry, if any.
    pub text: Option<String>,
    /// Attribute writes carried by alrust's `UPDATE` (a null value removes
    /// the key), applied on top of the re-read attributes.
    pub attributes: Map<String, Value>,
}

/// Whether the owner settled a `COMMIT` that failed with no server verdict.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CommitVerdict {
    /// The acknowledgement was saved; the request succeeds.
    Saved,
    /// Not handled: the commit error is returned as it is.
    NotHandled,
}

/// One integration that acts on acknowledgements. Every phase gets the
/// acknowledgement transaction's own connection, except `commit_outcome`,
/// which runs after that transaction ended and gets the pool.
pub trait AckIntegration: Send + Sync + fmt::Debug {
    /// On receive, before anything is looked up or written: may change the
    /// incoming alert (for example strip reserved attributes) or refuse it.
    /// Called for every registered integration, since ownership is not
    /// known yet. Default: nothing.
    fn pre_receive<'a>(
        &'a self,
        conn: &'a mut PgConnection,
        caller: &'a CallerFacts,
        incoming: &'a mut NewAlert,
    ) -> HookFuture<'a, ()> {
        let _ = (conn, caller, incoming);
        Box::pin(async { Ok(()) })
    }

    /// Before the alert is looked up. Default: nothing.
    fn pre_lookup<'a>(
        &'a self,
        conn: &'a mut PgConnection,
        caller: &'a CallerFacts,
    ) -> HookFuture<'a, ()> {
        let _ = (conn, caller);
        Box::pin(async { Ok(()) })
    }

    /// Whether this integration owns the locked alert.
    fn claims<'a>(&'a self, conn: &'a mut PgConnection, alert: &'a Alert) -> HookFuture<'a, bool>;

    /// Policy and health checks, with alrust's transition for this
    /// acknowledgement already computed.
    fn gate<'a>(
        &'a self,
        conn: &'a mut PgConnection,
        alert: &'a Alert,
        caller: &'a CallerFacts,
        transition: &'a Transition,
    ) -> HookFuture<'a, Gate>;

    /// The integration's staging call.
    fn stage<'a>(
        &'a self,
        conn: &'a mut PgConnection,
        alert: &'a Alert,
        caller: &'a CallerFacts,
        text: &'a str,
    ) -> HookFuture<'a, Staged>;

    /// After alrust's `UPDATE`, immediately before `COMMIT`. Default:
    /// nothing.
    fn pre_commit<'a>(
        &'a self,
        conn: &'a mut PgConnection,
        alert: &'a Alert,
        entry: &'a HistoryEntry,
    ) -> HookFuture<'a, ()> {
        let _ = (conn, alert, entry);
        Box::pin(async { Ok(()) })
    }

    /// Only when `COMMIT` failed with no server verdict. Default: not
    /// handled.
    fn commit_outcome<'a>(
        &'a self,
        pool: &'a PgPool,
        alert: &'a Alert,
        entry: &'a HistoryEntry,
    ) -> HookFuture<'a, CommitVerdict> {
        let _ = (pool, alert, entry);
        Box::pin(async { Ok(CommitVerdict::NotHandled) })
    }
}

/// The refusal when more than one integration claims an alert (section 3,
/// "at most one owner").
pub const MULTIPLE_OWNERS: &str = "ACK not processed: more than one integration claims this alert";

/// Whether a failed `COMMIT` carries a verdict from the server (an
/// SQLSTATE). Anything else, such as a lost connection, leaves the outcome
/// unknown (section 5, commit outcome).
pub fn commit_has_verdict(e: &sqlx::Error) -> bool {
    matches!(e, sqlx::Error::Database(_))
}

/// The request's result once `COMMIT` returned `commit`: success; a
/// failure with a server verdict as that database error, with no hook call;
/// otherwise, after exactly one `commit_outcome` call, success (`Saved`),
/// the original database error (`NotHandled`), or `OutcomeUnknown` (the
/// hook failed). Nothing is retried.
pub(crate) async fn settle_commit(
    commit: Result<(), sqlx::Error>,
    owner: &dyn AckIntegration,
    pool: &PgPool,
    alert: Alert,
    entry: &HistoryEntry,
) -> Result<Alert, StoreError> {
    match commit {
        Ok(()) => Ok(alert),
        Err(e) if commit_has_verdict(&e) => Err(e.into()),
        Err(e) => match owner.commit_outcome(pool, &alert, entry).await {
            Ok(CommitVerdict::Saved) => Ok(alert),
            Ok(CommitVerdict::NotHandled) => Err(e.into()),
            Err(h) => Err(StoreError::OutcomeUnknown(h)),
        },
    }
}

/// Zabalack's receive-time privacy filter, ported for its future adapter
/// (section 4.3; `zab-al-ack@eaf2acf:src/zabalack/normalizer.py:15-17`,
/// `34-36`). Nothing registers it yet: it runs only once a Zabalack adapter
/// is enabled, as the normaliser runs only where Zabalack is installed.
pub mod zabalack {
    use serde_json::{Map, Value};

    /// Case-sensitive key prefixes (`normalizer.py:16`); `zabbix_ack_sync`
    /// has no trailing underscore.
    pub const RESERVED_PREFIXES: &[&str] = &["zabbix_ack_sync", "zabalack_", "ackline_"];

    /// Exact keys (`normalizer.py:17`).
    pub const RESERVED_FIELDS: &[&str] = &[
        "zabbix_ack_intent",
        "zabbix_ack_proof",
        "ack_intent",
        "ack_proof",
    ];

    /// Removes reserved top-level keys; other keys and every value,
    /// including nested ones, are unchanged (`strip_reserved`,
    /// `normalizer.py:34-36`).
    pub fn strip_reserved(attributes: &mut Map<String, Value>) {
        attributes.retain(|k, _| {
            !RESERVED_FIELDS.contains(&k.as_str())
                && !RESERVED_PREFIXES.iter().any(|p| k.starts_with(p))
        });
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use serde_json::json;

        fn stripped(v: Value) -> Value {
            let Value::Object(mut m) = v else {
                panic!("an object")
            };
            strip_reserved(&mut m);
            Value::Object(m)
        }

        // Codex's prepared cases (44d0cef0) for the pinned normaliser.
        #[test]
        fn reserved_keys_go_and_everything_else_stays() {
            assert_eq!(
                stripped(
                    json!({"zabbix_ack_syncfoo": 1, "zabalack_any": 2, "ackline_legacy": 3,
                                 "ack_intent": 4, "safe": {"keep": 1}})
                ),
                json!({"safe": {"keep": 1}})
            );
            // Case variants, near lookalikes, suffixed exact fields and
            // nested keys are not reserved.
            let kept = json!({"ZABBIX_ACK_SYNC_intent": 1, "Zabalack_x": 2, "zabalack": 3,
                              "ackline": 4, "zabbix_ack_proofsuffix": 5,
                              "ordinary": {"ack_intent": 6}});
            assert_eq!(stripped(kept.clone()), kept);
            assert_eq!(
                stripped(json!({"source_system": "other", "ackline_legacy": "x",
                                 "safe": [1, null, {"zabalack_nested": 2}]})),
                json!({"source_system": "other", "safe": [1, null, {"zabalack_nested": 2}]})
            );
            // Every exact field and every prefix.
            assert_eq!(
                stripped(
                    json!({"zabbix_ack_intent": 1, "zabbix_ack_proof": 2, "ack_proof": 3,
                                 "zabbix_ack_sync": 4, "zabbix_ack_sync_intent": 5, "z": 6})
                ),
                json!({"z": 6})
            );
        }
    }
}

/// Applies attribute writes: a null value removes the key.
pub(crate) fn apply_writes(attributes: &mut Map<String, Value>, writes: &Map<String, Value>) {
    for (k, v) in writes {
        if v.is_null() {
            attributes.remove(k);
        } else {
            attributes.insert(k.clone(), v.clone());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    use std::borrow::Cow;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use sqlx::error::{DatabaseError, ErrorKind};

    /// A server error with an SQLSTATE, as PostgreSQL returns for a refused
    /// `COMMIT` (for example a deferred constraint).
    #[derive(Debug)]
    struct ServerRefusal {
        message: String,
    }

    impl fmt::Display for ServerRefusal {
        fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
            f.write_str(&self.message)
        }
    }

    impl std::error::Error for ServerRefusal {}

    impl DatabaseError for ServerRefusal {
        fn message(&self) -> &str {
            &self.message
        }
        fn code(&self) -> Option<Cow<'_, str>> {
            Some(Cow::Borrowed("23505"))
        }
        fn as_error(&self) -> &(dyn std::error::Error + Send + Sync + 'static) {
            self
        }
        fn as_error_mut(&mut self) -> &mut (dyn std::error::Error + Send + Sync + 'static) {
            self
        }
        fn into_error(self: Box<Self>) -> Box<dyn std::error::Error + Send + Sync + 'static> {
            self
        }
        fn kind(&self) -> ErrorKind {
            ErrorKind::UniqueViolation
        }
    }

    fn refused() -> sqlx::Error {
        sqlx::Error::Database(Box::new(ServerRefusal {
            message: "deferred constraint violated".into(),
        }))
    }

    fn lost() -> sqlx::Error {
        sqlx::Error::Io(std::io::Error::other("connection reset"))
    }

    /// An owner whose `commit_outcome` answers `answer` and counts its calls.
    #[derive(Debug)]
    struct Reconciler {
        answer: Result<CommitVerdict, HookError>,
        calls: AtomicUsize,
    }

    impl AckIntegration for Reconciler {
        fn claims<'a>(&'a self, _: &'a mut PgConnection, _: &'a Alert) -> HookFuture<'a, bool> {
            Box::pin(async { Ok(true) })
        }
        fn gate<'a>(
            &'a self,
            _: &'a mut PgConnection,
            _: &'a Alert,
            _: &'a CallerFacts,
            _: &'a Transition,
        ) -> HookFuture<'a, Gate> {
            Box::pin(async { Ok(Gate::Proceed) })
        }
        fn stage<'a>(
            &'a self,
            _: &'a mut PgConnection,
            _: &'a Alert,
            _: &'a CallerFacts,
            _: &'a str,
        ) -> HookFuture<'a, Staged> {
            Box::pin(async { Ok(Staged::default()) })
        }
        fn commit_outcome<'a>(
            &'a self,
            _: &'a PgPool,
            _: &'a Alert,
            _: &'a HistoryEntry,
        ) -> HookFuture<'a, CommitVerdict> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            let answer = self.answer.clone();
            Box::pin(async move { answer })
        }
    }

    fn alert() -> Alert {
        let t = chrono::Utc::now();
        Alert {
            id: "a".into(),
            resource: "r".into(),
            event: "e".into(),
            environment: "Production".into(),
            severity: "major".into(),
            correlate: vec![],
            status: "ack".into(),
            service: vec![],
            group: "Misc".into(),
            value: None,
            text: String::new(),
            tags: vec![],
            attributes: Map::new(),
            origin: "o".into(),
            event_type: "exceptionAlert".into(),
            create_time: t,
            timeout: Some(0),
            raw_data: None,
            customer: None,
            duplicate_count: Some(0),
            repeat: Some(false),
            previous_severity: None,
            trend_indication: None,
            receive_time: t,
            last_receive_id: None,
            last_receive_time: None,
            update_time: Some(t),
            history: vec![],
        }
    }

    fn entry() -> HistoryEntry {
        HistoryEntry {
            id: "a".into(),
            event: "e".into(),
            severity: None,
            status: Some("ack".into()),
            value: None,
            text: Some("t".into()),
            change_type: "ack".into(),
            update_time: chrono::Utc::now(),
            user: None,
            timeout: None,
        }
    }

    /// `settle_commit` for one commit result and one reconciler answer:
    /// the result and how often `commit_outcome` was called.
    async fn settle(
        commit: Result<(), sqlx::Error>,
        answer: Result<CommitVerdict, HookError>,
    ) -> (Result<Alert, StoreError>, usize) {
        let owner = Reconciler {
            answer,
            calls: AtomicUsize::new(0),
        };
        // Never connected: the synthetic reconciler does not use it.
        let pool = PgPool::connect_lazy("postgres://unused@127.0.0.1:1/unused").expect("lazy pool");
        let r = settle_commit(commit, &owner, &pool, alert(), &entry()).await;
        (r, owner.calls.load(Ordering::SeqCst))
    }

    #[tokio::test]
    async fn commit_outcome_runs_only_without_a_verdict_and_settles_each_answer() {
        let unknown = HookError::new(503, "ACK outcome unknown: check the alert before retrying");
        // Success and a server refusal: no reconciliation.
        let (r, n) = settle(Ok(()), Ok(CommitVerdict::Saved)).await;
        assert!(r.is_ok() && n == 0);
        let (r, n) = settle(Err(refused()), Ok(CommitVerdict::Saved)).await;
        assert!(matches!(r, Err(StoreError::Db(sqlx::Error::Database(_)))) && n == 0);
        // No verdict: exactly one call, and each answer settles as stated.
        let (r, n) = settle(Err(lost()), Ok(CommitVerdict::Saved)).await;
        assert!(r.is_ok() && n == 1);
        let (r, n) = settle(Err(lost()), Ok(CommitVerdict::NotHandled)).await;
        assert!(matches!(r, Err(StoreError::Db(sqlx::Error::Io(_)))) && n == 1);
        let (r, n) = settle(Err(lost()), Err(unknown.clone())).await;
        assert!(matches!(&r, Err(StoreError::OutcomeUnknown(h)) if *h == unknown) && n == 1);
    }

    #[test]
    fn a_database_error_is_a_verdict_and_a_lost_connection_is_not() {
        assert!(commit_has_verdict(&refused()));
        assert!(!commit_has_verdict(&sqlx::Error::PoolClosed));
        assert!(!commit_has_verdict(&sqlx::Error::Io(
            std::io::Error::other("reset")
        )));
        assert!(!commit_has_verdict(&sqlx::Error::Protocol("eof".into())));
    }

    #[test]
    fn writes_set_and_null_removes() {
        let mut m: Map<String, Value> =
            serde_json::from_value(serde_json::json!({"a": 1, "b": 2})).expect("map");
        let w: Map<String, Value> =
            serde_json::from_value(serde_json::json!({"a": null, "c": 3})).expect("map");
        apply_writes(&mut m, &w);
        assert_eq!(Value::Object(m), serde_json::json!({"b": 2, "c": 3}));
    }
}
