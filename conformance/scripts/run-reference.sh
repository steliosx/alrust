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
#
# Readiness is bound to the server this script launched (as in the reviewed
# AX64-V3-E2 smoke): the port must be observably free before launch; the
# launched child must stay alive; and the listening socket must belong to it
# (or to a process it spawned) both before and after the health response.
# The deadline is measured on the monotonic clock (/proc/uptime), and every
# health request is capped by timeout(1) and curl's own limits to the time
# left. On any failure the script stops and reaps only its own child. The
# .reference.pid file is informational and never read back.
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
owns_port() {  # true only when ss succeeds and a listener on :$PORT is $1 or its descendant
  local out lpid p n
  out=$(ss -ltnpH "sport = :$PORT" 2>/dev/null) || return 1
  for lpid in $(grep -o 'pid=[0-9]*' <<<"$out" | cut -d= -f2); do
    p=$lpid
    for n in 1 2 3 4 5 6; do
      [[ $p == "$1" ]] && return 0
      p=$(awk '/^PPid:/ {print $2}' "/proc/$p/status" 2>/dev/null) || break
      [[ -n $p && $p != 0 ]] || break
    done
  done
  return 1
}

case $(port_state) in
  FREE) ;;
  BUSY) echo "run-reference: port $PORT already in use; refusing" >&2; exit 1 ;;
  *) echo "run-reference: cannot observe port $PORT; refusing" >&2; exit 1 ;;
esac
command -v pg_config >/dev/null \
  || { echo "run-reference: pg_config not found; install the libpq development files" >&2; exit 1; }

if [[ -n ${VENV:-} ]]; then
  if [[ -e $VENV ]]; then
    echo "run-reference: $VENV already exists; refusing to reuse or delete it" >&2
    exit 1
  fi
  venv=$VENV
  src=$(mktemp -d "${RUNNER_TEMP:-/tmp}/alrust-reference-src.XXXXXX")
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

start=$(now_ms) || { echo "run-reference: cannot read the monotonic clock" >&2; exit 1; }
deadline=$(( start + READY_TIMEOUT * 1000 ))
"$venv/bin/alertad" run --host 127.0.0.1 --port "$PORT" &
pid=$!
echo "$pid" > "$root/.reference.pid"

stop_owned_child() {  # stop and reap only the child this script launched
  kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  kill -KILL "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

while :; do
  now=$(now_ms) || { echo "run-reference: cannot read the monotonic clock" >&2; stop_owned_child; exit 1; }
  left=$(( (deadline - now) / 1000 ))
  if (( left < 1 )); then
    echo "reference server did not become healthy within ${READY_TIMEOUT}s" >&2
    stop_owned_child
    exit 1
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    rc=0; wait "$pid" 2>/dev/null || rc=$?
    echo "reference server exited during startup (status $rc)" >&2
    exit 1
  fi
  if owns_port "$pid"; then
    cap=$(( left < 5 ? left : 5 ))
    if timeout "$cap" curl -fsS --connect-timeout "$cap" --max-time "$cap" \
        "http://127.0.0.1:$PORT/management/healthcheck" >/dev/null 2>&1 \
        && kill -0 "$pid" 2>/dev/null && owns_port "$pid"; then
      echo "reference alerta $(cat "$ref/VERSION") up on :$PORT (pid $pid)"
      exit 0
    fi
  fi
  sleep 1
done
