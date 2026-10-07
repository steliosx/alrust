#!/usr/bin/env python3
"""Finalize a closed job, else write its failure record (executing workflow
W4; conformance/executing-workflow-design.md sections 2 and 10).

  finalize-job.py --job-root J --stages a,b,... --expected-pins P

Runs finalizer.finalize(J, STAGES, PINS). Only when that returns
NOT_FINALIZED (its quiescence gate did not pass, so nothing mutable was
read, removed or exported) does it run finalizer.failure_record(J,
STAGES), as finalize's contract directs; FAILED and SEALED never write a
failure record. A FAILED report does not always have a finalizer journal
behind it (export_exists, export_unavailable and a journal that cannot be
opened all happen before it exists); then the fixed report line and the
job's retained private state are the available evidence. Both functions keep their own gates, codes and writes; this
script adds none. P is the expected-pins JSON file (workflow literals; not
a secret). Standalone source: no workflow runs it yet (W5).

Classification: no process is created and no signal is sent, here or in
the finalizer (it opens pidfds only to observe that a generation ended).
It is NOT read-only: on the normal path the finalizer removes the job's
owned resources and writes the export, and the failure path writes the
failure record.

One JSON line on stdout; never a path, value or exception text:
  {"outcome", "cleanup", "reason", "failure_record"[, "manifest_sha256"]}
  with failure_record one of NOT_ATTEMPTED (finalize did not return
  NOT_FINALIZED), WRITTEN (plus "failure_record_sha256") or NONE.
  0  SEALED with cleanup COMPLETE;
  1  SEALED with cleanup not COMPLETE (results sealed, cleanup unproven);
  2  FAILED or NOT_FINALIZED (with or without a failure record);
  3  UNKNOWN: an unexpected error; the finalizer's journal and files, not
     this line, say what happened;
  64 NOT_RUN: usage;
  70 NOT_RUN: setup failed before the finalizer ran.
"""
import hashlib
import json
import os
import re
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_PINS = 1 << 16
MAX_RECORD = 1 << 20
SECRET_VARIABLES = ("DATABASE_URL", "SECRET_KEY", "PGPASSWORD")
OPTIONS = ("--job-root", "--stages", "--expected-pins")
OUTCOMES = ("SEALED", "FAILED", "NOT_FINALIZED")
CLEANUPS = ("COMPLETE", "UNPROVEN")
_STAGE = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def emit(record, code):
    sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    sys.stdout.flush()
    return code


def modules():
    conformance = os.path.dirname(HERE)
    if conformance not in sys.path:
        sys.path.insert(0, conformance)
    from alrust_conformance import finalizer
    return finalizer


def read_regular(path, limit):
    """The complete bytes of a regular file (checked on the opened
    descriptor), not followed if a link, read to EOF in a loop (a read may
    legally return fewer bytes than asked, W4-READ-R1) and refused once more
    than LIMIT bytes have accumulated. The descriptor is closed on every
    path; an OSError propagates. Raises ValueError otherwise."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not regular")
        pieces, size = [], 0
        while True:
            chunk = os.read(fd, limit + 1 - size)
            if not chunk:
                break
            pieces.append(chunk)
            size += len(chunk)
            if size > limit:
                raise ValueError("too large")
    finally:
        os.close(fd)
    return b"".join(pieces)


def read_pins(path):
    """The expected-pins JSON object, from the complete file (read_regular,
    at most MAX_PINS bytes). Raises ValueError or OSError."""
    value = json.loads(read_regular(path, MAX_PINS))
    if type(value) is not dict:
        raise ValueError("pins")
    return value


def parse(argv):
    """The options, or None for usage. Each option exactly once."""
    if len(argv) != 2 * len(OPTIONS) or sorted(argv[0::2]) != sorted(OPTIONS):
        return None
    opts = dict(zip(argv[0::2], argv[1::2]))
    for name in ("--job-root", "--expected-pins"):
        if not (os.path.isabs(opts[name]) and os.path.normpath(opts[name]) == opts[name]):
            return None
    stages = opts["--stages"].split(",")
    if not (1 <= len(stages) <= 31 and all(_STAGE.fullmatch(n) for n in stages)
            and len(set(stages)) == len(stages)):
        return None
    opts["--stages"] = stages
    return opts


def report_line(report):
    """The fixed fields of finalize's report, or ValueError if it is not of
    the documented shape (never echoed)."""
    if not (type(report) is dict and report.get("outcome") in OUTCOMES
            and report.get("cleanup") in CLEANUPS
            and type(report.get("reason")) is str and _CODE.fullmatch(report["reason"])):
        raise ValueError("report")
    line = {"outcome": report["outcome"], "cleanup": report["cleanup"],
            "reason": report["reason"]}
    if report["outcome"] == "SEALED":
        if not (type(report.get("manifest_sha256")) is str
                and _SHA256.fullmatch(report["manifest_sha256"])):
            raise ValueError("report")
        line["manifest_sha256"] = report["manifest_sha256"]
    return line


def record_digest(path):
    """sha256 of the failure record failure_record() just wrote, re-read
    from the file (O_NOFOLLOW, regular) rather than trusted from the
    return, because that identity goes into the signed line. Never its path
    or content. This and the pins file are the script's only reads; both
    hash or parse only the complete bytes, read to EOF (W4-READ-R1)."""
    return hashlib.sha256(read_regular(path, MAX_RECORD)).hexdigest()


def main(argv):
    opts = parse(argv)
    if opts is None:
        return emit({"result": "NOT_RUN", "reason": "usage"}, 64)
    if any(name in os.environ for name in SECRET_VARIABLES):
        return emit({"result": "NOT_RUN", "reason": "secret_in_environment"}, 70)
    try:
        fz = modules()
        pins = read_pins(opts["--expected-pins"])
    except BaseException:  # nothing ran; never print the error text
        return emit({"result": "NOT_RUN", "reason": "setup_failed"}, 70)
    job_root, stages = opts["--job-root"], opts["--stages"]
    try:
        line = report_line(fz.finalize(job_root, stages, pins))
        line["failure_record"] = "NOT_ATTEMPTED"
        if line["outcome"] == "NOT_FINALIZED":
            path = fz.failure_record(job_root, stages)
            if path is None:
                line["failure_record"] = "NONE"
            else:
                line["failure_record"] = "WRITTEN"
                line["failure_record_sha256"] = record_digest(path)
    except Exception:  # never a traceback: it could carry a value
        return emit({"result": "UNKNOWN", "code": "finalizer_error"}, 3)
    if line["outcome"] == "SEALED":
        return emit(line, 0 if line["cleanup"] == "COMPLETE" else 1)
    return emit(line, 2)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
