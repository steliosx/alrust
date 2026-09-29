# Status

_Last updated: 2026-09-29T21:25Z by claude-alrust._

## Current phase

**Phase 0 in progress. The gate is open.**

| Phase | State | Gate |
|---|---|---|
| 0 Spec + conformance | Spec slices and harness foundation under review (see below) | Open. No HTTP conformance has run; hosted CI never executed |
| 1 Domain/engine/store | blocked on 0 | — |
| 2 HTTP API | blocked on 1 | — |
| 3 Auth | blocked on 2 | — |
| 4 Receivers | blocked on 3. Real payloads [PIN]; labeled synthetic fixtures allowed meanwhile | — |
| 5 Ops | blocked on 4. alertad.conf [PIN] | — |
| 6 Tauri desktop | blocked on 2. Signing certs [PIN]; test certs allowed meanwhile | — |

## Work in flight

Each item is reviewed at its exact head. Approval never carries to a new head.

| Item | Author | Head | Review state |
|---|---|---|---|
| PR #1 scaffold + guard fix | claude | `e839ceaf` | Codex APPROVE at `e839ceaf`. Codex REQUEST_CHANGES at the earlier head `a674546` (guard bypasses). Draft; the `main` merge is the owner's |
| Conformance foundation, `codex/conformance-harness` | codex | `f357d696` | Claude APPROVE_WITH_COMMENTS. H1 (report lost on provenance failure) and H2 (server-clock field policy) must be fixed before the first real two-server run |
| `spec/customer-mapping.md` | claude | branch `claude/spec-customer-mapping` | corrections for CUSTOMER-01/02 submitted |
| `spec/lifecycle.md`, receive slice | claude | branch `claude/spec-lifecycle-receive` | corrections for LIFECYCLE-01..05 submitted |
| collaboration docs | claude | branch `claude/charter-sync` | correction for CHARTER-01 submitted |

## Checks

Hosted checks, local checks and live HTTP runs are recorded separately. A
local pass never satisfies a hosted gate.

| Check | Hosted (GitHub Actions) | Local |
|---|---|---|
| reference-guard | **CI_BLOCKED_BILLING**: never started | LOCAL_PASS. 12/12 regression cases, verified by both agents |
| fmt / clippy / test | **CI_BLOCKED_BILLING** | LOCAL_PASS. The crates are empty, so this proves no behavior |
| conformance, offline infrastructure | not wired | LOCAL_PASS. 10/10 harness unit tests on Python 3.12, reproduced by Claude |
| conformance vs reference over HTTP (Python 3.12 / Postgres 16) | **CI_BLOCKED_BILLING** | **NOT_RUN**. This waits for a signed heavy-test window |

The informal smoke run of 2026-09-29, made before the charter on host Python
3.14, is superseded. It does not count as evidence.

**Billing blocker** (account, not code): every job has been refused before it
started: "recent account payments have failed or your spending limit needs to
be increased". The owner resolves this. No gate is disabled and no runner is
registered to work around it. Reading branch protection returns HTTP 403 (plan
upgrade required), so the guard's anchoring limitation (ADR-0001) is also an
owner item.

## Collaboration

The signed channel is live (ADR-0003). The agreed rules are `abad4f68…`. The
heavy-window protocol `a5f4dbd4…` is agreed, and no window has been granted
yet.

## Open questions

1. webui: the v8.7.1 tag plus a specific fix, or an exact upstream commit?
   ADR-0004 will compare the two before the Phase 2 dependency.
2. Clock normalization policy for the differential runner. Codex owns it and
   Claude reviews it (harness finding H2).
3. The unknown-severity response (500 in the reference) is reproduced as it is.
   Any divergence goes to the owner (lifecycle L1).

## Operator inputs pending

- GitHub Actions billing, and the branch-protection plan.
- [PIN] Real Veeam ONE (and ideally Zabbix 7.0 LTS) webhook payloads, via an
  owner-approved private path.
- [PIN] Production `alertad.conf`.
- [PIN] Code-signing certificates for Windows and macOS. Production private
  keys are never requested through Git, chat, or the mailbox.

## Next actions

1. Codex re-reviews the three corrected spec/doc heads.
2. First heavy window, ALRUST-REF-SMOKE-01: a signed request, grant and
   acknowledgment come first, then the reference run on Python 3.12 /
   Postgres 16.
3. claude-alrust, Phase 0: the lifecycle action path, then timeout/expiry and
   heartbeats, then the OpenAPI description as implemented.
