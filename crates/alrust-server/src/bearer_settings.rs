//! The operator Bearer verifier's settings (spec/oidc-caller-facts.md
//! section 4; `spec/security-fixes.md` SF-MGMT items 2 and 3; DECISIONS Y9).
//!
//! [`resolve`] is pure: `main` passes in the environment values and the
//! random source, so the rules are tested without either. The key is never
//! logged, displayed or put in an error; `BearerConfig`'s `Debug` redacts it.

use alrust_api::{ApiConfig, BearerConfig, system_now};

/// The reference's default `SECRET_KEY` (`alerta/settings.py:15`), treated
/// as unset.
const DEFAULT_SECRET_KEY: &str = "changeme";

/// Below this many characters a configured key is used, with a warning.
const MIN_KEY_CHARS: usize = 32;

/// The values `main` reads from the environment. `None` is an unset
/// variable; `Some("")` is set and empty.
#[derive(Clone, Copy, Default)]
pub(crate) struct Settings<'a> {
    pub(crate) secret_key: Option<&'a str>,
    pub(crate) oauth2_client_id: Option<&'a str>,
    pub(crate) saml2_entity_id: Option<&'a str>,
    pub(crate) auth_provider: Option<&'a str>,
}

impl std::fmt::Debug for Settings<'_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Settings")
            .field("secret_key", &self.secret_key.map(|_| "[REDACTED]"))
            .field("oauth2_client_id", &self.oauth2_client_id)
            .field("saml2_entity_id", &self.saml2_entity_id)
            .field("auth_provider", &self.auth_provider)
            .finish()
    }
}

/// A start-up warning, logged once.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Warning {
    /// No usable `SECRET_KEY`: a random key was generated for this start.
    GeneratedKey,
    /// The configured `SECRET_KEY` has fewer than 32 characters.
    ShortKey,
}

impl Warning {
    pub(crate) fn text(self) -> &'static str {
        match self {
            Warning::GeneratedKey => {
                "SECRET_KEY is unset, empty or the default \"changeme\": a random signing key \
                 was generated for this start. Sessions, e-mail confirmation links and \
                 password-reset links will not survive a restart, and every instance behind \
                 one address needs the same configured SECRET_KEY."
            }
            Warning::ShortKey => {
                "SECRET_KEY is shorter than 32 characters; it is used as configured."
            }
        }
    }
}

/// 256 bits from the operating system's secure random source.
pub(crate) fn os_random() -> Result<[u8; 32], getrandom::Error> {
    let mut key = [0u8; 32];
    getrandom::fill(&mut key)?;
    Ok(key)
}

/// The verifier settings for this start.
///
/// - Key (Y9; SF-MGMT item 2): an unset, empty or exactly `changeme`
///   `SECRET_KEY` gives a key of 256 random bits, generated here once for
///   this start; any other value is used unchanged as its UTF-8 bytes, as
///   `PyJWT`'s `HMACAlgorithm.prepare_key` encodes a string key
///   (`jwt/algorithms.py:325-334`, `jwt/utils.py:16-22`), with a warning
///   below 32 characters. There is no configuration file, so the variable is
///   the only source.
/// - Audience (`alerta/models/token.py:50`): a non-empty `OAUTH2_CLIENT_ID`,
///   else a non-empty `SAML2_ENTITY_ID`, else `base_url`, the server's
///   effective base URL (`ALRUST_BASE_URL` with trailing `/` removed; never
///   the request's host).
/// - Provider (spec section 6, item 3): `AUTH_PROVIDER`, or empty when unset;
///   an empty provider matches no token, so a valid token still
///   authenticates but never carries hook facts.
///
/// # Errors
///
/// When a key must be generated and the random source fails; start-up then
/// stops. The error names no key material.
pub(crate) fn resolve<E: std::error::Error + Send + Sync + 'static>(
    settings: Settings<'_>,
    base_url: &str,
    random: impl FnOnce() -> Result<[u8; 32], E>,
) -> anyhow::Result<(BearerConfig, Vec<Warning>)> {
    let mut warnings = Vec::new();
    let key = match settings.secret_key {
        Some(k) if !k.is_empty() && k != DEFAULT_SECRET_KEY => {
            if k.chars().count() < MIN_KEY_CHARS {
                warnings.push(Warning::ShortKey);
            }
            k.as_bytes().to_vec()
        }
        _ => {
            warnings.push(Warning::GeneratedKey);
            random()
                .map_err(|e| anyhow::anyhow!(e))
                .map_err(|e| e.context("could not generate a random SECRET_KEY"))?
                .to_vec()
        }
    };
    let non_empty = |v: Option<&str>| v.filter(|s| !s.is_empty()).map(str::to_owned);
    let audience = non_empty(settings.oauth2_client_id)
        .or_else(|| non_empty(settings.saml2_entity_id))
        .unwrap_or_else(|| base_url.to_owned());
    let provider = settings.auth_provider.unwrap_or("").to_owned();
    Ok((
        BearerConfig {
            key,
            audience,
            provider,
            now: system_now,
        },
        warnings,
    ))
}

/// The variables [`install`] reads, by their reference names.
pub(crate) const VARIABLES: [&str; 4] = [
    "SECRET_KEY",
    "OAUTH2_CLIENT_ID",
    "SAML2_ENTITY_ID",
    "AUTH_PROVIDER",
];

/// The start-up step `run` uses: reads the four variables through `lookup`
/// (`None` only when unset), resolves them against `cfg.base_url` with
/// `random`, passes each warning's fixed text to `warn`, and returns `cfg`
/// with the verifier installed. Nothing it emits or returns carries the key.
///
/// # Errors
///
/// A failing `lookup` (such as a value that is not UTF-8) or random source.
pub(crate) fn install<E: std::error::Error + Send + Sync + 'static>(
    cfg: ApiConfig,
    lookup: impl Fn(&str) -> anyhow::Result<Option<String>>,
    random: impl FnOnce() -> Result<[u8; 32], E>,
    mut warn: impl FnMut(&'static str),
) -> anyhow::Result<ApiConfig> {
    let [secret_key, oauth2_client_id, saml2_entity_id, auth_provider] = VARIABLES;
    let secret_key = lookup(secret_key)?;
    let oauth2_client_id = lookup(oauth2_client_id)?;
    let saml2_entity_id = lookup(saml2_entity_id)?;
    let auth_provider = lookup(auth_provider)?;
    let (bearer, warnings) = resolve(
        Settings {
            secret_key: secret_key.as_deref(),
            oauth2_client_id: oauth2_client_id.as_deref(),
            saml2_entity_id: saml2_entity_id.as_deref(),
            auth_provider: auth_provider.as_deref(),
        },
        &cfg.base_url,
        random,
    )?;
    for w in warnings {
        warn(w.text());
    }
    Ok(ApiConfig {
        bearer: Some(bearer),
        // GET /config reports the configured client id as is.
        oauth2_client_id: oauth2_client_id.unwrap_or_default(),
        ..cfg
    })
}

#[cfg(test)]
mod tests {
    use std::cell::Cell;

    use super::{Settings, Warning, os_random, resolve};

    const BASE: &str = "https://alrust.example.test";

    #[derive(Debug)]
    struct Failed;

    impl std::fmt::Display for Failed {
        fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            f.write_str("no entropy")
        }
    }

    impl std::error::Error for Failed {}

    #[expect(
        clippy::panic_in_result_fn,
        reason = "a test double: drawing randomness for a configured key is the failure"
    )]
    fn never() -> Result<[u8; 32], Failed> {
        panic!("a configured key must not draw randomness")
    }

    fn with_key(key: Option<&str>) -> Settings<'_> {
        Settings {
            secret_key: key,
            ..Settings::default()
        }
    }

    #[test]
    fn unset_empty_or_default_keys_are_generated_once() {
        for key in [None, Some(""), Some("changeme")] {
            let calls = Cell::new(0);
            let (cfg, warnings) = resolve(with_key(key), BASE, || {
                calls.set(calls.get() + 1);
                Ok::<_, Failed>([7u8; 32])
            })
            .expect("resolved");
            assert_eq!(calls.get(), 1, "{key:?}");
            assert_eq!(cfg.key, vec![7u8; 32], "{key:?}");
            assert_eq!(warnings, vec![Warning::GeneratedKey], "{key:?}");
        }
    }

    #[test]
    fn the_os_source_gives_a_new_key_each_start() {
        let a = resolve(with_key(None), BASE, os_random).expect("a").0.key;
        let b = resolve(with_key(None), BASE, os_random).expect("b").0.key;
        assert_eq!(a.len(), 32);
        assert_ne!(a, b);
    }

    #[test]
    fn a_configured_key_is_used_unchanged() {
        for key in [
            "changeme ",
            " changeme",
            "CHANGEME",
            "  spaced synthetic key with tabs\t ",
            "synthetic-key-0123456789abcdefghij",
        ] {
            let (cfg, _) = resolve(with_key(Some(key)), BASE, never).expect("resolved");
            assert_eq!(cfg.key, key.as_bytes(), "{key:?}");
        }
    }

    #[test]
    fn short_keys_warn_by_characters_not_bytes() {
        let cases = [
            ("a".repeat(31), vec![Warning::ShortKey]),
            ("a".repeat(32), vec![]),
            // 31 two-byte characters are 62 bytes but still short ...
            ("\u{e9}".repeat(31), vec![Warning::ShortKey]),
            // ... and 32 of them are not.
            ("\u{e9}".repeat(32), vec![]),
        ];
        for (key, expected) in cases {
            let (cfg, warnings) = resolve(with_key(Some(&key)), BASE, never).expect("resolved");
            assert_eq!(warnings, expected, "{key:?}");
            assert_eq!(cfg.key, key.as_bytes());
        }
    }

    #[test]
    fn a_failing_random_source_stops_start_up() {
        let e = resolve(with_key(None), BASE, || Err(Failed)).expect_err("refused");
        let text = format!("{e:#}");
        assert!(
            text.contains("could not generate a random SECRET_KEY"),
            "{text}"
        );
        assert!(text.contains("no entropy"), "{text}");
    }

    #[test]
    fn the_warning_texts_are_exact() {
        assert_eq!(
            Warning::GeneratedKey.text(),
            "SECRET_KEY is unset, empty or the default \"changeme\": a random signing key was \
             generated for this start. Sessions, e-mail confirmation links and password-reset \
             links will not survive a restart, and every instance behind one address needs the \
             same configured SECRET_KEY."
        );
        assert_eq!(
            Warning::ShortKey.text(),
            "SECRET_KEY is shorter than 32 characters; it is used as configured."
        );
    }

    #[test]
    fn audience_takes_the_first_non_empty_source() {
        let aud = |oauth: Option<&str>, saml: Option<&str>| {
            let s = Settings {
                secret_key: Some("synthetic-key-0123456789abcdefghij"),
                oauth2_client_id: oauth,
                saml2_entity_id: saml,
                auth_provider: None,
            };
            resolve(s, BASE, never).expect("resolved").0.audience
        };
        assert_eq!(aud(Some("client-1"), Some("entity-1")), "client-1");
        assert_eq!(aud(Some(""), Some("entity-1")), "entity-1");
        assert_eq!(aud(None, Some("entity-1")), "entity-1");
        assert_eq!(aud(Some(""), Some("")), BASE);
        assert_eq!(aud(None, None), BASE);
    }

    #[test]
    fn an_unset_provider_is_empty() {
        let provider = |p: Option<&str>| {
            let s = Settings {
                auth_provider: p,
                ..with_key(Some("synthetic-key-0123456789abcdefghij"))
            };
            resolve(s, BASE, never).expect("resolved").0.provider
        };
        assert_eq!(provider(None), "");
        assert_eq!(provider(Some("")), "");
        assert_eq!(provider(Some("keycloak")), "keycloak");
    }

    /// An operator token for the router test, signed with `key` over its
    /// literal encoded segments.
    fn router_token(key: &[u8], now: i64) -> String {
        use base64::Engine as _;
        use base64::engine::general_purpose::URL_SAFE_NO_PAD;
        use hmac::{Hmac, Mac};

        let input = format!(
            "{}.{}",
            URL_SAFE_NO_PAD.encode(r#"{"alg":"HS256"}"#),
            URL_SAFE_NO_PAD.encode(
                serde_json::json!({
                    "sub": "user-1", "preferred_username": "ops@example.test",
                    "provider": "keycloak-test", "scope": "read", "aud": "client-1",
                    "exp": now + 3600, "alrust_hook_customers": ["acme"],
                })
                .to_string()
            )
        );
        let mut mac = Hmac::<sha2::Sha256>::new_from_slice(key).expect("key");
        mac.update(input.as_bytes());
        format!(
            "{input}.{}",
            URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes())
        )
    }

    /// A synthetic environment: these variables are set, every other unset;
    /// each name read is recorded.
    fn environment<'a>(
        set: &'a [(&'a str, &'a str)],
        read: &'a std::cell::RefCell<Vec<String>>,
    ) -> impl Fn(&str) -> anyhow::Result<Option<String>> + 'a {
        move |name: &str| {
            read.borrow_mut().push(name.to_owned());
            Ok(set
                .iter()
                .find(|(k, _)| *k == name)
                .map(|(_, v)| (*v).to_owned()))
        }
    }

    fn base_cfg(auth_required: AuthRequired) -> alrust_api::ApiConfig {
        alrust_api::ApiConfig {
            base_url: BASE.into(),
            auth_required: matches!(auth_required, AuthRequired::Yes),
            ..alrust_api::ApiConfig::default()
        }
    }

    #[test]
    fn install_reads_the_reference_names_and_installs_the_verifier() {
        let key = "synthetic-install-key-0123456789abc";
        let set = [
            ("SECRET_KEY", key),
            ("OAUTH2_CLIENT_ID", "client-1"),
            ("AUTH_PROVIDER", "keycloak-test"),
            // Other spellings are not read.
            ("ALRUST_SECRET_KEY", "wrong"),
            ("secret_key", "wrong"),
        ];
        let read = std::cell::RefCell::new(Vec::new());
        let mut warned = Vec::new();
        let cfg = super::install(
            base_cfg(AuthRequired::Yes),
            environment(&set, &read),
            never,
            |w| warned.push(w),
        )
        .expect("installed");
        assert_eq!(*read.borrow(), super::VARIABLES.map(str::to_owned));
        assert_eq!(warned, Vec::<&str>::new());
        let bearer = cfg.bearer.as_ref().expect("verifier installed");
        assert_eq!(bearer.key, key.as_bytes());
        assert_eq!(bearer.audience, "client-1");
        assert_eq!(bearer.provider, "keycloak-test");
        assert!(cfg.auth_required);
        assert_eq!(cfg.base_url, BASE);
        assert!(!format!("{cfg:?}").contains(key));
    }

    #[test]
    fn install_emits_each_warning_and_never_the_key() {
        // Unset: a generated key and the full Y9 warning.
        let read = std::cell::RefCell::new(Vec::new());
        let mut warned = Vec::new();
        let cfg = super::install(
            base_cfg(AuthRequired::Yes),
            environment(&[], &read),
            || Ok::<_, Failed>([0x5a; 32]),
            |w| warned.push(w),
        )
        .expect("installed");
        assert_eq!(warned, vec![Warning::GeneratedKey.text()]);
        let bearer = cfg.bearer.as_ref().expect("verifier installed");
        assert_eq!(bearer.key, vec![0x5a; 32]);
        assert_eq!(bearer.audience, BASE);
        assert_eq!(bearer.provider, "");
        let shown = format!("{cfg:?}{warned:?}");
        assert!(!shown.contains("ZZZZ"), "{shown}");

        // A short configured key: used, with the short-key warning only.
        let short = "synthetic-short-key";
        let mut warned = Vec::new();
        let cfg = super::install(
            base_cfg(AuthRequired::No),
            environment(&[("SECRET_KEY", short)], &read),
            never,
            |w| warned.push(w),
        )
        .expect("installed");
        assert_eq!(warned, vec![Warning::ShortKey.text()]);
        assert_eq!(
            cfg.bearer.as_ref().expect("installed").key,
            short.as_bytes()
        );
        assert!(!format!("{cfg:?}{warned:?}").contains(short));
    }

    #[test]
    fn install_stops_on_a_bad_variable_or_random_source() {
        let failing = |name: &str| -> anyhow::Result<Option<String>> {
            anyhow::bail!("{name} is not valid UTF-8")
        };
        let e = super::install(base_cfg(AuthRequired::Yes), failing, never, |_| {})
            .expect_err("refused");
        assert_eq!(e.to_string(), "SECRET_KEY is not valid UTF-8");
        let read = std::cell::RefCell::new(Vec::new());
        let e = super::install(
            base_cfg(AuthRequired::Yes),
            environment(&[], &read),
            || Err(Failed),
            |_| {},
        )
        .expect_err("refused");
        assert!(format!("{e:#}").contains("could not generate a random SECRET_KEY"));
    }

    /// The configuration `install` returns, through the real router with a
    /// never-connected pool: the route refuses on scope before any query.
    #[tokio::test]
    async fn the_installed_verifier_authenticates_through_the_router() {
        use std::sync::Arc;

        use alrust_api::{AppState, router};
        use alrust_store_pg::{Store, StoreConfig};
        use axum::body::Body;
        use axum::http::{Request, StatusCode};
        use tower::ServiceExt as _;

        const NOW: i64 = 1_800_000_000;
        fn fixed_now() -> i64 {
            NOW
        }

        let key = "synthetic-router-key-0123456789abcd";
        let token = router_token(key.as_bytes(), NOW);
        // The same claims signed with another key.
        let forged = router_token(b"another-synthetic-key-0123456789ab", NOW);
        let with_provider = [
            ("SECRET_KEY", key),
            ("OAUTH2_CLIENT_ID", "client-1"),
            ("AUTH_PROVIDER", "keycloak-test"),
        ];
        // No AUTH_PROVIDER: the same token still authenticates, without hook facts.
        let without_provider = [("SECRET_KEY", key), ("OAUTH2_CLIENT_ID", "client-1")];
        let read = std::cell::RefCell::new(Vec::new());
        for auth_required in [AuthRequired::Yes, AuthRequired::No] {
            for set in [&with_provider[..], &without_provider[..]] {
                let mut cfg = super::install(
                    base_cfg(auth_required),
                    environment(set, &read),
                    never,
                    |_| {},
                )
                .expect("installed");
                let bearer = cfg.bearer.as_mut().expect("verifier installed");
                // The resolver wires the system clock; the checks use a fixed one.
                assert!(((bearer.now)() - chrono::Utc::now().timestamp()).abs() <= 5);
                bearer.now = fixed_now;
                let pool = sqlx::postgres::PgPoolOptions::new()
                    .connect_lazy("postgres://unused@127.0.0.1:1/unused")
                    .expect("lazy pool");
                let app = router(Arc::new(AppState {
                    store: Store::new(pool, StoreConfig::default()),
                    cfg,
                }));
                let post = |authorization: String| {
                    Request::builder()
                        .method("POST")
                        .uri("/key")
                        .header("authorization", authorization)
                        .header("content-type", "application/json")
                        .body(Body::from("{}"))
                        .expect("request")
                };
                // The operator is authenticated, and lacks write:keys.
                let r = app
                    .clone()
                    .oneshot(post(format!("Bearer {token}")))
                    .await
                    .expect("response");
                assert_eq!(r.status(), StatusCode::FORBIDDEN, "{auth_required:?}");
                let body = axum::body::to_bytes(r.into_body(), usize::MAX)
                    .await
                    .expect("body");
                assert!(
                    String::from_utf8_lossy(&body).contains("Missing required scope: write:keys"),
                    "{auth_required:?}"
                );
                // A token signed with another key is a 401 in both settings.
                let r = app
                    .clone()
                    .oneshot(post(format!("Bearer {forged}")))
                    .await
                    .expect("response");
                assert_eq!(r.status(), StatusCode::UNAUTHORIZED, "{auth_required:?}");
            }
        }
    }

    #[derive(Debug, Clone, Copy)]
    enum AuthRequired {
        Yes,
        No,
    }

    #[test]
    fn debug_never_prints_the_key() {
        let key = "synthetic-secret-value-for-debug-test";
        let settings = with_key(Some(key));
        assert!(!format!("{settings:?}").contains(key));
        let (cfg, _) = resolve(settings, BASE, never).expect("resolved");
        assert!(!format!("{cfg:?}").contains(key));
        let generated = resolve(with_key(None), BASE, || Ok::<_, Failed>([0x41; 32]))
            .expect("resolved")
            .0;
        assert!(!format!("{generated:?}").contains("AAAAAAAA"));
    }
}
