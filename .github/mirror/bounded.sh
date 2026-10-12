#!/usr/bin/env bash
# Run one command with a time limit, a per-file size limit and a watchdog on
# the total bytes stored under DIR. Unprivileged; used only by the one-time
# GHCR mirror workflows (ECR-RATE-1 option D).
#
#   bounded.sh DIR MAX_BYTES TIMEOUT_S PER_FILE_BYTES -- COMMAND [ARGS...]
#
# - PER_FILE_BYTES: kernel-enforced (RLIMIT_FSIZE via ulimit -f, rounded up to
#   whole KiB); a write past it fails with EFBIG or SIGXFSZ.
# - MAX_BYTES: sampled about once a second (stored_bytes.py walk plus a 1 s
#   sleep). Above it, or if a measurement fails, the command's process group
#   is stopped: TERM, up to 5 s grace, then KILL. Not a byte-exact cap: bytes
#   written between samples and during the TERM grace are not prevented.
# - TIMEOUT_S: timeout(1) sends TERM, then KILL after 10 s.
# - If this wrapper receives INT, TERM or HUP, or exits unexpectedly, it stops
#   the command's process group before it finishes. A signal that arrives while
#   the command is starting is held until the command's process group exists
#   (or the command has already exited), then honoured.
# - BOUNDED_TEST_SIGNAL_AT (before_pid or before_group),
#   BOUNDED_TEST_DELAY_SETSID (seconds) and BOUNDED_TEST_FORCE_CUTOFF_AFTER
#   (a path: wait for it, then take the startup-cutoff branch) are test-only
#   hooks for the startup fixtures; the workflows never set them.
#
# Exit status: the command's own status on success (timeout gives 124, or 137
# after its KILL escalation); 2 usage or limit setup; 4 total over MAX_BYTES;
# 5 measurement failed; 6 group members outlived the command; 7 the command's
# process group did not form within 5 s; 128+N on signal N.
set -uo pipefail
if [ $# -lt 6 ] || [ "$5" != "--" ]; then
  echo "usage: bounded.sh DIR MAX_BYTES TIMEOUT_S PER_FILE_BYTES -- COMMAND [ARGS...]" >&2
  exit 2
fi
dir=$1 max=$2 secs=$3 perfile=$4
shift 5
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if ! ulimit -f $(( (perfile + 1023) / 1024 )); then
  echo "bounded: could not set the per-file limit" >&2
  exit 2
fi

pid=""
starting=1
pending=""
stop_group() {
  [ -n "$pid" ] || return 0
  if kill -0 -- "-$pid" 2>/dev/null; then
    kill -TERM -- "-$pid" 2>/dev/null
    for _ in 1 2 3 4 5; do
      kill -0 -- "-$pid" 2>/dev/null || break
      sleep 1
    done
    kill -KILL -- "-$pid" 2>/dev/null
  fi
  wait "$pid" 2>/dev/null
  pid=""
}
on_signal() {
  if [ -n "$starting" ]; then
    echo "bounded: SIG$1 received while starting; held until the process group exists"
    [ -n "$pending" ] || pending="$1 $2"
    return
  fi
  echo "bounded: interrupted by SIG$1; stopping process group ${pid:-none}"
  stop_group
  exit "$2"
}
trap 'on_signal INT 130' INT
trap 'on_signal TERM 143' TERM
trap 'on_signal HUP 129' HUP
trap 'stop_group' EXIT

used=""
measure() {
  local out
  if ! out=$(python3 -I "$here/stored_bytes.py" "$dir" 2>&1) || ! [[ "$out" =~ ^[0-9]+$ ]]; then
    echo "MEASUREMENT_FAILED under $dir: ${out:0:300}"
    return 1
  fi
  used=$out
}

# Non-interactive bash runs background jobs in its own process group, so setsid
# does not fork here: $! becomes the leader of a new process group once setsid
# has run. Signals received until that is confirmed are held in $pending.
(
  if [ -n "${BOUNDED_TEST_DELAY_SETSID:-}" ]; then sleep "$BOUNDED_TEST_DELAY_SETSID"; fi
  exec setsid timeout --signal=TERM --kill-after=10s "$secs" "$@"
) &
if [ "${BOUNDED_TEST_SIGNAL_AT:-}" = before_pid ]; then kill -TERM $$; fi
pid=$!
if [ "${BOUNDED_TEST_SIGNAL_AT:-}" = before_group ]; then kill -TERM $$; fi
group_formed=""
if [ -n "${BOUNDED_TEST_FORCE_CUTOFF_AFTER:-}" ]; then
  for _ in $(seq 100); do
    [ -e "$BOUNDED_TEST_FORCE_CUTOFF_AFTER" ] && break
    sleep 0.05
  done
else
  for _ in $(seq 100); do
    if ! stat_line=$(cat "/proc/$pid/stat" 2>/dev/null); then
      group_formed=exited
      break
    fi
    rest=${stat_line##*) }
    read -r _state _ppid pgrp _ <<< "$rest"
    if [ "$pgrp" = "$pid" ]; then
      group_formed=yes
      break
    fi
    sleep 0.05
  done
fi
if [ -z "$group_formed" ]; then
  echo "bounded: process group $pid not confirmed within 5 s; killing $pid and any group it formed"
  # The group may have formed after the last sample: kill the group and the
  # leader, reap the leader, then stop any remaining owned group members.
  kill -KILL -- "-$pid" 2>/dev/null
  kill -KILL "$pid" 2>/dev/null
  wait "$pid" 2>/dev/null
  stop_group
  pid=""
  exit 7
fi
starting=""
if [ -n "$pending" ]; then
  read -r name status <<< "$pending"
  on_signal "$name" "$status"
fi
while kill -0 "$pid" 2>/dev/null; do
  if ! measure; then
    stop_group
    exit 5
  fi
  if [ "$used" -gt "$max" ]; then
    echo "STAGE_CAP_EXCEEDED: $used bytes stored under $dir exceeds $max; stopping process group $pid"
    stop_group
    exit 4
  fi
  sleep 1
done
wait "$pid"
rc=$?
if kill -0 -- "-$pid" 2>/dev/null; then
  echo "bounded: process group $pid outlived its leader; stopping it"
  stop_group
  exit 6
fi
pid=""
if ! measure; then
  exit 5
fi
echo "bounded: exit $rc; $used bytes stored under $dir (cap $max)"
if [ "$used" -gt "$max" ]; then
  echo "STAGE_CAP_EXCEEDED: $used > $max after exit"
  exit 4
fi
exit "$rc"
