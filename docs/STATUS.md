# Status

_Last updated: 2026-09-29T22:35Z by claude-alrust, the sole writer of this
file and DECISIONS.md. Codex sends corrections through the mailbox._

## Current phase

**Phase 0 in progress. The gate is open.**

| Phase | State | Gate |
|---|---|---|
| 0 Spec + conformance | Spec slices and harness foundation under review; reference smoke window 03 acknowledged | Open. No suite is green in CI; hosted CI never executed |
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
| `claude/spec-lifecycle-receive` | claude | `a1de491c6677a3134c65d0e0b086cae08b925087` | LIFECYCLE-06/07 corrections submitted; awaiting review (the previous head `3d522225` got REQUEST_CHANGES) |
| `claude/charter-sync` | claude | this head | CHARTER-02 correction submitted; awaiting review (the previous head `484286c1` got REQUEST_CHANGES) |
| ALRUST-P0-CLOCK-01 policy | codex | proposal `ff2082ae…` | Claude review in progress |

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
| Reference HTTP smoke (Python 3.12 / Postgres 16, one reference) | **NOT_RUN**. Window `alrust-ref-smoke-20260929-03` is acknowledged, pending start and release. Windows 01 and 02 were released NOT_RUN before start |
| Two-server differential | **NOT_RUN**, and not yet approved |
| FAIL results on record | none executed. The pre-fix guard bypasses reproduced by Codex at `a674546` were a review FAIL, fixed at `e839ceaf` |

The informal smoke run of 2026-09-29, made before the charter on host Python
3.14, is superseded. It does not count as evidence.

## Owner items

- **GitHub Actions billing:** every job is refused before start.
- **Repository protection:** the API returns HTTP 403 (plan). The guard is
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

- **codex-alrust:** run window 03 (reference smoke), then send WINDOW_RELEASE;
  review the lifecycle and charter heads; revise the clock policy after
  Claude's review.
- **claude-alrust:** review the clock policy; then the lifecycle action-path
  spec (ALRUST-P0-SPEC-01 step 2); keep STATUS current.
