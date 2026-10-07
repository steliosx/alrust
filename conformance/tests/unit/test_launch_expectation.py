"""Pure controls for executing workflow W3, part 2: the launch journal's
launch_expectation record.

The payload builder runs on a real launch_prerequisites binding from a
synthetic job (test_finalizer_orchestration.build with a synthetic ended
generation; no process, socket or database). Its placement in
launch-reference.py's main() is checked on the source's syntax tree only:
the launcher's live path starts processes and stays NOT_RUN here."""
import ast
import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from exit_fixtures import EXPECTED
from alrust_conformance import database_stage as ds
from alrust_conformance import stage_journal as sj
from test_finalizer_orchestration import build

SYNTHETIC_SUPERVISOR = (4194293, 333)
LAUNCH_PREFIX = ["database-create", "database-create-exit"]
LAUNCHER = Path(__file__).resolve().parents[2] / "scripts" / "launch-reference.py"


@pytest.fixture
def binding(tmp_path, monkeypatch):
    if os.path.exists("/proc/%d" % SYNTHETIC_SUPERVISOR[0]):
        pytest.skip("synthetic ended PID is in use on this host")
    root = build(tmp_path, monkeypatch, upto="database-create-exit",
                 generation=SYNTHETIC_SUPERVISOR)
    return root, ds.launch_prerequisites(str(root), LAUNCH_PREFIX, EXPECTED)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_the_payload_is_the_admitted_binding_and_gate_inputs(binding):
    root, bound = binding
    payload = ds.launch_expectation(bound)
    info = os.stat(root / "database")
    assert payload == {
        "role": "alrust_scaffold_123_1",
        "database_dir": {"dev": info.st_dev, "ino": info.st_ino},
        "core_identity_sha256": _sha(root / sj.CORE_NAME),
        "gate_journals": [
            {"name": name, "sha256": _sha(sj.journal_path(str(root), index, name))}
            for index, name in enumerate(LAUNCH_PREFIX)]}


def test_the_payload_round_trips_through_a_real_launch_journal(binding):
    # StageJournal.record accepts it, and the closed journal aggregates VALID
    # with the expectation as its only record (no URL or secret field).
    root, bound = binding
    payload = ds.launch_expectation(bound)
    with sj.StageJournal(str(root), 2, "launch", "database-create-exit") as journal:
        journal.record("launch_expectation", payload)
    stages = LAUNCH_PREFIX + ["launch"]
    assert sj.aggregate(str(root), stages)["verdict"] == "VALID"
    lines = Path(sj.journal_path(str(root), 2, "launch")).read_bytes().splitlines()
    records = [json.loads(line)["evidence"] for line in lines]
    assert [(r["kind"], r["payload"]) for r in records[1:-1]] == [("launch_expectation",
                                                                   payload)]
    assert set(payload) == {"role", "database_dir", "core_identity_sha256", "gate_journals"}


def _broken(change):
    def apply(bound):
        bound = copy.deepcopy(bound)
        change(bound)
        return bound
    return apply


@pytest.mark.parametrize("change", [
    lambda b: b.pop("role"),
    lambda b: b.pop("aggregation"),
    lambda b: b["database_dir"].pop("ino"),
    lambda b: b.update(role="postgres"),
    lambda b: b.update(role=None),
    lambda b: b["database_dir"].update(dev=True),
    lambda b: b["database_dir"].update(ino="1"),
    lambda b: b["aggregation"].update(verdict="FAILED"),
    lambda b: b["aggregation"].pop("core_identity_sha256"),
    lambda b: b["aggregation"].update(core_identity_sha256="A" * 64),
    lambda b: b["aggregation"]["stages"].pop(),
    lambda b: b["aggregation"]["stages"].reverse(),
    lambda b: b["aggregation"]["stages"][0].update(sha256="0" * 63),
    lambda b: b["aggregation"]["stages"][1].update(name="launch"),
], ids=["no-role", "no-aggregation", "no-ino", "role-not-alrust", "role-none", "dev-bool",
        "ino-str", "not-valid", "no-core-sha", "core-sha-upper", "one-journal",
        "journals-reversed", "short-sha", "wrong-journal"])
def test_a_malformed_binding_is_refused(binding, change):
    _, bound = binding
    with pytest.raises(ds.DatabaseStageError) as error:
        ds.launch_expectation(_broken(change)(bound))
    assert error.value.code == "launch_expectation_invalid"


# --- W3-BIND-R1: the launch journal must bind the gate's inputs -----------------------------

def _load_launcher():
    import importlib.util
    spec = importlib.util.spec_from_file_location("launch_reference_w3", LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lr = _load_launcher()


def _rewrite_core(root):
    # Another VALID /3 core (one field changed, canonical bytes), as if the
    # core were replaced between the gate's read and the journal's entry.
    path = root / sj.CORE_NAME
    core = json.loads(path.read_bytes())
    core["captured_utc_ns"] += 1
    os.chmod(path, 0o600)
    path.write_bytes(sj._dumps(core))
    sj.validate_core(path.read_bytes(), str(root))


def _change_exit_journal(root):
    path = Path(sj.journal_path(str(root), 1, "database-create-exit"))
    os.chmod(path, 0o600)
    with open(path, "ab") as f:
        f.write(b"\n")


def _launch_stage(root, bound, monkeypatch=None, record_error=None):
    """Enter the real launch journal and run the launcher's own expectation
    step; on a failure, close it exactly as main() does (fail, then FAILED)."""
    journal = sj.StageJournal(str(root), 2, "launch", "database-create-exit").__enter__()
    if record_error is not None:
        def broken(kind, payload):
            raise record_error
        monkeypatch.setattr(journal, "record", broken)
    failure = lr.record_launch_expectation(ds, journal, bound)
    if failure is None:
        journal.__exit__(None, None, None)
    else:
        journal.fail(failure)
        journal.__exit__(lr.LaunchFailed, lr.LaunchFailed(1), None)
    lines = Path(sj.journal_path(str(root), 2, "launch")).read_bytes().splitlines()
    return failure, [json.loads(line)["evidence"] for line in lines]


def test_matching_entry_records_the_expectation(binding):
    root, bound = binding
    failure, records = _launch_stage(root, bound)
    assert failure is None
    assert [r["kind"] for r in records] == ["stage_begin", "launch_expectation",
                                            "stage_complete"]
    assert records[-1]["payload"] == {"result": "COMPLETE"}


@pytest.mark.parametrize("mutate,code", [
    (_rewrite_core, "launch_core_changed"),
    (_change_exit_journal, "launch_previous_changed"),
], ids=["core-replaced", "exit-journal-changed"])
def test_inputs_changed_between_gate_and_entry_fail_before_launch(binding, mutate, code):
    root, bound = binding                    # the gate has already run here
    mutate(root)
    failure, records = _launch_stage(root, bound)
    assert failure == code
    assert [r["kind"] for r in records] == ["stage_begin", "stage_complete"]
    assert records[-1]["payload"] == {"result": "FAILED", "code": code}


@pytest.mark.parametrize("error", [OSError("disk"), sj.JournalError("record_invalid")])
def test_a_failed_record_is_a_fixed_failure(binding, monkeypatch, error):
    root, bound = binding
    failure, records = _launch_stage(root, bound, monkeypatch, record_error=error)
    assert failure == "launch_expectation_failed"
    assert records[-1]["payload"] == {"result": "FAILED", "code": failure}


def test_a_malformed_binding_fails_the_launch_stage(binding):
    root, bound = binding
    bad = _broken(lambda b: b.update(role="postgres"))(bound)
    failure, records = _launch_stage(root, bad)
    assert failure == "launch_expectation_invalid"
    assert records[-1]["payload"] == {"result": "FAILED", "code": failure}


@pytest.mark.parametrize("core,previous,code", [
    (None, "ok", "launch_core_changed"),
    ("0" * 64, "ok", "launch_core_changed"),
    ("core", None, "launch_previous_changed"),
    ("core", {"index": 0}, "launch_previous_changed"),
    ("core", "index", "launch_previous_changed"),
    ("core", "name", "launch_previous_changed"),
    ("core", "sha", "launch_previous_changed"),
], ids=["core-none", "core-other", "previous-none", "previous-shape", "previous-index",
        "previous-name", "previous-sha"])
def test_check_launch_entry(binding, core, previous, code):
    _, bound = binding
    expectation = ds.launch_expectation(bound)
    exit_journal = expectation["gate_journals"][-1]
    good = {"index": 1, "name": exit_journal["name"], "sha256": exit_journal["sha256"]}
    core = expectation["core_identity_sha256"] if core == "core" else core
    previous = {"ok": good, "index": dict(good, index=2),
                "name": dict(good, name="database-create"),
                "sha": dict(good, sha256="0" * 64)}.get(previous, previous) \
        if isinstance(previous, str) else previous
    with pytest.raises(ds.DatabaseStageError) as error:
        ds.check_launch_entry(expectation, core, previous)
    assert error.value.code == code


def test_check_launch_entry_accepts_the_exact_entry(binding):
    _, bound = binding
    expectation = ds.launch_expectation(bound)
    exit_journal = expectation["gate_journals"][-1]
    assert ds.check_launch_entry(
        expectation, expectation["core_identity_sha256"],
        {"index": 1, "name": "database-create-exit", "sha256": exit_journal["sha256"]}) is None


def test_the_journal_exposes_its_previous_stage_binding(binding):
    root, _ = binding
    exit_sha = hashlib.sha256(Path(sj.journal_path(str(root), 1,
                                                   "database-create-exit")).read_bytes()).hexdigest()
    journal = sj.StageJournal(str(root), 2, "launch", "database-create-exit")
    assert journal.previous_stage is None
    with journal:
        assert journal.previous_stage == {"index": 1, "name": "database-create-exit",
                                          "sha256": exit_sha}


# --- placement in launch-reference.py main(), source only ---------------------------------

def _main():
    tree = ast.parse(LAUNCHER.read_text())
    [main] = [node for node in tree.body if isinstance(node, ast.FunctionDef)
              and node.name == "main"]
    return main


def _calls(node, test):
    return sorted((c.lineno, c) for c in ast.walk(node)
                  if isinstance(c, ast.Call) and test(c))


def _attr(call, name):
    return isinstance(call.func, ast.Attribute) and call.func.attr == name


def _is_step(c):
    return isinstance(c.func, ast.Name) and c.func.id == "record_launch_expectation"


def test_the_launcher_runs_the_step_after_stage_begin_and_before_launch():
    main = _main()
    [(enter_line, _)] = _calls(main, lambda c: _attr(c, "__enter__"))
    [(step_line, step)] = _calls(main, _is_step)
    assert [a.id for a in step.args] == ["ds", "journal", "binding"]
    # The stage path's launch(), the one handed the open journal.
    [(launch_line, _)] = _calls(main, lambda c: isinstance(c.func, ast.Name)
                                and c.func.id == "launch" and len(c.args) == 8
                                and isinstance(c.args[5], ast.Name)
                                and c.args[5].id == "journal")
    assert enter_line < step_line < launch_line


def test_a_step_failure_fails_the_stage_without_launching():
    main = _main()
    [branch] = [n for n in ast.walk(main) if isinstance(n, ast.If)
                and isinstance(n.test, ast.Compare)
                and isinstance(n.test.left, ast.Name) and n.test.left.id == "failure"]
    fails = [c for c in ast.walk(branch) if isinstance(c, ast.Call) and _attr(c, "fail")]
    assert len(fails) == 1 and fails[0].args[0].id == "failure"
    returns = [n for n in branch.body if isinstance(n, ast.Return)]
    assert len(returns) == 1 and returns[0].value.value == 1
    # The only record of the expectation is inside the step, never in main.
    assert not _calls(main, lambda c: _attr(c, "record") and c.args
                      and isinstance(c.args[0], ast.Constant)
                      and c.args[0].value == "launch_expectation")
