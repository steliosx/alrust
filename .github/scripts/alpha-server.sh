#!/usr/bin/env bash
# Start and stop the alrust server for the alpha-smoke CI job.
#
#   alpha-server.sh start <ready-url> -- <command> [args...]
#   alpha-server.sh stop
#
# start acquires a fresh private directory (mktemp -d) and a random token,
# exports ALRUST_ROOT and ALRUST_TOKEN to later steps of this job only
# (GITHUB_ENV), launches the command with its log in that directory, and
# records the child's pid together with its kernel start time. It then waits
# up to ALRUST_READY_TIMEOUT seconds (default 60) for <ready-url>.
#
# stop acts only on that receipt: without it nothing is signalled. It
# signals the recorded pid only if /proc still shows the same process (same
# start time), so an exited child or a reused pid is never adopted.
set -uo pipefail

proc_start() { # kernel start time (field 22 of /proc/<pid>/stat) or empty
  local stat
  stat=$(cat "/proc/$1/stat" 2>/dev/null) || return 0
  stat=${stat##*) }
  read -r -a f <<< "$stat"
  echo "${f[19]}"
}

case "${1:-}" in
start)
  set -e
  url=${2:?ready url}
  [[ "${3:-}" == "--" ]] || { echo "usage: alpha-server.sh start <ready-url> -- <command>..." >&2; exit 2; }
  shift 3
  : "${RUNNER_TEMP:?RUNNER_TEMP is required}"
  root=$(mktemp -d "$RUNNER_TEMP/alrust.XXXXXXXX")
  token=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
  ( umask 077; printf '%s\n' "$token" > "$root/owner" )
  if [[ -n "${GITHUB_ENV:-}" ]]; then
    { echo "ALRUST_ROOT=$root"; echo "ALRUST_TOKEN=$token"; } >> "$GITHUB_ENV"
  fi
  echo "ALRUST_ROOT=$root"
  echo "ALRUST_TOKEN=$token"
  nohup "$@" > "$root/alrust.log" 2>&1 &
  pid=$!
  printf '%s %s\n' "$pid" "$(proc_start "$pid")" > "$root/pid"
  for _ in $(seq "${ALRUST_READY_TIMEOUT:-60}"); do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      echo "alrust ready (pid $pid)"
      exit 0
    fi
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  echo "alrust not ready" >&2
  cat "$root/alrust.log" >&2 || true
  exit 1
  ;;
stop)
  root=${ALRUST_ROOT:-}
  token=${ALRUST_TOKEN:-}
  if [[ -z "$root" && -z "$token" ]]; then
    echo "alrust cleanup: this job started nothing; nothing signalled"
    exit 0
  fi
  if [[ -z "$root" || -z "$token" || ! -f "$root/owner" || "$(cat "$root/owner")" != "$token" ]]; then
    echo "alrust cleanup FAILED: start receipt does not match; nothing signalled" >&2
    exit 1
  fi
  if [[ -f "$root/pid" ]]; then
    read -r pid started < "$root/pid"
    now=$(proc_start "$pid")
    if [[ -n "$now" && -n "$started" && "$now" == "$started" ]]; then
      kill -TERM "$pid" 2>/dev/null
      for _ in $(seq 10); do
        [[ "$(proc_start "$pid")" == "$started" ]] || break
        sleep 1
      done
      if [[ "$(proc_start "$pid")" == "$started" ]]; then
        kill -KILL "$pid" 2>/dev/null
        sleep 1
      fi
      if [[ "$(proc_start "$pid")" == "$started" ]]; then
        echo "alrust cleanup FAILED: pid $pid still running; $root kept" >&2
        exit 1
      fi
      echo "alrust cleanup: stopped pid $pid"
    else
      echo "alrust cleanup: recorded pid $pid is no longer this job's child (exited or reused); not signalled"
    fi
  fi
  rm -rf -- "$root"
  if [[ -e "$root" ]]; then
    echo "alrust cleanup FAILED: $root remains" >&2
    exit 1
  fi
  echo "alrust cleanup COMPLETE: $root removed"
  ;;
*)
  echo "usage: alpha-server.sh start <ready-url> -- <command>... | stop" >&2
  exit 2
  ;;
esac
