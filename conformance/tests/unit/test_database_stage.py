"""Offline controls for the database stage library (part A).

A stub `psql` holds a tiny cluster state in a JSON file next to itself
(the stage runs psql under a cleared environment, so the stub cannot be
configured through variables). It answers exactly the tagged statements the
library sends, logs every argv and stdin it receives (for secret-exclusion
checks), and can inject one fault per operation: fail before the effect,
fail after the effect (a CREATE that succeeded but reported an error),
hang (a timeout), or print malformed output. A stub `docker` answers the
two inspect calls. No PostgreSQL server, network or container is used.
"""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sys

import pytest

import exit_fixtures as xf
from exit_fixtures import EXPECTED

from alrust_conformance import database_stage as ds
from alrust_conformance import stage_journal as sj

SUPERUSER_PASSWORD = "public-workflow-superuser-password"
CONTAINER = "0123456789abcdef0123"
IMAGE = "sha256:" + "a" * 64
CLUSTER = {"server_version_num": 180006, "system_identifier": "7300000000000000001",
           "postmaster_start_time": "2026-10-03T01:02:03.456789Z",
           "version": "PostgreSQL 18.6 (synthetic)"}

STUB_PSQL = r'''#!%(python)s
import json, os, re, sys, time
here = os.path.dirname(os.path.abspath(__file__))
state_path = os.path.join(here, "state.json")
state = json.load(open(state_path))
argv = sys.argv[1:]
if argv == ["--version"]:
    print("psql (PostgreSQL) 16.4 (synthetic)")
    sys.exit(0)
sql = sys.stdin.read()
with open(os.path.join(here, "received.log"), "a") as log:
    log.write(json.dumps({"argv": argv, "stdin": sql, "env": dict(os.environ)}) + "\n")
op = re.search(r"-- alrust:op=(\w+)", sql).group(1)
var = {}
for i, a in enumerate(argv):
    if a == "-v" and "=" in argv[i + 1]:
        k, v = argv[i + 1].split("=", 1)
        var[k] = v
fault = state.get("faults", {}).get(op)
def save():
    json.dump(state, open(state_path, "w"))
def out(value):
    print(json.dumps(value))
if fault == "fail":
    sys.stderr.write("ERROR: synthetic failure\n"); sys.exit(3)
if fault == "hang":
    time.sleep(120)
if fault == "garbage":
    print("not json"); sys.exit(0)
name = var.get("name")
roles, dbs = state["roles"], state["databases"]
if op == "identity":
    out(state["cluster"])
elif op == "exists":
    out({"database": name in dbs, "role": name in roles})
elif op == "create_role":
    m = re.search(r"PASSWORD '([^']*)'", sql)
    roles[name] = {"oid": state["next_oid"], "verifier": m.group(1)}
    state["next_oid"] += 1
elif op == "role_info":
    if name in roles:
        r = roles[name]
        out({"oid": r["oid"], "login": True, "super": state.get("role_super", False),
             "createdb": False, "createrole": False, "replication": False,
             "bypassrls": False})
elif op == "create_database":
    dbs[name] = {"oid": state["next_oid"], "owner": name}
    state["next_oid"] += 1
elif op == "database_info":
    if name in dbs:
        out(dbs[name])
elif op == "drop_database":
    dbs.pop(name, None)
elif op == "drop_role":
    if any(d["owner"] == name for d in dbs.values()):
        sys.stderr.write("ERROR: role owns a database\n"); sys.exit(3)
    if not state.get("drop_role_noop"):
        roles.pop(name, None)
elif op == "registry_keys":
    keys = sorted(state.get("keys", []))[:int(var["limit"])]
    out({"keys": keys})
elif op == "absence":
    out({"database_by_name": name in dbs,
         "database_by_oid": any(d["oid"] == int(var["database_oid"]) for d in dbs.values()),
         "role_by_name": name in roles,
         "role_by_oid": any(r["oid"] == int(var["role_oid"]) for r in roles.values())})
save()
if fault == "fail_after":
    sys.stderr.write("ERROR: synthetic failure after the effect\n"); sys.exit(3)
'''

STUB_DOCKER = r'''#!%(python)s
import json, sys
argv = sys.argv[1:]
if argv[:1] == ["inspect"]:
    print(json.dumps({"Image": "%(image)s", "NetworkSettings": {"Ports": {
        "5432/tcp": [{"HostIp": "0.0.0.0", "HostPort": "5432"}]}}}))
elif argv[:2] == ["image", "inspect"]:
    print(json.dumps(["postgres@sha256:%(digest)s"]))
else:
    sys.exit(2)
'''


@pytest.fixture
def env(tmp_path):
    job = tmp_path / "job"
    job.mkdir(mode=0o700)
    ref = "e" * 40
    sj.write_core_identity(str(job), profile="scaffold", event_head_sha="a" * 40,
                           checkout_sha="b" * 40, reference_sha=ref, reference_pin=ref,
                           lock_sha256={"build": "c" * 64}, lock_pins={"build": "c" * 64},
                           manifest_sha256="d" * 64, manifest_pin="d" * 64,
                           env={"GITHUB_REPOSITORY": "steliosx/alrust",
                                "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
                                "GITHUB_JOB": "reference", "ImageOS": "ubuntu24",
                                "ImageVersion": "20261001.1"},
                           **xf.core_pins())          # /3 (R-EP plan P3a)
    sj.prepare_journal_dir(str(job))
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "psql").write_text(STUB_PSQL % {"python": sys.executable})
    (stub / "docker").write_text(STUB_DOCKER % {"python": sys.executable, "image": IMAGE,
                                                "digest": "b" * 64})
    for name in ("psql", "docker"):
        (stub / name).chmod(0o755)
    state = {"cluster": dict(CLUSTER), "roles": {}, "databases": {}, "next_oid": 16384,
             "faults": {}}
    (stub / "state.json").write_text(json.dumps(state))
    return job, stub


def state(stub):
    return json.loads((stub / "state.json").read_text())


def set_state(stub, **changes):
    value = state(stub)
    value.update(changes)
    (stub / "state.json").write_text(json.dumps(value))


def kw(stub, deadline=60):
    import time
    return dict(port=5432, superuser="postgres", superuser_password=SUPERUSER_PASSWORD,
                psql_path=str(stub / "psql"),
                deadline_ns=time.monotonic_ns() + int(deadline * 1e9))


def run_create(job, stub, index=0, deadline=60, **overrides):
    args = dict(profile="customer-mapping", run_id="123", attempt="1",
                container_id=CONTAINER, docker_path=str(stub / "docker"))
    args.update(kw(stub, deadline))
    args.update(overrides)
    error = None
    try:
        with sj.StageJournal(str(job), index, ds.CREATE_STAGE) as journal:
            try:
                url = ds.create_database(str(job), journal, **args)
            except ds.DatabaseStageError as failure:
                journal.fail(failure.code)
                raise
    except ds.DatabaseStageError as failure:
        error, url = failure.code, None
    return url, error


CREATE_EXIT = "database-create-exit"
NO_LAUNCH = ["database-create", CREATE_EXIT]


def close_create_exit(job, index=1):
    """The database-create holder's exit stage, as run_tool_exit_stage would
    record it (synthetic ended P_T generation; exit_fixtures)."""
    with sj.StageJournal(str(job), index, CREATE_EXIT, ds.CREATE_STAGE) as journal:
        journal.record("tool_exit_verdict", xf.tool_verdict(job, ds.CREATE_STAGE, index - 1))


def create_bytes(job, index=0):
    return Path(sj.journal_path(str(job), index, ds.CREATE_STAGE)).read_bytes()


def records(data):
    return [json.loads(line)["evidence"] for line in data.splitlines()]


def run_drop(job, stub, index=1):
    verdicts = []
    with sj.StageJournal(str(job), index, ds.DROP_STAGE, ds.CREATE_STAGE) as journal:
        verdicts.append(ds.drop_database(str(job), journal,
                                         create_journal_bytes=create_bytes(job), **kw(stub)))
    drop = [r["payload"] for r in records(
        Path(sj.journal_path(str(job), index, ds.DROP_STAGE)).read_bytes())
        if r["kind"] == "database_drop"]
    assert len(drop) == 1
    return verdicts[0], drop[0]


# --- name -------------------------------------------------------------------

def test_name_follows_the_contract():
    assert ds.database_name("customer-mapping", "123", "1") == "alrust_customer_mapping_123_1"
    longest = ds.database_name("p" * 31, "9" * 20, "999")
    assert len(longest) == 63


@pytest.mark.parametrize("args", [("p" * 32, "1", "1"), ("x", "9" * 21, "1"),
                                  ("x", "1", "1000"), ("X", "1", "1"), ("x", "01", "1"),
                                  ("x", "1", "0"), ("x_", "", "1"), ("-x", "1", "1"),
                                  (1, "1", "1")])
def test_name_refuses_rather_than_truncates(args):
    with pytest.raises(ds.DatabaseStageError):
        ds.database_name(*args)


# --- SCRAM (RFC 7677 section 3 example) -------------------------------------

def test_scram_keys_reproduce_the_rfc7677_exchange():
    salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
    client_key, stored_key, server_key = ds.scram_keys(b"pencil", salt)
    auth = (b"n=user,r=rOprNGfwEbeRWgbNEkqO,"
            b"r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,"
            b"s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,"
            b"c=biws,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0")
    signature = hmac.new(stored_key, auth, "sha256").digest()
    proof = bytes(a ^ b for a, b in zip(client_key, signature))
    assert base64.b64encode(proof) == b"dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ="
    assert base64.b64encode(hmac.new(server_key, auth, "sha256").digest()) == \
        b"6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="


def test_verifier_has_postgresql_format_and_no_password():
    verifier = ds.scram_verifier(b"x" * 64, salt=b"s" * 16)
    assert re.fullmatch(r"SCRAM-SHA-256\$4096:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+",
                        verifier)
    assert "x" * 16 not in verifier
    with pytest.raises(ds.DatabaseStageError):
        ds.scram_verifier(b"short")


# --- create -----------------------------------------------------------------

def test_create_records_every_object_and_keeps_the_password_private(env):
    job, stub = env
    url, error = run_create(job, stub)
    assert error is None
    name = "alrust_customer_mapping_123_1"
    text = Path(url).read_text()
    password = re.fullmatch(r"postgresql://%s:([0-9a-f]{64})@127\.0\.0\.1:5432/%s\n"
                            % (name, name), text)[1]
    assert os.stat(url).st_mode & 0o777 == 0o600
    data = create_bytes(job)
    kinds = [r["kind"] for r in records(data)]
    # PR #100 review: the container's published port is checked before any SQL.
    assert kinds == ["stage_begin", "owned_resource", "database_container",
                     "database_role_attempted", "database_role_created",
                     "database_create_attempted", "database_created", "stage_complete"]
    created = [r["payload"] for r in records(data) if r["kind"] == "database_created"][0]
    current = state(stub)
    assert created == {"name": name, "oid": current["databases"][name]["oid"], "role": name,
                       "role_oid": current["roles"][name]["oid"],
                       "system_identifier": CLUSTER["system_identifier"],
                       "postmaster_start_time": CLUSTER["postmaster_start_time"],
                       "server_version_num": CLUSTER["server_version_num"],
                       "version": CLUSTER["version"],
                       "psql": {"path": str(stub / "psql"),
                                "version": "psql (PostgreSQL) 16.4 (synthetic)"}}
    container = [r["payload"] for r in records(data) if r["kind"] == "database_container"][0]
    assert container["image_id"] == IMAGE and container["container_id"] == CONTAINER
    assert container["database_port"] == 5432 and container["port"] == [
        {"container": "5432/tcp", "host_ip": "0.0.0.0", "host_port": "5432"}]
    # The server received only the SCRAM verifier, and the password appears
    # nowhere outside the URL file: not in argv, stdin, the child
    # environment, or the journal.
    received = (stub / "received.log").read_text()
    assert password not in received and password not in data.decode()
    assert current["roles"][name]["verifier"].startswith("SCRAM-SHA-256$4096:")
    for line in received.splitlines():
        entry = json.loads(line)
        assert set(entry["env"]) <= {"PATH", "LANG", "PGPASSFILE", "PGCONNECT_TIMEOUT",
                                     "PGOPTIONS", "LC_CTYPE"}
        assert SUPERUSER_PASSWORD not in " ".join(entry["argv"])
    assert sj.aggregate(str(job), [ds.CREATE_STAGE])["verdict"] == "VALID"


def test_existing_name_is_refused_before_anything_is_attempted(env):
    job, stub = env
    set_state(stub, roles={"alrust_customer_mapping_123_1": {"oid": 1, "verifier": ""}})
    url, error = run_create(job, stub)
    assert error == "name_already_exists"
    kinds = [r["kind"] for r in records(create_bytes(job))]
    assert "database_role_attempted" not in kinds


@pytest.mark.parametrize("version", [160004, 180005, 180007, 190000])
def test_unsupported_server_version_is_refused(env, version):
    job, stub = env
    set_state(stub, cluster=dict(CLUSTER, server_version_num=version))
    assert run_create(job, stub)[1] == "server_version_unsupported"


def test_role_with_extra_attributes_is_refused(env):
    job, stub = env
    set_state(stub, role_super=True)
    assert run_create(job, stub)[1] == "role_attributes_invalid"


# Partial allocation states (DBC023-R2).
@pytest.mark.parametrize("op,fault,role_state,db_state", [
    ("create_role", "fail", "UNKNOWN", "NONE"),          # error after sending
    ("create_role", "fail_after", "UNKNOWN", "NONE"),    # created, then an error
    ("role_info", "garbage", "UNKNOWN", "NONE"),         # read-back failed
    ("create_database", "fail_after", "RECORDED", "UNKNOWN"),
    ("database_info", "fail", "RECORDED", "UNKNOWN"),
])
def test_partial_allocation_states(env, op, fault, role_state, db_state):
    job, stub = env
    set_state(stub, faults={op: fault})
    url, error = run_create(job, stub)
    assert url is None and error is not None
    role, db, _, _ = ds.allocation_states(create_bytes(job))
    assert (role, db) == (role_state, db_state)
    assert sj.aggregate(str(job), [ds.CREATE_STAGE])["verdict"] == "FAILED"


def test_create_timeout_is_an_unknown_outcome(env):
    job, stub = env
    set_state(stub, faults={"create_role": "hang"})
    url, error = run_create(job, stub, deadline=2)
    assert error == "psql_timeout"
    assert ds.allocation_states(create_bytes(job))[:2] == ("UNKNOWN", "NONE")


def test_container_failure_refuses_before_any_sql_or_allocation(env):
    # The service container is proved before any SQL (PR #100 review,
    # comment 4174199627), so a docker failure allocates nothing. Faults
    # after allocation are test_partial_allocation_states.
    job, stub = env
    (stub / "docker").write_text("#!/bin/sh\nexit 1\n")
    url, error = run_create(job, stub)
    assert url is None and error == "docker_failed"
    assert not (stub / "received.log").exists()
    assert ds.allocation_states(create_bytes(job))[:2] == ("NONE", "NONE")
    assert sj.aggregate(str(job), [ds.CREATE_STAGE])["verdict"] == "FAILED"


# --- drop -------------------------------------------------------------------

def test_drop_complete_after_a_full_create(env):
    job, stub = env
    run_create(job, stub)
    verdict, record = run_drop(job, stub)
    assert verdict == "COMPLETE" and record["reason"] == "dropped_and_verified_absent"
    created = [r["payload"] for r in records(create_bytes(job))
               if r["kind"] == "database_created"][0]
    for key in ("oid", "role", "role_oid", "system_identifier", "postmaster_start_time"):
        assert record[key] == created[key]
    assert record["database"] == created["name"]
    assert state(stub)["roles"] == {} and state(stub)["databases"] == {}


def test_drop_runner_gates_before_any_sql(env):
    # launched=True with a prefix that has launch but no registry: the drop
    # routes to the failed-entrypoint gate, which refuses this prefix before
    # any SQL. The stage then closes FAILED with the code.
    job, stub = env
    run_create(job, stub)
    close_create_exit(job)
    with sj.StageJournal(str(job), 2, "launch", CREATE_EXIT):
        pass
    before = (stub / "received.log").read_text()
    prefix = NO_LAUNCH + ["launch"]
    with pytest.raises(ds.DatabaseStageError, match="failed_gate_prefix_invalid"):
        ds.run_drop_stage(str(job), prefix, launched=True, expected=EXPECTED, **kw(stub))
    assert (stub / "received.log").read_text() == before
    assert state(stub)["databases"] and state(stub)["roles"]
    last = records(Path(sj.journal_path(str(job), 3, ds.DROP_STAGE)).read_bytes())[-1]
    assert last["payload"] == {"result": "FAILED", "code": "failed_gate_prefix_invalid"}


def test_drop_runner_refuses_a_flag_that_disagrees_with_the_evidence(env):
    # P3a: launched=True with no launch in the prefix cannot select the gate.
    job, stub = env
    run_create(job, stub)
    close_create_exit(job)
    before = (stub / "received.log").read_text()
    with pytest.raises(ds.DatabaseStageError, match="drop_path_mismatch"):
        ds.run_drop_stage(str(job), NO_LAUNCH, launched=True, expected=EXPECTED, **kw(stub))
    assert (stub / "received.log").read_text() == before
    assert not Path(sj.journal_path(str(job), 2, ds.DROP_STAGE)).exists()


def test_drop_role_only_when_no_database_was_attempted(env, monkeypatch):
    # The stage stops after the role record and BEFORE the database attempt
    # record, so the database state is NONE: a genuine role-only cleanup.
    job, stub = env
    real = sj.StageJournal.record

    def record(self, kind, payload):
        if kind == "database_create_attempted":
            raise ds.DatabaseStageError("synthetic_stop_before_database")
        return real(self, kind, payload)
    monkeypatch.setattr(sj.StageJournal, "record", record)
    url, error = run_create(job, stub)
    monkeypatch.setattr(sj.StageJournal, "record", real)
    assert error == "synthetic_stop_before_database"
    assert ds.allocation_states(create_bytes(job))[:2] == ("RECORDED", "NONE")
    verdict, record = run_drop(job, stub)
    assert (verdict, record["reason"]) == ("ROLE_ONLY_COMPLETE", "role_dropped_no_database")
    assert record["database"] is None and record["oid"] is None
    assert state(stub)["roles"] == {}


def test_drop_unknown_role_drops_nothing(env):
    job, stub = env
    set_state(stub, faults={"create_role": "fail_after"})
    run_create(job, stub)
    set_state(stub, faults={})
    verdict, record = run_drop(job, stub)
    assert (verdict, record["reason"]) == ("UNPROVEN", "role_outcome_unknown")
    assert state(stub)["roles"]          # never adopted by name


def test_drop_unknown_database_present_drops_nothing(env):
    job, stub = env
    set_state(stub, faults={"create_database": "fail_after"})
    run_create(job, stub)
    set_state(stub, faults={})
    verdict, record = run_drop(job, stub)
    assert (verdict, record["reason"]) == ("UNPROVEN", "database_outcome_unknown_present")
    assert state(stub)["databases"] and state(stub)["roles"]


def test_drop_unknown_database_absent_drops_role_but_stays_unproven(env):
    job, stub = env
    set_state(stub, faults={"create_database": "fail"})   # attempt recorded, no effect
    run_create(job, stub)
    set_state(stub, faults={})
    verdict, record = run_drop(job, stub)
    assert (verdict, record["reason"]) == ("UNPROVEN", "role_dropped_database_outcome_unknown")
    assert state(stub)["roles"] == {}


def test_drop_refuses_another_cluster(env):
    job, stub = env
    run_create(job, stub)
    set_state(stub, cluster=dict(CLUSTER, postmaster_start_time="2026-10-03T09:00:00.000000Z"))
    verdict, record = run_drop(job, stub)
    assert (verdict, record["reason"]) == ("UNPROVEN", "cluster_mismatch")
    assert state(stub)["databases"]


def test_drop_refuses_a_replaced_database(env):
    job, stub = env
    run_create(job, stub)
    current = state(stub)
    name = next(iter(current["databases"]))
    current["databases"][name]["oid"] += 1000
    (stub / "state.json").write_text(json.dumps(current))
    verdict, record = run_drop(job, stub)
    assert (verdict, record["reason"]) == ("UNPROVEN", "database_identity_mismatch")
    assert state(stub)["databases"]


def test_drop_failed_absence_is_unproven(env):
    job, stub = env
    run_create(job, stub)
    set_state(stub, drop_role_noop=True)
    verdict, record = run_drop(job, stub)
    assert (verdict, record["reason"]) == ("UNPROVEN", "still_present")


def test_drop_with_nothing_attempted(env):
    job, stub = env
    set_state(stub, faults={"identity": "fail"})
    run_create(job, stub)
    set_state(stub, faults={})
    verdict, record = run_drop(job, stub)
    assert verdict == "NONE_ALLOCATED"


def test_drop_refuses_an_unclosed_create_journal(env):
    job, stub = env
    run_create(job, stub)
    data = create_bytes(job)
    unclosed = b"\n".join(data.splitlines()[:-1]) + b"\n"
    with pytest.raises(ds.DatabaseStageError, match="create_journal_not_closed"):
        ds.allocation_states(unclosed)


def test_oversized_and_malformed_output_are_refused(env):
    job, stub = env
    set_state(stub, faults={"identity": "garbage"})
    assert run_create(job, stub)[1] == "psql_output_invalid"


# --- DB61-R1: bounded draining; overflow and timeout refuse, never truncate ---

def raw_psql(tmp_path, body):
    directory = tmp_path / "database"
    directory.mkdir(mode=0o700)
    stub = tmp_path / "psql-raw"
    stub.write_text("#!%s\nimport os, sys, time\nsys.stdin.read()\n%s\n" % (sys.executable, body))
    stub.chmod(0o755)
    import time as _time
    return directory, ds.Psql(str(stub), port=5432, user="postgres",
                              password="SYNTHETIC-public-password",
                              database_dir=str(directory),
                              deadline_ns=_time.monotonic_ns() + 3_000_000_000)


def evidence(directory):
    return [p for p in directory.iterdir() if p.name.startswith("psql-stderr-")]


def test_stderr_over_limit_refuses_with_bounded_evidence(tmp_path):
    # Codex's control from 5425c1b8: 1 MiB stderr plus valid output.
    directory, client = raw_psql(tmp_path, "sys.stderr.write('x' * 1048576)\nprint('{}')")
    with pytest.raises(ds.DatabaseStageError, match="psql_output_too_large"):
        client.query_json("-- SYNTHETIC\nSELECT 1;", "postgres")
    [kept] = evidence(directory)
    assert kept.stat().st_size == ds.MAX_OUTPUT


def test_stdout_over_limit_refuses(tmp_path):
    _, client = raw_psql(tmp_path, "sys.stdout.write('{' + ' ' * 1048576 + '}')")
    with pytest.raises(ds.DatabaseStageError, match="psql_output_too_large"):
        client.query("-- SYNTHETIC\nSELECT 1;", "postgres")


@pytest.mark.parametrize("size,accepted", [(65536, True), (65537, False)])
def test_stderr_bound_is_exact(tmp_path, size, accepted):
    directory, client = raw_psql(
        tmp_path, "sys.stderr.write('e' * %d)\nsys.stderr.flush()\nprint('{}')" % size)
    if accepted:
        assert client.query_json("-- SYNTHETIC\nSELECT 1;", "postgres") == {}
    else:
        with pytest.raises(ds.DatabaseStageError, match="psql_output_too_large"):
            client.query_json("-- SYNTHETIC\nSELECT 1;", "postgres")
    [kept] = evidence(directory)
    assert kept.stat().st_size == min(size, ds.MAX_OUTPUT)


def test_hung_child_is_stopped_and_reaped(tmp_path):
    pid_file = tmp_path / "child.pid"
    _, client = raw_psql(tmp_path, "open(%r, 'w').write(str(os.getpid()))\ntime.sleep(60)"
                         % str(pid_file))
    import time as _time
    started = _time.monotonic()
    with pytest.raises(ds.DatabaseStageError, match="psql_timeout"):
        client.query("-- SYNTHETIC\nSELECT 1;", "postgres")
    assert _time.monotonic() - started < 10            # stopped at the deadline
    pid = int(pid_file.read_text())
    assert not os.path.exists("/proc/%d" % pid)       # killed AND reaped


def test_docker_output_over_limit_refuses(tmp_path):
    stub = tmp_path / "docker-raw"
    stub.write_text("#!%s\nimport sys\nsys.stdout.write('[' + ' ' * 1048576 + ']')\n"
                    % sys.executable)
    stub.chmod(0o755)
    import time as _time
    with pytest.raises(ds.DatabaseStageError, match="docker_output_too_large"):
        ds.container_identity(str(stub), CONTAINER, _time.monotonic_ns() + 3_000_000_000, 5432)


# --- DBA2-R2: stdin is fed under the same deadline ------------------------------

def test_stdin_feed_obeys_call_deadline(tmp_path):
    # Codex's control from f44f8e40: a child that never reads its stdin.
    directory = tmp_path / "database"
    directory.mkdir(mode=0o700)
    pidfile = tmp_path / "stub.pid"
    stub = tmp_path / "psql-never-reads"
    stub.write_text("#!" + sys.executable + "\nimport os,time\n"
                    + "open(" + repr(str(pidfile)) + ", 'w').write(str(os.getpid()))\n"
                    + "time.sleep(2)\n")
    stub.chmod(0o755)
    import time as _time
    client = ds.Psql(str(stub), port=5432, user="postgres",
                     password="SYNTHETIC-public-password", database_dir=str(directory),
                     deadline_ns=_time.monotonic_ns() + 200_000_000)
    start = _time.monotonic()
    with pytest.raises(ds.DatabaseStageError, match="psql_timeout"):
        client.query("-- SYNTHETIC\n" + " " * (128 * 1024), "postgres")
    elapsed = _time.monotonic() - start
    pid = int(pidfile.read_text())
    assert not os.path.exists("/proc/%d" % pid), "owned child not reaped"
    assert elapsed < 1.0, "stdin feeding escaped the call deadline"


def test_large_stdin_reaches_a_reading_child(tmp_path):
    # 128 KiB is more than a pipe buffer: it must be fed while draining.
    directory = tmp_path / "database"
    directory.mkdir(mode=0o700)
    stub = tmp_path / "psql-reads"
    stub.write_text("#!%s\nimport sys, json\ndata = sys.stdin.read()\n"
                    "print(json.dumps({'received': len(data)}))\n" % sys.executable)
    stub.chmod(0o755)
    import time as _time
    client = ds.Psql(str(stub), port=5432, user="postgres",
                     password="SYNTHETIC-public-password", database_dir=str(directory),
                     deadline_ns=_time.monotonic_ns() + 5_000_000_000)
    sql = "-- SYNTHETIC\n" + " " * (128 * 1024)
    assert client.query_json(sql, "postgres") == {"received": len(sql)}


def test_oversized_input_is_refused_before_spawning(tmp_path):
    pidfile = tmp_path / "spawned"
    directory = tmp_path / "database"
    directory.mkdir(mode=0o700)
    stub = tmp_path / "psql-spawn-marker"
    stub.write_text("#!%s\nopen(%r, 'w').close()\n" % (sys.executable, str(pidfile)))
    stub.chmod(0o755)
    import time as _time
    client = ds.Psql(str(stub), port=5432, user="postgres",
                     password="SYNTHETIC-public-password", database_dir=str(directory),
                     deadline_ns=_time.monotonic_ns() + 5_000_000_000)
    with pytest.raises(ds.DatabaseStageError, match="psql_input_too_large"):
        client.query("-- SYNTHETIC\n" + " " * ds.MAX_INPUT, "postgres")
    assert not pidfile.exists()


@pytest.mark.parametrize("size", [10, 256 * 1024])
def test_child_exiting_without_reading_is_a_clean_failure(tmp_path, size):
    directory = tmp_path / "database"
    directory.mkdir(mode=0o700)
    stub = tmp_path / "psql-exits"
    stub.write_text("#!%s\nimport sys\nsys.exit(3)\n" % sys.executable)
    stub.chmod(0o755)
    import time as _time
    client = ds.Psql(str(stub), port=5432, user="postgres",
                     password="SYNTHETIC-public-password", database_dir=str(directory),
                     deadline_ns=_time.monotonic_ns() + 5_000_000_000)
    with pytest.raises(ds.DatabaseStageError, match="psql_failed"):
        client.query("-- SYNTHETIC\n" + " " * size, "postgres")

# --- part B: registry stage ---------------------------------------------------

SECRET_KEY = "synthetic-signing-" + "s" * 40


def run_registry(job, stub, other=(), index=2):
    key_file = job / "secret-key"
    if not key_file.exists():
        key_file.write_text(SECRET_KEY + "\n")
        key_file.chmod(0o600)
    # P3c: the binding the registry stage derives from the gate's evidence,
    # here from this job's own create record and database directory.
    created = [r["payload"] for r in records(create_bytes(job)) if r["kind"] == "database_created"]
    info = os.stat(job / "database")
    binding = {"role": created[0]["role"], "database_dir": {"dev": info.st_dev,
                                                             "ino": info.st_ino},
               "core_sha256": hashlib.sha256((job / sj.CORE_NAME).read_bytes()).hexdigest()}
    with sj.StageJournal(str(job), index, "registry", ds.CREATE_STAGE if index == 1 else
                         "launch") as journal:
        return ds.build_registry(str(job), journal,
                                 secret_key_path=str(job / "secret-key"),
                                 other_secrets=list(other), port=5432,
                                 psql_path=str(stub / "psql"), binding=binding,
                                 deadline_ns=__import__("time").monotonic_ns() + int(60e9))


def registry_job(env):
    job, stub = env
    run_create(job, stub)
    # A stand-in for the launch stage between create and registry.
    with sj.StageJournal(str(job), 1, "launch", ds.CREATE_STAGE):
        pass
    return job, stub


def test_registry_holds_every_secret_in_the_finalizer_format(env):
    from alrust_conformance import finalizer as fz
    job, stub = registry_job(env)
    set_state(stub, keys=["k" * 40, "j" * 40])
    path = run_registry(job, stub, other=["provisioner-role-key-" + "r" * 20])
    data = Path(path).read_bytes()
    record = [r["payload"] for r in records(
        Path(sj.journal_path(str(job), 2, "registry")).read_bytes())
        if r["kind"] == "credential_registry"][0]
    assert record["sha256"] == hashlib.sha256(data).hexdigest()
    secrets = fz.load_registry(str(job), record["sha256"])
    url = Path(job / "database" / "url").read_text().strip()
    password = re.search(r":([0-9a-f]{64})@", url)[1]
    assert set(s.decode() for s in secrets) == {"j" * 40, "k" * 40,
                                                 "provisioner-role-key-" + "r" * 20,
                                                 SECRET_KEY, url, password}
    assert os.stat(path).st_mode & 0o777 == 0o600
    # The job role, not the superuser, enumerated the keys.
    last = json.loads((stub / "received.log").read_text().splitlines()[-1])
    assert "-U" in last["argv"] and last["argv"][last["argv"].index("-U") + 1] \
        == "alrust_customer_mapping_123_1"


def test_registry_runner_gates_before_anything_is_read(env, monkeypatch):
    # The prefix lacks launch-confirm and stop: writer_quiescence refuses
    # before the registry journal opens. No private file is opened, no key
    # is enumerated, no registry is written (DBC023-N3).
    job, stub = registry_job(env)
    (job / "secret-key").write_text(SECRET_KEY)
    (job / "secret-key").chmod(0o600)
    before = (stub / "received.log").read_text()
    opened = []
    real_open = os.open

    def spy(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(ds.os, "open", spy)
    with pytest.raises(ds.DatabaseStageError, match="gate_stages_invalid"):
        ds.run_registry_stage(str(job), [ds.CREATE_STAGE, "launch"], expected=EXPECTED,
                              secret_key_path=str(job / "secret-key"), other_secrets=[],
                              port=5432, psql_path=str(stub / "psql"),
                              deadline_ns=__import__("time").monotonic_ns() + int(60e9))
    monkeypatch.undo()
    assert (stub / "received.log").read_text() == before
    assert not any(p.endswith("/url") or p.endswith("secret-key") for p in opened)
    assert not (job / "credential-registry.json").exists()
    last = records(Path(sj.journal_path(str(job), 2, "registry")).read_bytes())[-1]
    assert last["payload"] == {"result": "FAILED", "code": "gate_stages_invalid"}


def test_registry_refuses_rather_than_truncates(env):
    from alrust_conformance import finalizer as fz
    job, stub = registry_job(env)
    set_state(stub, keys=["k%039d" % i for i in range(fz.MAX_SECRETS)])
    with pytest.raises(ds.DatabaseStageError, match="registry_incomplete"):
        run_registry(job, stub)
    assert not (job / "credential-registry.json").exists()


@pytest.mark.parametrize("keys,other", [(["short"], []), ([], ["short"]), ([1], [])])
def test_registry_refuses_invalid_secrets(env, keys, other):
    job, stub = registry_job(env)
    set_state(stub, keys=keys)
    with pytest.raises(ds.DatabaseStageError):
        run_registry(job, stub, other=other)
    assert not (job / "credential-registry.json").exists()


# --- part B: no-launch gate ---------------------------------------------------

def failed_create(env):
    job, stub = env
    set_state(stub, faults={"create_database": "fail"})
    run_create(job, stub)
    set_state(stub, faults={})
    close_create_exit(job)
    return job, stub


def test_no_launch_gate_allows_cleanup_of_a_failed_allocation(env):
    job, stub = failed_create(env)
    with pytest.raises(ds.DatabaseStageError, match="drop_unproven"):
        ds.run_drop_stage(str(job), NO_LAUNCH, launched=False, expected=EXPECTED, **kw(stub))
    assert state(stub)["roles"] == {}                  # the recorded role was dropped
    recs = records(Path(sj.journal_path(str(job), 2, ds.DROP_STAGE)).read_bytes())
    [drop] = [r["payload"] for r in recs if r["kind"] == "database_drop"]
    assert (drop["verdict"], drop["reason"]) == ("UNPROVEN",
                                                 "role_dropped_database_outcome_unknown")
    assert recs[-1]["payload"] == {"result": "FAILED", "code": "drop_unproven"}


def test_no_launch_role_only_closes_failed_but_records_the_cleanup(env, monkeypatch):
    job, stub = env
    real = sj.StageJournal.record

    def record(self, kind, payload):
        if kind == "database_create_attempted":
            raise ds.DatabaseStageError("synthetic_stop_before_database")
        return real(self, kind, payload)
    monkeypatch.setattr(sj.StageJournal, "record", record)
    run_create(job, stub)
    monkeypatch.setattr(sj.StageJournal, "record", real)
    close_create_exit(job)
    with pytest.raises(ds.DatabaseStageError, match="drop_role_only_complete"):
        ds.run_drop_stage(str(job), NO_LAUNCH, launched=False, expected=EXPECTED, **kw(stub))
    assert state(stub)["roles"] == {}


@pytest.mark.parametrize("evidence", ["reference", "observer", "launch_journal"])
def test_no_launch_gate_refuses_any_launch_evidence(env, evidence):
    job, stub = failed_create(env)
    if evidence == "launch_journal":
        Path(sj.journal_path(str(job), 2, "launch")).write_text("")
    else:
        (job / evidence).mkdir(mode=0o700)
    before = (stub / "received.log").read_text()
    with pytest.raises(ds.DatabaseStageError):
        ds.run_drop_stage(str(job), NO_LAUNCH, launched=False, expected=EXPECTED, **kw(stub))
    assert (stub / "received.log").read_text() == before
    assert state(stub)["roles"]                        # nothing dropped


def test_no_launch_gate_refuses_an_unclosed_create_journal(env):
    job, stub = env
    run_create(job, stub)
    close_create_exit(job)
    path = Path(sj.journal_path(str(job), 0, ds.CREATE_STAGE))
    data = path.read_bytes()
    path.chmod(0o600)
    path.write_bytes(b"\n".join(data.splitlines()[:-1]) + b"\n")
    with pytest.raises(ds.DatabaseStageError, match="no_launch_prefix_not_closed"):
        ds.no_launch_quiescence(str(job), NO_LAUNCH, EXPECTED)


def test_registry_runner_end_to_end_through_the_real_writer_gate(env, tmp_path, monkeypatch):
    # A quiescent prefix (database-create .. stop) from the finalizer's own
    # fixture: VALID journals, an ended supervisor generation, a linked stop
    # verdict and launch-confirm hand-off. The runner gates with
    # writer_quiescence BEFORE opening the registry journal, then builds it.
    from test_finalizer_orchestration import build, STAGES
    _, stub = env
    (tmp_path / "composed").mkdir()
    job = build(tmp_path / "composed", monkeypatch, upto="stop",
                inventory=["provisioned-role-key-" + "r" * 24])
    (job / fz_registry_name()).unlink()          # the fixture's own registry
    prefix = STAGES[:STAGES.index("stop") + 1]
    (job / "database").mkdir(mode=0o700, exist_ok=True)  # build() made it (P3c)
    password = "f" * 64
    url = "postgresql://alrust_scaffold_123_1:%s@127.0.0.1:5432/alrust_scaffold_123_1" % password
    (job / "database" / "url").write_text(url + "\n")
    (job / "database" / "url").chmod(0o600)
    (job / "secret-key").write_text(SECRET_KEY)
    (job / "secret-key").chmod(0o600)
    set_state(stub, keys=["k" * 40])
    path = ds.run_registry_stage(str(job), prefix, expected=EXPECTED,
                                 secret_key_path=str(job / "secret-key"),
                                 other_secrets=[], port=5432, psql_path=str(stub / "psql"),
                                 deadline_ns=__import__("time").monotonic_ns() + int(60e9))
    assert Path(path).exists()
    assert sj.aggregate(str(job), prefix + ["registry"])["verdict"] == "VALID"


def fz_registry_name():
    from alrust_conformance import finalizer as fz
    return fz.REGISTRY_NAME


# --- DBFE-R2: a launched drop requires the frozen registry --------------------

def composed_drop(env, tmp_path, monkeypatch, upto):
    from test_finalizer_orchestration import build, STAGES, DB_IDENTITY
    _, stub = env
    (tmp_path / "composed").mkdir()
    job = build(tmp_path / "composed", monkeypatch, upto=upto)
    (job / "database").mkdir(mode=0o700, exist_ok=True)  # build() made it (P3c)
    name = "alrust_scaffold_123_1"
    set_state(stub, cluster=dict(CLUSTER, system_identifier=DB_IDENTITY["system_identifier"],
                                 postmaster_start_time=DB_IDENTITY["postmaster_start_time"]),
              roles={name: {"oid": DB_IDENTITY["role_oid"]}},
              databases={name: {"oid": DB_IDENTITY["oid"], "owner": name}})
    return job, stub, STAGES[:STAGES.index(upto) + 1]


def test_launched_drop_complete_after_the_registry(env, tmp_path, monkeypatch):
    job, stub, prefix = composed_drop(env, tmp_path, monkeypatch, "registry-exit")
    assert ds.run_drop_stage(str(job), prefix, launched=True, expected=EXPECTED,
                             **kw(stub)) == "COMPLETE"
    assert state(stub)["roles"] == {} and state(stub)["databases"] == {}
    assert sj.aggregate(str(job), prefix + [ds.DROP_STAGE])["verdict"] == "VALID"


def test_launched_drop_refuses_without_a_registry(env, tmp_path, monkeypatch):
    # Codex's control from 5425c1b8: quiescent through stop, no registry.
    job, stub, prefix = composed_drop(env, tmp_path, monkeypatch, "stop")
    before = (stub / "received.log").read_bytes() if (stub / "received.log").exists() else b""
    # P3b: without a registry the failed-entrypoint gate applies, and it
    # refuses a passing entrypoint (that path needs the registry first).
    with pytest.raises(ds.DatabaseStageError, match="entrypoint_not_failed"):
        ds.run_drop_stage(str(job), prefix, launched=True, expected=EXPECTED, **kw(stub))
    after = (stub / "received.log").read_bytes() if (stub / "received.log").exists() else b""
    assert after == before                                   # no SQL at all
    assert state(stub)["roles"] and state(stub)["databases"]  # nothing dropped


def test_launched_drop_refuses_a_failed_registry_stage(env, tmp_path, monkeypatch):
    from test_finalizer_orchestration import build, STAGES
    job, stub, prefix = composed_drop(env, tmp_path, monkeypatch, "stop")
    # Replace the job with one whose registry stage closed FAILED.
    (tmp_path / "failed").mkdir()
    job = build(tmp_path / "failed", monkeypatch, upto="registry", fail_stage="registry")
    (job / "database").mkdir(mode=0o700, exist_ok=True)  # build() made it (P3c)
    with pytest.raises(ds.DatabaseStageError, match="writer_gate_failed"):
        ds.run_drop_stage(str(job), STAGES[:STAGES.index("registry") + 1], launched=True,
                          expected=EXPECTED, **kw(stub))
    assert state(stub)["databases"]


def test_launched_drop_refuses_a_changed_registry(env, tmp_path, monkeypatch):
    from alrust_conformance import finalizer as fz
    job, stub, prefix = composed_drop(env, tmp_path, monkeypatch, "registry-exit")
    registry = job / fz.REGISTRY_NAME
    registry.write_text(registry.read_text().replace("SYNTHETIC", "CHANGED"))
    with pytest.raises(ds.DatabaseStageError, match="registry_changed"):
        ds.run_drop_stage(str(job), prefix, launched=True, expected=EXPECTED, **kw(stub))
    assert state(stub)["databases"]


def test_launched_drop_refuses_a_registry_frozen_before_stop(env, tmp_path, monkeypatch):
    # A registry written while the server might still run is not frozen.
    import test_finalizer_orchestration as tf
    order = ["database-create", "database-create-exit", "launch", "launch-confirm",
             "entrypoint", "entrypoint-exit", "registry", "registry-exit", "stop"]
    monkeypatch.setattr(tf, "STAGES", order)
    _, stub = env
    (tmp_path / "early").mkdir()
    job = tf.build(tmp_path / "early", monkeypatch, upto="stop")
    (job / "database").mkdir(mode=0o700, exist_ok=True)  # build() made it (P3c)
    with pytest.raises(ds.DatabaseStageError, match="registry_not_frozen"):
        ds.run_drop_stage(str(job), order, launched=True, expected=EXPECTED, **kw(stub))



# --- Q1 reader: inventory, inclusion gate, registry-authored count ------------

INVENTORY = ["provisioned-role-key-" + "r" * 24, "revoked-test-key-" + "v" * 30]


def reader_job(env, tmp_path, monkeypatch, inventory=INVENTORY, mutate=None, **build_kwargs):
    from test_finalizer_orchestration import build, STAGES
    _, stub = env
    (tmp_path / "reader").mkdir()
    job = build(tmp_path / "reader", monkeypatch, upto="stop", inventory=inventory,
                **build_kwargs)
    (job / fz_registry_name()).unlink()
    (job / "database").mkdir(mode=0o700, exist_ok=True)  # build() made it (P3c)
    url = "postgresql://alrust_scaffold_123_1:%s@127.0.0.1:5432/alrust_scaffold_123_1" % ("f" * 64)
    (job / "database" / "url").write_text(url + "\n")
    (job / "database" / "url").chmod(0o600)
    (job / "secret-key").write_text(SECRET_KEY)
    (job / "secret-key").chmod(0o600)
    set_state(stub, keys=["k" * 40])
    if mutate:
        mutate(job)
    return job, stub, STAGES[:STAGES.index("stop") + 1]


def run_reader(job, stub, prefix):
    import time as _time
    return ds.run_registry_stage(str(job), prefix, expected=EXPECTED,
                                 secret_key_path=str(job / "secret-key"),
                                 other_secrets=[], port=5432, psql_path=str(stub / "psql"),
                                 deadline_ns=_time.monotonic_ns() + int(60e9))


def registry_records(job, index=7):                  # STAGES.index("registry") (P3a)
    return records(Path(sj.journal_path(str(job), index, "registry")).read_bytes())


def test_reader_includes_every_inventory_value_and_records_the_count(env, tmp_path, monkeypatch):
    from alrust_conformance import finalizer as fz
    job, stub, prefix = reader_job(env, tmp_path, monkeypatch)
    path = run_reader(job, stub, prefix)
    data = Path(path).read_bytes()
    secrets = set(s.decode() for s in fz.load_registry(str(job), hashlib.sha256(data).hexdigest()))
    assert set(INVENTORY) <= secrets
    [count] = [r["payload"] for r in registry_records(job) if r["kind"] == "obtained_secret_count"]
    assert count == {"count": 2, "registry_sha256": hashlib.sha256(data).hexdigest()}
    assert sj.aggregate(str(job), prefix + ["registry"])["verdict"] == "VALID"


def test_reader_explicit_empty_inventory(env, tmp_path, monkeypatch):
    job, stub, prefix = reader_job(env, tmp_path, monkeypatch, inventory=[])
    run_reader(job, stub, prefix)
    [count] = [r["payload"] for r in registry_records(job) if r["kind"] == "obtained_secret_count"]
    assert count["count"] == 0


@pytest.mark.parametrize("case,code", [
    ("missing_record", "inventory_record_missing_or_repeated"),
    ("changed_file", "inventory_changed"),
    ("replaced_file", "inventory_changed"),
    ("wrong_core", "inventory_core_mismatch"),
    ("wrong_count", "inventory_count_mismatch"),
    ("wrong_path", "inventory_record_invalid"),
    ("extra_key", "inventory_record_invalid"),
    ("record_in_launch", "inventory_record_missing_or_repeated"),
    ("symlinked_file", "inventory_unreadable"),
    ("missing_file", "inventory_unreadable"),
    ("group_readable", "private_file_invalid"),
])
def test_reader_refusals_before_any_registry(env, tmp_path, monkeypatch, case, code):
    def mutate(job):
        snapshot = job / "entrypoint-secrets.json"
        if case == "changed_file":
            data = snapshot.read_bytes().replace(b"r" * 24, b"x" * 24)
            snapshot.write_bytes(data)
        elif case == "replaced_file":
            # Same bytes, new inode: the replacement is created while the
            # original still exists, so no filesystem can reuse its inode.
            data = snapshot.read_bytes()
            fresh = job / "entrypoint-secrets.json.new"
            fresh.write_bytes(data)
            fresh.chmod(0o600)
            os.replace(fresh, snapshot)
        elif case == "symlinked_file":
            moved = job / "elsewhere.json"
            snapshot.rename(moved)          # same inode, reached only via a link
            snapshot.symlink_to(moved)
        elif case == "missing_file":
            snapshot.unlink()
        elif case == "group_readable":
            snapshot.chmod(0o640)
    record_changes = {"wrong_count": lambda r: r.update(count=3),
                      "wrong_path": lambda r: r.update(path=r["path"] + ".other"),
                      "extra_key": lambda r: r.update(note="x")}
    if case == "missing_record":
        job, stub, prefix = reader_job(env, tmp_path, monkeypatch, inventory=None)
    elif case == "wrong_core":
        # A self-consistent snapshot (record digest matches) bound to another job.
        job, stub, prefix = reader_job(env, tmp_path, monkeypatch, inventory_core="0" * 64)
    elif case == "record_in_launch":
        job, stub, prefix = reader_job(env, tmp_path, monkeypatch, inventory_stage="launch")
    elif case in record_changes:
        job, stub, prefix = reader_job(env, tmp_path, monkeypatch,
                                       inventory_record_change=record_changes[case])
    else:
        job, stub, prefix = reader_job(env, tmp_path, monkeypatch, mutate=mutate)
    before = (stub / "received.log").read_bytes() if (stub / "received.log").exists() else b""
    with pytest.raises(ds.DatabaseStageError, match=code):
        run_reader(job, stub, prefix)
    after = (stub / "received.log").read_bytes() if (stub / "received.log").exists() else b""
    assert after == before                                  # no key query was sent
    assert not (job / fz_registry_name()).exists()          # no registry written
    assert registry_records(job)[-1]["payload"]["result"] == "FAILED"


def test_reader_refuses_a_same_count_different_secret_registry(env, tmp_path, monkeypatch):
    # The value-set inclusion gate: a buggy builder swaps one inventory value
    # for another of the same size; the count still matches, but it refuses.
    job, stub, prefix = reader_job(env, tmp_path, monkeypatch)
    real = ds.build_registry

    def swapped(job_root, journal, *, other_secrets, **kwargs):
        other_secrets = ["substituted-secret-" + "z" * 30 if s == INVENTORY[1] else s
                         for s in other_secrets]
        return real(job_root, journal, other_secrets=other_secrets, **kwargs)
    monkeypatch.setattr(ds, "build_registry", swapped)
    with pytest.raises(ds.DatabaseStageError, match="registry_inclusion_failed"):
        run_reader(job, stub, prefix)
    kinds = [r["kind"] for r in registry_records(job)]
    assert "obtained_secret_count" not in kinds
    assert registry_records(job)[-1]["payload"]["result"] == "FAILED"


def test_reader_gate_refusal_opens_no_inventory(env, tmp_path, monkeypatch):
    # A fresh job whose writer journals stop at launch: the reader's gate
    # needs the stages through stop, so it refuses. The gate runs read-only
    # before any journal of this stage is open; _run_stage then opens the
    # registry journal at index len(prefix), bound to launch, and closes it
    # FAILED with the gate's code. No inventory is read and no SQL is sent.
    from test_finalizer_orchestration import build
    _, stub = env
    (tmp_path / "reader3").mkdir()
    job = build(tmp_path / "reader3", monkeypatch, upto="launch")
    prefix = ["database-create", "database-create-exit", "launch"]
    # A private inventory sentinel, so a wrong read would be seen by the spy.
    sentinel = job / ds.INVENTORY_NAME
    if not sentinel.exists():
        sentinel.write_text('["sentinel-inventory-' + "s" * 24 + '"]')
    sentinel.chmod(0o600)
    (job / "database").mkdir(mode=0o700, exist_ok=True)
    (job / "secret-key").write_text(SECRET_KEY)
    (job / "secret-key").chmod(0o600)
    opened = []
    real_open = os.open

    def spy(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(ds.os, "open", spy)
    before = (stub / "received.log").read_bytes() if (stub / "received.log").exists() else b""
    with pytest.raises(ds.DatabaseStageError, match="gate_stages_invalid"):
        run_reader(job, stub, prefix)
    monkeypatch.undo()
    # The spy saw the gate read the writer journals, and never the inventory.
    assert any(p.endswith("02-launch.jsonl") for p in opened)
    assert not any(p.endswith(ds.INVENTORY_NAME) for p in opened)
    after = (stub / "received.log").read_bytes() if (stub / "received.log").exists() else b""
    assert after == before
    refusal = Path(sj.journal_path(str(job), len(prefix), "registry"))
    assert refusal.name == "03-registry.jsonl"
    rows = records(refusal.read_bytes())
    launch = Path(sj.journal_path(str(job), 2, "launch")).read_bytes()
    assert rows[0]["kind"] == "stage_begin"
    assert rows[0]["payload"]["previous_stage"] == {
        "index": 2, "name": "launch", "sha256": hashlib.sha256(launch).hexdigest()}
    assert rows[-1]["kind"] == "stage_complete"
    assert rows[-1]["payload"] == {"result": "FAILED", "code": "gate_stages_invalid"}


# --- Q1R44-R1: the held root, bound to the validated core, after the gate ---

def _swap_ancestor(job):
    # Codex's independent control (f56aa58e): the same job inode, path
    # spelling and bytes, reached through one symlinked owned ancestor.
    ancestor = job.parent
    backing = ancestor.with_name("reader-real")
    ancestor.rename(backing)
    ancestor.symlink_to(backing, target_is_directory=True)


def _replace_root_with_copy(job):
    import shutil
    copy = job.with_name("job-copy")
    shutil.copytree(job, copy, symlinks=True)
    job.rename(job.with_name("job-original"))
    copy.rename(job)


def _symlink_root(job):
    real = job.with_name("job-real")
    job.rename(real)
    job.symlink_to(real, target_is_directory=True)


def _change_core(job):
    core = job / "core-identity.json"
    data = core.read_bytes()
    core.chmod(0o600)
    core.write_bytes(data.replace(b'"profile":"scaffold"', b'"profile":"scaffolx"'))


@pytest.mark.parametrize("case,tamper", [
    ("ancestor_symlink", _swap_ancestor),
    ("root_replaced_by_copy", _replace_root_with_copy),
    ("root_symlink", _symlink_root),
    ("core_changed", _change_core),
    ("root_mode", lambda job: job.chmod(0o750)),
])
def test_reader_refuses_a_root_changed_after_the_gate(env, tmp_path, monkeypatch, case, tamper):
    from alrust_conformance import finalizer as fz
    job, stub, prefix = reader_job(env, tmp_path, monkeypatch)
    evidence = fz.writer_quiescence(str(job), prefix, EXPECTED, ("database-create",))     # the real gate passed
    assert ds.read_inventory(str(job), evidence) == INVENTORY      # control
    tamper(job)
    with pytest.raises(ds.DatabaseStageError, match="^inventory_root_changed$"):
        ds.read_inventory(str(job), evidence)


@pytest.mark.parametrize("spelling", ["{}/", "{}/.", "{}/../job", "relative"])
def test_reader_refuses_an_unnormalized_root(env, tmp_path, monkeypatch, spelling):
    from alrust_conformance import finalizer as fz
    job, stub, prefix = reader_job(env, tmp_path, monkeypatch)
    evidence = fz.writer_quiescence(str(job), prefix, EXPECTED, ("database-create",))
    with pytest.raises(ds.DatabaseStageError, match="^inventory_root_changed$"):
        ds.read_inventory(spelling.format(job) if "{}" in spelling else "job", evidence)


def test_root_change_refuses_the_registry_stage_before_any_query(env, tmp_path, monkeypatch):
    # The swap lands after the gate and after the registry journal opened
    # (a swap before that refuses earlier, in StageJournal's own root check).
    job, stub, prefix = reader_job(env, tmp_path, monkeypatch)
    real_read = ds.read_inventory

    def swap_then_read(job_root, evidence):
        _swap_ancestor(job)
        return real_read(job_root, evidence)
    monkeypatch.setattr(ds, "read_inventory", swap_then_read)
    before = (stub / "received.log").read_bytes() if (stub / "received.log").exists() else b""
    with pytest.raises(ds.DatabaseStageError, match="^inventory_root_changed$"):
        run_reader(job, stub, prefix)
    after = (stub / "received.log").read_bytes() if (stub / "received.log").exists() else b""
    assert after == before
    assert not (job / fz_registry_name()).exists()
    assert registry_records(job)[-1]["payload"]["result"] == "FAILED"
