#!/usr/bin/env python3
"""Remove the S0 job's credential-bearing files and verify they are gone
(W5b; S0PLAN-R3). The drop removes only the SQL objects and the
NOT_FINALIZED finalizer removes nothing, so without this step the job
role's URL and the passfiles stay in the job root until runner discard.

  dispose-credentials.py JOB_ROOT

Only JOB_ROOT/database is touched (database_stage.DATABASE_DIR). Inside it,
exactly these names are credential-bearing and removed:
  url                      the job role's DATABASE_URL (its password)
  pgpass-<user>-<8 hex>    a libpq passfile
  psql-stderr-<8 hex>-<n>  raw psql stderr (unscanned, so treated as secret)
Each must be a regular file (no link followed); it is unlinked relative to
the opened directory and its absence verified. Any other entry is left in
place and reported, never removed.

One JSON line, never a path or file content:
  0  {"result": "DISPOSED", "removed": {...counts...}, "other_entries": N}
     ("NONE_PRESENT" instead of "DISPOSED" when the directory is absent)
  2  {"result": "REFUSED", "code": ...}   the job root or directory is not
     what it must be; nothing removed
  3  {"result": "UNKNOWN", "code": ...}   some removal or verification
     failed; disposal is not proven
  64 {"result": "NOT_RUN", "reason": "usage"}
Standard library only; no process, signal or network."""
import json
import os
import re
import stat
import sys

DATABASE_DIR = "database"
_CLASSES = (
    ("url", re.compile(r"url\Z")),
    ("pgpass", re.compile(r"pgpass-[a-z_][a-z0-9_]{0,62}-[0-9a-f]{8}\Z")),
    ("psql_stderr", re.compile(r"psql-stderr-[0-9a-f]{8}-[0-9]{1,6}\Z")),
)


def classify(name):
    for label, pattern in _CLASSES:
        if pattern.match(name):
            return label
    return None


def dispose(job_root):
    """Returns (record, exit code)."""
    try:
        root_fd = os.open(job_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return {"result": "REFUSED", "code": "job_root_unreadable"}, 2
    try:
        try:
            dir_fd = os.open(DATABASE_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                             | os.O_CLOEXEC, dir_fd=root_fd)
        except FileNotFoundError:
            return {"result": "NONE_PRESENT", "removed": {label: 0 for label, _ in _CLASSES},
                    "other_entries": 0}, 0
        except OSError:
            return {"result": "REFUSED", "code": "database_dir_invalid"}, 2
    finally:
        os.close(root_fd)
    try:
        removed = {label: 0 for label, _ in _CLASSES}
        other = 0
        try:
            names = os.listdir(dir_fd)
        except OSError:
            return {"result": "UNKNOWN", "code": "database_dir_unlistable"}, 3
        for name in sorted(names):
            label = classify(name)
            if label is None:
                other += 1
                continue
            try:
                info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError:
                return {"result": "UNKNOWN", "code": "credential_unstattable"}, 3
            if not stat.S_ISREG(info.st_mode):
                return {"result": "UNKNOWN", "code": "credential_not_regular"}, 3
            try:
                os.unlink(name, dir_fd=dir_fd)
            except OSError:
                return {"result": "UNKNOWN", "code": "credential_unlink_failed"}, 3
            removed[label] += 1
        # Verify: no credential-named entry remains, by a fresh listing.
        try:
            remaining = [n for n in os.listdir(dir_fd) if classify(n) is not None]
        except OSError:
            return {"result": "UNKNOWN", "code": "database_dir_unlistable"}, 3
        if remaining:
            return {"result": "UNKNOWN", "code": "credential_still_present"}, 3
        return {"result": "DISPOSED", "removed": removed, "other_entries": other}, 0
    finally:
        os.close(dir_fd)


def main(argv):
    if not (len(argv) == 1 and os.path.isabs(argv[0]) and os.path.normpath(argv[0]) == argv[0]):
        record, code = {"result": "NOT_RUN", "reason": "usage"}, 64
    else:
        try:
            record, code = dispose(argv[0])
        except Exception:
            record, code = {"result": "UNKNOWN", "code": "dispose_error"}, 3
    sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
