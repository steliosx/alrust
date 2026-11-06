#!/usr/bin/env python3
"""Stage the S0 run's original, non-credential evidence for the owner-chosen
custody (W5b; S0PLAN-R1; DECISIONS Y24): an exact allowlist copied into a
fresh private staging directory that the next workflow step uploads as a
1-day Actions artifact. The finalizer is not involved and is unchanged.

  export-evidence.py JOB_ROOT OUT_DIR STAGING_DIR --attempted 1 --expected-pins PINS
  export-evidence.py JOB_ROOT OUT_DIR STAGING_DIR --attempted 0

The same gate as dispose-credentials.py first (W5B-R1, R2;
alrust_conformance.custody): with --attempted 1 the create holder's exit
must be proven now (state HOLDER_PROVEN); with --attempted 0 no holder can
have existed (state NO_CREATE_ATTEMPTED: the core, J and the available
step records are staged, which is the evidence of an early refusal). An
unproven holder refuses: nothing is staged and the originals stay private.

Then it refuses unless the credential disposal has already happened: no
credential-named entry may remain in JOB_ROOT/database (the names
dispose-credentials.py removes). Each allowlisted file is opened without
following a link, must be a regular file under 1 MiB, is read to EOF and
scanned; a file containing a credential marker refuses the whole export.
Absent allowlisted files are reported, not invented (S0 may stop early).

Prints one JSON line per staged file {"file", "bytes", "sha256"} (the
in-log manifest that custody is checked against), then one summary line:
  0  {"absent": [...], "files": N, "manifest_sha256": ..., "result": "STAGED",
      "state": "HOLDER_PROVEN" | "NO_CREATE_ATTEMPTED"}
     Every staged file and the manifest are fully written, then re-read from
     the staging directory and compared before STAGED is printed (W5B-R3).
  2  {"result": "REFUSED", "code": ...}   nothing usable staged
  3  {"result": "UNKNOWN", "code": ...}
  64 {"result": "NOT_RUN", "reason": "usage"}
Never prints file content or a path outside the allowlisted names.
Imports only the project's alrust_conformance package and the standard
library; no process, signal or network."""
import hashlib
import json
import os
import re
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_PINS = 1 << 16

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


def _write_all(dir_fd, name, data):
    """Create NAME exclusively under DIR_FD and write every byte (W5B-R3):
    a short write continues, a zero-byte write raises."""
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=dir_fd)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
    finally:
        os.close(fd)


def _open_dir(path):
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def _list_dir(dir_fd):
    """Names in the directory DIR_FD refers to, read through a fresh descriptor
    of that same directory (same device and inode, checked). Listing the very
    descriptor through which the entries were just created can return no
    names on btrfs, which made the W5B-R3 re-check fail there."""
    fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                 dir_fd=dir_fd)
    try:
        was, now = os.fstat(dir_fd), os.fstat(fd)
        if (was.st_dev, was.st_ino) != (now.st_dev, now.st_ino):
            raise OSError("staging directory changed")
        return os.listdir(fd)
    finally:
        os.close(fd)


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


def export(job_root, out_dir, staging, attempted, expected=None):
    """Returns (manifest lines, summary record, exit code)."""
    custody = modules()
    try:
        if attempted:
            custody.holder_proven(job_root, expected)
            state = "HOLDER_PROVEN"
        else:
            custody.not_launched(job_root)
            state = "NO_CREATE_ATTEMPTED"
    except custody.CustodyError as error:
        return [], {"result": "REFUSED", "code": str(error)}, 2
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
            lines, written = [], []
            for label, data in staged:
                name = label.replace("/", "__")
                _write_all(st_fd, name, data)
                written.append((name, data))
                lines.append({"file": label, "bytes": len(data),
                              "sha256": hashlib.sha256(data).hexdigest()})
            manifest = (json.dumps(lines, sort_keys=True) + "\n").encode()
            _write_all(st_fd, "manifest.json", manifest)
            written.append(("manifest.json", manifest))
            # W5B-R3: re-read every staged file and the manifest and compare.
            for name, data in written:
                if _read(st_fd, name) != data:
                    return [], {"result": "UNKNOWN", "code": "staging_verify_failed"}, 3
            if sorted(_list_dir(st_fd)) != sorted(name for name, _ in written):
                return [], {"result": "UNKNOWN", "code": "staging_verify_failed"}, 3
        except (OSError, ValueError):
            return [], {"result": "UNKNOWN", "code": "staging_write_failed"}, 3
        finally:
            os.close(st_fd)
        return lines, {"result": "STAGED", "state": state, "files": len(lines), "absent": absent,
                       "manifest_sha256": hashlib.sha256(manifest).hexdigest()}, 0
    finally:
        os.close(job_fd)
        os.close(out_fd)


def _absolute(path):
    return os.path.isabs(path) and os.path.normpath(path) == path


def parse(argv):
    """(job_root, out_dir, staging, attempted, pins path or None), or None."""
    if len(argv) >= 3 and all(_absolute(a) for a in argv[:3]):
        if argv[3:] == ["--attempted", "0"]:
            return argv[0], argv[1], argv[2], 0, None
        if len(argv) == 7 and argv[3:6] == ["--attempted", "1", "--expected-pins"] \
                and _absolute(argv[6]):
            return argv[0], argv[1], argv[2], 1, argv[6]
    return None


def main(argv):
    parsed = parse(argv)
    if parsed is None:
        lines, record, code = [], {"result": "NOT_RUN", "reason": "usage"}, 64
    else:
        job_root, out_dir, staging, attempted, pins = parsed
        try:
            expected = read_pins(pins) if pins else None
        except (OSError, ValueError):
            lines, record, code = [], {"result": "REFUSED", "code": "pins_unreadable"}, 2
        else:
            try:
                lines, record, code = export(job_root, out_dir, staging, attempted, expected)
            except Exception:
                lines, record, code = [], {"result": "UNKNOWN", "code": "export_error"}, 3
    for line in lines:
        sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
