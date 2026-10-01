# Decisions (ADR index)

| ADR | Title | Status | Date |
|---|---|---|---|
| [0001](adr/0001-reference-version-pins.md) | Reference version pins (Alerta server + webui) | Accepted; guard correction approved by Codex at `e839ceaf`; **not enforced** (no repo protection, HTTP 403) | 2026-09-29 |
| [0002](adr/0002-conformance-suite-language.md) | Conformance suite language | Selected by Codex: Python 3.12 + pytest + httpx + Hypothesis (ADR text lands with `codex/conformance-harness`) | 2026-09-29 |
| [0003](adr/0003-agent-collaboration.md) | Agent collaboration channel | Accepted; agreed rules `abad4f68…`, signed round trip complete | 2026-09-29 |

## Owner decisions: Linux x64 CI and beta (2026-10-01, given in Claude's session)

| # | Question | Decision |
|---|---|---|
| X1 | Phase A (Zabalack runner 22 as the only tenant; no broker, registration or new credential) | **Approved**, conditional on codex-omarchy approving the exact script package; the preflight must confirm runner 22 is the only runner on the VM |
| X2 | Phase B (shared-ci broker and runner units) | **Approved in principle only.** Installation needs a separate final owner approval of a package in which both Codex reviewers approve the same broker revision, Zabalack's C6 has retired the in-job legacy lock and the legacy lock file is root-owned, units, provisioning and rollback are reviewed, and the work runs in an announced maintenance window with no CI jobs running |
| X3 | Runner registrations | One repository-level runner for `steliosx/alrust`, registered **only during the Phase B installation**, under a dedicated non-login account with automatic updates disabled. Zabalack reconfigures runner 22 instead of adding a runner. The owner generates registration tokens and uses them directly on the VM; never in Git, chat or the mailbox |
| X4 | Quarantine clearance | The owner alone, after a written Codex disposition. No agent may clear a quarantine, and no agent clearance tool is built. This restriction covers clearance only: approved, reviewed scripts still run on the VM through the `zabadmin` account, with their output captured. Delegation may be revisited once a reviewed clearance script exists |
| X5 | Phase 0 CI gate | Use the free monthly GitHub-hosted allowance, then the self-hosted x64 VM; runs on either count, but only for the exact commit, a job that actually ran (queued or refused jobs do not count), with logs and evidence kept and the `reference/` guard included. **No paid usage.** Routing is an automatic check: a first job reads billing usage with a fine-grained, read-only token (billing "Plan" read only) stored as a repository secret, created by the owner; if the check fails or is ambiguous, jobs go to the self-hosted VM; the spending limit stays at $0 |
| X6 | Alrust beta | The Linux x64 server, built in CI from an exact commit, passing the Phase 0 to 5 gates. The desktop application (Phase 6) and ARM64 are excluded |
| X7 | Merging the x64 workflow to `main` | The owner merges after Codex approves the workflow at its exact commit (PR #1, its base, merges first). Until the Alrust runner exists, jobs routed to it wait in the queue, and a queued job is not validation |
| X8 | `codex/*` push cadence | Same rules as `claude/*`: push reviewed branches after an approving verdict or every 4 to 5 mailbox cycles; one pull request per branch, draft until its exact commit is approved; no force-push or rebase; merging stays with the owner. **Takes effect when the owner gives this instruction to Codex directly** (ST46-C1) |

## Pinned versions (quick reference)

| Component | Pin | Commit | Released | Recorded |
|---|---|---|---|---|
| Alerta server (`reference/alerta`) | v9.1.0 | `f4312727783dad64198b62ed778bbca98a5144a3` | 2026-03-28 | 2026-09-29 |
| alerta-webui (`reference/alerta-webui`) | v8.7.1 | `2687e4afa93b41c4fa1d46201bba9ea38be0d491` | 2024-04-11 | 2026-09-29 |
| Zabbix | ≥ 7.0.25 (7.0 LTS) | — | — | 2026-09-29 |
| Veeam ONE | _not yet consulted_ (Phase 4) | — | — | — |
| Rust toolchain | 1.98.1 | — | 2026-09-01 | 2026-09-29 |

## Dropped API surface

None yet. Each endpoint or query operator dropped because nothing consumes it
gets its own ADR and a row here.
