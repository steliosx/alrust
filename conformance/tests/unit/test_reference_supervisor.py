"""Offline fault controls for the reference supervisor and stop helper.

These run real local processes (sleep, sh, python) and no server, socket
or database. Each case checks the exact verdict and that no unrelated
process was signalled.
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
SUPERVISOR = ROOT / "conformance/scripts/reference-supervisor.py"
STOP = ROOT / "conformance/scripts/stop-reference.py"
PY = [sys.executable, "-I", "-S", "-B"]

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="Linux /proc, prctl and pidfd")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stop_mod = load(STOP, "stop_reference")
sup_mod = load(SUPERVISOR, "reference_supervisor")


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


def start(tmp_path, command, observer=False, extra=()):
    receipt, terminal = tmp_path / "launch.json", tmp_path / "terminal.json"
    args = PY + [str(SUPERVISOR), "--receipt", str(receipt), "--terminal",
                 str(terminal), "--venv", str(tmp_path)]
    if observer:
        args += ["--observer-socket", str(tmp_path / "observer.sock")]
    args += list(extra) + ["--"] + command
    proc = subprocess.Popen(args, process_group=0, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return proc, receipt, terminal


def started(proc, receipt):
    wait_for(lambda: receipt.exists() and receipt.stat().st_size > 0)
    data = receipt.read_bytes()
    return json.loads(data), hashlib.sha256(data).hexdigest()


def stop(receipt, digest, *extra_env):
    out = subprocess.run(PY + [str(STOP), str(receipt), digest],
                         capture_output=True, text=True, timeout=60)
    return out.returncode, json.loads(out.stdout)


def write_script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def cleanup():
    pids = []
    yield pids
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_normal_stop_complete_and_receipts(tmp_path):
    proc, receipt, terminal = start(tmp_path, ["/bin/sleep", "300"])
    record, digest = started(proc, receipt)
    sup = record["supervisor"]
    assert sup["pid"] == proc.pid == sup["pgid"]
    assert sup["subreaper"] == 1 and sup["uid"] == os.getuid()
    assert record["child"]["pid"] != proc.pid
    assert record["boot_id"] == stop_mod.boot_id()
    rc, out = stop(receipt, digest)
    assert (rc, out["verdict"], out["reason"]) == (0, "COMPLETE", "tree_empty")
    assert proc.wait(timeout=10) == 0
    final = json.loads(terminal.read_text())
    assert final["result"] == "tree_empty"
    assert final["trigger"] == "stop_requested"
    assert not alive(record["child"]["pid"])


def test_observer_variables_only_in_child(tmp_path):
    dump = tmp_path / "child-env"
    script = write_script(tmp_path, "child.sh",
                          "#!/bin/sh\nenv > %s\nexec /bin/sleep 300\n" % dump)
    proc, receipt, _ = start(tmp_path, [script], observer=True)
    record, digest = started(proc, receipt)
    wait_for(lambda: dump.exists() and dump.stat().st_size > 0)
    child_env = dict(line.split("=", 1) for line in
                     dump.read_text().splitlines() if "=" in line)
    assert child_env["ALRUST_OBSERVER_SOCKET"] == str(tmp_path / "observer.sock")
    assert child_env["ALRUST_OBSERVER_PARENT"] == str(proc.pid)
    own = Path("/proc/%d/environ" % proc.pid).read_bytes()
    assert b"ALRUST_OBSERVER" not in own
    assert stop(receipt, digest)[1]["verdict"] == "COMPLETE"
    proc.wait(timeout=10)


def test_inherited_observer_variables_are_dropped_without_option(tmp_path):
    dump = tmp_path / "child-env"
    script = write_script(tmp_path, "child.sh",
                          "#!/bin/sh\nenv > %s\nexec /bin/sleep 300\n" % dump)
    receipt, terminal = tmp_path / "launch.json", tmp_path / "terminal.json"
    env = dict(os.environ, ALRUST_OBSERVER_SOCKET="/tmp/x",
               ALRUST_OBSERVER_PARENT="1")
    proc = subprocess.Popen(PY + [str(SUPERVISOR), "--receipt", str(receipt),
                                  "--terminal", str(terminal), "--venv",
                                  str(tmp_path), "--", script],
                            process_group=0, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    record, digest = started(proc, receipt)
    wait_for(lambda: dump.exists() and dump.stat().st_size > 0)
    assert "ALRUST_OBSERVER" not in dump.read_text()
    assert stop(receipt, digest)[1]["verdict"] == "COMPLETE"
    proc.wait(timeout=10)


def test_child_exit_triggers_teardown_and_late_stop_is_complete(tmp_path):
    proc, receipt, terminal = start(tmp_path, ["/bin/sh", "-c", "exit 7"])
    record, digest = started(proc, receipt)
    assert proc.wait(timeout=20) == 0
    final = json.loads(terminal.read_text())
    assert final["trigger"] == "child_exited"
    assert os.WEXITSTATUS(final["child_wait_status"]) == 7
    rc, out = stop(receipt, digest)
    assert (rc, out["verdict"]) == (0, "COMPLETE")


def test_escaped_descendants_in_new_session_are_adopted_and_killed(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    # The child double-forks a grandchild into a new session (so a new
    # process group too), then the intermediate exits: the grandchild is
    # orphaned and must be adopted by the subreaper.
    script = write_script(tmp_path, "escape.sh", (
        "#!/bin/sh\n"
        "/usr/bin/setsid /bin/sh -c 'echo $$ > %s; exec /bin/sleep 300' &\n"
        "exec /bin/sleep 300\n") % pidfile)
    proc, receipt, terminal = start(tmp_path, [script])
    record, digest = started(proc, receipt)
    grandchild = int(wait_for(lambda: pidfile.exists() and
                              pidfile.read_text().strip()))
    wait_for(lambda: stop_mod.read_generation(grandchild) is not None)
    assert os.getpgid(grandchild) != proc.pid
    rc, out = stop(receipt, digest)
    assert (rc, out["verdict"]) == (0, "COMPLETE")
    assert not alive(grandchild)
    assert json.loads(terminal.read_text())["result"] == "tree_empty"


def test_term_ignoring_descendants_are_killed(tmp_path):
    script = write_script(tmp_path, "stubborn.sh", (
        "#!/bin/sh\n"
        "trap '' TERM\n"
        "/bin/sh -c \"trap '' TERM; while :; do sleep 1; done\" &\n"
        "while :; do sleep 1; done\n"))
    proc, receipt, terminal = start(tmp_path, [script])
    record, digest = started(proc, receipt)
    time.sleep(0.3)
    began = time.monotonic()
    rc, out = stop(receipt, digest)
    assert (rc, out["verdict"]) == (0, "COMPLETE")
    final = json.loads(terminal.read_text())
    assert final["result"] == "tree_empty"
    assert final["signals_sent"] >= 3
    assert 9 <= time.monotonic() - began < 20


def test_external_kill_of_supervisor_is_unproven_and_orphan_untouched(
        tmp_path, cleanup):
    pidfile = tmp_path / "child.pid"
    script = write_script(tmp_path, "child.sh",
                          "#!/bin/sh\necho $$ > %s\nexec /bin/sleep 300\n"
                          % pidfile)
    proc, receipt, terminal = start(tmp_path, [script])
    record, digest = started(proc, receipt)
    child = int(wait_for(lambda: pidfile.exists() and
                         pidfile.read_text().strip()))
    cleanup.append(child)
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)
    rc, out = stop(receipt, digest)
    assert (rc, out["verdict"], out["reason"]) == (
        2, "UNPROVEN", "no_terminal_receipt")
    assert not terminal.exists()
    assert alive(child), "the helper must not signal an orphan"


def test_reused_pid_is_never_signalled(tmp_path, cleanup):
    # A live process whose PID appears in a receipt with another
    # start_ticks stands for a reused PID (Codex's trace in 04ec376b).
    foreign = subprocess.Popen(["/bin/sleep", "300"], process_group=0)
    cleanup.append(foreign.pid)
    ticks, uid = stop_mod.read_generation(foreign.pid)
    receipt = tmp_path / "launch.json"
    data = json.dumps({
        "schema": stop_mod.SCHEMA_LAUNCH,
        "supervisor": {"pid": foreign.pid, "pgid": foreign.pid,
                       "start_ticks": ticks - 1, "uid": uid, "subreaper": 1},
        "boot_id": stop_mod.boot_id(),
        "terminal_receipt": str(tmp_path / "terminal.json")}).encode()
    receipt.write_bytes(data)
    rc, out = stop(receipt, hashlib.sha256(data).hexdigest())
    assert (rc, out["verdict"], out["reason"]) == (
        2, "UNPROVEN", "no_terminal_receipt")
    assert alive(foreign.pid)


def fake_receipt(tmp_path, pid, **overrides):
    ticks, uid = stop_mod.read_generation(pid)
    record = {"schema": stop_mod.SCHEMA_LAUNCH,
              "supervisor": {"pid": pid, "pgid": pid, "start_ticks": ticks,
                             "uid": uid, "subreaper": 1},
              "boot_id": stop_mod.boot_id(),
              "terminal_receipt": str(tmp_path / "terminal.json")}
    record.update(overrides)
    data = json.dumps(record).encode()
    path = tmp_path / "launch.json"
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest()


def test_caller_is_refused(tmp_path, cleanup):
    # A sacrificial leader whose process group the helper joins: the
    # receipt names the helper's own group, so the helper must refuse.
    # A defect would TERM the sacrificial process, not this test run.
    leader = subprocess.Popen(["/bin/sleep", "300"], process_group=0)
    cleanup.append(leader.pid)
    path, digest = fake_receipt(tmp_path, leader.pid)
    out = subprocess.run(PY + [str(STOP), str(path), digest],
                         capture_output=True, text=True, timeout=60,
                         process_group=leader.pid)
    assert (out.returncode, json.loads(out.stdout)["reason"]) == (
        2, "refused_own_process")
    assert alive(leader.pid)


def test_tree_empty_without_supervisor_exit_is_unproven(tmp_path, cleanup):
    # A terminal receipt alone is not enough: the helper must also see the
    # supervisor exit. This process never exits, so the verdict stays
    # UNPROVEN after the bounded wait, and it is not signalled.
    other = subprocess.Popen(["/bin/sleep", "300"], process_group=0)
    cleanup.append(other.pid)
    path, digest = fake_receipt(tmp_path, other.pid)
    ticks, _ = stop_mod.read_generation(other.pid)
    (tmp_path / "terminal.json").write_text(json.dumps({
        "schema": stop_mod.SCHEMA_TERMINAL, "result": "tree_empty",
        "supervisor": {"pid": other.pid, "start_ticks": ticks},
        "boot_id": stop_mod.boot_id()}))
    rc, out = stop(path, digest)
    assert (rc, out["reason"]) == (2, "supervisor_did_not_exit")
    assert alive(other.pid)


def test_boot_mismatch_is_refused(tmp_path, cleanup):
    other = subprocess.Popen(["/bin/sleep", "300"], process_group=0)
    cleanup.append(other.pid)
    path, digest = fake_receipt(tmp_path, other.pid, boot_id="0" * 36)
    rc, out = stop(path, digest)
    assert (rc, out["reason"]) == (2, "boot_mismatch")
    assert alive(other.pid)


def test_group_mismatch_is_malformed(tmp_path, cleanup):
    other = subprocess.Popen(["/bin/sleep", "300"])
    cleanup.append(other.pid)
    ticks, uid = stop_mod.read_generation(other.pid)
    path, digest = fake_receipt(tmp_path, other.pid, supervisor={
        "pid": other.pid, "pgid": os.getpgid(0), "start_ticks": ticks,
        "uid": uid, "subreaper": 1})
    rc, out = stop(path, digest)
    assert (rc, out["reason"]) == (2, "receipt_malformed")
    assert alive(other.pid)


@pytest.mark.parametrize("mutate,reason", [
    (lambda p, d: (p, "0" * 64), "receipt_hash_mismatch"),
    (lambda p, d: (p, d[:63]), "usage"),
    (lambda p, d: (Path("relative.json"), d), "usage"),
    (lambda p, d: (p.with_name("missing.json"), d), "receipt_unreadable"),
])
def test_unauthorized_or_bad_input(tmp_path, cleanup, mutate, reason):
    other = subprocess.Popen(["/bin/sleep", "300"], process_group=0)
    cleanup.append(other.pid)
    path, digest = fake_receipt(tmp_path, other.pid)
    path, digest = mutate(path, digest)
    rc, out = stop(path, digest)
    assert (rc, out["verdict"], out["reason"]) == (2, "UNPROVEN", reason)
    assert alive(other.pid)


def test_symlinked_receipt_is_refused(tmp_path, cleanup):
    other = subprocess.Popen(["/bin/sleep", "300"], process_group=0)
    cleanup.append(other.pid)
    path, digest = fake_receipt(tmp_path, other.pid)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    rc, out = stop(link, digest)
    assert (rc, out["reason"]) == (2, "receipt_unreadable")


def test_terminal_tree_not_empty_is_failed(tmp_path, cleanup):
    other = subprocess.Popen(["/bin/sleep", "300"], process_group=0)
    cleanup.append(other.pid)
    path, digest = fake_receipt(tmp_path, other.pid)
    ticks, _ = stop_mod.read_generation(other.pid)
    (tmp_path / "terminal.json").write_text(json.dumps({
        "schema": stop_mod.SCHEMA_TERMINAL, "result": "tree_not_empty",
        "supervisor": {"pid": other.pid, "start_ticks": ticks},
        "boot_id": stop_mod.boot_id()}))
    other.kill()
    other.wait()
    rc, out = stop(path, digest)
    assert (rc, out["verdict"]) == (1, "FAILED")


def test_terminal_of_another_generation_is_invalid(tmp_path, cleanup):
    other = subprocess.Popen(["/bin/sleep", "300"], process_group=0)
    cleanup.append(other.pid)
    path, digest = fake_receipt(tmp_path, other.pid)
    ticks, _ = stop_mod.read_generation(other.pid)
    (tmp_path / "terminal.json").write_text(json.dumps({
        "schema": stop_mod.SCHEMA_TERMINAL, "result": "tree_empty",
        "supervisor": {"pid": other.pid, "start_ticks": ticks + 1},
        "boot_id": stop_mod.boot_id()}))
    other.kill()
    other.wait()
    rc, out = stop(path, digest)
    assert (rc, out["reason"]) == (2, "terminal_receipt_invalid")


def test_supervisor_refuses_existing_receipt(tmp_path):
    (tmp_path / "launch.json").write_text("old")
    proc, _, terminal = start(tmp_path, ["/bin/sleep", "300"])
    assert proc.wait(timeout=10) == 70
    assert not terminal.exists()


def test_supervisor_refuses_when_not_group_leader(tmp_path):
    receipt, terminal = tmp_path / "launch.json", tmp_path / "terminal.json"
    proc = subprocess.run(PY + [str(SUPERVISOR), "--receipt", str(receipt),
                                "--terminal", str(terminal), "--venv",
                                str(tmp_path), "--", "/bin/sleep", "300"],
                          capture_output=True, timeout=10)
    assert proc.returncode == 70
    assert not receipt.exists()


@pytest.mark.parametrize("args", [
    [], ["--receipt", "relative", "--terminal", "/t", "--venv", "/v", "--",
         "/bin/true"],
    ["--receipt", "/r", "--terminal", "/t", "--venv", "/v"],
    ["--receipt", "/r", "--receipt", "/r2", "--terminal", "/t", "--venv",
     "/v", "--", "/bin/true"],
])
def test_supervisor_usage(args):
    proc = subprocess.run(PY + [str(SUPERVISOR)] + args, capture_output=True,
                          timeout=10, process_group=0)
    assert proc.returncode == 64


def test_interrupted_launch_receipt_tears_down(tmp_path):
    receipt = tmp_path / "missing-dir" / "launch.json"
    terminal = tmp_path / "terminal.json"
    proc = subprocess.Popen(PY + [str(SUPERVISOR), "--receipt", str(receipt),
                                  "--terminal", str(terminal), "--venv",
                                  str(tmp_path), "--", "/bin/sleep", "300"],
                            process_group=0, stderr=subprocess.PIPE)
    assert proc.wait(timeout=20) == 4
    final = json.loads(terminal.read_text())
    assert (final["trigger"], final["result"]) == ("receipt_failed",
                                                   "tree_empty")


def test_unwritable_terminal_leaves_no_terminal_and_stop_is_unproven(tmp_path):
    receipt = tmp_path / "launch.json"
    terminal = tmp_path / "missing-dir" / "terminal.json"
    proc = subprocess.Popen(PY + [str(SUPERVISOR), "--receipt", str(receipt),
                                  "--terminal", str(terminal), "--venv",
                                  str(tmp_path), "--", "/bin/sleep", "300"],
                            process_group=0, stderr=subprocess.PIPE)
    record, digest = started(proc, receipt)
    rc, out = stop(receipt, digest)
    assert (rc, out["reason"]) == (2, "no_terminal_receipt")
    assert proc.wait(timeout=10) == 4


def test_sigint_also_stops(tmp_path):
    proc, receipt, terminal = start(tmp_path, ["/bin/sleep", "300"])
    record, digest = started(proc, receipt)
    os.kill(proc.pid, signal.SIGINT)
    assert proc.wait(timeout=20) == 0
    assert json.loads(terminal.read_text())["result"] == "tree_empty"
    assert stop(receipt, digest)[1]["verdict"] == "COMPLETE"


def test_supervisor_missing_venv_fails_before_fork(tmp_path):
    receipt, terminal = tmp_path / "launch.json", tmp_path / "terminal.json"
    marker = tmp_path / "child-ran"
    proc = subprocess.run(PY + [str(SUPERVISOR), "--receipt", str(receipt),
                                "--terminal", str(terminal), "--venv",
                                str(tmp_path / "no-venv"), "--", "/bin/sh",
                                "-c", "touch %s" % marker],
                          capture_output=True, timeout=10, process_group=0)
    assert proc.returncode == 70
    assert not receipt.exists() and not terminal.exists()
    assert not marker.exists()


def run_in_process(capsys, path, digest):
    rc = stop_mod.main([str(path), digest])
    return rc, json.loads(capsys.readouterr().out)


def test_unreadable_generation_is_unproven(tmp_path, cleanup, monkeypatch,
                                           capsys):
    other = subprocess.Popen(["/bin/sleep", "300"], process_group=0)
    cleanup.append(other.pid)
    path, digest = fake_receipt(tmp_path, other.pid)

    def unreadable(pid):
        raise PermissionError(13, "denied")
    monkeypatch.setattr(stop_mod, "read_generation", unreadable)
    rc, out = run_in_process(capsys, path, digest)
    assert (rc, out["reason"]) == (2, "generation_unreadable")
    assert alive(other.pid)


def test_unreadable_boot_id_is_unproven(tmp_path, cleanup, monkeypatch,
                                        capsys):
    other = subprocess.Popen(["/bin/sleep", "300"], process_group=0)
    cleanup.append(other.pid)
    path, digest = fake_receipt(tmp_path, other.pid)

    def unreadable():
        raise PermissionError(13, "denied")
    monkeypatch.setattr(stop_mod, "boot_id", unreadable)
    rc, out = run_in_process(capsys, path, digest)
    assert (rc, out["reason"]) == (2, "boot_unreadable")
    assert alive(other.pid)


def test_truncated_receipt_with_matching_hash_is_malformed(tmp_path, cleanup):
    other = subprocess.Popen(["/bin/sleep", "300"], process_group=0)
    cleanup.append(other.pid)
    path, _ = fake_receipt(tmp_path, other.pid)
    data = path.read_bytes()[:-20]
    path.write_bytes(data)
    rc, out = stop(path, hashlib.sha256(data).hexdigest())
    assert (rc, out["reason"]) == (2, "receipt_malformed")
    assert alive(other.pid)


def test_oversized_receipt_is_unreadable(tmp_path):
    path = tmp_path / "launch.json"
    data = b" " * (stop_mod.MAX_RECEIPT + 1)
    path.write_bytes(data)
    rc, out = stop(path, hashlib.sha256(data).hexdigest())
    assert (rc, out["reason"]) == (2, "receipt_unreadable")


def test_emptiness_comes_from_echild_not_from_the_scan(monkeypatch):
    # A child the /proc scan cannot see must keep the tree non-empty: only
    # waitpid's ECHILD proves emptiness. This test process is the parent.
    child = subprocess.Popen(["/bin/sleep", "300"])
    try:
        monkeypatch.setattr(sup_mod, "children", lambda me: [])
        log = {"signals": 0, "reaped": 0}
        assert sup_mod.signal_phase(os.getpid(), signal.SIGTERM, 0.3,
                                    log) is False
        assert log["signals"] == 0
    finally:
        child.kill()
        child.wait()
