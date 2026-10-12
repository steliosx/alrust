#!/usr/bin/env bash
# Always-run cleanup for the one-time GHCR mirror workflows.
#
#   cleanup.sh AUTH_DIR|- [PATH...]
#
# Removes the credentials directory first and checks it independently, then
# attempts every other path even if an earlier one failed. Prints one line per
# scope and exits 1 (hold, not released) if anything remains.
set +e
fail=0
auth=$1
shift
if [ "$auth" != "-" ]; then
  rm -rf -- "$auth"
  if [ -e "$auth" ]; then
    echo "cleanup FAILED: credentials remain at $auth"
    fail=1
  else
    echo "cleanup COMPLETE: credentials removed ($auth)"
  fi
fi
for p in "$@"; do
  rm -rf -- "$p"
  if [ -e "$p" ]; then
    echo "cleanup FAILED: $p remains"
    fail=1
  else
    echo "cleanup COMPLETE: $p"
  fi
done
if [ "$fail" -ne 0 ]; then
  echo "cleanup INCOMPLETE: hold; not released"
  exit 1
fi
echo "cleanup ALL COMPLETE"
