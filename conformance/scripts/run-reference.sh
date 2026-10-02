#!/usr/bin/env bash
# Boot the pinned reference Alerta server (reference/alerta) against PostgreSQL
# and wait until it answers. Used by CI and locally as the target for the
# conformance suite.
#
#   DATABASE_URL     postgres URL (default postgres://postgres:postgres@localhost:5432/alerta)
#   PORT             listen port (default 8080)
#   VENV             virtualenv dir to create; it must not exist yet
#                    (default: a new temporary directory under RUNNER_TEMP or /tmp)
#   REFERENCE_PYTHON interpreter for the virtualenv (default python3; must be CPython 3.12)
#   READY_TIMEOUT    readiness deadline in seconds after launch (default 60)
#   JOB_ROOT         supervised mode (docs/ci-launch-attestation-contract.md):
#                    a private 0700 directory owned by this user. VENV
#                    (default JOB_ROOT/venv) and every temporary path stay
#                    inside it, nothing is written to the checkout, and the
#                    built server is launched by launch-reference.py under
#                    reference-supervisor.py instead of the group launch below.
#
# Readiness is bound to the server this script launched (as in the reviewed
# AX64-V3-E2 smoke): the port must be observably free before launch; the
# server is launched as the leader of its own process group, which must stay
# alive, and the listening socket must belong to a process of that group, both
# before and after the health response. Acceptance and cleanup share that one
# scope: on any failure, unexpected exit or TERM/INT, the whole group is
# stopped (TERM, then KILL), bounded, and its emptiness checked with the kernel
# (kill -0 on the group: PRESENT, EMPTY or UNKNOWN); only EMPTY is reported as
# complete, and the primary failure status is kept. Success hands the server
# off only after readiness is proven AND the success line has been written; a
# closed output pipe is a failure that still triggers cleanup.
# The deadline is measured on the monotonic clock (/proc/uptime), and every
# health request is capped by timeout(1) and curl's own limits to the time
# left. The caller's own group is never signalled. The .reference.pid file is
# informational and never read back.
#
# The environment is built fresh on every run from hash locks (AX64-G1):
# .github/reference-build.lock, then .github/reference-requirements.lock, both
# with --require-hashes --no-deps and pip's cache disabled, then the pinned
# reference itself offline from a temporary copy with --no-deps --no-index, so
# the submodule tree stays pristine. psycopg2 is built from its hashed sdist
# and needs pg_config (libpq development files). An existing VENV is refused,
# never reused or deleted.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ref="$root/reference/alerta"
: "${DATABASE_URL:=postgres://postgres:postgres@localhost:5432/alerta}"
: "${PORT:=8080}"

if [[ ! -f "$ref/setup.py" ]]; then
  echo "reference/alerta is not checked out; run: git submodule update --init" >&2
  exit 1
fi

py=${REFERENCE_PYTHON:-python3}
pyver=$("$py" -c 'import sys; print("%s %d.%d" % (sys.implementation.name, *sys.version_info[:2]))') \
  || { echo "run-reference: no usable $py" >&2; exit 1; }
[[ $pyver == "cpython 3.12" ]] \
  || { echo "run-reference: the hash locks need CPython 3.12, found $pyver; refusing" >&2; exit 1; }
: "${READY_TIMEOUT:=60}"
[[ $READY_TIMEOUT =~ ^[1-9][0-9]{0,3}$ ]] \
  || { echo "run-reference: READY_TIMEOUT must be 1-9999 seconds" >&2; exit 1; }

now_ms() {  # monotonic milliseconds since boot
  local up
  read -r up _ </proc/uptime || return 1
  echo $(( ${up%.*} * 1000 + 10#${up#*.} * 10 ))
}
port_state() {  # FREE/BUSY only on a successful observation, else UNKNOWN
  local out
  if ! out=$(ss -ltnH "sport = :$PORT" 2>/dev/null); then echo UNKNOWN; return; fi
  if [[ -z $out ]]; then echo FREE; else echo BUSY; fi
}
pgid_of() {  # process group of PID $1 from /proc/<pid>/stat (field after the command), or fail
  local st rest
  { read -r st <"/proc/$1/stat"; } 2>/dev/null || return 1
  rest=${st##*) }
  set -- $rest
  [[ $3 =~ ^[0-9]+$ ]] || return 1
  echo "$3"
}
owns_port() {  # true only when ss succeeds and a listener on :$PORT is in process group $1
  local out lpid
  out=$(ss -ltnpH "sport = :$PORT" 2>/dev/null) || return 1
  for lpid in $(grep -o 'pid=[0-9]*' <<<"$out" | cut -d= -f2); do
    [[ $(pgid_of "$lpid") == "$1" ]] && return 0
  done
  return 1
}
group_state() {  # PRESENT, EMPTY or UNKNOWN for process group $1, asked of the kernel
  local out
  if out=$(LC_ALL=C kill -0 -- "-$1" 2>&1); then echo PRESENT; return; fi
  case $out in
    *"No such process"*) echo EMPTY ;;
    *) echo UNKNOWN ;;
  esac
}
leader_gone() {  # true only on positive evidence that PID $1 no longer exists (ESRCH)
  local out
  out=$(LC_ALL=C kill -0 "$1" 2>&1) && return 1
  [[ $out == *"No such process"* ]]
}
reap_leader_if_exited() {  # wait only on positive exit evidence; an unknown state never blocks
  local st state
  if leader_gone "$1"; then wait "$1" 2>/dev/null || true; return 0; fi
  { read -r st <"/proc/$1/stat"; } 2>/dev/null || return 0
  state=${st##*) }; state=${state%% *}
  [[ $state == Z ]] && { wait "$1" 2>/dev/null || true; }
  return 0
}

case $(port_state) in
  FREE) ;;
  BUSY) echo "run-reference: port $PORT already in use; refusing" >&2; exit 1 ;;
  *) echo "run-reference: cannot observe port $PORT; refusing" >&2; exit 1 ;;
esac
command -v pg_config >/dev/null \
  || { echo "run-reference: pg_config not found; install the libpq development files" >&2; exit 1; }

if [[ -n ${JOB_ROOT:-} ]]; then
  [[ $JOB_ROOT == /* && -d $JOB_ROOT && ! -L $JOB_ROOT && -O $JOB_ROOT ]] \
    || { echo "run-reference: JOB_ROOT must be an existing directory owned by this user" >&2; exit 1; }
  : "${VENV:=$JOB_ROOT/venv}"
  [[ $VENV == "$JOB_ROOT"/* ]] \
    || { echo "run-reference: VENV must be inside JOB_ROOT" >&2; exit 1; }
  [[ ${HOME:-} == "$JOB_ROOT"/* ]] \
    || { echo "run-reference: HOME must be inside JOB_ROOT in supervised mode" >&2; exit 1; }
fi
if [[ -n ${VENV:-} ]]; then
  if [[ -e $VENV ]]; then
    echo "run-reference: $VENV already exists; refusing to reuse or delete it" >&2
    exit 1
  fi
  venv=$VENV
  src=$(mktemp -d "${JOB_ROOT:-${RUNNER_TEMP:-/tmp}}/alrust-reference-src.XXXXXX")
else
  envroot=$(mktemp -d "${RUNNER_TEMP:-/tmp}/alrust-reference-env.XXXXXX")
  venv=$envroot/venv
  src=$envroot/src-copy
fi

"$py" -m venv "$venv"
"$venv/bin/python" -m pip install -q --no-cache-dir --require-hashes --no-deps \
  -r "$root/.github/reference-build.lock"
"$venv/bin/python" -m pip install -q --no-cache-dir --require-hashes --no-deps --no-build-isolation \
  -r "$root/.github/reference-requirements.lock"
mkdir -p "$src" && cp -a "$ref/." "$src/"
"$venv/bin/python" -m pip install -q --no-cache-dir --no-deps --no-build-isolation --no-index "$src"
rm -rf -- "$src"
"$venv/bin/python" -m pip check
echo "run-reference: environment $venv (fresh), installed:"
"$venv/bin/python" -m pip freeze --all

export DATABASE_URL
export AUTH_REQUIRED="${AUTH_REQUIRED:-False}"
export SECRET_KEY="${SECRET_KEY:-conformance-not-secret}"

if [[ -n ${JOB_ROOT:-} ]]; then
  # Supervised mode: the launcher owns launch, readiness, receipt custody and
  # cleanup; nothing below runs.
  exec "$py" -I -S -B "$root/conformance/scripts/launch-reference.py" \
    --job-root "$JOB_ROOT" --venv "$venv" --port "$PORT" --ready-timeout "$READY_TIMEOUT"
fi

read -r version <"$ref/VERSION" || { echo "run-reference: cannot read reference VERSION" >&2; exit 1; }
start=$(now_ms) || { echo "run-reference: cannot read the monotonic clock" >&2; exit 1; }
deadline=$(( start + READY_TIMEOUT * 1000 ))
own_pgid=$(pgid_of $$) || { echo "run-reference: cannot read its own process group" >&2; exit 1; }

pid=''
handed_off=0
stop_owned_group() {  # bounded stop of the launched group; never the caller's group
  local i state
  [[ -n $pid && $pid != "$own_pgid" ]] || return 0
  kill -TERM -- "-$pid" 2>/dev/null || true
  for i in $(seq 1 20); do
    reap_leader_if_exited "$pid"
    state=$(group_state "$pid"); [[ $state == EMPTY ]] && break
    sleep 0.5
  done
  if [[ $state != EMPTY ]]; then
    kill -KILL -- "-$pid" 2>/dev/null || true
    for i in $(seq 1 10); do
      reap_leader_if_exited "$pid"
      state=$(group_state "$pid"); [[ $state == EMPTY ]] && break
      sleep 0.5
    done
  fi
  case $state in
    EMPTY) echo "run-reference: cleanup complete: process group $pid is empty" >&2 ;;
    PRESENT) echo "run-reference: cleanup FAILED: process group $pid still has members" >&2; return 1 ;;
    *) echo "run-reference: cleanup UNPROVEN: the state of process group $pid is unknown" >&2; return 1 ;;
  esac
}
on_exit() {
  local rc=$?
  trap - EXIT INT TERM
  if (( handed_off == 0 )); then
    stop_owned_group || true
  fi
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

set -m  # job control: the server becomes the leader of its own process group
"$venv/bin/alertad" run --host 127.0.0.1 --port "$PORT" &
pid=$!
set +m
[[ $(pgid_of "$pid") == "$pid" && $pid != "$own_pgid" ]] \
  || { echo "run-reference: the server is not the leader of its own process group" >&2; exit 1; }
echo "$pid" > "$root/.reference.pid"

while :; do
  now=$(now_ms) || { echo "run-reference: cannot read the monotonic clock" >&2; exit 1; }
  left=$(( (deadline - now) / 1000 ))
  if (( left < 1 )); then
    echo "reference server did not become healthy within ${READY_TIMEOUT}s" >&2
    exit 1
  fi
  if leader_gone "$pid"; then
    rc=0; wait "$pid" 2>/dev/null || rc=$?
    echo "reference server exited during startup (status $rc)" >&2
    exit 1
  fi
  if owns_port "$pid"; then
    cap=$(( left < 5 ? left : 5 ))
    if timeout "$cap" curl -fsS --connect-timeout "$cap" --max-time "$cap" \
        "http://127.0.0.1:$PORT/management/healthcheck" >/dev/null 2>&1 \
        && kill -0 "$pid" 2>/dev/null && owns_port "$pid"; then
      trap '' PIPE  # a closed output pipe becomes a write error, handled below, not a fatal signal
      if ! printf 'reference alerta %s up on :%s (pid %s, process group %s)\n' "$version" "$PORT" "$pid" "$pid"; then
        echo "run-reference: could not report readiness; treating as failure" >&2
        exit 1
      fi
      handed_off=1
      trap - EXIT INT TERM
      exit 0
    fi
  fi
  sleep 1
done
