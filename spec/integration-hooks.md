# Integration hook contract (Zabalack and Voalack)

Status: **draft for review**. No alrust code implements this yet. Decisions
behind it: DECISIONS Y35 (alrust must fully support Zabalack and Voalack;
exact reference storage first) and Y41 (option A: a hook in alrust calls each
integration's existing SQL inside the acknowledgement's own transaction; their
own sessions change their repositories from this spec; Claude opens no PRs
there).

This page states what alrust must provide, what each integration's in-Alerta
Python layer does today, which part each side changes, and the questions that
stay open. Citations are `<repo>@<commit>:<path>:<line>` from read-only reads
of the pinned commits below; they are not re-verified against Alerta itself,
whose source is not in either repository.

## Pinned sources

| Integration | Repository | Commit | In-Alerta Python layer |
|---|---|---|---|
| Zabalack | `steliosx/zab-al-ack` `main` | `eaf2acf0abd919b6f3f0b3e6b146adb81f83e70c` | `src/zabalack/plugins.py` (554 lines), `normalizer.py` (106), `query_guard.py` (246); shared helpers `crypto.py`, `policy.py`, `health.py`, `config.py` |
| Voalack | `steliosx/voalack` `main` | `4d70e69c3b3e2ccf95dd7b196c2cfe7a1de9d982` | `packages/voalack/src/voalack/boundary.py` (925), `intent.py` (344), `envelope.py` (367), `plugin.py` (63), `txn.py`, `schema.py`; `alerta_ack_core` canonical/profile helpers |
| Voalack (coexistence branch) | `steliosx/voalack` `vo/build/coexist-3a` | `e50b2885c05e6dd872bcc53c0e21d98fa710ffa6` | as `main`, plus an alert-class check before auth and SQL guard phases (22 commits ahead, unmerged) |

A later head of either repository needs this page rechecked before alrust code
relies on it.

## 1. What stays unchanged: the SQL side

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
  `voalack.capture_ack()` (`schema.sql:140-167`) looks for an intent staged
  **in the same transaction** (`txid = txid_current()`), and refuses the
  update unless `(NEW.history[1])."user"` equals the staged actor login and
  `NEW.customer` matches the staged customer and an active binding.

Since PR #150 (ADR-0005) alrust stores alerts in the reference layout, with the
`history` composite array newest first. Its acknowledgement path already has
the shape both triggers need (`crates/alrust-store-pg/src/lib.rs`,
`take_action`): one transaction that locks the alert row `FOR UPDATE`, builds
one history entry (`type = 'ack'`, `user` = the caller's login, `text` = the
request text), and writes the row with a single `UPDATE`, then commits. The
triggers, capture functions, workers, reconcilers and Voalack's note projector
(a separate process, `voalack@4d70e69:packages/voalack/src/voalack/projector.py:46-60`)
therefore need no change for alrust. What alrust lacks is the Python layer that
ran inside Alerta's request.

## 2. Prerequisites in alrust

1. **Human logins.** Both integrations count only acknowledgements by an
   authenticated human and refuse API keys: Zabalack's `_human_actor` requires
   `g.user_id` and `g.login` (`zab-al-ack@eaf2acf:src/zabalack/plugins.py:269-281`);
   Voalack's gate refuses with 403 "ACK requires an authenticated user, not an
   API key" (`voalack@4d70e69:packages/voalack/src/voalack/boundary.py:141-147`).
   alrust today authenticates API keys only, so `login` is the key's `user`
   and there is no user id (`crates/alrust-api/src/lib.rs`, `authenticate`).
   **Until alrust implements the login routes specified under Y28, every
   acknowledgement is an API-key acknowledgement, and both hooks must decline
   it exactly as the integrations do today** (Zabalack records a skip, Voalack
   refuses; see sections 4 and 5). The hooks are inert, not broken, before
   logins exist.
2. **Caller facts the hook receives.** For each request: user id, login, the
   verified authentication method (Voalack distinguishes `basic` and
   `bearer:<provider>`, `intent.py:307-324`), scopes, the customer list
   (`g.customers`), whether `AUTH_REQUIRED` and customer views are on, and the
   request id.
3. **Customer views.** Zabalack refuses unless `AUTH_REQUIRED` and
   `CUSTOMER_VIEWS` are both on (`plugins.py:436-437`). alrust has
   `auth_required`; customer filtering follows the key's customer
   (`customer-mapping.md`). The Y28 login work must state alrust's equivalent
   of `CUSTOMER_VIEWS` so the hook can report it.
4. **Transaction shape.** The acknowledgement keeps running in one
   transaction at `READ COMMITTED` (Voalack checks this,
   `boundary.py:156-173`), with the alert locked before any hook call and the
   single history `UPDATE` after it. The hook calls run on the same connection
   and transaction. A hook error rolls the whole acknowledgement back.
5. **Route.** Both integrations act only on `PUT /alert/<id>/action` with
   `action = "ack"` (Zabalack `plugins.py:284-288`, Voalack `boundary.py:88-96`).
   Other routes that change status (`PUT /alert/<id>/status`) call no hook,
   as in Alerta, and the triggers then find no staged intent or marker.

## 3. The hook contract (alrust side)

alrust defines one internal interface for integrations that act on an
acknowledgement. Each registered integration implements:

| Phase | When, inside the acknowledgement transaction | Purpose |
|---|---|---|
| `claims(alert)` | after the alert row is locked | Ownership. Zabalack claims `attributes.source_system = 'zabbix'`; Voalack claims `event = 'VeeamOneAlarm'` with `resource LIKE 'veeamone:_%'` (`schema.sql:64-65`). |
| `gate(alert, caller)` | after `claims`, before the history entry is built | Policy checks and integration locks; may decline (no capture, a recorded skip) or refuse (the request fails, everything rolls back). |
| `stage(alert, caller, text)` | after `gate` | Calls the integration's staging SQL in this transaction; may return replacement text for the history entry. |
| `finalize(alert, entry)` | after alrust's `UPDATE` (so after the trigger ran), before commit | Reads back the final row and completes the integration's own record. |

Rules:

- **At most one owner.** If more than one integration claims an alert, alrust
  refuses the acknowledgement (503) and changes nothing. This replaces
  Zabalack's "exactly one status-change receiver, signer last in the chain"
  checks (`plugins.py:291-295`, `372-390`), which exist only because Alerta's
  plugin chain could reorder or append other plugins. In alrust the order is
  fixed in code and there is no third-party plugin chain, so the hook needs no
  run-time chain inspection.
- **Fail closed.** A hook error, a database error in hook SQL, or a missing
  key refuses the acknowledgement with the integration's own status code, and
  the transaction rolls back.
- **No remote I/O** inside the hook. Both integrations already keep delivery
  in separate workers.
- **No secrets in logs, responses or attributes.** Zabalack's marker and
  Voalack's envelope stay out of logs and JSON (`plugins.py:546`).
- **Unclaimed alerts** run exactly as today.
- **Hook SQL that writes the alert row.** Zabalack's `stage_generation`
  updates `public.alerts.attributes` itself (`008_generation_fence.sql:138-166`).
  Inside one transaction, alrust's own `UPDATE` would otherwise overwrite those
  keys with its in-memory copy, so after any `stage` call alrust re-reads the
  locked row's `attributes` and applies its own changes on top before writing.

## 4. Zabalack adapter

What the Python layer does, and its alrust replacement:

| Today (in Alerta) | Citation (`zab-al-ack@eaf2acf`) | In alrust |
|---|---|---|
| Version gate: `alerta-server == 9.1.0` and `ALARM_MODEL == alerta`, otherwise a silent `skipped_reference_version` skip | `src/zabalack/plugins.py:421-428` | Zabalack's session replaces this with a check that accepts alrust's server identity (to be named in alrust's `/management` or a header; open question Z3). |
| Receive-time normaliser: strips reserved attribute keys and prefixes, validates Zabbix source and ingestion identity (ingestion runs as a mapped API-key login with no user id) | `normalizer.py:16-36`, `57-100` | A receive-time hook in alrust with the same reserved-key list and ingestion mapping. |
| Ack checks in order: route, version, privacy guard, single receiver, `AUTH_REQUIRED` and `CUSTOMER_VIEWS`, canonical id and source, cohort, human actor, `authorize_actor` | `plugins.py:410-454`; `policy.py:96-101` | `gate`: the same checks minus the route, version and chain checks. Skips call the same `project_skip` attribute update. |
| Skip record: sets `zabbix_ack_sync`, `zabbix_ack_sync_reason`, `zabbix_ack_sync_config_revision`, `zabbix_ack_sync_local_at`; removes `zabbix_ack_sync_intent` | `normalizer.py:39-47` | Same attribute writes on the locked row, written by alrust's `UPDATE`. |
| Staging: `SELECT zabalack.stage_generation(alert_id, action_id, intent)` on Alerta's connection, **then its own `commit()`** | `plugins.py:310-319`; SQL `008_generation_fence.sql:138-166` | `stage`: the same call **inside** the acknowledgement transaction, with no separate commit (open question Z1). |
| Signing: canonical bytes `zabalack\0v1\0` plus sorted compact JSON (at most 16,384 bytes), HMAC-SHA256 under `signing_kid` from key files of 32 to 128 bytes | `crypto.py:135-168`, `203-215`; `config.py:28-29` | `stage` computes the same marker in Rust from the same key files (open question X1) and returns the text with `\n[zabalack:v1 action=<uuid> kid=<kid> mac=<hex>]` appended. |
| Marker placement: appended to the acknowledgement text only, never to attributes, logs or JSON; operator text that already contains `[zabalack:` or `[ackline:` is refused with 400, and the text is capped at 8,192 bytes | `plugins.py:536-547` | Same. Note: `docs/sql_api.md:35` says 60,000 bytes; the code says 8,192, and the code is followed. |
| Pending attributes after signing: `zabbix_ack_sync = pending`, `zabbix_ack_sync_reason = awaiting_capture`, `zabbix_ack_sync_action_id`, `zabbix_ack_sync_intent` | `plugins.py:506-509` | Same, in alrust's `UPDATE`. |
| Hiding the reserved intent key from responses and searches (`exclude_intent_from_responses`, `query_guard`) | `plugins.py:60-114`, `214-234`; `query_guard.py` | alrust's alert serialiser and query parser treat `zabbix_ack_sync_intent` as hidden, for every alert, whether or not Zabalack is installed. |
| Log and error-body redaction; reader guard returning 503 when the privacy adapter is not ready | `plugins.py:30-46`, `140-168` | alrust's logging never prints attribute values of reserved keys; the readiness check becomes part of `gate`. |
| Health probe `zabalack.capture_health()` on a separate read-only connection | `health.py:119-130` | Out of the acknowledgement path; a later alrust health extension. |

`finalize` has nothing to do for Zabalack: the trigger completes its record.

## 5. Voalack adapter

| Today (in Alerta) | Citation (`voalack@4d70e69`, under `packages/voalack/src/voalack/`) | In alrust |
|---|---|---|
| Version gate: `alerta.version.__version__ != "9.1.0"` raises at install | `boundary.py:75`, `800-803` | Voalack's session replaces it with an alrust identity check (open question Z3 applies to both). |
| App wrapping: replaces `Alert.find_by_id`, wraps the action view, `finalize_request`, the session interface, before/after/teardown handlers, and swaps the connection for one whose `commit()` is deferred, all to keep the acknowledgement in one transaction | `boundary.py:166-167`, `729-735`, `811-812`, `898-917`; `txn.py:11-29` | Not needed: alrust's acknowledgement is already one transaction and the hook phases run inside it. |
| Gate: human login and user id, else 403; customer-filtered load; `READ COMMITTED`; `SELECT ... FOR UPDATE` on the alert and `voalack.lock_binding(alert_id)`; verified auth type; `voalack.lock_grant(...)`; frozen gate facts | `boundary.py:102`, `141-253`; `intent.py:307-324` | `gate`, using the caller facts of section 2. The customer is the locked row's `customer`, not a caller value (`boundary.py:205`). |
| Flask-specific refusals: unreviewed response callbacks, foreign `request_finished` receivers, audit-trail writers, pinned `flask-cors`/`Flask-Compress` | `boundary.py:77`, `174-184`, `311-314`, `738-752` | No alrust equivalent; Voalack's session drops them for alrust or replaces them with a statement of what alrust guarantees (open question V2). |
| Staging: `voalack.stage_intent(action_id, alert_id, request_id, actor_login, payload, customer, binding_id, binding_revision, issued_at, capture_expires_at, execution_expires_at)`, timestamps as exact ISO strings | `plugin.py:22-54`; SQL `schema.sql:225` | `stage`: the same call in this transaction. The history entry's `user` must equal `actor_login`, or the trigger refuses. |
| Finalisation: reads the final history row, checks it, builds and HMAC-SHA256 signs the version 2 `human_ack_intent` envelope (domain `alerta-ack-core\0intent\0v2\0`, sorted compact UTF-8 JSON) with `VOALACK_ACTIVE_KID` from `VOALACK_SIGNING_KEY_FILES`, then `voalack.approve_intent(action_id, canonical, sha256, kid, mac, final)` | `boundary.py:358-433`, `498-526`; `envelope.py:20-22`, `307-344`; `alerta_ack_core` `profiles.py:47-60`, `canonical.py:16-24`; SQL `schema.sql:255-282` | `finalize`: the same check, envelope and call in Rust (open question X1), before alrust commits. |
| Configuration: deployment id, key files, active kid, allowed auth providers, message-policy revision, capture window (1 to 60 s), execution window (up to 14,400 s); runtime role not superuser, owner or privileged | `intent.py:34-56`; `boundary.py:321-341`, `856-886` | Read from alrust configuration under the same names; the role check runs at alrust start-up when the Voalack adapter is enabled. |
| Notes | Written only by `voalack.project_next` in the separate projector process (`schema.sql:3877-3879`; `projector.py:46-60`) | Unchanged; alrust's `notes` table is the reference's 9-column layout (ADR-0005). |

On `main`, Voalack's gate refuses an API-key acknowledgement with 403
before it looks the alert up, so the response reveals neither the alert's
existence nor its customer (`boundary.py:144-146`, B2). In this contract
`claims` runs after alrust's customer-filtered lock, so an API-key
acknowledgement of an alert Voalack does not own proceeds as today, and one
of an alert it owns is refused only after the lookup. The coexistence branch
below also classifies the alert before authentication. Whether that
difference is acceptable is for Voalack's session (open question V3).

The `vo/build/coexist-3a` branch adds an alert-class check before
authentication and passes non-Voalack alerts through untouched; `claims`
covers that. The branch also tightens the resource check to require at least
one character after `veeamone:`, which this page already uses.

## 6. Changes in the integrations' repositories (their sessions, Y41)

Listed for each session to plan; alrust opens no PRs there.

- Zabalack and Voalack: replace the `9.1.0` checks with an identity check that
  also accepts alrust (Z3); keep the Alerta path for Alerta deployments.
- Zabalack: confirm `stage_generation` is correct without its own commit when
  the acknowledgement rolls back after staging (Z1).
- Both: publish signing test vectors (key, input, expected marker or
  envelope bytes and MAC) so alrust's Rust signing is checked byte for byte
  (X1).
- Voalack: state which Flask-specific refusals have an alrust counterpart (V2).
- Both: an alrust-backed acceptance run of their capture tests once alrust has
  logins; that is their CI, not alrust's.

## 7. Open questions

- **X1 (Codex and the owner): where signing keys live.** This page proposes
  that alrust reads each integration's key files (the same file-based model,
  readable only by alrust's service account) and computes the HMAC in Rust,
  checked against the integrations' test vectors. The alternative is a
  signing helper run by each integration that alrust calls locally; it keeps
  keys out of alrust but adds a process and a failure mode inside the
  acknowledgement transaction. Y41 already places the signing step in the
  adapters; this question is only where the key material sits.
- **Z1 (Zabalack's session):** staging inside the acknowledgement
  transaction instead of Zabalack's own commit
  (`plugins.py:300-319` explains the commit as a workaround for Alerta
  committing tags before attributes), including the attribute re-read in
  section 3.
- **Z3 (both sessions):** how alrust identifies itself to the integrations'
  version checks (a server identity string in `/management/status`, a fixed
  constant, or configuration).
- **V2 (Voalack's session):** see section 5.
- **V3 (Voalack's session):** the B2 order of the API-key refusal against the
  alert lookup; see section 5.
- **C1 (Voalack DESIGN C1, open on their side):** running both integrations
  on one server is designed but untested even on Alerta. alrust's
  at-most-one-owner rule refuses any alert both claim; whether the predicates
  can overlap at all is theirs to confirm.
- **Order of work (no new decision):** logins (Y28) come before either hook
  can capture anything. Until then alrust can ship the hook interface, the
  reserved-key hiding and the receive-time normaliser, which do not depend on
  logins.
