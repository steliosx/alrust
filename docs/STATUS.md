# Status

_Last updated: 2026-09-29 by codex-alrust on the conformance foundation branch._

## Current phase

**Phase 0 in progress; gate open.** Independent review of draft PR #1 at
`a674546648c9ea6fd40e9915b23137b8cfaad150` is REQUEST_CHANGES: the reference
guard trusts a candidate-controlled manifest and ignores extra submodule
settings. The legitimate scaffold passes, but adversarial pin/contents changes
also pass when accompanied by manifest edits. Claude owns the guard correction
and source-cited spec extraction. Codex coordinates and owns the independent
review and conformance foundation; Claude reviews Codex-authored harness changes.

| Phase | State | Gate |
|---|---|---|
| 0 Spec + conformance | harness foundation implemented; source spec pending | open; hosted CI blocked by billing |
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
2. Clock normalization and stateful differential coverage await source/spec and
   Claude review. Python 3.12 + pytest/httpx/Hypothesis selected (ADR-0002).
3. Mailbox: fresh Alrust identities; Claude's exact owner-relayed public file
   verified and pinned by Codex. Codex pin relay, peer concurrence on the
   owner-authorized reconciled charter and signed round trip remain pending.
   The latest owner charter supersedes proposed merge rights and fixed models;
   current account/model settings stay unchanged and all main merges are excluded.

## Operator inputs pending

- **GitHub Actions is blocked by billing.** On 2026-09-29, PR #1's jobs were not
  started: "recent account payments have failed or your spending limit needs to
  be increased". Until this is fixed, no CI gate can pass.
- [PIN] Relay Codex's public pin to Claude. The owner's direct charter already
  authorizes isolated local setup and routine Alrust work. The exact accepted
  rules snapshot is retained/signed outside Git; peer concurrence is distinct.
- [PIN] Captured real Veeam ONE (and ideally Zabbix 7.0 LTS) webhook payloads.
- [PIN] Current production `alertad.conf`.
- [PIN] Code-signing certificates for Windows and macOS.

## Next actions

1. Claude: acknowledge the signed bootstrap/task/source, concur on the reconciled
   charter or state concrete conflicts, then correct the guard and extract the
   source-cited Phase 0 spec. No later-phase gate is closed by scaffold success.
2. Codex: hand off this foundation for Claude's independent review; validate the
   corrected guard at its exact new head; expand cases from the extracted spec.
3. Agree the existing laptop resource window before the supported Python 3.12 /
   PostgreSQL 16 reference run. Empty-crate fmt/clippy/test and offline harness
   checks are LOCAL_PASS; reference service/HTTP equivalence is NOT_RUN in this
   review. The latest hosted run remains CI_BLOCKED_BILLING with no executed
   steps. Local success never satisfies the hosted Phase 0 gate.
4. Check inboxes at task boundaries. If peer execution is idle, record
   WAITING_FOR_PEER and the next action; do not add a poller/timer/launcher.
   Checkpoint at five substantive cycles; report unresolved blockers by ten.
