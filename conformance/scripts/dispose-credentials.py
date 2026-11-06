#!/usr/bin/env python3
"""Remove the S0 job's credential-bearing files and verify they are gone
(W5b; S0PLAN-R3). The drop removes only the SQL objects and the
NOT_FINALIZED finalizer removes nothing, so without this step the job
role's URL and the passfiles stay in the job root until runner discard.

  dispose-credentials.py JOB_ROOT --attempted 1 --expected-pins PINS
  dispose-credentials.py JOB_ROOT --attempted 0

Gate first (W5B-R1, R2; alrust_conformance.custody), before anything is
touched:
  --attempted 1: custody.holder_proven: the closed create prefix, the core
    bound to it with these pins, and the database-create holder's exit
    proof recomputed COMPLETE. The opened JOB_ROOT/database descriptor must
    have the dev/ino of the create stage's owned_resource record.
  --attempted 0: custody.not_launched: no journal, tool receipt, database
    directory or launch evidence; the result is NONE_PRESENT.
Any gate failure is REFUSED with nothing removed: the files stay private
and held.

Only JOB_ROOT/database is touched (database_stage.DATABASE_DIR). Inside it,
exactly these names are credential-bearing and removed:
  url                      the job role's DATABASE_URL (its password)
  pgpass-<user>-<8 hex>    a libpq passfile
  psql-stderr-<8 hex>-<n>  raw psql stderr (unscanned, so treated as secret)
Each must be a regular file (no link followed); it is unlinked relative to
the opened directory and its absence verified. Any other entry is left in
place and reported, never removed.

One JSON line, never a path or file content:
  0  {"holder": "PROVEN", "result": "DISPOSED", "removed": {...}, "other_entries": N}
     {"holder": "PROVEN" | "NOT_LAUNCHED", "result": "NONE_PRESENT", ...} when
     no database directory exists
  2  {"result": "REFUSED", "code": ...}   the job root or directory is not
     what it must be; nothing removed
  3  {"result": "UNKNOWN", "code": ...}   some removal or verification
     failed; disposal is not proven
  64 {"result": "NOT_RUN", "reason": "usage"}
Imports only the project's alrust_conformance package and the standard
library; no process, signal or network."""
import json
import os
import re
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_PINS = 1 << 16

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


def modules():
    conformance = os.path.dirname(HERE)
    if conformance not in sys.path:
        sys.path.insert(0, conformance)
    from alrust_conformance import custody
    return custody


def read_pins(path):
    """The expected-pins JSON object: regular, not followed, bounded, to EOF."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("pins")
        pieces, size = [], 0
        while True:
            chunk = os.read(fd, MAX_PINS + 1 - size)
            if not chunk:
                break
            pieces.append(chunk)
            size += len(chunk)
            if size > MAX_PINS:
                raise ValueError("pins")
    finally:
        os.close(fd)
    value = json.loads(b"".join(pieces))
    if type(value) is not dict:
        raise ValueError("pins")
    return value


def dispose(job_root, attempted, expected=None):
    """Returns (record, exit code). ATTEMPTED is 0 or 1; EXPECTED the pins."""
    custody = modules()
    try:
        if attempted:
            recorded = custody.holder_proven(job_root, expected)
            holder = "PROVEN"
        else:
            custody.not_launched(job_root)
            recorded, holder = None, "NOT_LAUNCHED"
    except custody.CustodyError as error:
        return {"result": "REFUSED", "code": str(error)}, 2
    try:
        root_fd = os.open(job_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return {"result": "REFUSED", "code": "job_root_unreadable"}, 2
    try:
        try:
            dir_fd = os.open(DATABASE_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                             | os.O_CLOEXEC, dir_fd=root_fd)
        except FileNotFoundError:
            return {"holder": holder, "result": "NONE_PRESENT",
                    "removed": {label: 0 for label, _ in _CLASSES}, "other_entries": 0}, 0
        except OSError:
            return {"result": "REFUSED", "code": "database_dir_invalid"}, 2
    finally:
        os.close(root_fd)
    # W5B-R1: the directory opened must be the one the create stage recorded.
    info = os.fstat(dir_fd)
    if recorded is None or (info.st_dev, info.st_ino) != (recorded["dev"], recorded["ino"]):
        os.close(dir_fd)
        return {"result": "REFUSED", "code": "database_identity"}, 2
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
        return {"holder": holder, "result": "DISPOSED", "removed": removed,
                "other_entries": other}, 0
    finally:
        os.close(dir_fd)


def _absolute(path):
    return os.path.isabs(path) and os.path.normpath(path) == path


def parse(argv):
    """(job_root, attempted, pins path or None), or None for usage."""
    if len(argv) == 3 and argv[1:] == ["--attempted", "0"] and _absolute(argv[0]):
        return argv[0], 0, None
    if (len(argv) == 5 and argv[1:3] == ["--attempted", "1"] and argv[3] == "--expected-pins"
            and _absolute(argv[0]) and _absolute(argv[4])):
        return argv[0], 1, argv[4]
    return None


def main(argv):
    parsed = parse(argv)
    if parsed is None:
        record, code = {"result": "NOT_RUN", "reason": "usage"}, 64
    else:
        job_root, attempted, pins = parsed
        try:
            expected = read_pins(pins) if pins else None
        except (OSError, ValueError):
            record, code = {"result": "REFUSED", "code": "pins_unreadable"}, 2
        else:
            try:
                record, code = dispose(job_root, attempted, expected)
            except Exception:
                record, code = {"result": "UNKNOWN", "code": "dispose_error"}, 3
    sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
