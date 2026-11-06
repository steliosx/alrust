"""Pure controls for alrust_conformance.custody (W5b; W5B-R1, W5B-R2). Real
temporary job roots for the structural checks; the journal aggregation,
core gate, record reader and holder proof are replaced by stand-ins where
a control needs a specific outcome, so no process-classified fixture is
imported. No process, signal or network."""
import os

import pytest

from alrust_conformance import custody
from alrust_conformance import database_stage as ds
from alrust_conformance import finalizer as fz
from alrust_conformance import stage_journal as sj
from alrust_conformance import tool_exit as te

CREATE, EXIT, DROP = ds.CREATE_STAGE, te.exit_stage(ds.CREATE_STAGE), ds.DROP_STAGE


def root_with(tmp_path, journals=(), dirs=()):
    root = tmp_path / "job"
    (root / sj.JOURNAL_DIR).mkdir(parents=True)
    for index, name in enumerate(journals):
        (root / sj.JOURNAL_DIR / ("%02d-%s.jsonl" % (index, name))).write_text("{}\n")
    for d in dirs:
        (root / d).mkdir(parents=True)
    return root


@pytest.fixture
def proofs(monkeypatch):
    """Stand-ins for the reviewed proof functions; each test sets outcomes."""
    state = {"verdict": "VALID", "holder": None, "records": [], "core": None}

    def aggregate(job_root, stages):
        state["stages"] = stages
        return {"verdict": state["verdict"], "core_identity_sha256": "0" * 64, "stages": []}

    def gate_core(job_root, aggregation, expected):
        if state["core"]:
            raise fz.FinalizerError(state["core"])
        return b"core", {}

    def holder_proofs(job_root, aggregation, records, required, expected, core_bytes):
        state["holder_args"] = (required, expected, core_bytes)
        if state["holder"]:
            raise te.HolderError(state["holder"])

    monkeypatch.setattr(custody.sj, "aggregate", aggregate)
    monkeypatch.setattr(custody.fz, "_gate_core", gate_core)
    monkeypatch.setattr(custody.fz, "_journal_records", lambda root, agg: state["records"])
    monkeypatch.setattr(custody.te, "holder_proofs", holder_proofs)
    return state


PINS = {"entrypoint": {}, "tools": {"database-create": {}, "registry": {}}}


@pytest.mark.parametrize("journals", [(CREATE, EXIT), (CREATE, EXIT, DROP)])
def test_a_proven_holder_returns_the_recorded_directory(tmp_path, proofs, journals):
    root = root_with(tmp_path, journals)
    proofs["records"] = [(CREATE, "owned_resource", {"path": os.path.join(str(root), "database"),
                                                     "dev": 7, "ino": 9, "type": "dir"})]
    assert custody.holder_proven(str(root), PINS) == {"dev": 7, "ino": 9}
    assert proofs["stages"] == list(journals)
    assert proofs["holder_args"] == ((CREATE,), PINS["tools"], b"core")


def test_no_recorded_directory_returns_none(tmp_path, proofs):
    root = root_with(tmp_path, (CREATE, EXIT))
    assert custody.holder_proven(str(root), PINS) is None


@pytest.mark.parametrize("journals,code", [
    ((), "custody_journals_unexpected"), ((CREATE,), "custody_journals_unexpected"),
    ((CREATE, EXIT, DROP, "launch"), "custody_journals_unexpected"),
    ((EXIT, CREATE), "custody_journals_unexpected")])
def test_an_unexpected_journal_set_refuses(tmp_path, proofs, journals, code):
    root = root_with(tmp_path, journals)
    with pytest.raises(custody.CustodyError, match=code):
        custody.holder_proven(str(root), PINS)


def test_a_stray_journal_entry_refuses(tmp_path, proofs):
    root = root_with(tmp_path, (CREATE, EXIT))
    (root / sj.JOURNAL_DIR / "notes.txt").write_text("x")
    with pytest.raises(custody.CustodyError, match="custody_journals_unexpected"):
        custody.holder_proven(str(root), PINS)


def test_an_incomplete_prefix_refuses(tmp_path, proofs):
    root = root_with(tmp_path, (CREATE, EXIT))
    proofs["verdict"] = "INCOMPLETE"
    with pytest.raises(custody.CustodyError, match="custody_prefix_not_closed"):
        custody.holder_proven(str(root), PINS)


def test_a_failed_create_with_a_proven_holder_is_admitted(tmp_path, proofs):
    root = root_with(tmp_path, (CREATE, EXIT, DROP))
    proofs["verdict"] = "FAILED"
    assert custody.holder_proven(str(root), PINS) is None


def test_an_unproven_holder_refuses(tmp_path, proofs):
    root = root_with(tmp_path, (CREATE, EXIT))
    proofs["holder"] = "holder_exit_unproven"
    with pytest.raises(custody.CustodyError, match="holder_unproven"):
        custody.holder_proven(str(root), PINS)


def test_a_refused_core_refuses(tmp_path, proofs):
    root = root_with(tmp_path, (CREATE, EXIT))
    proofs["core"] = "gate_core_changed"
    with pytest.raises(custody.CustodyError, match="custody_core_refused"):
        custody.holder_proven(str(root), PINS)


@pytest.mark.parametrize("name", ["reference", "observer"])
def test_launch_evidence_refuses(tmp_path, proofs, name):
    root = root_with(tmp_path, (CREATE, EXIT), dirs=(name,))
    with pytest.raises(custody.CustodyError, match="launch_evidence_present"):
        custody.holder_proven(str(root), PINS)


@pytest.mark.parametrize("records", [
    [(CREATE, "owned_resource", {"path": "P", "dev": 1, "ino": 2, "type": "file"})],
    [(CREATE, "owned_resource", {"path": "P", "dev": 1, "ino": 2, "type": "dir"})] * 2,
    [(CREATE, "owned_resource", {"path": "P", "dev": "1", "ino": 2, "type": "dir"})]])
def test_a_malformed_directory_record_refuses(tmp_path, proofs, records):
    root = root_with(tmp_path, (CREATE, EXIT))
    path = os.path.join(str(root), "database")
    proofs["records"] = [(s, k, dict(p, path=path)) for s, k, p in records]
    with pytest.raises(custody.CustodyError, match="database_dir_record_invalid"):
        custody.holder_proven(str(root), PINS)


def test_a_record_from_another_stage_is_ignored(tmp_path, proofs):
    root = root_with(tmp_path, (CREATE, EXIT))
    path = os.path.join(str(root), "database")
    proofs["records"] = [(EXIT, "owned_resource", {"path": path, "dev": 1, "ino": 2, "type": "dir"})]
    assert custody.holder_proven(str(root), PINS) is None


def test_not_launched_admits_an_empty_journal_directory(tmp_path):
    root = root_with(tmp_path)
    (root / "core-identity.json").write_text("{}")
    custody.not_launched(str(root))


@pytest.mark.parametrize("make,code", [
    (lambda r: (r / sj.JOURNAL_DIR / "00-database-create.jsonl").write_text("{}"), "custody_journal_present"),
    (lambda r: (r / "tools").mkdir(), "custody_receipts_present"),
    (lambda r: (r / "database").mkdir(), "custody_database_present"),
    (lambda r: (r / "reference").mkdir(), "launch_evidence_present")])
def test_not_launched_refuses_any_trace_of_a_launch(tmp_path, make, code):
    root = root_with(tmp_path)
    make(root)
    with pytest.raises(custody.CustodyError, match=code):
        custody.not_launched(str(root))


def test_a_linked_journal_directory_refuses(tmp_path):
    root = tmp_path / "job"
    root.mkdir()
    real = tmp_path / "real"
    real.mkdir()
    os.symlink(real, root / sj.JOURNAL_DIR)
    with pytest.raises(custody.CustodyError, match="custody_not_directory"):
        custody.not_launched(str(root))


def test_the_gate_uses_the_reviewed_proof_functions():
    source = open(custody.__file__).read()
    for call in ("sj.aggregate(job_root, stages)", "fz._gate_core(job_root, aggregation, expected)",
                 "fz._journal_records(job_root, aggregation)",
                 "te.holder_proofs(job_root, aggregation, records, (ds.CREATE_STAGE,), expected[\"tools\"],"):
        assert call in source, call
    for forbidden in ("subprocess", "os.kill", "os.unlink", "os.remove", "shutil", "import signal"):
        assert forbidden not in source, forbidden
