#!/usr/bin/env bash
# Bounded store database run (ADR-0005), inside one fixed UTC interval.
#
#   scripts/store-db-run.sh generate   # reference-catalog.json, alpha_shape.expected, 0002
#   scripts/store-db-run.sh test       # cargo test -p alrust-store-pg, serially
#
# Connection (required): STORE_RUN_PG_HOST, STORE_RUN_PG_PORT, STORE_RUN_PG_USER,
#   STORE_RUN_PG_PASSFILE (the password, read into the environment only),
#   STORE_RUN_PG_BIN (directory with psql); optional STORE_RUN_PG_LIB
#   (LD_LIBRARY_PATH for psql only).
# Binding (required, from the admission): STORE_RUN_EXPECT_HEAD (full commit),
#   STORE_RUN_EXPECT_SERVER ("<server_version_num>|<port>|<postmaster start, UTC,
#   YYYY-MM-DDTHH:MM:SSZ>"; the start time identifies this server instance),
#   STORE_RUN_NOT_BEFORE and STORE_RUN_HARD_STOP (UTC epoch seconds).
#
# Time budget, all inside the admitted interval:
# - A query runs under `timeout 12 --kill-after=3` (statement_timeout 8 s,
#   lock_timeout 4 s, connect timeout 4 s), so it is gone within 15 s; one
#   starts only if 20 s (15 + 5 s margin) remain before the hard stop.
# - Cleanup needs at most 5 s of reaping plus 5 queries (owned list, up to
#   2 drops, absence, other names) = 80 s; CLEANUP_RESERVE is 150 s.
# - A step runs in its own process session under `timeout <left - 10>
#   --kill-after=10`, so it is gone by the work deadline (hard stop minus
#   the reserve); anything left in its session is then killed by pid.
# Every database the run creates is named alrust_t_<run id>_*. On exit, for
# any reason, the trap first verifies that no process of the run's steps
# survives. Only then does it drop what is left under the prefix, confirm
# through a successful query that none remain, and check that the server's
# other database names are unchanged (names only, not their contents). If
# a process survives or a query fails, it reports HOLD and fails: it never
# claims absence, and never infers an empty set from a failed query.
set -euo pipefail

CLEANUP_RESERVE=150
QUERY_LIMIT=12
QUERY_KILL=3
QUERY_MARGIN=5
mode=${1:?usage: store-db-run.sh generate|test}
case "$mode" in generate | test) ;; *) echo "unknown mode $mode" >&2; exit 2 ;; esac
for v in STORE_RUN_PG_HOST STORE_RUN_PG_PORT STORE_RUN_PG_USER STORE_RUN_PG_PASSFILE STORE_RUN_PG_BIN \
         STORE_RUN_EXPECT_HEAD STORE_RUN_EXPECT_SERVER STORE_RUN_NOT_BEFORE STORE_RUN_HARD_STOP; do
    [ -n "${!v:-}" ] || { echo "$v is required" >&2; exit 2; }
done
now() { date -u +%s; }
not_before=$STORE_RUN_NOT_BEFORE
hard_stop=$STORE_RUN_HARD_STOP
work_deadline=$((hard_stop - CLEANUP_RESERVE))
[ "$(now)" -ge "$not_before" ] || { echo "refused: before the admitted interval" >&2; exit 2; }
[ "$(now)" -lt "$work_deadline" ] || { echo "refused: too late to start before the hard stop" >&2; exit 2; }

repo=$(git rev-parse --show-toplevel)
cd "$repo"
[ "$(git rev-parse HEAD)" = "$STORE_RUN_EXPECT_HEAD" ] || { echo "refused: HEAD is not the admitted head" >&2; exit 2; }
[ -z "$(git status --porcelain --untracked-files=all)" ] || { echo "refused: the worktree is not clean" >&2; exit 2; }

run_id=$(date -u +%Y%m%d%H%M%S)$$
prefix="alrust_t_${run_id}_"
run_dir=$HOME/.cache/alrust-store-runs/$run_id
mkdir -p -m 700 "$run_dir"
exec > >(tee -a "$run_dir/run.log") 2>&1

PGPASSWORD=$(tr -d '\n' < "$STORE_RUN_PG_PASSFILE")
export PGPASSWORD
export PGHOST=$STORE_RUN_PG_HOST PGPORT=$STORE_RUN_PG_PORT PGUSER=$STORE_RUN_PG_USER PGDATABASE=postgres
export PGCONNECT_TIMEOUT=4
export PGOPTIONS="-c statement_timeout=8s -c lock_timeout=4s"
# The password reaches cargo only through the environment, in the URL.
pw_url=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read().strip(), safe=""))' \
    < "$STORE_RUN_PG_PASSFILE")
export DATABASE_URL="postgres://$STORE_RUN_PG_USER:$pw_url@$PGHOST:$PGPORT/postgres"
unset pw_url
export ALRUST_TEST_DB_PREFIX=$prefix
like="$(printf '%s' "$prefix" | sed 's/_/\\_/g')%"

# A bounded psql: gone within QUERY_LIMIT + QUERY_KILL seconds, and only
# started if that plus QUERY_MARGIN still ends before the hard stop.
psql_q() {
    if [ $(($(now) + QUERY_LIMIT + QUERY_KILL + QUERY_MARGIN)) -gt "$hard_stop" ]; then
        echo "no time left before the hard stop for a query" >&2
        return 1
    fi
    LD_LIBRARY_PATH=${STORE_RUN_PG_LIB:-} timeout --kill-after="$QUERY_KILL" "$QUERY_LIMIT" \
        "$STORE_RUN_PG_BIN/psql" -X -v ON_ERROR_STOP=1 -qAt -c "$1"
}
list_owned() { psql_q "SELECT datname FROM pg_database WHERE datname LIKE '$like' ORDER BY 1"; }
list_others() { psql_q "SELECT datname FROM pg_database WHERE datname NOT LIKE '$like' ORDER BY 1"; }

# Processes whose session id is $1, other than this shell and its ancestors.
session_members() {
    python3 - "$1" "$$" <<'PY'
import os, sys
sid, me = int(sys.argv[1]), int(sys.argv[2])
ancestors, p = set(), me
while p > 1:
    ancestors.add(p)
    try:
        p = int(open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()[1])
    except OSError:
        break
for d in os.listdir("/proc"):
    if d.isdigit() and int(d) not in ancestors:
        try:
            f = open(f"/proc/{d}/stat").read().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if int(f[3]) == sid:
            print(d)
PY
}
step_sid=""
reap_session() {
    [ -n "$step_sid" ] || return 0
    local left
    left=$(session_members "$step_sid") || return 1
    if [ -n "$left" ]; then
        echo "killing processes left in step session $step_sid: $left"
        # shellcheck disable=SC2086
        kill -KILL $left 2>/dev/null || true
        sleep 1
        left=$(session_members "$step_sid") || return 1
        [ -z "$left" ] || { echo "FAILED: processes survive in session $step_sid: $left"; return 1; }
    fi
    step_sid=""
}
# Run one step in a new session; it is gone by the work deadline.
step() {
    local name=$1; shift
    local left=$((work_deadline - $(now) - 10))
    [ "$left" -gt 0 ] || { echo "FAILED: no time left for step $name"; return 1; }
    echo "step $name: limit ${left}s (+10 s kill grace)"
    setsid timeout --kill-after=10 "$left" "$@" &
    step_sid=$!
    local rc=0
    wait "$step_sid" || rc=$?
    reap_session || rc=6
    echo "step $name: exit $rc"
    return "$rc"
}

cleanup() {
    local rc=$?
    set +e
    echo "cleanup (exit $rc)"
    # Cessation first: a surviving process of a step could still create a
    # database under the prefix after any absence check.
    if ! reap_session; then
        echo "HOLD: a process of step session $step_sid survives (see above); databases under" \
             "$prefix may still be created. No absence is claimed; the run is not released."
        echo "outcome: exit 6 (HOLD) at $(date -u +%FT%TZ); log $run_dir/run.log"
        exit 6
    fi
    echo "confirmed: no process of the run's steps survives"
    local owned others held=0
    if ! owned=$(list_owned); then
        echo "FAILED: could not list databases under $prefix"; held=1
    else
        printf '%s\n' "$owned" > "$run_dir/leftover.txt"
        for db in $owned; do
            echo "dropping leftover $db"
            psql_q "DROP DATABASE IF EXISTS \"$db\" WITH (FORCE)" || { echo "FAILED: drop $db"; held=1; }
        done
    fi
    if ! owned=$(list_owned); then
        echo "FAILED: could not confirm absence under $prefix"; held=1
    elif [ -n "$owned" ]; then
        echo "FAILED: databases remain under $prefix: $owned"; held=1
    else
        echo "confirmed (successful query): no database remains under $prefix"
    fi
    if ! others=$(list_others); then
        echo "FAILED: could not list the server's other databases"; held=1
    else
        printf '%s\n' "$others" > "$run_dir/databases-after.txt"
        if [ "$others" != "$(cat "$run_dir/databases-before.txt")" ]; then
            echo "FAILED: the server's other database names changed"; held=1
        else
            echo "confirmed: the server's other database names are unchanged (names only)"
        fi
    fi
    [ "$(now)" -le "$hard_stop" ] || { echo "FAILED: cleanup ended after the hard stop"; held=1; }
    if [ "$held" -ne 0 ]; then
        echo "HOLD: cleanup is incomplete or unverified for $prefix; the run is not released."
        rc=4
    fi
    echo "outcome: exit $rc at $(date -u +%FT%TZ); log $run_dir/run.log"
    exit "$rc"
}

echo "run $run_id mode=$mode prefix=$prefix head=$STORE_RUN_EXPECT_HEAD (clean)"
echo "interval: not before $(date -u -d @"$not_before" +%FT%TZ), work until $(date -u -d @"$work_deadline" +%FT%TZ), hard stop $(date -u -d @"$hard_stop" +%FT%TZ)"
server=$(psql_q "SELECT current_setting('server_version_num') || '|' || inet_server_port() || '|'
    || to_char(pg_postmaster_start_time() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')") \
    || { echo "refused: cannot query the server" >&2; exit 2; }
[ "$server" = "$STORE_RUN_EXPECT_SERVER" ] || { echo "refused: server is $server, not $STORE_RUN_EXPECT_SERVER" >&2; exit 2; }
echo "server: $server ($(psql_q 'SELECT version()'))"
owned=$(list_owned) || { echo "refused: cannot list databases" >&2; exit 2; }
[ -z "$owned" ] || { echo "refused: databases already exist under $prefix" >&2; exit 2; }
list_others > "$run_dir/databases-before.txt" || { echo "refused: cannot list databases" >&2; exit 2; }
trap cleanup EXIT
trap 'exit 130' INT TERM

# Build first: compiling needs no database.
step build cargo test -p alrust-store-pg --no-run
case "$mode" in
generate)
    step generate cargo test -p alrust-store-pg --test reference_layout -- \
        --ignored --exact generate_reference_catalog generate_alpha_shape --test-threads=1
    step regenerate python3 scripts/gen-0002.py
    git diff --stat
    ;;
test)
    step test cargo test -p alrust-store-pg -- --test-threads=1
    ;;
esac
