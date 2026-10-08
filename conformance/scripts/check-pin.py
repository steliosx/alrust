#!/usr/bin/env python3
"""Check one file's bytes against a literal sha256 before the workflow runs
it (W5A-R1; executing-workflow design sections 9-10, Q-W-3: the stop
helpers are not in the core's pins, so their literal digests are checked
before execution).

  check-pin.py PATH SHA256

PATH is absolute and normalized; it is opened without following a final
link, must be a regular file (checked on the opened descriptor), and is read
to EOF under a 1 MiB bound. One JSON line, never the path or file content:
  0  {"result": "MATCH"}
  2  {"result": "REFUSED", "code": "pin_mismatch" | "pin_unreadable"}
  64 {"result": "NOT_RUN", "reason": "usage"}
Standard library only; no process, signal or network."""
import hashlib
import json
import os
import re
import stat
import sys

MAX_BYTES = 1 << 20
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def file_digest(path, limit=MAX_BYTES):
    """sha256 of the complete bytes of a regular file. Raises ValueError or
    OSError for anything else."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not regular")
        digest, size = hashlib.sha256(), 0
        while True:
            chunk = os.read(fd, limit + 1 - size)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise ValueError("too large")
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def main(argv):
    if not (len(argv) == 2 and os.path.isabs(argv[0])
            and os.path.normpath(argv[0]) == argv[0] and _SHA256.fullmatch(argv[1])):
        line, code = {"result": "NOT_RUN", "reason": "usage"}, 64
    else:
        try:
            actual = file_digest(argv[0])
        except (OSError, ValueError):
            line, code = {"result": "REFUSED", "code": "pin_unreadable"}, 2
        else:
            if actual == argv[1]:
                line, code = {"result": "MATCH"}, 0
            else:
                line, code = {"result": "REFUSED", "code": "pin_mismatch"}, 2
    sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
