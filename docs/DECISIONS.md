# Decisions (ADR index)

| ADR | Title | Status | Date |
|---|---|---|---|
| [0001](adr/0001-reference-version-pins.md) | Reference version pins (Alerta server + webui) | Accepted, pending Codex review | 2026-09-29 |
| [0002](adr/0002-conformance-suite-language.md) | Conformance suite language | Proposed, pending Codex concurrence | 2026-09-29 |
| [0003](adr/0003-agent-collaboration.md) | Agent collaboration channel | Proposed, pending owner approval | 2026-09-29 |

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
