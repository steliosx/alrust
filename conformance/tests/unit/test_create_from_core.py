"""Pure controls for executing workflow W3, part 1: the database-create
stage takes the database name's profile, run id and attempt only from the
/3 core that its own stage_begin bound, never from the caller.

A real StageJournal at index 0 on a private job root written by
test_tool_supervisor_pure._job (core run_id "123", run_attempt "1", profile
"scaffold"); create_database is replaced by a spy, so no SQL, Docker or
process is reached. Private temporary files only."""
import hashlib
import json
import os

import pytest

from alrust_conformance import database_stage as ds
from alrust_conformance import stage_journal as sj
from test_tool_supervisor_pure import CREATE, _job, drv

PASSED = {"port": 5432, "superuser": "postgres", "superuser_password": "postgres",
          "container_id": "a" * 12, "psql_path": "/usr/bin/psql",
          "docker_path": "/usr/bin/docker", "deadline_ns": 1000}


@pytest.fixture
def spy(monkeypatch):
    calls = []

    def create(job_root, journal, **kwargs):
        calls.append((job_root, kwargs))
        return os.path.join(job_root, "database", "url")
    monkeypatch.setattr(ds, "create_database", create)
    return calls


def closing(root):
    data = sj.read_bounded(sj.journal_path(str(root), 0, "database-create"), sj.MAX_JOURNAL)
    return json.loads(data.splitlines()[-1])["evidence"]["payload"]


def bound_sha(root):
    data = sj.read_bounded(sj.journal_path(str(root), 0, "database-create"), sj.MAX_JOURNAL)
    return json.loads(data.splitlines()[0])["evidence"]["payload"]["core_identity_sha256"]


def test_the_names_come_from_the_bound_core(tmp_path, monkeypatch, spy):
    root, digest = _job(tmp_path, monkeypatch)
    ds.run_create_stage(str(root), **PASSED)
    [(job_root, kwargs)] = spy
    assert job_root == str(root)
    assert kwargs == dict(PASSED, profile="scaffold", run_id="123", attempt="1")
    assert closing(root) == {"result": "COMPLETE"}
    assert bound_sha(root) == digest


def test_the_journal_exposes_the_sha_its_stage_begin_bound(tmp_path, monkeypatch):
    root, digest = _job(tmp_path, monkeypatch)
    journal = sj.StageJournal(str(root), 0, "database-create")
    assert journal.core_identity_sha256 is None
    with journal:
        assert journal.core_identity_sha256 == digest
    assert bound_sha(root) == digest


@pytest.mark.parametrize("name", sorted(ds.CORE_NAME_KWARGS))
def test_a_caller_supplied_name_part_is_refused(tmp_path, monkeypatch, spy, name):
    root, _ = _job(tmp_path, monkeypatch)
    with pytest.raises(ds.DatabaseStageError) as error:
        ds.run_create_stage(str(root), **dict(PASSED, **{name: "1"}))
    assert error.value.code == "create_names_from_caller" and spy == []
    assert closing(root) == {"result": "FAILED", "code": "create_names_from_caller"}


def test_a_legacy_core_is_refused(tmp_path, monkeypatch, spy):
    root, _ = _job(tmp_path, monkeypatch, schema=2)
    with pytest.raises(ds.DatabaseStageError) as error:
        ds.run_create_stage(str(root), **PASSED)
    assert error.value.code == "create_legacy_core" and spy == []
    assert closing(root) == {"result": "FAILED", "code": "create_legacy_core"}


def _after_begin(monkeypatch, action):
    real_enter = sj.StageJournal.__enter__

    def enter(self):
        result = real_enter(self)
        action(self._root)
        return result
    monkeypatch.setattr(sj.StageJournal, "__enter__", enter)


def test_a_core_changed_after_stage_begin_is_refused(tmp_path, monkeypatch, spy):
    # The core is rewritten (still a valid /3 core, a different run id)
    # between stage_begin and the name read: the bound sha refuses it.
    root, _ = _job(tmp_path, monkeypatch)
    path = os.path.join(str(root), sj.CORE_NAME)

    def rewrite(job_root):
        core = json.loads(open(path, "rb").read())
        core["run_id"] = "124"
        os.chmod(path, 0o600)
        with open(path, "wb") as f:
            f.write(json.dumps(core, sort_keys=True, separators=(",", ":")).encode())
    _after_begin(monkeypatch, rewrite)
    with pytest.raises(ds.DatabaseStageError) as error:
        ds.run_create_stage(str(root), **PASSED)
    assert error.value.code == "create_core_changed" and spy == []
    assert closing(root) == {"result": "FAILED", "code": "create_core_changed"}


def test_a_core_removed_after_stage_begin_is_refused(tmp_path, monkeypatch, spy):
    root, _ = _job(tmp_path, monkeypatch)
    _after_begin(monkeypatch, lambda job_root: os.unlink(os.path.join(job_root, sj.CORE_NAME)))
    with pytest.raises(ds.DatabaseStageError) as error:
        ds.run_create_stage(str(root), **PASSED)
    assert error.value.code == "create_core_unreadable" and spy == []
    assert closing(root) == {"result": "FAILED", "code": "create_core_unreadable"}


@pytest.mark.parametrize("bound", [None, "0" * 64])
def test_core_names_require_the_exact_bound_sha(tmp_path, monkeypatch, bound):
    root, _ = _job(tmp_path, monkeypatch)
    with pytest.raises(ds.DatabaseStageError) as error:
        ds._core_names(str(root), bound)
    assert error.value.code == "create_core_changed"


def test_core_names_refuse_bytes_that_do_not_validate(tmp_path, monkeypatch):
    # Matching sha, invalid core: create_core_invalid, never a name.
    root, _ = _job(tmp_path, monkeypatch)
    path = os.path.join(str(root), sj.CORE_NAME)
    data = b'{"schema":"alrust-hosted-core/3"}'
    os.chmod(path, 0o600)
    with open(path, "wb") as f:
        f.write(data)
    with pytest.raises(ds.DatabaseStageError) as error:
        ds._core_names(str(root), hashlib.sha256(data).hexdigest())
    assert error.value.code == "create_core_invalid"


def test_core_names_map_run_attempt_to_attempt(tmp_path, monkeypatch):
    root, digest = _job(tmp_path, monkeypatch)
    assert ds._core_names(str(root), digest) == {"profile": "scaffold", "run_id": "123",
                                                 "attempt": "1"}


@pytest.mark.parametrize("option", ["--profile", "--run-id", "--attempt"])
def test_the_driver_rejects_name_options_as_usage(capsys, monkeypatch, option):
    monkeypatch.setattr(ds, "run_create_stage", lambda *a, **k: pytest.fail("runner reached"))
    for name in drv.SECRET_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    assert drv.main(CREATE + [option, "1"]) == 64
    assert json.loads(capsys.readouterr().out) == {"result": "NOT_RUN", "reason": "usage"}
