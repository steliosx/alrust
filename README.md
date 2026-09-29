# alrust

Rust rewrite of [Alerta](https://github.com/alerta/alerta) for internal MSP
NOC use: alert aggregation and deduplication for multi-tenant monitoring
sources (Zabbix ≥ 7.0.25, Veeam ONE, generic webhooks), PostgreSQL-only,
API-compatible with alerta-webui, delivered as a server binary plus Tauri
desktop apps for Windows and macOS.

Built entirely by agents: Opus 5.5 (primary) and Codex Sol6.1 (secondary
reviewer), per the plan in [docs/KICKOFF.md](docs/KICKOFF.md).

## Layout

- `crates/` — Rust workspace (domain, engine, store-pg, api, receivers)
- `webui/` — vendored alerta-webui (Vue), lightly patched
- `desktop/` — Tauri shell
- `conformance/` — black-box HTTP conformance suite + differential fuzzer
- `spec/` — specs extracted from the reference Python implementation
- `reference/` — pinned read-only submodules of upstream alerta/alerta-webui
- `docs/` — kickoff, ADRs, status

## License

Internal project. Vendored Alerta code is Apache-2.0; see LICENSE/NOTICE files
in the vendored trees.
