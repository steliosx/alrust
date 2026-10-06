"""Pure controls for the launcher tests' process cleanup
(LAUNCHER-TEST-CLEANUP-N1).

pidfd_open, /proc reads, pidfd_send_signal, poll, close and os.kill are
replaced: no process is started, inspected for real or signalled. The
launcher test module defines only constants and functions at import."""
import inspect
import os
import signal

import pytest

import test_reference_launcher as trl


@pytest.fixture
def calls(monkeypatch):
    log = []
    monkeypatch.setattr(os, "pidfd_open", lambda pid: log.append(("open", pid)) or 55)
    monkeypatch.setattr(os, "close", lambda fd: log.append(("close", fd)))
    monkeypatch.setattr(os, "kill", lambda *a: log.append(("bare_kill",) + a))
    monkeypatch.setattr(signal, "pidfd_send_signal",
                        lambda fd, sig: log.append(("pidfd_signal", fd, sig)))

    class Poll:
        def register(self, fd, mask):
            log.append(("register", fd))

        def poll(self, ms):
            return [(55, 1)]
    monkeypatch.setattr(trl.select, "poll", Poll)
    return log


def test_matching_generation_is_held_and_killed_through_pidfd(monkeypatch, calls):
    monkeypatch.setattr(trl, "read_generation", lambda pid: (10, 123))
    fd = trl.hold_generation(777, 10, 123)
    trl.kill_held(fd)
    assert calls == [("open", 777), ("pidfd_signal", 55, signal.SIGKILL),
                     ("register", 55), ("close", 55)]


@pytest.mark.parametrize("current", [(10, 456), (1, 123), None])
def test_reused_reparented_or_vanished_generation_signals_nothing(monkeypatch, calls,
                                                                  current):
    monkeypatch.setattr(trl, "read_generation", lambda pid: current)
    with pytest.raises(pytest.fail.Exception, match="HOLD, nothing signalled"):
        trl.hold_generation(777, 10, 123)
    assert calls == [("open", 777), ("close", 55)]


def test_unreadable_generation_signals_nothing(monkeypatch, calls):
    def denied(pid):
        raise PermissionError(13, "synthetic")
    monkeypatch.setattr(trl, "read_generation", denied)
    with pytest.raises(PermissionError):
        trl.hold_generation(777, 10, 123)
    assert calls == [("open", 777), ("close", 55)]


def test_gone_before_hold_signals_nothing(monkeypatch, calls):
    def gone(pid):
        raise ProcessLookupError(3, "synthetic")
    monkeypatch.setattr(os, "pidfd_open", gone)
    with pytest.raises(pytest.fail.Exception, match="HOLD, nothing signalled"):
        trl.hold_generation(777, 10, 123)
    assert calls == []


def test_already_exited_generation_is_not_resignalled(monkeypatch, calls):
    def esrch(fd, sig):
        raise ProcessLookupError(3, "synthetic")
    monkeypatch.setattr(signal, "pidfd_send_signal", esrch)
    trl.kill_held(55)
    assert calls == [("register", 55), ("close", 55)]


def test_no_exit_fails_and_still_closes(monkeypatch, calls):
    class Silent:
        def register(self, fd, mask):
            pass

        def poll(self, ms):
            return []
    monkeypatch.setattr(trl.select, "poll", Silent)
    with pytest.raises(pytest.fail.Exception, match="did not exit"):
        trl.kill_held(55)
    assert calls[-1] == ("close", 55) and not any(c[0] == "bare_kill" for c in calls)


def test_read_generation_absent_is_none(tmp_path, monkeypatch):
    real_open = open

    def fake_open(path, *a, **k):
        if path == "/proc/777/stat":
            raise FileNotFoundError(2, "synthetic")
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", fake_open)
    assert trl.read_generation(777) is None


class FakeProc:
    def __init__(self, log, name):
        self.log, self.name = log, name

    def kill(self):
        self.log.append(("kill", self.name))

    def wait(self, timeout=None):
        self.log.append(("wait", self.name))


def test_cleanup_fixture_kills_and_reaps_its_own_popen_children():
    log = []
    assert hasattr(trl.cleanup, "__wrapped__"), "fixture function not reachable"
    gen = trl.cleanup.__wrapped__()
    procs = next(gen)
    procs.extend([FakeProc(log, "a"), FakeProc(log, "b")])
    with pytest.raises(StopIteration):
        next(gen)
    assert log == [("kill", "a"), ("wait", "a"), ("kill", "b"), ("wait", "b")]


def test_no_bare_pid_signal_remains_in_launcher_tests():
    source = inspect.getsource(trl)
    assert "os.kill(" not in source
    fixture = inspect.getsource(trl.owned_pair)
    assert "hold_generation(child, p.pid" in fixture
    assert fixture.index("kill_held(held)") < fixture.index("proc.kill()")
