#!/usr/bin/env bash
# Stop the cluster started by start-postgres18.sh in this job and remove its
# data directory, but only when this job owns it ($RUNNER_TEMP/pg18-owner,
# written by the start script before initdb). A data directory without that
# marker is never touched. Data is removed only after pg_ctl positively
# reports that no server is running; any uncertainty keeps it and fails.
# PG_BIN overrides the server binaries (for the script's own tests).
set -uo pipefail
data="${RUNNER_TEMP:?RUNNER_TEMP is required}/pg18-data"
owner="$RUNNER_TEMP/pg18-owner"
bin=${PG_BIN:-/usr/lib/postgresql/18/bin}

if [[ ! -f "$owner" ]]; then
  echo "postgres cleanup: no ownership marker; nothing of this job's to stop (nothing touched)"
  exit 0
fi
if [[ "$(cat "$owner")" != "$data" ]]; then
  echo "postgres cleanup FAILED: $owner does not name $data; nothing touched" >&2
  exit 1
fi
if [[ ! -e "$data" ]]; then
  # Owned, but initdb never created the directory (partial start).
  rm -f -- "$owner"
  echo "postgres cleanup COMPLETE: no data directory was created; ownership released"
  exit 0
fi
if [[ ! -x "$bin/pg_ctl" ]]; then
  echo "postgres cleanup FAILED: $bin/pg_ctl unavailable; data kept at $data" >&2
  exit 1
fi
"$bin/pg_ctl" -D "$data" status >/dev/null 2>&1
st=$?
if [[ $st == 0 ]]; then
  "$bin/pg_ctl" -D "$data" -m fast -w -t 60 stop >/dev/null 2>&1
  "$bin/pg_ctl" -D "$data" status >/dev/null 2>&1
  st=$?
fi
# pg_ctl status: 3 = no server running (4 = no valid data directory, e.g. a
# partial initdb, which also has no server).
if [[ $st != 3 && $st != 4 ]]; then
  echo "postgres cleanup FAILED: server not proven stopped (pg_ctl status $st); data kept at $data" >&2
  exit 1
fi
rm -rf -- "$data"
if [[ -e "$data" ]]; then
  echo "postgres cleanup FAILED: $data remains" >&2
  exit 1
fi
rm -f -- "$owner"
echo "postgres cleanup COMPLETE: server stopped (pg_ctl status $st), $data removed, ownership released"
