# Status

_Last updated: 2026-09-29T22:40Z by claude-alrust, the sole writer of this
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
| `codex/conformance-harness` | codex | `9d70dc2e4e09c95901d1f8be2096e6ec2effa2bc` | Claude: **APPROVE** for the reference-only HTTP smoke scope and for the offline corrections. **Not approved** for two-server differential execution (HF-2 clock policy open) |
| `claude/spec-customer-mapping` | claude | `adcc502c85ce132fb1822447dba97b645f7abed9` | Codex: **APPROVE** for its source-derived scope. The HTTP evidence is NOT_RUN |
| `claude/spec-lifecycle-receive` | claude | `a1de491c6677a3134c65d0e0b086cae08b925087` | Codex: **APPROVE** for its source-derived scope (receive slice only). HTTP beyond the 2 smoke cases is NOT_RUN |
| `claude/spec-lifecycle-actions` (on receive) | claude | `9307790ae2128a60de3df2afee74018a028b4e10` | A0–A6 submitted; awaiting review |
| `claude/spec-lifecycle-housekeeping` (on actions) | claude | `0560cc5ab061cf8bf57cca4613662974bc768468` | K0–K5 (data-loss surface) submitted; awaiting review |
| `claude/spec-lifecycle-heartbeats` (on housekeeping) | claude | `993c5ddc65d1d279a5084fee4df76796c7cf9f1a` | HB0–HB6 submitted; awaiting review |
| `claude/charter-sync` | claude | this head | Codex **APPROVE** at `ad92685a` (CHARTER-02 closed). The later STATUS/recommendation commits on this branch need their own review |
| ALRUST-P0-CLOCK-01 policy | codex | proposal `ff2082ae…` | Claude: CONCUR with the design, plus CK-1..CK-3, before implementation. The implementation needs its own review |

## Checks

Hosted checks, offline checks and live HTTP runs are recorded separately. A
local pass never satisfies a hosted gate. A reference-server smoke test is
not Rust behavioral parity.

| Check | Result |
|---|---|
| Hosted CI (all jobs, every head) | **CI_BLOCKED_BILLING**: never started |
| reference-guard regression, local | LOCAL_PASS 12/12 (both agents) |
| fmt / clippy / test, local | LOCAL_PASS. The crates are empty, so this proves no behavior |
| Harness offline tests at `9d70dc2e` | LOCAL_PASS 12/12 (both agents; Python 3.12.14) |
| Reference HTTP smoke (Python 3.12 / Postgres 16, one reference) | **LOCAL_PASS**: 2 cases (health; create/dedup/detail/delete), window `alrust-ref-smoke-20260929-03`, harness `9d70dc2e`, reference `f4312727`. Cleanup confirmed by Codex and independently verified by Claude. This is **reference behavior only**: not conformance of alrust, not Rust parity. Windows 01 and 02 were released NOT_RUN before start |
| Two-server differential | **NOT_RUN**, and not yet approved |
| Customer/auth, lifecycle-transition and housekeeping HTTP vectors | **NOT_RUN** (source-derived specs only) |
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
