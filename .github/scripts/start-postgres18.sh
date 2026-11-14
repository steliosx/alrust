#!/usr/bin/env bash
# Start a throwaway PostgreSQL 18.7 cluster on a GitHub-hosted ubuntu-24.04
# runner, from the official PostgreSQL apt repository (no container
# registry, so no Docker Hub or ECR pull limits).
#
#   .github/scripts/start-postgres18.sh <database> [user] [password] [port]
#
# Ownership: before anything is installed or initialised, this invocation
# acquires a fresh private directory with mktemp -d (exclusive creation) and
# writes a random token into it. The directory and token are exported to
# later steps of this job only (PG18_ROOT, PG18_TOKEN via $GITHUB_ENV), and
# .github/scripts/stop-postgres18.sh acts only on that pair. No pre-existing
# path is ever adopted.
# The server listens on 127.0.0.1 only and runs as the runner user.
set -euo pipefail

db=${1:?database name}
user=${2:-postgres}
password=${3:-postgres}
port=${4:-5432}
version=18.7-1.pgdg24.04+2
: "${RUNNER_TEMP:?RUNNER_TEMP is required}"

if ss -ltnH "sport = :$port" | grep -q .; then
  echo "port $port is already in use; nothing acquired" >&2
  ss -ltnp "sport = :$port" >&2 || true
  exit 1
fi

# Acquire: a new directory only this invocation can have created.
root=$(mktemp -d "$RUNNER_TEMP/pg18.XXXXXXXX")
token=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
( umask 077; printf '%s\n' "$token" > "$root/owner" )
if [[ -n "${GITHUB_ENV:-}" ]]; then
  { echo "PG18_ROOT=$root"; echo "PG18_TOKEN=$token"; } >> "$GITHUB_ENV"
fi
echo "PG18_ROOT=$root"
echo "PG18_TOKEN=$token"
data="$root/data"

{
  # The PGDG signing key (fingerprint B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8).
  sudo install -d /usr/share/postgresql-common/pgdg
  sudo curl -fsSL --retry 3 -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    https://www.postgresql.org/media/keys/ACCC4CF8.asc
  gpg --show-keys --with-colons /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    | grep -q '^fpr:::::::::B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8:' \
    || { echo "unexpected PGDG key fingerprint" >&2; exit 1; }
  # The main suite keeps only the newest build of each package; older builds,
  # including this pin once it is superseded, move to the archive suite.
  printf '%s\n' \
    "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt noble-pgdg main" \
    "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt-archive.postgresql.org/pub/repos/apt noble-pgdg-archive main" \
    | sudo tee /etc/apt/sources.list.d/pgdg.list >/dev/null
  # Do not let the package create and start its own "18 main" cluster.
  sudo install -d /etc/postgresql-common/createcluster.d
  echo 'create_main_cluster = false' | sudo tee /etc/postgresql-common/createcluster.d/alrust-ci.conf >/dev/null
  sudo apt-get update -q
  sudo apt-get install -y -q --no-install-recommends "postgresql-18=$version" "postgresql-client-18=$version"
}

bin=/usr/lib/postgresql/18/bin
"$bin/postgres" -V
pwfile="$root/pwfile"
trap 'rm -f "$pwfile"' EXIT
( umask 077; printf '%s' "$password" > "$pwfile" )
"$bin/initdb" -D "$data" -U "$user" --pwfile="$pwfile" -A scram-sha-256 >/dev/null
rm -f "$pwfile"
"$bin/pg_ctl" -D "$data" -l "$root/pg.log" -w \
  -o "-p $port -c listen_addresses=127.0.0.1 -c unix_socket_directories=''" start >/dev/null
PGPASSWORD="$password" "$bin/psql" -h 127.0.0.1 -p "$port" -U "$user" -d postgres -v ON_ERROR_STOP=1 \
  -c "CREATE DATABASE \"$db\"" >/dev/null
PGPASSWORD="$password" "$bin/psql" -h 127.0.0.1 -p "$port" -U "$user" -d "$db" -tAc 'SELECT version()'
