#!/usr/bin/env bash
# Benchmark setup on a GitHub-hosted ubuntu-24.04 runner (bench-compare.yml):
# PostgreSQL 18.7 and pgbouncer from PGDG, a throwaway cluster on
# 127.0.0.1:5432, and the reference Alerta environment with gunicorn.
# Varnish and HAProxy, for the cache suite, are Ubuntu's own packages.
# PostgreSQL 19, for the pg19 suite only, is a second cluster on port 5433
# with the same options; alrust's pin stays 18.7.
# PostgreSQL 18.7 is alrust's pin since #226 (Y67); the owner first chose it
# for this benchmark (2026-11-15: "use postgres 18.7 which is the current
# stable one"). Writes BENCH_* lines to $GITHUB_ENV.
set -euo pipefail
: "${RUNNER_TEMP:?RUNNER_TEMP is required}"
root=$(git rev-parse --show-toplevel)
version=18.7-1.pgdg24.04+2
version19=19.0-2.pgdg24.04+1
port=5432
port19=5433
work="$RUNNER_TEMP/bench-env"
mkdir -m 700 -- "$work"

for p in "$port" "$port19"; do
  if ss -ltnH "sport = :$p" | grep -q .; then
    echo "port $p is already in use" >&2
    exit 1
  fi
done

# The PGDG signing key (fingerprint B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8).
# --retry-all-errors also retries a dropped transfer (curl exit 56).
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -fsSL --retry 5 --retry-all-errors -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
  https://www.postgresql.org/media/keys/ACCC4CF8.asc
gpg --show-keys --with-colons /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
  | grep -q '^fpr:::::::::B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8:' \
  || { echo "unexpected PGDG key fingerprint" >&2; exit 1; }
printf '%s\n' \
  "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt noble-pgdg main" \
  "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt-archive.postgresql.org/pub/repos/apt noble-pgdg-archive main" \
  | sudo tee /etc/apt/sources.list.d/pgdg.list >/dev/null
sudo install -d /etc/postgresql-common/createcluster.d
echo 'create_main_cluster = false' | sudo tee /etc/postgresql-common/createcluster.d/bench.conf >/dev/null
sudo apt-get update -q
sudo apt-get install -y -q --no-install-recommends \
  "postgresql-18=$version" "postgresql-client-18=$version" \
  "postgresql-19=$version19" "postgresql-client-19=$version19" pgbouncer libpq-dev varnish haproxy
# The packages start their own services; the benchmark runs its own processes.
sudo systemctl disable --now pgbouncer varnish haproxy >/dev/null 2>&1 || true

bin=/usr/lib/postgresql/18/bin
"$bin/postgres" -V
pwfile="$work/pwfile"
( umask 077; printf '%s' postgres > "$pwfile" )
bin19=/usr/lib/postgresql/19/bin
"$bin19/postgres" -V
"$bin/initdb" -D "$work/data" -U postgres --pwfile="$pwfile" -A scram-sha-256 >/dev/null
"$bin19/initdb" -D "$work/data19" -U postgres --pwfile="$pwfile" -A scram-sha-256 >/dev/null
rm -f -- "$pwfile"
"$bin/pg_ctl" -D "$work/data" -l "$work/pg.log" -w \
  -o "-p $port -c listen_addresses=127.0.0.1 -c unix_socket_directories='' -c max_connections=200" start >/dev/null
"$bin19/pg_ctl" -D "$work/data19" -l "$work/pg19.log" -w \
  -o "-p $port19 -c listen_addresses=127.0.0.1 -c unix_socket_directories='' -c max_connections=200" start >/dev/null
PGPASSWORD=postgres "$bin/psql" -h 127.0.0.1 -p "$port" -U postgres -d postgres -tAc 'SELECT version()'
PGPASSWORD=postgres "$bin19/psql" -h 127.0.0.1 -p "$port19" -U postgres -d postgres -tAc 'SELECT version()'

# The reference Alerta environment, built as conformance/scripts/run-reference.sh
# builds it (both hash locks, then the pinned source), plus gunicorn.
venv="$work/alerta-venv"
python3.12 -m venv "$venv"
"$venv/bin/python" -m pip install -q --no-cache-dir --require-hashes --no-deps \
  -r "$root/.github/reference-build.lock"
"$venv/bin/python" -m pip install -q --no-cache-dir --require-hashes --no-deps --no-build-isolation \
  -r "$root/.github/reference-requirements.lock"
src="$work/alerta-src"
mkdir -p "$src" && cp -a "$root/reference/alerta/." "$src/"
"$venv/bin/python" -m pip install -q --no-cache-dir --no-deps --no-build-isolation --no-index "$src"
rm -rf -- "$src"
"$venv/bin/python" -m pip install -q --no-cache-dir --require-hashes --no-deps \
  -r "$root/scripts/bench/gunicorn.lock"
"$venv/bin/python" -m pip check
"$venv/bin/python" -m pip freeze --all > "$work/alerta-pip-freeze.txt"

{
  echo "BENCH_WORK=$work"
  echo "BENCH_PG_BIN=$bin"
  echo "BENCH_PG19_BIN=$bin19"
  echo "BENCH_ALERTA_VENV=$venv"
  echo "BENCH_PGBOUNCER=$(command -v pgbouncer)"
  echo "BENCH_VARNISHD=/usr/sbin/varnishd"
  echo "BENCH_VARNISHSTAT=/usr/bin/varnishstat"
  echo "BENCH_HAPROXY=/usr/sbin/haproxy"
} >> "${GITHUB_ENV:-/dev/null}"
pgbouncer --version
/usr/sbin/varnishd -V 2>&1 | head -1
/usr/bin/varnishstat -V 2>&1 | head -1
/usr/sbin/haproxy -v | head -1
echo "setup done: $work"
