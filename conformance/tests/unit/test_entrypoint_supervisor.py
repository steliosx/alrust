"""Offline controls for the entrypoint supervisor (R-EP, design 409eabfa).

These run real local processes (python, sh, sleep) and no server, socket,
database, container or VM. Each case checks the exact receipts and that no
unrelated process was signalled.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[3]
SUPERVISOR = ROOT / "conformance/scripts/entrypoint-supervisor.py"
PY = [sys.executable, "-I", "-S", "-B"]

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="Linux /proc and prctl")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sup = load(SUPERVISOR, "entrypoint_supervisor")

# A stand-in entrypoint: writes a stage-journal-shaped file, then behaves as
# MODE says. Only the last line matters to the supervisor's observation.
ENTRYPOINT = r'''
import json, os, signal, subprocess, sys, time
journal, mode = sys.argv[1], sys.argv[2]
def line(seq, kind, payload):
    return json.dumps({"sequence": seq, "evidence": {"kind": kind, "payload": payload}}) + "\n"
def write(lines):
    with open(journal, "w") as f:
        f.write("".join(lines))
begin = line(0, "stage_begin", {})
if mode == "complete":
    write([begin, line(1, "stage_complete", {"result": "COMPLETE"})])
elif mode == "failed":
    write([begin, line(1, "stage_complete", {"result": "FAILED", "code": "secret_capture_aborted"})])
    sys.exit(1)
elif mode == "incomplete":
    write([begin])
elif mode == "missing":
    pass
elif mode == "escape":
    write([begin, line(1, "stage_complete", {"result": "COMPLETE"})])
    subprocess.Popen(["/bin/sleep", "300"], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
elif mode == "stuck-term-complete":
    def done(signum, frame):
        write([begin, line(1, "stage_complete", {"result": "COMPLETE"})])
        sys.exit(0)
    signal.signal(signal.SIGTERM, done)
    time.sleep(300)
elif mode == "stuck-ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(300)
elif mode == "sleep":
    time.sleep(300)
'''


def wait_for(predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out")


def alive(pid):
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            state = f.read().rsplit(b")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state != b"Z"


def setup(tmp_path):
    core = tmp_path / "core-identity.json"
    core.write_bytes(b'{"synthetic": "core"}\n')
    script = tmp_path / "entrypoint.py"
    script.write_text(ENTRYPOINT)
    return core, hashlib.sha256(core.read_bytes()).hexdigest(), script


def start(tmp_path, mode, *, deadline_s=60.0, core_sha=None, extra_args=(),
          script_in_command=True):
    core, digest, script = setup(tmp_path)
    receipt, terminal = tmp_path / "launch.json", tmp_path / "terminal.json"
    journal = tmp_path / "02-entrypoint.jsonl"
    deadline = time.monotonic_ns() + int(deadline_s * 1e9)
    command = [sys.executable, "-I", str(script) if script_in_command else "-c",
               str(journal), mode]
    if not script_in_command:
        command = [sys.executable, "-I", "-c", "pass"]
    args = PY + [str(SUPERVISOR), "--receipt", str(receipt), "--terminal", str(terminal),
                 "--core", str(core), "--core-sha256", core_sha or digest,
                 "--script", str(script), "--entrypoint-journal", str(journal),
                 "--deadline-monotonic-ns", str(deadline)] + list(extra_args) + ["--"] + command
    proc = subprocess.Popen(args, process_group=0, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return proc, receipt, terminal, journal, digest, script


def finish(proc, terminal, timeout=60):
    rc = proc.wait(timeout=timeout)
    return rc, json.loads(terminal.read_text())


def test_complete_entrypoint_passes_with_bound_receipts(tmp_path):
    proc, receipt, terminal, journal, digest, script = start(tmp_path, "complete")
    rc, final = finish(proc, terminal)
    launch_bytes = receipt.read_bytes()
    launch = json.loads(launch_bytes)
    assert rc == 0
    assert launch["schema"] == "alrust-entrypoint-launch/1"
    assert launch["supervisor"]["pid"] == proc.pid == launch["supervisor"]["pgid"]
    assert launch["supervisor"]["subreaper"] == 1
    assert launch["core_identity_sha256"] == digest
    assert launch["command"]["script"]["sha256"] == hashlib.sha256(script.read_bytes()).hexdigest()
    assert launch["deadline"]["teardown_budget_ns"] == 16_000_000_000
    assert launch["deadline"]["boot_id"] == launch["boot_id"] == sup.boot_id()
    assert final["schema"] == "alrust-entrypoint-terminal/1"
    assert final["launch_receipt_sha256"] == hashlib.sha256(launch_bytes).hexdigest()
    assert (final["result"], final["trigger"], final["execution"]) == \
        ("tree_empty", "child_exited", "PASS")
    assert final["child_signalled"] is False and final["other_descendant_signals"] == 0
    assert final["entrypoint_journal"] == {
        "sha256": hashlib.sha256(journal.read_bytes()).hexdigest(),
        "state": "COMPLETE", "code": None}


@pytest.mark.parametrize("mode,state,code", [
    ("failed", "FAILED", "secret_capture_aborted"),
    ("incomplete", "INCOMPLETE", None),
])
def test_failed_or_incomplete_journal_is_execution_fail(tmp_path, mode, state, code):
    proc, receipt, terminal, journal, digest, script = start(tmp_path, mode)
    rc, final = finish(proc, terminal)
    assert rc == 0 and final["result"] == "tree_empty"
    assert final["execution"] == "FAIL"
    assert final["entrypoint_journal"]["state"] == state
    assert final["entrypoint_journal"]["code"] == code
    assert final["entrypoint_journal"]["sha256"] == \
        hashlib.sha256(journal.read_bytes()).hexdigest()


def test_missing_journal_is_incomplete_without_digest(tmp_path):
    proc, receipt, terminal, journal, digest, script = start(tmp_path, "missing")
    rc, final = finish(proc, terminal)
    assert final["entrypoint_journal"] == {"sha256": None, "state": "INCOMPLETE", "code": None}
    assert final["execution"] == "FAIL"


def test_escaped_descendant_is_killed_and_counted(tmp_path):
    proc, receipt, terminal, journal, digest, script = start(tmp_path, "escape")
    rc, final = finish(proc, terminal)
    assert rc == 0 and final["result"] == "tree_empty"
    # The entrypoint itself exited cleanly; a descendant outlived it and was
    # signalled. The supervisor records that; the gates refuse on it.
    assert final["child_signalled"] is False
    assert final["other_descendant_signals"] >= 1
    assert final["children_reaped"] >= 2


def test_deadline_with_term_handled_is_execution_fail(tmp_path):
    proc, receipt, terminal, journal, digest, script = start(
        tmp_path, "stuck-term-complete", deadline_s=16.5)
    rc, final = finish(proc, terminal, timeout=60)
    assert final["trigger"] == "deadline" and final["result"] == "tree_empty"
    assert final["child_signalled"] is True
    # The child closed its journal COMPLETE on TERM: still FAIL.
    assert final["entrypoint_journal"]["state"] == "COMPLETE"
    assert final["execution"] == "FAIL"


def test_deadline_with_term_ignored_needs_kill(tmp_path):
    proc, receipt, terminal, journal, digest, script = start(
        tmp_path, "stuck-ignore-term", deadline_s=16.5)
    rc, final = finish(proc, terminal, timeout=90)
    launch = json.loads(receipt.read_text())
    assert final["trigger"] == "deadline" and final["result"] == "tree_empty"
    assert final["child_signalled"] is True and final["signals_sent"] >= 2
    assert os.WIFSIGNALED(final["child_wait_status"])
    assert os.WTERMSIG(final["child_wait_status"]) == signal.SIGKILL
    assert final["execution"] == "FAIL"
    assert not alive(launch["child"]["pid"])


def test_deadline_too_close_refuses_before_fork(tmp_path):
    proc, receipt, terminal, journal, digest, script = start(tmp_path, "complete",
                                                             deadline_s=5)
    rc = proc.wait(timeout=10)
    assert rc == sup.EXIT_SETUP
    assert b"teardown budget" in proc.stderr.read()
    assert not receipt.exists() and not terminal.exists() and not journal.exists()


def test_core_mismatch_refuses_before_fork(tmp_path):
    proc, receipt, terminal, journal, digest, script = start(tmp_path, "complete",
                                                             core_sha="b" * 64)
    assert proc.wait(timeout=10) == sup.EXIT_SETUP
    assert not receipt.exists() and not journal.exists()


def test_script_must_be_in_command(tmp_path):
    proc, receipt, terminal, journal, digest, script = start(tmp_path, "complete",
                                                             script_in_command=False)
    assert proc.wait(timeout=10) == sup.EXIT_USAGE
    assert not receipt.exists()


def test_stop_request_is_execution_fail(tmp_path):
    proc, receipt, terminal, journal, digest, script = start(tmp_path, "sleep")
    wait_for(lambda: receipt.exists() and receipt.stat().st_size > 0)
    proc.send_signal(signal.SIGTERM)
    rc, final = finish(proc, terminal)
    assert final["trigger"] == "stop_requested" and final["result"] == "tree_empty"
    assert final["child_signalled"] is True and final["execution"] == "FAIL"


def test_outside_kill_leaves_no_terminal_receipt(tmp_path):
    proc, receipt, terminal, journal, digest, script = start(tmp_path, "sleep")
    launch = json.loads(wait_for(lambda: receipt.exists() and receipt.stat().st_size > 0
                                 and receipt.read_text()))
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)
    assert not terminal.exists()
    # The orphan left supervision; the gates read this as UNPROVEN.
    child = launch["child"]["pid"]
    try:
        os.kill(child, signal.SIGKILL)
    except ProcessLookupError:
        pass


def test_existing_receipt_is_refused(tmp_path):
    (tmp_path / "launch.json").write_text("{}")
    proc, receipt, terminal, journal, digest, script = start(tmp_path, "complete")
    assert proc.wait(timeout=10) == sup.EXIT_SETUP
    assert not terminal.exists() and not journal.exists()


def test_not_group_leader_is_refused(tmp_path):
    core, digest, script = setup(tmp_path)
    journal = tmp_path / "j.jsonl"
    deadline = time.monotonic_ns() + 60 * 10**9
    out = subprocess.run(PY + [str(SUPERVISOR), "--receipt", str(tmp_path / "r.json"),
                               "--terminal", str(tmp_path / "t.json"), "--core", str(core),
                               "--core-sha256", digest, "--script", str(script),
                               "--entrypoint-journal", str(journal),
                               "--deadline-monotonic-ns", str(deadline), "--",
                               sys.executable, str(script), str(journal), "complete"],
                         capture_output=True, timeout=30)
    assert out.returncode == sup.EXIT_SETUP
    assert not (tmp_path / "r.json").exists()


@pytest.mark.parametrize("args", [
    [], ["--receipt", "relative.json"], ["--deadline-monotonic-ns", "-5"],
])
def test_usage(args):
    out = subprocess.run(PY + [str(SUPERVISOR)] + args + ["--", "/bin/true"],
                         capture_output=True, timeout=30)
    assert out.returncode == sup.EXIT_USAGE


def _journal(tmp_path, data):
    path = tmp_path / "j.jsonl"
    path.write_bytes(data)
    return str(path)


def _line(seq, kind, payload):
    return (json.dumps({"sequence": seq, "evidence": {"kind": kind, "payload": payload}})
            + "\n").encode()


@pytest.mark.parametrize("data,state", [
    (_line(0, "stage_begin", {}) + _line(1, "stage_complete", {"result": "COMPLETE"}),
     "COMPLETE"),
    (_line(0, "stage_begin", {}) + _line(1, "stage_complete",
                                          {"result": "FAILED", "code": "x"}), "FAILED"),
    (_line(0, "stage_begin", {}), "INCOMPLETE"),
    (_line(0, "stage_begin", {}) + b'{"sequence": 1', "INCOMPLETE"),
    (b"[1, 2]\n", "INCOMPLETE"),
    (_line(0, "stage_complete", {"result": "COMPLETE", "extra": 1}), "INCOMPLETE"),
])
def test_journal_observation(tmp_path, data, state):
    observed = sup.journal_observation(_journal(tmp_path, data))
    assert observed["state"] == state
    assert observed["sha256"] == hashlib.sha256(data).hexdigest()


def test_journal_symlink_and_fifo_are_incomplete_and_do_not_block(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(_line(1, "stage_complete", {"result": "COMPLETE"}))
    link = tmp_path / "link"
    link.symlink_to(target)
    assert sup.journal_observation(str(link))["state"] == "INCOMPLETE"
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert sup.journal_observation(str(fifo)) == \
        {"sha256": None, "state": "INCOMPLETE", "code": None}


def test_oversized_journal_is_incomplete(tmp_path):
    path = _journal(tmp_path, b"x" * (sup.MAX_JOURNAL + 1))
    assert sup.journal_observation(path) == \
        {"sha256": None, "state": "INCOMPLETE", "code": None}


# REP1-R1 regression controls (Codex's counterexample, 20261006T065451Z
# f645dca1). Synthetic: main() runs with fork, kill, signal handlers,
# subreaper, /proc scans and reaping all mocked; no process is created.
def run_mocked(tmp_path, reap_batches, scan, source=None):
    import contextlib
    from unittest import mock
    if source is None:
        mod = load(SUPERVISOR, "entrypoint_supervisor_mocked")
    else:
        spec = importlib.util.spec_from_loader("entrypoint_supervisor_mutant", loader=None)
        mod = importlib.util.module_from_spec(spec)
        exec(compile(source, "mutant", "exec"), mod.__dict__)
    core, digest, script = setup(tmp_path)
    journal = tmp_path / "journal.jsonl"
    journal.write_bytes(_line(0, "stage_begin", {})
                        + _line(1, "stage_complete", {"result": "COMPLETE"}))
    argv = ["--receipt", str(tmp_path / "launch.json"), "--terminal",
            str(tmp_path / "terminal.json"), "--core", str(core), "--core-sha256", digest,
            "--script", str(script), "--entrypoint-journal", str(journal),
            "--deadline-monotonic-ns", str(time.monotonic_ns() + 60 * 10**9),
            "--", sys.executable, str(script)]
    records, kills, batches = {}, [], iter(reap_batches)

    def write(path, obj):
        data = (json.dumps(obj, sort_keys=True) + "\n").encode()
        records[Path(path).name] = json.loads(data)
        return hashlib.sha256(data).hexdigest()

    with contextlib.ExitStack() as stack:
        for obj, name, value in [
                (mod.os, "getpid", lambda: 999), (mod.os, "getpgid", lambda pid: 999),
                (mod.os, "fork", lambda: 100), (mod.os, "kill", lambda p, s: kills.append(p)),
                (mod.signal, "signal", lambda *a: None), (mod, "set_subreaper", lambda: 1),
                (mod, "read_stat", lambda pid: (999, 123)), (mod, "boot_id", lambda: "boot"),
                (mod, "children", lambda me: list(scan)),
                (mod, "reap_all", lambda: next(batches)), (mod, "write_exclusive", write)]:
            stack.enter_context(mock.patch.object(obj, name, value))
        rc = mod.main(argv)
    return rc, records["terminal.json"], kills


def test_reused_pid_never_overwrites_the_original_status(tmp_path):
    # Original C_E (PID 100) exits 1; later an adopted descendant that
    # reused PID 100 is reaped with status 0. The original status stays.
    rc, final, kills = run_mocked(tmp_path, [([(100, 256)], False), ([(100, 0)], True)], [])
    assert final["child_wait_status"] == 256
    assert final["execution"] == "FAIL"
    assert final["other_descendants_reaped"] == 1


def test_reused_pid_signalled_after_exit_is_another_descendant(tmp_path):
    # Original C_E exits 0; in teardown PID 100 is held by a different
    # process. Its signal counts as another descendant, never as C_E.
    rc, final, kills = run_mocked(tmp_path, [([(100, 0)], False), ([(100, 9)], True)], [100])
    assert kills == [100]
    assert final["child_signalled"] is False
    assert final["other_descendant_signals"] == 1
    assert final["child_wait_status"] == 0
    # Execution reflects C_E alone; the descendant count is for the gates.
    assert final["execution"] == "PASS"


def test_duplicate_pid_statuses_in_one_batch_keep_the_first(tmp_path):
    rc, final, kills = run_mocked(tmp_path, [([(100, 256), (100, 0)], False), ([], True)], [])
    assert final["child_wait_status"] == 256
    assert final["other_descendants_reaped"] == 1
    assert final["execution"] == "FAIL"


def test_record_reaped_attribution():
    log = {"child_status": None, "other_reaped": 0}
    sup.record_reaped(log, 100, [(7, 0), (100, 256), (100, 0), (8, 0)])
    assert log == {"child_status": 256, "other_reaped": 3}


def test_child_signalled_condition_is_now_redundant_in_codex_scenario(tmp_path):
    # REP1-N1: at f4c19067, removing "not child_signalled" changed the
    # verdict in this scenario. With generation-aware attribution the
    # original and the mutant agree, because a reused PID is never C_E.
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    original = run_mocked(tmp_path / "a", [([(100, 0)], False), ([(100, 9)], True)], [100])
    source = SUPERVISOR.read_text()
    assert source.count('and not log["child_signalled"]') == 1
    mutant = run_mocked(tmp_path / "b", [([(100, 0)], False), ([(100, 9)], True)], [100],
                        source=source.replace('and not log["child_signalled"]', "and True"))
    assert original[1]["execution"] == mutant[1]["execution"] == "PASS"
