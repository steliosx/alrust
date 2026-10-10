"""Independent P4 pre-signal controls; synthetic receipt, live path fully mocked.
No process, pidfd, signal, socket or wait targets a real process.
"""
import contextlib
import io
import json
import os
import signal
import subprocess
from pathlib import Path
import pytest
import test_stop_supervisor as peer
from alrust_conformance import entrypoint_exit as ee
from alrust_conformance import stage_journal as sj
from alrust_conformance import tool_exit as te
import exit_fixtures as xf

sup = peer.sup
job = peer.job

@pytest.fixture(autouse=True)
def external_refusals(monkeypatch):
    # Refuse mixed checkouts before the synthetic core fixture is built.
    checkout = Path(__file__).resolve().parents[3]
    for module in (ee, sj, te):
        assert Path(module.__file__).resolve().parents[2] == checkout
    assert Path(peer.__file__).resolve().parents[3] == checkout
    assert Path(peer.sup.__file__).resolve().parents[2] == checkout
    assert Path(xf.__file__).resolve().parents[3] == checkout

    def refuse(*a, **k):
        pytest.fail("independent pure control attempted external process/signal")
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "kill", refuse)
    monkeypatch.setattr(os, "killpg", refuse)


def full_receipt(root, tool=None):
    launch, terminal = peer.receipt(root, tool, 0 if tool else None)
    value = json.loads(launch.read_bytes())
    key = "stage_journal" if tool else "entrypoint_journal"
    value.update(child={"pid": 4194296, "start_ticks": 222},
                 command={"argv_sha256": "c"*64,
                          "executable": {"path": "/synthetic/python", "dev": 1, "ino": 2},
                          "script": {"path": "/synthetic/driver.py", "dev": 1, "ino": 3,
                                     "sha256": "d"*64}},
                 deadline={"monotonic_ns": 10**18, "teardown_budget_ns": ee.TEARDOWN_BUDGET_NS,
                           "boot_id": value["boot_id"]},
                 launched_utc_ns=1, monotonic_ns=1)
    value[key] = sj.journal_path(str(root), 0, tool or "entrypoint")
    spec = te.spec(tool, 0) if tool else ee.ENTRYPOINT
    ee._check_launch_schema(value, spec)
    return launch, value


def invoke(root, monkeypatch, *, tool=None, generation=None):
    calls = []
    # /dev/null is only an inert local descriptor. No actual pidfd is opened.
    monkeypatch.setattr(sup.os, "pidfd_open", lambda *a: os.open("/dev/null", os.O_RDONLY))
    monkeypatch.setattr(sup, "read_generation", lambda p: generation or (peer.TICKS, os.getuid()))
    monkeypatch.setattr(sup.signal, "pidfd_send_signal", lambda *a: calls.append(a[1:]))
    monkeypatch.setattr(sup, "wait_exit", lambda *a: True)
    args = ["--job-root", str(root), "--entrypoint"] if tool is None else [
        "--job-root", str(root), "--tool", tool, "--stage-index", "0"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = sup.main(args)
    return code, json.loads(buf.getvalue()), calls

@pytest.mark.parametrize("tool", [None, "database-create"])
def test_full_schema_positive(job, monkeypatch, tool):
    path, value = full_receipt(job, tool)
    path.write_text(json.dumps(value))
    code, result, calls = invoke(job, monkeypatch, tool=tool)
    assert (code, result["outcome"], result["signalled"]) == (0, "ENDED", True)
    assert calls == [(signal.SIGTERM, None, 0)]

@pytest.mark.parametrize("bad", [False, 0.0])
def test_mistyped_tool_stage_never_signals(job, monkeypatch, bad):
    path, value = full_receipt(job, "database-create")
    value["tool"]["stage_index"] = bad
    path.write_text(json.dumps(value))
    code, result, calls = invoke(job, monkeypatch, tool="database-create")
    assert calls == [], "P4-BIND-R1: mistyped tool index reached mocked SIGTERM"
    assert code == 2 and result["outcome"] == "UNPROVEN"

@pytest.mark.parametrize("change", [
    lambda v: v.pop("child"),
    lambda v: v["supervisor"].update(pgid=float(peer.ABSENT)),
    lambda v: v["supervisor"].update(subreaper=0),
])
def test_invalid_launch_identity_never_signals(job, monkeypatch, change):
    path, value = full_receipt(job)
    change(value)
    path.write_text(json.dumps(value))
    code, result, calls = invoke(job, monkeypatch)
    assert calls == [], "P4-BIND-R1: invalid launch schema/identity reached mocked SIGTERM"
    assert code == 2 and result["outcome"] == "UNPROVEN"


def test_foreign_uid_in_receipt_and_live_generation_never_signals(job, monkeypatch):
    path, value = full_receipt(job)
    value["supervisor"]["uid"] = os.getuid() + 1
    path.write_text(json.dumps(value))
    code, result, calls = invoke(job, monkeypatch, generation=(peer.TICKS, os.getuid() + 1))
    assert calls == [], "P4-OWNER-R2: matching foreign uid reached mocked SIGTERM"
    assert code == 2 and result["outcome"] == "UNPROVEN"
