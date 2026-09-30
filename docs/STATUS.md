# Status

_Last updated: 2026-09-30T04:00Z by claude-alrust, the sole writer of this
file and DECISIONS.md. Codex sends corrections through the mailbox._

## Current phase

**Phase 0 in progress. The gate is open.**

| Phase | State | Gate |
|---|---|---|
| 0 Spec + conformance | Lifecycle (receive, actions, housekeeping, heartbeats) and customer-mapping specs source-derived and reviewed. Separate scoped reference HTTP runs: smoke 2, customer 4, operator 5, metadata 2, heartbeat 3, heartbeat input/query 4 (each LOCAL_PASS at its own head and window). OpenAPI: heartbeat and single-alert slices (source-derived, reviewed) | **Open.** These are separate scoped runs, not a full-suite pass. Hosted CI has never executed. OpenAPI extraction and broader vectors are outstanding |
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
| `claude/spec-customer-mapping` | claude | `207f1bcac270afa40b8efc1bb2464e1886e48857` | Codex: **APPROVE** for its source-derived scope (C1–C19) |
| `claude/spec-lifecycle-receive` | claude | `a1de491c6677a3134c65d0e0b086cae08b925087` | Codex: **APPROVE** for its source-derived scope (receive slice only). HTTP beyond the 2 smoke cases is NOT_RUN |
| `claude/spec-lifecycle-actions` (on receive) | claude | `cedfcdff5afb8198bfb3588836156ca8510d076c` | Codex: **APPROVE** for its source-derived scope (A0–A6) |
| `claude/spec-lifecycle-housekeeping` (on actions) | claude | `c6aecf66d55d476c2ff936588e2f68d9a9ed9648` | Codex: **APPROVE** for its source-derived scope (K0–K5). Deletion semantics preserved |
| `claude/spec-lifecycle-heartbeats` (on housekeeping) | claude | `84b2ed5de5b15d0baac5b24a423f8f1c23cb4ad1` | Codex: **APPROVE** for the source-derived cumulative lifecycle (OA-1 through OA-6 closed). R1.3 and OA-5/A5.2 are corroborated at runtime by the metadata run (`678f1996`, window released `0f72c944`). The other exception, custom-plugin and receive-status-change edges are runtime NOT_RUN |
| `claude/charter-sync` | claude | this head | Latest Codex review: **APPROVE_WITH_COMMENTS** at `b18c071c` (`4a9de0c6`; both comments applied here). Earlier: APPROVE at `408bf854` (`29831e24`) and `fb5823ca`. This head needs its own review |
| `claude/spec-openapi` (on `e839ceaf`) | claude | `791bf3fe0e26a47d5fbec8883dfe6bbfa41a0337` | OpenAPI "as implemented": **heartbeat and single-alert slices** (`POST /alert` and the per-id alert operations). Codex **APPROVE** for the source-description scope (`b10ea724`) after fix-forward rounds: `c5678dbe`, `c3c7ab1b` (`ebeae471`), `df3cfbad` (`589be671`), `6a94eb7f` (`15a7720e`) and `2f0b0f57` (`8cafc987`) each got REQUEST_CHANGES; OAH1–5, OAA-R1–4 and OAA-P1–2 are closed. Not a complete OpenAPI spec; HTTP/SQL behavior of the new source findings NOT_RUN; schema validator NOT_RUN |
| `codex/harness-integration` (on `e839ceaf`) | codex | `061d2a969d8fc4bd876f54885758727c5643c896` | Claude **APPROVE** for the source/offline scope: provenance verified, guard PASS, 50 offline tests, 16 collected. HTTP NOT_RUN at this head |
| `codex/heartbeat-input-query` (on `061d2a96`) | codex | `60d2e021eeb9c1040cfa3bdb373cfe949e9b5db0` | Claude **APPROVE** for the source/offline scope (`ab47f0a4`): 4 vectors (present-null containers, unknown-only status, pre-filter total and paging). Runner `48e3f654` approved (`99738ba0`). Window `alrust-heartbeat-input-query-reference-20260930-01`: REQUEST `586679fb`, ACK `d8afb6df`, START `ff2b9fac`, **RELEASED** `ef919f45`. Runtime: **LOCAL_PASS**, 4 HTTP tests in 0.42 s plus config and stored-key attestations, frozen `60d2e021`. Cleanup and key-free capture verified by both agents (`312fdb6f`). Reference only |
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

- **codex-alrust:** review this STATUS head; propose further vectors (for
  example OpenAPI source findings OAA1-5) as separate reviewed heads.
- **claude-alrust:** next OpenAPI slice (the alert list, count and top-N
  endpoints and the `q=` grammar); review Codex's heads; keep STATUS
  current.
- **Owner:**
  - PR #2 merge (approved, ready);
  - Actions billing;
  - the repository-protection plan;
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
