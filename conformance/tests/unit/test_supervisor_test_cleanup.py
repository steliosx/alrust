"""Pure controls for the supervisor tests' orphan cleanup (P1-TEST-CLEANUP-R2).

pidfd_open, read_stat, pidfd_send_signal, poll and close are replaced: no
process is started, inspected for real or signalled. Importing the
supervisor test module only defines its helpers."""
import os
import signal

import pytest

import test_entrypoint_supervisor as tes

LAUNCH = {"child": {"pid": 777, "start_ticks": 123}}


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
    monkeypatch.setattr(tes.select, "poll", Poll)
    return log


def test_matching_generation_is_held_and_killed_through_pidfd(monkeypatch, calls):
    monkeypatch.setattr(tes.sup, "read_stat", lambda pid: (1, 123))
    fd = tes.hold_child(LAUNCH)
    tes.kill_held(fd)
    assert calls == [("open", 777), ("pidfd_signal", 55, signal.SIGKILL),
                     ("register", 55), ("close", 55)]


@pytest.mark.parametrize("stat", [(1, 456), None])
def test_reused_or_vanished_generation_signals_nothing(monkeypatch, calls, stat):
    monkeypatch.setattr(tes.sup, "read_stat", lambda pid: stat)
    with pytest.raises(pytest.fail.Exception, match="HOLD, nothing signalled"):
        tes.hold_child(LAUNCH)
    assert calls == [("open", 777), ("close", 55)]


def test_unreadable_generation_signals_nothing(monkeypatch, calls):
    def denied(pid):
        raise PermissionError(13, "synthetic")
    monkeypatch.setattr(tes.sup, "read_stat", denied)
    with pytest.raises(PermissionError):
        tes.hold_child(LAUNCH)
    assert calls == [("open", 777), ("close", 55)]


@pytest.mark.parametrize("launch", [{"child": {"pid": 777, "start_ticks": None}},
                                    {"child": {"pid": 777}}])
def test_unknown_generation_signals_nothing(calls, launch):
    with pytest.raises(pytest.fail.Exception, match="HOLD, nothing signalled"):
        tes.hold_child(launch)
    assert calls == []


def test_child_gone_before_hold_signals_nothing(monkeypatch, calls):
    def gone(pid):
        raise ProcessLookupError(3, "synthetic")
    monkeypatch.setattr(os, "pidfd_open", gone)
    with pytest.raises(pytest.fail.Exception, match="HOLD, nothing signalled"):
        tes.hold_child(LAUNCH)
    assert calls == []


def test_held_generation_already_exited_is_not_resignalled(monkeypatch, calls):
    def esrch(fd, sig):
        raise ProcessLookupError(3, "synthetic")
    monkeypatch.setattr(signal, "pidfd_send_signal", esrch)
    tes.kill_held(55)
    assert calls == [("register", 55), ("close", 55)]


def test_no_exit_fails_and_still_closes(monkeypatch, calls):
    class Silent:
        def register(self, fd, mask):
            pass

        def poll(self, ms):
            return []
    monkeypatch.setattr(tes.select, "poll", Silent)
    with pytest.raises(pytest.fail.Exception, match="did not exit"):
        tes.kill_held(55)
    assert calls[-1] == ("close", 55) and not any(c[0] == "bare_kill" for c in calls)


def test_outside_kill_test_uses_no_bare_pid_signal():
    import inspect
    source = inspect.getsource(tes.test_outside_kill_leaves_no_terminal_receipt)
    assert "os.kill" not in source and "hold_child(launch)" in source
    assert source.index("hold_child(launch)") < source.index("proc.kill()")
