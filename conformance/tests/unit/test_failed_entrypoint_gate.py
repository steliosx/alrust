"""Pure controls for the failed-entrypoint gate, A2 (R-EP plan P3b).

Complete synthetic jobs through stop (test_finalizer_orchestration.build
with a synthetic ended reference generation; exit_fixtures with absent
PIDs), so no process is created or signalled. The drop runner's own drop is
replaced by a recorder, so no SQL is sent either."""
import os

import pytest

from exit_fixtures import EXPECTED
from alrust_conformance import database_stage as ds
from alrust_conformance import entrypoint_exit as ee
from alrust_conformance import finalizer as fz
from alrust_conformance import stage_journal as sj
from alrust_conformance import tool_exit as te
from test_finalizer_orchestration import STAGES, build

SYNTHETIC_SUPERVISOR = (4194293, 333)
PREFIX = STAGES[:STAGES.index("stop") + 1]
DEADLINE = {"change": lambda launch, terminal: terminal.update(trigger="deadline"),
            "execution": "FAIL"}


@pytest.fixture
def job(tmp_path, monkeypatch):
    if os.path.exists("/proc/%d" % SYNTHETIC_SUPERVISOR[0]):
        pytest.skip("synthetic ended PID is in use on this host")

    def make(**kw):
        kw.setdefault("generation", SYNTHETIC_SUPERVISOR)
        kw.setdefault("upto", "stop")
        return build(tmp_path, monkeypatch, **kw)
    return make


def gate(root, stages=PREFIX, expected=EXPECTED):
    return fz.failed_entrypoint_quiescence(str(root), stages, expected)


# --- the two admitted cases ------------------------------------------------------

def test_f1_closed_failed_entrypoint(job):
    root = job(fail_stage="entrypoint")
    result = gate(root)
    assert result["case"] == "F1" and result["aggregation"]["verdict"] == "FAILED"
    proof = result["proofs"]["entrypoint_exit"]
    assert (proof["verdict"], proof["execution"], proof["entrypoint_state"]) == \
        ("COMPLETE", "FAIL", "FAILED")
    assert [h["tool"] for h in result["proofs"]["holders"]] == ["database-create"]


def test_f2_complete_journal_with_a_verified_execution_failure(job):
    root = job(entrypoint_exit=DEADLINE)
    result = gate(root)
    assert result["case"] == "F2" and result["aggregation"]["verdict"] == "VALID"
    proof = result["proofs"]["entrypoint_exit"]
    assert (proof["verdict"], proof["execution"], proof["entrypoint_state"], proof["trigger"]) \
        == ("COMPLETE", "FAIL", "COMPLETE", "deadline")


def test_f2_nonzero_exit_is_a_verified_failure(job):
    root = job(entrypoint_exit={"change": lambda l, t: t.update(child_wait_status=256),
                                "execution": "FAIL"})
    assert gate(root)["case"] == "F2"


# --- refusals ------------------------------------------------------------------------

def test_a_passing_entrypoint_is_refused(job):
    root = job()
    with pytest.raises(fz.FinalizerError, match="entrypoint_not_failed"):
        gate(root)


@pytest.mark.parametrize("failed", [("entrypoint", "launch"), ("entrypoint", "stop"),
                                    ("entrypoint", "launch-confirm")])
def test_a_second_failed_stage_is_refused(job, failed):
    root = job(fail_stage=failed)
    with pytest.raises(fz.FinalizerError, match="failed_gate_other_stage_not_complete"):
        gate(root)


def test_only_another_stage_failed_is_refused(job):
    root = job(fail_stage="launch")
    with pytest.raises(fz.FinalizerError, match="failed_gate_other_stage_not_complete"):
        gate(root)


def test_an_incomplete_entrypoint_journal_is_refused(job):
    root = job(fail_stage="entrypoint")
    path = sj.journal_path(str(root), STAGES.index("entrypoint"), "entrypoint")
    data = open(path, "rb").read()
    os.chmod(path, 0o600)
    with open(path, "wb") as f:
        f.write(b"\n".join(data.splitlines()[:-1]) + b"\n")     # no stage_complete
    with pytest.raises(fz.FinalizerError, match="failed_gate_incomplete"):
        gate(root)


@pytest.mark.parametrize("build_kw,code", [
    # F2 claimed, but the receipts contradict it: the verdict is UNPROVEN.
    ({"entrypoint_exit": {"execution": "FAIL"}}, "entrypoint_exit_not_proven"),
    # F1 with an UNPROVEN verdict (execution PASS claimed for a FAILED journal).
    ({"fail_stage": "entrypoint", "entrypoint_exit": {"execution": "PASS"}},
     "entrypoint_exit_not_proven"),
])
def test_an_unproven_exit_is_refused(job, build_kw, code):
    root = job(**build_kw)
    with pytest.raises(fz.FinalizerError, match=code):
        gate(root)


def test_a_stale_entrypoint_receipt_is_refused(job):
    root = job(fail_stage="entrypoint")
    with open(ee.receipt_paths(str(root))[1], "a") as f:
        f.write(" ")
    with pytest.raises(fz.FinalizerError, match="entrypoint_exit_verdict_mismatch"):
        gate(root)


def test_a_stale_create_holder_is_refused(job):
    root = job(fail_stage="entrypoint")
    os.unlink(te.receipt_paths(str(root), "database-create", 0)[1])
    with pytest.raises(fz.FinalizerError, match="holder_verdict_mismatch"):
        gate(root)


@pytest.mark.parametrize("kw,code", [
    ({"stop_change": lambda s: s.update(reason="tree_not_empty")}, "stop_not_proven"),
    ({"omit_handoff": True}, "launch_handoff_missing"),
])
def test_stop_and_handoff_are_required(job, kw, code):
    root = job(fail_stage="entrypoint", **kw)
    with pytest.raises(fz.FinalizerError, match=code):
        gate(root)


def test_the_prefix_must_end_at_stop(job):
    root = job(fail_stage="entrypoint", upto="registry-exit")
    with pytest.raises(fz.FinalizerError, match="failed_gate_prefix_invalid"):
        gate(root, STAGES[:STAGES.index("registry-exit") + 1])


@pytest.mark.parametrize("stages", [
    [s for s in PREFIX if s != "entrypoint-exit"],
    [s for s in PREFIX if s != "database-create-exit"],
    [s for s in PREFIX if s != "stop"],
])
def test_order_and_exit_stages_are_required(job, stages):
    root = job(fail_stage="entrypoint")
    with pytest.raises(fz.FinalizerError):
        gate(root, stages)


def test_wrong_pins_are_refused(job):
    root = job(fail_stage="entrypoint")
    pins = dict(EXPECTED, entrypoint=dict(EXPECTED["entrypoint"],
                                          entrypoint_script_sha256="f" * 64))
    with pytest.raises(fz.FinalizerError, match="gate_pins_mismatch"):
        gate(root, expected=pins)


# --- the other gates refuse the failed cases (disjoint) ---------------------------------

@pytest.mark.parametrize("build_kw,code", [
    ({"fail_stage": "entrypoint"}, "writer_gate_failed"),
    ({"entrypoint_exit": DEADLINE}, "entrypoint_execution_failed"),
])
def test_the_registry_gate_refuses_both_cases(job, build_kw, code):
    root = job(**build_kw)
    with pytest.raises(fz.FinalizerError, match=code):
        fz.writer_quiescence(str(root), PREFIX, EXPECTED, ("database-create",))


# --- the drop runner's A2 path -------------------------------------------------------------

DROP_KW = dict(port=5432, superuser="postgres", superuser_password="postgres",
               psql_path="/usr/bin/psql", deadline_ns=10**18)


@pytest.fixture
def recorder(monkeypatch):
    calls = []

    def drop_database(job_root, journal, **kwargs):
        calls.append("drop")
        return "COMPLETE"
    monkeypatch.setattr(ds, "drop_database", drop_database)
    monkeypatch.setattr(ds, "Psql", lambda *a, **k: calls.append("psql"))
    return calls


def _drop_records(root):
    path = sj.journal_path(str(root), len(PREFIX), ds.DROP_STAGE)
    return [r["evidence"] for r in map(__import__("json").loads,
                                         open(path, "rb").read().splitlines())]


@pytest.mark.parametrize("build_kw,case", [({"fail_stage": "entrypoint"}, "F1"),
                                           ({"entrypoint_exit": DEADLINE}, "F2")])
def test_drop_runs_after_the_failed_entrypoint_gate(job, recorder, build_kw, case):
    root = job(**build_kw)
    assert ds.run_drop_stage(str(root), PREFIX, launched=True, expected=EXPECTED,
                             **DROP_KW) == "COMPLETE"
    assert recorder == ["drop"]
    kinds = [(r["kind"], r["payload"]) for r in _drop_records(root)]
    assert kinds[1] == ("drop_gate", {"gate": "failed_entrypoint", "case": case})


def test_drop_refused_by_the_failed_entrypoint_gate_sends_nothing(job, recorder):
    root = job()                                       # a passing entrypoint
    with pytest.raises(ds.DatabaseStageError, match="entrypoint_not_failed"):
        ds.run_drop_stage(str(root), PREFIX, launched=True, expected=EXPECTED, **DROP_KW)
    assert recorder == []
    assert _drop_records(root)[-1]["payload"] == {"result": "FAILED",
                                                   "code": "entrypoint_not_failed"}
