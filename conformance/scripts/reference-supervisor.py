#!/usr/bin/env python3
"""Supervise one reference server and prove its whole process tree ended.

Contract: docs/ci-launch-attestation-contract.md, section 5. Run as
`python3 -I -S -B reference-supervisor.py --receipt R --terminal T
--venv V [--observer-socket S] -- COMMAND...` from a launcher that made
this process the leader of its own process group.

- It marks itself a child subreaper, so every orphaned descendant is
  reparented to it while it lives.
- It forks the served child C and writes the launch receipt R
  exclusively.
- On SIGTERM or SIGINT, or when C exits, it tears the tree down:
  1. TERM, then KILL, to its own unreaped children only;
  2. it reaps until waitpid reports ECHILD.
- It then writes the terminal receipt T exclusively: tree_empty or
  tree_not_empty.

Rules:
- Signal handlers only set a flag.
- SIGCHLD keeps its default action, and nothing else reaps.
- Between scanning /proc for children and signalling them nothing is
  reaped, so a scanned PID still names the same unreaped child.
"""
import ctypes
import hashlib
import json
import os
import signal
import sys
import time

SCHEMA_LAUNCH = "alrust-reference-launch/1"
SCHEMA_TERMINAL = "alrust-reference-terminal/1"
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
TERM_SECONDS = 10.0
KILL_SECONDS = 5.0
POLL_SECONDS = 0.05
OBSERVER_SOCKET = "ALRUST_OBSERVER_SOCKET"
OBSERVER_PARENT = "ALRUST_OBSERVER_PARENT"

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
    sys.stderr.write("reference-supervisor: %s\n" % message)
    sys.exit(code)


def parse_args(argv):
    opts = {"--receipt": None, "--terminal": None, "--venv": None,
            "--observer-socket": None}
    i = 0
    while i < len(argv) and argv[i] != "--":
        name = argv[i]
        if name not in opts or i + 1 >= len(argv) or opts[name] is not None:
            fail(EXIT_USAGE, "bad argument %r" % name)
        opts[name] = argv[i + 1]
        i += 2
    command = argv[i + 1:]
    if not command or opts["--receipt"] is None or opts["--terminal"] is None \
            or opts["--venv"] is None:
        fail(EXIT_USAGE, "usage: --receipt R --terminal T --venv V "
                         "[--observer-socket S] -- COMMAND...")
    for name in ("--receipt", "--terminal", "--venv", "--observer-socket"):
        if opts[name] is not None and not os.path.isabs(opts[name]):
            fail(EXIT_USAGE, "%s must be absolute" % name)
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
            stat = read_stat(int(name))
        except (OSError, ValueError):
            continue
        if stat is not None and stat[0] == me:
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


def signal_phase(me, sig, seconds, log):
    """Signal current children, then reap, until ECHILD or the deadline."""
    deadline = time.monotonic() + seconds
    while True:
        # Scan, then signal, then reap: nothing is reaped between the scan
        # and the signals, so each PID is still our unreaped child.
        for pid in children(me):
            try:
                os.kill(pid, sig)
                log["signals"] += 1
            except ProcessLookupError:
                pass
        reaped, empty = reap_all()
        log["reaped"] += len(reaped)
        if empty:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)


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
        venv = os.stat(opts["--venv"])
        boot = boot_id()
    except (OSError, ValueError) as error:
        fail(EXIT_SETUP, "setup read failed: %s" % error)
    if my_stat is None:
        fail(EXIT_SETUP, "own /proc entry unreadable")

    env = dict(os.environ)
    env.pop(OBSERVER_SOCKET, None)
    env.pop(OBSERVER_PARENT, None)
    if opts["--observer-socket"] is not None:
        env[OBSERVER_SOCKET] = opts["--observer-socket"]
        env[OBSERVER_PARENT] = str(me)

    child = os.fork()
    if child == 0:
        try:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            os.execve(command[0], command, env)
        finally:
            os._exit(127)

    try:
        child_stat = read_stat(child)
    except (OSError, ValueError):
        child_stat = None
    log = {"signals": 0, "reaped": 0}
    receipt = {
        "schema": SCHEMA_LAUNCH,
        "supervisor": {"pid": me, "pgid": os.getpgid(0),
                       "start_ticks": my_stat[1], "uid": os.getuid(),
                       "subreaper": readback},
        "child": {"pid": child,
                  "start_ticks": child_stat[1] if child_stat else None},
        "boot_id": boot,
        "venv": {"path": opts["--venv"], "dev": venv.st_dev,
                 "ino": venv.st_ino},
        "terminal_receipt": opts["--terminal"],
        "observer_socket": opts["--observer-socket"],
        "launched_utc_ns": time.time_ns(),
        "monotonic_ns": time.monotonic_ns(),
    }
    receipt_ok = True
    try:
        write_exclusive(opts["--receipt"], receipt)
    except OSError as error:
        sys.stderr.write("reference-supervisor: launch receipt: %s\n" % error)
        receipt_ok = False

    child_status = None
    while receipt_ok and not stop_requested:
        reaped, _ = reap_all()
        log["reaped"] += len(reaped)
        for pid, status in reaped:
            if pid == child:
                child_status = status
        if child_status is not None:
            break
        time.sleep(POLL_SECONDS)

    started = time.monotonic_ns()
    empty = signal_phase(me, signal.SIGTERM, TERM_SECONDS, log)
    if not empty:
        empty = signal_phase(me, signal.SIGKILL, KILL_SECONDS, log)
    terminal = {
        "schema": SCHEMA_TERMINAL,
        "result": "tree_empty" if empty else "tree_not_empty",
        "supervisor": {"pid": me, "start_ticks": my_stat[1]},
        "boot_id": receipt["boot_id"],
        "trigger": ("receipt_failed" if not receipt_ok else
                    "stop_requested" if child_status is None else
                    "child_exited"),
        "child_wait_status": child_status,
        "signals_sent": log["signals"],
        "children_reaped": log["reaped"],
        "teardown_ns": time.monotonic_ns() - started,
        "finished_utc_ns": time.time_ns(),
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
