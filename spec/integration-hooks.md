# Integration hook contract (Zabalack and Voalack)

Status: **draft for review**. alrust code implements part of this page so
far. Section 4.2: the restricted attribute is left out of every alert
response, search parameters that could select, sort or group by it are
refused (`crates/alrust-domain/src/lib.rs`, `crates/alrust-api/src/lib.rs`),
and error bodies and log records that may carry restricted evidence are
redacted. Section 3: the hook interface and its phase order inside the
acknowledgement transaction (`crates/alrust-store-pg/src/hooks.rs`,
`take_action_with`), and the receive-time `pre_receive` phase of 4.3
(`receive_with`), tested with synthetic integrations only. The Zabalack
receive policy of 4.3.1 and the actor policy of 4.1.1 are implemented as
pure functions. No adapter is
implemented or registered, so no request calls a hook yet.
Nothing here is a qualification or a deployment result. Decisions behind it: DECISIONS
Y35 (alrust must fully support Zabalack and Voalack; exact reference storage
first) and Y41 (option A: a hook in alrust calls each integration's existing
SQL inside the acknowledgement's own transaction; their own sessions change
their repositories from this spec; Claude opens no PRs there). The auth target
is KICKOFF's and is unchanged by this page (section 2).

This page states what alrust must provide, what each integration's in-Alerta
Python layer does today, which part each side changes, and the questions that
stay open, including the owner decisions it needs. Citations are
`<repo>@<commit>:<path>:<line>` from read-only reads of the pinned commits
below; Alerta's own source is in neither repository, so statements about
Alerta internals are each integration's own, as cited.

## Pinned sources

| Integration | Repository | Commit | In-Alerta Python layer |
|---|---|---|---|
| Zabalack | `steliosx/zab-al-ack` `main` | `eaf2acf0abd919b6f3f0b3e6b146adb81f83e70c` | `src/zabalack/plugins.py` (554 lines), `normalizer.py` (106), `query_guard.py` (246); shared helpers `crypto.py`, `policy.py`, `health.py`, `config.py` |
| Voalack | `steliosx/voalack` `main` | `4d70e69c3b3e2ccf95dd7b196c2cfe7a1de9d982` | `packages/voalack/src/voalack/boundary.py` (925), `intent.py` (344), `envelope.py` (367), `plugin.py` (63), `txn.py`, `schema.py`; `alerta_ack_core` canonical/profile helpers |

**Scope: pinned `main` of both repositories.** Voalack's unmerged
coexistence branch `vo/build/coexist-3a` (`e50b2885c05e6dd872bcc53c0e21d98fa710ffa6`)
is not covered by sections 3 to 5; section 6 lists what it adds as a separate,
pending checklist. A later head of either repository needs this page
rechecked before alrust code relies on it.

## 1. The SQL side: a compatibility objective

Both integrations capture an acknowledgement with an `AFTER UPDATE` trigger on
`public.alerts` that reads the newest history entry as the reference's
composite `history` type:

- Zabalack `zabalack_capture` (`zab-al-ack@eaf2acf:sql/install/10_trigger.sql:10-18`)
  fires when `NEW.status = 'ack'`, `(NEW.history[1]).type = 'ack'`, the
  newest entry's `(update_time, type, text)` changed, and `[zabalack:` occurs
  in the first 65,536 characters of its `text`. It runs
  `zabalack.capture_ack()` (body in `sql/migrations/008_generation_fence.sql:333`
  and following), which checks the signed marker and intent binding against
  `NEW.attributes`, `NEW.customer` and `(NEW.history[1])."user"`.
- Voalack `voalack_capture_ack`
  (`voalack@4d70e69:packages/voalack/src/voalack/sql/schema.sql:169-176`) fires
  when the newest entry changed, its type is `ack` and `NEW.status = 'ack'`.
  `voalack.capture_ack()` (`schema.sql:140-167`) looks for an uncaptured
  intent staged **in the same transaction** (`txid = txid_current()`). If
  there is none it returns `NULL` without raising (`schema.sql:146-151`);
  because the trigger is `AFTER UPDATE`, PostgreSQL ignores that return
  value, so the alert update stands and nothing is captured. Only when an
  intent is found does it refuse the update unless
  `(NEW.history[1])."user"` equals the staged actor login and
  `NEW.customer` matches the staged customer and an active binding.

Neither trigger is a fallback refusal for a missing stage. The SQL side
ignores acknowledgements it has no staged intent or marker for, and refuses
only mismatched staged captures. Refusing an acknowledgement that must not
proceed unstaged, such as one of a bound alert whose source has drifted, is
the application gate's job (sections 3 and 5).

Since PR #150 (ADR-0005) alrust stores alerts in the reference layout, with the
`history` composite array newest first. Its acknowledgement path
(`crates/alrust-store-pg/src/lib.rs`, `take_action`) is one transaction that
locks the alert row `FOR UPDATE`, builds one history entry (`type = 'ack'`,
`user` = the caller's login, `text` = the request text), and writes the row,
attributes included, with a single `UPDATE` before it commits.

**Objective, not a verified result:** that the triggers, capture functions,
workers, reconcilers and Voalack's note projector (a separate process,
`voalack@4d70e69:packages/voalack/src/voalack/projector.py:46-60`) run against
alrust unchanged. The storage layout makes this plausible. It is confirmed
only by each integration's own affected review and its tests against alrust
(section 7), including Z1 below.

## 2. Auth and customer semantics: owner decisions needed

**The approved auth target is KICKOFF's:** API keys for inbound sources (one
per tenant monitoring instance, which sets an alert's `customer`) and OIDC for
NOC operators, with **no customer-views subsystem and no per-customer RBAC**
(`docs/KICKOFF.md`, auth and Phase 3). DECISIONS Y28 set the depth of the
Phase 0 auth-route spec only; it adopts nothing and leaves the owner gate on
auth-contract changes in place. alrust today authenticates API keys only:
`login` is the key's `user` and there is no user id
(`crates/alrust-api/src/lib.rs`, `authenticate`).

What the integrations require of the caller, at the pinned commits:

- **Zabalack:** a human actor with `g.user_id` and `g.login`, not an
  ingestion login (`zab-al-ack@eaf2acf:src/zabalack/plugins.py:269-281`);
  `AUTH_REQUIRED` and `CUSTOMER_VIEWS` both on, else a
  `skipped_actor_policy` skip (`plugins.py:436-437`); and the alert's customer
  in the actor's customer list unless the cohort allows admins
  (`policy.py:96-101`).
- **Voalack:** `g.user_id` and `g.login`, else a uniform 403 "ACK requires an
  authenticated user, not an API key" **before any alert lookup**, for every
  acknowledgement on the server, including nonexistent alerts and alerts
  Voalack does not own (`voalack@4d70e69:packages/voalack/src/voalack/boundary.py:141-149`,
  B2: the response reveals neither existence nor membership); a verified auth
  type `basic` or `bearer:<provider>` (`intent.py:307-324`); and a policy grant
  per user and customer (`voalack.lock_grant`, `boundary.py:226`).

These requirements meet the KICKOFF target at three points, each an alrust
auth-contract question for the **owner** (open questions A1 to A3). This page
does not settle them and imports no customer-views model into alrust:

- **A1, human actors.** Under KICKOFF the only human callers are OIDC
  operators. Until OIDC exists every acknowledgement is an API-key
  acknowledgement, which both integrations decline (Zabalack skips; Voalack
  refuses), so the hooks capture nothing before then. The owner question is
  whether OIDC operator acknowledgements are the intended capture path, and
  which identity fields alrust passes as user id, login and verified auth type
  (for example `bearer:<provider>`).
- **A2, customer membership.** Zabalack's `CUSTOMER_VIEWS` requirement and its
  actor-customer check, and Voalack's per-customer grant, assume operators
  carry a customer list. KICKOFF has no customer views and no per-customer
  RBAC. The owner decides how an operator's customers are defined for
  integration acknowledgements (for example from the OIDC token, from
  configuration, or not at all), and whether that is a change to the auth
  contract.
- **A3, the API-key refusal and lookup order.** With pinned Voalack `main`
  installed, every API-key acknowledgement on the server gets a uniform 403
  before lookup. A design that looks the alert up first and asks the
  integration whether it owns the alert would instead return 404 for an
  absent or customer-filtered id and let API-key acknowledgements of alerts
  Voalack does not own proceed. That changes alrust's observable
  auth, lookup and acceptance contract, so the owner chooses: (a) reproduce
  pinned `main` (when the Voalack adapter is enabled, refuse every API-key
  acknowledgement with the uniform 403 before lookup), or (b) ownership first
  (lookup, then refuse only API-key acknowledgements of owned alerts). The
  unmerged coexistence branch classifies alerts before authentication and so
  behaves closer to (b); it is not pinned `main`. Technical compatibility of
  either choice with Voalack's capture remains Voalack's session's review.

Other prerequisites, not owner-gated:

1. **Caller facts the hook receives**, once A1 and A2 are decided: user id,
   login, verified auth type, scopes, customer list, whether auth is required,
   and the request id.
2. **Transaction shape.** The acknowledgement runs in one transaction at
   `READ COMMITTED` (Voalack checks this, `boundary.py:156-173`), with the
   alert locked before every hook call except `pre_lookup`, which by
   definition runs before the lookup and the lock (so that A3 option (a)'s
   refusal stays uniform), and the single history `UPDATE` after the stage
   phase. Hook calls use the same connection and transaction.
3. **Route.** Both integrations act only on `PUT /alert/<id>/action` with
   `action = "ack"` (Zabalack `plugins.py:284-288`, Voalack `boundary.py:88-96`).
   `PUT /alert/<id>/status` calls no hook and so creates no hook-generated
   marker or staged intent, as in Alerta. Caller text on that route can
   contain marker-like bytes, but its history entry has type `status`, which
   keeps it out of both acknowledgement triggers (each requires
   `(NEW.history[1]).type = 'ack'`).

## 3. The hook contract (alrust side)

alrust defines one internal interface for integrations that act on an
acknowledgement. Each registered integration implements:

| Phase | When, inside the acknowledgement transaction | Purpose |
|---|---|---|
| `pre_receive(caller, incoming)` | on `POST /alert` only, in the receive transaction, for every registered integration, before anything is looked up or written | Section 4.3: change the incoming alert (strip reserved keys) or refuse it. Never called by an action. |
| `pre_lookup(caller)` | before the alert is looked up | Refusals that must not depend on the alert (A3 option (a)). |
| `claims(alert)` | after the alert row is locked | Ownership on pinned `main`. Zabalack claims an alert with `attributes.source_system = 'zabbix'` (`plugins.py:411`). Voalack claims an alert when the protected registry `voalack.bindings` has an active row for its id, **whatever its current event or resource** (`boundary.py:99-109`, `158-165`); an error querying the registry refuses 503 "ACK not processed: capture registry unavailable" (`159-162`), and under the lock the binding is re-read through `voalack.lock_binding` (`185-198`), a binding gone by then leaving the alert unclaimed. Source drift of a bound alert is refused in `gate`, not treated as unclaimed. |
| `gate(alert, caller)` | after `claims`, before the history entry is built | Policy and health checks and integration locks; may decline (no capture, a recorded skip) or refuse (the request fails and everything rolls back). |
| `stage(alert, caller, text)` | after `gate` | Calls the integration's staging SQL; may return replacement text for the history entry. |
| `pre_commit(alert, entry)` | after alrust's `UPDATE` (so after the trigger ran), immediately before `COMMIT` | Final checks and the integration's completing call; any failure rolls back before commit. |
| `commit_outcome(error)` | only when `COMMIT` fails with no server verdict | Reconciles an interrupted commit (section 5). |

Rules:

- **At most one owner.** If more than one integration claims an alert (for
  example an alert with an active Voalack binding whose attributes also say
  `source_system = 'zabbix'`), alrust refuses the acknowledgement (503) and
  changes nothing. This takes the place
  of Zabalack's "exactly one status-change receiver, signer last" checks
  (`plugins.py:291-295`, `372-390`), which exist because Alerta's plugin chain
  could reorder or append plugins; alrust's order is fixed in code.
- **Fail closed.** A hook error, a database error in hook SQL, or a missing
  key refuses the acknowledgement with the integration's own status code, and
  the transaction rolls back before commit.
- **One call per phase per request.** alrust calls each phase exactly once.
- **Re-read after staging.** Zabalack's `stage_generation` itself updates
  `public.alerts.attributes` (`008_generation_fence.sql:138-166`). Inside one
  transaction alrust's own `UPDATE` would otherwise overwrite those keys with
  its in-memory copy, so after any `stage` call alrust re-reads the locked
  row's `attributes` and applies its own changes on top before writing.
- **No remote I/O** inside the hook. Both integrations keep delivery in
  separate workers.
- **Secrets and restricted data** follow section 4.2's privacy rules, which
  apply on every read path, not only here.
- **Unclaimed alerts** run exactly as today, subject to A3. An alert is
  unclaimed by Voalack only when it has no active binding; a bound alert
  whose event, resource or other source facts have drifted stays claimed and
  is refused in `gate` (section 5).

## 4. Zabalack adapter (pinned `main`)

### 4.1 Acknowledgement path

| Today (in Alerta) | Citation (`zab-al-ack@eaf2acf`, `src/zabalack/`) | In alrust |
|---|---|---|
| Version gate: `alerta-server == 9.1.0` and `ALARM_MODEL == alerta`, otherwise a `skipped_reference_version` skip, not a refusal | `plugins.py:421-428` | Zabalack's session replaces it with an identity check that also accepts alrust (Z3). |
| Entry conditions: action `ack`, the alert not already `ack`, `source_system = zabbix`; capture bypass | `plugins.py:410-419` | `claims` and `gate`, unchanged. An already acknowledged alert gets no Zabalack capture: the adapter's `gate` or `claims` must decline it before `stage`. The generic `pre_lookup` and `claims` calls still run, and A3's refusal before lookup stays. |
| Checks in order: privacy readiness, `AUTH_REQUIRED` and `CUSTOMER_VIEWS`, canonical id and source, cohort and one-to-one binding, human actor, `authorize_actor` | `plugins.py:432-454`; `policy.py:96-101` | `gate`, same order and outcomes, subject to A1 and A2. |
| Duplicate call in one request reuses its staged identity | `plugins.py:455-459` | Redundant by construction: alrust calls `stage` once per request. |
| Transition decided before staging: an invalid action stages nothing; Alerta's own refusal propagates; an undecidable transition refuses 503 `zabalack_generation_unavailable`; a transition that does not end in `ack` records `skipped_no_ack_transition` | `plugins.py:460-483` | `gate` runs alrust's engine transition for the same inputs before `stage` and maps each outcome the same way. |
| **Health gate immediately before staging:** `_check_health` probes `zabalack.capture_health()` through a bounded, cached probe (cache seconds, acquire timeout, overall timeout; a 500 ms statement timeout), and refuses 503 `zabalack_capture_unhealthy` when unconfigured, unavailable or unhealthy | `plugins.py:392-408`, called at `484`; `health.py:38`, `50-135` | `gate`, last step before `stage`: the same probe contract on a separate read-only connection with the same bounds and caching, and the same 503. |
| Skip record: sets `zabbix_ack_sync`, `_reason`, `_config_revision`, `_local_at`; removes `zabbix_ack_sync_intent` | `normalizer.py:39-47` | Same attribute writes, carried by alrust's `UPDATE`. |
| Staging: `zabalack.stage_generation(alert_id, action_id, intent)` on Alerta's connection, **then its own commit**; failure refuses 503 | `plugins.py:300-319`, `497-503`; SQL `008_generation_fence.sql:138-166` | `stage`: the same call **inside** the acknowledgement transaction, with no separate commit, then the re-read of section 3 (Z1). |
| Final binding checks before signing: exactly one staged context; adapter enabled and not bypassed; config revision unchanged; the row's `zabbix_ack_sync_intent` equal to the staged intent; actor, customer and the attribute snapshot equal to the intent's; else 503 `zabalack_final_binding_invalid` | `plugins.py:512-529` | `stage`, after the re-read and before signing: all of these checks, against the re-read row. The duplicate-callback branch (`530-535`) is redundant by construction (one call). |
| Signing: canonical bytes `zabalack\0v1\0` plus sorted compact JSON (at most 16,384 bytes), HMAC-SHA256 under `signing_kid` from key files of 32 to 128 bytes | `crypto.py:135-168`, `203-215`; `config.py:28-29` | `stage` computes the same marker in Rust (X1) and returns the text with `\n[zabalack:v1 action=<uuid> kid=<kid> mac=<hex>]` appended. |
| Operator text containing `[zabalack:` or `[ackline:`, or over 8,192 bytes, is refused with 400 | `plugins.py:536-538` | Same. (`docs/sql_api.md:35` says 60,000 bytes; the code says 8,192 and is followed.) |
| Pending attributes after staging: `zabbix_ack_sync = pending`, `_reason = awaiting_capture`, `_action_id`, `zabbix_ack_sync_intent` | `plugins.py:506-509` | Same, carried by alrust's `UPDATE`. |

`pre_commit` and `commit_outcome` have nothing to do for Zabalack: its
trigger completes the record inside the same `UPDATE`.

### 4.1.1 Actor policy port: proposal

Status: the proposal was reviewed and merged (PR #183); it is implemented as
`hooks::zabalack::human_actor`, `authorize_actor` and `actor_gate`, with
`Policy::select_cohort` (`policy.py:87-94`) so that a caller can obtain the
cohort, and unit tests for the cases listed below. Nothing is registered.
It ports only the last
two checks of Zabalack's acknowledgement gate, human-actor extraction and
actor authorisation (`plugins.py:452-454`, using `_human_actor` at
`plugins.py:269-281` and `authorize_actor` at `policy.py:96-101`, at
`eaf2acf`), as pure functions the Zabalack adapter's `gate` will call.
Nothing is registered.

**Scope, delimited.** The gate's earlier checks are not in this slice and
keep their pinned order and outcomes when the adapter's `gate` is written
(`plugins.py:410-451`): the entry conditions, capture disabled, capture
bypass, the route, the reference-version check, privacy readiness (503
`zabalack_privacy_adapter_required`), routing order, `AUTH_REQUIRED` and
`CUSTOMER_VIEWS`, the canonical alert id and source, the cohort, and
ambiguity. This slice assumes the caller has already selected the cohort
(as `select_cohort` does at `plugins.py:445`) and passes it in. The
transition decision, the health probe and staging that follow
(`plugins.py:460` onwards) are also out of scope.

**Inputs, and nothing else:** `CallerFacts`; the alert's customer; the
selected cohort; an injected policy; and an injected `now` for the skip
record. No file, environment variable, key, clock or database. The policy
gains `admin_exceptions`, a list of non-empty strings, checked when it is
built (refusal `admin_exception_invalid`, `config.py:158-159`), and each
cohort keeps `allow_admin`. The policy type is renamed from `ReceivePolicy`
to `Policy` (with `PolicyInput`), since it is Zabalack's settings subset for
both paths. The rename touches only `hooks::zabalack` and the names in 4.3.1;
nothing registers the policy, and no other crate uses it.

**Human-actor extraction** (`plugins.py:269-281`), from `CallerFacts` only:

1. `from_request` must be true, and `user_id` and `login` must be present
   and non-empty. Otherwise there is no actor.
2. A login that has an ingestion mapping is not a human actor.
3. `admin` is true when any one scope is exactly the string `admin` or
   exactly `admin:alerts`; the caller's other scopes are irrelevant, and
   there is no prefix or case-insensitive match (`plugins.py:275-276`). An
   admin whose login is not in `admin_exceptions` is not an actor.
4. The caller's customer list must be present (`customers` is `Some`); an
   empty list is allowed. `None`, which in alrust means every customer, is
   not a list and gives no actor.
5. The actor is the login, the customer list sorted and de-duplicated, the
   admin flag, and the auth type `user`.

**Authorisation** (`policy.py:96-101`): the alert's customer must be
non-empty and the actor's login non-empty. An admin actor is authorised
exactly when the cohort's `allow_admin` is true, whatever its customer
list (no membership check is added). A non-admin actor is authorised
exactly when the alert's customer is in its customer list. An admin
exception lets an admin be an actor; it does not by itself authorise.

**Outcome.** No actor, or an actor not authorised, is the skip
`skipped_actor_policy`: the adapter's `gate` returns `Gate::Skip` with
`project_skip`'s writes (the intent removed by a null; `zabbix_ack_sync`,
`_reason`, `_config_revision`, `_local_at` with the injected `now`), and the
acknowledgement proceeds without capture. It is never a refusal and never a
new 403. An authorised actor is returned for the later staging intent.

**Identity, stated plainly.** These are the facts the hooks will receive
for a verified human operator once OIDC exists (A1, A2; DECISIONS Y44). A
production caller must supply upstream-verified human facts: populated
fields and `from_request` are not credential verification. Today the API
sets `user_id` and `auth_type` to none for every caller (API keys only), so
every current API-key caller that reaches this slice gets
`skipped_actor_policy`; earlier gate checks can decide other outcomes.
This slice evaluates facts; it does not verify a credential, does not turn
an API key into a human, and adds no customer views, RBAC or OIDC mapping.

Tests the code will carry, with synthetic facts, cohorts, policy and a
fixed `now`: facts not from a request; a missing or empty `user_id` or
`login`; an ingestion-mapped login; each admin scope and near lookalikes
(`admin:alert`, `Admin`, `admin:alerts:x`); an admin with and without an
exception; an admin under `allow_admin` true (authorised even with an empty
customer list) and false (skipped even with the customer listed); an
ordinary actor in and not in the customer list; an empty customer list
(extracted, then skipped); `customers` absent (no actor); an empty alert
customer; the actor's customers sorted and de-duplicated; the exact skip
writes; and the constructor refusing an empty `admin_exceptions` entry
(`admin_exception_invalid`).

### 4.2 Privacy on every read path

Zabalack treats one attribute, `zabbix_ack_sync_intent`
(`crypto.py:42`; `query_guard.py:42`), as restricted staging data. An adopted
database can already hold committed intents before alrust handles any new
acknowledgement, so these rules apply on **every** read path, whether or not
the acknowledgement adapter is enabled:

- **Serialisation.** The restricted key is removed from serialised alert
  attributes in every response (alert, list, history), and nothing else is
  removed (`plugins.py:60-114`). alrust's alert serialiser leaves the
  restricted key out of `attributes` and emits history `text` verbatim
  (`AlertBody` in `crates/alrust-domain/src/lib.rs`).
- **The committed proof stays visible.** The signed marker in the
  acknowledgement's history `text` is the committed history proof, which
  Zabalack deliberately keeps in successful history responses (A1 on their
  side, `plugins.py:88-99`). alrust keeps it. Removing it would be a
  divergence needing its own approval, not parity.
- **Queries.** Every query form that can read the restricted key without
  returning it is refused with a stable, content-free 400 ("Query selector is
  not permitted on restricted attributes"), for every caller including
  administrators: keyed attribute searches and `_exists_`, direct attribute
  filters, sorting by it, grouping by the whole `attributes` object, and a
  default field of `attributes` (`query_guard.py:1-35`, `42-50`). Ordinary
  keyed searches on other attributes are unaffected. alrust's query parser
  applies this to whichever of these forms it supports, now and as it grows.
- **Readiness.** Zabalack refuses every reader request with 503 when its
  guard is not installed (`plugins.py:148-168`), because Alerta's query builder
  can be rebound at run time. In alrust the filter and the query refusal are
  compiled into the serialiser and the query parser, with no run-time
  installation step, so there is no unguarded state to refuse. Tests must show
  each read path applies them.
- **Diagnostics.** Log records and error bodies that could carry
  marker-bearing or intent-bearing text are scrubbed (`plugins.py:30-46`,
  `140-146`). The marker is never written to attributes, the alert's own
  `text`, logs or request JSON (`plugins.py:546`); its only home is the
  history entry's `text`.
  In alrust (HOOK-DIAG-1), with Zabalack's test: text carries evidence
  when, ignoring case, it contains `[zabalack:`, `[ackline:` or the
  restricted key's name (`plugins.py:26-27`). Both checks sit at the
  output boundary, not at individual call sites:
  - **Error responses.** A layer on the router reads every 4xx and 5xx
    body, including the plain-text rejections axum writes itself outside
    `ApiError` (a malformed JSON body's message quotes the key path), and
    replaces one that carries evidence with the error body
    `zabalack_restricted_error_redacted`, with the same status code
    (Zabalack's `redact_error_response`, `plugins.py:140-146`). An error
    body over 1 MiB, or one that cannot be read, is replaced the same way
    without being checked (fail closed). Other responses, and error
    bodies without evidence, pass unchanged, including the reference's
    reflection of an invalid API key.
  - **Logs.** Every formatted log record, from alrust or a dependency,
    at any level the filter admits, is written whole or replaced by
    `zabalack_restricted_diagnostic_redacted`. This covers trigger names
    logged at start-up and sqlx's `sqlx::postgres::notice` records, which
    carry PostgreSQL notices and warnings raised by triggers
    (Zabalack's `CaptureLogRedactor`, `plugins.py:30-46`). A record is
    checked both as written and with ANSI escape sequences removed.
  Not covered: a panic message, which Rust's default panic hook prints to
  standard error directly.

### 4.3 Receive path

The normaliser strips reserved attribute keys and prefixes on receive and
validates Zabbix source and ingestion identity (ingestion is a mapped API-key
login with no user id) (`normalizer.py:16-36`, `57-100`). alrust needs a
receive-time hook with the same reserved list and mapping.

In alrust the hook is `pre_receive` (section 3). Like the normaliser, which
runs only where Zabalack is installed, it runs only when an integration is
registered; with none, attributes are stored as received (and the
restricted key stays hidden on read by 4.2). The reserved list is ported as
`hooks::zabalack::strip_reserved` for the adapter to call: the exact keys
`zabbix_ack_intent`, `zabbix_ack_proof`, `ack_intent` and `ack_proof`, and
the case-sensitive key prefixes `zabbix_ack_sync` (no trailing underscore),
`zabalack_` and `ackline_` (`normalizer.py:16-17`, `34-36`). Only top-level
keys are removed; values, nested keys and every other key are unchanged.
Nothing registers it yet. The rest of the normaliser (`normalizer.py:57-100`:
the settings file, ingestion mappings, `AUTH_REQUIRED` and `CUSTOMER_VIEWS`,
the identity policy, the customer, instance and cohort checks, and the skip
records) is adapter work and is deferred; 4.3.1 proposes its port. A
rejection there maps to the reference's 403 for `RejectException`
(`reference/alerta/alerta/views/alerts.py:57-60`). Registration (the adapter
is present) is not Zabalack's `settings.enabled` (capture enablement): a
registered adapter strips every incoming alert, Zabbix or not and with
capture on or off, because the normaliser strips before it looks at
`source_system` or `settings.enabled` (`normalizer.py:58-63`).
In alrust the `pre_receive` call comes after the API's own environment and
service policy and its Heartbeat conversion (the store is called only after
those). In 9.1.0 the plugin order decides whether the normaliser sees a
Heartbeat event. `PUT /alert/<id>/attributes` and the tag routes have no
receive hook, in Alerta or here.

### 4.3.1 Receive policy port: proposal

Status: the proposal was reviewed and merged (PR #180); it is implemented as
`hooks::zabalack::pre_receive` and `Policy` (named `ReceivePolicy` until
4.1.1 added the actor fields)
(`crates/alrust-store-pg/src/hooks/zabalack.rs`), with unit tests for the
cases listed below. It ports `normalizer.py:57-100` at `eaf2acf` as one pure
function, which the Zabalack adapter's `pre_receive` will call once that
adapter exists. Nothing is registered. `CallerFacts.from_request` is set by
the API for both routes and consulted by this path's `pre_receive` and by
4.1.1's `human_actor`.

Inputs, and nothing else: the incoming alert, the caller's `CallerFacts`,
an injected `Policy`, and an injected current time (`now`, UTC), used
only for the skip record's timestamp, so that evaluation is deterministic.
The function reads no file, environment variable, key, clock or database,
and performs no I/O. `Policy` holds, for this path,
the settings fields this path uses (`config.py:15-62`): `enabled`,
`capture_bypass`, `bypass_change_ref`, `config_revision`,
`ingestion_mappings` (login to `{customer, instance}`) and `cohorts`. It is
built only through a constructor that applies the pinned checks for those
fields: each mapping's login is a non-empty string and the mapping has
exactly `customer` and `instance`, both non-empty strings
(`config.py:153-157`); each cohort passes `validate_cohort`
(`policy.py:63-85`) and no two share (customer, instance, trigger)
(`config.py:162-168`); `capture_bypass` needs a change reference
(`config.py:145-146`). The flags are typed booleans and the revision and
change reference typed strings, so the pinned boolean check
(`config.py:121-123`) holds by construction. This is the subset of
`validate_settings` these fields need, not full deployment or configuration
validation, and an injected policy has not passed Zabalack's own loader.
An invalid policy is refused when it is built, not per request. Where the
policy comes from (a configuration file, its path, its permissions), and the
errors a future loader can raise, belong to registration and are deferred.

Order, as pinned (`normalizer.py:57-100`):

1. Strip reserved keys (4.3), for every alert.
2. `source_system` not `zabbix`: done, alert unchanged otherwise.
3. Capture disabled (`enabled` false): skip `skipped_disabled`.
3a. Request context (`normalizer.py:64-65`): `Store::receive` and
   `receive_with` are public and can be called outside HTTP, so the hook
   cannot assume a request. `CallerFacts` gains `from_request`, true only
   when the API builds the facts from the request it is serving (the same
   facts it authenticated); every other caller, including the legacy
   `Store::receive`, gets false. False refuses with 403
   `zabbix_ingestion_requires_authenticated_context`. No identity or
   context is inferred from other fields.
4. Tenant gate (`normalizer.py:68-70`): Zabalack requires Alerta's
   `AUTH_REQUIRED` and `CUSTOMER_VIEWS`. Proposed mapping:
   `AUTH_REQUIRED` is `CallerFacts.auth_required`. `CUSTOMER_VIEWS` has no
   alrust switch: alrust can bind a customer to an API key and then always
   scopes that key to it; binding is optional today (no global customer
   views, no per-customer RBAC; KICKOFF, DECISIONS Y44 A2). So the
   `CUSTOMER_VIEWS` half is treated as satisfied only by a customer-bound
   API key, which step 6 enforces (an unbound key is refused there).
   Refusal: 403
   `zabbix_ingestion_tenant_gate` when `auth_required` is false. This adds
   no customer views and changes no authentication; it evaluates existing
   caller facts. It sits next to the owner's auth-contract gate, so the
   mapping is stated here for review rather than assumed.
5. Identity policy (`normalizer.py:66-67`, `71-73`): the caller's login must
   have an ingestion mapping, `user_id` must be absent (an API key, not a
   human), and the scopes must contain neither `admin` nor `admin:alerts`.
   Otherwise 403 `zabbix_ingestion_identity_policy`.
6. Customer (`normalizer.py:74-77`): the alert's customer must be
   non-empty, equal the mapping's customer, and be in the caller's customer
   list; a caller with no customer binding (`customers` absent, meaning
   every customer in alrust) fails this. Otherwise 403
   `zabbix_ingestion_customer_mismatch`.
7. Any of `source_contract_version`, `zabbix_instance`, `zabbix_event_id`,
   `zabbix_trigger_id` missing or falsy: skip `legacy_missing_mapping`.
8. `zabbix_instance` not the mapping's instance: 403
   `zabbix_ingestion_instance_mismatch`.
9. `validate_source` (`policy.py:49-61`) fails for any reason: skip
   `legacy_missing_mapping`. It needs all seven source-contract keys
   present (`policy.py:26`, `50-51`): `source_system`, `zabbix_instance`,
   `zabbix_event_id`, `zabbix_trigger_id`, `zabbix_cause_event_id`,
   `zabbix_recovery_event_id` and `source_contract_version`, so a **missing**
   cause or recovery key is a skip, not a null. A **present** cause or
   recovery value that is one of Python's zero sentinels becomes null
   (`policy.py:33-35`): null, `""`, `"0"`, and, because Python compares them
   equal to `0`, any JSON number equal to zero (`0`, `0.0`, `-0.0`) and the
   JSON `false` (but not `true`, `"0.0"` or `" 0"`). alrust keeps that exact
   parity. Any other value must be a decimal id string
   (`policy.py:36-37`), else the skip. On success the normalised values are
   written back.
10. No enabled cohort for (customer, instance, trigger)
    (`policy.py:87-94`): skip `skipped_cohort_policy`.
11. The cohort is not one-to-one, or any of `zabbix_event_ids`,
    `zabbix_trigger_ids`, `source_members` is present: skip
    `skipped_ambiguous_source`.
12. `capture_bypass`: skip `skipped_capture_bypass` and set
    `zabbix_ack_sync_change_reference` to the change reference. Otherwise
    the alert passes unchanged.

A skip is Zabalack's `project_skip` (`normalizer.py:39-47`), and the
receive proceeds: remove `zabbix_ack_sync_intent`, then set
`zabbix_ack_sync = skipped`, `zabbix_ack_sync_reason`,
`zabbix_ack_sync_config_revision` and `zabbix_ack_sync_local_at` (the
injected `now`, ISO 8601 with microseconds and `+00:00`, as Python's
`isoformat(timespec="microseconds")` writes it). A refusal is a
`HookError` with status 403 and the reason string as its message, and
nothing is stored. Zabalack's `zabalack_configuration_invalid` (503) does not arise per
request here, because the injected policy is validated when built; a future
configuration loader's own errors belong to registration. Refusals (403),
that configuration error (503) and skips (the receive proceeds, with the
skip record) stay distinct.

Malformed-policy parity, disclosed rather than claimed: in a cohort's
`host_ids` or `group_ids`, Python's `len(set(values))` raises `TypeError` for
an unhashable member (a crash, not a `PolicyError`), and treats `1`, `true`
and `1.0` as one value, so `[1, true]` or `[1, 1.0]` is
`cohort_host_group_bound`. The port de-duplicates by JSON text and then
refuses any non-string member as `invalid_source_id`. Both refuse every such
policy, and valid string-id lists agree.

Tests the code will carry, with synthetic policies, caller facts and a
fixed `now` only: a non-Zabbix alert (stripped, otherwise unchanged);
capture disabled (strip and `skipped_disabled`); facts not from a request;
each refusal above; each skip; a missing cause or recovery key (skip)
against a present null, `""`, `"0"`, `0`, `0.0`, `-0.0` or `false` (null)
and `true` or `"0.0"` (skip); a
passing alert with normalised source values; capture bypass; constructor
refusals (empty login, extra mapping key, bad cohort, duplicate cohort,
bypass without a change reference); and stripping of reserved keys a
producer supplied, also when the alert is then skipped.

## 5. Voalack adapter (pinned `main`)

| Today (in Alerta) | Citation (`voalack@4d70e69`, under `packages/voalack/src/voalack/`) | In alrust |
|---|---|---|
| Version gate: `alerta.version.__version__ != "9.1.0"` raises at install | `boundary.py:75`, `800-803` | Voalack's session replaces it with an identity check that also accepts alrust (Z3). |
| App wrapping: replaces `Alert.find_by_id`, wraps the action view, `finalize_request` and the session interface, adds before, after and teardown handlers, and defers Alerta's commits, to keep the acknowledgement in one transaction that commits only at the request boundary | `boundary.py:166-167`, `729-735`, `811-812`, `898-917`; `txn.py:11-29` | Not needed for the transaction itself: alrust's acknowledgement is already one transaction. The commit-time checks and the commit outcome it implemented are carried below. |
| API-key refusal before lookup (uniform 403, B2) | `boundary.py:141-149` | `pre_lookup` or `gate`, per the owner's A3 choice. |
| Gate: customer-filtered load; the protected binding lookup (section 3, `claims`); `READ COMMITTED`; the alert `FOR UPDATE`, then `voalack.lock_binding(alert_id)`, and a complete reload under the lock; the reloaded alert checked against the binding revision by `source_mismatch` (resource, event, environment and origin; the Veeam ONE class; customer; combination and capture ids; active state and severity; snapshot digest), else 503 "ACK not processed: the alert no longer matches its approved binding"; refusal of unreviewed response callbacks and foreign `request_finished` receivers; verified auth type; `voalack.lock_grant(...)`; frozen gate facts | `boundary.py:99-109`, `148-253` (source check at `217-220`); `intent.py:67-89`, `307-324` | `gate`, with A1 and A2's caller facts. The customer is the locked row's `customer`, not a caller value (`boundary.py:205`). Flask-only refusals: V2. |
| Staging: `voalack.stage_intent(action_id, alert_id, request_id, actor_login, payload, customer, binding_id, binding_revision, issued_at, capture_expires_at, execution_expires_at)`, timestamps as the exact signed ISO strings | `plugin.py:22-54`; SQL `schema.sql:225` | `stage`, same call. The history entry's `user` must equal `actor_login`, or the trigger refuses. |
| Finalisation after the update: joins the staged intent, its capture and the final history head; builds and signs the version 2 `human_ack_intent` envelope (domain `alerta-ack-core\0intent\0v2\0`, sorted compact UTF-8 JSON, HMAC-SHA256 under `VOALACK_ACTIVE_KID`); `voalack.approve_intent(action_id, canonical, sha256, kid, mac, final)` | `boundary.py:358-433`, `498-526`; `envelope.py:20-22`, `307-344`; `alerta_ack_core` `profiles.py:47-60`, `canonical.py:16-24`; SQL `schema.sql:255-282` | `pre_commit`, first part. |
| **Commit-time verification, immediately before `COMMIT`**, reached through `_matches_final_history(require_finalized_note=True)`: the stored canonical bytes hash to the stored digest, which equals the capture's; the MAC verifies under the frozen kid; the alert reloaded in this transaction still matches the binding revision (`_source_holds`); the envelope rebuilt from the frozen gate facts, the current revision, the generation and the final note equals the signed one, byte for byte; `voalack.lock_grant` returns the frozen policy revision; and `issued_at <= clock_timestamp() < capture_expires_at` on the database clock (exclusive expiry). Any failure rolls back before commit and the response becomes an error ("ACK not saved: response or history changed before commit") | `boundary.py:436-495`, `591-621` (check at `602`) | `pre_commit`, second part: every one of these invariants, after `approve_intent` and immediately before alrust's `COMMIT`. `approve_intent` checks only digest syntax and linkage and stores the proof (`schema.sql:255-282`); it does not enforce the authorisation or the deadline. Example the checks catch: staging inside the window, a stall past `capture_expires_at`, then signing and approval; pinned `main` refuses this, and so must alrust. |
| **Commit outcome:** if `COMMIT` fails with no server verdict (no SQLSTATE; the connection was lost), a fresh connection with a 5 s statement timeout looks for the action's row in `voalack.captures`. Found: the acknowledgement was saved, and the response is the success. Not found, or the database unreachable: 503 "ACK outcome unknown: check the alert before retrying" with `outcome = unknown` and the `action_id`, never "saved" or "not saved". No automatic retry | `boundary.py:602-607`, `624-654` | `commit_outcome`, same contract. alrust never retries the acknowledgement automatically and never reports "not saved" merely because `COMMIT` returned an error. |
| Configuration: deployment id, key files, active kid, allowed auth providers, message-policy revision, capture window (1 to 60 s), execution window (up to 14,400 s); the runtime role is not superuser, owner or privileged | `intent.py:34-56`; `boundary.py:321-341`, `856-886` | Read from alrust configuration under the same names; the role check runs at alrust start-up when the Voalack adapter is enabled. |
| Notes | Written only by `voalack.project_next` in the separate projector process (`schema.sql:3877-3879`; `projector.py:46-60`) | Unchanged; alrust's `notes` table is the reference's 9-column layout (ADR-0005). |

## 6. Voalack coexistence branch: separate, pending

`vo/build/coexist-3a` at `e50b2885` is unmerged and outside the scope above.
`claims` alone does not preserve its guarantees. If Voalack merges it, this
page needs an affected revision covering at least:

- the alert-class check before authentication, with non-Voalack alerts passed
  through untouched, and the class rechecked under the lock (503 "ACK not
  processed: alert changed, try again"; `voalack@e50b288:packages/voalack/src/voalack/boundary.py:207-209`);
- refusal before frozen facts or staging when the binding reports
  `protections_intact = false` (503) or `lifetime_current = false` (503)
  (`boundary.py:210-218` on the branch), which `lock_binding` alone does not
  enforce;
- the guard SQL phases and lifetime preparation it adds (`guard_pre.sql`,
  `guard_expected.sql`, `guard_phases.sql`, `guard_admit.sql`,
  `guard_post.sql`, `lifetime_prepare.sql`);
- its stricter resource prefix (at least one character after `veeamone:`).

## 7. Changes in the integrations' repositories (their sessions, Y41)

Listed for each session to plan. No external repository change is assigned or
authorised by this page, and alrust opens no PRs there.

- Both: replace the `9.1.0` checks with an identity check that also accepts
  alrust (Z3); keep the Alerta path for Alerta deployments.
- Zabalack: review staging inside the acknowledgement transaction (Z1).
- Both: publish signing test vectors made with clearly synthetic test keys
  (key, input, expected marker or envelope bytes and MAC), never production or
  evidence keys, so alrust's Rust signing is checked byte for byte (X1).
- Voalack: state which Flask-specific refusals have an alrust counterpart (V2).
- Both: an alrust-backed run of their capture tests once alrust has human
  operators (A1); that is their CI, not alrust's.

## 8. Open questions

- **A1, A2, A3 (owner): auth and customer semantics.** See section 2.
- **X1 (owner): where signing keys live.** Codex's design view (`2d3adb3b`):
  prefer native Rust signing in the adapter under the existing file-based
  service-principal custody model, because it adds no subprocess lifetime or
  failure handling inside the locked transaction; it needs the owner's
  approval of the exact key placement and access boundary, and byte-for-byte
  vectors made with synthetic keys. If the owner requires that the alrust
  process never hold these keys, use a separately reviewed local signer
  contract instead. Unresolved until the owner decides a concrete custody
  proposal; nothing here reads, mounts or grants key files.
- **Z1 (Zabalack's session; pending):** staging inside the acknowledgement
  transaction instead of Zabalack's own commit (`plugins.py:300-319` explains
  the commit as a workaround for Alerta committing tags before attributes).
  alrust's transaction shape and the attribute re-read are supported by the
  sources, but atomic rollback of a staged generation and capture, lock
  ordering, and interactions with Zabalack's existing writers and workers
  need Zabalack's affected review and later tests.
- **Z3 (both sessions):** how alrust identifies itself to the integrations'
  version checks (a server identity string in `/management/status`, a fixed
  constant, or configuration).
- **V2 (Voalack's session):** which Flask-only refusals (response callbacks,
  `request_finished` receivers, audit-trail writers, pinned `flask-cors` and
  `Flask-Compress`; `boundary.py:77`, `174-184`, `311-314`, `738-752`) have an
  alrust counterpart, and the statement of what alrust guarantees instead.
- **C1 (Voalack DESIGN C1, open on their side):** running both integrations
  on one server is designed but untested even on Alerta. alrust's
  at-most-one-owner rule refuses any alert both claim; whether the predicates
  can overlap is theirs to confirm.
