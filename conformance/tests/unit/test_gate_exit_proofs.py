"""Pure controls for the gates' exit proofs (R-EP plan P3a).

Complete synthetic jobs from test_finalizer_orchestration.build with a
synthetic ended reference-supervisor generation (an absent PID), so no
process is created or signalled. Each refusal asserts that no mutable read,
SQL, removal or export happened: the registry load, the sealing steps, the
export directory, the psql client, private-file reads, the inventory read
and the drop are spied on."""
import json
import os
import shutil

import pytest

import exit_fixtures as xf
from exit_fixtures import EXPECTED
from alrust_conformance import database_stage as ds
from alrust_conformance import entrypoint_exit as ee
from alrust_conformance import finalizer as fz
from alrust_conformance import stage_journal as sj
from alrust_conformance import tool_exit as te
from test_finalizer_orchestration import STAGES, build

SYNTHETIC_SUPERVISOR = (4194293, 333)
PREFIX = STAGES[:STAGES.index("stop") + 1]
DROP_PREFIX = STAGES[:STAGES.index("registry-exit") + 1]


@pytest.fixture
def job(tmp_path, monkeypatch):
    if os.path.exists("/proc/%d" % SYNTHETIC_SUPERVISOR[0]):
        pytest.skip("synthetic ended PID is in use on this host")

    def make(**kw):
        kw.setdefault("generation", SYNTHETIC_SUPERVISOR)
        return build(tmp_path, monkeypatch, **kw)
    return make


@pytest.fixture
def spies(monkeypatch):
    called = []

    def spy(owner, name):
        real = getattr(owner, name)

        def wrapper(*a, **k):
            called.append(name)
            return real(*a, **k)
        monkeypatch.setattr(owner, name, wrapper)
    for owner, name in ((fz, "load_registry"), (fz, "_sealing_steps"), (ds, "Psql"),
                        (ds, "_read_private_file"), (ds, "read_inventory"),
                        (ds, "drop_database"), (ds, "build_registry")):
        spy(owner, name)
    real_mkdir = os.mkdir

    def mkdir(path, *a, **k):
        if str(path).endswith(os.sep + fz.EXPORT_DIR):
            called.append("export_mkdir")
        return real_mkdir(path, *a, **k)
    monkeypatch.setattr(fz.os, "mkdir", mkdir)
    return called


# --- positives -------------------------------------------------------------------

def test_finalize_seals_with_all_exit_proofs(job):
    root = job()
    report = fz.finalize(str(root), STAGES, EXPECTED)
    report.pop("manifest_sha256", None)
    assert report == {"outcome": "SEALED", "cleanup": "COMPLETE", "reason": "sealed"}


def test_registry_writer_gate_passes_with_the_create_holder(job):
    root = job(upto="stop")
    evidence = fz.writer_quiescence(str(root), PREFIX, EXPECTED, ("database-create",))
    assert [h["tool"] for h in evidence["proofs"]["holders"]] == ["database-create"]
    assert evidence["proofs"]["entrypoint_exit"]["execution"] == "PASS"


def test_drop_writer_gate_passes_with_both_holders(job):
    root = job(upto="registry-exit")
    evidence = fz.writer_quiescence(str(root), DROP_PREFIX, EXPECTED,
                                    ("database-create", "registry"))
    assert [h["tool"] for h in evidence["proofs"]["holders"]] == ["database-create", "registry"]


# --- refusals before any side effect: finalize ---------------------------------------

def _rewrite(path, change):
    data = json.loads(open(path).read())
    change(data)
    with open(path, "w") as f:
        f.write(json.dumps(data, sort_keys=True) + "\n")


FINALIZE_VECTORS = [
    # entrypoint-exit recorded UNPROVEN (execution contradiction in the receipts)
    ({"entrypoint_exit": {"execution": "FAIL"}}, None, "entrypoint_exit_not_proven"),
    # closure proven but a teardown signal went to another descendant
    ({"entrypoint_exit": {"other_signals": 1}}, None, "entrypoint_descendant_signalled"),
    # the entrypoint terminal receipt changed after the verdict was recorded (stale)
    ({}, lambda root: _rewrite(ee.receipt_paths(str(root))[1],
                               lambda t: t.update(teardown_ns=7)),
     "entrypoint_exit_verdict_mismatch"),
    # a holder's terminal receipt removed after its verdict (stale)
    ({}, lambda root: os.unlink(te.receipt_paths(str(root), "registry",
                                                 STAGES.index("registry"))[1]),
     "holder_verdict_mismatch"),
    ({}, lambda root: os.unlink(te.receipt_paths(str(root), "database-create", 0)[1]),
     "holder_verdict_mismatch"),
]


@pytest.mark.parametrize("build_kw,after,code", FINALIZE_VECTORS)
def test_finalize_refuses_before_any_mutable_step(job, spies, build_kw, after, code):
    root = job(**build_kw)
    if after:
        after(root)
    report = fz.finalize(str(root), STAGES, EXPECTED)
    assert (report["outcome"], report["reason"]) == ("NOT_FINALIZED", code)
    assert spies == [] and not (root / fz.EXPORT_DIR).exists()


@pytest.mark.parametrize("expected,code", [
    (dict(EXPECTED, entrypoint=dict(EXPECTED["entrypoint"], entrypoint_script_sha256="f" * 64)),
     "gate_pins_mismatch"),
    (dict(EXPECTED, tools=dict(EXPECTED["tools"], registry={
        "driver_sha256": "f" * 64, "interpreter_path": xf.PYTHON})), "holder_verdict_mismatch"),
    ({"entrypoint": EXPECTED["entrypoint"]}, "gate_pins_invalid"),
])
def test_finalize_refuses_pins_other_than_the_cores(job, spies, expected, code):
    root = job()
    report = fz.finalize(str(root), STAGES, expected)
    assert (report["outcome"], report["reason"]) == ("NOT_FINALIZED", code)
    assert spies == []


@pytest.mark.parametrize("stages", [
    [s for s in STAGES if s != "registry-exit"],
    [s for s in STAGES if s != "entrypoint-exit"],
    [s for s in STAGES if s != "database-create-exit"],
])
def test_finalize_refuses_a_missing_exit_stage(job, spies, stages):
    root = job()
    report = fz.finalize(str(root), stages, EXPECTED)
    assert report["outcome"] == "NOT_FINALIZED" and spies == []


def test_finalize_refuses_a_forged_entrypoint_verdict(job, spies, monkeypatch):
    # A recorded verdict that differs in any field from the recomputation.
    real = xf.entrypoint_verdict

    def forged(*a, **k):
        verdict = real(*a, **k)
        verdict["trigger"] = "deadline"
        return verdict
    monkeypatch.setattr(xf, "entrypoint_verdict", forged)
    root = job()
    report = fz.finalize(str(root), STAGES, EXPECTED)
    assert (report["outcome"], report["reason"]) == ("NOT_FINALIZED",
                                                     "entrypoint_exit_verdict_mismatch")
    assert spies == []


# --- refusals before any side effect: the writer gate and the database stages ----------

def test_registry_stage_refuses_a_stale_create_holder_before_sql(job, spies):
    root = job(upto="stop")
    os.unlink(te.receipt_paths(str(root), "database-create", 0)[1])
    with pytest.raises(ds.DatabaseStageError, match="holder_verdict_mismatch"):
        ds.run_registry_stage(str(root), PREFIX, expected=EXPECTED,
                              secret_key_path=str(root / "secret-key"), port=5432,
                              psql_path="/usr/bin/psql", deadline_ns=10**18)
    assert spies == []


def test_registry_stage_refuses_an_unproven_entrypoint_before_sql(job, spies):
    root = job(upto="stop", entrypoint_exit={"other_signals": 2})
    with pytest.raises(ds.DatabaseStageError, match="entrypoint_descendant_signalled"):
        ds.run_registry_stage(str(root), PREFIX, expected=EXPECTED,
                              secret_key_path=str(root / "secret-key"), port=5432,
                              psql_path="/usr/bin/psql", deadline_ns=10**18)
    assert spies == []


def _drop_kw():
    return dict(port=5432, superuser="postgres", superuser_password="postgres",
                psql_path="/usr/bin/psql", deadline_ns=10**18)


def test_launched_drop_refuses_a_stale_registry_holder_before_sql(job, spies):
    root = job(upto="registry-exit")
    os.unlink(te.receipt_paths(str(root), "registry", STAGES.index("registry"))[1])
    with pytest.raises(ds.DatabaseStageError, match="holder_verdict_mismatch"):
        ds.run_drop_stage(str(root), DROP_PREFIX, launched=True, expected=EXPECTED, **_drop_kw())
    assert spies == []


def test_launched_drop_needs_the_registry_exit_stage(job, spies):
    root = job(upto="registry")
    prefix = STAGES[:STAGES.index("registry") + 1]
    with pytest.raises(ds.DatabaseStageError):
        ds.run_drop_stage(str(root), prefix, launched=True, expected=EXPECTED, **_drop_kw())
    assert spies == []


@pytest.mark.parametrize("launched", [False, None, 1])
def test_drop_path_comes_from_the_evidence(job, spies, launched):
    root = job(upto="registry-exit")
    with pytest.raises(ds.DatabaseStageError, match="drop_path_mismatch"):
        ds.run_drop_stage(str(root), DROP_PREFIX, launched=launched, expected=EXPECTED,
                          **_drop_kw())
    assert spies == []


# --- the no-launch gate ---------------------------------------------------------------

def _no_launch_job(job, **kw):
    root = job(upto="database-create-exit", **kw)
    shutil.rmtree(root / "reference")                        # nothing was launched
    return root


NO_LAUNCH = ["database-create", "database-create-exit"]


def test_no_launch_gate_passes_with_the_create_holder(job):
    root = _no_launch_job(job)
    ds.no_launch_quiescence(str(root), NO_LAUNCH, EXPECTED)


def test_no_launch_gate_passes_after_a_failed_create(job):
    root = _no_launch_job(job, fail_stage="database-create")
    assert sj.aggregate(str(root), NO_LAUNCH)["verdict"] == "FAILED"
    ds.no_launch_quiescence(str(root), NO_LAUNCH, EXPECTED)


@pytest.mark.parametrize("prefix", [["database-create"], NO_LAUNCH + ["launch"],
                                    ["database-create-exit", "database-create"]])
def test_no_launch_gate_prefix_is_exact(job, spies, prefix):
    root = _no_launch_job(job)
    with pytest.raises(ds.DatabaseStageError, match="no_launch_prefix_invalid"):
        ds.no_launch_quiescence(str(root), prefix, EXPECTED)
    assert spies == []


def test_no_launch_drop_refuses_a_stale_create_holder_before_sql(job, spies):
    root = _no_launch_job(job)
    os.unlink(te.receipt_paths(str(root), "database-create", 0)[1])
    with pytest.raises(ds.DatabaseStageError, match="holder_verdict_mismatch"):
        ds.run_drop_stage(str(root), NO_LAUNCH, launched=False, expected=EXPECTED, **_drop_kw())
    assert spies == []


def test_no_launch_gate_refuses_wrong_pins(job, spies):
    root = _no_launch_job(job)
    pins = dict(EXPECTED, tools=dict(EXPECTED["tools"], **{"database-create": {
        "driver_sha256": "f" * 64, "interpreter_path": xf.PYTHON}}))
    with pytest.raises(ds.DatabaseStageError, match="holder_verdict_mismatch"):
        ds.no_launch_quiescence(str(root), NO_LAUNCH, pins)


def _swap_core_at_the_gate_read(monkeypatch):
    """The gate's own core read returns other (valid) core bytes than the
    ones the aggregation hashed: the snapshot must be bound."""
    real = fz.read_bounded

    def read_bounded(path, limit):
        data = real(path, limit)
        if str(path).endswith(sj.CORE_NAME):
            core = json.loads(data)
            core["captured_utc_ns"] += 1
            data = sj._dumps(core)
        return data
    monkeypatch.setattr(fz, "read_bounded", read_bounded)


def test_finalize_binds_its_core_snapshot_to_the_aggregation(job, spies, monkeypatch):
    root = job()
    _swap_core_at_the_gate_read(monkeypatch)
    report = fz.finalize(str(root), STAGES, EXPECTED)
    assert (report["outcome"], report["reason"]) == ("NOT_FINALIZED", "gate_core_changed")
    assert spies == []


def test_writer_gate_binds_its_core_snapshot_to_the_aggregation(job, monkeypatch):
    root = job(upto="stop")
    _swap_core_at_the_gate_read(monkeypatch)
    with pytest.raises(fz.FinalizerError, match="^gate_core_changed$"):
        fz.writer_quiescence(str(root), PREFIX, EXPECTED, ("database-create",))
