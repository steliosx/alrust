#!/usr/bin/env python3
"""Stage the S0 run's original, non-credential evidence for the owner-chosen
custody (W5b; S0PLAN-R1; DECISIONS Y24): an exact allowlist copied into a
fresh private staging directory that the next workflow step uploads as a
1-day Actions artifact. The finalizer is not involved and is unchanged.

  export-evidence.py JOB_ROOT OUT_DIR STAGING_DIR

Refuses unless the credential disposal has already happened: no
credential-named entry may remain in JOB_ROOT/database (the names
dispose-credentials.py removes). Each allowlisted file is opened without
following a link, must be a regular file under 1 MiB, is read to EOF and
scanned; a file containing a credential marker refuses the whole export.
Absent allowlisted files are reported, not invented (S0 may stop early).

Prints one JSON line per staged file {"file", "bytes", "sha256"} (the
in-log manifest that custody is checked against), then one summary line:
  0  {"result": "STAGED", "files": N, "absent": [...], "manifest_sha256": ...}
  2  {"result": "REFUSED", "code": ...}   nothing usable staged
  3  {"result": "UNKNOWN", "code": ...}
  64 {"result": "NOT_RUN", "reason": "usage"}
Never prints file content or a path outside the allowlisted names.
Standard library only; no process, signal or network."""
import hashlib
import json
import os
import re
import stat
import sys

MAX_BYTES = 1 << 20
DATABASE_DIR = "database"
# Relative to JOB_ROOT: the core, J, the closed journals and the tool receipts.
JOB_FILES = (
    "core-identity.json",
    "job-deadline.json",
    "journal/00-database-create.jsonl",
    "journal/01-database-create-exit.jsonl",
    "journal/02-database-drop.jsonl",
    "tools/00-database-create/launch.json",
    "tools/00-database-create/terminal.json",
)
# Relative to OUT_DIR: the expected pins and every step's stdout record.
OUT_FILES = (
    "expected-pins.json",
    "core.out", "deadline-fix.out", "plan.out", "create-pin.out",
    "create-deadline.out", "create.out", "stop-supervisor-pin.out",
    "stop-supervisor.out", "stop-supervisor.rc", "create-exit.out",
    "drop-deadline.out", "drop.out", "finalize.out", "finalize.rc",
    "dispose.out",
)
# Credential names dispose-credentials.py removes; any survivor refuses.
_CREDENTIAL = re.compile(r"(url|pgpass-[a-z_][a-z0-9_]{0,62}-[0-9a-f]{8}"
                         r"|psql-stderr-[0-9a-f]{8}-[0-9]{1,6})\Z")
# Markers that must never appear in exported bytes.
_MARKERS = (b"postgresql://", b"postgres://", b"SCRAM-SHA-256$", b"PGPASSWORD",
            b"127.0.0.1:5432:*:")


def _read(base_fd, rel):
    """Bytes of a regular file under base_fd, no link followed at any part."""
    parts = rel.split("/")
    fd = os.dup(base_fd)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                          dir_fd=fd)
            os.close(fd)
            fd = nxt
        ffd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                      dir_fd=fd)
    finally:
        os.close(fd)
    try:
        if not stat.S_ISREG(os.fstat(ffd).st_mode):
            raise ValueError("not regular")
        chunks, size = [], 0
        while True:
            chunk = os.read(ffd, MAX_BYTES + 1 - size)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError("too large")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(ffd)


def _open_dir(path):
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def credentials_remaining(job_fd):
    try:
        dfd = os.open(DATABASE_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                      dir_fd=job_fd)
    except FileNotFoundError:
        return False
    try:
        return any(_CREDENTIAL.match(n) for n in os.listdir(dfd))
    finally:
        os.close(dfd)


def export(job_root, out_dir, staging):
    """Returns (manifest lines, summary record, exit code)."""
    try:
        job_fd, out_fd = _open_dir(job_root), _open_dir(out_dir)
    except OSError:
        return [], {"result": "REFUSED", "code": "source_dir_unreadable"}, 2
    try:
        try:
            if credentials_remaining(job_fd):
                return [], {"result": "REFUSED", "code": "credentials_present"}, 2
        except OSError:
            return [], {"result": "UNKNOWN", "code": "database_dir_unlistable"}, 3
        staged, absent = [], []
        for prefix, fd, names in (("job", job_fd, JOB_FILES), ("out", out_fd, OUT_FILES)):
            for rel in names:
                label = prefix + "/" + rel
                try:
                    data = _read(fd, rel)
                except FileNotFoundError:
                    absent.append(label)
                    continue
                except (OSError, ValueError):
                    return [], {"result": "REFUSED", "code": "evidence_unreadable"}, 2
                if any(marker in data for marker in _MARKERS):
                    return [], {"result": "REFUSED", "code": "credential_marker"}, 2
                staged.append((label, data))
        if not staged:
            return [], {"result": "REFUSED", "code": "nothing_to_stage"}, 2
        try:
            os.mkdir(staging, 0o700)
            st_fd = _open_dir(staging)
        except OSError:
            return [], {"result": "REFUSED", "code": "staging_unavailable"}, 2
        try:
            lines = []
            for label, data in staged:
                name = label.replace("/", "__")
                wfd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                              | os.O_CLOEXEC, 0o600, dir_fd=st_fd)
                try:
                    view = memoryview(data)
                    while view:
                        view = view[os.write(wfd, view):]
                finally:
                    os.close(wfd)
                lines.append({"file": label, "bytes": len(data),
                              "sha256": hashlib.sha256(data).hexdigest()})
            manifest = (json.dumps(lines, sort_keys=True) + "\n").encode()
            mfd = os.open("manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                          | os.O_CLOEXEC, 0o600, dir_fd=st_fd)
            try:
                os.write(mfd, manifest)
            finally:
                os.close(mfd)
        except OSError:
            return [], {"result": "UNKNOWN", "code": "staging_write_failed"}, 3
        finally:
            os.close(st_fd)
        return lines, {"result": "STAGED", "files": len(lines), "absent": absent,
                       "manifest_sha256": hashlib.sha256(manifest).hexdigest()}, 0
    finally:
        os.close(job_fd)
        os.close(out_fd)


def main(argv):
    ok = (len(argv) == 3 and all(os.path.isabs(a) and os.path.normpath(a) == a for a in argv))
    if not ok:
        lines, record, code = [], {"result": "NOT_RUN", "reason": "usage"}, 64
    else:
        try:
            lines, record, code = export(*argv)
        except Exception:
            lines, record, code = [], {"result": "UNKNOWN", "code": "export_error"}, 3
    for line in lines:
        sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
