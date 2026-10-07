"""The `stop` stage: record the reference stop helper's verdict (executing
workflow design, W1; conformance/executing-workflow-design.md section 6).

run_stop_stage() opens the `stop` journal right after `entrypoint-exit`,
runs `stop-reference.py RECEIPT SHA256 JOB_ROOT` exactly once through the
caller's RUN_HELPER, and records one `stop_verdict` of the exact shape that
finalizer._stop_evidence and finalizer.failure_record read:

  {"verdict", "reason", "launch_receipt_sha256", "terminal_receipt_sha256"}

The helper's line is parsed strictly (strict JSON, an exact key set, the
optional integer `errno` only for pidfd_open_failed and signal_failed,
(verdict, reason) in finalizer.STOP_VERDICTS, the exit code equal to the
helper's EXIT for that verdict). Only the fixed verdict and reason are
projected into the record; `errno` never is. The receipt digests are
computed here from the fixed paths, never taken from the line: the launch
digest must also equal the caller's digest (from the launcher's ready
line) before the helper runs; the terminal digest is null when no terminal
receipt exists, which only an UNPROVEN verdict allows. The raw helper
output is kept privately in the job root as owned files (never exported;
the finalizer removes owned resources). Any other outcome closes the stage
FAILED with a fixed code, with no retry and no stop_verdict.

This module creates no process itself; RUN_HELPER does (scripts/record-stop.py
passes database_stage._bounded_run). It proves nothing about cleanup: the
gates re-read and re-hash both receipts and re-observe the supervisor
generation (finalizer._stop_evidence)."""
import hashlib
import os
import stat

from . import stage_journal as _sj
from .finalizer import STOP_VERDICTS

STAGE_NAME = "stop"
PREVIOUS = "entrypoint-exit"
HELPER_EXIT = {"COMPLETE": 0, "FAILED": 1, "UNPROVEN": 2}   # stop-reference.py EXIT
ERRNO_REASONS = frozenset({"pidfd_open_failed", "signal_failed"})
MAX_OUTPUT = 4096
MAX_RECEIPT = 65536
HELPER_TIMEOUT_S = 60
RAW_NAMES = ("stop-helper.stdout", "stop-helper.stderr")


class StopRecorderError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _require(condition, code):
    if not condition:
        raise StopRecorderError(code)


def receipt_paths(job_root):
    base = os.path.join(job_root, "reference")
    return os.path.join(base, "launch.json"), os.path.join(base, "terminal.json")


def _read_receipt(path):
    """Bytes of a small regular file, not followed if a link, or None if
    absent. Raises StopRecorderError for anything else."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:
        raise StopRecorderError("receipt_unreadable") from None
    try:
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_size <= MAX_RECEIPT,
                 "receipt_unreadable")
        data = os.read(fd, MAX_RECEIPT + 1)
        _require(len(data) <= MAX_RECEIPT, "receipt_unreadable")
        return data
    finally:
        os.close(fd)


def parse_helper_output(stdout, returncode):
    """(verdict, reason) from the helper's one line, or StopRecorderError
    helper_output_invalid. Pure."""
    _require(type(stdout) is bytes and type(returncode) is int
             and 0 < len(stdout) <= MAX_OUTPUT and stdout.endswith(b"\n")
             and stdout.count(b"\n") == 1, "helper_output_invalid")
    try:
        line = _sj.strict_json(stdout[:-1], "helper_output_invalid")
    except _sj.JournalError:
        raise StopRecorderError("helper_output_invalid") from None
    _require(type(line) is dict and type(line.get("verdict")) is str
             and type(line.get("reason")) is str, "helper_output_invalid")
    verdict, reason = line["verdict"], line["reason"]
    if set(line) == {"verdict", "reason", "errno"}:
        _require(reason in ERRNO_REASONS and type(line["errno"]) is int,
                 "helper_output_invalid")
    else:
        _require(set(line) == {"verdict", "reason"}, "helper_output_invalid")
    _require((verdict, reason) in STOP_VERDICTS
             and returncode == HELPER_EXIT[verdict], "helper_output_invalid")
    return verdict, reason


def _keep_private(job_root, journal, name, data):
    """Write DATA to <job_root>/NAME, 0600, exclusive, and record it as an
    owned resource so that the finalizer accounts for and removes it."""
    path = os.path.join(job_root, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
        info = os.fstat(fd)
    finally:
        os.close(fd)
    journal.record("owned_resource", {"path": path, "dev": info.st_dev,
                                      "ino": info.st_ino, "type": "file"})


def run_stop_stage(job_root, index, receipt_digest, *, run_helper, helper_argv):
    """Open `stop` at INDEX after `entrypoint-exit`, run the helper once and
    record its verdict. RUN_HELPER(argv) returns (returncode, stdout, stderr)
    or raises OSError/TimeoutError. Returns {"stage", "verdict", "reason"}
    with stage COMPLETE, or {"stage": "FAILED", "code"}."""
    _require(type(receipt_digest) is str and len(receipt_digest) == 64
             and not receipt_digest.strip("0123456789abcdef"), "usage")
    launch_path, terminal_path = receipt_paths(job_root)
    try:
        with _sj.StageJournal(job_root, index, STAGE_NAME, PREVIOUS) as journal:
            try:
                launch = _read_receipt(launch_path)
                _require(launch is not None, "no_launch_receipt")
                launch_sha256 = hashlib.sha256(launch).hexdigest()
                # Before the helper runs: the caller's digest (the launcher's
                # ready line) must name exactly this receipt.
                _require(launch_sha256 == receipt_digest, "receipt_digest_mismatch")
                try:
                    returncode, stdout, stderr = run_helper(list(helper_argv))
                except (OSError, TimeoutError):
                    raise StopRecorderError("helper_unavailable") from None
                for name, data in zip(RAW_NAMES, (stdout, stderr)):
                    _require(type(data) is bytes, "helper_output_invalid")
                    _keep_private(job_root, journal, name, data)
                verdict, reason = parse_helper_output(stdout, returncode)
                terminal = _read_receipt(terminal_path)
                _require(terminal is not None or verdict == "UNPROVEN", "terminal_missing")
                # The launch receipt must be unchanged across the helper run.
                _require(_read_receipt(launch_path) == launch, "receipt_changed")
                journal.record("stop_verdict", {
                    "verdict": verdict, "reason": reason,
                    "launch_receipt_sha256": launch_sha256,
                    "terminal_receipt_sha256": (None if terminal is None
                                                else hashlib.sha256(terminal).hexdigest())})
            except StopRecorderError as error:
                journal.fail(error.code)
                raise
    except StopRecorderError as error:
        return {"stage": "FAILED", "code": error.code}
    return {"stage": "COMPLETE", "verdict": verdict, "reason": reason}
