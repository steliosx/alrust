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
    def __init__(self, log, name, pid, fail=()):
        self.log, self.name, self.pid, self.fail = log, name, pid, fail

    def kill(self):
        self.log.append(("kill", self.name))
        if "kill" in self.fail:
            raise OSError("synthetic kill failure")

    def wait(self, timeout=None):
        self.log.append(("wait", self.name))
        if "wait" in self.fail:
            raise OSError("synthetic wait failure")


def fixture_function(fixture):
    assert hasattr(fixture, "__wrapped__"), "fixture function not reachable"
    return fixture.__wrapped__


def test_run_all_attempts_every_step_and_raises():
    log = []

    def bad(name):
        def step():
            log.append(name)
            raise OSError(name)
        return step
    with pytest.raises(OSError, match="one"):
        trl.run_all([bad("one"), lambda: log.append("two")])
    assert log == ["one", "two"]
    log.clear()
    with pytest.raises(BaseExceptionGroup) as group:
        trl.run_all([bad("a"), lambda: log.append("b"), bad("c")])
    assert log == ["a", "b", "c"] and len(group.value.exceptions) == 2
    trl.run_all([])


def test_cleanup_fixture_kills_and_reaps_each_popen():
    log = []
    gen = fixture_function(trl.cleanup)()
    next(gen).extend([FakeProc(log, "a", 1), FakeProc(log, "b", 2)])
    with pytest.raises(StopIteration):
        next(gen)
    assert log == [("kill", "a"), ("wait", "a"), ("kill", "b"), ("wait", "b")]


@pytest.mark.parametrize("fail", [("kill",), ("wait",)])
def test_cleanup_fixture_failure_still_cleans_later_children(fail):
    log = []
    gen = fixture_function(trl.cleanup)()
    next(gen).extend([FakeProc(log, "a", 1, fail), FakeProc(log, "b", 2)])
    with pytest.raises(OSError, match="synthetic"):
        next(gen)
    assert log == [("kill", "a"), ("wait", "a"), ("kill", "b"), ("wait", "b")]


def test_cleanup_fixture_runs_when_the_test_fails():
    log = []
    gen = fixture_function(trl.cleanup)()
    next(gen).append(FakeProc(log, "a", 1))
    with pytest.raises(RuntimeError):
        gen.throw(RuntimeError("test body failed"))
    assert log == [("kill", "a"), ("wait", "a")]


@pytest.fixture
def pair(monkeypatch, calls):
    """owned_pair with Popen, /proc listing, generation reads, the pidfd hold
    and the held kill replaced. calls records pidfd-level actions."""
    spawned, plan = [], {"fail_spawn": None, "proc_fail": {}, "held_fail": False}
    names = iter([("p", 100), ("unrelated", 200)])

    def popen(argv, *a, **k):
        name, pid = next(names)
        if plan["fail_spawn"] == name:
            raise OSError("synthetic spawn failure")
        proc = FakeProc(calls, name, pid, plan["proc_fail"].get(name, ()))
        spawned.append(proc)
        return proc
    monkeypatch.setattr(trl.subprocess, "Popen", popen)
    monkeypatch.setattr(trl.os, "listdir", lambda path: ["4242"])
    monkeypatch.setattr(trl.time, "sleep", lambda s: None)
    monkeypatch.setattr(trl, "read_generation",
                        lambda pid: (100, 99) if pid == 4242 else None)
    monkeypatch.setattr(trl, "hold_generation",
                        lambda pid, ppid, ticks: calls.append(("hold", pid, ppid, ticks)) or 55)

    def kill_held(fd):
        calls.append(("kill_held", fd))
        if plan["held_fail"]:
            raise pytest.fail.Exception("held child did not exit")
    monkeypatch.setattr(trl, "kill_held", kill_held)
    return plan, spawned


FULL_TEARDOWN = [("kill_held", 55), ("kill", "p"), ("wait", "p"),
                 ("kill", "unrelated"), ("wait", "unrelated")]


def test_owned_pair_positive_flow(pair, calls):
    gen = fixture_function(trl.owned_pair)()
    assert next(gen) == (100, 4242, pair[1][1])
    assert calls == [("hold", 4242, 100, 99)]
    with pytest.raises(StopIteration):
        next(gen)
    assert calls[1:] == FULL_TEARDOWN and not any(c[0] == "bare_kill" for c in calls)


def test_owned_pair_second_spawn_failure_cleans_the_first(pair, calls):
    pair[0]["fail_spawn"] = "unrelated"
    with pytest.raises(OSError, match="synthetic spawn"):
        next(fixture_function(trl.owned_pair)())
    assert calls == [("kill", "p"), ("wait", "p")]


def test_owned_pair_held_cleanup_failure_still_reaps_both(pair, calls):
    pair[0]["held_fail"] = True
    gen = fixture_function(trl.owned_pair)()
    next(gen)
    with pytest.raises(pytest.fail.Exception, match="did not exit"):
        next(gen)
    assert calls[1:] == FULL_TEARDOWN


@pytest.mark.parametrize("fail", [("kill",), ("wait",)])
def test_owned_pair_first_popen_failure_still_cleans_the_rest(pair, calls, fail):
    pair[0]["proc_fail"]["p"] = fail
    gen = fixture_function(trl.owned_pair)()
    next(gen)
    with pytest.raises(OSError, match="synthetic"):
        next(gen)
    assert calls[1:] == FULL_TEARDOWN


def test_owned_pair_several_failures_are_all_surfaced(pair, calls):
    pair[0]["held_fail"] = True
    pair[0]["proc_fail"]["unrelated"] = ("wait",)
    gen = fixture_function(trl.owned_pair)()
    next(gen)
    with pytest.raises(BaseExceptionGroup) as group:
        next(gen)
    assert len(group.value.exceptions) == 2 and calls[1:] == FULL_TEARDOWN


def test_owned_pair_cleans_up_when_the_test_fails(pair, calls):
    gen = fixture_function(trl.owned_pair)()
    next(gen)
    with pytest.raises(RuntimeError):
        gen.throw(RuntimeError("test body failed"))
    assert calls[1:] == FULL_TEARDOWN


def test_no_bare_pid_signal_remains_in_launcher_tests():
    source = inspect.getsource(trl)
    assert "os.kill(" not in source
    fixture = inspect.getsource(trl.owned_pair)
    assert "hold_generation(child, p.pid" in fixture  # teardown order: FULL_TEARDOWN
