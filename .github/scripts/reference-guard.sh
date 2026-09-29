#!/usr/bin/env bash
# Enforce that reference/ and .gitmodules hold exactly the approved upstream
# pins (ADR-0001), and that nothing changes them after they exist on the base.
#
#   reference-guard.sh [HEAD_COMMIT] [BASE_COMMIT]
#
# Limitation: under the pull_request event this script and its workflow come
# from the candidate, so a PR can edit the policy together with the pins. Only
# branch protection or a ruleset (owner-controlled) anchors it fully.
set -euo pipefail

head="${1:-HEAD}"
base="${2:-}"

# Approved policy. The tree id fixes exactly two entries under reference/, each
# mode 160000 at the ADR-0001 commit; the blob id fixes the exact .gitmodules bytes.
readonly APPROVED_REFERENCE_TREE=69fd8341776b852c289e5d803983fe2fd4a6ebd5
readonly APPROVED_GITMODULES_BLOB=4c688ccc9b5eb7087edf513d0a062f0b511ed7fc
readonly APPROVED_GITLINKS=$'160000 commit f4312727783dad64198b62ed778bbca98a5144a3\treference/alerta\n160000 commit 2687e4afa93b41c4fa1d46201bba9ea38be0d491\treference/alerta-webui'

fail=0
err() { echo "::error::$*"; fail=1; }

entry() { git ls-tree "$1" -- "$2" | awk '{print $1, $2, $3}'; }

ref_entry="$(entry "$head" reference)"
if [[ "$ref_entry" != "040000 tree $APPROVED_REFERENCE_TREE" ]]; then
  err "reference/ is '${ref_entry:-absent}', expected tree $APPROVED_REFERENCE_TREE"
  git ls-tree -r "$head" -- reference | sed 's/^/  have: /'
fi

gm_entry="$(entry "$head" .gitmodules)"
if [[ "$gm_entry" != "100644 blob $APPROVED_GITMODULES_BLOB" ]]; then
  err ".gitmodules is '${gm_entry:-absent}', expected regular blob $APPROVED_GITMODULES_BLOB"
fi

gitlinks="$(git ls-tree -r --full-tree "$head" | awk -F'\t' '$1 ~ /^160000 /' )"
if [[ "$gitlinks" != "$APPROVED_GITLINKS" ]]; then
  err "repository gitlinks differ from the approved set:"
  printf '%s\n' "$gitlinks" | sed 's/^/  have: /'
fi

if [[ -n "$base" ]] && ! [[ "$base" =~ ^0+$ ]]; then
  if ! git cat-file -e "$base^{commit}" 2>/dev/null; then
    err "base commit $base is not available; refusing to skip the base comparison"
  elif [[ -n "$(git ls-tree "$base" -- .gitmodules reference)" ]]; then
    for p in reference .gitmodules; do
      if [[ "$(git ls-tree "$base" -- "$p")" != "$(git ls-tree "$head" -- "$p")" ]]; then
        err "$p differs from base $base"
      fi
    done
  fi
fi

exit "$fail"
