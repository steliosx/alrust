# Third-party notices

This file records third-party material included in alrust. It does not select
a license for alrust's original code; that remains an owner decision.

## Alerta

Source: https://github.com/alerta/alerta at `f4312727783dad64198b62ed778bbca98a5144a3` (v9.1.0).
The reference schema (`alerta/sql/schema.sql`) is copied into the generated
`crates/alrust-store-pg/migrations/0002_reference_layout.sql` by
`scripts/gen-0002.py`. The complete upstream Apache-2.0 license and NOTICE
are retained without modification in `third_party/alerta/LICENSE` and
`third_party/alerta/NOTICE`. Preserve these files with distributions of the
copied material. The existing migration bytes are unchanged by this notice.

The pinned `reference/alerta` and `reference/alerta-webui` Git submodules retain
their own upstream licensing. Any future vendoring needs its applicable
notices retained with the vendored material.

## BeautifulRust

The imported skills and lint-policy provenance are recorded in
`.agents/skills/README.md`; retain `.agents/skills/LICENSE-BeautifulRust`.
