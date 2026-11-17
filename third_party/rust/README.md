# Rust dependency notice corpus

This directory retains unmodified legal-text files and literal attributed source
excerpts from 157 authenticated crates selected by the static default-feature
`alrust-server` normal/build graph for `x86_64-unknown-linux-gnu`. There are 138
target-role packages and 122 host-role packages, with 103 in both roles. Host
build/proc-macro and inactive-feature notices are conservatively retained.
This is not a list of 157 libraries linked into the binary, a binary SBOM, or
complete distribution or legal clearance. This corpus does not select alrust's
own licence; that is Apache-2.0 (DECISIONS Y58), in `LICENSE` and `NOTICE`.

`manifest.json` binds every archive to its Cargo.lock checksum and each retained
text to its literal SHA256 and byte count. Expressions are copied as declared:
AND terms and legacy slash forms are preserved, and no OR alternative is chosen.
Nested files, UNLICENSE/AUTHORS/COPYRIGHT files and separately attributed source
notices are included. Excerpts retain original comment markers and exact original
line bytes; their complete source member hash and line interval are recorded.
Some excerpts concern code behind inactive features (including axum WebSockets
and uuid JavaScript randomness); retaining them is not a claim that code ships.

The Docker build verifies `SHA256SUMS` from the repository root before compiling.
It binds Cargo.lock, all seven Cargo manifests, rust-toolchain.toml,
`.cargo/config.toml`, this manifest
and all retained texts. The Docker build selects alrust-server's default features
and explicitly targets x86_64-unknown-linux-gnu, refusing a different builder host.
A dependency/feature/
target change needs a new resolved selection and affected notice/source review;
do not merely rewrite the lock checksum to bypass a stale corpus. Reconstruct
texts from the exact public archive URLs/checksums and original member/line
selections in the manifest. Also review bundled third-party material and source
headers; filename or SPDX metadata searches alone are insufficient.

The runtime image copies this corpus and the build toolchain's standard-library
copyright document and license directory. The latter is selected from the actual
builder sysroot, not from this Cargo.lock inventory. The pinned builder still
requires actual image/build validation; local source inspection is not proof of
the shipped image's contents. Debian/base-image/CA certificate material, Alerta,
reference Python environments and any future vendored web UI have their separate
licensing and distribution scopes. Existing Debian copyright files are retained.

Before any binary/image release, verify actual artifacts, enabled features,
standard library/native/base-image materials and applicable obligations under the
owner-selected license. This corpus does not establish compatibility or replace
that review. All 19 conformance profiles remain NOT_RUN; ARM64 is deferred.
