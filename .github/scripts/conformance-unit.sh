#!/usr/bin/env bash
# The conformance harness's unit tests, as ci.yml's conformance-unit job runs
# them (CONF-UNIT-ROT-1), for the Forgehold lane (FH-ALRUST-CONF-UNIT-1).
#
#   conformance-unit.sh install          locked, hash-checked, wheel-only venv
#   conformance-unit.sh test <shard>     pytest on one shard of conformance/tests/unit
#
# The lane runs the suite as five shards, one workflow each, because one lane
# VM runs it several times slower than a laptop core. The shards together are
# exactly the test_*.py files of conformance/tests/unit: three named files,
# then every other file split by first letter (a-e, f-z).
#
# The venv and pytest's temporary files live under $RUNNER_TEMP. The pip
# install and each pytest run have a hard wall-clock bound (timeout(1): 90 s
# and 240 s). pytest gets SIGINT first, so an overrun still prints its
# progress and the slowest durations, then KILL 10 s later. Venv creation and
# the other job steps are not bounded here; whether a whole job fits the
# Forgehold lane's episode budget is for that lane to qualify.
set -euo pipefail
: "${RUNNER_TEMP:?RUNNER_TEMP is not set}"
venv="$RUNNER_TEMP/conformance-venv"
unit=conformance/tests/unit
named=(test_reference_supervisor.py test_entrypoint_supervisor.py test_reference_launcher.py)
case "${1:-}" in
  install)
    python -m venv "$venv"
    timeout -k 10 90 "$venv/bin/python" -m pip install -q --require-hashes \
      --only-binary=:all: -r conformance/requirements.lock
    ;;
  test)
    case "${2:-}" in
      ref-supervisor) select=("$unit/${named[0]}") ;;
      entry-supervisor) select=("$unit/${named[1]}") ;;
      ref-launcher) select=("$unit/${named[2]}") ;;
      rest-ae|rest-fz)
        if [[ "$2" == rest-ae ]]; then range=a-e; else range=f-z; fi
        select=()
        for f in "$unit"/test_[$range]*.py; do  # range is a-e or f-z, set above
          [[ " ${named[*]} " == *" ${f##*/} "* ]] || select+=("$f")
        done
        [[ ${#select[@]} -gt 0 ]] || { echo "conformance-unit.sh: no files for $2" >&2; exit 2; } ;;
      *) echo "usage: conformance-unit.sh test ref-supervisor|entry-supervisor|ref-launcher|rest-ae|rest-fz" >&2; exit 2 ;;
    esac
    PYTHONPATH="$PWD/conformance:$PWD/$unit" \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
      timeout -s INT -k 10 240 "$venv/bin/python" -m pytest -c conformance/pytest.ini \
      -p no:cacheprovider -q --durations=25 --basetemp="$RUNNER_TEMP/pytest" "${select[@]}"
    ;;
  *) echo "usage: conformance-unit.sh install|test <shard>" >&2; exit 2 ;;
esac
