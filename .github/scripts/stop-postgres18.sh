#!/usr/bin/env bash
# Stop the cluster started by start-postgres18.sh and remove its data
# directory. Safe to run when nothing was started; fails if the server or
# its data cannot be removed.
set -uo pipefail
data="${RUNNER_TEMP:?RUNNER_TEMP is required}/pg18-data"
bin=/usr/lib/postgresql/18/bin
if [[ ! -e "$data" ]]; then
  echo "postgres cleanup: nothing to stop (no $data)"
  exit 0
fi
if [[ -x "$bin/pg_ctl" ]] && "$bin/pg_ctl" -D "$data" status >/dev/null 2>&1; then
  "$bin/pg_ctl" -D "$data" -m fast -w -t 60 stop || { echo "postgres cleanup FAILED: stop" >&2; exit 1; }
fi
rm -rf -- "$data"
if [[ -e "$data" ]]; then
  echo "postgres cleanup FAILED: $data remains" >&2
  exit 1
fi
echo "postgres cleanup COMPLETE: server stopped, $data removed"
