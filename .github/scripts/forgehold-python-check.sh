#!/usr/bin/env bash
# Check the interpreter actions/setup-python installed for the Forgehold
# action-compatibility job (FH-ALRUST-COMPAT-1): both `python` on PATH and
# $pythonLocation/bin/python3 must be CPython 3.12.x. Reads only.
set -euo pipefail
fail() { echo "forgehold-python-check.sh: $1" >&2; exit 1; }
[[ -n "${pythonLocation:-}" ]] || fail "pythonLocation is not set"
for py in python "$pythonLocation/bin/python3"; do
  v=$("$py" -c 'import platform, sys; print(platform.python_implementation(), "%d.%d" % sys.version_info[:2])') || fail "$py did not run"
  [[ "$v" == "CPython 3.12" ]] || fail "$py is $v, expected CPython 3.12"
done
