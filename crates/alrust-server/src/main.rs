//! The alrust server: reads its configuration from the environment, connects
//! to PostgreSQL, migrates or adopts the database (ADR-0005 decision 4),
//! ensures the bootstrap API key, and serves the HTTP API.
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
//!   `ALRUST_SHELVE_TIMEOUT` (7200), `ALRUST_HISTORY_LIMIT` (100); each
//!   timeout at most 2147483647, the reference's `integer` columns
//! - `ALRUST_HOUSEKEEPING_INTERVAL` seconds (default 0 = off; the reference
//!   relies on an external cron calling `/management/housekeeping`)
//! - `ALRUST_REQUEST_TIMEOUT` seconds (default 30; 0 = off): a request that
//!   runs longer is answered 503
//! - `ALRUST_DB_ACQUIRE_TIMEOUT` seconds (default 10): the longest wait for a
//!   free database connection
//! - `ALRUST_DB_LOCK_TIMEOUT` (default 10) and `ALRUST_DB_STATEMENT_TIMEOUT`
//!   (default 0) seconds, 0 = off: PostgreSQL's `lock_timeout` and
//!   `statement_timeout` on serving connections; start-up runs without them
//! - `RUST_LOG` (default `info`)
//!
//! Operator Bearer tokens (spec/oidc-caller-facts.md; DECISIONS Y9, Y50,
//! Y51), under the reference's names:
//! - `SECRET_KEY`: the HS256 signing key, used unchanged; if unset, empty or
//!   `changeme`, a random 256-bit key is generated at each start, with a
//!   warning (shorter than 32 characters: used, with a warning)
//! - `OAUTH2_CLIENT_ID`, else `SAML2_ENTITY_ID`, else `ALRUST_BASE_URL`: the
//!   tokens' expected audience
//! - `AUTH_PROVIDER`: the OIDC provider name whose tokens carry hook facts
//!   (unset: none do)

mod bearer_settings;
mod limits;
mod redacted_log;

use std::net::SocketAddr;
use std::process::ExitCode;
use std::sync::Arc;
use std::time::Duration;

use alrust_api::{ApiConfig, AppState, MAX_TIMEOUT, router};
use alrust_domain::{ApiKey, redact_diagnostic};
use alrust_store_pg::{Outcome, Store, StoreConfig};
use anyhow::Context;
use sqlx::postgres::{PgConnectOptions, PgPoolOptions};
use tower_http::cors::CorsLayer;
use tower_http::trace::TraceLayer;

fn env_or(name: &str, default: &str) -> String {
    std::env::var(name)
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| default.to_string())
}

/// A variable that may be set and empty: `None` only when unset. A value
/// that is not UTF-8 stops start-up; the message never contains the value.
fn env_present(name: &str) -> anyhow::Result<Option<String>> {
    match std::env::var(name) {
        Ok(v) => Ok(Some(v)),
        Err(std::env::VarError::NotPresent) => Ok(None),
        Err(std::env::VarError::NotUnicode(_)) => anyhow::bail!("{name} is not valid UTF-8"),
    }
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

/// The fixed id of the `ALRUST_ADMIN_KEY` bootstrap key. Because the id never
/// changes, a new value replaces the previous one, which stops authenticating.
const BOOTSTRAP_KEY_ID: &str = "a1a7b007-5a1e-4b00-8000-000000000001";

/// Connects a pool of at most `max_connections`. With `limits` it is the
/// serving pool; without, the start-up pool, which keeps sqlx's default
/// acquire wait and has no lock or statement limit.
async fn connect(
    url: &str,
    max_connections: u32,
    limits: Option<limits::DbLimits>,
) -> anyhow::Result<sqlx::PgPool> {
    // Pin search_path to the reference objects and TimeZone to UTC on every
    // session (ADR-0005 decisions 4 and 5).
    let mut opts = alrust_store_pg::session_options(
        url.parse::<PgConnectOptions>()
            .context("DATABASE_URL is not a valid PostgreSQL URL")?,
    );
    let mut pool_opts = PgPoolOptions::new().max_connections(max_connections);
    if let Some(limits) = limits {
        opts = limits.session(opts);
        pool_opts = pool_opts.acquire_timeout(limits.acquire);
    }
    let mut delay = Duration::from_millis(500);
    for attempt in 1..=30 {
        match pool_opts.clone().connect_with(opts.clone()).await {
            Ok(pool) => return Ok(pool),
            Err(e) if attempt < 30 => {
                tracing::warn!(attempt, error = %redact_diagnostic(e.to_string()), "database not ready; retrying");
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

/// Runs the server. A fatal error is logged with any restricted evidence
/// redacted (hook spec section 4.2): the error chain can carry database
/// text. `run` installs the log subscriber first.
#[tokio::main]
async fn main() -> ExitCode {
    match run().await {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            tracing::error!(error = %redact_diagnostic(format!("{e:?}")), "alrust stopped");
            ExitCode::FAILURE
        }
    }
}

#[expect(
    clippy::too_many_lines,
    reason = "settings, store preparation and the server are read top to bottom in one place"
)]
async fn run() -> anyhow::Result<()> {
    // As `fmt().init()` did: also forwards `log` records (tracing-log).
    tracing_subscriber::util::SubscriberInitExt::try_init(redacted_log::subscriber(
        "info,sqlx=warn",
        std::io::stdout,
    ))
    .context("could not install the log subscriber")?;

    let database_url = std::env::var("DATABASE_URL").context("DATABASE_URL is required")?;
    let store_cfg = StoreConfig {
        alert_timeout: env_i64("ALRUST_ALERT_TIMEOUT", 86400)?,
        ack_timeout: env_i64("ALRUST_ACK_TIMEOUT", 0)?,
        shelve_timeout: env_i64("ALRUST_SHELVE_TIMEOUT", 7200)?,
        history_limit: usize::try_from(env_i64("ALRUST_HISTORY_LIMIT", 100)?.max(1))
            .context("ALRUST_HISTORY_LIMIT is too large")?,
        history_on_value_change: env_bool("ALRUST_HISTORY_ON_VALUE_CHANGE", true)?,
    };
    for (name, v) in [
        ("ALRUST_ALERT_TIMEOUT", store_cfg.alert_timeout),
        ("ALRUST_ACK_TIMEOUT", store_cfg.ack_timeout),
        ("ALRUST_SHELVE_TIMEOUT", store_cfg.shelve_timeout),
    ] {
        if !(0..=MAX_TIMEOUT).contains(&v) {
            anyhow::bail!("{name} must be between 0 and {MAX_TIMEOUT}, not {v}");
        }
    }
    let hostname =
        std::fs::read_to_string("/etc/hostname").map_or_default(|h| h.trim().to_string());
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
    let api_cfg =
        bearer_settings::install(api_cfg, env_present, bearer_settings::os_random, |w| {
            tracing::warn!("{w}");
        })?;

    let db_limits = limits::DbLimits {
        acquire: Duration::from_secs(limits::secs_in_range(
            "ALRUST_DB_ACQUIRE_TIMEOUT",
            env_i64("ALRUST_DB_ACQUIRE_TIMEOUT", 10)?,
            1,
        )?),
        lock_timeout_s: limits::secs_in_range(
            "ALRUST_DB_LOCK_TIMEOUT",
            env_i64("ALRUST_DB_LOCK_TIMEOUT", 10)?,
            0,
        )?,
        statement_timeout_s: limits::secs_in_range(
            "ALRUST_DB_STATEMENT_TIMEOUT",
            env_i64("ALRUST_DB_STATEMENT_TIMEOUT", 0)?,
            0,
        )?,
    };
    let request_timeout = limits::secs_in_range(
        "ALRUST_REQUEST_TIMEOUT",
        env_i64("ALRUST_REQUEST_TIMEOUT", 30)?,
        0,
    )?;

    // Start-up has its own pool with no lock or statement limit: a migration
    // may run long, or wait for another instance's. It is closed before the
    // serving pool opens.
    let startup_pool = connect(&database_url, 2, None).await?;
    let store = Store::new(startup_pool.clone(), store_cfg.clone());
    let report = store.prepare().await.context("database start-up refused")?;
    match report.outcome {
        Outcome::Fresh => tracing::info!("database initialised with the reference schema"),
        Outcome::Migrated => tracing::info!("database migrations up to date"),
        Outcome::Adopted => tracing::info!("existing reference database adopted"),
    }
    for t in &report.triggers {
        tracing::info!(trigger = %t, "trigger on a reference table");
    }
    if report.inactive_keys > 0 {
        tracing::warn!(
            count = report.inactive_keys,
            "API keys with no expiry or no scopes; they cannot authenticate until edited"
        );
    }

    if let Ok(key) = std::env::var("ALRUST_ADMIN_KEY") {
        if !key.is_empty() {
            store
                .ensure_key(&ApiKey {
                    id: BOOTSTRAP_KEY_ID.to_owned(),
                    key,
                    user: "admin".into(),
                    scopes: Some(vec!["admin".into(), "read".into(), "write".into()]),
                    text: "Bootstrap admin key (ALRUST_ADMIN_KEY)".into(),
                    expire_time: Some(chrono::Utc::now() + chrono::Duration::days(3650)),
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
    drop(store);
    startup_pool.close().await;
    let store = Store::new(
        connect(&database_url, 16, Some(db_limits)).await?,
        store_cfg,
    );

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
                    Ok(done) if done != alrust_store_pg::Housekeeping::default() => {
                        tracing::info!(
                            expired = done.expired.len(),
                            unshelve = done.unshelve.len(),
                            unack = done.unack.len(),
                            "housekeeping"
                        );
                    }
                    Ok(_) => {}
                    Err(e) => {
                        tracing::error!(error = %redact_diagnostic(e.to_string()), "housekeeping failed");
                    }
                }
            }
        });
    }

    let bind: SocketAddr = env_or("ALRUST_BIND", "0.0.0.0:8080")
        .parse()
        .context("ALRUST_BIND must be host:port")?;
    let app = limits::with_deadline(
        router(Arc::new(AppState {
            store,
            cfg: api_cfg,
        })),
        (request_timeout > 0).then(|| Duration::from_secs(request_timeout)),
    )
    .layer(CorsLayer::permissive())
    // The span records the path only: a query string can carry `api-key`.
    .layer(
        TraceLayer::new_for_http().make_span_with(|req: &axum::http::Request<_>| {
            tracing::debug_span!("request", method = %req.method(), path = %req.uri().path())
        }),
    );
    let listener = tokio::net::TcpListener::bind(bind)
        .await
        .with_context(|| format!("cannot bind {bind}"))?;
    tracing::info!(%bind, "alrust listening");
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}
