"""Read-only core/launch/readiness bytes; not a stage or process qualifier.

Expected hashes and invocation fields must be independently authenticated by
the caller. This reader does not obtain them from environment variables, files
being inspected or readiness text. Closed launch-stage/journal-chain evidence,
current P/C, interpreter, installed files, runtime captures and database binding
remain separate prerequisites. No imports of the reference, writes or services.
"""
from dataclasses import dataclass
import hashlib
import os
import re
import stat

from . import stage_journal as sj

MAX_BYTES = 65536
MAX_PATH = 4096
CORE_EXPECTATIONS = sj.CORE_KEYS - {"captured_utc_ns", "captured_monotonic_ns"}
# Core /2 (R-EP plan P1) adds entrypoint_pins; the expectation set follows the
# expected schema exactly, so a /1 expectation never matches a /2 core.
CORE_EXPECTATIONS_V2 = sj.CORE_KEYS_V2 - {"captured_utc_ns", "captured_monotonic_ns"}
LAUNCH_KEYS = frozenset({"schema", "supervisor", "child", "boot_id", "venv",
                         "terminal_receipt", "observer_socket", "observer",
                         "launched_utc_ns", "monotonic_ns"})
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"[A-Za-z0-9._+-]{1,64}\Z")


class BindingReadError(ValueError):
    """Private fixed codes; never echo bytes, paths or OS errors."""


def _require(condition, code):
    if not condition:
        raise BindingReadError(code)


def _int(value, low=0):
    return type(value) is int and low <= value <= sj.MAX_INT


def _digest(value):
    return type(value) is str and _SHA.fullmatch(value) is not None


def _identity(value):
    return (type(value) is tuple and len(value) == 2
            and _int(value[0]) and _int(value[1], 1))


def _path(value):
    return (type(value) is str and 0 < len(value) <= MAX_PATH
            and "\x00" not in value and os.path.isabs(value)
            and os.path.normpath(value) == value and value != "/"
            and not value.startswith("//") and len(value.split("/")) <= 128)


class _Handles:
    def __init__(self):
        self.fds = []

    def __enter__(self):
        return self

    def open(self, path, *, directory=False, parent=None):
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOCTTY
        if directory:
            flags |= os.O_DIRECTORY
        fd = os.open(path, flags, dir_fd=parent)
        try:
            self.fds.append(fd)
        except BaseException:
            try:
                os.close(fd)
            except BaseException:
                pass  # Preserve the primary failure; no cleanup success claim.
            raise
        return fd

    def root(self, path):
        current = self.open("/", directory=True)
        for part in path[1:].split("/"):
            current = self.open(part, directory=True, parent=current)
        return current

    def __exit__(self, exc_type, exc, traceback):
        first = None
        for fd in reversed(self.fds):
            try:
                os.close(fd)
            except BaseException as error:
                if first is None:
                    first = error
        self.fds.clear()
        if exc_type is None and first is not None:
            raise first
        return False


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _root_matches(fd, identity):
    info = os.fstat(fd)
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
             and stat.S_IMODE(info.st_mode) == 0o700
             and (info.st_dev, info.st_ino) == identity, "root_identity_mismatch")


def _read(handles, directory, name):
    fd = handles.open(name, parent=directory)
    info = os.fstat(fd)
    _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
             and stat.S_IMODE(info.st_mode) == 0o600
             and info.st_size <= MAX_BYTES, "file_invalid")
    data = bytearray()
    while len(data) <= MAX_BYTES:
        chunk = os.read(fd, MAX_BYTES + 1 - len(data))
        if not chunk:
            break
        data.extend(chunk)
    _require(len(data) <= MAX_BYTES and len(data) == info.st_size
             and _stamp(os.fstat(fd)) == _stamp(info), "file_changed")
    return bytes(data), fd, _stamp(info)


@dataclass(frozen=True)
class BindingInputs:
    core_sha256: str
    launch_sha256: str
    readiness_sha256: str
    supervisor: tuple[int, int]
    child: tuple[int, int]
    boot_id: str
    venv_path: str
    venv_identity: tuple[int, int]
    observer_manifest_sha256: str
    core_bytes: bytes
    launch_bytes: bytes
    scope: str = "CORE_LAUNCH_READINESS_BYTES_ONLY_NOT_STAGE_OR_PROCESS_QUALIFICATION"


def read_binding_inputs(job_root, root_identity, *, expected_core, core_sha256,
                        launch_sha256, readiness_line, readiness_sha256,
                        expected_version, expected_port, expected_venv_path,
                        observer_manifest_sha256):
    """Read a single bounded private snapshot using independent caller pins.

    Fixed launcher layout: core-identity.json, reference/launch.json,
    reference/terminal.json, observer/observer.sock and reference/observer-install.json.
    Readiness bytes are caller-provided original stdout, independently pinned;
    parsing or hashing them cannot prove their origin or a closed launch stage.
    Actual venv/interpreter inspection is deliberately outside this component.
    All directory ancestors are opened without following symlinks. Rechecks
    detect ordinary replacement; same-user mutation is not a security boundary.
    """
    _require(_path(job_root) and _identity(root_identity)
             and _path(expected_venv_path)
             and expected_venv_path.startswith(job_root + "/"), "expected_identity_invalid")
    _require(all(_digest(x) for x in (core_sha256, launch_sha256,
                                     readiness_sha256, observer_manifest_sha256)),
             "expected_digest_invalid")
    _require(type(expected_core) is dict and (
        (expected_core.get("schema") == sj.SCHEMA_CORE
         and set(expected_core) == CORE_EXPECTATIONS)
        or (expected_core.get("schema") == sj.SCHEMA_CORE_V2
            and set(expected_core) == CORE_EXPECTATIONS_V2)), "expected_core_invalid")
    _require(type(expected_version) is str and _VERSION.fullmatch(expected_version)
             and type(expected_port) is int and 1 <= expected_port <= 65535,
             "expected_readiness_invalid")
    _require(type(readiness_line) is bytes and 0 < len(readiness_line) <= MAX_BYTES
             and hashlib.sha256(readiness_line).hexdigest() == readiness_sha256,
             "readiness_digest_mismatch")
    try:
        with _Handles() as handles:
            root = handles.root(job_root)
            _root_matches(root, root_identity)
            reference = handles.open("reference", directory=True, parent=root)
            ref_info = os.fstat(reference)
            _require(ref_info.st_uid == os.getuid()
                     and stat.S_IMODE(ref_info.st_mode) == 0o700, "reference_invalid")
            core_data, core_fd, core_stamp = _read(handles, root, sj.CORE_NAME)
            launch_data, launch_fd, launch_stamp = _read(handles, reference, "launch.json")
            _require(hashlib.sha256(core_data).hexdigest() == core_sha256,
                     "core_digest_mismatch")
            _require(hashlib.sha256(launch_data).hexdigest() == launch_sha256,
                     "launch_digest_mismatch")
            # Existing fully typed schema, also applied to independent inputs
            # so Python's bool/int equality cannot authenticate forged pins.
            sj.validate_core(sj._dumps(dict(expected_core, captured_utc_ns=0,
                                           captured_monotonic_ns=0)), job_root)
            core = sj.validate_core(core_data, job_root)
            _require(set(core) - {"captured_utc_ns", "captured_monotonic_ns"}
                     == set(expected_core)
                     and {k: core[k] for k in expected_core} == expected_core,
                     "core_expectation_mismatch")
            _require(core["boot_id"] == sj.boot_id(), "boot_mismatch")
            launch = sj.strict_json(launch_data, "launch_invalid")
            _require(type(launch) is dict and set(launch) == LAUNCH_KEYS
                     and launch["schema"] == "alrust-reference-launch/1", "launch_invalid")
            sup, child, venv = launch["supervisor"], launch["child"], launch["venv"]
            _require(type(sup) is dict and set(sup) == {
                "pid", "pgid", "start_ticks", "uid", "subreaper"}
                and all(_int(sup[k], 1) for k in ("pid", "pgid", "start_ticks"))
                and sup["pgid"] == sup["pid"] and type(sup["uid"]) is int
                and sup["uid"] == core["uid"] and type(sup["subreaper"]) is int
                and sup["subreaper"] == 1, "launch_supervisor_invalid")
            _require(type(child) is dict and set(child) == {"pid", "start_ticks"}
                     and all(_int(v, 1) for v in child.values())
                     and child["pid"] != sup["pid"], "launch_child_invalid")
            _require(type(venv) is dict and set(venv) == {"path", "dev", "ino"}
                     and venv["path"] == expected_venv_path
                     and _identity((venv["dev"], venv["ino"])), "launch_venv_invalid")
            observer = {"socket": job_root + "/observer/observer.sock",
                        "manifest": job_root + "/reference/observer-install.json",
                        "manifest_sha256": observer_manifest_sha256}
            _require(launch["observer"] == observer
                     and launch["observer_socket"] == observer["socket"]
                     and launch["terminal_receipt"] == job_root + "/reference/terminal.json",
                     "launch_observer_invalid")
            _require(launch["boot_id"] == core["boot_id"]
                     and _int(launch["launched_utc_ns"])
                     and _int(launch["monotonic_ns"])
                     and core["captured_monotonic_ns"] <= launch["monotonic_ns"],
                     "launch_clock_or_boot_invalid")
            expected_line = ("reference alerta %s up on :%d (supervisor %d, child %d, "
                             "receipt %s/reference/launch.json sha256 %s)\n" % (
                                 expected_version, expected_port, sup["pid"],
                                 child["pid"], job_root, launch_sha256)).encode()
            _require(readiness_line == expected_line, "readiness_binding_mismatch")
            for parent, name, fd, stamp in ((root, sj.CORE_NAME, core_fd, core_stamp),
                                          (reference, "launch.json", launch_fd, launch_stamp)):
                _require(_stamp(os.fstat(fd)) == stamp
                         and _stamp(os.stat(name, dir_fd=parent, follow_symlinks=False)) == stamp,
                         "file_changed")
            current_ref = handles.open("reference", directory=True, parent=root)
            _require(_stamp(os.fstat(current_ref)) == _stamp(ref_info), "reference_changed")
            _root_matches(handles.root(job_root), root_identity)
            return BindingInputs(core_sha256, launch_sha256, readiness_sha256,
                                 (sup["pid"], sup["start_ticks"]),
                                 (child["pid"], child["start_ticks"]), core["boot_id"],
                                 venv["path"], (venv["dev"], venv["ino"]),
                                 observer_manifest_sha256, core_data, launch_data)
    except BindingReadError:
        raise
    except (sj.JournalError, OSError, ValueError, TypeError, RecursionError):
        raise BindingReadError("binding_read_failed") from None
