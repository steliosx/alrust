#!/usr/bin/env bash
# Stop and remove only the PostgreSQL cluster this job's start-postgres18.sh
# acquired: the directory in PG18_ROOT whose owner file holds PG18_TOKEN.
# Without that pair nothing is touched, so stale data, markers or other
# runs' directories are never adopted. Data is removed only after pg_ctl
# positively reports no server (status 3, or 4 for a partial initdb); an
# unavailable pg_ctl or a server that will not stop keeps everything and
# fails. PG_BIN overrides the server binaries (for the scripts' own tests).
set -uo pipefail
bin=${PG_BIN:-/usr/lib/postgresql/18/bin}
root=${PG18_ROOT:-}
token=${PG18_TOKEN:-}

if [[ -z "$root" && -z "$token" ]]; then
  echo "postgres cleanup: this job acquired nothing; nothing touched"
  exit 0
fi
if [[ -z "$root" || -z "$token" || ! -f "$root/owner" || "$(cat "$root/owner")" != "$token" ]]; then
  echo "postgres cleanup FAILED: acquisition receipt does not match; nothing touched" >&2
  exit 1
fi
data="$root/data"
st=4
if [[ -e "$data" ]]; then
  if [[ ! -x "$bin/pg_ctl" ]]; then
    echo "postgres cleanup FAILED: $bin/pg_ctl unavailable; $root kept" >&2
    exit 1
  fi
  "$bin/pg_ctl" -D "$data" status >/dev/null 2>&1
  st=$?
  if [[ $st == 0 ]]; then
    "$bin/pg_ctl" -D "$data" -m fast -w -t 60 stop >/dev/null 2>&1
    "$bin/pg_ctl" -D "$data" status >/dev/null 2>&1
    st=$?
  fi
  if [[ $st != 3 && $st != 4 ]]; then
    echo "postgres cleanup FAILED: server not proven stopped (pg_ctl status $st); $root kept" >&2
    exit 1
  fi
fi
rm -rf -- "$root"
if [[ -e "$root" ]]; then
  echo "postgres cleanup FAILED: $root remains" >&2
  exit 1
fi
echo "postgres cleanup COMPLETE: no server running (pg_ctl status $st), $root removed"
