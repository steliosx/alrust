//! Time limits of the serving path: a request deadline, and the serving
//! pool's acquire, lock-wait and statement limits. Start-up (migration,
//! adoption, the bootstrap key) runs on its own pool without them, because a
//! migration may run long or wait for another instance's.

use std::time::Duration;

use alrust_api::ApiError;
use axum::extract::{Request, State};
use axum::http::StatusCode;
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use sqlx::postgres::PgConnectOptions;

/// The largest accepted limit, in seconds (one day).
pub(crate) const MAX_SECS: i64 = 86_400;

/// A limit in whole seconds within `min..=MAX_SECS`.
///
/// # Errors
///
/// A value outside the range; the message names the variable.
pub(crate) fn secs_in_range(name: &str, value: i64, min: u64) -> anyhow::Result<u64> {
    u64::try_from(value)
        .ok()
        .filter(|v| *v >= min && value <= MAX_SECS)
        .ok_or_else(|| {
            anyhow::anyhow!("{name} must be between {min} and {MAX_SECS} seconds, not {value}")
        })
}

/// Limits of the serving pool. A lock or statement limit of zero is off, as
/// in PostgreSQL.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct DbLimits {
    /// The longest wait for a free pool connection.
    pub(crate) acquire: Duration,
    pub(crate) lock_timeout_s: u64,
    pub(crate) statement_timeout_s: u64,
}

impl DbLimits {
    /// Adds `lock_timeout` and `statement_timeout` to the session options
    /// (after the pinned search path and time zone), in milliseconds.
    pub(crate) fn session(self, opts: PgConnectOptions) -> PgConnectOptions {
        opts.options([
            ("lock_timeout", self.lock_timeout_s.saturating_mul(1000)),
            (
                "statement_timeout",
                self.statement_timeout_s.saturating_mul(1000),
            ),
        ])
    }
}

/// Ends a request that outlives `limit` with 503 in the API's error shape.
///
/// The handler is dropped. That schedules a rollback of a transaction it
/// still holds open, but it is not proof that nothing was written: a
/// `COMMIT` already sent may still succeed, and work committed earlier in
/// the request (for example housekeeping batches) stays. A 503 from here
/// means the outcome of a write is unknown, so it is not safe to retry
/// blindly. The query the database is running is not cancelled; with
/// `statement_timeout` at 0 nothing bounds it.
async fn deadline(State(limit): State<Duration>, req: Request, next: Next) -> Response {
    if let Ok(response) = tokio::time::timeout(limit, next.run(req)).await {
        response
    } else {
        tracing::warn!(limit_s = limit.as_secs(), "request timed out");
        ApiError::new(StatusCode::SERVICE_UNAVAILABLE, "request timed out").into_response()
    }
}

/// Applies the request deadline to every route; `None` leaves the router
/// unchanged.
pub(crate) fn with_deadline(app: axum::Router, limit: Option<Duration>) -> axum::Router {
    match limit {
        Some(limit) => app.layer(axum::middleware::from_fn_with_state(limit, deadline)),
        None => app,
    }
}

#[cfg(test)]
mod tests {
    use axum::body::Body;
    use axum::routing::get;
    use tower::ServiceExt as _;

    use super::*;

    #[test]
    fn limits_are_range_checked() {
        assert_eq!(secs_in_range("X", 0, 0).ok(), Some(0));
        assert_eq!(secs_in_range("X", 86_400, 1).ok(), Some(86_400));
        for (value, min) in [(-1, 0), (0, 1), (86_401, 0), (i64::MAX, 0), (i64::MIN, 0)] {
            let e = secs_in_range("ALRUST_X", value, min).expect_err("refused");
            assert!(e.to_string().starts_with("ALRUST_X must be between"), "{e}");
        }
    }

    #[test]
    fn session_limits_follow_the_pinned_options() {
        let limits = DbLimits {
            acquire: Duration::from_secs(10),
            lock_timeout_s: 10,
            statement_timeout_s: 0,
        };
        let opts = limits.session(alrust_store_pg::session_options(PgConnectOptions::new()));
        let got = opts.get_options().expect("options are set");
        assert!(got.starts_with("-c search_path="), "{got}");
        assert!(
            got.ends_with("-c TimeZone=UTC -c lock_timeout=10000 -c statement_timeout=0"),
            "{got}"
        );
    }

    async fn slow() -> &'static str {
        tokio::time::sleep(Duration::from_secs(30)).await;
        "late"
    }

    async fn status_and_body(app: axum::Router) -> (StatusCode, String) {
        let r = app
            .oneshot(
                Request::builder()
                    .uri("/")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        let status = r.status();
        let body = axum::body::to_bytes(r.into_body(), usize::MAX)
            .await
            .expect("body");
        (status, String::from_utf8_lossy(&body).into_owned())
    }

    #[tokio::test]
    async fn a_request_past_the_deadline_is_a_503_in_the_error_shape() {
        let app = axum::Router::new().route("/", get(slow));
        let (status, body) =
            status_and_body(with_deadline(app, Some(Duration::from_millis(20)))).await;
        assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(
            body,
            r#"{"code":503,"errors":null,"message":"request timed out","status":"error"}"#
        );
    }

    #[tokio::test]
    async fn a_request_inside_the_deadline_is_unchanged() {
        let app = || axum::Router::new().route("/", get(|| async { "ok" }));
        for limit in [Some(Duration::from_secs(30)), None] {
            let (status, body) = status_and_body(with_deadline(app(), limit)).await;
            assert_eq!((status, body.as_str()), (StatusCode::OK, "ok"), "{limit:?}");
        }
    }
}
