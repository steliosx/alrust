"""Exception-safe custody of a test's own Popen children (P2B2-WINDOW-R1).

Every helper acts only through the owned Popen handle of a child the test
started and has not reaped, so no signal can reach a reused PID. Each step
is attempted even if an earlier one failed; the failures are kept and
raised afterwards, without hiding a primary failure. A descendant that
outlives these steps is left for the window's read-only check (HOLD); it
is never signalled by bare PID.
"""
import os
import select
import signal
import subprocess


def _raise(primary, errors, label):
    if not errors:
        return
    if primary is not None:
        raise BaseExceptionGroup(label, [primary] + errors) from None
    if len(errors) == 1:
        raise errors[0]
    raise BaseExceptionGroup(label, errors)


def run_steps(steps, primary=None, label="cleanup failed"):
    """Attempt every step; raise what failed (with PRIMARY, if any)."""
    errors = []
    for step in steps:
        try:
            step()
        except BaseException as error:
            errors.append(error)
    _raise(primary, errors, label)


def stop_owned(proc, term_wait_s, kill_wait_s):
    """A still-running owned supervisor: TERM (its own teardown), a bounded
    wait, then, only if it has not exited, KILL through the handle and a
    bounded reap. A TimeoutExpired from the first wait is the expected
    route to the fallback, not a failure."""
    if proc.poll() is not None:
        return
    errors = []
    try:
        proc.send_signal(signal.SIGTERM)
    except BaseException as error:
        errors.append(error)
    try:
        proc.wait(timeout=term_wait_s)
        exited = True
    except subprocess.TimeoutExpired:
        exited = False
    except BaseException as error:
        errors.append(error)
        exited = False
    if not exited:
        for step in (proc.kill, lambda: proc.wait(timeout=kill_wait_s)):
            try:
                step()
            except BaseException as error:
                errors.append(error)
    _raise(None, errors, "stop_owned: %d steps failed" % len(errors))


def kill_and_reap(proc, wait_s):
    """A still-running owned leaf child (no teardown of its own): KILL
    through the handle and a bounded reap, each attempted."""
    if proc.poll() is not None:
        return
    errors = []
    for step in (proc.kill, lambda: proc.wait(timeout=wait_s)):
        try:
            step()
        except BaseException as error:
            errors.append(error)
    _raise(None, errors, "kill_and_reap: %d steps failed" % len(errors))


class HoldRefused(Exception):
    """A generation that cannot be proven: HOLD, nothing signalled."""


def read_generation(pid):
    """(ppid, start_ticks) of PID, or None if it is gone."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            rest = f.read().rsplit(b")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return int(rest[1]), int(rest[19])


def hold_generation(pid, ppid, start_ticks):
    """A pidfd for exactly the generation (PID, ppid, start_ticks). The
    descriptor is opened first and the generation read after it: that
    generation existed before the open, so a match proves the descriptor
    names it. Anything else closes the descriptor and refuses (HOLD)."""
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        raise HoldRefused("pid %d gone before it was held" % pid) from None
    try:
        current = read_generation(pid)
    except BaseException:
        os.close(fd)
        raise
    if current != (ppid, start_ticks):
        os.close(fd)
        raise HoldRefused("pid %d is not the expected generation" % pid)
    return fd


class Held:
    """A held generation for a cleanup list."""

    def __init__(self, fd):
        self.fd = fd


def kill_held(fd, wait_s):
    """SIGKILL through the held pidfd only, prove exit by polling it (it
    becomes readable), then close it; each step attempted."""
    errors = []
    try:
        signal.pidfd_send_signal(fd, signal.SIGKILL)
    except ProcessLookupError:
        pass                      # that exact generation already exited
    except BaseException as error:
        errors.append(error)
    try:
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        if not poller.poll(int(wait_s * 1000)):
            errors.append(TimeoutError("held process did not exit: HOLD"))
    except BaseException as error:
        errors.append(error)
    try:
        os.close(fd)
    except BaseException as error:
        errors.append(error)
    _raise(None, errors, "kill_held: %d steps failed" % len(errors))


def release_all(items, popen_wait_s, held_wait_s):
    """Clean every item of a cleanup list independently: an owned Popen by
    kill_and_reap, a Held generation by kill_held."""
    run_steps([(lambda i=i: kill_held(i.fd, held_wait_s)) if isinstance(i, Held)
               else (lambda i=i: kill_and_reap(i, popen_wait_s)) for i in items],
              label="cleanup list failed")
