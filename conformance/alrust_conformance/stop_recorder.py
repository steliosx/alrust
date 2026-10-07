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
output, including the bounded partial streams of a timed-out or
over-long run (W1-R1), is kept privately in the job root as owned files,
each recorded as owned as soon as it exists (never exported; the
finalizer removes owned resources). Any other outcome closes the stage
FAILED with a fixed code, with no retry and no stop_verdict. Expected I/O
and journal failures map to fixed codes too (W1-R2): FAILED when the
journal can still close, INCOMPLETE when it cannot; no exception text is
ever returned or recorded.

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
RUNNER_FAILURES = {"timeout": "helper_timeout", "output_too_large": "helper_output_too_large"}


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
        try:
            info = os.fstat(fd)
            _require(stat.S_ISREG(info.st_mode) and info.st_size <= MAX_RECEIPT,
                     "receipt_unreadable")
            # Read to EOF: a read may legally return fewer bytes than asked
            # (W4-READ-R1); stop once the bound is passed, then refuse below.
            pieces, size = [], 0
            while True:
                chunk = os.read(fd, MAX_RECEIPT + 1 - size)
                if not chunk:
                    break
                pieces.append(chunk)
                size += len(chunk)
                if size > MAX_RECEIPT:
                    break
            data = b"".join(pieces)
        except OSError:
            raise StopRecorderError("receipt_unreadable") from None
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
    """Write DATA to <job_root>/NAME, 0600, exclusive. The file is recorded
    as an owned resource as soon as it exists, before any byte is written,
    so a partial write is still accounted for and removed by the
    finalizer. An OSError, including one from the final close, is the fixed
    private_output_failed (W1-R2). The descriptor is closed exactly once on
    every path; a close error after an earlier error leaves that earlier
    error primary, and a possibly closed descriptor is never closed again."""
    path = os.path.join(job_root, name)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600)
    except OSError:
        raise StopRecorderError("private_output_failed") from None
    try:
        info = os.fstat(fd)
        journal.record("owned_resource", {"path": path, "dev": info.st_dev,
                                          "ino": info.st_ino, "type": "file"})
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except BaseException as primary:
        try:
            os.close(fd)
        except OSError:
            pass                        # the primary error stays primary
        if isinstance(primary, OSError):
            raise StopRecorderError("private_output_failed") from None
        raise
    try:
        os.close(fd)
    except OSError:
        raise StopRecorderError("private_output_failed") from None


def _helper_result(result):
    """(failure, returncode, stdout, stderr), checked BEFORE anything is
    written (W1-N2): FAILURE None or a runner failure name, both streams
    bytes of at most MAX_OUTPUT each (database_stage._bounded_run's
    per-stream bound); anything else is helper_output_invalid."""
    _require(type(result) is tuple and len(result) == 4, "helper_output_invalid")
    failure, returncode, stdout, stderr = result
    _require((failure is None or (type(failure) is str and failure in RUNNER_FAILURES))
             and type(stdout) is bytes and type(stderr) is bytes
             and len(stdout) <= MAX_OUTPUT and len(stderr) <= MAX_OUTPUT,
             "helper_output_invalid")
    return failure, returncode, stdout, stderr


def _record(job_root, journal, receipt_digest, run_helper, helper_argv):
    """The stage body; raises StopRecorderError with a fixed code."""
    launch_path, terminal_path = receipt_paths(job_root)
    launch = _read_receipt(launch_path)
    _require(launch is not None, "no_launch_receipt")
    launch_sha256 = hashlib.sha256(launch).hexdigest()
    # Before the helper runs: the caller's digest (the launcher's ready
    # line) must name exactly this receipt.
    _require(launch_sha256 == receipt_digest, "receipt_digest_mismatch")
    try:
        result = run_helper(list(helper_argv))
    except (OSError, TimeoutError):
        raise StopRecorderError("helper_unavailable") from None
    failure, returncode, stdout, stderr = _helper_result(result)
    # Kept before any judgement, so a failed run's partial streams survive
    # as private evidence (W1-R1).
    for name, data in zip(RAW_NAMES, (stdout, stderr)):
        _keep_private(job_root, journal, name, data)
    _require(failure is None, RUNNER_FAILURES.get(failure, "helper_output_invalid"))
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
    return verdict, reason


def run_stop_stage(job_root, index, receipt_digest, *, run_helper, helper_argv):
    """Open `stop` at INDEX after `entrypoint-exit`, run the helper once and
    record its verdict. RUN_HELPER(argv) returns database_stage._bounded_run's
    (failure, returncode, stdout, stderr), or raises OSError/TimeoutError.
    Returns {"stage": "COMPLETE", "verdict", "reason"}; {"stage": "FAILED",
    "code"} when the stage closed FAILED; {"stage": "INCOMPLETE", "code":
    "journal_failed"} when the journal could not be opened or closed."""
    _require(type(receipt_digest) is str and len(receipt_digest) == 64
             and not receipt_digest.strip("0123456789abcdef"), "usage")
    try:
        with _sj.StageJournal(job_root, index, STAGE_NAME, PREVIOUS) as journal:
            try:
                verdict, reason = _record(job_root, journal, receipt_digest, run_helper,
                                          helper_argv)
            except StopRecorderError as error:
                journal.fail(error.code)
                raise
            except (OSError, _sj.JournalError):
                # A journal record or other I/O failure inside the stage.
                journal.fail("recorder_io_failed")
                raise StopRecorderError("recorder_io_failed") from None
    except StopRecorderError as error:
        return {"stage": "FAILED", "code": error.code}
    except (OSError, _sj.JournalError):
        # The journal could not be opened, or could not be closed: the stage
        # is missing or INCOMPLETE, and the gates refuse it.
        return {"stage": "INCOMPLETE", "code": "journal_failed"}
    return {"stage": "COMPLETE", "verdict": verdict, "reason": reason}
