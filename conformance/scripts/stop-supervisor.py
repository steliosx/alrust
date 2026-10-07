#!/usr/bin/env python3
"""Stop a still-live entrypoint or stage-tool supervisor before its exit stage.

R-EP plan P4. Design conformance/entrypoint-exit-proof-design.md section 4,
the row "P_E still live with a matching generation (via pidfd), for example
after a step timeout: TERM via pidfd, poll up to 20 s". Contract:
tool-exit-contract.md section 9.

This helper proves nothing. It only makes the exit-stage verifier's positive
row reachable when a supervisor outlived its step; the verifier
(entrypoint_exit / tool_exit) then decides from the receipts, unchanged. Its
only hazard is signalling the wrong process, so every check below exists to
refuse to signal, not to produce evidence.

Usage:
  python3 -I -S -B stop-supervisor.py --job-root J --entrypoint
  python3 -I -S -B stop-supervisor.py --job-root J --tool NAME --stage-index N

The workflow runs it unconditionally before each exit stage; on a
supervisor that has already ended it is a no-op.

Binding (there is no caller-supplied receipt digest: no launcher ready line
exists for P_E or P_T): the private job root J; the fixed launch-receipt
path for the selected tree (entrypoint/launch.json, or
tools/NN-NAME/launch.json); the exact launch schema and, for a tool, the
receipt's tool binding equal to NAME and N; the receipt's terminal path
equal to the fixed one; its core_identity_sha256 equal to the digest of the
core read now, validated for J; its boot_id equal to the core's and to
/proc now; then, through a pidfd, the supervisor's generation (start_ticks
and uid). A stale receipt from an earlier attempt is harmless: a reused PID
has other start_ticks and is never signalled.

The helper sends exactly one signal, SIGTERM, with pidfd_send_signal and
flags 0, to the supervisor named by the receipt, only after the pidfd's
generation is confirmed and only if no terminal receipt exists yet. It
never sends a kill signal, never signals a group, a numeric PID or any
other process. The supervisor's own teardown owns its tree (TERM 10 s, KILL
5 s, receipt 1 s: B = 16 s), so the bounded 20 s wait on the pidfd, inside
this one invocation, covers a supervisor that honours TERM. One that does
not is UNPROVEN.

Output: one JSON line {"outcome", "signalled", "reason"}; exit 0 for
ENDED (the supervisor's generation is not live) and 2 for UNPROVEN.
Neither outcome is a verdict.
"""
import hashlib
import json
import os
import re
import select
import signal
import stat
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EXIT_WAIT_SECONDS = 20.0
MAX_RECEIPT = 65536
PID_LIMIT = 4194304
EXIT = {"ENDED": 0, "UNPROVEN": 2}
_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
_INDEX = re.compile(r"(0|[1-9][0-9]?)\Z")


def outcome(name, reason, signalled=False):
    sys.stdout.write(json.dumps({"outcome": name, "signalled": signalled,
                                 "reason": reason}, sort_keys=True) + "\n")
    sys.stdout.flush()
    return EXIT[name]


def package():
    conformance = os.path.dirname(HERE)
    if conformance not in sys.path:
        sys.path.insert(0, conformance)
    from alrust_conformance import entrypoint_exit, stage_journal, tool_exit
    return entrypoint_exit, stage_journal, tool_exit


def read_bounded(path, limit=MAX_RECEIPT):
    """A small regular file, opened without following links or blocking."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
                 | os.O_NOCTTY)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("not a small regular file")
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise ValueError("too large")
        return data
    finally:
        os.close(fd)


def read_generation(pid):
    """(start_ticks, uid) of PID from /proc, or None if PID is absent."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            data = f.read(4096)
        uid = os.stat("/proc/%d" % pid).st_uid
    except (FileNotFoundError, ProcessLookupError):
        return None
    rest = data[data.rindex(b")") + 2:].split()
    return int(rest[19]), uid


def boot_id():
    with open("/proc/sys/kernel/random/boot_id") as f:
        return f.read().strip()


def wait_exit(fd, seconds):
    """True once the process behind pidfd FD has exited (bounded wait)."""
    deadline = time.monotonic() + seconds
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if poller.poll(int(remaining * 1000) + 1):
            return True


def private_root(job_root):
    try:
        info = os.lstat(job_root)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() \
        and not info.st_mode & 0o077 and os.path.realpath(job_root) == job_root


def parse_args(argv):
    """(job_root, tool, stage_index); tool None for the entrypoint."""
    if len(argv) == 3 and argv[0] == "--job-root" and argv[2] == "--entrypoint":
        return argv[1], None, None
    if len(argv) == 6 and argv[0::2] == ["--job-root", "--tool", "--stage-index"] \
            and _NAME.fullmatch(argv[3]) and _INDEX.fullmatch(argv[5]):
        return argv[1], argv[3], int(argv[5])
    raise ValueError("usage")


def selected(ee, te, job_root, tool, index):
    """(launch_path, terminal_path, launch_schema, terminal_schema, binding)."""
    if tool is None:
        launch, terminal = ee.receipt_paths(job_root)
        return launch, terminal, ee.SCHEMA_LAUNCH, ee.SCHEMA_TERMINAL, None
    if tool not in te.HOLDER_TOOLS:
        raise ValueError("usage")
    launch, terminal = te.receipt_paths(job_root, tool, index)
    return launch, terminal, te.SCHEMA_LAUNCH, te.SCHEMA_TERMINAL, \
        {"name": tool, "stage_index": index}


def supervisor_of(receipt, schema, terminal_path, binding, core_sha256, boot):
    """(pid, start_ticks, uid) of the receipt's supervisor; ValueError if the
    receipt is not this job's, this tree's and this boot's."""
    if type(receipt) is not dict or receipt.get("schema") != schema:
        raise ValueError("schema")
    sup = receipt["supervisor"]
    pid, ticks, uid = sup["pid"], sup["start_ticks"], sup["uid"]
    if not (type(pid) is int and 1 < pid <= PID_LIMIT and sup["pgid"] == pid
            and type(ticks) is int and 1 < ticks < 2 ** 63
            and type(uid) is int and 0 <= uid < 2 ** 32):
        raise ValueError("supervisor")
    if receipt["terminal_receipt"] != terminal_path \
            or receipt["core_identity_sha256"] != core_sha256 \
            or receipt["boot_id"] != boot:
        raise ValueError("binding")
    if binding is not None and receipt.get("tool") != binding:
        raise ValueError("tool")
    return pid, ticks, uid


def main(argv):
    try:
        job_root, tool, index = parse_args(argv)
    except ValueError:
        return outcome("UNPROVEN", "usage")
    if not (os.path.isabs(job_root) and os.path.normpath(job_root) == job_root
            and job_root != "/" and private_root(job_root)):
        return outcome("UNPROVEN", "job_root_invalid")
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return outcome("UNPROVEN", "pidfd_unsupported")
    try:
        ee, sj, te = package()
        launch_path, terminal_path, schema, _, binding = selected(ee, te, job_root, tool, index)
    except (ImportError, ValueError):
        return outcome("UNPROVEN", "usage")
    try:
        core_bytes = read_bounded(os.path.join(job_root, sj.CORE_NAME), sj.MAX_CORE)
        core = sj.validate_core(core_bytes, job_root)
    except (OSError, ValueError, sj.JournalError):
        return outcome("UNPROVEN", "core_unreadable")
    try:
        if boot_id() != core["boot_id"]:
            return outcome("UNPROVEN", "boot_mismatch")
    except OSError:
        return outcome("UNPROVEN", "boot_unreadable")
    try:
        receipt = json.loads(read_bounded(launch_path))
    except FileNotFoundError:
        return outcome("UNPROVEN", "no_launch_receipt")
    except (OSError, ValueError, RecursionError):
        return outcome("UNPROVEN", "receipt_unreadable")
    try:
        pid, ticks, uid = supervisor_of(receipt, schema, terminal_path, binding,
                                        hashlib.sha256(core_bytes).hexdigest(),
                                        core["boot_id"])
    except (KeyError, TypeError, ValueError):
        return outcome("UNPROVEN", "receipt_not_bound")
    try:
        own = {os.getpid(), os.getppid(), os.getpgid(0), os.getsid(0)}
    except OSError:
        return outcome("UNPROVEN", "self_unreadable")
    if pid in own:
        return outcome("UNPROVEN", "refused_own_process")

    try:
        fd = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        return outcome("ENDED", "not_running")
    except OSError:
        return outcome("UNPROVEN", "pidfd_open_failed")
    try:
        # The pidfd names whatever held PID when it was opened; a matching
        # generation read afterwards proves it is the receipt's supervisor.
        try:
            current = read_generation(pid)
        except (OSError, ValueError, IndexError):
            return outcome("UNPROVEN", "generation_unreadable")
        if current is None or current[0] != ticks:
            return outcome("ENDED", "not_running")      # PID reused: never signalled
        if current[1] != uid:
            return outcome("UNPROVEN", "uid_mismatch")  # never signalled
        signalled = False
        if not os.path.lexists(terminal_path):
            try:
                signal.pidfd_send_signal(fd, signal.SIGTERM, None, 0)
                signalled = True
            except ProcessLookupError:
                pass
            except OSError:
                return outcome("UNPROVEN", "signal_failed")
        if wait_exit(fd, EXIT_WAIT_SECONDS):
            return outcome("ENDED", "exited", signalled)
        return outcome("UNPROVEN", "did_not_exit", signalled)
    finally:
        os.close(fd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
