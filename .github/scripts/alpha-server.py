#!/usr/bin/env python3
"""Start and stop the alrust server for the alpha-smoke CI job.

  alpha-server.py start <ready-url> -- <command> [args...]
  alpha-server.py stop

start acquires a fresh private directory (mkdtemp) and a random token and
exports ALRUST_ROOT and ALRUST_TOKEN to later steps of this job only
(GITHUB_ENV). It writes a "launching" marker, launches the command with its
log in that directory, and records the child's pid and kernel start time
while the child is still its own unreaped child, so the record cannot name
another process. If the record cannot be written, or the server is not ready
within ALRUST_READY_TIMEOUT seconds (default 60), start stops its own child
through its process handle and fails.

stop acts only on that receipt. It opens a pidfd for the recorded pid, then
checks the start time, and signals only through the pidfd, so a pid reused at
any point is never signalled. The directory is removed only on positive
evidence that the child is gone (never launched, pid free or held by another
process, or the pidfd reports exit). A missing or malformed record, or a
process that cannot be inspected or will not stop, keeps the directory and
fails as UNPROVEN.

Test hooks: ALRUST_PROC replaces /proc for start-time reads, and
ALRUST_TEST_BEFORE_SIGNAL names a command run with the pid after the identity
check and before the first signal.
"""
import os
import secrets
import select
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

PROC = os.environ.get("ALRUST_PROC", "/proc")


def start_time(pid):
    """The kernel start time of pid, or None if no such process. Any other
    failure (unreadable, malformed) raises."""
    try:
        with open(f"{PROC}/{pid}/stat") as f:
            stat = f.read()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return int(stat[stat.rindex(")") + 2:].split()[19])


def exited(pidfd, timeout):
    """True once the process behind pidfd has exited."""
    p = select.poll()
    p.register(pidfd, select.POLLIN)
    return bool(p.poll(timeout * 1000))


def stop_own(child):
    """Stop a child we launched and have not reaped (its pid cannot be reused)."""
    for send, wait in ((child.terminate, 10), (child.kill, 5)):
        if child.poll() is not None:
            return True
        send()
        try:
            child.wait(wait)
            return True
        except subprocess.TimeoutExpired:
            pass
    return child.poll() is not None


def start(args):
    if len(args) < 3 or args[1] != "--":
        print("usage: alpha-server.py start <ready-url> -- <command>...", file=sys.stderr)
        return 2
    url, cmd = args[0], args[2:]
    root = tempfile.mkdtemp(prefix="alrust.", dir=os.environ["RUNNER_TEMP"])
    token = secrets.token_hex(16)
    with open(os.open(f"{root}/owner", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        f.write(token + "\n")
    if os.environ.get("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"], "a") as f:
            f.write(f"ALRUST_ROOT={root}\nALRUST_TOKEN={token}\n")
    print(f"ALRUST_ROOT={root}\nALRUST_TOKEN={token}", flush=True)

    open(f"{root}/launching", "x").close()
    log = open(f"{root}/alrust.log", "wb")
    try:
        child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        os.unlink(f"{root}/launching")
        print(f"alrust not started: {e}", file=sys.stderr)
        return 1
    try:
        st = start_time(child.pid)
        if st is None:
            raise RuntimeError(f"no /proc entry for own child {child.pid}")
        with open(f"{root}/pid.tmp", "w") as f:
            f.write(f"{child.pid} {st}\n")
            f.flush()
            os.fsync(f.fileno())
        os.rename(f"{root}/pid.tmp", f"{root}/pid")
    except Exception as e:
        ok = stop_own(child)
        print(f"alrust start FAILED: pid record not written ({e}); "
              f"own child {'stopped' if ok else 'STILL RUNNING'}", file=sys.stderr)
        return 1

    for _ in range(int(os.environ.get("ALRUST_READY_TIMEOUT", "60"))):
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    print(f"alrust ready (pid {child.pid})")
                    return 0
        except OSError:
            pass
        if child.poll() is not None:
            break
        time.sleep(1)
    ok = stop_own(child)
    print(f"alrust not ready; own child {'stopped' if ok else 'STILL RUNNING'}", file=sys.stderr)
    with open(f"{root}/alrust.log", errors="replace") as f:
        sys.stderr.write(f.read())
    return 1


def remove(root, why):
    subprocess.run(["rm", "-rf", "--", root], check=False)
    if os.path.lexists(root):
        print(f"alrust cleanup FAILED: {root} remains", file=sys.stderr)
        return 1
    print(f"alrust cleanup COMPLETE: {why}; {root} removed")
    return 0


def unproven(root, why):
    print(f"alrust cleanup UNPROVEN: {why}; {root} kept", file=sys.stderr)
    return 1


def stop():
    root = os.environ.get("ALRUST_ROOT", "")
    token = os.environ.get("ALRUST_TOKEN", "")
    if not root and not token:
        print("alrust cleanup: this job started nothing; nothing signalled")
        return 0
    try:
        with open(f"{root}/owner") as f:
            owned = bool(root and token) and f.read().strip() == token
    except OSError:
        owned = False
    if not owned:
        print("alrust cleanup FAILED: start receipt does not match; nothing signalled", file=sys.stderr)
        return 1

    if not os.path.lexists(f"{root}/launching"):
        return remove(root, "nothing was launched")
    try:
        with open(f"{root}/pid") as f:
            pid_s, st_s = f.read().split()
        pid, recorded = int(pid_s), int(st_s)
        if pid <= 0:
            raise ValueError(f"pid {pid}")
    except Exception as e:
        return unproven(root, f"a child was launched but its pid record is missing or malformed ({e!r})")

    try:
        pidfd = os.pidfd_open(pid)
    except ProcessLookupError:
        return remove(root, f"recorded child {pid} has exited; not signalled")
    except OSError as e:
        return unproven(root, f"cannot open pid {pid} ({e!r})")
    try:
        now = start_time(pid)
    except Exception as e:
        return unproven(root, f"cannot inspect pid {pid} ({e!r})")
    if now != recorded:
        # The pid is free, or held by a process that started later: the
        # recorded child cannot be running.
        return remove(root, f"recorded child {pid} has exited (pid now "
                      f"{'free' if now is None else 'another process'}); not signalled")

    # pidfd is now bound to the recorded child; signals go only through it.
    if os.environ.get("ALRUST_TEST_BEFORE_SIGNAL"):
        subprocess.run([os.environ["ALRUST_TEST_BEFORE_SIGNAL"], str(pid)], check=False)
    for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        if exited(pidfd, 0):
            break
        try:
            signal.pidfd_send_signal(pidfd, sig)
        except ProcessLookupError:
            break
        except OSError as e:
            return unproven(root, f"cannot signal recorded child {pid} ({e!r})")
        if exited(pidfd, wait):
            print(f"alrust cleanup: recorded child {pid} stopped by {sig.name}")
            break
    if not exited(pidfd, 0):
        return unproven(root, f"recorded child {pid} still running")
    return remove(root, f"recorded child {pid} has exited")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["start"]:
        sys.exit(start(a[1:]))
    if a == ["stop"]:
        sys.exit(stop())
    print("usage: alpha-server.py start <ready-url> -- <command>... | stop", file=sys.stderr)
    sys.exit(2)
