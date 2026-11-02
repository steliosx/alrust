#!/usr/bin/env bash
# The conformance harness's unit tests, as ci.yml's conformance-unit job runs
# them (CONF-UNIT-ROT-1), for the Forgehold lane (FH-ALRUST-CONF-UNIT-1).
#
#   conformance-unit.sh install          locked, hash-checked, wheel-only venv
#   conformance-unit.sh test <shard>     pytest on one shard of conformance/tests/unit
#   conformance-unit.sh check-shards     the shards' collections must add up to the suite
#
# The lane runs the suite as five shards, one workflow each. The shards together
# are exactly the test_*.py files of conformance/tests/unit: three named files,
# then every other file split by first letter (a-e, f-z); check-shards proves
# that against pytest's own collection.
#
# Many tests create and fsync files, which costs far more on a disk than on a
# tmpfs (rest-fz locally: 167 s on disk, 13 s on tmpfs). pytest's basetemp
# therefore goes on /dev/shm when that is a writable tmpfs, else under
# $RUNNER_TEMP; the choice is printed. The full suite writes about 170 MB
# there. The venv lives under $RUNNER_TEMP.
#
# The pip install and each pytest run have a hard wall-clock bound (timeout(1):
# 90 s and 240 s). pytest gets SIGINT first and KILL 10 s later; printing its
# progress and slowest durations after SIGINT is best-effort, not guaranteed.
# Venv creation and the other job steps are not bounded here; whether a whole
# job fits the Forgehold lane's episode budget is for that lane to qualify.
set -euo pipefail
: "${RUNNER_TEMP:?RUNNER_TEMP is not set}"
venv="$RUNNER_TEMP/conformance-venv"
unit=conformance/tests/unit
named=(test_reference_supervisor.py test_entrypoint_supervisor.py test_reference_launcher.py)
shards=(ref-supervisor entry-supervisor ref-launcher rest-ae rest-fz)

# Sets $select to the pytest arguments for shard $1.
select_shard() {
  case "$1" in
    ref-supervisor) select=("$unit/${named[0]}") ;;
    entry-supervisor) select=("$unit/${named[1]}") ;;
    ref-launcher) select=("$unit/${named[2]}") ;;
    rest-ae|rest-fz)
      if [[ "$1" == rest-ae ]]; then range=a-e; else range=f-z; fi
      select=()
      for f in "$unit"/test_[$range]*.py; do  # range is a-e or f-z, set above
        [[ " ${named[*]} " == *" ${f##*/} "* ]] || select+=("$f")
      done
      [[ ${#select[@]} -gt 0 ]] || { echo "conformance-unit.sh: no files for $1" >&2; exit 2; } ;;
    *) echo "usage: conformance-unit.sh test ref-supervisor|entry-supervisor|ref-launcher|rest-ae|rest-fz" >&2; exit 2 ;;
  esac
}

pytest_env() {
  PYTHONPATH="$PWD/conformance:$PWD/$unit" \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 "$@"
}

# Number of tests pytest collects for the given arguments.
collected() {
  pytest_env "$venv/bin/python" -m pytest -c conformance/pytest.ini \
    -p no:cacheprovider -q --collect-only "$@" | grep -c '::'
}

case "${1:-}" in
  install)
    python -m venv "$venv"
    timeout -k 10 90 "$venv/bin/python" -m pip install -q --require-hashes \
      --only-binary=:all: -r conformance/requirements.lock
    ;;
  test)
    select_shard "${2:-}"
    base="$RUNNER_TEMP"
    if [[ -d /dev/shm && -w /dev/shm && "$(stat -f -c %T /dev/shm)" == tmpfs ]]; then
      base=$(mktemp -d /dev/shm/alrust-cu-XXXXXX)
      trap 'chmod -R u+w "$base" 2>/dev/null; rm -rf "$base"' EXIT
    fi
    echo "conformance-unit.sh: basetemp $base/pytest ($(stat -f -c %T "$base"))"
    pytest_env timeout -s INT -k 10 240 "$venv/bin/python" -m pytest -c conformance/pytest.ini \
      -p no:cacheprovider -q --durations=25 --basetemp="$base/pytest" "${select[@]}"
    ;;
  check-shards)
    whole=$(collected "$unit")
    sum=0
    for s in "${shards[@]}"; do
      select_shard "$s"
      n=$(collected "${select[@]}")
      echo "conformance-unit.sh: shard $s collects $n"
      sum=$((sum + n))
    done
    echo "conformance-unit.sh: shards $sum, suite $whole"
    [[ "$sum" -eq "$whole" && "$whole" -gt 0 ]]
    ;;
  *) echo "usage: conformance-unit.sh install|test <shard>|check-shards" >&2; exit 2 ;;
esac
