# Status

_Last updated: 2026-09-29T20:20Z by claude-alrust._

## Current phase

**Phase 0 in progress, not gated.** The scaffold (kickoff steps 1–2) is in PR #1
(`claude/scaffold` @ `a674546`), which is awaiting Codex review. claude-alrust
is starting spec extraction. codex-alrust owns the conformance harness.

| Phase | State | Gate |
|---|---|---|
| 0 Spec + conformance | in progress | not met: the suite does not exist, hosted CI is unexecuted, and Codex's lifecycle review is pending |
| 1 Domain/engine/store | blocked on 0 | — |
| 2 HTTP API | blocked on 1 | — |
| 3 Auth | blocked on 2 | — |
| 4 Receivers | blocked on 3; real payloads [PIN], synthetic fixtures allowed meanwhile | — |
| 5 Ops | blocked on 4; alertad.conf [PIN] | — |
| 6 Tauri desktop | blocked on 2; signing certs [PIN], test certs allowed meanwhile | — |

## Checks

Hosted checks and local checks are tracked separately. A local pass never
satisfies a hosted gate.

| Check | Hosted (GitHub Actions) | Local |
|---|---|---|
| reference-guard | **unexecuted**: billing blocker | pass (manual run of the same shell logic, 2026-09-29) |
| fmt / clippy / test | **unexecuted**: billing blocker | pass on Rust 1.98.1 (empty crates, so this proves no behavior) |
| conformance vs reference | **unexecuted**: billing blocker | reference server boots on Postgres 16 and answers a smoke check; no suite exists yet |

**Billing blocker** (account, not code): every job in runs `36621806737` and
`36621879628` was refused before start with "recent account payments have
failed or your spending limit needs to be increased". This is for the owner to
resolve. No gate is disabled and no runner is registered to work around it.

## What exists

- Cargo workspace with the five crates as empty libs.
- `reference/alerta` @ v9.1.0 and `reference/alerta-webui` @ v8.7.1 (ADR-0001).
- CI with three jobs (see `.github/workflows/ci.yml`).
- `conformance/scripts/run-reference.sh`, which boots the pinned reference server.

## Open questions

1. webui: the v8.7.1 tag plus a specific fix, or an exact upstream commit?
   ADR-0004 will compare the two before the Phase 2 dependency. Codex reviews
   compatibility and evidence.
2. Conformance suite language (ADR-0002): Codex decides.
3. The agreed charter hash (ADR-0003) must be recorded by both agents.

## Operator inputs pending

- Relay of the codex-alrust key pin to claude-alrust (identity, key ID, full
  public key, exact public-file SHA-256).
- GitHub Actions billing.
- [PIN] Real Veeam ONE (and ideally Zabbix 7.0 LTS) webhook payloads, via an
  owner-approved private path.
- [PIN] Production `alertad.conf`.
- [PIN] Code-signing certificates for Windows and macOS. Production private
  keys are never requested through Git, chat, or the mailbox.

## Next actions

1. Codex reviews PR #1 @ `a674546`.
2. Owner relays the codex-alrust pin, then the first signed round trip happens.
3. claude-alrust: Phase 0 spec extraction, starting with
   `spec/customer-mapping.md`.
