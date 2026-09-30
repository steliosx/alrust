# Status

_Last updated: 2026-09-30T07:10Z by claude-alrust, the sole writer of this
file and DECISIONS.md. Codex sends corrections through the mailbox._

## Current phase

**Phase 0 in progress. The gate is open.**

| Phase | State | Gate |
|---|---|---|
| 0 Spec + conformance | Lifecycle (receive, actions, housekeeping, heartbeats) and customer-mapping specs source-derived and reviewed. Separate scoped reference HTTP runs: smoke 2, customer 4, operator 5, metadata 2, heartbeat 3, heartbeat input/query 4, metadata mutations 3, alert queries 3, HF7 scaffold smoke 2 (each HTTP LOCAL_PASS at its own head and window; the first metadata-mutations attempt FAILED on a harness precondition, HF9, and is preserved). OpenAPI: heartbeat, single-alert, alert-query, `q=` (reviewed), notes and blackouts (under review); keys, users, customers and management outstanding | **Open.** These are separate scoped runs, not a full-suite pass. Hosted CI has never executed. OpenAPI extraction and broader vectors are outstanding |
| 1 Domain/engine/store | blocked on 0 | — |
| 2 HTTP API | blocked on 1 | — |
| 3 Auth | blocked on 2 | — |
| 4 Receivers | blocked on 3 (see PIN inputs) | — |
| 5 Ops | blocked on 4 (see PIN inputs) | — |
| 6 Tauri desktop | blocked on 2 (see PIN inputs) | — |

## Work in flight

Each disposition is bound to the exact head listed. A new head needs its own
review.

| Item | Author | Head | Disposition |
|---|---|---|---|
| PR #1 (`claude/scaffold`) | claude | `e839ceaf8cb43d6ed996c8fe12f884d0bb141743` | Codex: **guard correction APPROVE at `e839ceaf`**. The collaboration-doc reconciliation is a separate head (`claude/charter-sync`) pending its own review and integration. Historical: REQUEST_CHANGES at `a674546648c9ea6fd40e9915b23137b8cfaad150`. Hosted gate blocked. Draft; the `main` merge is the owner's |
| `codex/conformance-harness` | codex | `9d70dc2e4e09c95901d1f8be2096e6ec2effa2bc` | Claude: **APPROVE** for the reference-only HTTP smoke and offline scopes (the basis of window 03) |
| `codex/receive-clock-correction` | codex | `db06ce44f1fdbc3b0dbeb66b6f2952eb0d702071` | Claude: **APPROVE** for the offline `receive-clock-v1` scope (controlled fixture) and the reference-only HTTP smoke at this head. That smoke **completed** in window 04 (LOCAL_PASS). Any future heavy job needs a fresh signed window. No two-server run has been requested; there is no candidate server yet |
| `codex/synthetic-key-preflight` HF-5 (on `db06ce44`) | codex | `e91dcabea1291b6540bc4d97636e6eef8df2bef0` | Claude: **APPROVE** for the differential-CLI credential contract (`SYNTHETIC-` plus 48 hex) |
| `codex/hash-locked-harness` HF-4/HF-8 (on `e91dcabe`) | codex | `130145ea7b0f0b7502043c19b953e38da3e47a59` | Claude: **APPROVE**. Hash-locked wheels reproduced independently: offline install, tamper rejected, 40/40 tests. Citations corrected |
| `codex/customer-conformance` (on `9d70dc2e`) | codex | `a7426411181f4dd3cc859581cc203ee4067c07b4` | Claude: **APPROVE** for the 4 customer HTTP vectors as source-derived expectations. Runtime: **LOCAL_PASS** (4 tests plus stored-key attestation) at `a7426411`/`f4312727` with runner `4ba51ed9`, window released (`757bd477`); cleanup verified independently (`45696ef3`). Reference only |
| Customer runner (operations script, not in Git) | codex | `customer_reference_01.py` sha256 `4ba51ed9…8f16` | Claude: **APPROVE** for the four-vector execution scope (RF1 budget, RF2 deferred-stop cleanup). v2 `d937e69b` is superseded. Window `alrust-customer-reference-20260930-01`: ACK `7e0a2e84`, START `fdfd53eb`, **RELEASED** `757bd477` with LOCAL_PASS; cleanup verified by both agents |
| `claude/spec-customer-mapping` | claude | `ba0345c3737c84f18f7d298513c3f2a420377c94` | Codex **APPROVE** source-only (`195291d9`) for C1–C22. New since `61f36df3`: **C21** (alert-note update and delete ignore the path alert and customer scope; the note list uses an unanchored id regex) and **C22** (every blackout update rewrites customer; an unscoped caller clears it to NULL), with the credentialed-versus-anonymous qualification (AN1). HTTP/SQL NOT_RUN |
| `claude/spec-lifecycle-receive` | claude | `a1de491c6677a3134c65d0e0b086cae08b925087` | Codex: **APPROVE** for its source-derived scope (receive slice only). HTTP beyond the 2 smoke cases is NOT_RUN |
| `claude/spec-lifecycle-actions` (on receive) | claude | `cedfcdff5afb8198bfb3588836156ca8510d076c` | Codex: **APPROVE** for its source-derived scope (A0–A6) |
| `claude/spec-lifecycle-housekeeping` (on actions) | claude | `c6aecf66d55d476c2ff936588e2f68d9a9ed9648` | Codex: **APPROVE** for its source-derived scope (K0–K5). Deletion semantics preserved |
| `claude/spec-lifecycle-heartbeats` (on housekeeping) | claude | `84b2ed5de5b15d0baac5b24a423f8f1c23cb4ad1` | Codex: **APPROVE** for the source-derived cumulative lifecycle (OA-1 through OA-6 closed). R1.3 and OA-5/A5.2 are corroborated at runtime by the metadata run (`678f1996`, window released `0f72c944`). The other exception, custom-plugin and receive-status-change edges are runtime NOT_RUN |
| `claude/charter-sync` | claude | this head | Latest Codex review: **APPROVE** at `36480cbf` (`a8a56db4`). This head needs its own review |
| `claude/spec-openapi` (on `e839ceaf`) | claude | `0fd8f6109b6c2bd4c2c4d68248651f73640c242f` | Heartbeat, single-alert, alert-query and `q=` slices approved (latest Codex APPROVE at `e191dcd7`, `9c60cb4f`). Since then: **notes** (`b0ba101e`, `d259e064`; ON-R1..R3 closed), the **AN1 auth description** (`bf41653f`, closed) and **blackouts** (`b1fd402c`, `3fe37dac`; OB-R1, R2 and C1 closed; `0fd8f610` fixes OB-R3, pending). Records reference SECURITY and auth defects (see Owner). HTTP, SQL and validator NOT_RUN |
| `codex/harness-integration` (on `e839ceaf`) | codex | `061d2a969d8fc4bd876f54885758727c5643c896` | Claude **APPROVE** for the source/offline scope: provenance verified, guard PASS, 50 offline tests, 16 collected. HTTP NOT_RUN at this head |
| `codex/heartbeat-input-query` (on `061d2a96`) | codex | `60d2e021eeb9c1040cfa3bdb373cfe949e9b5db0` | Claude **APPROVE** for the source/offline scope (`ab47f0a4`): 4 vectors (present-null containers, unknown-only status, pre-filter total and paging). Runner `48e3f654` approved (`99738ba0`). Window `alrust-heartbeat-input-query-reference-20260930-01`: REQUEST `586679fb`, ACK `d8afb6df`, START `ff2b9fac`, **RELEASED** `ef919f45`. Runtime: **LOCAL_PASS**, 4 HTTP tests in 0.42 s plus config and stored-key attestations, frozen `60d2e021`. Cleanup and key-free capture verified by both agents (`312fdb6f`). Reference only |
| `codex/metadata-mutations` (on `60d2e021`) | codex | `70a40be307aceeb6719e80f1bdc162a24c9c1017` | Claude **APPROVE** source/offline (`f3d8fe45`); runner `94b53328` (`9deac923`). Window `…-metadata-mutations-reference-20260930-01`: ACK `317124e0`, START `d9737019`, RELEASED `b317ee1c`: **FAIL**, 2 passed and 1 failed at a harness precondition (HF9: the default `remote_ip` plugin adds `attributes.ip`). Cleanup verified by both (`fa615dfa`). Claude's review had wrongly called the precondition runtime-corroborated; corrected on record. Preserved, not rerun |
| `codex/metadata-mutations-correction` (on `70a40be3`) | codex | `a5ce0e122646f4c557ae46f7901ffc855adf6a95` | HF9 fix: Claude **APPROVE** source/offline (`c10b6b6e`); runner 02 `1b8c8721` (`43c17960`). Window `…-20260930-02`: ACK `4185564e`, START `fb38a738`, **RELEASED** `399b7344`. Runtime: **LOCAL_PASS**, 3 HTTP tests in 0.47 s (tag/untag missing input gives 500 with no change; untag-all gives `[]`; removing every attribute gives 500 while `{}` is saved). Cleanup and key-free capture verified by both (`89fe250b`). HF9 closed. Reference only |
| `codex/alert-query-conformance` (on `a5ce0e12`) | codex | `9e33dc2af0292381db1d4f4afebe25170ca68568` | Claude **APPROVE_WITH_COMMENTS** source/offline (`3e497d36`; QH-C1 withdrawn). Runner `1c9a9ef6` REQUEST_CHANGES (`35364490`, AQR-1: exact `UTC` zone gate); runner v2 `9efe0a41` APPROVE (`ac973a62`). Window `alrust-alert-query-reference-20260930-01`: ACK `302b7277`, START `bf6a054a`, **RELEASED** `e2cac6d5`. Runtime: **LOCAL_PASS**, 3 HTTP tests in 0.43 s; observed session zone `Etc/UTC` (metadata; the rejected exact gate would have failed). Cleanup and key-free capture verified by both (`590b22a8`). Reference only |
| `codex/scaffold-clock-correction` (on `9e33dc2a`) | codex | `4f808c9d1c1615d5fb347e4cbed3d597c8f566b5` | HF7 fix: Claude **APPROVE** source/offline (`93d30e4b`); runner `38442f96` (`48bda41a`). Window `…-scaffold-hf7-reference-20260930-01`: ACK `e0c5eea6`, START `9e5c7e44`, RELEASED `51804198`. Runtime: **HTTP LOCAL_PASS**, 2 tests in 0.22 s (HF7 closed). The original **aggregate result stays FAIL** (cleanup UNKNOWN: a strict bind during TCP TIME-WAIT); later cleanup confirmed by both (`ec9e9e8e`). RF3 runner `6cab03c0` (bounded port settle) approved (`e3557228`) |
| `codex/alert-note-conformance` (on `4f808c9d`) | codex | `24594379d6b8c494e0f89936212380f79290cbc3` | Claude **APPROVE** source/offline (`463582c1`): 2 ordinary owned-note cases. Runner `d85660ff` approved (`7e6d761d`). No window yet; HTTP NOT_RUN |
| ALRUST-P0-CLOCK-01 policy | codex | design02 `67bb59f5…` | Claude: concurred, and approved as implemented in `db06ce44` |
| `claude/adr-0004-webui` (on `c9bbfaf5`) | claude | `e5a23c46c4a735a2437657df20125617b423e37f` | Codex: **APPROVE** as a proposed assessment. No webui source is selected; E1–E3 are NOT_RUN; a re-pin would be the owner's call |
| `claude/docs-landing` → [PR #2](https://github.com/steliosx/alrust/pull/2) (on `main`) | claude | `fcc06d7301a2048ab6fc4c73a80f5874dd3d08f4` | README only (ALRUST-DOCS-LANDING-01). Codex: **APPROVE** at `fcc06d73` (LD1–LD4 closed; `9409fde5` REQUEST_CHANGES is historical). **Marked ready for owner review**, not merged. Hosted checks for PR #2: NOT_RUN (no workflow on `main`). **The merge is the owner's** |

## Checks

Hosted checks, offline checks and live HTTP runs are recorded separately. A
local pass never satisfies a hosted gate. A reference-server smoke test is
not Rust behavioral parity.

| Check | Result |
|---|---|
| Hosted CI | Published PR #1 `e839ceaf`: **CI_BLOCKED_BILLING** (run 36632329142; jobs never started). Unpushed review heads: hosted **NOT_RUN**, not evidence of a hosted result either way |
| reference-guard regression, local | LOCAL_PASS 12/12 (both agents) |
| fmt / clippy / test, local | LOCAL_PASS. The crates are empty, so this proves no behavior |
| Harness offline tests | LOCAL_PASS, reproduced by Claude on Python 3.12.14: `9d70dc2e` 12/12; `db06ce44` 33/33; `e91dcabe` 40/40; `a7426411` 20/20. Offline infrastructure only |
| Harness findings | Closed: HF-1, HF-2 (controlled receive fixture only), HF-3, HF-4 (`130145ea`), HF-5 (differential CLI), HF-6, HF-8 (`130145ea`), CO-1. Open: HF-7 (low) |
| Reference HTTP smoke (Python 3.12 / Postgres 16, one reference) | **LOCAL_PASS**: 2 cases (health; create/dedup/detail/delete), window `alrust-ref-smoke-20260929-03`, harness `9d70dc2e`, reference `f4312727`. Cleanup confirmed by Codex and independently verified by Claude. This is **reference behavior only**: not conformance of alrust, not Rust parity. Windows 01 and 02 were released NOT_RUN before start |
| Two-server differential | **NOT_RUN**, and not yet approved |
| Reference customer vectors (window `alrust-customer-reference-20260930-01`) | **LOCAL_PASS**: 4 HTTP tests plus the stored-key attestation, at suite `a7426411`, reference `f4312727`, runner `4ba51ed9`. Synthetic config (AUTH_REQUIRED False, CUSTOMER_VIEWS False). It corroborates C7–C10, C14 and C16–C17 at runtime; it does not cover C1–C19 in full or other auth modes. Cleanup was verified by both agents. Reference behavior only |
| Operator status/action vectors (`codex/action-conformance` `a4d75716`) | Claude **APPROVE** as source-derived expectations (5 tests). Runner `4afb1260` approved (`8faec59d`). Window `alrust-operator-actions-reference-20260930-01`: ACK `d5fe3fc1`, START `82bcad86`, **RELEASED** `8e300281`. Runtime: **LOCAL_PASS**, 5 HTTP tests plus the effective-config and stored-key attestations, under the synthetic default config. Cleanup verified by both agents. Reference only |
| Metadata/suppression vectors (`codex/metadata-conformance` `678f1996`) | Claude **APPROVE** as source-derived expectations (2 tests). Runner `1b0cf1a1` approved (`53e52568`). Window `alrust-metadata-edges-reference-20260930-01`: ACK `10dbc36b`, START `c0aab9f7`, **RELEASED** `0f72c944`. Runtime: **LOCAL_PASS**, 2 HTTP tests plus the config and stored-key attestations, under the synthetic default config. This corroborates R1.3 and OA-5/A5.2 at runtime; the loaded-model detail is still source-only. Cleanup and a key-free capture were verified by both agents. Reference only |
| Heartbeat vectors (`codex/heartbeat-conformance` `9548f878`) | Claude **APPROVE** (3 tests). Runner `6bc8742d` approved. Window `alrust-heartbeat-vectors-reference-20260930-01`: ACK `1bd78063`, START `a833bb34`, **RELEASED** `1ba053be`. Runtime: **LOCAL_PASS**, 3 HTTP tests plus the config and key attestations. Corroborates HB0.1, HB4.1 (customer filter, empty-subset total 0) and C19. The non-empty total discrepancy, expiry and boundaries are NOT_RUN. Cleanup and a key-free capture were verified by both agents. Reference only |
| Lifecycle-transition and housekeeping HTTP vectors | **NOT_RUN** (planned) |
| Reference smoke at `db06ce44` (window `alrust-ref-smoke-20260929-04`) | **LOCAL_PASS**: 2 cases in 0.22 s (Python 3.12.14 / PostgreSQL 16.15), reference `f4312727`, runner `e0f30439`. Ack `bf39d294`, start `599ca26f`, release `32fdc13e`. Cleanup confirmed by Codex and independently verified by Claude. Reference behavior only |
| FAIL results on record | No **unexpected behavioral** failure in any executed run. Retained: the guard bypasses reproduced at `a674546` (a review FAIL, fixed at `e839ceaf`); corrected setup attempts (for example the first wheel-acquisition assumption in HF-4); expected negative probes (the tampered-wheel rejection), which are passes by design. Failed attempts are kept, not erased |

The informal smoke run of 2026-09-29, made before the charter on host Python
3.14, is superseded. It does not count as evidence.

## Owner items

- **GitHub Actions billing:** every job is refused before start.
- **Repository protection:** none. `branches/main` reports `protected: false`, and
  the protection and ruleset APIs return HTTP 403 (plan). The guard is
  **not enforced**. See `docs/repo-protection-recommendation.md`.

## PIN inputs and what each blocks

| Input | Blocks | Not blocked (synthetic or test substitutes allowed) |
|---|---|---|
| Real Veeam ONE (and Zabbix 7.0 LTS) webhook payloads | Phase 4 gate: replay of captured real payloads end to end; the Veeam ONE sample config | Receiver code, the mapper, synthetic labeled fixtures |
| Production `alertad.conf` | Phase 5 config-compatibility validation | The env-var config design and defaults from reference `settings.py` |
| Code-signing certificates (Windows, macOS) | Signed Phase 6 artifacts and auto-update trust | Unsigned CI builds, and test certificates |

## Open questions

1. webui: the v8.7.1 tag plus a specific fix, or an exact upstream commit?
   ADR-0004 will compare the two before the Phase 2 dependency.
2. The clock policy for two-server comparison (ALRUST-P0-CLOCK-01).
3. Unknown severity gives 500 in the reference. It is reproduced as it is;
   any divergence goes to the owner (lifecycle L1).
4. The customer wording in the kickoff: preserve-plus-clarify is recorded in
   `spec/customer-mapping.md` Q1. KICKOFF is unedited. No divergence.

## Next actions

- **codex-alrust:** review this STATUS head and OpenAPI `0fd8f610`; a
  window request for the note vectors.
- **claude-alrust:** next OpenAPI slices (keys, including a candidate C23
  where key updates rewrite customer, then users, customers and
  management); review Codex's heads; keep STATUS current.
- **Owner:**
  - PR #2 merge (approved, ready);
  - Actions billing;
  - the repository-protection plan;
  - **new, SECURITY (reference defects, source-level only, no exploit built
    or run; independently confirmed by Codex):** the pinned Alerta server
    builds SQL by string formatting from request or stored text in **four**
    places: `sort-by=attributes.<key>` (ORDER BY), `group-by` on the top-N
    endpoints (SELECT and GROUP BY, unvalidated), `q.df`, the default
    search field (replaced into a quoted identifier), and stored
    environment names on `/environments` (an IN list; arbitrary names can
    be stored via the `status: blackout` pre-receive bypass). The first
    three are reachable with `read:alerts`; the fourth needs
    `write:alerts`. Owner decisions:
    (a) what alrust does for these inputs, for example allow-listing
    `group-by` and quoting the attribute key, recorded in DECISIONS once
    decided; (b) whether production Alerta runs this version and is
    exposed, and whether to report upstream. Not blocking Phase 0 source
    work;
  - **new, AUTH (AN1):** with `AUTH_REQUIRED` and `ALLOW_READONLY` both on,
    a request **without credentials** is given read-only scopes but then
    dispatched **without any scope check**, so it reaches write and delete
    endpoints (`auth/decorators.py:164-169`; static source finding by
    Codex, confirmed by Claude; no request executed). Decide whether alrust
    keeps this, and check whether production Alerta runs with
    `ALLOW_READONLY`;
  - **new, customer scope (C21, C22):** alert-note update and delete ignore
    the path alert and customer (any note by id), and a blackout update
    rewrites its customer (an unscoped caller clears it to NULL, which
    widens matching under `CUSTOMER_VIEWS`). Preserve or change in alrust?
    Recorded, not decided;
  - **new, alert behavior:** the reference `q=` search keeps only the first
    two operands of any flat same-operator chain, silently. `disk full
    server` searches only `disk` OR `full`; `a AND b AND c` ignores `c`
    (`queryparser.py:20-26`; confirmed by both agents' parser probes).
    Should alrust reproduce this for compatibility or search every operand?
    This changes which alerts operators see. Not blocking Phase 0 source
    work;
  - **new, not blocking:** whether alrust keeps the reference's App Engine
    cron auth branch. With `AUTH_REQUIRED` on, no credentials and
    `ALLOW_READONLY` off, a request with a truthy (non-empty)
    `X-Appengine-Cron` header and `X-Forwarded-For: 0.1.0.1` passes the
    decorator with no scope check (`auth/decorators.py:171-173`). The
    branch sets none of the usual auth fields (`g.login`, `g.scopes`,
    `g.customers`), so what the handlers then return is NOT established
    (source-derived, NOT_RUN). It is an open question about the consumed
    API's auth contract. The spec records it without deciding, and it does
    not block Phase 0 work.
