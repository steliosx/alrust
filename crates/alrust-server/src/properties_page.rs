//! The settings of `GET /management/properties` (`spec/security-fixes.md`
//! SF-MGMT alrust item 1; DECISIONS Y8): alrust's fixed schema, filled from
//! the values start-up has already read, and rendered once by
//! `alrust_api::properties::render`.
//!
//! A setting that is the reference's keeps the reference's name, with the
//! environment variable as its source (`AUTH_REQUIRED` from
//! `ALRUST_AUTH_REQUIRED`); an alrust-only setting keeps its variable's name.
//! `HEARTBEAT_EVENTS` is the heartbeat plugin's setting
//! (`plugins/heartbeat.py:17`). The default origin is not a setting: it is
//! derived from the host name and shown in every alert's `origin`.
//!
//! [`settings`] asks `env` only for the raw strings the typed configurations
//! do not keep ([`RAW_VARIABLES`]); it never lists the process environment.
//! An optional string is `null` when its variable is unset and `""` when it
//! is set and empty.

use alrust_api::ApiConfig;
use alrust_api::properties::{Setting, SettingValue};
use alrust_store_pg::StoreConfig;

/// The variables whose raw text the page needs: the sources of the secret
/// and connection settings, and the optional strings no typed configuration
/// keeps. Sorted.
pub(crate) const RAW_VARIABLES: [&str; 7] = [
    "ALRUST_ADMIN_KEY",
    "ALRUST_BASE_URL",
    "AUTH_PROVIDER",
    "DATABASE_URL",
    "OAUTH2_CLIENT_ID",
    "SAML2_ENTITY_ID",
    "SECRET_KEY",
];

/// The start-up values outside `ApiConfig` and `StoreConfig`.
#[derive(Debug, Clone)]
pub(crate) struct Runtime {
    pub(crate) bind: String,
    pub(crate) housekeeping_interval_s: i64,
    pub(crate) request_timeout_s: u64,
    pub(crate) db_acquire_timeout_s: u64,
    pub(crate) db_lock_timeout_s: u64,
    pub(crate) db_statement_timeout_s: u64,
}

fn int(v: impl TryInto<i64>) -> SettingValue {
    SettingValue::Int(v.try_into().unwrap_or(i64::MAX))
}

/// Every setting of the page. `SECRET_KEY`'s sources are its variable and
/// the effective signing key when that key is text; a generated key is 32
/// random bytes, which count only when they happen to be UTF-8, since no
/// other form of them is ever emitted.
pub(crate) fn settings(
    api: &ApiConfig,
    store: &StoreConfig,
    run: &Runtime,
    env: impl Fn(&str) -> Option<String>,
) -> Vec<Setting> {
    let env = |name: &str| {
        debug_assert!(RAW_VARIABLES.contains(&name), "{name} is not listed");
        env(name)
    };
    let sources = |name: &str| env(name).into_iter().collect::<Vec<_>>();
    let optional = |name: &str| env(name).map_or(SettingValue::Null, SettingValue::Str);
    let mut secret_key = sources("SECRET_KEY");
    if let Some(key) = api
        .bearer
        .as_ref()
        .and_then(|b| String::from_utf8(b.key.clone()).ok())
    {
        secret_key.push(key);
    }
    let mut base_url = sources("ALRUST_BASE_URL");
    base_url.push(api.base_url.clone());
    let database_url = env("DATABASE_URL");
    let saml2_entity_id = env("SAML2_ENTITY_ID");
    vec![
        Setting::public("ACK_TIMEOUT", int(store.ack_timeout)),
        Setting::public("ALERT_TIMEOUT", int(store.alert_timeout)),
        Setting::public(
            "ALLOWED_ENVIRONMENTS",
            SettingValue::List(api.allowed_environments.clone()),
        ),
        Setting::secret("ALRUST_ADMIN_KEY", sources("ALRUST_ADMIN_KEY")),
        Setting::public("ALRUST_BIND", SettingValue::Str(run.bind.clone())),
        Setting::public("ALRUST_DB_ACQUIRE_TIMEOUT", int(run.db_acquire_timeout_s)),
        Setting::public("ALRUST_DB_LOCK_TIMEOUT", int(run.db_lock_timeout_s)),
        Setting::public(
            "ALRUST_DB_STATEMENT_TIMEOUT",
            int(run.db_statement_timeout_s),
        ),
        Setting::public(
            "ALRUST_HOUSEKEEPING_INTERVAL",
            int(run.housekeeping_interval_s),
        ),
        Setting::public("ALRUST_REQUEST_TIMEOUT", int(run.request_timeout_s)),
        Setting::public("AUTH_PROVIDER", optional("AUTH_PROVIDER")),
        Setting::public("AUTH_REQUIRED", SettingValue::Bool(api.auth_required)),
        Setting::connection("BASE_URL", Some(api.base_url.clone()), base_url),
        Setting::connection(
            "DATABASE_URL",
            database_url.clone(),
            database_url.into_iter().collect(),
        ),
        Setting::public("DEFAULT_PAGE_SIZE", int(api.default_page_size)),
        Setting::public("DELETE_EXPIRED_AFTER", int(api.delete_expired_after_s)),
        Setting::public("DELETE_INFO_AFTER", int(api.delete_info_after_s)),
        Setting::public(
            "HEARTBEAT_EVENTS",
            SettingValue::List(api.heartbeat_events.clone()),
        ),
        Setting::public("HEARTBEAT_MAX_LATENCY", int(api.heartbeat_max_latency_ms)),
        Setting::public("HISTORY_LIMIT", int(store.history_limit)),
        Setting::public(
            "HISTORY_ON_VALUE_CHANGE",
            SettingValue::Bool(store.history_on_value_change),
        ),
        Setting::public("OAUTH2_CLIENT_ID", optional("OAUTH2_CLIENT_ID")),
        Setting::connection(
            "SAML2_ENTITY_ID",
            saml2_entity_id.clone(),
            saml2_entity_id.into_iter().collect(),
        ),
        Setting::secret("SECRET_KEY", secret_key),
        Setting::public("SHELVE_TIMEOUT", int(store.shelve_timeout)),
    ]
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;

    use alrust_api::BearerConfig;
    use alrust_api::properties::render;

    use super::*;

    fn run() -> Runtime {
        Runtime {
            bind: "0.0.0.0:8080".into(),
            housekeeping_interval_s: 0,
            request_timeout_s: 30,
            db_acquire_timeout_s: 10,
            db_lock_timeout_s: 10,
            db_statement_timeout_s: 0,
        }
    }

    fn from(vars: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let vars: Vec<(String, String)> = vars
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
            .collect();
        move |name| vars.iter().find(|(k, _)| k == name).map(|(_, v)| v.clone())
    }

    fn with_key(key: &[u8]) -> ApiConfig {
        ApiConfig {
            bearer: Some(BearerConfig {
                key: key.to_vec(),
                audience: "http://alrust.test".into(),
                provider: String::new(),
                now: alrust_api::system_now,
            }),
            ..ApiConfig::default()
        }
    }

    /// Vector 1's environment predicate: only the schema's raw sources are
    /// read, so a variable that is not a setting never reaches the page.
    #[test]
    fn only_the_schema_sources_are_read() {
        let asked = RefCell::new(Vec::new());
        let env = |name: &str| {
            asked.borrow_mut().push(name.to_owned());
            (name == "SYNTHETIC_ENV_SECRET").then(|| "SENTINEL-env-0001".to_owned())
        };
        let page = render(&settings(
            &ApiConfig::default(),
            &StoreConfig::default(),
            &run(),
            env,
        ));
        let mut names = asked.into_inner();
        names.sort();
        names.dedup();
        assert_eq!(names, RAW_VARIABLES.map(str::to_owned).to_vec());
        assert!(!page.contains("SENTINEL") && !page.contains("SYNTHETIC_ENV"));
    }

    /// Vector 8 and the schema: every setting once, sorted, with the
    /// defaults; secrets redacted also when unset.
    #[test]
    fn the_default_page() {
        let env = from(&[(
            "DATABASE_URL",
            "postgres://alrust:SENTINEL-db-0001@db:5432/alrust",
        )]);
        let page = render(&settings(
            &ApiConfig::default(),
            &StoreConfig::default(),
            &run(),
            env,
        ));
        assert_eq!(
            page,
            "ACK_TIMEOUT: 0\n\
             ALERT_TIMEOUT: 86400\n\
             ALLOWED_ENVIRONMENTS: [\"Production\",\"Development\"]\n\
             ALRUST_ADMIN_KEY: [REDACTED]\n\
             ALRUST_BIND: \"0.0.0.0:8080\"\n\
             ALRUST_DB_ACQUIRE_TIMEOUT: 10\n\
             ALRUST_DB_LOCK_TIMEOUT: 10\n\
             ALRUST_DB_STATEMENT_TIMEOUT: 0\n\
             ALRUST_HOUSEKEEPING_INTERVAL: 0\n\
             ALRUST_REQUEST_TIMEOUT: 30\n\
             AUTH_PROVIDER: null\n\
             AUTH_REQUIRED: true\n\
             BASE_URL: \"http://localhost:8080\"\n\
             DATABASE_URL: \"postgres://[REDACTED]@db:5432/[REDACTED]\"\n\
             DEFAULT_PAGE_SIZE: 1000\n\
             DELETE_EXPIRED_AFTER: 7200\n\
             DELETE_INFO_AFTER: 43200\n\
             HEARTBEAT_EVENTS: [\"Heartbeat\"]\n\
             HEARTBEAT_MAX_LATENCY: 2000\n\
             HISTORY_LIMIT: 100\n\
             HISTORY_ON_VALUE_CHANGE: true\n\
             OAUTH2_CLIENT_ID: null\n\
             SAML2_ENTITY_ID: null\n\
             SECRET_KEY: [REDACTED]\n\
             SHELVE_TIMEOUT: 7200\n"
        );
    }

    /// Vector 1: a sentinel in every secret and connection source; the
    /// signing key counts as a source, configured or generated.
    #[test]
    fn sentinels_never_appear() {
        let api = ApiConfig {
            base_url: "http://u:SENTINEL-base-0001@alrust.test".into(),
            ..with_key(b"SENTINEL-signing-key-0001-abcdefgh")
        };
        let env = from(&[
            ("SECRET_KEY", "SENTINEL-secret-0001-abcdefghijklmno"),
            ("ALRUST_ADMIN_KEY", "SENTINEL-admin-0001"),
            (
                "DATABASE_URL",
                "postgres://alrust:SENTINEL-db-0001@db:5432/alrust",
            ),
            ("ALRUST_BASE_URL", "http://u:SENTINEL-base-0001@alrust.test"),
            (
                "AUTH_PROVIDER",
                "keycloak SENTINEL-signing-key-0001-abcdefgh",
            ),
            ("OAUTH2_CLIENT_ID", ""),
        ]);
        let page = render(&settings(&api, &StoreConfig::default(), &run(), env));
        assert!(!page.contains("SENTINEL"), "{page}");
        for want in [
            "SECRET_KEY: [REDACTED]\n",
            "ALRUST_ADMIN_KEY: [REDACTED]\n",
            "BASE_URL: \"http://[REDACTED]@alrust.test\"\n",
            "AUTH_PROVIDER: [REDACTED]\n",
            "OAUTH2_CLIENT_ID: \"\"\n",
        ] {
            assert!(page.contains(want), "{want} not in {page}");
        }
        // A generated key that is not UTF-8 has no text form to compare.
        let page = render(&settings(
            &with_key(&[0xff; 32]),
            &StoreConfig::default(),
            &run(),
            from(&[("AUTH_PROVIDER", "keycloak")]),
        ));
        assert!(page.contains("AUTH_PROVIDER: \"keycloak\"\n"), "{page}");
    }
}
