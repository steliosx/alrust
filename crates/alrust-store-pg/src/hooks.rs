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
//! Hooks run only for `PUT /alert/<id>/action` with `action = "ack"`, and
//! only when at least one integration is registered; otherwise the
//! acknowledgement runs exactly as before. Any hook error refuses the
//! acknowledgement and the transaction rolls back before commit.
//!
//! No integration is registered yet: the adapters are later work.

use std::fmt;
use std::future::Future;
use std::pin::Pin;

use alrust_domain::{Alert, HistoryEntry};
use alrust_engine::Transition;
use serde_json::{Map, Value};
use sqlx::{PgConnection, PgPool};

/// The future a hook phase returns.
pub type HookFuture<'a, T> = Pin<Box<dyn Future<Output = Result<T, HookError>> + Send + 'a>>;

/// A refusal from a hook: the request fails with this HTTP status and
/// message, and nothing is committed.
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

    #[test]
    fn a_database_error_is_a_verdict_and_a_lost_connection_is_not() {
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
