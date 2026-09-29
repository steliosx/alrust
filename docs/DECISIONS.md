# Decisions (ADR index)

| ADR | Title | Status | Date |
|---|---|---|---|
| [0001](adr/0001-reference-version-pins.md) | Reference version pins (Alerta server + webui) | Accepted; guard correction approved by Codex at `e839ceaf`; **not enforced** (no repo protection, HTTP 403) | 2026-09-29 |
| [0002](adr/0002-conformance-suite-language.md) | Conformance suite language | Selected by Codex: Python 3.12 + pytest + httpx + Hypothesis (ADR text lands with `codex/conformance-harness`) | 2026-09-29 |
| [0003](adr/0003-agent-collaboration.md) | Agent collaboration channel | Accepted; agreed rules `abad4f68…`, signed round trip complete | 2026-09-29 |
| [0004](adr/0004-webui-vendoring-source.md) | Which alerta-webui source to vendor (v8.7.1 plus #575 vs `e6eb99e`) | Proposed; pending Codex review and build evidence; a re-pin would be the owner's call | 2026-09-29 |

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
