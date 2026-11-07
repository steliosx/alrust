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

## Rust dependencies and standard library

`third_party/rust/` retains legal texts and attributed source excerpts from the
static default-feature x64 server normal/build package selection. Its manifest
records exact crate archive checksums, original paths/line intervals and literal
text hashes/bytes. Host build and inactive-feature material may be included;
this is not a linked-binary SBOM or complete licensing clearance. Preserve AND
expressions, legacy declarations and all included notices without selecting an
Alrust license by implication. See its README for scope and regeneration review.

The proposed Docker build checks this corpus against Cargo.lock and copies it
to `/usr/share/licenses/alrust/rust/`. It also retains the actual builder sysroot's
`COPYRIGHT-library.html` and license directory under
`/usr/share/licenses/alrust/rust-standard-library/`. Actual image contents and
toolchain/base-image obligations still require distribution review. Debian's
existing copyright files remain present; this notice is not their replacement.
