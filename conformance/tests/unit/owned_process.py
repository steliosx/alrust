"""Exception-safe custody of a test's own Popen children (P2B2-WINDOW-R1).

Every helper acts only through the owned Popen handle of a child the test
started and has not reaped, so no signal can reach a reused PID. Each step
is attempted even if an earlier one failed; the failures are kept and
raised afterwards, without hiding a primary failure. A descendant that
outlives these steps is left for the window's read-only check (HOLD); it
is never signalled by bare PID.
"""
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
