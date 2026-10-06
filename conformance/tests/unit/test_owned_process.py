"""Pure controls for owned-process custody (P2B2-WINDOW-R1): the shared
helper, the entrypoint-supervisor tests' start/finish/custody, and the
finalizer tests' ended_generation. Popen, open and the handles' signals,
kills and waits are replaced: no process is started or signalled."""
import signal
import subprocess

import pytest

import owned_process as op
import test_entrypoint_supervisor as tes
import test_finalizer_orchestration as tfo


class Fake:
    """An owned Popen stand-in. WAITS lists what successive wait() calls
    do: an int return code, or an exception to raise."""

    def __init__(self, log, waits=(0,), *, running=True, term_error=None, kill_error=None,
                 name="p", pid=4242):
        self.log, self.waits, self.name, self.pid = log, list(waits), name, pid
        self.returncode = None if running else 0
        self.term_error, self.kill_error = term_error, kill_error

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.log.append((self.name, "wait", timeout))
        step = self.waits.pop(0)
        if isinstance(step, BaseException):
            raise step
        self.returncode = step
        return step

    def send_signal(self, sig):
        self.log.append((self.name, "signal", sig))
        if self.term_error:
            raise self.term_error

    def kill(self):
        self.log.append((self.name, "kill"))
        if self.kill_error:
            raise self.kill_error


TIMEOUT = subprocess.TimeoutExpired("x", 1)


# --- the helper --------------------------------------------------------------

def test_exited_child_is_left_alone():
    log = []
    op.stop_owned(Fake(log, running=False), 21, 5)
    op.kill_and_reap(Fake(log, running=False), 10)
    assert log == []


def test_stop_owned_term_then_exit():
    log = []
    op.stop_owned(Fake(log, [-15]), 21, 5)
    assert log == [("p", "signal", signal.SIGTERM), ("p", "wait", 21)]


def test_stop_owned_stall_then_owned_kill_and_reap():
    log = []
    op.stop_owned(Fake(log, [TIMEOUT, -9]), 21, 5)
    assert log == [("p", "signal", signal.SIGTERM), ("p", "wait", 21), ("p", "kill"),
                   ("p", "wait", 5)]


def test_stop_owned_attempts_every_step_after_failures():
    log = []
    proc = Fake(log, [KeyboardInterrupt(), 0], term_error=OSError("term"),
                kill_error=OSError("kill"))
    with pytest.raises(BaseExceptionGroup) as group:
        op.stop_owned(proc, 21, 5)
    assert [type(e) for e in group.value.exceptions] == [OSError, KeyboardInterrupt, OSError]
    assert log == [("p", "signal", signal.SIGTERM), ("p", "wait", 21), ("p", "kill"),
                   ("p", "wait", 5)]


def test_kill_and_reap_still_reaps_after_a_failed_kill():
    log = []
    with pytest.raises(OSError, match="kill"):
        op.kill_and_reap(Fake(log, [-9], kill_error=OSError("kill")), 10)
    assert log == [("p", "kill"), ("p", "wait", 10)]


def test_kill_and_reap_bounded_wait_timeout_is_raised():
    log = []
    with pytest.raises(subprocess.TimeoutExpired):
        op.kill_and_reap(Fake(log, [TIMEOUT]), 10)


def test_run_steps_keeps_primary_and_attempts_all():
    log = []

    def bad(name):
        def step():
            log.append(name)
            raise OSError(name)
        return step
    with pytest.raises(BaseExceptionGroup) as group:
        op.run_steps([bad("a"), lambda: log.append("b"), bad("c")], RuntimeError("primary"))
    assert log == ["a", "b", "c"]
    assert [type(e) for e in group.value.exceptions] == [RuntimeError, OSError, OSError]
    with pytest.raises(OSError, match="only"):
        op.run_steps([bad("only")])
    op.run_steps([lambda: None], RuntimeError("primary"))   # nothing failed: no raise


# --- the entrypoint supervisor tests' custody ----------------------------------

def test_start_registers_before_the_stderr_file_closes(tmp_path, monkeypatch):
    log = []
    created = []

    def popen(args, **kw):
        proc = Fake(log)
        created.append(proc)
        return proc

    class ClosingFails:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            log.append(("file", "close"))
            raise OSError("synthetic close failure")
    monkeypatch.setattr(tes.subprocess, "Popen", popen)
    monkeypatch.setattr(tes, "open", ClosingFails, raising=False)
    del tes._OWNED[:]
    with pytest.raises(OSError, match="synthetic close failure"):
        tes.start(tmp_path, "complete")
    assert tes._OWNED == created and len(created) == 1
    del tes._OWNED[:]


def test_custody_teardown_stops_every_owned_supervisor_independently():
    log = []
    first = Fake(log, [KeyboardInterrupt(), 0], name="a", term_error=OSError("term"))
    second = Fake(log, [-15], name="b")
    done = Fake(log, running=False, name="c")
    gen = tes.custody.__wrapped__()
    next(gen)
    tes._OWNED.extend([first, second, done])
    with pytest.raises(BaseExceptionGroup):
        next(gen)
    assert ("b", "signal", signal.SIGTERM) in log and ("b", "wait", 21.0) in log
    assert not any(entry[0] == "c" for entry in log) and tes._OWNED == []


def test_finish_waits_deadline_plus_teardown_plus_margin(tmp_path):
    log = []
    proc = Fake(log, [0])
    proc.alrust_deadline_s = 16.5
    (tmp_path / "t.json").write_text("{}")
    tes.finish(proc, tmp_path / "t.json")
    assert log == [("p", "wait", 16.5 + 16.0 + 5.0)]
    assert tes.TEARDOWN_S * 1e9 == tes.sup.TEARDOWN_BUDGET_NS


def test_stderr_is_read_bounded(tmp_path):
    path = tmp_path / "e.txt"
    path.write_bytes(b"x" * (tes.MAX_STDERR + 10))

    class P:
        alrust_stderr = path
    assert len(tes.stderr_of(P())) == tes.MAX_STDERR


# --- the finalizer tests' ended_generation -------------------------------------

def test_ended_generation_reaps_even_if_reading_proc_fails(monkeypatch):
    log = []
    child = Fake(log, [-9])
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: child)
    real_open = open

    def failing_open(path, *a, **k):
        if str(path) == "/proc/%d/stat" % child.pid:
            raise PermissionError(13, "synthetic")
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", failing_open)
    with pytest.raises(PermissionError):
        tfo.ended_generation()
    assert log == [("p", "kill"), ("p", "wait", tfo.REAP_WAIT_S)]


def test_ended_generation_keeps_the_primary_when_cleanup_fails(monkeypatch):
    log = []
    child = Fake(log, [TIMEOUT], kill_error=OSError("kill"))
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: child)
    real_open = open

    def failing_open(path, *a, **k):
        if str(path) == "/proc/%d/stat" % child.pid:
            raise PermissionError(13, "synthetic")
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", failing_open)
    with pytest.raises(BaseExceptionGroup) as group:
        tfo.ended_generation()
    assert type(group.value.exceptions[0]) is PermissionError
    assert log == [("p", "kill"), ("p", "wait", tfo.REAP_WAIT_S)]
