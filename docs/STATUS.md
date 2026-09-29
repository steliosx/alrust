# Status

_Last updated: 2026-09-29 by claude-alrust._

## Current phase

**Pre-Phase 0.** Kickoff steps 1–2 (scaffold, CI, reference pins) are done on
branch `claude/scaffold` and await Codex review. Phase 0 (spec extraction and
conformance harness) **has not started**. It is waiting on the collaboration
channel and on Codex.

| Phase | State | Gate |
|---|---|---|
| 0 Spec + conformance | not started | — |
| 1 Domain/engine/store | blocked on 0 | — |
| 2 HTTP API | blocked on 1 | — |
| 3 Auth | blocked on 2 | — |
| 4 Receivers | blocked on 3; [PIN] payloads | — |
| 5 Ops | blocked on 4; [PIN] alertad.conf | — |
| 6 Tauri desktop | blocked on 2; [PIN] signing certs | — |

## What exists

- Cargo workspace with the five crates as empty libs; `fmt`, `clippy -D
  warnings` and `test` pass on Rust 1.98.1.
- `reference/alerta` @ v9.1.0 and `reference/alerta-webui` @ v8.7.1 (ADR-0001).
- CI (`.github/workflows/ci.yml`) has three jobs. `reference-guard` checks pins,
  upstream URLs and unexpected files. `rust` runs fmt, clippy and test with a
  Postgres 16 service. `conformance-reference` boots the reference server on
  Postgres 16 and runs a healthcheck smoke test; the suite itself comes later.
- `conformance/scripts/run-reference.sh` boots the reference server locally
  or in CI. It was verified locally on 2026-09-29: alert create and
  `/alerts/count` work.

## Open questions

1. webui: vendor v8.7.1 plus a cherry-pick of #575 (Keycloak fix), or a
   pinned `master` commit? (ADR-0001, decide before Phase 2)
2. Conformance suite language (ADR-0002, Codex decides).
3. Collaboration rules and mailbox (ADR-0003, owner approves, Codex concurs).

## Operator inputs pending

- **GitHub Actions is blocked by billing.** On 2026-09-29, PR #1's jobs were not
  started: "recent account payments have failed or your spending limit needs to
  be increased". Until this is fixed, no CI gate can pass.
- [PIN] Mailbox bootstrap: relay key pins between claude-alrust and
  codex-alrust (see `/home/user/claude-alrust-mailbox/BOOTSTRAP-REQUEST.md`)
  and approve `RULES.md`.
- [PIN] Captured real Veeam ONE (and ideally Zabbix 7.0 LTS) webhook payloads.
- [PIN] Current production `alertad.conf`.
- [PIN] Code-signing certificates for Windows and macOS.

## Next actions

1. Codex reviews the scaffold PR.
2. Complete the mailbox bootstrap.
3. Start Phase 0: spec extraction (claude-alrust) and conformance harness
   (codex-alrust) in parallel.
