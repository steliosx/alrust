# Reference config read candidate

This is one Phase 0 reference-only GET `/config` using an existing labeled
synthetic unscoped-admin fixture. It creates no object and closes its client on
success or failure. HTTP is NOT_RUN until this exact harness, the future runner
and a fresh signed laptop window are separately reviewed and acknowledged.
Offline synthetic responses are harness evidence, not executed server behavior.

Source: Alerta `f4312727783dad64198b62ed778bbca98a5144a3`.
`alerta/views/config.py:9-74` is the complete view; whole-file SHA-256 is
`e76815b75e1ba22b14a2e0d20fcd47b5edf2e596a3f69c16b2bfed3ee20c7c44`.
The 36 top-level keys and nested keysets come from its direct `jsonify` object.
Top-level severity/colors aliases are at lines 42-43. Model selector `ALERTA`
loads `models/alarms/alerta.py`; lines 81-82 set display name `Alerta 9.1.0`
using `management/views.py:19` and `version.py:1`. This incorporates peer plan
review CP-C1/C2; the earlier selector-as-display-name expectation is discarded.

The runner must observe actual factory configuration at the pinned source and
write a private regular attestation before any HTTP call. Required exact JSON
members: `project: alrust`, `fixture: SYNTHETIC`, `reference_head` (above),
`target: http://127.0.0.1:18081`, and `effective_config` with exactly
`BASE_URL: ""`, `AUTH_REQUIRED: false`, `CUSTOMER_VIEWS: false`, `DEBUG: false`,
`ALARM_MODEL: ALERTA`, `ALERT_TIMEOUT: 86400`, `ACK_TIMEOUT: 0`.
Set `ALRUST_CONFIG_ATTESTATION_FILE` to that file and use the existing private
`ALRUST_SYNTHETIC_KEYS_FILE` plus matching `ALRUST_TARGET`. File validation is
a preflight, not proof that a server obeys an authored JSON declaration. Actual
runner source/config/instance and stored-fixture attestation remain prerequisites.
In particular, nonempty BASE_URL fails before GET; endpoint must equal the exact
scheme, host and port requested, without a trailing slash, never a localhost alias.

Compare original JSON in memory; normalize **no fields** and export **no bodies**,
URLs, configuration values, headers, credentials or exception representations.
Only fixed-case completion metadata is returned. Require the controlled flags,
display name and alert/ack timeout values; compare severity/colors aliases. Other
provider, signup, readonly, OAuth URLs, tracking and preference values remain
unasserted. No auth-provider flow, anonymous probe, auth/key mutation, direct SQL,
management endpoint, production configuration or Rust parity claim follows.
The view lacks a permission decorator; the admin fixture does not make this an
authorization test (peer CP-N1).

For later reviewed execution use the hash-installed environment with
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, clear inherited `PYTEST_ADDOPTS` and ALRUST
variables before setting the bounded prerequisites, and select `config-pytest.ini`.
It pins plain assertions, line-only tracebacks and no cache. Do not add `-rP`,
show-locals or body capture. Before sharing, scan every complete eligible artifact
against all actual private fixture keys and the generic synthetic-key pattern;
only send after that scan exits successfully. Keep private raw evidence outside
Git/mailbox. Local evidence never satisfies hosted CI, currently billing-blocked.
