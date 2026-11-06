#!/usr/bin/env bash
# R-NOTICE-1: read-only check of the Rust notice corpus (third_party/rust).
# SHA256SUMS binds the corpus to Cargo.lock, the Cargo manifests and the
# toolchain file. A change to any of them, or to the corpus, must fail here.
# Fix a failure by regenerating and reviewing the corpus, never by editing
# SHA256SUMS alone. This script writes nothing in the checkout and needs no
# build, Docker or network.
set -euo pipefail

cd -- "$(git rev-parse --show-toplevel)"
sums=third_party/rust/SHA256SUMS
work=$(mktemp -d -- "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/notice-corpus.XXXXXXXX")
trap 'rm -rf -- "$work"' EXIT

# 1. Every listed file matches its checksum; a malformed line fails (--strict).
sha256sum --check --strict --quiet -- "$sums"

# 2. The listed paths must be exactly the tracked corpus files (except
#    SHA256SUMS itself) plus these build inputs. The input list is fixed here,
#    never derived from SHA256SUMS. Missing, extra or duplicate rows fail.
#    Each producer runs in the foreground so that its failure stops the script.
git ls-files -- third_party/rust >"$work/tracked"
grep -vxF -- "$sums" "$work/tracked" >"$work/corpus"
cat -- "$work/corpus" - >"$work/expected-unsorted" <<'INPUTS'
Cargo.lock
Cargo.toml
rust-toolchain.toml
crates/alrust-api/Cargo.toml
crates/alrust-domain/Cargo.toml
crates/alrust-engine/Cargo.toml
crates/alrust-receivers/Cargo.toml
crates/alrust-server/Cargo.toml
crates/alrust-store-pg/Cargo.toml
INPUTS
LC_ALL=C sort -- "$work/expected-unsorted" >"$work/expected"
sed -E 's/^[0-9a-f]{64}  //' -- "$sums" >"$work/listed-unsorted"
LC_ALL=C sort -- "$work/listed-unsorted" >"$work/listed"
diff -- "$work/expected" "$work/listed"
echo "notice corpus: $(wc -l <"$work/listed") checksummed paths match"
