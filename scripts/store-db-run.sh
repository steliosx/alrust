#!/usr/bin/env bash
# Bounded store database run (ADR-0005): generate the catalog files, or run
# the store tests, against one existing PostgreSQL 18.6 server.
#
#   scripts/store-db-run.sh generate   # reference-catalog.json, alpha_shape.expected, 0002
#   scripts/store-db-run.sh test       # cargo test -p alrust-store-pg, serially
#
# Environment (required):
#   STORE_RUN_PG_HOST, STORE_RUN_PG_PORT, STORE_RUN_PG_USER
#   STORE_RUN_PG_PASSFILE   file holding the password (never on a command line)
#   STORE_RUN_PG_BIN        directory with psql
# Optional: STORE_RUN_PG_LIB (LD_LIBRARY_PATH for psql only),
#           STORE_RUN_TIMEOUT (seconds, default 900), STORE_RUN_DIR.
#
# Every database the run creates is named alrust_t_<run id>_*. On exit, for
# any reason, the trap drops whatever is left under that prefix and checks
# that none remain and that the server's other databases are exactly the
# ones listed before the run. Nothing else is created or touched.
set -euo pipefail

mode=${1:?usage: store-db-run.sh generate|test}
case "$mode" in generate | test) ;; *) echo "unknown mode $mode" >&2; exit 2 ;; esac
: "${STORE_RUN_PG_HOST:?}" "${STORE_RUN_PG_PORT:?}" "${STORE_RUN_PG_USER:?}"
: "${STORE_RUN_PG_PASSFILE:?}" "${STORE_RUN_PG_BIN:?}"
timeout_s=${STORE_RUN_TIMEOUT:-900}

repo=$(git rev-parse --show-toplevel)
cd "$repo"
run_id=$(date -u +%Y%m%d%H%M%S)$$
prefix="alrust_t_${run_id}_"
run_dir=${STORE_RUN_DIR:-$HOME/.cache/alrust-store-runs}/$run_id
mkdir -p -m 700 "$run_dir"
exec > >(tee -a "$run_dir/run.log") 2>&1

PGPASSWORD=$(tr -d '\n' < "$STORE_RUN_PG_PASSFILE")
export PGPASSWORD
export PGHOST=$STORE_RUN_PG_HOST PGPORT=$STORE_RUN_PG_PORT PGUSER=$STORE_RUN_PG_USER PGDATABASE=postgres
export PGCONNECT_TIMEOUT=10
psql() { LD_LIBRARY_PATH=${STORE_RUN_PG_LIB:-} "$STORE_RUN_PG_BIN/psql" -X -v ON_ERROR_STOP=1 -qAt "$@"; }
# The password reaches cargo only through the environment, in the URL.
pw_url=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read().strip(), safe=""))' \
    < "$STORE_RUN_PG_PASSFILE")
export DATABASE_URL="postgres://$STORE_RUN_PG_USER:$pw_url@$PGHOST:$PGPORT/postgres"
unset pw_url
export ALRUST_TEST_DB_PREFIX=$prefix
like="$(printf '%s' "$prefix" | sed 's/_/\\_/g')%"

list_others() { psql -c "SELECT datname FROM pg_database WHERE datname NOT LIKE '$like' ORDER BY 1"; }
list_owned() { psql -c "SELECT datname FROM pg_database WHERE datname LIKE '$like' ORDER BY 1"; }

echo "run $run_id mode=$mode prefix=$prefix head=$(git rev-parse HEAD) dirty=$(git status --porcelain | wc -l)"
echo "server: $(psql -c 'SELECT version()')"
echo "server data directory: $(psql -c 'SHOW data_directory' 2>/dev/null || echo '(not visible to this role)')"
list_others > "$run_dir/databases-before.txt"
if [ -n "$(list_owned)" ]; then echo "databases already exist under $prefix; refusing" >&2; exit 3; fi

cleanup() {
    rc=$?
    set +e
    echo "cleanup (exit $rc): dropping anything left under $prefix"
    list_owned > "$run_dir/leftover.txt"
    while read -r db; do
        [ -n "$db" ] || continue
        echo "dropping leftover $db"
        psql -c "DROP DATABASE IF EXISTS \"$db\" WITH (FORCE)" || rc=4
    done < "$run_dir/leftover.txt"
    if [ -n "$(list_owned)" ]; then
        echo "FAILED: databases remain under $prefix:"; list_owned; rc=4
    else
        echo "confirmed: no database remains under $prefix"
    fi
    list_others > "$run_dir/databases-after.txt"
    if ! cmp -s "$run_dir/databases-before.txt" "$run_dir/databases-after.txt"; then
        echo "FAILED: the server's other databases changed:"
        diff "$run_dir/databases-before.txt" "$run_dir/databases-after.txt"; rc=4
    else
        echo "confirmed: the server's other databases are unchanged"
    fi
    echo "outcome: exit $rc; log $run_dir/run.log"
    exit $rc
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# Build first: compiling needs no database.
cargo test -p alrust-store-pg --no-run

case "$mode" in
generate)
    timeout --kill-after=30 "$timeout_s" cargo test -p alrust-store-pg --test reference_layout -- \
        --ignored --exact generate_reference_catalog generate_alpha_shape --test-threads=1
    python3 scripts/gen-0002.py
    git diff --stat
    ;;
test)
    timeout --kill-after=30 "$timeout_s" cargo test -p alrust-store-pg -- --test-threads=1
    ;;
esac
