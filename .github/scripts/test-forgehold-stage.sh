#!/usr/bin/env bash
# Controls for forgehold-stage.sh: a passing command emits exactly the PASS
# line and exits 0; a failing command emits a FAIL line, no PASS line, and
# keeps its exit status; bad arguments refuse. Any failed assertion fails the suite.
set -euo pipefail
stage=$(cd "$(dirname "$0")" && pwd)/forgehold-stage.sh
fail=0
run() { set +e; out=$("$stage" "$@" 2>&1); rc=$?; set -e; }
run j s true
if [[ $rc -eq 0 && "$out" == 'RQ_JSON {"job":"j","stage":"s","result":"PASS"}' ]]; then echo "ok   pass"; else echo "FAIL: pass ($rc: $out)" >&2; fail=1; fi
run j s sh -c 'exit 3'
if [[ $rc -eq 3 && "$out" == *'"result":"FAIL","exit":3}'* && "$out" != *'"result":"PASS"'* ]]; then echo "ok   fail keeps status, no PASS"; else echo "FAIL: fail ($rc: $out)" >&2; fail=1; fi
run j s
if [[ $rc -eq 2 && "$out" != *PASS* ]]; then echo "ok   no command refused"; else echo "FAIL: no command ($rc: $out)" >&2; fail=1; fi
run 'j"x' s true
if [[ $rc -eq 2 && "$out" != *PASS* ]]; then echo "ok   bad name refused"; else echo "FAIL: bad name ($rc: $out)" >&2; fail=1; fi
exit $fail
