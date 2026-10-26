//! The alrust server: reads its configuration from the environment, connects
//! to PostgreSQL, applies migrations, ensures the bootstrap API key, and
//! serves the HTTP API.
//!
//! Environment:
//! - `DATABASE_URL` (required), e.g. `postgres://alrust:alrust@db:5432/alrust`
//! - `ALRUST_BIND` (default `0.0.0.0:8080`)
//! - `ALRUST_BASE_URL` (default `http://localhost:8080`), used in `href` fields
//! - `ALRUST_AUTH_REQUIRED` (default `true`; `false` skips route permission
//!   checks for requests without a key, which then hold no scopes, as in the
//!   reference; any value other than true/false/1/0/yes/no/on/off is an error)
//! - `ALRUST_ADMIN_KEY`: if set, an admin API key with this value is ensured
//! - `ALRUST_ALLOWED_ENVIRONMENTS` (default `Production,Development`)
//! - `ALRUST_ALERT_TIMEOUT` (default 86400), `ALRUST_ACK_TIMEOUT` (0),
//!   `ALRUST_SHELVE_TIMEOUT` (7200), `ALRUST_HISTORY_LIMIT` (100)
//! - `ALRUST_HOUSEKEEPING_INTERVAL` seconds (default 0 = off; the reference
//!   relies on an external cron calling `/management/housekeeping`)
//! - `RUST_LOG` (default `info`)

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use alrust_api::{ApiConfig, AppState, router};
use alrust_domain::ApiKey;
use alrust_store_pg::{Store, StoreConfig};
use anyhow::Context;
use sqlx::postgres::PgPoolOptions;
use tower_http::cors::CorsLayer;
use tower_http::trace::TraceLayer;

fn env_or(name: &str, default: &str) -> String {
    std::env::var(name)
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| default.to_string())
}

fn env_i64(name: &str, default: i64) -> anyhow::Result<i64> {
    match std::env::var(name) {
        Ok(v) if !v.is_empty() => v
            .parse()
            .with_context(|| format!("{name} must be an integer")),
        _ => Ok(default),
    }
}

/// Strict boolean settings: an unrecognised value stops start-up rather than
/// silently reading as false (a typo must never disable authentication).
#[expect(
    clippy::fn_params_excessive_bools,
    reason = "the bool is the setting's default value, not a mode switch"
)]
fn env_bool(name: &str, default: bool) -> anyhow::Result<bool> {
    match std::env::var(name) {
        Ok(v) if !v.trim().is_empty() => match v.trim().to_lowercase().as_str() {
            "1" | "true" | "yes" | "on" => Ok(true),
            "0" | "false" | "no" | "off" => Ok(false),
            _ => anyhow::bail!("{name} must be true or false, not {v:?}"),
        },
        _ => Ok(default),
    }
}

async fn connect(url: &str) -> anyhow::Result<sqlx::PgPool> {
    let mut delay = Duration::from_millis(500);
    for attempt in 1..=30 {
        match PgPoolOptions::new().max_connections(16).connect(url).await {
            Ok(pool) => return Ok(pool),
            Err(e) if attempt < 30 => {
                tracing::warn!(attempt, error = %e, "database not ready; retrying");
                tokio::time::sleep(delay).await;
                delay = (delay * 2).min(Duration::from_secs(5));
            }
            Err(e) => return Err(e).context("could not connect to the database"),
        }
    }
    unreachable!()
}

async fn shutdown_signal() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let term = async {
        if let Ok(mut s) = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            s.recv().await;
        }
    };
    #[cfg(not(unix))]
    let term = std::future::pending::<()>();
    tokio::select! { () = ctrl_c => {}, () = term => {} }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,sqlx=warn".into()),
        )
        .init();

    let database_url = std::env::var("DATABASE_URL").context("DATABASE_URL is required")?;
    let store_cfg = StoreConfig {
        alert_timeout: env_i64("ALRUST_ALERT_TIMEOUT", 86400)?,
        ack_timeout: env_i64("ALRUST_ACK_TIMEOUT", 0)?,
        shelve_timeout: env_i64("ALRUST_SHELVE_TIMEOUT", 7200)?,
        history_limit: usize::try_from(env_i64("ALRUST_HISTORY_LIMIT", 100)?.max(1))
            .context("ALRUST_HISTORY_LIMIT is too large")?,
        history_on_value_change: env_bool("ALRUST_HISTORY_ON_VALUE_CHANGE", true)?,
    };
    let hostname = std::fs::read_to_string("/etc/hostname")
        .map(|h| h.trim().to_string())
        .unwrap_or_default();
    let api_cfg = ApiConfig {
        base_url: env_or("ALRUST_BASE_URL", "http://localhost:8080")
            .trim_end_matches('/')
            .to_string(),
        auth_required: env_bool("ALRUST_AUTH_REQUIRED", true)?,
        allowed_environments: env_or("ALRUST_ALLOWED_ENVIRONMENTS", "Production,Development")
            .split(',')
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect(),
        default_origin: if hostname.is_empty() {
            "alrust".into()
        } else {
            format!("alrust/{hostname}")
        },
        ..ApiConfig::default()
    };

    let pool = connect(&database_url).await?;
    let store = Store::new(pool, store_cfg);
    store.migrate().await.context("database migration failed")?;
    tracing::info!("database ready");

    if let Ok(key) = std::env::var("ALRUST_ADMIN_KEY") {
        if !key.is_empty() {
            store
                .ensure_key(&ApiKey {
                    id: uuid::Uuid::new_v4().to_string(),
                    key,
                    user: "admin".into(),
                    scopes: vec!["admin".into(), "read".into(), "write".into()],
                    text: "Bootstrap admin key (ALRUST_ADMIN_KEY)".into(),
                    expire_time: chrono::Utc::now() + chrono::Duration::days(3650),
                    count: 0,
                    last_used_time: None,
                    customer: None,
                })
                .await
                .context("could not ensure the bootstrap admin key")?;
            tracing::info!("bootstrap admin key ensured");
        }
    } else if api_cfg.auth_required {
        tracing::warn!(
            "ALRUST_AUTH_REQUIRED is on but ALRUST_ADMIN_KEY is unset; create keys in the database"
        );
    }

    // A zero or negative interval is off (`try_from` fails only for negatives).
    if let Ok(hk_secs) = u64::try_from(env_i64("ALRUST_HOUSEKEEPING_INTERVAL", 0)?)
        && hk_secs > 0
    {
        let store = store.clone();
        let (expired_s, info_s) = (api_cfg.delete_expired_after_s, api_cfg.delete_info_after_s);
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(Duration::from_secs(hk_secs));
            loop {
                tick.tick().await;
                match store.housekeeping(expired_s, info_s).await {
                    Ok(ids) if !ids.is_empty() => {
                        tracing::info!(expired = ids.len(), "housekeeping");
                    }
                    Ok(_) => {}
                    Err(e) => tracing::error!(error = %e, "housekeeping failed"),
                }
            }
        });
    }

    let bind: SocketAddr = env_or("ALRUST_BIND", "0.0.0.0:8080")
        .parse()
        .context("ALRUST_BIND must be host:port")?;
    let app = router(Arc::new(AppState {
        store,
        cfg: api_cfg,
    }))
    .layer(CorsLayer::permissive())
    .layer(TraceLayer::new_for_http());
    let listener = tokio::net::TcpListener::bind(bind)
        .await
        .with_context(|| format!("cannot bind {bind}"))?;
    tracing::info!(%bind, "alrust listening");
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}
