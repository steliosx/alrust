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
pyver=$("$py" -c 'import sys; print("%d.%d" % sys.version_info[:2])') \
  || { echo "run-reference: no usable $py" >&2; exit 1; }
[[ $pyver == 3.12 ]] \
  || { echo "run-reference: the hash locks need CPython 3.12, found $pyver; refusing" >&2; exit 1; }
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

"$venv/bin/alertad" run --host 127.0.0.1 --port "$PORT" &
pid=$!
echo "$pid" > "$root/.reference.pid"

for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$PORT/management/healthcheck" >/dev/null 2>&1; then
    echo "reference alerta $(cat "$ref/VERSION") up on :$PORT (pid $pid)"
    exit 0
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "reference server exited during startup" >&2
    exit 1
  fi
  sleep 1
done
echo "reference server did not become healthy in 60s" >&2
kill "$pid" 2>/dev/null || true
exit 1
