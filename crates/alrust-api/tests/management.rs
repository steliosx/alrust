//! `GET /management/gtg` through the router when the database does not
//! answer: 503 `FAILED`, as the reference (`management/views.py:127-134`).
//! The pool is lazy and closed, so no database is needed; the `OK` answer is
//! checked against a real database by `scripts/alpha-smoke.py`.

use std::sync::Arc;

use alrust_api::{ApiConfig, AppState, router};
use alrust_store_pg::{Store, StoreConfig};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use sqlx::postgres::{PgConnectOptions, PgPoolOptions};
use tower::ServiceExt as _;

#[tokio::test]
async fn gtg_is_503_failed_when_the_database_does_not_answer() {
    let pool = PgPoolOptions::new().connect_lazy_with(PgConnectOptions::new().host("127.0.0.1"));
    pool.close().await;
    let app = router(Arc::new(AppState {
        store: Store::new(pool, StoreConfig::default()),
        cfg: ApiConfig::default(),
    }));
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/management/gtg")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    let body = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    assert_eq!(&body[..], b"FAILED");
}
