#!/usr/bin/env bash
# Start a throwaway PostgreSQL 18.6 cluster on a GitHub-hosted ubuntu-24.04
# runner, from the official PostgreSQL apt repository (no container
# registry, so no Docker Hub or ECR pull limits).
#
#   .github/scripts/start-postgres18.sh <database> [user] [password] [port]
#
# The server listens on 127.0.0.1 only and runs as the runner user with its
# data under $RUNNER_TEMP/pg18-data, which must not already exist. Stop it
# with .github/scripts/stop-postgres18.sh.
set -euo pipefail

db=${1:?database name}
user=${2:-postgres}
password=${3:-postgres}
port=${4:-5432}
version=18.6-1.pgdg24.04+2
data="${RUNNER_TEMP:?RUNNER_TEMP is required}/pg18-data"

if [[ -e "$data" ]]; then
  echo "refusing to use existing data directory $data" >&2
  exit 1
fi
if ss -ltnH "sport = :$port" | grep -q .; then
  echo "port $port is already in use" >&2
  ss -ltnp "sport = :$port" >&2 || true
  exit 1
fi

# The PGDG signing key (fingerprint B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8).
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -fsSL --retry 3 -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
  https://www.postgresql.org/media/keys/ACCC4CF8.asc
gpg --show-keys --with-colons /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
  | grep -q '^fpr:::::::::B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8:' \
  || { echo "unexpected PGDG key fingerprint" >&2; exit 1; }
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt noble-pgdg main" \
  | sudo tee /etc/apt/sources.list.d/pgdg.list >/dev/null
# Do not let the package create and start its own "18 main" cluster.
sudo install -d /etc/postgresql-common/createcluster.d
echo 'create_main_cluster = false' | sudo tee /etc/postgresql-common/createcluster.d/alrust-ci.conf >/dev/null
sudo apt-get update -q
sudo apt-get install -y -q --no-install-recommends "postgresql-18=$version" "postgresql-client-18=$version"

bin=/usr/lib/postgresql/18/bin
"$bin/postgres" -V
pwfile=$(mktemp)
trap 'rm -f "$pwfile"' EXIT
printf '%s' "$password" > "$pwfile"
"$bin/initdb" -D "$data" -U "$user" --pwfile="$pwfile" -A scram-sha-256 >/dev/null
rm -f "$pwfile"
"$bin/pg_ctl" -D "$data" -l "$RUNNER_TEMP/pg18.log" -w \
  -o "-p $port -c listen_addresses=127.0.0.1 -c unix_socket_directories=''" start
PGPASSWORD="$password" "$bin/psql" -h 127.0.0.1 -p "$port" -U "$user" -d postgres -v ON_ERROR_STOP=1 \
  -c "CREATE DATABASE \"$db\""
PGPASSWORD="$password" "$bin/psql" -h 127.0.0.1 -p "$port" -U "$user" -d "$db" -tAc 'SELECT version()'
