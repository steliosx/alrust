"""Pure controls for stop-supervisor.py (R-EP plan P4).

The ended path uses the real pidfd_open on an absent PID (ProcessLookupError,
no process involved). Live paths replace pidfd_open, the generation read,
pidfd_send_signal and the wait with in-process fakes; os.kill is a spy that
fails the test. No process is created, signalled or waited for."""
import hashlib
import importlib.util
import json
import os
import signal
from pathlib import Path

import pytest

import exit_fixtures as xf
from alrust_conformance import entrypoint_exit as ee
from alrust_conformance import stage_journal as sj
from alrust_conformance import tool_exit as te

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "stop-supervisor.py"
ABSENT = 4194297                     # exit_fixtures.ENDED["entrypoint"]
TICKS = 111
ENV = {"GITHUB_REPOSITORY": "steliosx/alrust", "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "1",
       "GITHUB_JOB": "reference", "ImageOS": "ubuntu24", "ImageVersion": "SYNTHETIC"}


def _load():
    spec = importlib.util.spec_from_file_location("stop_supervisor", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sup = _load()


@pytest.fixture
def job(tmp_path, monkeypatch):
    if os.path.exists("/proc/%d" % ABSENT):
        pytest.skip("synthetic ended PID is in use on this host")
    monkeypatch.setattr(sj, "EXTERNAL_CONFIG", str(tmp_path / "no-alertad.conf"))
    root = tmp_path / "job"
    root.mkdir(mode=0o700)
    ref = "e" * 40
    sj.write_core_identity(str(root), profile="scaffold", event_head_sha="a" * 40,
                           checkout_sha="b" * 40, reference_sha=ref, reference_pin=ref,
                           lock_sha256={"build": "c" * 64}, lock_pins={"build": "c" * 64},
                           manifest_sha256="d" * 64, manifest_pin="d" * 64, env=ENV,
                           **xf.core_pins())
    return root


def receipt(root, tool=None, index=None, change=None, pid=ABSENT):
    """A launch receipt at the fixed path for the selected tree."""
    core_bytes = (root / sj.CORE_NAME).read_bytes()
    core = json.loads(core_bytes)
    if tool is None:
        launch, terminal = ee.receipt_paths(str(root))
        schema = ee.SCHEMA_LAUNCH
    else:
        launch, terminal = te.receipt_paths(str(root), tool, index)
        schema = te.SCHEMA_LAUNCH
    body = {"schema": schema,
            "supervisor": {"pid": pid, "pgid": pid, "start_ticks": TICKS, "uid": os.getuid(),
                           "subreaper": 1},
            "boot_id": core["boot_id"],
            "core_identity_sha256": hashlib.sha256(core_bytes).hexdigest(),
            "terminal_receipt": terminal}
    if tool is not None:
        body["tool"] = {"name": tool, "stage_index": index}
    if change:
        change(body)
    Path(launch).parent.mkdir(parents=True, exist_ok=True)
    Path(launch).write_text(json.dumps(body))
    return Path(launch), Path(terminal)


ENTRY = lambda root: ["--job-root", str(root), "--entrypoint"]          # noqa: E731
TOOL = lambda root: ["--job-root", str(root), "--tool", "database-create",  # noqa: E731
                     "--stage-index", "0"]


def run(argv, capsys):
    code = sup.main(argv)
    line = json.loads(capsys.readouterr().out)
    assert set(line) == {"outcome", "signalled", "reason"}      # never a verdict
    return code, line


@pytest.fixture
def no_kill(monkeypatch):
    monkeypatch.setattr(os, "kill", lambda *a: pytest.fail("os.kill used"))
    monkeypatch.setattr(os, "killpg", lambda *a: pytest.fail("os.killpg used"))


# --- the ended path, with the real syscall --------------------------------------------

def test_an_ended_supervisor_is_not_signalled(job, capsys, no_kill):
    receipt(job)
    assert run(ENTRY(job), capsys) == (0, {"outcome": "ENDED", "signalled": False,
                                           "reason": "not_running"})


def test_an_ended_tool_supervisor_is_not_signalled(job, capsys, no_kill):
    receipt(job, "database-create", 0, pid=xf.ENDED["database-create"])
    assert run(TOOL(job), capsys)[1]["outcome"] == "ENDED"


def test_it_is_idempotent(job, capsys, no_kill):
    receipt(job)
    assert run(ENTRY(job), capsys) == run(ENTRY(job), capsys)


# --- the live path, with fakes ----------------------------------------------------------

@pytest.fixture
def live(monkeypatch, no_kill):
    """A fake live supervisor of the receipt's generation."""
    state = {"generation": (TICKS, os.getuid()), "exits": True, "signals": [],
             "send_error": None, "open_error": None, "fds": []}
    read_end, write_end = os.pipe()

    def pidfd_open(pid, flags):
        assert flags == 0
        if state["open_error"]:
            raise state["open_error"]
        fd = os.dup(read_end)
        state["fds"].append(fd)
        return fd

    def send(fd, sig, info, flags):
        if state["send_error"]:
            raise state["send_error"]
        state["signals"].append((fd in state["fds"], sig, info, flags))

    def generation(pid):
        if isinstance(state["generation"], Exception):
            raise state["generation"]
        return state["generation"]
    monkeypatch.setattr(sup.os, "pidfd_open", pidfd_open)
    monkeypatch.setattr(sup.signal, "pidfd_send_signal", send)
    monkeypatch.setattr(sup, "read_generation", generation)
    # The design's bound (section 4: "poll up to 20 s"), as a literal.
    monkeypatch.setattr(sup, "wait_exit", lambda fd, seconds: (
        seconds == 20.0 and state["exits"]))
    yield state
    os.close(read_end)
    os.close(write_end)


def test_a_live_supervisor_gets_one_term_through_its_pidfd(job, capsys, live):
    receipt(job)
    assert run(ENTRY(job), capsys) == (0, {"outcome": "ENDED", "signalled": True,
                                           "reason": "exited"})
    assert live["signals"] == [(True, signal.SIGTERM, None, 0)]


def test_a_live_tool_supervisor_gets_one_term(job, capsys, live):
    receipt(job, "registry", 7)
    argv = ["--job-root", str(job), "--tool", "registry", "--stage-index", "7"]
    assert run(argv, capsys)[1] == {"outcome": "ENDED", "signalled": True, "reason": "exited"}
    assert live["signals"] == [(True, signal.SIGTERM, None, 0)]


def test_a_supervisor_that_ignores_term_is_unproven(job, capsys, live):
    receipt(job)
    live["exits"] = False
    assert run(ENTRY(job), capsys) == (2, {"outcome": "UNPROVEN", "signalled": True,
                                           "reason": "did_not_exit"})
    assert len(live["signals"]) == 1                    # TERM once, never escalated


def test_an_existing_terminal_receipt_means_no_signal(job, capsys, live):
    _, terminal = receipt(job)
    terminal.write_text("{}")
    assert run(ENTRY(job), capsys)[1] == {"outcome": "ENDED", "signalled": False,
                                          "reason": "exited"}
    assert live["signals"] == []


@pytest.mark.parametrize("generation", [None, (TICKS + 1, os.getuid())])
def test_a_reused_pid_is_never_signalled(job, capsys, live, generation):
    receipt(job)
    live["generation"] = generation
    assert run(ENTRY(job), capsys)[1] == {"outcome": "ENDED", "signalled": False,
                                          "reason": "not_running"}
    assert live["signals"] == []


def test_another_owner_is_never_signalled(job, capsys, live):
    receipt(job)
    live["generation"] = (TICKS, os.getuid() + 1)
    assert run(ENTRY(job), capsys) == (2, {"outcome": "UNPROVEN", "signalled": False,
                                           "reason": "uid_mismatch"})
    assert live["signals"] == []


@pytest.mark.parametrize("error,reason", [(OSError("x"), "generation_unreadable"),
                                          (ValueError("x"), "generation_unreadable")])
def test_an_unreadable_generation_is_never_signalled(job, capsys, live, error, reason):
    receipt(job)
    live["generation"] = error
    assert run(ENTRY(job), capsys)[1]["reason"] == reason and live["signals"] == []


def test_exited_between_check_and_signal(job, capsys, live):
    receipt(job)
    live["send_error"] = ProcessLookupError()
    assert run(ENTRY(job), capsys)[1] == {"outcome": "ENDED", "signalled": False,
                                          "reason": "exited"}


def test_a_signal_failure_is_unproven(job, capsys, live):
    receipt(job)
    live["send_error"] = PermissionError()
    assert run(ENTRY(job), capsys) == (2, {"outcome": "UNPROVEN", "signalled": False,
                                           "reason": "signal_failed"})


def test_a_pidfd_open_failure_is_unproven(job, capsys, live):
    receipt(job)
    live["open_error"] = PermissionError()
    assert run(ENTRY(job), capsys)[1]["reason"] == "pidfd_open_failed"
    assert live["signals"] == []


# --- refusals before any pidfd is opened -----------------------------------------------

@pytest.fixture
def no_pidfd(monkeypatch, no_kill):
    monkeypatch.setattr(sup.os, "pidfd_open", lambda *a: pytest.fail("pidfd_open reached"))
    monkeypatch.setattr(sup.signal, "pidfd_send_signal", lambda *a: pytest.fail("signal"))


@pytest.mark.parametrize("argv", [
    [], ["--job-root"], ["--entrypoint", "--job-root", "/x"],
    ["--job-root", "/x", "--tool", "database-create"],
    ["--job-root", "/x", "--tool", "Bad", "--stage-index", "0"],
    ["--job-root", "/x", "--tool", "database-create", "--stage-index", "01"],
    ["--job-root", "/x", "--tool", "database-create", "--stage-index", "100"],
    ["--job-root", "/x", "--tool", "launch", "--stage-index", "2"],   # not a holder tool
])
def test_usage(tmp_path, capsys, no_pidfd, argv):
    if len(argv) > 1 and argv[1] == "/x":
        root = tmp_path / "job"
        root.mkdir(mode=0o700)
        argv = [str(root) if a == "/x" else a for a in argv]
    assert run(argv, capsys) == (2, {"outcome": "UNPROVEN", "signalled": False,
                                     "reason": "usage"})


def test_a_non_private_or_relative_job_root_refuses(job, capsys, no_pidfd):
    receipt(job)
    job.chmod(0o755)
    assert run(ENTRY(job), capsys)[1]["reason"] == "job_root_invalid"
    job.chmod(0o700)
    assert run(["--job-root", "job", "--entrypoint"], capsys)[1]["reason"] == "job_root_invalid"


@pytest.mark.parametrize("change,reason", [
    (lambda r: r.update(schema=te.SCHEMA_LAUNCH), "receipt_not_bound"),
    (lambda r: r.update(core_identity_sha256="0" * 64), "receipt_not_bound"),
    (lambda r: r.update(boot_id="another-boot"), "receipt_not_bound"),
    (lambda r: r.update(terminal_receipt="/tmp/elsewhere.json"), "receipt_not_bound"),
    (lambda r: r["supervisor"].update(pgid=2), "receipt_not_bound"),
    (lambda r: r["supervisor"].update(pid=1), "receipt_not_bound"),
    (lambda r: r["supervisor"].update(pid=sup.PID_LIMIT + 1), "receipt_not_bound"),
    (lambda r: r["supervisor"].update(start_ticks=True), "receipt_not_bound"),
    (lambda r: r["supervisor"].update(uid=-1), "receipt_not_bound"),
    (lambda r: r.pop("supervisor"), "receipt_not_bound"),
])
def test_an_unbound_entrypoint_receipt_refuses(job, capsys, no_pidfd, change, reason):
    receipt(job, change=change)
    assert run(ENTRY(job), capsys) == (2, {"outcome": "UNPROVEN", "signalled": False,
                                           "reason": reason})


@pytest.mark.parametrize("change", [
    lambda r: r.update(tool={"name": "registry", "stage_index": 0}),
    lambda r: r.update(tool={"name": "database-create", "stage_index": 1}),
    lambda r: r.pop("tool"),
    lambda r: r.update(schema=ee.SCHEMA_LAUNCH),
])
def test_an_unbound_tool_receipt_refuses(job, capsys, no_pidfd, change):
    receipt(job, "database-create", 0, change=change)
    assert run(TOOL(job), capsys)[1]["reason"] == "receipt_not_bound"


def test_missing_oversized_and_malformed_receipts_refuse(job, capsys, no_pidfd):
    assert run(ENTRY(job), capsys)[1]["reason"] == "no_launch_receipt"
    launch, _ = receipt(job)
    launch.write_text("{not json")
    assert run(ENTRY(job), capsys)[1]["reason"] == "receipt_unreadable"
    launch.write_text(" " * (sup.MAX_RECEIPT + 1))
    assert run(ENTRY(job), capsys)[1]["reason"] == "receipt_unreadable"
    launch.unlink()
    launch.symlink_to(job / sj.CORE_NAME)
    assert run(ENTRY(job), capsys)[1]["reason"] == "receipt_unreadable"


def test_core_and_boot_must_match(job, capsys, no_pidfd, monkeypatch):
    receipt(job)
    monkeypatch.setattr(sup, "boot_id", lambda: "another-boot")
    assert run(ENTRY(job), capsys)[1]["reason"] == "boot_mismatch"
    monkeypatch.undo()
    (job / sj.CORE_NAME).chmod(0o600)
    (job / sj.CORE_NAME).write_text("{}")
    assert run(ENTRY(job), capsys)[1]["reason"] == "core_unreadable"


def test_its_own_process_is_refused(job, capsys, no_pidfd):
    receipt(job, pid=os.getpid())
    assert run(ENTRY(job), capsys)[1]["reason"] == "refused_own_process"


# --- the source never kills, never signals a group, sends TERM once ---------------------

def test_the_source_has_one_term_through_pidfd_and_no_kill():
    source = SCRIPT.read_text()
    code = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#")).split('"""', 2)[2]
    for forbidden in ("os.kill(", "killpg", "SIGKILL", "subprocess", "os.system"):
        assert forbidden not in code, forbidden
    assert code.count("pidfd_send_signal(") == 1
    assert "pidfd_send_signal(fd, signal.SIGTERM, None, 0)" in code


def test_the_wait_covers_the_supervisors_own_teardown():
    # 20 s > B = 16 s (TERM 10 s, KILL 5 s, receipt 1 s), so a supervisor that
    # honours TERM writes its terminal receipt inside the wait.
    assert sup.EXIT_WAIT_SECONDS == 20.0
    assert sup.EXIT_WAIT_SECONDS * 1e9 > ee.TEARDOWN_BUDGET_NS == 16 * 10 ** 9
