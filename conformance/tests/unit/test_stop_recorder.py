"""Pure controls for the stop recorder (executing workflow W1).

Synthetic jobs from test_finalizer_orchestration.build (absent synthetic
generations); the stop helper is an in-process fake, and the script's
bounded runner is replaced, so no process is created or signalled."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

from alrust_conformance import finalizer as fz
from alrust_conformance import stage_journal as sj
from alrust_conformance import stop_recorder as sr
from test_finalizer_orchestration import STAGES, build

SYNTHETIC_SUPERVISOR = (4194293, 333)
INDEX = STAGES.index("stop")
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "record-stop.py"
COMPLETE_LINE = b'{"reason": "tree_empty", "verdict": "COMPLETE"}\n'


@pytest.fixture
def job(tmp_path, monkeypatch):
    if os.path.exists("/proc/%d" % SYNTHETIC_SUPERVISOR[0]):
        pytest.skip("synthetic ended PID is in use on this host")
    root = build(tmp_path, monkeypatch, upto="entrypoint-exit", generation=SYNTHETIC_SUPERVISOR)
    return root


def digest(root):
    return hashlib.sha256((root / "reference" / "launch.json").read_bytes()).hexdigest()


class Helper:
    def __init__(self, returncode=0, stdout=COMPLETE_LINE, stderr=b"", error=None, effect=None):
        self.calls, self.result, self.error, self.effect = [], (returncode, stdout, stderr), error, effect

    def __call__(self, argv):
        self.calls.append(argv)
        if self.effect:
            self.effect()
        if self.error:
            raise self.error
        return self.result


def run(root, helper, receipt=None):
    return sr.run_stop_stage(str(root), INDEX, receipt or digest(root), run_helper=helper,
                             helper_argv=["stop-reference", "argv"])


def records(root):
    data = Path(sj.journal_path(str(root), INDEX, "stop")).read_bytes()
    return [json.loads(line)["evidence"] for line in data.splitlines()]


def stop_records(root):
    return [r["payload"] for r in records(root) if r["kind"] == "stop_verdict"]


def closing(root):
    return records(root)[-1]["payload"]


# --- the positive path, checked by the real gate reader ---------------------------------

def test_complete_is_recorded_and_accepted_by_the_gate(job):
    helper = Helper()
    assert run(job, helper) == {"stage": "COMPLETE", "verdict": "COMPLETE", "reason": "tree_empty"}
    assert helper.calls == [["stop-reference", "argv"]]
    [stop] = stop_records(job)
    terminal = (job / "reference" / "terminal.json").read_bytes()
    assert stop == {"verdict": "COMPLETE", "reason": "tree_empty",
                    "launch_receipt_sha256": digest(job),
                    "terminal_receipt_sha256": hashlib.sha256(terminal).hexdigest()}
    assert closing(job) == {"result": "COMPLETE"}
    # The real gate reader accepts the record (re-reads, re-hashes, generation ended).
    aggregation = sj.aggregate(str(job), STAGES[:INDEX + 1])
    assert aggregation["verdict"] == "VALID"
    core = json.loads((job / sj.CORE_NAME).read_bytes())
    recorded, _ = fz._stop_evidence(str(job), fz._journal_records(str(job), aggregation), core)
    assert recorded == stop


def test_raw_helper_output_is_kept_privately_and_owned(job):
    run(job, Helper(stderr=b"diagnostic\n"))
    owned = [r["payload"]["path"] for r in records(job) if r["kind"] == "owned_resource"]
    assert owned == [str(job / name) for name in sr.RAW_NAMES]
    assert (job / "stop-helper.stdout").read_bytes() == COMPLETE_LINE
    assert (job / "stop-helper.stderr").read_bytes() == b"diagnostic\n"
    assert all(os.stat(job / n).st_mode & 0o777 == 0o600 for n in sr.RAW_NAMES)


def test_the_stop_verdicts_are_the_finalizers():
    assert sr.STOP_VERDICTS is fz.STOP_VERDICTS


# --- failure evidence is kept, not erased ------------------------------------------------

def test_unproven_without_terminal_records_a_null_terminal_digest(job):
    (job / "reference" / "terminal.json").unlink()
    line = b'{"reason": "no_terminal_receipt", "verdict": "UNPROVEN"}\n'
    assert run(job, Helper(2, line))["verdict"] == "UNPROVEN"
    [stop] = stop_records(job)
    assert stop["terminal_receipt_sha256"] is None and stop["launch_receipt_sha256"] == digest(job)
    assert closing(job) == {"result": "COMPLETE"}


@pytest.mark.parametrize("reason", sorted(sr.ERRNO_REASONS))
def test_errno_is_accepted_on_its_two_reasons_and_never_recorded(job, reason):
    line = ('{"errno": 1, "reason": "%s", "verdict": "UNPROVEN"}\n' % reason).encode()
    assert run(job, Helper(2, line))["stage"] == "COMPLETE"
    [stop] = stop_records(job)
    assert set(stop) == {"verdict", "reason", "launch_receipt_sha256", "terminal_receipt_sha256"}


def test_tree_not_empty_is_recorded_as_failed(job):
    line = b'{"reason": "tree_not_empty", "verdict": "FAILED"}\n'
    assert run(job, Helper(1, line))["verdict"] == "FAILED"
    assert stop_records(job)[0]["terminal_receipt_sha256"] is not None


# --- strict output grammar --------------------------------------------------------------

@pytest.mark.parametrize("returncode,stdout", [
    (2, COMPLETE_LINE),                                                      # exit code
    (0, b'{"reason": "tree_empty", "verdict": "COMPLETE", "x": 1}\n'),      # extra key
    (0, b'{"reason": "tree_empty", "verdict": "COMPLETE", "errno": 1}\n'),  # errno elsewhere
    (2, b'{"errno": true, "reason": "signal_failed", "verdict": "UNPROVEN"}\n'),
    (2, b'{"errno": "1", "reason": "signal_failed", "verdict": "UNPROVEN"}\n'),
    (2, b'{"errno": 1.0, "reason": "signal_failed", "verdict": "UNPROVEN"}\n'),
    (2, b'{"reason": "not_a_reason", "verdict": "UNPROVEN"}\n'),
    (0, b'{"reason": "no_terminal_receipt", "verdict": "COMPLETE"}\n'),    # mismatched pair
    (0, b'{"reason": "tree_empty", "reason": "tree_empty", "verdict": "COMPLETE"}\n'),
    (0, b'{"reason": "tree_empty", "verdict": "COMPLETE"}'),                # no newline
    (0, COMPLETE_LINE * 2),                                                 # two lines
    (0, COMPLETE_LINE + b"\n"),                                         # trailing blank line
    (0, b"\n" + COMPLETE_LINE),                                         # leading blank line
    (0, b""),
    (0, b'["COMPLETE", "tree_empty"]\n'),
    (0, b'{"reason": NaN, "verdict": "COMPLETE"}\n'),
    (0, b"\xff\n"),
    (0, b'{"reason": "tree_empty", "verdict": "COMPLETE"}' + b" " * 5000 + b"\n"),
])
def test_any_other_output_closes_failed_without_a_verdict(job, returncode, stdout):
    assert run(job, Helper(returncode, stdout)) == {"stage": "FAILED",
                                                   "code": "helper_output_invalid"}
    assert stop_records(job) == []
    assert closing(job) == {"result": "FAILED", "code": "helper_output_invalid"}


# --- binding and availability -----------------------------------------------------------

def test_a_wrong_receipt_digest_never_runs_the_helper(job):
    helper = Helper()
    assert run(job, helper, receipt="0" * 64) == {"stage": "FAILED",
                                                 "code": "receipt_digest_mismatch"}
    assert helper.calls == [] and stop_records(job) == []


def test_complete_without_a_terminal_receipt_is_refused(job):
    (job / "reference" / "terminal.json").unlink()
    assert run(job, Helper())["code"] == "terminal_missing"
    assert stop_records(job) == []


def test_a_launch_receipt_changed_during_the_helper_is_refused(job):
    path = job / "reference" / "launch.json"
    helper = Helper(effect=lambda: (path.chmod(0o600), path.write_bytes(path.read_bytes() + b" ")))
    assert run(job, helper)["code"] == "receipt_changed"


@pytest.mark.parametrize("error", [OSError("x"), TimeoutError()])
def test_an_unavailable_helper_closes_failed(job, error):
    assert run(job, Helper(error=error)) == {"stage": "FAILED", "code": "helper_unavailable"}
    assert not (job / "stop-helper.stdout").exists()


def test_missing_or_linked_launch_receipt(job, tmp_path):
    receipt = digest(job)
    path = job / "reference" / "launch.json"
    os.rename(path, tmp_path / "moved.json")
    path.symlink_to(tmp_path / "moved.json")
    assert run(job, Helper(), receipt)["code"] == "receipt_unreadable"


def test_usage_is_refused_before_any_journal(job):
    with pytest.raises(sr.StopRecorderError, match="usage"):
        sr.run_stop_stage(str(job), INDEX, "XYZ", run_helper=Helper(), helper_argv=[])
    assert not Path(sj.journal_path(str(job), INDEX, "stop")).exists()


# --- the script: argv, bounded runner, exit codes (runner replaced in-process) -----------

def _script():
    spec = importlib.util.spec_from_file_location("record_stop", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script(monkeypatch):
    module = _script()
    ds, _ = module.package()
    calls = []

    def bounded(argv, stdin, env, timeout_s, limit):
        calls.append((argv, stdin, env, timeout_s, limit))
        return calls_result[0]
    calls_result = [(None, 0, COMPLETE_LINE, b"")]
    monkeypatch.setattr(ds, "_bounded_run", bounded)
    return module, calls, calls_result


@pytest.mark.parametrize("result,code", [
    ((None, 0, COMPLETE_LINE, b""), 0),
    ((None, 2, b'{"reason": "no_terminal_receipt", "verdict": "UNPROVEN"}\n', b""), 1),
    ((None, 0, b"garbage\n", b""), 2),
    (("timeout", None, b"", b""), 2),
])
def test_script_runs_the_helper_once_with_a_cleared_environment(job, script, capsys, result, code):
    module, calls, calls_result = script
    calls_result[0] = result
    argv = ["--job-root", str(job), "--stage-index", str(INDEX), "--receipt-digest", digest(job)]
    assert module.main(argv) == code
    [(helper_argv, stdin, env, timeout_s, limit)] = calls
    assert helper_argv[1:] == ["-I", "-S", "-B", module.STOP_HELPER,
                               str(job / "reference" / "launch.json"), digest(job), str(job)]
    assert stdin is None and env == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    assert timeout_s == 60 and limit == sr.MAX_OUTPUT
    assert set(json.loads(capsys.readouterr().out)) <= {"stage", "verdict", "reason", "code"}


@pytest.mark.parametrize("argv", [
    [], ["--job-root", "rel", "--stage-index", "6", "--receipt-digest", "0" * 64],
    ["--job-root", "/j", "--stage-index", "06", "--receipt-digest", "0" * 64],
    ["--job-root", "/j", "--stage-index", "6", "--receipt-digest", "0" * 63],
    ["--stage-index", "6", "--job-root", "/j", "--receipt-digest", "0" * 64],
])
def test_script_usage(script, capsys, argv):
    module, calls, _ = script
    assert module.main(argv) == 64 and calls == []
