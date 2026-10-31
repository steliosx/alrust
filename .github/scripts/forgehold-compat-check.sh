#!/usr/bin/env bash
# Check one actions/checkout variant for the Forgehold action-compatibility
# job (FH-ALRUST-COMPAT-1). It reads only; nothing is written.
#
#   forgehold-compat-check.sh <path> <shallow:true|false> <submodules:yes|no> <persist:yes|no>
#
# The checkout at <path> must be at $GITHUB_SHA, shallow or not as stated,
# with both reference/ submodules initialised at the recorded commits when
# <submodules> is yes (and absent when no), and with a persisted credential
# header present when <persist> is yes (and absent when no). Any mismatch
# exits 1 with a reason on stderr.
set -euo pipefail
path=${1:?path}; shallow=${2:?shallow}; subs=${3:?submodules}; persist=${4:?persist}
fail() { echo "forgehold-compat-check.sh: $path: $1" >&2; exit 1; }
want=${GITHUB_SHA:-}
[[ "$want" =~ ^[0-9a-f]{40}$ ]] || fail "GITHUB_SHA missing or not a full 40-hex sha"
head=$(git -C "$path" rev-parse HEAD) || fail "git rev-parse HEAD failed"
[[ "$head" == "$want" ]] || fail "HEAD $head is not GITHUB_SHA $want"
got=$(git -C "$path" rev-parse --is-shallow-repository) || fail "shallow check failed"
[[ "$got" == "$shallow" ]] || fail "shallow is $got, expected $shallow"
status=$(git -C "$path" submodule status) || fail "git submodule status failed"
if [[ "$subs" == yes ]]; then
  for sub in reference/alerta reference/alerta-webui; do
    rec=$(git -C "$path" ls-tree HEAD "$sub" | awk '{print $3}')
    [[ "$rec" =~ ^[0-9a-f]{40}$ ]] || fail "no recorded commit for $sub"
    grep -qx " $rec $sub.*" <<<"$status" || fail "$sub is not initialised at $rec"
  done
else
  ! grep -q '^[ +U]' <<<"$status" || fail "a submodule is initialised"
fi
hdr=$(git -C "$path" config --local --name-only --get-regexp '^http\..*\.extraheader$' || true)
if [[ "$persist" == yes ]]; then
  [[ -n "$hdr" ]] || fail "no persisted credential header"
else
  [[ -z "$hdr" ]] || fail "a credential header is persisted"
fi
