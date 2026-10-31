#!/usr/bin/env bash
# Bind a Forgehold-dispatched checkout to the expected commit (FH-ALRUST-BIND-1).
#
#   forgehold-bind.sh <job-name> [expected-commit]
#
# The expected commit is the argument, else $GITHUB_SHA (set by the runner to
# the dispatched commit). It must be a full 40-hex SHA and must equal HEAD, and
# the checkout must be clean; every git inspection must itself succeed and
# yield a well-formed value (FH-BIND-OBSERVE-1). Otherwise this refuses
# (exit 1) and prints an RQ_JSON line with result FAIL. On success it prints
# the RQ_JSON checkout line carrying commit, tree, ref, run and attempt.
# Nothing is written.
set -euo pipefail
job=${1:?job name}
expected=${2:-${GITHUB_SHA:-}}
refuse() {
  echo "RQ_JSON {\"job\":\"$job\",\"stage\":\"checkout\",\"result\":\"FAIL\",\"reason\":\"$1\"}"
  echo "::error::$1" >&2
  exit 1
}
[[ "$expected" =~ ^[0-9a-f]{40}$ ]] || refuse "expected commit missing or not a full 40-hex sha"
actual=$(git rev-parse HEAD 2>/dev/null) || refuse "git rev-parse HEAD failed"
[[ "$actual" =~ ^[0-9a-f]{40}$ ]] || refuse "HEAD is not a full 40-hex sha"
[[ "$actual" == "$expected" ]] || refuse "HEAD $actual is not the expected commit $expected"
tree=$(git rev-parse 'HEAD^{tree}' 2>/dev/null) || refuse "git rev-parse HEAD^{tree} failed"
[[ "$tree" =~ ^[0-9a-f]{40}$ ]] || refuse "tree is not a full 40-hex sha"
status=$(git status --porcelain --untracked-files=all 2>/dev/null) || refuse "git status failed"
[[ -z "$status" ]] || refuse "the checkout is not clean"
echo "RQ_JSON {\"job\":\"$job\",\"stage\":\"checkout\",\"result\":\"PASS\",\"commit\":\"$actual\",\"tree\":\"$tree\",\"ref\":\"${GITHUB_REF:-}\",\"run\":\"${GITHUB_RUN_ID:-}\",\"attempt\":\"${GITHUB_RUN_ATTEMPT:-}\"}"
