"""Pure regressions for the three PR #100 review findings (chatgpt-codex-connector
comments 4174199627, 4174199632, 4174199634 on d9ad6378):

1. database endpoint identity: the service container must publish 5432/tcp
   on the port every SQL call uses, checked before any SQL;
2. required container evidence: finalize() needs exactly one well-formed
   database_container record in database-create, binding that port;
3. cleanup-stage attribution: the database_drop verdict counts only from the
   database-drop stage.

docker is replaced by an in-process fake of _bounded_run, Psql by a recorder;
finalize() runs on synthetic journals with a synthetic ended generation
(absent PIDs). Forward-ported onto the R-EP stack, where finalize() also
takes the expected pins (P3a). No process, socket, container or database is used."""
import json
import os

import pytest

from alrust_conformance import database_stage as ds
from alrust_conformance import finalizer as fz
from exit_fixtures import EXPECTED
from test_finalizer_orchestration import STAGES, build

CONTAINER = "0123456789abcdef0123"
IMAGE = "sha256:" + "a" * 64
SYNTHETIC_SUPERVISOR = (4194293, 333)
FAR = 10 ** 18


def _fake_docker(monkeypatch, mapping):
    calls = []

    def run(argv, stdin, env, timeout, limit):
        calls.append(argv[1:3])
        if argv[1] == "inspect":
            out = {"Image": IMAGE, "NetworkSettings": {"Ports": {"5432/tcp": mapping}}}
        else:
            out = ["postgres@sha256:" + "b" * 64]
        return None, 0, json.dumps(out).encode(), b""
    monkeypatch.setattr(ds, "_bounded_run", run)
    return calls


# --- 1. database endpoint identity ------------------------------------------------------

@pytest.mark.parametrize("host_ip", ["0.0.0.0", "127.0.0.1"])
def test_container_publishing_the_sql_port_is_accepted(monkeypatch, host_ip):
    _fake_docker(monkeypatch, [{"HostIp": host_ip, "HostPort": "5432"}])
    identity = ds.container_identity("/usr/bin/docker", CONTAINER, FAR, 5432)
    assert identity["database_port"] == 5432
    assert identity["port"] == [{"container": "5432/tcp", "host_ip": host_ip,
                                 "host_port": "5432"}]


@pytest.mark.parametrize("mapping,port", [
    ([{"HostIp": "0.0.0.0", "HostPort": "5432"}], 5433),        # SQL goes elsewhere
    ([{"HostIp": "0.0.0.0", "HostPort": "15432"}], 5432),
    ([{"HostIp": "::", "HostPort": "5432"}], 5432),             # IPv6 only
    ([{"HostIp": "192.0.2.7", "HostPort": "5432"}], 5432),      # not reachable on loopback
    ([{"HostIp": "0.0.0.0", "HostPort": "5432"}], True),
    ([{"HostIp": "0.0.0.0", "HostPort": "5432"}], "5432"),
])
def test_container_not_publishing_the_sql_port_refuses(monkeypatch, mapping, port):
    _fake_docker(monkeypatch, mapping)
    with pytest.raises(ds.DatabaseStageError, match="container_port_mismatch"):
        ds.container_identity("/usr/bin/docker", CONTAINER, FAR, port)


@pytest.mark.parametrize("mapping", [
    [{"HostIp": "0.0.0.0", "HostPort": "5432"}] * 17,
    [{"HostIp": "0" * 46, "HostPort": "5432"}],
    [{"HostIp": "0.0.0.0", "HostPort": "5432/tcp"}],
    [],
])
def test_unbounded_or_malformed_docker_mapping_refuses(monkeypatch, mapping):
    _fake_docker(monkeypatch, mapping)
    with pytest.raises(ds.DatabaseStageError, match="docker_output_invalid"):
        ds.container_identity("/usr/bin/docker", CONTAINER, FAR, 5432)


class _Journal:
    def __init__(self):
        self.records = []

    def record(self, kind, payload):
        self.records.append(kind)


def test_create_checks_the_endpoint_before_any_sql(tmp_path, monkeypatch):
    _fake_docker(monkeypatch, [{"HostIp": "0.0.0.0", "HostPort": "15432"}])
    psql = []
    monkeypatch.setattr(ds, "Psql", lambda *a, **k: psql.append(k) or pytest.fail("SQL"))
    job = tmp_path / "job"
    job.mkdir(mode=0o700)
    journal = _Journal()
    with pytest.raises(ds.DatabaseStageError, match="container_port_mismatch"):
        ds.create_database(str(job), journal, profile="scaffold", run_id="1", attempt="1",
                           port=5432, superuser="postgres", superuser_password="postgres",
                           container_id=CONTAINER, psql_path="/usr/bin/psql",
                           docker_path="/usr/bin/docker", deadline_ns=FAR)
    assert psql == [] and journal.records == ["owned_resource"]


def test_create_records_the_container_before_connecting(tmp_path, monkeypatch):
    _fake_docker(monkeypatch, [{"HostIp": "0.0.0.0", "HostPort": "5432"}])
    journal = _Journal()

    class Stop(Exception):
        pass

    def psql(*a, **k):
        journal.records.append("psql")
        raise Stop()
    monkeypatch.setattr(ds, "Psql", psql)
    job = tmp_path / "job"
    job.mkdir(mode=0o700)
    with pytest.raises(Stop):
        ds.create_database(str(job), journal, profile="scaffold", run_id="1", attempt="1",
                           port=5432, superuser="postgres", superuser_password="postgres",
                           container_id=CONTAINER, psql_path="/usr/bin/psql",
                           docker_path="/usr/bin/docker", deadline_ns=FAR)
    assert journal.records == ["owned_resource", "database_container", "psql"]


# --- 2 and 3: the finalizer's database linkage ------------------------------------------

@pytest.fixture
def job(tmp_path, monkeypatch):
    if os.path.exists("/proc/%d" % SYNTHETIC_SUPERVISOR[0]):
        pytest.skip("synthetic ended PID is in use on this host")

    def make(**kw):
        kw.setdefault("generation", SYNTHETIC_SUPERVISOR)
        return build(tmp_path, monkeypatch, **kw)
    return make


def _refused(root, reason):
    report = fz.finalize(str(root), STAGES, EXPECTED)
    assert (report["outcome"], report["reason"]) == ("NOT_FINALIZED", reason)
    assert not (root / "export").exists()


def test_complete_database_evidence_seals(job):
    report = fz.finalize(str(job()), STAGES, EXPECTED)
    assert report["outcome"] == "SEALED", report


def test_missing_container_evidence_refuses(job):
    _refused(job(container_stage=None), "database_container_missing_or_repeated")


def test_container_evidence_from_another_stage_refuses(job):
    _refused(job(container_stage="launch"), "database_container_missing_or_repeated")


def test_repeated_container_evidence_refuses(job):
    extra = [("database-create", "database_container", {"container_id": CONTAINER})]
    _refused(job(extra_records=extra), "database_container_missing_or_repeated")


@pytest.mark.parametrize("change", [
    lambda c: c.pop("image_id"),
    lambda c: c.pop("database_port"),
    lambda c: c.update(extra=1),
    lambda c: c.update(container_id="NOT-HEX"),
    lambda c: c.update(image_id="sha256:short"),
    lambda c: c.update(repo_digests=["z", "a"]),                # not sorted
    lambda c: c.update(repo_digests=[1]),
    lambda c: c.update(port=[]),
    lambda c: c.update(port=[{"container": "5433/tcp", "host_ip": "0.0.0.0",
                              "host_port": "5432"}]),
    lambda c: c.update(port=[{"container": "5432/tcp", "host_ip": "0.0.0.0",
                              "host_port": 5432}]),
    lambda c: c.update(port=[{"container": "5432/tcp", "host_ip": "0.0.0.0",
                              "host_port": "5432"}] * 17),          # unbounded mapping list
    lambda c: c.update(port=[{"container": "5432/tcp", "host_ip": "0" * 46,
                              "host_port": "5432"}]),
    lambda c: c.update(port=[{"container": "5432/tcp", "host_ip": "0.0.0.0",
                              "host_port": "5432x"}]),
    lambda c: c.update(database_port="5432"),
    lambda c: c.update(database_port=True),
    lambda c: c.update(database_port=0),
    lambda c: c.update(database_port=65536),
])
def test_malformed_container_evidence_refuses(job, change):
    _refused(job(container_change=change), "database_container_invalid")


@pytest.mark.parametrize("change", [
    lambda c: c.update(database_port=5433),                      # SQL port not published
    lambda c: c.update(port=[{"container": "5432/tcp", "host_ip": "::",
                              "host_port": "5432"}]),
    lambda c: c.update(port=[{"container": "5432/tcp", "host_ip": "192.0.2.7",
                              "host_port": "5432"}]),
])
def test_container_not_binding_the_database_port_refuses(job, change):
    _refused(job(container_change=change), "database_endpoint_unbound")


@pytest.mark.parametrize("stage", ["stop", "registry", "database-create"])
def test_drop_verdict_from_another_stage_refuses(job, stage):
    _refused(job(drop_stage=stage), "database_drop_misplaced")
