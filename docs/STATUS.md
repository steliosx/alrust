# Status

_Last updated: 2026-09-29T23:25Z by claude-alrust, the sole writer of this
file and DECISIONS.md. Codex sends corrections through the mailbox._

## Current phase

**Phase 0 in progress. The gate is open.**

| Phase | State | Gate |
|---|---|---|
| 0 Spec + conformance | Lifecycle slices (receive, actions, housekeeping, heartbeats) and customer mapping written; harness foundation approved for reference smoke; reference smoke LOCAL_PASS (2 cases) | Open. The suite covers 2 cases; hosted CI never executed; lifecycle review pending |
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
| `codex/receive-clock-correction` | codex | `db06ce44f1fdbc3b0dbeb66b6f2952eb0d702071` | Claude: **APPROVE** for the offline `receive-clock-v1` scope (controlled fixture) and the reference-only HTTP smoke at this head. The smoke needs a new signed window. No two-server run has been requested; there is no candidate server yet |
| `codex/…` HF-5 credential correction (on `db06ce44`) | codex | `e91dcabea1291b6540bc4d97636e6eef8df2bef0` | Claude: **APPROVE** for the differential-CLI credential contract (`SYNTHETIC-` plus 48 hex) |
| `codex/customer-conformance` (on `9d70dc2e`) | codex | `a7426411181f4dd3cc859581cc203ee4067c07b4` | Claude: **APPROVE** for the 4 customer HTTP vectors as source-derived expectations. Runtime is **NOT_RUN** and needs its own window, with CLI-seeded synthetic keys |
| `claude/spec-customer-mapping` | claude | `207f1bcac270afa40b8efc1bb2464e1886e48857` | Codex APPROVE at `edb13f94` (C19). The newer citation-only commit is pending review |
| `claude/spec-lifecycle-receive` | claude | `a1de491c6677a3134c65d0e0b086cae08b925087` | Codex: **APPROVE** for its source-derived scope (receive slice only). HTTP beyond the 2 smoke cases is NOT_RUN |
| `claude/spec-lifecycle-actions` (on receive) | claude | `cedfcdff5afb8198bfb3588836156ca8510d076c` | ACTION-01..03 corrections submitted (the previous head `9307790a` got REQUEST_CHANGES) |
| `claude/spec-lifecycle-housekeeping` (on actions) | claude | `c6aecf66d55d476c2ff936588e2f68d9a9ed9648` | HOUSEKEEPING-01 correction submitted (the previous head `0560cc5a` got REQUEST_CHANGES) |
| `claude/spec-lifecycle-heartbeats` (on housekeeping) | claude | `d3383b8a882b6b708b4b3c41d9405fca568d25e0` | HEARTBEAT-01 corrected. HEARTBEAT-02 partly disputed with a citation: an empty filtered result does return `total: 0`. Previous head `993c5ddc` |
| `claude/charter-sync` | claude | this head | Codex **APPROVE** at `ac667cf8`. This STATUS refresh needs its own review |
| ALRUST-P0-CLOCK-01 policy | codex | design02 `67bb59f5…` | Claude: concurred, and approved as implemented in `db06ce44` |

## Checks

Hosted checks, offline checks and live HTTP runs are recorded separately. A
local pass never satisfies a hosted gate. A reference-server smoke test is
not Rust behavioral parity.

| Check | Result |
|---|---|
| Hosted CI (all jobs, every head) | **CI_BLOCKED_BILLING**: never started |
| reference-guard regression, local | LOCAL_PASS 12/12 (both agents) |
| fmt / clippy / test, local | LOCAL_PASS. The crates are empty, so this proves no behavior |
| Harness offline tests | LOCAL_PASS, reproduced by Claude on Python 3.12.14: `9d70dc2e` 12/12; `db06ce44` 33/33; `e91dcabe` 40/40; `a7426411` 20/20. Offline infrastructure only |
| Harness findings | Closed: HF-1, HF-2 (controlled receive fixture only), HF-3, HF-5 (differential CLI), HF-6, CO-1. Open: HF-4 (dependency hashes), HF-7 (low), HF-8 (format.py citations) |
| Reference HTTP smoke (Python 3.12 / Postgres 16, one reference) | **LOCAL_PASS**: 2 cases (health; create/dedup/detail/delete), window `alrust-ref-smoke-20260929-03`, harness `9d70dc2e`, reference `f4312727`. Cleanup confirmed by Codex and independently verified by Claude. This is **reference behavior only**: not conformance of alrust, not Rust parity. Windows 01 and 02 were released NOT_RUN before start |
| Two-server differential | **NOT_RUN**, and not yet approved |
| Customer/auth HTTP vectors (`a7426411`), lifecycle-transition, housekeeping and heartbeat HTTP vectors | **NOT_RUN**. The customer vectors are approved as source; the others are planned |
| Reference smoke at `db06ce44` (window 04) | **NOT_RUN**. The draft runner exists; no request or grant yet |
| FAIL results on record | none executed. The pre-fix guard bypasses reproduced by Codex at `a674546` were a review FAIL, fixed at `e839ceaf` |

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

- **codex-alrust:** review the lifecycle heads (receive a1de491c, actions,
  housekeeping, heartbeats) and this charter head; author synthetic
  customer/auth vectors (CLI-seeded keys); implement the clock policy with
  CK-1..3; follow up HF-4/5/6.
- **claude-alrust:** add HB4.2 (per-id heartbeat reads unfiltered) to
  customer-mapping; ADR-0004 (webui tag plus fix vs exact commit) before
  Phase 2; then the OpenAPI "as implemented" extraction (ALRUST-P0-SPEC-01
  step 4); review Codex's vector and clock heads; keep STATUS current.
