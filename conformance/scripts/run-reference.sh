#!/usr/bin/env bash
# Boot the pinned reference Alerta server (reference/alerta) against PostgreSQL
# and wait until it answers. Used by CI and locally as the target for the
# conformance suite.
#
#   DATABASE_URL  postgres URL (default postgres://postgres:postgres@localhost:5432/alerta)
#   PORT          listen port (default 8080)
#   VENV          virtualenv dir (default .venv-reference)
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ref="$root/reference/alerta"
: "${DATABASE_URL:=postgres://postgres:postgres@localhost:5432/alerta}"
: "${PORT:=8080}"
: "${VENV:=$root/.venv-reference}"

if [[ ! -f "$ref/setup.py" ]]; then
  echo "reference/alerta is not checked out; run: git submodule update --init" >&2
  exit 1
fi

if [[ ! -x "$VENV/bin/alertad" ]]; then
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -r "$ref/requirements.txt"
  # Non-editable install from a temp copy so the submodule tree stays pristine.
  tmp="$(mktemp -d)"
  cp -a "$ref/." "$tmp/"
  "$VENV/bin/pip" install -q "$tmp"
  rm -rf "$tmp"
fi

export DATABASE_URL
export AUTH_REQUIRED="${AUTH_REQUIRED:-False}"
export SECRET_KEY="${SECRET_KEY:-conformance-not-secret}"

"$VENV/bin/alertad" run --host 127.0.0.1 --port "$PORT" &
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
