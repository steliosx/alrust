"""Read-only, independently pinned closed launch prefix; no qualification.

The caller authenticates the invocation, original hashes and actual launcher
and confirmation exit codes. Files and this result cannot supply that trust.
Declarations are historical evidence, not current process/executable checks.
"""
from dataclasses import dataclass
import hashlib
import os
import stat
import time

from . import observer_binding_reader as br
from . import stage_journal as sj


class ClosedLaunchError(ValueError):
    """Fixed private diagnostics, without bytes, paths or OS text."""


def _require(condition, code):
    if not condition:
        raise ClosedLaunchError(code)


def _check_deadline(deadline):
    _require(time.monotonic_ns() < deadline, "deadline_expired")


def _pair(value):
    _require(type(value) is dict and set(value) == {"pid", "start_ticks"}
             and all(br._int(v, 1) for v in value.values()), "generation_invalid")
    return value["pid"], value["start_ticks"]


def _one(records, kind):
    found = [r for r in records if r["kind"] == kind]
    _require(len(found) == 1, "required_record_count")
    return found[0]


def _read_journal(handles, directory, name, deadline):
    fd = handles.open(name, parent=directory)
    info = os.fstat(fd)
    _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
             and stat.S_IMODE(info.st_mode) == 0o600
             and info.st_size <= sj.MAX_JOURNAL, "journal_file_invalid")
    chunks, size = [], 0
    while size <= sj.MAX_JOURNAL:
        _check_deadline(deadline)
        chunk = os.read(fd, sj.MAX_JOURNAL + 1 - size)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    _require(size == info.st_size and size <= sj.MAX_JOURNAL
             and br._stamp(os.fstat(fd)) == br._stamp(info), "journal_file_changed")
    return b"".join(chunks), fd, br._stamp(info)


@dataclass(frozen=True)
class ClosedLaunchInputs:
    stages: tuple[tuple[str, str], ...]
    core_sha256: str
    launch_sha256: str
    supervisor: tuple[int, int]
    child: tuple[int, int]
    readiness_interpreter: tuple[int, int]
    readiness_monotonic_ns: int
    handoff_monotonic_ns: int
    scope: str = "CLOSED_LAUNCH_BYTES_ONLY_NOT_CURRENT_PROCESS_OR_EXECUTION_QUALIFICATION"


def read_closed_launch(job_root, root_identity, *, bindings, stages,
                       launcher_exit_code, confirmation_exit_code, deadline_ns):
    """Validate a closed prefix ending launch, launch-confirm.

    `stages` is an independently retained tuple of (name, original sha256)
    for EVERY stage in the prefix, never hashes chosen by these files. Call
    before creating the next stage journal; extra directory entries refuse.
    `bindings` is the prior read_binding_inputs result, not an authorization
    capability. Retain the caller's original absolute deadline (<=5 seconds
    away at entry). No writes, signals, services or artifact export.
    """
    _require(br._path(job_root) and br._identity(root_identity)
             and type(bindings) is br.BindingInputs, "expected_inputs_invalid")
    _require(type(stages) is tuple and 2 <= len(stages) <= sj.MAX_STAGES
             and all(type(s) is tuple and len(s) == 2
                     and type(s[0]) is str and sj._STAGE.fullmatch(s[0])
                     and br._digest(s[1]) for s in stages), "expected_stages_invalid")
    names = tuple(s[0] for s in stages)
    _require(len(set(names)) == len(names)
             and names[-2:] == ("launch", "launch-confirm"), "expected_stages_invalid")
    _require(type(launcher_exit_code) is int and launcher_exit_code == 0
             and type(confirmation_exit_code) is int and confirmation_exit_code == 0,
             "invocation_not_successful")
    now = time.monotonic_ns()
    _require(type(deadline_ns) is int and now < deadline_ns <= now + 5_000_000_000,
             "deadline_invalid")
    try:
        with br._Handles() as handles:
            root = handles.root(job_root)
            br._root_matches(root, root_identity)
            journal_dir = handles.open(sj.JOURNAL_DIR, directory=True, parent=root)
            reference = handles.open("reference", directory=True, parent=root)
            directories = []
            for name, fd in ((sj.JOURNAL_DIR, journal_dir), ("reference", reference)):
                info = os.fstat(fd)
                _require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
                         "directory_not_private")
                directories.append((name, fd, br._stamp(info)))
            expected_files = ["%02d-%s.jsonl" % (i, name) for i, name in enumerate(names)]
            _require(sorted(os.listdir(journal_dir)) == sorted(expected_files),
                     "journal_set_mismatch")
            core_data, core_fd, core_stamp = br._read(handles, root, sj.CORE_NAME)
            launch_data, launch_fd, launch_stamp = br._read(handles, reference, "launch.json")
            _require(core_data == bindings.core_bytes
                     and hashlib.sha256(core_data).hexdigest() == bindings.core_sha256
                     and launch_data == bindings.launch_bytes
                     and hashlib.sha256(launch_data).hexdigest() == bindings.launch_sha256,
                     "binding_bytes_changed")
            core = sj.validate_core(core_data, job_root)
            _require(core["boot_id"] == bindings.boot_id == sj.boot_id(), "boot_mismatch")
            held = [(root, sj.CORE_NAME, core_fd, core_stamp),
                    (reference, "launch.json", launch_fd, launch_stamp)]
            parsed = []
            floor, previous_digest, previous_name = core["captured_monotonic_ns"], None, None
            for i, (name, digest) in enumerate(stages):
                _check_deadline(deadline_ns)
                data, fd, stamp = _read_journal(handles, journal_dir, expected_files[i], deadline_ns)
                _require(hashlib.sha256(data).hexdigest() == digest, "journal_digest_mismatch")
                result, _, floor = sj._parse_journal(data, i, name, core,
                    bindings.core_sha256, previous_digest, previous_name, floor)
                _require(result == "COMPLETE", "stage_not_complete")
                records = [sj.strict_json(line, "journal_invalid")["evidence"]
                           for line in data.splitlines()]
                parsed.append(records)
                held.append((journal_dir, expected_files[i], fd, stamp))
                previous_digest, previous_name = digest, name
            launch_records, confirm_records = parsed[-2:]
            receipt = _one(launch_records, "launch_receipt")
            readiness = _one(launch_records, "launch_readiness")
            handoff = _one(confirm_records, "launch_handoff")
            _require(launch_records.index(receipt) < launch_records.index(readiness)
                     and len(confirm_records) == 3, "launch_record_order_invalid")
            _require(receipt["payload"] == {"path": job_root + "/reference/launch.json",
                                           "sha256": bindings.launch_sha256},
                     "launch_receipt_mismatch")
            ready = readiness["payload"]
            _require(set(ready) == {"receipt_sha256", "supervisor", "child", "child_exe",
                                   "venv_python3", "observed_monotonic_ns"}, "readiness_invalid")
            _require(ready["receipt_sha256"] == bindings.launch_sha256
                     and _pair(ready["supervisor"]) == bindings.supervisor
                     and _pair(ready["child"]) == bindings.child, "readiness_binding_mismatch")
            for key in ("child_exe", "venv_python3"):
                value = ready[key]
                _require(type(value) is dict and set(value) == {"dev", "ino"}
                         and br._identity((value["dev"], value["ino"])), "interpreter_invalid")
            _require(ready["child_exe"] == ready["venv_python3"], "interpreter_mismatch")
            launch = sj.strict_json(launch_data, "launch_invalid")
            observed = ready["observed_monotonic_ns"]
            _require(br._int(observed) and launch["monotonic_ns"] <= observed
                     <= readiness["monotonic_ns"], "readiness_clock_invalid")
            confirmed = handoff["payload"]
            _require(set(confirmed) == {"launch_journal_sha256", "receipt_sha256", "supervisor",
                                       "child", "observed_monotonic_ns"}, "handoff_invalid")
            _require(confirmed["launch_journal_sha256"] == stages[-2][1]
                     and confirmed["receipt_sha256"] == bindings.launch_sha256
                     and _pair(confirmed["supervisor"]) == bindings.supervisor
                     and _pair(confirmed["child"]) == bindings.child, "handoff_binding_mismatch")
            handoff_ns = confirmed["observed_monotonic_ns"]
            _require(br._int(handoff_ns) and launch_records[-1]["monotonic_ns"] <= handoff_ns
                     <= handoff["monotonic_ns"], "handoff_clock_invalid")
            for parent, name, fd, stamp in held:
                _require(br._stamp(os.fstat(fd)) == stamp
                         and br._stamp(os.stat(name, dir_fd=parent, follow_symlinks=False)) == stamp,
                         "file_changed")
            for name, fd, stamp in directories:
                _require(br._stamp(os.fstat(fd)) == stamp
                         and br._stamp(os.stat(name, dir_fd=root, follow_symlinks=False)) == stamp,
                         "directory_changed")
            _require(sorted(os.listdir(journal_dir)) == sorted(expected_files),
                     "journal_set_mismatch")
            br._root_matches(handles.root(job_root), root_identity)
            _check_deadline(deadline_ns)
            result = ClosedLaunchInputs(stages, bindings.core_sha256, bindings.launch_sha256,
                bindings.supervisor, bindings.child,
                (ready["child_exe"]["dev"], ready["child_exe"]["ino"]), observed, handoff_ns)
        # Include normal handle-close time in the original deadline.
        _check_deadline(deadline_ns)
        return result
    except ClosedLaunchError:
        raise
    except (br.BindingReadError, sj.JournalError, OSError, ValueError, TypeError,
            KeyError, IndexError, RecursionError):
        raise ClosedLaunchError("closed_launch_read_failed") from None
