"""Pure controls for finalize-job.py (executing workflow W4).

The real finalizer runs on synthetic jobs from
test_finalizer_orchestration.build with a synthetic ended generation
(absent PIDs), as test_failure_record.py does, on the A2 path, where
finalize() stops at its gate (NOT_FINALIZED) before anything is removed
and failure_record() writes its record. Every other outcome is driven by a
spy in place of finalize / failure_record. No process, socket, database or
signal."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

from exit_fixtures import EXPECTED
from alrust_conformance import finalizer as fz
from alrust_conformance import stage_journal as sj
from test_finalizer_orchestration import STAGES, build

SYNTHETIC_SUPERVISOR = (4194293, 333)
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "finalize-job.py"
UNPROVEN_STOP = dict(verdict="UNPROVEN", reason="no_terminal_receipt")
A2 = [s for s in STAGES if s not in ("registry", "registry-exit")]
SECRET_TEXT = "synthetic-diagnostic-value"


def _script():
    spec = importlib.util.spec_from_file_location("finalize_job", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fj = _script()


@pytest.fixture
def pins(tmp_path, monkeypatch):
    for name in fj.SECRET_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "pins.json"
    path.write_text(json.dumps(EXPECTED))
    return path


@pytest.fixture
def a2_job(tmp_path, monkeypatch):
    if os.path.exists("/proc/%d" % SYNTHETIC_SUPERVISOR[0]):
        pytest.skip("synthetic ended PID is in use on this host")
    root = build(tmp_path, monkeypatch, upto="stop", generation=SYNTHETIC_SUPERVISOR,
                 stop_change=lambda s: s.update(UNPROVEN_STOP))
    with sj.StageJournal(str(root), len(A2) - 1, "database-drop", "stop"):
        pass
    return root


def run(argv, capsys):
    code = fj.main(argv)
    return code, json.loads(capsys.readouterr().out)


def argv(root, pins, stages=None):
    return ["--job-root", str(root), "--stages", ",".join(stages or A2),
            "--expected-pins", str(pins)]


# --- the real finalizer, A2 path --------------------------------------------------------

def test_a2_is_not_finalized_and_writes_the_failure_record(a2_job, pins, capsys):
    code, line = run(argv(a2_job, pins), capsys)
    assert code == 2
    written = [p for p in (a2_job / fz.EXPORT_DIR).iterdir()]
    assert len(written) == 1
    assert line == {"outcome": "NOT_FINALIZED", "cleanup": "UNPROVEN",
                    "reason": "gate_stages_invalid", "failure_record": "WRITTEN",
                    "failure_record_sha256": hashlib.sha256(written[0].read_bytes()).hexdigest()}
    assert str(a2_job) not in json.dumps(line)


def test_a_failure_record_that_cannot_be_written_is_none(a2_job, pins, capsys):
    # The stage list omits the existing database-drop journal: aggregation
    # is INCOMPLETE, so finalize refuses and failure_record writes nothing.
    stages = A2[:-1]
    code, line = run(argv(a2_job, pins, stages), capsys)
    assert code == 2 and line["outcome"] == "NOT_FINALIZED"
    assert line["failure_record"] == "NONE" and "failure_record_sha256" not in line
    assert not (a2_job / fz.EXPORT_DIR).exists()


# --- every outcome, with spies --------------------------------------------------------------

@pytest.fixture
def spies(monkeypatch, pins, tmp_path):
    calls = []
    state = {"report": None, "record": None}

    def finalize(job_root, stages, expected):
        calls.append(("finalize", job_root, stages, expected))
        return state["report"]

    def failure_record(job_root, stages):
        calls.append(("failure_record", job_root, stages))
        return state["record"]
    monkeypatch.setattr(fz, "finalize", finalize)
    monkeypatch.setattr(fz, "failure_record", failure_record)
    return calls, state


SEALED = {"outcome": "SEALED", "cleanup": "COMPLETE", "reason": "sealed",
          "manifest_sha256": "a" * 64}


def test_sealed_complete_is_zero_and_never_records_a_failure(spies, pins, capsys, tmp_path):
    calls, state = spies
    state["report"] = dict(SEALED)
    code, line = run(argv(tmp_path / "job", pins), capsys)
    assert code == 0
    assert line == dict(SEALED, failure_record="NOT_ATTEMPTED")
    assert calls == [("finalize", str(tmp_path / "job"), A2, EXPECTED)]


def test_sealed_with_unproven_cleanup_is_one(spies, pins, capsys, tmp_path):
    calls, state = spies
    state["report"] = dict(SEALED, cleanup="UNPROVEN")
    code, line = run(argv(tmp_path / "job", pins), capsys)
    assert code == 1 and line["cleanup"] == "UNPROVEN"
    assert [c[0] for c in calls] == ["finalize"]


def test_failed_is_two_and_never_records_a_failure(spies, pins, capsys, tmp_path):
    calls, state = spies
    state["report"] = {"outcome": "FAILED", "cleanup": "UNPROVEN", "reason": "scan_finding"}
    code, line = run(argv(tmp_path / "job", pins), capsys)
    assert code == 2 and line == {"outcome": "FAILED", "cleanup": "UNPROVEN",
                                  "reason": "scan_finding", "failure_record": "NOT_ATTEMPTED"}
    assert [c[0] for c in calls] == ["finalize"]


def test_not_finalized_takes_the_failure_path_with_the_same_stages(spies, pins, capsys,
                                                                  tmp_path):
    calls, state = spies
    record = tmp_path / "record.json"
    record.write_bytes(b'{"outcome":"FAIL"}\n')
    state["report"] = {"outcome": "NOT_FINALIZED", "cleanup": "UNPROVEN",
                       "reason": "aggregation_incomplete"}
    state["record"] = str(record)
    code, line = run(argv(tmp_path / "job", pins), capsys)
    assert code == 2
    assert line["failure_record"] == "WRITTEN"
    assert line["failure_record_sha256"] == hashlib.sha256(record.read_bytes()).hexdigest()
    assert calls[1] == ("failure_record", str(tmp_path / "job"), A2)


@pytest.mark.parametrize("report", [
    None, [], {"outcome": "SEALED", "cleanup": "COMPLETE", "reason": "sealed"},
    dict(SEALED, manifest_sha256="A" * 64),
    dict(SEALED, outcome="PASSED"),
    dict(SEALED, cleanup="FAILED"),
    dict(SEALED, reason=SECRET_TEXT + " with spaces"),
    dict(SEALED, reason=1),
], ids=["none", "list", "sealed-no-manifest", "manifest-upper", "outcome-unknown",
        "cleanup-unknown", "reason-free-text", "reason-not-str"])
def test_an_unexpected_report_is_unknown_and_never_echoed(spies, pins, capsys, tmp_path,
                                                          report):
    calls, state = spies
    state["report"] = report
    code, line = run(argv(tmp_path / "job", pins), capsys)
    assert code == 3 and line == {"result": "UNKNOWN", "code": "finalizer_error"}
    assert SECRET_TEXT not in json.dumps(line)


def test_a_finalizer_exception_is_unknown_and_never_echoed(spies, pins, capsys, tmp_path,
                                                          monkeypatch):
    def boom(*a, **k):
        raise RuntimeError(SECRET_TEXT)
    monkeypatch.setattr(fz, "finalize", boom)
    code, line = run(argv(tmp_path / "job", pins), capsys)
    assert code == 3 and line == {"result": "UNKNOWN", "code": "finalizer_error"}


def test_a_record_path_that_is_not_a_regular_file_is_unknown(spies, pins, capsys, tmp_path):
    calls, state = spies
    state["report"] = {"outcome": "NOT_FINALIZED", "cleanup": "UNPROVEN", "reason": "x"}
    target = tmp_path / "real.json"
    target.write_text("{}")
    link = tmp_path / "record.json"
    link.symlink_to(target)
    state["record"] = str(link)
    code, line = run(argv(tmp_path / "job", pins), capsys)
    assert code == 3 and line == {"result": "UNKNOWN", "code": "finalizer_error"}


# --- setup and usage --------------------------------------------------------------------------

def test_a_secret_in_the_environment_is_refused_before_anything(spies, pins, capsys,
                                                                 monkeypatch, tmp_path):
    calls, _ = spies
    monkeypatch.setenv("SECRET_KEY", SECRET_TEXT)
    code, line = run(argv(tmp_path / "job", pins), capsys)
    assert code == 70 and line == {"result": "NOT_RUN", "reason": "secret_in_environment"}
    assert calls == []


@pytest.mark.parametrize("content", [None, b"[]", b"{not json", "link"])
def test_bad_pins_are_setup_failures(spies, pins, capsys, tmp_path, content):
    calls, _ = spies
    bad = tmp_path / "bad-pins.json"
    if content == "link":
        bad.symlink_to(pins)
    elif content is not None:
        bad.write_bytes(content)
    code, line = run(argv(tmp_path / "job", bad), capsys)
    assert code == 70 and line == {"result": "NOT_RUN", "reason": "setup_failed"}
    assert calls == []


@pytest.mark.parametrize("args", [
    [],
    ["--job-root", "/j", "--stages", "stop"],
    ["--job-root", "rel", "--stages", "stop", "--expected-pins", "/p"],
    ["--job-root", "/j/../k", "--stages", "stop", "--expected-pins", "/p"],
    ["--job-root", "/j", "--stages", "Stop", "--expected-pins", "/p"],
    ["--job-root", "/j", "--stages", "", "--expected-pins", "/p"],
    ["--job-root", "/j", "--stages", "stop,stop", "--expected-pins", "/p"],
    ["--job-root", "/j", "--job-root", "/j", "--expected-pins", "/p"],
    ["--job-root", "/j", "--stages", "stop", "--expected-pins", "p"],
])
def test_usage(capsys, args):
    assert run(args, capsys) == (64, {"result": "NOT_RUN", "reason": "usage"})


def test_the_script_makes_no_direct_signal_or_process_call():
    # Direct calls only; the finalizer itself is classified separately
    # (it opens pidfds to observe, never to signal).
    source = SCRIPT.read_text()
    for forbidden in ("os.kill", "killpg", "pidfd_send_signal", "subprocess", "os.system",
                      "os.fork", "os.exec", "os.remove", "os.unlink", "shutil"):
        assert forbidden not in source, forbidden
