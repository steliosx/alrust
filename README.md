# alrust

<p align="center">
  <img src="docs/assets/readme-hero.png" alt="alrust — Many signals. One clear view. Planned, Phase 0." width="1280">
</p>

**A planned alert aggregation and deduplication server for an MSP network
operations centre: a Rust rewrite of [Alerta](https://github.com/alerta/alerta).
Its goal is to be API-compatible with the Alerta API as the existing Alerta
web UI and our scripts use it.**

> **Project stage: planning and Phase 0 (specification and conformance
> harness).** There is no working alrust server, API or desktop app yet. The
> status table below separates what exists from what is planned.

[Project website](https://alrust.stel.chatgpt.site/) ·
[What it is](#what-it-is) ·
[Status](#status) ·
[Architecture](#architecture-planned) ·
[Getting started](#getting-started) ·
[Roadmap](#roadmap) ·
[How work is done](#how-work-is-done) ·
[Licensing](#licensing)

---

## What it is

The goal: alrust **will** receive alerts from many monitoring systems, such
as per-tenant Zabbix instances, Veeam ONE and generic webhooks. It will
deduplicate and correlate them, and present them to NOC operators through
the existing Alerta web UI, wrapped as Windows and macOS desktop apps. None
of this runs yet.

These are the planned design decisions, fixed in the kickoff:

| Design choice | Decision (planned) |
|---|---|
| Users | Internal NOC operators only. `customer` is a grouping and routing attribute. |
| Backend | Rust (axum, tokio, sqlx) on PostgreSQL only |
| Compatibility target | The Alerta API as the Alerta web UI and our scripts actually use it; full documented API parity is not a goal. To be verified against a pinned upstream release; not yet verified |
| Frontend | The upstream Alerta web UI, vendored and lightly patched. There is no frontend rewrite. |
| Desktop | Tauri wrapper with native notifications from a new server-sent-events endpoint |
| Inbound | Native Zabbix ≥ 7.0.25 webhook receiver, and a configurable generic webhook mapper (used for Veeam ONE) |

The full plan and its fixed decisions are in the
[project kickoff](https://github.com/steliosx/alrust/blob/e839ceaf8cb43d6ed996c8fe12f884d0bb141743/docs/KICKOFF.md)
(pinned to the reviewed scaffold revision).

## Status

**Current checkpoint: 7 October 2026**, inspected at main
[`7c07e52`](https://github.com/steliosx/alrust/tree/7c07e52f27b88b8c5a3b38d9f4cd73fff5f89554).
The scaffold, extracted specifications and conformance tooling are merged.
**Phase 0 remains open; no Rust server or two-server conformance result is claimed.**

| Area | Current evidence | What it does not establish |
|---|---|---|
| CI bootstrap and scaffold | [Run 37696114824](https://github.com/steliosx/alrust/actions/runs/37696114824) passed all four jobs at `7c07e52`: reference guard, Rust formatting/lint/build checks, inert profile preflight and supervised reference-server bootstrap. | Rust targets still contain zero tests; no HTTP conformance vectors run in this workflow. |
| Supervised reference process | [PR #88](https://github.com/steliosx/alrust/pull/88) integrates the supervisor/launcher into hosted CI. The reference job observes a healthy Python server, then verifies `tree_empty / COMPLETE` and no remaining listener after shutdown. | This is reference bootstrap and cleanup evidence, not an implemented Alrust server or full-suite conformance. |
| Profile inventory and entrypoint | All **19 profiles return `NOT_RUN`**, with execution disabled, in the current preflight job. | A green preflight gives no Phase 0 execution credit. Each enabling head and the complete reference suite still need qualification. |
| Launch and observer interoperability | [PR #98](https://github.com/steliosx/alrust/pull/98), head `effe4099`, adds opt-in launch journals and launch confirmation. Codex-authored [#99](https://github.com/steliosx/alrust/pull/99), head `583cd721`, is merged after Claude's reported exact-head component review; it corrects the receipt reader's socket path and adds synthetic producer/consumer controls. Source inspected. | Synthetic HTTP stand-ins and offline components do not establish actual reference-observer capture, SQL qualification or complete Phase 0 execution. |
| Evidence finalizer and database lifecycle | Finalizer components are merged. [PR #100](https://github.com/steliosx/alrust/pull/100), head `d9ad6378`, adds database creation, guarded drop and finalizer linkage. Its three automatic P1 findings (the SQL port bound to the inspected container; required container evidence; the drop verdict bound to its stage) are fixed by [PR #109](https://github.com/steliosx/alrust/pull/109), head `54cebbb0`, merged after Codex's source/offline approval. | CI does not execute these component unit tests or qualify these database-lifecycle operations against real PostgreSQL; the fixes have source/offline evidence only. No workflow calls the finalizer yet; see [interfaces](conformance/finalizer-interfaces.md). |
| Local reference and offline checks | Scoped results and attributed independent dispositions are recorded in [STATUS](docs/STATUS.md). | Local passes and published review summaries are separate from CI qualification, direct inspection of signed private records and cross-implementation parity. |
| Open proposals | [PR #71](https://github.com/steliosx/alrust/pull/71) proposes structural OpenAPI validation; draft [PR #70](https://github.com/steliosx/alrust/pull/70) addresses batch retry safety. | Neither proposal is part of this main checkpoint. The retry contract needs an owner decision. |

Hosted jobs are now executing; earlier billing-refused runs remain historical
failures to execute. The cost policy is **no paid CI**: free hosted allowance
and the authorised self-hosted x64 path, subject to its own qualification.
See [DECISIONS](docs/DECISIONS.md) for the current policy and merge authority.

<details>
<summary>Historical checkpoint — 29 September 2026</summary>

**Historical snapshot: 2026-09-29.** This table preserves the original
checkpoint. The scaffold, specifications and conformance tooling have since
landed on `main`; consult [the current status record](docs/STATUS.md) for
subsequent merges, scoped results and the Phase 0 gate. The illustrations
describe the planned system and do not establish implementation or test results.

In the snapshot below, "unpublished" means the work existed and had been
independently reviewed, but had not yet been pushed to this repository.

| Area | State | Evidence |
|---|---|---|
| Repository scaffold (Rust workspace, CI, pinned upstream references) | Open draft [PR #1](https://github.com/steliosx/alrust/pull/1) at [`e839ceaf`](https://github.com/steliosx/alrust/commit/e839ceaf8cb43d6ed996c8fe12f884d0bb141743) | The five Rust crates are **empty libraries with zero tests**. That is scaffold only, not a working server. |
| Guard keeping `reference/` read-only | Implemented at `e839ceaf`; independently reviewed | Local checks pass. **Not enforced by GitHub**: the repository has no branch protection. |
| Hosted CI | **Blocked by account billing** | [Run 36632329142](https://github.com/steliosx/alrust/actions/runs/36632329142): jobs were never started, so no step executed. This is not a code test result. |
| Phase 0 specifications (alert lifecycle, customer mapping) | Unpublished; independently reviewed | Derived from the pinned upstream source. They are not yet checked against a running server. |
| Conformance harness (black-box Python test suite) | Unpublished; independently reviewed | 40 offline self-tests pass locally. |
| Reference smoke test (2 HTTP cases against the upstream Python server) | Passed locally | This checks the upstream **reference** server only. It is not alrust behaviour. |
| Customer, lifecycle and two-server conformance runs | Not run | — |
| alrust HTTP API, receivers, auth, desktop app | Not started | Planned phases 1–6 below |

</details>

## Architecture (planned)

Nothing in this diagram runs yet. By design, `alrust-engine` is a pure
library with no I/O: it computes lifecycle decisions, and the surrounding
server code (orchestration) does the database reads and writes through
`alrust-store-pg`.

<p align="center">
  <img src="docs/assets/architecture-planned.svg" alt="Planned Alrust architecture. Receivers feed API orchestration, which separately calls the pure engine and reads or writes PostgreSQL. The Alerta UI and Tauri desktop consume the API; SSE and outbound webhooks deliver notifications. All runtime components are planned." width="1280">
</p>

<details>
<summary>Editable Mermaid architecture</summary>

```mermaid
flowchart LR
    Z[Zabbix 7.0 per tenant] -->|webhook| R
    V[Veeam ONE] -->|webhook via generic mapper| R
    O[Other sources] -->|generic webhook| R
    subgraph srv [alrust server - planned]
        R[alrust-receivers] --> A
        A["alrust-api: Alerta-compatible HTTP and SSE, orchestration"] -->|calls| E["alrust-engine: pure library, no I/O - dedup, correlate, lifecycle"]
        A -->|reads and writes| S[("alrust-store-pg: PostgreSQL")]
    end
    W[Alerta web UI, vendored] --> A
    D[Tauri desktop app] --> W
    A -->|SSE alert stream| D
    A -->|outbound webhooks| F[Notification targets]
```

</details>

Compatibility is judged against pinned upstream releases:
[alerta v9.1.0](https://github.com/alerta/alerta/tree/f4312727783dad64198b62ed778bbca98a5144a3)
and
[alerta-webui v8.7.1](https://github.com/alerta/alerta-webui/tree/2687e4afa93b41c4fa1d46201bba9ea38be0d491).

## Getting started

**The repository now contains the scaffold, specifications and conformance
tooling. There is no working alrust server to start.** See
[the current status record](docs/STATUS.md) for the latest development state.

```sh
git clone https://github.com/steliosx/alrust.git   # private repository: needs access
cd alrust
```

**Optional: inspect the original scaffold snapshot** from PR #1, pinned to
its reviewed commit:

```sh
git fetch origin pull/1/head
git switch --detach e839ceaf8cb43d6ed996c8fe12f884d0bb141743
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace               # 0 tests: the crates are empty
```

What has been checked:
- **The git steps** were verified in a fresh clone to land on `e839ceaf`.
- **The three `cargo` commands** passed with Rust 1.98.1 at the scaffold's
  earlier commit `a674546`. `Cargo.toml`, `Cargo.lock`,
  `rust-toolchain.toml`, `rustfmt.toml` and `crates/` are unchanged at
  `e839ceaf`, so that result carries over. They were not re-run for this
  README. They show only that the empty, zero-test scaffold compiles, not
  anything about compatibility.

The upstream reference sources are git submodules
(`git submodule update --init`). The cargo checks don't need them, and that
step was not run as part of validating this README.

## Roadmap

Each phase is **planned** and has a gate that must pass before dependent work
starts. The details are in the
[kickoff](https://github.com/steliosx/alrust/blob/e839ceaf8cb43d6ed996c8fe12f884d0bb141743/docs/KICKOFF.md).

<p align="center">
  <img src="docs/assets/roadmap-planned.svg" alt="Planned roadmap: Phase 0 specify and prove; phases 1–2 build and compare; phases 3–5 connect and qualify; phase 6 desktop. Phase 0 is in progress and its gate remains open." width="1280">
</p>

| Phase | Scope | Gate |
|---|---|---|
| 0 | Specs extracted from upstream source; conformance suite and differential fuzzer | Suite green against the upstream server **in CI**: GitHub-hosted, or the self-hosted x64 runner once the free hosted allowance is used up (owner decision X5); lifecycle spec reviewed. Open: local runs do not substitute for a CI run |
| 1 | Domain model, pure lifecycle engine, PostgreSQL store | Property tests and spec-derived vectors pass |
| 2 | HTTP API including the query grammar, plus a new SSE endpoint | Conformance suite green against alrust; the differential fuzzer shows no unexplained divergence over a long run; the vendored web UI works against alrust with no code changes beyond configuration |
| 3 | API keys (one per tenant source) and OIDC for operators | Auth conformance; web UI login |
| 4 | Zabbix receiver, generic webhook mapper, outbound forwarder | Replay of real captured payloads *(needs operator-supplied samples)* |
| 5 | Housekeeping, per-tenant heartbeat alerts, metrics, config, Docker, shadow-run tooling | One-week shadow run with explained diffs ≈ 0 *(needs production config)* |
| 6 | Tauri desktop apps for Windows and macOS | Both build in CI and receive live notifications *(signed builds need certificates)* |

## How work is done

- Two agents collaborate. One authors each change, and the other reviews that
  exact commit independently. Every new commit needs a fresh review.
- Changes go on isolated branches and are delivered as pull requests or
  signed bundles, one concern each.
- Reviews are recorded as signed dispositions and PR comments. Both agents
  share one GitHub login, so they are **not** formal GitHub approvals.
- Claude merges a pull request into `main` once the other agent has
  approved its exact head, has admitted a signed merge window for that
  head, and Claude's advisor has no objection to that exact head (owner
  decisions Y5 and Y20). **Only the repository owner** cuts releases and
  decides on billing, repository settings, branch protection, security,
  production and cutover.
- Behaviour must match the pinned upstream release. Any intentional
  difference in alert lifecycle, authentication or data retention is
  recorded in an ADR and decided by the owner.

## Licensing

- alrust is licensed under the [Apache License, Version 2.0](LICENSE)
  (owner decision, DECISIONS Y58). See [NOTICE](NOTICE) for the parts
  derived from Alerta and their attribution.
- The upstream Alerta sources that alrust is checked against keep their own
  licences, and vendored code keeps its notices:
  - Alerta server:
    [LICENSE](https://github.com/alerta/alerta/blob/f4312727783dad64198b62ed778bbca98a5144a3/LICENSE),
    [NOTICE](https://github.com/alerta/alerta/blob/f4312727783dad64198b62ed778bbca98a5144a3/NOTICE)
    (Apache-2.0);
  - Alerta web UI:
    [LICENSE](https://github.com/alerta/alerta-webui/blob/2687e4afa93b41c4fa1d46201bba9ea38be0d491/LICENSE)
    (Apache-2.0).
