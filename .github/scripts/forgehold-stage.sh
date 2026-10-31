#!/usr/bin/env bash
# Run one Forgehold stage and emit its RQ_JSON result line from this script's
# own output (FH-ALRUST-ECHO-LOG): the PASS line is printed only after the
# command exits 0, and it never appears in a workflow step's run text, so its
# presence in the job log is evidence that the stage itself succeeded.
#
#   forgehold-stage.sh <job> <stage> <command> [args...]
set -uo pipefail
job=${1:?job}; stage=${2:?stage}; shift 2
[[ $# -gt 0 ]] || { echo "forgehold-stage.sh: no command" >&2; exit 2; }
[[ "$job$stage" =~ ^[a-z0-9-]+$ ]] || { echo "forgehold-stage.sh: job and stage must be [a-z0-9-]" >&2; exit 2; }
"$@"
rc=$?
if [[ $rc -eq 0 ]]; then
  printf 'RQ_JSON {"job":"%s","stage":"%s","result":"%s"}\n' "$job" "$stage" "PASS"
else
  printf 'RQ_JSON {"job":"%s","stage":"%s","result":"%s","exit":%d}\n' "$job" "$stage" "FAIL" "$rc"
fi
exit $rc
