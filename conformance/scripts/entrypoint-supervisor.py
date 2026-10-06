#!/usr/bin/env python3
"""Supervise the executing entrypoint and prove its whole process tree ended.

Design: conformance/entrypoint-exit-proof-design.md, approved by Codex at
409eabfa on branch claude/ci-entrypoint-exit-proof (not yet on main),
sections 2, 3 and 3a. This head is item 1 of its section 8 only: the
supervisor and its offline controls; the entrypoint-exit verifier and the
gates are later heads. A sibling of reference-supervisor.py: the teardown
(subreaper, TERM then KILL to its own unreaped children only, reap until
ECHILD) is the same; the differences are listed in the design, section 2.

Run as `python3 -I -S -B entrypoint-supervisor.py --receipt R --terminal T
--core C --core-sha256 H --script S --entrypoint-journal J
--deadline-monotonic-ns D -- COMMAND...` from a step that made this process
the leader of its own process group. COMMAND must name S as one argument.

- It checks, before the fork: group leadership, absent receipts, the core
  file against the independently supplied H, the script's digest, and that
  the deadline D leaves at least the teardown budget. Any failure exits
  before a child exists.
- It marks itself a child subreaper, forks C_E and writes the launch
  receipt R exclusively. Secrets reach C_E only through this fork.
- On C_E's exit, on SIGTERM or SIGINT, or at D, it tears the tree down and
  reaps until waitpid reports ECHILD.
- Only after ECHILD does it read the closed entrypoint journal J (bounded,
  read-only). It then writes the terminal receipt T exclusively.

Rules, as in reference-supervisor.py: signal handlers only set a flag;
SIGCHLD keeps its default action and nothing else reaps; between scanning
/proc for children and signalling them nothing is reaped.
"""
import ctypes
import hashlib
import json
import os
import signal
import stat
import sys
import time

SCHEMA_LAUNCH = "alrust-entrypoint-launch/1"
SCHEMA_TERMINAL = "alrust-entrypoint-terminal/1"
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
TERM_SECONDS = 10.0
KILL_SECONDS = 5.0
RECEIPT_ALLOWANCE_NS = 1_000_000_000
# Design section 3a: B = TERM phase + KILL phase + the receipt allowance.
TEARDOWN_BUDGET_NS = int((TERM_SECONDS + KILL_SECONDS) * 1e9) + RECEIPT_ALLOWANCE_NS
POLL_SECONDS = 0.05
MAX_CORE = 1 << 16
MAX_SCRIPT = 1 << 22
MAX_JOURNAL = 1 << 20

EXIT_TREE_EMPTY = 0
EXIT_USAGE = 64
EXIT_SETUP = 70
EXIT_TREE_NOT_EMPTY = 3
EXIT_RECEIPT = 4

stop_requested = False


def request_stop(signum, frame):
    global stop_requested
    stop_requested = True


def fail(code, message):
    sys.stderr.write("entrypoint-supervisor: %s\n" % message)
    sys.exit(code)


def is_sha256(value):
    return len(value) == 64 and not value.strip("0123456789abcdef")


OPTIONS = ("--receipt", "--terminal", "--core", "--core-sha256", "--script",
           "--entrypoint-journal", "--deadline-monotonic-ns")
PATHS = ("--receipt", "--terminal", "--core", "--script", "--entrypoint-journal")


def parse_args(argv):
    opts = dict.fromkeys(OPTIONS)
    i = 0
    while i < len(argv) and argv[i] != "--":
        name = argv[i]
        if name not in opts or i + 1 >= len(argv) or opts[name] is not None:
            fail(EXIT_USAGE, "bad argument %r" % name)
        opts[name] = argv[i + 1]
        i += 2
    command = argv[i + 1:]
    if not command or any(opts[name] is None for name in OPTIONS):
        fail(EXIT_USAGE, "usage: --receipt R --terminal T --core C --core-sha256 H "
                         "--script S --entrypoint-journal J "
                         "--deadline-monotonic-ns D -- COMMAND...")
    for name in PATHS:
        if not os.path.isabs(opts[name]) or os.path.normpath(opts[name]) != opts[name]:
            fail(EXIT_USAGE, "%s must be an absolute normalized path" % name)
    if not os.path.isabs(command[0]):
        fail(EXIT_USAGE, "COMMAND[0] must be an absolute path")
    if not is_sha256(opts["--core-sha256"]):
        fail(EXIT_USAGE, "--core-sha256 must be 64 lowercase hex characters")
    deadline = opts["--deadline-monotonic-ns"]
    if not deadline.isdigit() or len(deadline) > 20:
        fail(EXIT_USAGE, "--deadline-monotonic-ns must be a decimal integer")
    opts["--deadline-monotonic-ns"] = int(deadline)
    if command[1:].count(opts["--script"]) != 1:
        fail(EXIT_USAGE, "COMMAND must name --script exactly once")
    return opts, command


def read_stat(pid):
    """Return (ppid, start_ticks) from /proc/PID/stat, or None if absent."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            data = f.read(4096)
    except FileNotFoundError:
        return None
    except ProcessLookupError:
        return None
    rest = data[data.rindex(b")") + 2:].split()
    # rest[0] is field 3 (state); field 4 is ppid; field 22 is starttime.
    return int(rest[1]), int(rest[19])


def boot_id():
    with open("/proc/sys/kernel/random/boot_id") as f:
        return f.read().strip()


def read_regular(path, limit):
    """Read a regular file without following a final symlink or blocking on
    a FIFO; bounded. Returns (bytes, stat)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular file")
        pieces, size = [], 0
        while size <= limit:
            chunk = os.read(fd, limit + 1 - size)
            if not chunk:
                break
            pieces.append(chunk)
            size += len(chunk)
        if size > limit:
            raise ValueError("too large")
        return b"".join(pieces), info
    finally:
        os.close(fd)


def write_exclusive(path, record):
    data = (json.dumps(record, sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                 | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    dfd = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return hashlib.sha256(data).hexdigest()


def set_subreaper():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER")
    value = ctypes.c_int(0)
    if libc.prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(value), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_GET_CHILD_SUBREAPER")
    return value.value


def children(me):
    """PIDs whose parent is this process.

    An unreadable entry is ignored here. Completeness is never inferred
    from this scan: only waitpid's ECHILD proves that no child is left.
    """
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            stat_ = read_stat(int(name))
        except (OSError, ValueError):
            continue
        if stat_ is not None and stat_[0] == me:
            found.append(int(name))
    return found


def reap_all():
    """Reap every exited child without blocking.

    Returns (reaped [(pid, status)], no_children).
    """
    reaped = []
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return reaped, True
        if pid == 0:
            return reaped, False
        reaped.append((pid, status))


def record_reaped(log, child, reaped):
    """Attribute reaped statuses (REP1-R1).

    C_E's PID names the original generation only until that generation is
    reaped: an unreaped zombie keeps its PID, so before then no other
    process can hold it. Once C_E's status is collected, a later holder of
    the same PID (an adopted descendant that reused it) is a different
    process. The first collected status is kept and never overwritten,
    also when two statuses for that PID arrive in one drained batch."""
    for pid, status in reaped:
        if pid == child and log["child_status"] is None:
            log["child_status"] = status
        else:
            log["other_reaped"] += 1


def signal_phase(me, sig, seconds, log, child):
    """Signal current children, then reap, until ECHILD or the deadline.

    Signals to C_E and to any other descendant are counted separately: a
    signal to another descendant means something outlived the entrypoint
    (design section 3, the normal-path completeness guardrail)."""
    deadline = time.monotonic() + seconds
    while True:
        # Scan, then signal, then reap: nothing is reaped between the scan
        # and the signals, so each PID is still our unreaped child.
        for pid in children(me):
            try:
                os.kill(pid, sig)
                log["signals"] += 1
                # Only the unreaped original generation is C_E (REP1-R1).
                if pid == child and log["child_status"] is None:
                    log["child_signalled"] = True
                else:
                    log["other_signals"] += 1
            except ProcessLookupError:
                pass
        reaped, empty = reap_all()
        log["reaped"] += len(reaped)
        record_reaped(log, child, reaped)
        if empty:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)


def journal_observation(path):
    """Read the entrypoint journal after ECHILD: its sha256 and close state.

    This is an observation, not a validation; the verifier and the stage
    aggregation validate the chain. COMPLETE or FAILED only when the last
    line is a well-formed stage_complete record; anything else, including
    a missing or unreadable file, is INCOMPLETE."""
    try:
        data, _ = read_regular(path, MAX_JOURNAL)
    except (OSError, ValueError):
        return {"sha256": None, "state": "INCOMPLETE", "code": None}
    digest = hashlib.sha256(data).hexdigest()
    incomplete = {"sha256": digest, "state": "INCOMPLETE", "code": None}
    if not data.endswith(b"\n"):
        return incomplete
    try:
        last = json.loads(data[:-1].split(b"\n")[-1])
        evidence = last["evidence"]
        payload = evidence["payload"]
        if type(last) is not dict or set(last) != {"sequence", "evidence"} \
                or evidence["kind"] != "stage_complete":
            return incomplete
    except (ValueError, KeyError, TypeError, IndexError):
        return incomplete
    if payload == {"result": "COMPLETE"}:
        return {"sha256": digest, "state": "COMPLETE", "code": None}
    if (type(payload) is dict and set(payload) == {"result", "code"}
            and payload["result"] == "FAILED" and type(payload["code"]) is str):
        return {"sha256": digest, "state": "FAILED", "code": payload["code"]}
    return incomplete


def main(argv):
    opts, command = parse_args(argv)
    me = os.getpid()
    if os.getpgid(0) != me:
        fail(EXIT_SETUP, "not the leader of its own process group")
    for name in (opts["--receipt"], opts["--terminal"]):
        if os.path.lexists(name):
            fail(EXIT_SETUP, "%s already exists" % name)
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        readback = set_subreaper()
    except OSError as error:
        fail(EXIT_SETUP, "subreaper unavailable: %s" % error)
    if readback != 1:
        fail(EXIT_SETUP, "subreaper read-back %r" % readback)
    # Everything that can fail is read before the fork, so a failure here
    # never leaves an unsupervised child.
    try:
        my_stat = read_stat(me)
        boot = boot_id()
        core, _ = read_regular(opts["--core"], MAX_CORE)
        script, script_info = read_regular(opts["--script"], MAX_SCRIPT)
        executable = os.stat(command[0])
    except (OSError, ValueError) as error:
        fail(EXIT_SETUP, "setup read failed: %s" % error)
    if my_stat is None:
        fail(EXIT_SETUP, "own /proc entry unreadable")
    core_sha256 = hashlib.sha256(core).hexdigest()
    if core_sha256 != opts["--core-sha256"]:
        fail(EXIT_SETUP, "core identity does not match --core-sha256")
    deadline = opts["--deadline-monotonic-ns"]
    if deadline < time.monotonic_ns() + TEARDOWN_BUDGET_NS:
        fail(EXIT_SETUP, "deadline leaves less than the teardown budget")

    child = os.fork()
    if child == 0:
        try:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            os.execv(command[0], command)
        finally:
            os._exit(127)

    try:
        child_stat = read_stat(child)
    except (OSError, ValueError):
        child_stat = None
    log = {"signals": 0, "reaped": 0, "child_signalled": False,
           "other_signals": 0, "other_reaped": 0, "child_status": None}
    argv_digest = hashlib.sha256(json.dumps(command).encode()).hexdigest()
    receipt = {
        "schema": SCHEMA_LAUNCH,
        "supervisor": {"pid": me, "pgid": os.getpgid(0),
                       "start_ticks": my_stat[1], "uid": os.getuid(),
                       "subreaper": readback},
        "child": {"pid": child,
                  "start_ticks": child_stat[1] if child_stat else None},
        "boot_id": boot,
        "core_identity_sha256": core_sha256,
        "command": {
            "argv_sha256": argv_digest,
            "executable": {"path": command[0], "dev": executable.st_dev,
                           "ino": executable.st_ino},
            "script": {"path": opts["--script"], "dev": script_info.st_dev,
                       "ino": script_info.st_ino,
                       "sha256": hashlib.sha256(script).hexdigest()},
        },
        "entrypoint_journal": opts["--entrypoint-journal"],
        "terminal_receipt": opts["--terminal"],
        "deadline": {"monotonic_ns": deadline,
                     "teardown_budget_ns": TEARDOWN_BUDGET_NS, "boot_id": boot},
        "launched_utc_ns": time.time_ns(),
        "monotonic_ns": time.monotonic_ns(),
    }
    receipt_ok = True
    launch_sha256 = None
    try:
        launch_sha256 = write_exclusive(opts["--receipt"], receipt)
    except OSError as error:
        sys.stderr.write("entrypoint-supervisor: launch receipt: %s\n" % error)
        receipt_ok = False

    trigger = "receipt_failed" if not receipt_ok else None
    while trigger is None:
        if time.monotonic_ns() >= deadline:
            trigger = "deadline"
            break
        if stop_requested:
            trigger = "stop_requested"
            break
        reaped, _ = reap_all()
        log["reaped"] += len(reaped)
        record_reaped(log, child, reaped)
        if log["child_status"] is not None:
            trigger = "child_exited"
            break
        time.sleep(POLL_SECONDS)

    started = time.monotonic_ns()
    empty = signal_phase(me, signal.SIGTERM, TERM_SECONDS, log, child)
    if not empty:
        empty = signal_phase(me, signal.SIGKILL, KILL_SECONDS, log, child)
    # The journal is read only after ECHILD: no descendant can still write.
    journal = journal_observation(opts["--entrypoint-journal"]) if empty else None
    status = log["child_status"]
    execution = "PASS" if (
        empty and trigger == "child_exited" and status is not None
        and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        # Kept as defence in depth. With the generation-aware attribution
        # above, C_E can be signalled only before its status is collected,
        # so on trigger "child_exited" this should not be needed; Codex
        # showed (REP1-N1) that it was needed before that fix.
        and not log["child_signalled"]
        and journal is not None and journal["state"] == "COMPLETE") else "FAIL"
    terminal = {
        "schema": SCHEMA_TERMINAL,
        "result": "tree_empty" if empty else "tree_not_empty",
        "supervisor": {"pid": me, "start_ticks": my_stat[1]},
        "boot_id": boot,
        "launch_receipt_sha256": launch_sha256,
        "trigger": trigger,
        "child_wait_status": status,
        "child_signalled": log["child_signalled"],
        "other_descendant_signals": log["other_signals"],
        "other_descendants_reaped": log["other_reaped"],
        "entrypoint_journal": journal,
        "execution": execution,
        "signals_sent": log["signals"],
        "children_reaped": log["reaped"],
        "teardown_ns": time.monotonic_ns() - started,
        "finished_utc_ns": time.time_ns(),
        "finished_monotonic_ns": time.monotonic_ns(),
    }
    try:
        write_exclusive(opts["--terminal"], terminal)
    except OSError as error:
        fail(EXIT_RECEIPT, "terminal receipt: %s" % error)
    if not receipt_ok:
        return EXIT_RECEIPT
    return EXIT_TREE_EMPTY if empty else EXIT_TREE_NOT_EMPTY


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
