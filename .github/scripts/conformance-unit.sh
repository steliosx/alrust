#!/usr/bin/env bash
# The conformance harness's unit tests, as ci.yml's conformance-unit job runs
# them (CONF-UNIT-ROT-1), for the Forgehold lane (FH-ALRUST-CONF-UNIT-1).
#
#   conformance-unit.sh install          locked, hash-checked, wheel-only venv
#   conformance-unit.sh test <shard>     pytest on one shard of conformance/tests/unit
#   conformance-unit.sh check-shards     the shards' collections must be exactly the suite
#
# The lane runs the suite as six shards, one workflow each. The shards together
# are exactly the test_*.py files of conformance/tests/unit: four named files,
# then every other file split by first letter (a-e, f-z). check-shards proves
# that against pytest's own collection: the shards' node IDs, taken together,
# must equal the suite's node IDs, with none missing, extra or duplicated.
#
# Filesystem for pytest's basetemp, fixed per shard (override with CU_TMP):
# - disk: under $RUNNER_TEMP. For the shards whose tests execute scripts they
#   write to tmp_path (ref-supervisor, ref-launcher, db-stage), because the
#   lane guest offers no executable tmpfs (its /dev/shm is noexec).
# - tmpfs: a fresh private directory on the first of /dev/shm and /tmp that is
#   a writable tmpfs (executable or not), removed on exit; refuses (exit 2) if
#   neither is. For the other shards, whose many create-and-fsync tests cost
#   far more on a disk (rest-fz locally: 167 s on disk, 13 s on tmpfs). The
#   guest's tmpfs is small (/dev/shm 64 MiB), so tmpfs mode also loads the
#   cu_tmp_reaper plugin (next to this script), which removes each test's
#   tmp_path after its teardown. tmpfs mode is a speed mode: fsync there is not
#   disk durability, so it is not disk-backed evidence. ci.yml's hosted job
#   remains the disk-backed run of the whole suite.
# The mode, directory, filesystem and (for tmpfs) size are printed. The venv
# lives under $RUNNER_TEMP.
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
named=(test_reference_supervisor.py test_entrypoint_supervisor.py test_reference_launcher.py test_database_stage.py)
shards=(ref-supervisor entry-supervisor ref-launcher db-stage rest-ae rest-fz)

# Sets $select to the pytest arguments for shard $1, and $mode to its
# filesystem mode.
select_shard() {
  case "$1" in
    ref-supervisor) select=("$unit/${named[0]}") mode=disk ;;
    entry-supervisor) select=("$unit/${named[1]}") mode=tmpfs ;;
    ref-launcher) select=("$unit/${named[2]}") mode=disk ;;
    db-stage) select=("$unit/${named[3]}") mode=disk ;;
    rest-ae|rest-fz)
      if [[ "$1" == rest-ae ]]; then range=a-e; else range=f-z; fi
      select=() mode=tmpfs
      for f in "$unit"/test_[$range]*.py; do  # range is a-e or f-z, set above
        [[ " ${named[*]} " == *" ${f##*/} "* ]] || select+=("$f")
      done
      [[ ${#select[@]} -gt 0 ]] || { echo "conformance-unit.sh: no files for $1" >&2; exit 2; } ;;
    *) echo "usage: conformance-unit.sh test ref-supervisor|entry-supervisor|ref-launcher|db-stage|rest-ae|rest-fz" >&2; exit 2 ;;
  esac
}

pytest_env() {
  PYTHONPATH="$PWD/conformance:$PWD/$unit${extra_path:+:$extra_path}" \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 "$@"
}

# Prints the first of /dev/shm and /tmp that is a writable tmpfs; fails if
# there is none.
writable_tmpfs() {
  local dir
  for dir in /dev/shm /tmp; do
    if [[ -d "$dir" && -w "$dir" && "$(stat -f -c %T "$dir")" == tmpfs ]]; then
      echo "$dir"; return 0
    fi
  done
  return 1
}

# Node IDs pytest collects for the given arguments, one per line. Any
# collection error (pytest's exit status) fails; only lines that are node IDs
# of the suite's test files are kept, so diagnostics are never counted.
collected() {
  local out rc=0
  out=$(pytest_env "$venv/bin/python" -m pytest -c conformance/pytest.ini \
    -p no:cacheprovider -q --collect-only "$@") || rc=$?
  [[ $rc -eq 0 ]] || { echo "conformance-unit.sh: collection failed (pytest exit $rc)" >&2; exit 1; }
  grep -E '^tests/unit/test_[A-Za-z0-9_]+\.py::' <<<"$out" || true
}

case "${1:-}" in
  install)
    python -m venv "$venv"
    timeout -k 10 90 "$venv/bin/python" -m pip install -q --require-hashes \
      --only-binary=:all: -r conformance/requirements.lock
    ;;
  test)
    select_shard "${2:-}"
    mode="${CU_TMP:-$mode}"
    plugin=() extra_path=""
    case "$mode" in
      tmpfs)
        if ! dir=$(writable_tmpfs); then
          echo "conformance-unit.sh: tmpfs mode but neither /dev/shm nor /tmp is a writable tmpfs" >&2; exit 2
        fi
        base=$(mktemp -d "$dir/alrust-cu-XXXXXX")  # private (0700), unique
        trap 'chmod -R u+w "$base" 2>/dev/null; rm -rf "$base"' EXIT
        echo "conformance-unit.sh: $dir $(df -k --output=size,avail "$dir" | tail -1 | awk '{print "size " $1 " KiB, available " $2 " KiB"}')"
        extra_path="$PWD/.github/scripts"
        plugin=(-p cu_tmp_reaper) ;;
      disk) base="$RUNNER_TEMP" ;;
      *) echo "conformance-unit.sh: CU_TMP must be tmpfs or disk" >&2; exit 2 ;;
    esac
    echo "conformance-unit.sh: shard $2, mode $mode, basetemp $base/pytest ($(stat -f -c %T "$base"))"
    pytest_env timeout -s INT -k 10 240 "$venv/bin/python" -m pytest -c conformance/pytest.ini \
      -p no:cacheprovider "${plugin[@]}" -q --durations=25 --basetemp="$base/pytest" "${select[@]}"
    ;;
  check-shards)
    whole=$(collected "$unit" | sort)
    parts=""
    for s in "${shards[@]}"; do
      select_shard "$s"
      ids=$(collected "${select[@]}")
      echo "conformance-unit.sh: shard $s ($mode) collects $(grep -c . <<<"$ids")"
      parts+="$ids"$'\n'
    done
    parts=$(grep . <<<"$parts" | sort)
    dups=$(uniq -d <<<"$parts")
    echo "conformance-unit.sh: shards $(grep -c . <<<"$parts"), suite $(grep -c . <<<"$whole")"
    [[ -n "$whole" ]] || { echo "conformance-unit.sh: the suite collected nothing" >&2; exit 1; }
    [[ -z "$dups" ]] || { echo "conformance-unit.sh: node IDs in more than one shard:" >&2; echo "$dups" >&2; exit 1; }
    if [[ "$parts" != "$whole" ]]; then
      echo "conformance-unit.sh: shard node IDs differ from the suite's:" >&2
      diff <(echo "$whole") <(echo "$parts") >&2 || true
      exit 1
    fi
    ;;
  *) echo "usage: conformance-unit.sh install|test <shard>|check-shards" >&2; exit 2 ;;
esac
