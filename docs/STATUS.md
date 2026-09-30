# Status

_Last updated: 2026-09-30T13:40Z by claude-alrust, the sole writer of this
file and DECISIONS.md. Codex sends corrections through the mailbox._

## Current phase

**Phase 0 in progress. The gate is open.**

| Phase | State | Gate |
|---|---|---|
| 0 Spec + conformance | Lifecycle (receive, actions, housekeeping, heartbeats) and customer-mapping specs source-derived and reviewed. Separate scoped reference HTTP runs: smoke 2, customer 4, operator 5, metadata 2, heartbeat 3, heartbeat input/query 4, metadata mutations 3, alert queries 3, HF7 scaffold smoke 2, alert notes 2, blackouts 2, API keys 1, config 1, groups 1, permissions 1 (each HTTP LOCAL_PASS at its own head and window; the first metadata-mutations attempt FAILED on a harness precondition, HF9, and is preserved). OpenAPI: heartbeat, single-alert, alert-query, `q=`, notes, blackouts, API keys, users, groups, permissions, customers and `/config` (reviewed); management outstanding (priorities from the web UI consumer inventory) | **Open.** These are separate scoped runs, not a full-suite pass. Hosted CI has never executed. OpenAPI extraction and broader vectors are outstanding |
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
| `claude/spec-customer-mapping` | claude | `decbb78aa03a55934d9b078b9f5a8c153f454870` | Codex **APPROVE** source-only (`0b27a3bc`) for C1–C23. C23: every API-key update rewrites customer; an unscoped caller clears it, removing customer filtering from alert reads the key's scopes otherwise permit. HTTP/SQL NOT_RUN |
| `claude/spec-lifecycle-receive` | claude | `a1de491c6677a3134c65d0e0b086cae08b925087` | Codex: **APPROVE** for its source-derived scope (receive slice only). HTTP beyond the 2 smoke cases is NOT_RUN |
| `claude/spec-lifecycle-actions` (on receive) | claude | `cedfcdff5afb8198bfb3588836156ca8510d076c` | Codex: **APPROVE** for its source-derived scope (A0–A6) |
| `claude/spec-lifecycle-housekeeping` (on actions) | claude | `c6aecf66d55d476c2ff936588e2f68d9a9ed9648` | Codex: **APPROVE** for its source-derived scope (K0–K5). Deletion semantics preserved |
| `claude/spec-lifecycle-heartbeats` (on housekeeping) | claude | `84b2ed5de5b15d0baac5b24a423f8f1c23cb4ad1` | Codex: **APPROVE** for the source-derived cumulative lifecycle (OA-1 through OA-6 closed). R1.3 and OA-5/A5.2 are corroborated at runtime by the metadata run (`678f1996`, window released `0f72c944`). The other exception, custom-plugin and receive-status-change edges are runtime NOT_RUN |
| `claude/charter-sync` | claude | this head | Latest Codex review: **APPROVE_EXACT_SOURCE_WITH_COMMENT** at `48b11e3c` (`136ef825`; ST30-C1, fixed on this head). Earlier: `10bed7e8` (`c7c629aa`; ST29-C1: checking production exposure is the owner's, under the production-access gate, not an agent task). This head needs its own review |
| `claude/spec-openapi` (on `e839ceaf`) | claude | `9b4c0dc47903372fb356f6ef67f8cf96b6f60a95` | All slices through **`/config`** approved: Codex **APPROVE_EXACT_SOURCE** at `9b4c0dc4` (`eb6fb326`), cumulative from `e6a625d9` (customers, `7a5fd582`), after REQUEST_CHANGES at `773d5c27` (`efcd64f7`, CF-R1: config-sourced values are not guaranteed booleans or objects) and `f667fd4f` (`10e0fac5`, CF-R2: the model name and default status depend on `ALARM_MODEL`). Earlier: customers `d1fdb7a8` (`702587fc`, CU-R1..R3); groups and permissions `08d751a9`, `12631144`, `593f511e`. 52 paths, 70 operations. `/config` cites the config window for exactly what it checked. Records G-SEC-1, P-SEC-1, C-SEC-1, G-DEF-1 and GR-R1 (see Owner). SQL, HTTP, auth and validator NOT_RUN |
| `codex/harness-integration` (on `e839ceaf`) | codex | `061d2a969d8fc4bd876f54885758727c5643c896` | Claude **APPROVE** for the source/offline scope: provenance verified, guard PASS, 50 offline tests, 16 collected. HTTP NOT_RUN at this head |
| `codex/heartbeat-input-query` (on `061d2a96`) | codex | `60d2e021eeb9c1040cfa3bdb373cfe949e9b5db0` | Claude **APPROVE** for the source/offline scope (`ab47f0a4`): 4 vectors (present-null containers, unknown-only status, pre-filter total and paging). Runner `48e3f654` approved (`99738ba0`). Window `alrust-heartbeat-input-query-reference-20260930-01`: REQUEST `586679fb`, ACK `d8afb6df`, START `ff2b9fac`, **RELEASED** `ef919f45`. Runtime: **LOCAL_PASS**, 4 HTTP tests in 0.42 s plus config and stored-key attestations, frozen `60d2e021`. Cleanup and key-free capture verified by both agents (`312fdb6f`). Reference only |
| `codex/metadata-mutations` (on `60d2e021`) | codex | `70a40be307aceeb6719e80f1bdc162a24c9c1017` | Claude **APPROVE** source/offline (`f3d8fe45`); runner `94b53328` (`9deac923`). Window `…-metadata-mutations-reference-20260930-01`: ACK `317124e0`, START `d9737019`, RELEASED `b317ee1c`: **FAIL**, 2 passed and 1 failed at a harness precondition (HF9: the default `remote_ip` plugin adds `attributes.ip`). Cleanup verified by both (`fa615dfa`). Claude's review had wrongly called the precondition runtime-corroborated; corrected on record. Preserved, not rerun |
| `codex/metadata-mutations-correction` (on `70a40be3`) | codex | `a5ce0e122646f4c557ae46f7901ffc855adf6a95` | HF9 fix: Claude **APPROVE** source/offline (`c10b6b6e`); runner 02 `1b8c8721` (`43c17960`). Window `…-20260930-02`: ACK `4185564e`, START `fb38a738`, **RELEASED** `399b7344`. Runtime: **LOCAL_PASS**, 3 HTTP tests in 0.47 s (tag/untag missing input gives 500 with no change; untag-all gives `[]`; removing every attribute gives 500 while `{}` is saved). Cleanup and key-free capture verified by both (`89fe250b`). HF9 closed. Reference only |
| `codex/alert-query-conformance` (on `a5ce0e12`) | codex | `9e33dc2af0292381db1d4f4afebe25170ca68568` | Claude **APPROVE_WITH_COMMENTS** source/offline (`3e497d36`; QH-C1 withdrawn). Runner `1c9a9ef6` REQUEST_CHANGES (`35364490`, AQR-1: exact `UTC` zone gate); runner v2 `9efe0a41` APPROVE (`ac973a62`). Window `alrust-alert-query-reference-20260930-01`: ACK `302b7277`, START `bf6a054a`, **RELEASED** `e2cac6d5`. Runtime: **LOCAL_PASS**, 3 HTTP tests in 0.43 s; observed session zone `Etc/UTC` (metadata; the rejected exact gate would have failed). Cleanup and key-free capture verified by both (`590b22a8`). Reference only |
| `codex/scaffold-clock-correction` (on `9e33dc2a`) | codex | `4f808c9d1c1615d5fb347e4cbed3d597c8f566b5` | HF7 fix: Claude **APPROVE** source/offline (`93d30e4b`); runner `38442f96` (`48bda41a`). Window `…-scaffold-hf7-reference-20260930-01`: ACK `e0c5eea6`, START `9e5c7e44`, RELEASED `51804198`. Runtime: **HTTP LOCAL_PASS**, 2 tests in 0.22 s (HF7 closed). The original **aggregate result stays FAIL** (cleanup UNKNOWN: a strict bind during TCP TIME-WAIT); later cleanup confirmed by both (`ec9e9e8e`). RF3 runner `6cab03c0` (bounded port settle) approved (`e3557228`) |
| `codex/alert-note-conformance` (on `4f808c9d`) | codex | `24594379d6b8c494e0f89936212380f79290cbc3` | Claude **APPROVE** source/offline (`463582c1`); runner `d85660ff` (`7e6d761d`). Window `…-alert-note-reference-20260930-01`: ACK `b7e82bba`, START `2624a63a`, **RELEASED** `ca5f1be0`. Runtime: **LOCAL_PASS**, 2 HTTP tests in 0.64 s, aggregate PASS, RF3 port settle CONFIRMED. Cleanup verified by both (`600e3acf`). Reference only |
| `codex/blackout-conformance` (on `24594379`) | codex | `0f2566d63fe0d4a76687a64160d3158bf2e2a9e0` | Claude **APPROVE** source/offline (`aeacced6`, `3507bfe7`); runner `a058df0f` (`1febbdd1`). Window `…-blackout-reference-20260930-01`: ACK `b3ca545c`, START `66331864`, **RELEASED** `39dd3946`. Runtime: **LOCAL_PASS**, 2 HTTP tests in 0.43 s, aggregate PASS; `remaining` observed as the integer `120` on create and the string `"0"` on reads (OB-R4). Cleanup verified by both (`58aaa2f5`). Reference only |
| `codex/api-key-conformance` (on `0f2566d6`) | codex | `b1439810c16e90a701b2f136fc3e41482f7f0fa5` | Claude **APPROVE_WITH_COMMENTS** source/offline (`05d992f3`): one owned API-key create, read, update and delete case with no response bodies exported and a fixed-message, plain-assert pytest config; 66 offline. Runner `48f74f96` approved (`8a6c7c2f`): BASE_URL attested empty, five-key plus generic-pattern exclusion over every shareable artifact. Window `alrust-api-key-reference-20260930-01`: REQUEST `c0056601`, ACK `86edb30e`, START `38085eb5`, **RELEASED** `03bafb2c`. Runtime: **LOCAL_PASS**, 1 HTTP test in 0.12 s (one owned key: create 201, read, text-only update, delete, then 404; the new key was never authenticated), aggregate PASS. Cleanup and five-key plus generic exclusion verified by both (`7a323f9b`). Reference only; none of the key SECURITY items was exercised |
| `codex/config-conformance` (on `b1439810`) | codex | `0c83a5a38b8d490b70263bf1058652564d4e518e` | Plan v2 `39480ddb` (v1 `b6170dc8` superseded: it expected the selector string as the model name): Claude **APPROVE_WITH_COMMENTS** (`e7a3881b`; CP-C1/C2 citations fixed in the harness, CP-N1: `/config` has no permission decorator, so a run is not authorization evidence). Harness: Claude **APPROVE** source/offline (`8a987c58`): 106 offline, 1 collected. Runner `f8811c59` (tests `574e02c7`) approved (`2d661ca9`): the pinned CLI is launched with one read-only first-request hook that attests the served app's debug, BASE_URL and ProxyFix state and binds its PID (CF-N1 to N3). Window `alrust-config-reference-20260930-01`: REQUEST `044882e3`, ACK `eb144153`, START `63776d76`, **RELEASED** `bb031d5b`. Runtime: **LOCAL_PASS**, 1 HTTP test in 0.04 s against the *instrumented* pinned reference (36 root keys, nested keysets, model name `Alerta 9.1.0`, severity/colors aliases, controlled flags and timeouts, exact endpoint), aggregate PASS. Cleanup, served PID binding and four-key plus generic exclusion verified by both (`5198b8d8`). Reference only; not authorization evidence |
| Web UI consumer inventory (source only, not Git) | codex | helper `8efc0f9e`, summary `2e212266`, correction `d47bb458` | Lexical inventory of webui `2687e4af` service calls. Claude **APPROVE_WITH_COMMENTS** (`0fd57eab`): 71 declared calls; 59 with a direct caller (corrected from 58 in `131b3b08`, WI-R1: a relative import of `getCounts` was missed); 24 of those are not yet in OpenAPI (groups 8, permissions 5 including `/scopes`, customers 4, management 3, `/config`, auth confirm/forgot/reset 3). WI-C1: `getUserAttributes(userId)` targets a GET that exists only at `/user/me/attributes`, and has no caller. Not a call graph and not a drop justification. Codex's static route inventory (`96083962`): 82 API view declarations, 50 in OpenAPI |
| `codex/groups-conformance` (on `0c83a5a3`) | codex | `6266212b413926e68b083c3fc9f36af179909292` | Plan GRP-S1..S5 (`039150fc`, identical resend `73f6ef60`): Claude **APPROVE** plan only (`ea14a636`; GRP-N1..N3). Harness: Claude **APPROVE** source/offline (`a64db9f9`): one `GET /groups?page=1&page-size=1` on an attested empty table; 40 focused, 146 offline, 1 collected. Runner `4579b607` (tests `d9624b48`) approved (`c3a055f6`): a read-only `COUNT(1) FROM groups` on the served app's own connection after readiness, bound to the served PID, window and config by a private hash chain (GH-N1..N3). Window `alrust-groups-reference-20260930-01`: REQUEST `9e4102fd`, ACK `73ab1e9e`, START `3001cb1e`, **RELEASED** `3b2690f6`. Runtime: **LOCAL_PASS**, 1 HTTP test in 0.05 s against the *instrumented* pinned reference (read-only before-request hook on every request; one count after readiness), with an attested count of 0; the empty page matched GRP-S2 type-exactly. Aggregate PASS. Cleanup, the count chain, 14 private hashes and four-key plus generic exclusion verified by both (`ad726311`). Reference only: no nonempty branch, `Group.count`, auth or UI-default paging claim |
| Permissions read-only plan (source only) | codex | plan `97d1413b` | Claude **APPROVE** plan only (`6ca6658a`): one `GET /perms?page=1&page-size=1` on an attested empty perms table; seven keys, total 3, the three virtual built-ins with fresh uuid4 ids and exact hrefs (PN-1: the `urljoin` double-slash collapse was checked on Python 3.12.14) |
| `codex/permissions-conformance` (on `6266212b`) | codex | `49c2ec3682519dbe4f7ea50ebc5d6eed65574145` | Harness: Claude **APPROVE** source/offline (`2ca07db9`): 74 focused, 220 offline, 1 collected; PN-1..PN-4 addressed. Runner `a4822c91` (tests `116094f6`) approved (`5c870440`): the five served role settings and `COUNT(1) FROM perms` observed once after readiness on the served connection, chained to the PID, window and config (PRN-1..3). Window `alrust-permissions-reference-20260930-01`: REQUEST `a12a5435`, ACK `829d938b`, START `c86fe3d8`, **RELEASED** `08c6a471`. Runtime: **LOCAL_PASS**, 1 HTTP test in 0.05 s against the *instrumented* pinned reference (baseline hook on every request; role settings and count observed once after readiness), with an attested empty table and the five default role settings; seven root keys, total 3, the three virtual rows in order with exact scopes, canonical distinct uuid4 ids and exact hrefs (PERM-S1/S2). Aggregate PASS. Cleanup, the roles/count chain, 14 private hashes and four-key plus generic exclusion verified by both (`3258751a`). Reference only: no stored rows, UI-default paging, detail, `/scopes`, filter or authorization claim |
| Management health read-only plan (source only) | codex | plan `233c2108` | Claude **APPROVE** plan only (`05c3489e`): one `GET /management/healthcheck` on an attested empty heartbeats table, expecting 200 and the text `OK` (MGH-S1..S4). Notes: at most the first 1000 heartbeats are examined, unordered (MGH-N1); a negative age can appear stale through `timedelta.seconds`, depending on the timeout and the within-day remainder (MGH-N2); readiness requests do not count (MGH-N4). Harness `de7e0cbc` (adds `Client.request_text`): Claude **APPROVE** source/offline (`fedb4cdd`; 48 focused, 268 offline, 1 collected). Runner `661e2576` (tests `106418b3`) approved (`82cc7ca6`): `COUNT(1) FROM heartbeats` once after readiness on the served connection, whole-record hash chain; readiness and setup health requests are separate from the one suite request (MHR-N1). Window `alrust-management-health-reference-20260930-01` requested (`04393291`) and ACKed; HTTP NOT_RUN at this snapshot |
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
| Harness findings | Closed: HF-1, HF-2 (controlled receive fixture only), HF-3, HF-4 (`130145ea`), HF-5 (differential CLI), HF-6, HF-7 (`4f808c9d`; HTTP LOCAL_PASS in window scaffold-hf7-01, whose original aggregate result stays FAIL), HF-8 (`130145ea`), HF-9 (`a5ce0e12`), CO-1. Open: none |
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

- **codex-alrust:** review this STATUS head; further bounded vectors as
  separate reviewed heads.
- **claude-alrust:** next OpenAPI slice: management (with MGH-N1/N2),
  then the remaining uninventoried routes (for example `/` and `/_`). The auth confirm/forgot/reset endpoints wait for the owner's
  auth-contract decision. Review Codex's heads; keep STATUS current.
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
  - **new, customer scope (C21, C22, C23):** after the path alert is found
    with the usual customer filter, note update and delete look the note up
    by id with no alert or customer predicate (C21). A blackout update
    rewrites its customer; an unscoped caller clears it to NULL, which
    widens matching under `CUSTOMER_VIEWS` (C22). An API-key update does the
    same, so a customer-scoped key edited by an admin loses customer
    filtering on the alert reads its scopes otherwise permit (C23; clearing
    the customer does not grant `read:alerts`). Preserve or change in alrust? Recorded,
    not decided;
  - **new, SECURITY (users; source-level, nothing executed; urgent for
    production):** `PUT /user/<id>` checks the role ceiling only for a
    plural `roles` field, but the model also accepts a legacy singular
    `role`, so `{"role": "admin"}` gives any user the admin role without
    the check (`views/users.py:181-186`, `models/user.py:180-181`). A holder
    of `admin:users` can therefore create full admins under the default role
    mapping. Combined with AN1 (`ALLOW_READONLY`), a caller without
    credentials reaches that handler and could set a user's role and
    password, an inferred takeover chain. Also: a failed user insert can
    return 201 with unsaved data, and a missing password is hashed as `''`.
    Decide alrust's behavior and check production exposure;
  - **new, SECURITY (groups, permissions and customer mappings; source-level, nothing
    executed; confirmed by Codex as source traces; same class as the users
    item):**
    - G-SEC-1: at password login, group names are passed to the role lookup
      (`auth/basic.py:52-53`, `:93-94`), so a group whose name is in the
      configured `ADMIN_ROLES` (default `['admin']`) gives its members
      `ADMIN_SCOPES`. No group endpoint applies a role ceiling, so a holder
      of `admin:groups` can create that group, add any user (including
      themselves), and that user obtains full admin at their next
      successful password login (the usual password, account-status,
      domain and email-verification checks still apply). The token keeps
      those scopes until it expires (default 14 days), even after removal.
      Under `CUSTOMER_VIEWS`, group names also act as customer match keys;
    - P-SEC-1: `PUT /perm/<id>` applies neither the scope ceiling nor the
      reserved role names that `POST /perm` does
      (`views/permissions.py:132-156` against `:26-36`), so a holder of
      `admin:perms` can give any role, including the default `user` role,
      any scope such as `admin`. `POST` also lets empty-string scopes
      through its ceiling;
    - GR-R1 (availability, data integrity; inferred from source): `PUT
      /group/<id>` with `{"name": ""}` commits the empty name and then
      returns 500. After that the group can no longer be read, fixed or
      deleted through the API. A member's password login fails once it
      passes the usual credential, account, domain and email checks, and
      so does every member request that selects the auth-proxy branch;
      API key, Hawk and Bearer requests are checked earlier
      (`auth/decorators.py:31-95` against `:104-116`) and do not reach it;
    - G-DEF-1 (inferred from source, not executed): for a login that is
      not in `ADMIN_USERS`, when a request selects the auth-proxy branch
      with `CUSTOMER_VIEWS` on and the user is in at least one group, Group
      objects are bound as SQL parameters, predicting a 500
      (`auth/decorators.py:104-116`, `auth/utils.py:42-49`,
      `postgres/base.py:1351-1359`). `ADMIN_USERS` logins return `*` before
      any SQL, and earlier authentication paths bypass the branch;
    - C-SEC-1 (source-confirmed by both agents, static, not executed):
      `POST` and `PUT /customer` need only `admin:customers`, and nothing
      compares the mapped customer with the caller's own customers. So a
      customer-scoped holder of `admin:customers` can map a name that a
      later lookup will match for them to another customer, or to `*`,
      which removes customer filtering. With `CUSTOMER_VIEWS` on, which
      name matches depends on the login path: login, group names and
      domain at password login (sealed into the token until it expires),
      email and domain for HTTP Basic, and the auth proxy hits G-DEF-1.
      `ADMIN_USERS` logins already see all customers. It takes effect at
      the next successful matching lookup.
    Decide alrust's behavior (for example whether group names act as roles,
    and whether role, group, permission and customer-mapping edits share
    one ceiling) and check whether production grants `admin:groups`,
    `admin:perms` or `admin:customers` apart from `admin`;
  - **new, SECURITY (API keys; source-level, nothing executed):**
    - an edit that omits `expireTime` stores NULL, and every read rebuilds
      it as now + `API_KEY_EXPIRE_DAYS` (default 365), so with a positive
      setting the key **never expires**;
    - `scopes: []` on update, or an unrecognised `type` on create for a
      non-admin key user, passes the scope-ceiling check but reads back as
      the default read and write;
    - every serialized ApiKey entity (create, get, update, list) includes
      the secret and an `href` built from it, and keys can be looked up by
      secret (a delete returns only `{status: ok}`);
    - without a login (AN1 branch), the key user filter is dropped.
    Decide alrust's behavior and check production keys;
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
