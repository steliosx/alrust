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


def test_the_launcher_records_it_after_stage_begin_and_before_launch():
    main = _main()
    [(enter_line, _)] = _calls(main, lambda c: _attr(c, "__enter__"))
    records = _calls(main, lambda c: _attr(c, "record") and c.args
                     and isinstance(c.args[0], ast.Constant)
                     and c.args[0].value == "launch_expectation")
    assert len(records) == 1
    [(record_line, record)] = records
    payload = record.args[1]
    assert _attr(payload, "launch_expectation") and payload.func.value.id == "ds"
    assert [a.id for a in payload.args] == ["binding"]
    # The stage path's launch(), the one handed the open journal.
    [(launch_line, _)] = _calls(main, lambda c: isinstance(c.func, ast.Name)
                                and c.func.id == "launch" and len(c.args) == 8
                                and isinstance(c.args[5], ast.Name)
                                and c.args[5].id == "journal")
    assert enter_line < record_line < launch_line


def test_a_failed_record_fails_the_stage_without_launching():
    main = _main()
    [handler] = [h for t in ast.walk(main) if isinstance(t, ast.Try)
                 for h in t.handlers
                 if any(_attr(c, "record") and c.args and isinstance(c.args[0], ast.Constant)
                        and c.args[0].value == "launch_expectation"
                        for b in t.body for c in ast.walk(b) if isinstance(c, ast.Call))]
    fails = [c for c in ast.walk(handler) if isinstance(c, ast.Call) and _attr(c, "fail")]
    assert [c.args[0].value for c in fails] == ["launch_expectation_failed"]
    returns = [n for n in handler.body if isinstance(n, ast.Return)]
    assert len(returns) == 1 and returns[0].value.value == 1
    caught = {e.attr if isinstance(e, ast.Attribute) else e.id for e in handler.type.elts}
    assert caught == {"DatabaseStageError", "OSError", "ValueError"}
