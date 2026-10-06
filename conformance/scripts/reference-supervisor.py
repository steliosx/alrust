#!/usr/bin/env python3
"""Supervise one reference server and prove its whole process tree ended.

Contract: docs/ci-launch-attestation-contract.md, section 5. Run as
`python3 -I -S -B reference-supervisor.py --receipt R --terminal T
--venv V --database-url-file U --secret-key-file K --database-port N
[--observer-socket S ...] -- COMMAND...` from a launcher that made this
process the leader of its own process group.

Server secrets, paths only (R-EP plan P2c; contract
tool-exit-contract.md section 10). This process never holds DATABASE_URL
or SECRET_KEY: it refuses to start if DATABASE_URL, SECRET_KEY or
PGPASSWORD is in its environment, and it is given only the two file
paths. The served child C reads them itself, and only after a barrier:
- before the fork, P creates a pipe; C closes the write end and blocks on
  a one-byte read;
- P writes the byte only after the launch receipt R is written and synced.
  If R cannot be written, P closes the pipe unwritten, and C exits (126)
  without opening either file;
- after the byte, C opens each path one component at a time from "/"
  without following links. Each file must be a private (no group or other
  bits) regular single-link file of this uid, bounded. The URL must be the
  job role's postgresql://NAME:PASSWORD@127.0.0.1:N/NAME with N the given
  port. The key must be 16..4096 bytes of printable text. Any refusal exits
  (125) with a fixed code, never a value;
- C then places DATABASE_URL and SECRET_KEY in its own environment and
  execs COMMAND exactly as before.
So the first read of either value happens after R exists, inside P's
supervised tree; C and its descendants are the only holders.

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
import re
import signal
import stat
import sys
import time

SCHEMA_LAUNCH = "alrust-reference-launch/1"
SCHEMA_TERMINAL = "alrust-reference-terminal/1"
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
TERM_SECONDS = 10.0
KILL_SECONDS = 5.0
POLL_SECONDS = 0.05
SECRET_OPTIONS = ("--database-url-file", "--secret-key-file", "--database-port")
SECRET_VARIABLES = ("DATABASE_URL", "SECRET_KEY", "PGPASSWORD")
MAX_URL = 1024
MIN_KEY, MAX_KEY = 16, 4096
# The job role's URL as database_stage writes it (its _URL, with the
# database name required equal to the role name).
_URL = re.compile(r"postgresql://(alrust_[a-z0-9_]{1,56}):([0-9a-f]{64})"
                  r"@127\.0\.0\.1:([1-9][0-9]{0,4})/\1\Z")
RELEASE = b"1"
EXIT_CHILD_BARRIER = 126
EXIT_CHILD_SECRET = 125
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


OBSERVER_OPTIONS = ("--observer-socket", "--observer-manifest",
                    "--observer-manifest-sha256")


def is_sha256(value):
    return len(value) == 64 and not value.strip("0123456789abcdef")


def parse_args(argv):
    opts = {"--receipt": None, "--terminal": None, "--venv": None}
    opts.update(dict.fromkeys(OBSERVER_OPTIONS))
    opts.update(dict.fromkeys(SECRET_OPTIONS))
    i = 0
    while i < len(argv) and argv[i] != "--":
        name = argv[i]
        if name not in opts or i + 1 >= len(argv) or opts[name] is not None:
            fail(EXIT_USAGE, "bad argument %r" % name)
        opts[name] = argv[i + 1]
        i += 2
    command = argv[i + 1:]
    if not command or any(opts[name] is None for name in
                          ("--receipt", "--terminal", "--venv") + SECRET_OPTIONS):
        fail(EXIT_USAGE, "usage: --receipt R --terminal T --venv V "
                         "--database-url-file U --secret-key-file K --database-port N "
                         "[--observer-socket S --observer-manifest M "
                         "--observer-manifest-sha256 H] -- COMMAND...")
    for name in ("--receipt", "--terminal", "--venv", "--observer-socket",
                 "--observer-manifest"):
        if opts[name] is not None and not os.path.isabs(opts[name]):
            fail(EXIT_USAGE, "%s must be absolute" % name)
    for name in ("--database-url-file", "--secret-key-file"):
        if not os.path.isabs(opts[name]) or os.path.normpath(opts[name]) != opts[name] \
                or opts[name] == "/":
            fail(EXIT_USAGE, "%s must be an absolute normalized file path" % name)
    port = opts["--database-port"]
    if not port.isdigit() or port != str(int(port)) or not 1 <= int(port) <= 65535:
        fail(EXIT_USAGE, "--database-port must be 1..65535")
    opts["--database-port"] = int(port)
    given = [opts[name] is not None for name in OBSERVER_OPTIONS]
    if any(given) and not all(given):
        fail(EXIT_USAGE, "the three observer options go together")
    name = "--observer-manifest-sha256"
    if opts[name] is not None and not is_sha256(opts[name]):
        fail(EXIT_USAGE, "%s must be 64 lowercase hex characters" % name)
    return opts, command


class SecretRefused(Exception):
    """A fixed code; never a value or a path."""


def read_private(path, limit):
    """The bytes of PATH, opened one component at a time from "/" without
    following links, only if it is a private regular single-link file of
    this uid of at most LIMIT bytes."""
    names = path.split("/")[1:]
    flags = os.O_RDONLY | os.O_CLOEXEC
    try:
        fd = os.open("/", flags | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise SecretRefused("secret_path_unreadable") from None
    try:
        for name in names[:-1]:
            try:
                child = os.open(name, flags | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError:
                raise SecretRefused("secret_path_unreadable") from None
            os.close(fd)
            fd = child
        try:
            leaf = os.open(names[-1], flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY,
                           dir_fd=fd)
        except OSError:
            raise SecretRefused("secret_path_unreadable") from None
    finally:
        os.close(fd)
    try:
        info = os.fstat(leaf)
        if not (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) & 0o077 == 0 and info.st_nlink == 1
                and info.st_size <= limit):
            raise SecretRefused("secret_file_not_private")
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(leaf, limit + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > limit:
            raise SecretRefused("secret_file_not_private")
        return b"".join(chunks)
    except OSError:
        raise SecretRefused("secret_path_unreadable") from None
    finally:
        os.close(leaf)


def _one_line(data):
    return data[:-1] if data.endswith(b"\n") else data


def child_secrets(url_path, key_path, port):
    """The two values, read and validated by the served child after the
    barrier. Returns {"DATABASE_URL", "SECRET_KEY"}; raises SecretRefused."""
    url = _one_line(read_private(url_path, MAX_URL))
    try:
        url = url.decode("ascii")
    except UnicodeError:
        raise SecretRefused("database_url_invalid") from None
    match = _URL.fullmatch(url)
    if not match or int(match[3]) != port:
        raise SecretRefused("database_url_invalid")
    key = _one_line(read_private(key_path, MAX_KEY + 1))
    try:
        text = key.decode("utf-8")
    except UnicodeError:
        raise SecretRefused("secret_key_invalid") from None
    if not MIN_KEY <= len(key) <= MAX_KEY or not text.isprintable():
        raise SecretRefused("secret_key_invalid")
    return {"DATABASE_URL": url, "SECRET_KEY": text}


def await_release(fd):
    """The child's side of the barrier: True only for the release byte. EOF
    (the parent closed the pipe unwritten) or anything else is False."""
    try:
        return os.read(fd, 1) == RELEASE
    except OSError:
        return False


def release(fd, receipt_ok):
    """The parent's side: the byte only after the launch receipt was written
    and synced; otherwise close unwritten. Returns whether it was sent."""
    sent = False
    try:
        if receipt_ok:
            sent = os.write(fd, RELEASE) == 1
    except OSError:
        sent = False
    finally:
        os.close(fd)
    return sent


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
    present = [name for name in SECRET_VARIABLES if name in os.environ]
    if present:
        # Paths only: this process never holds a value (P2c).
        fail(EXIT_SETUP, "secret value variables in the environment: %s"
             % ", ".join(present))

    env = dict(os.environ)
    env.pop(OBSERVER_SOCKET, None)
    env.pop(OBSERVER_PARENT, None)
    if opts["--observer-socket"] is not None:
        env[OBSERVER_SOCKET] = opts["--observer-socket"]
        env[OBSERVER_PARENT] = str(me)

    try:
        gate_read, gate_write = os.pipe()
    except OSError as error:
        fail(EXIT_SETUP, "barrier pipe: %s" % error)
    child = os.fork()
    if child == 0:
        try:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            os.close(gate_write)
            released = await_release(gate_read)
            os.close(gate_read)
            if not released:
                os.write(2, b"reference-supervisor: child: no release, nothing read\n")
                os._exit(EXIT_CHILD_BARRIER)
            try:
                env.update(child_secrets(opts["--database-url-file"],
                                         opts["--secret-key-file"], opts["--database-port"]))
            except SecretRefused as refused:
                os.write(2, b"reference-supervisor: child: secrets refused: %s\n"
                         % str(refused).encode("ascii", "replace"))
                os._exit(EXIT_CHILD_SECRET)
            except BaseException:
                os.write(2, b"reference-supervisor: child: secrets refused: "
                            b"secret_read_failed\n")
                os._exit(EXIT_CHILD_SECRET)
            os.execve(command[0], command, env)
        finally:
            os._exit(127)
    os.close(gate_read)

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
        # The launcher's install manifest (every installed observer file's
        # path and digest, and the activator's), bound by its digest
        # (contract section 6). P records it; the launcher cross-checks it.
        "observer": None if opts["--observer-socket"] is None else {
            "socket": opts["--observer-socket"],
            "manifest": opts["--observer-manifest"],
            "manifest_sha256": opts["--observer-manifest-sha256"]},
        "launched_utc_ns": time.time_ns(),
        "monotonic_ns": time.monotonic_ns(),
    }
    receipt_ok = True
    try:
        write_exclusive(opts["--receipt"], receipt)
    except OSError as error:
        sys.stderr.write("reference-supervisor: launch receipt: %s\n" % error)
        receipt_ok = False
    # The barrier: C may read its secrets only now that R exists (P2c).
    release(gate_write, receipt_ok)

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
