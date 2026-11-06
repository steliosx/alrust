"""PR100 independent evidence controls. Synthetic receipts/journals only.

No subprocess/SQL/socket/Docker: all external adapters are refusing or fixed
in-process fakes. Explicit absent generation; tests never call ended_generation.
"""
import hashlib
import inspect
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from alrust_conformance import database_stage as ds
from alrust_conformance import finalizer as fz
from alrust_conformance import stage_journal as sj
from test_finalizer_orchestration import STAGES, build
import exit_fixtures as xf

PORT = 15432
CONTAINER = "0123456789abcdef0123"
IMAGE = "sha256:" + "a" * 64
GENERATION = (4194293, 333)


@pytest.fixture(autouse=True)
def forbid_processes_and_unseal(tmp_path, monkeypatch):
    # Refuse mixed checkout imports before any synthetic fixture is built.
    checkout = Path(__file__).resolve().parents[3]
    assert Path(ds.__file__).resolve().parents[2] == checkout
    assert Path(fz.__file__).resolve().parents[2] == checkout
    assert Path(sj.__file__).resolve().parents[2] == checkout
    assert Path(build.__code__.co_filename).resolve().parents[3] == checkout
    assert Path(xf.__file__).resolve().parents[3] == checkout
    assert not os.path.exists("/proc/%d" % GENERATION[0])

    def refuse(*args, **kwargs):
        pytest.fail("pure PR100 control attempted a subprocess")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    yield
    for export in tmp_path.glob("**/export"):
        if export.is_dir() and not export.is_symlink():
            export.chmod(0o700)
            for p in export.iterdir():
                if p.is_file() and not p.is_symlink():
                    p.chmod(0o600)


def mapping(monkeypatch, host_port):
    calls = []

    def fake(argv, stdin, env, timeout_s, limit):
        calls.append(argv)
        assert argv[0] == "/synthetic/docker-never-executed"
        value = ({"Image": IMAGE, "NetworkSettings": {"Ports": {
            "5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(host_port)}]}}}
                 if argv[1] == "inspect" else ["synthetic/postgres@" + IMAGE])
        return None, 0, json.dumps(value).encode(), b""

    monkeypatch.setattr(ds, "_bounded_run", fake)
    kwargs = {"port": PORT} if "port" in inspect.signature(ds.container_identity).parameters else {}
    return ds.container_identity("/synthetic/docker-never-executed", CONTAINER,
                                 deadline_ns=time.monotonic_ns() + 10**10, **kwargs)


def test_matching_mapping_positive(monkeypatch):
    value = mapping(monkeypatch, PORT)
    assert any(m["host_port"] == str(PORT) for m in value["port"])


def test_wrong_endpoint_mapping_is_refused(monkeypatch):
    with pytest.raises(ds.DatabaseStageError, match="container_port_mismatch"):
        mapping(monkeypatch, PORT + 1)


def test_container_refusal_precedes_any_sql(tmp_path, monkeypatch):
    calls = []
    cluster = {"server_version_num": 180006, "system_identifier": "7300000000000000001",
               "postmaster_start_time": "2026-10-03T01:02:03.456789Z",
               "version": "PostgreSQL 18.6 (synthetic)"}
    name = ds.database_name("scaffold", "123", "1")

    class FakePsql:
        def __init__(self, *args, **kwargs):
            pass

        def version(self):
            return "psql (PostgreSQL) 16.4 (synthetic)"

        def query(self, sql, database, *args):
            calls.append("query")

        def query_json(self, sql, database, *args):
            calls.append("query_json")
            if sql == ds.SQL_IDENTITY:
                return cluster
            if sql == ds.SQL_EXISTS:
                return {"database": False, "role": False}
            if sql == ds.SQL_ROLE_INFO:
                return {"oid": 16383, "login": True, "super": False, "createdb": False,
                        "createrole": False, "replication": False, "bypassrls": False}
            if sql == ds.SQL_DATABASE_INFO:
                return {"oid": 16384, "owner": name}
            pytest.fail("unexpected fake SQL operation")

    class Journal:
        def record(self, *args):
            pass

    def refuse_container(*args, **kwargs):
        raise ds.DatabaseStageError("synthetic_endpoint_mismatch")

    monkeypatch.setattr(ds, "Psql", FakePsql)
    monkeypatch.setattr(ds, "container_identity", refuse_container)
    with pytest.raises(ds.DatabaseStageError, match="synthetic_endpoint_mismatch"):
        ds.create_database(str(tmp_path), Journal(), profile="scaffold", run_id="123",
                           attempt="1", port=PORT, superuser="postgres",
                           superuser_password="synthetic-password-only",
                           container_id=CONTAINER, psql_path="/synthetic/psql-never-executed",
                           docker_path="/synthetic/docker-never-executed",
                           deadline_ns=time.monotonic_ns() + 10**10)
    assert calls == [], "PR100-ENDPOINT-R1: SQL adapter reached before container refusal"


def container_record():
    return {"container_id": CONTAINER, "image_id": IMAGE, "database_port": 5432,
            "repo_digests": ["synthetic/postgres@" + IMAGE],
            "port": [{"container": "5432/tcp", "host_ip": "127.0.0.1",
                      "host_port": "5432"}]}


def make(tmp_path, monkeypatch, extra=()):
    kwargs = {"container_stage": None} if "container_stage" in inspect.signature(build).parameters else {}
    # Explicit fixture API adaptation: retain original absent-container baseline,
    # then supply this control's own reviewed record only for positive/drop cases.
    return build(tmp_path, monkeypatch, generation=GENERATION, extra_records=extra, **kwargs)


def test_recorded_container_positive_seals(tmp_path, monkeypatch):
    root = make(tmp_path, monkeypatch,
                [("database-create", "database_container", container_record())])
    result = fz.finalize(str(root), STAGES, xf.EXPECTED)
    assert result["outcome"] == "SEALED"
    assert fz.verify_manifest(str(root / "export"), result["manifest_sha256"])


@pytest.mark.parametrize("malformed", [False, True])
def test_missing_or_malformed_container_never_seals(tmp_path, monkeypatch, malformed):
    extra = [("database-create", "database_container", {})] if malformed else []
    root = make(tmp_path, monkeypatch, extra)
    result = fz.finalize(str(root), STAGES, xf.EXPECTED)
    expected = "database_container_invalid" if malformed else "database_container_missing_or_repeated"
    assert (result["outcome"], result["reason"]) == ("NOT_FINALIZED", expected), "PR100-CONTAINER-R2: invalid container evidence sealed"
    assert not (root / "export" / "manifest.json").exists()


def test_database_drop_from_entrypoint_never_seals(tmp_path, monkeypatch):
    root = make(tmp_path, monkeypatch,
                [("database-create", "database_container", container_record())])
    aggregation = sj.aggregate(str(root), STAGES)
    assert aggregation["verdict"] == "VALID"
    records = fz._journal_records(str(root), aggregation)
    # Rebuild a real valid chain, placing exactly one drop payload at entrypoint.
    # Launch-confirm and exit proofs must bind the rewritten journal bytes.
    for name in STAGES:
        Path(sj.journal_path(str(root), STAGES.index(name), name)).unlink()
    previous = None
    for index, name in enumerate(STAGES):
        with sj.StageJournal(str(root), index, name, previous) as journal:
            for stage, kind, payload in records:
                if kind in ("stage_begin", "stage_complete"):
                    continue  # StageJournal generates fresh structural envelopes.
                target = "entrypoint" if kind == "database_drop" else stage
                if target != name:
                    continue
                if kind == "launch_handoff":
                    payload = dict(payload, launch_journal_sha256=hashlib.sha256(
                        Path(sj.journal_path(str(root), STAGES.index("launch"), "launch")).read_bytes()).hexdigest())
                if kind == "tool_exit_verdict":
                    selected = "database-create" if name == "database-create-exit" else "registry"
                    payload = xf.tool_verdict(str(root), selected, STAGES.index(selected))
                if kind == "entrypoint_exit_verdict":
                    payload = xf.entrypoint_verdict(str(root), STAGES.index("entrypoint"))
                journal.record(kind, payload)
        previous = name
    aggregation = sj.aggregate(str(root), STAGES)
    assert aggregation["verdict"] == "VALID"
    actual = fz._journal_records(str(root), aggregation)
    assert [(stage, kind) for stage, kind, payload in actual if kind == "database_drop"] == [
        ("entrypoint", "database_drop")]
    result = fz.finalize(str(root), STAGES, xf.EXPECTED)
    assert (result["outcome"], result["reason"]) == ("NOT_FINALIZED", "database_drop_misplaced"), "PR100-DROP-R3: wrong-stage cleanup record sealed"
    assert not (root / "export" / "manifest.json").exists()
