#!/usr/bin/env bash
# The conformance harness's unit tests, as ci.yml's conformance-unit job runs
# them (CONF-UNIT-ROT-1), for the Forgehold lane (FH-ALRUST-CONF-UNIT-1).
#
#   conformance-unit.sh install   locked, hash-checked, wheel-only venv
#   conformance-unit.sh test      pytest on conformance/tests/unit
#
# The venv and pytest's temporary files live under $RUNNER_TEMP.
set -euo pipefail
: "${RUNNER_TEMP:?RUNNER_TEMP is not set}"
venv="$RUNNER_TEMP/conformance-venv"
case "${1:-}" in
  install)
    python -m venv "$venv"
    "$venv/bin/python" -m pip install -q --require-hashes \
      --only-binary=:all: -r conformance/requirements.lock
    ;;
  test)
    PYTHONPATH="$PWD/conformance:$PWD/conformance/tests/unit" \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
      "$venv/bin/python" -m pytest -c conformance/pytest.ini \
      -p no:cacheprovider -q --basetemp="$RUNNER_TEMP/pytest" conformance/tests/unit
    ;;
  *) echo "usage: conformance-unit.sh install|test" >&2; exit 2 ;;
esac
