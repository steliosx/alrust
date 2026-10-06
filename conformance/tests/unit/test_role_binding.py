"""Pure controls for the job-role binding before the first secret read
(R-EP plan P3c): the launch gate and the registry's binding.

Synthetic jobs from test_finalizer_orchestration.build with a synthetic ended
generation (absent PIDs); Psql is replaced by an in-process fake, so no
process, socket or database is used."""
import os
import shutil

import pytest

import exit_fixtures as xf
from exit_fixtures import EXPECTED
from alrust_conformance import database_stage as ds
from alrust_conformance import stage_journal as sj
from alrust_conformance import tool_exit as te
from test_finalizer_orchestration import STAGES, build

SYNTHETIC_SUPERVISOR = (4194293, 333)
LAUNCH_PREFIX = ["database-create", "database-create-exit"]
ROLE = "alrust_scaffold_123_1"


@pytest.fixture
def job(tmp_path, monkeypatch):
    if os.path.exists("/proc/%d" % SYNTHETIC_SUPERVISOR[0]):
        pytest.skip("synthetic ended PID is in use on this host")

    def make(**kw):
        kw.setdefault("generation", SYNTHETIC_SUPERVISOR)
        return build(tmp_path, monkeypatch, **kw)
    return make


def _identity(root):
    info = os.stat(root / "database")
    return {"dev": info.st_dev, "ino": info.st_ino}


# --- the launch gate ------------------------------------------------------------------

def test_launch_gate_derives_role_and_directory(job):
    root = job(upto="database-create-exit")
    binding = ds.launch_prerequisites(str(root), LAUNCH_PREFIX, EXPECTED)
    assert binding["role"] == ROLE and binding["database_dir"] == _identity(root)
    assert binding["aggregation"]["verdict"] == "VALID"


@pytest.mark.parametrize("build_kw,code", [
    ({"created_change": lambda c: c.update(name="alrust_scaffold_999_1")},
     "database_role_unexpected"),
    ({"created_change": lambda c: c.update(role="alrust_other_123_1")},
     "database_role_unexpected"),
    ({"fail_stage": "database-create"}, "launch_prefix_not_complete"),
])
def test_launch_gate_refusals(job, build_kw, code):
    root = job(upto="database-create-exit", **build_kw)
    with pytest.raises(ds.DatabaseStageError, match=code):
        ds.launch_prerequisites(str(root), LAUNCH_PREFIX, EXPECTED)


def test_launch_gate_needs_exactly_one_recorded_directory(job, tmp_path):
    # A second owned_resource for the same database directory.
    root = job(upto="database-create-exit", extra_records=[
        ("database-create", "owned_resource", {"path": str(tmp_path / "job" / "database"),
                                               "dev": 1, "ino": 2, "type": "dir"})])
    with pytest.raises(ds.DatabaseStageError, match="database_dir_unrecorded"):
        ds.launch_prerequisites(str(root), LAUNCH_PREFIX, EXPECTED)


def test_launch_gate_needs_the_create_holder(job):
    root = job(upto="database-create-exit")
    os.unlink(te.receipt_paths(str(root), "database-create", 0)[1])
    with pytest.raises(ds.DatabaseStageError, match="holder_verdict_mismatch"):
        ds.launch_prerequisites(str(root), LAUNCH_PREFIX, EXPECTED)


@pytest.mark.parametrize("prefix", [["database-create"], LAUNCH_PREFIX + ["launch"],
                                    list(reversed(LAUNCH_PREFIX))])
def test_launch_gate_prefix_is_exact(job, prefix):
    root = job(upto="database-create-exit")
    with pytest.raises(ds.DatabaseStageError, match="launch_prefix_invalid"):
        ds.launch_prerequisites(str(root), prefix, EXPECTED)


def test_launch_gate_checks_pins(job):
    root = job(upto="database-create-exit")
    pins = dict(EXPECTED, entrypoint=dict(EXPECTED["entrypoint"],
                                          entrypoint_script_sha256="f" * 64))
    with pytest.raises(ds.DatabaseStageError, match="gate_pins_mismatch"):
        ds.launch_prerequisites(str(root), LAUNCH_PREFIX, pins)


def test_launch_gate_never_reads_the_url_or_key(job, monkeypatch):
    root = job(upto="database-create-exit")
    (root / "database" / "url").write_text("never read\n")
    opened = []
    real_open = os.open

    def spy(path, *a, **k):
        opened.append(str(path))
        return real_open(path, *a, **k)
    monkeypatch.setattr(os, "open", spy)
    ds.launch_prerequisites(str(root), LAUNCH_PREFIX, EXPECTED)
    assert not any(p.endswith("/url") or p.endswith("secret-key") for p in opened)


# --- the registry binding --------------------------------------------------------------

class FakePsql:
    calls = []

    def __init__(self, *a, user=None, **k):
        FakePsql.calls.append(("psql", user))

    def query_json(self, sql, database, params):
        return {"keys": []}


def _registry_job(job, url_role=ROLE):
    root = job(upto="stop", inventory=["provisioned-role-key-" + "r" * 24])
    (root / "credential-registry.json").unlink()
    url = "postgresql://%s:%s@127.0.0.1:5432/%s" % (url_role, "f" * 64, url_role)
    (root / "database" / "url").write_text(url + "\n")
    (root / "database" / "url").chmod(0o600)
    (root / "secret-key").write_text("synthetic-signing-" + "s" * 40)
    (root / "secret-key").chmod(0o600)
    return root


def _run_registry(root):
    return ds.run_registry_stage(str(root), STAGES[:STAGES.index("stop") + 1], expected=EXPECTED,
                                 secret_key_path=str(root / "secret-key"), other_secrets=[],
                                 port=5432, psql_path="/usr/bin/psql", deadline_ns=10**18)


@pytest.fixture
def fake_psql(monkeypatch):
    FakePsql.calls = []
    monkeypatch.setattr(ds, "Psql", FakePsql)
    return FakePsql.calls


def test_registry_reads_as_the_bound_role(job, fake_psql):
    root = _registry_job(job)
    assert os.path.exists(_run_registry(root))
    assert fake_psql == [("psql", ROLE)]


def test_registry_refuses_another_role_before_psql(job, fake_psql):
    root = _registry_job(job, url_role="alrust_scaffold_999_1")
    with pytest.raises(ds.DatabaseStageError, match="database_role_unexpected"):
        _run_registry(root)
    assert fake_psql == [] and not (root / "credential-registry.json").exists()


def test_registry_refuses_a_replaced_database_directory_before_psql(job, fake_psql, tmp_path):
    root = _registry_job(job)
    os.rename(root / "database", tmp_path / "old-database")
    (root / "database").mkdir(mode=0o700)
    shutil.copy2(tmp_path / "old-database" / "url", root / "database" / "url")
    with pytest.raises(ds.DatabaseStageError, match="database_dir_not_bound"):
        _run_registry(root)
    assert fake_psql == []


def test_build_registry_requires_a_binding(job, fake_psql):
    root = _registry_job(job)
    with sj.StageJournal(str(root), STAGES.index("registry"), "registry", "stop") as journal:
        with pytest.raises(ds.DatabaseStageError, match="database_dir_not_bound"):
            ds.build_registry(str(root), journal, secret_key_path=str(root / "secret-key"),
                              other_secrets=[], port=5432, psql_path="/usr/bin/psql",
                              deadline_ns=10**18, binding=None)
    assert fake_psql == []


# --- the launcher's own fixtures against the real launch gate (no process) -------------

import json                                                   # noqa: E402

import test_reference_launcher as trl                         # noqa: E402


def test_launcher_fixture_passes_the_launch_gate(tmp_path):
    job = trl.make_job(tmp_path)
    binding = ds.launch_prerequisites(str(job), LAUNCH_PREFIX, xf.EXPECTED)
    info = os.stat(job / "database")
    assert binding["role"] == trl.TEST_ROLE == "alrust_scaffold_1_1"
    assert binding["database_dir"] == {"dev": info.st_dev, "ino": info.st_ino}
    # The URL the fixture's child will read names exactly that role.
    assert trl.TEST_URL.split("@")[0].endswith(trl.TEST_ROLE + ":" + "5e" * 32)


def _main_until_popen(monkeypatch, lr_mod):
    started = []
    for name in ("check_environment", "check_external_config", "check_home"):
        monkeypatch.setattr(lr_mod, name, lambda *a, **k: None)

    def popen(*a, **k):
        started.append(a)
        raise RuntimeError("stop before the supervisor")
    monkeypatch.setattr(lr_mod.subprocess, "Popen", popen)
    return started


def _launch_argv(job, port=18081):
    return (["--job-root", str(job), "--venv", str(job / "venv"), "--port", str(port),
             "--ready-timeout", "5"] + trl.secret_args(job) + trl.STAGE_ARGS)


@pytest.mark.parametrize("tamper", [
    lambda job: os.unlink(te.receipt_paths(str(job), "database-create", 0)[1]),
    lambda job: (job.parent / "pins.json").write_text(json.dumps(dict(
        xf.EXPECTED, entrypoint={"entrypoint_script_sha256": "f" * 64,
                                 "interpreter_path": xf.PYTHON}))),
])
def test_launcher_main_refuses_before_any_supervisor(tmp_path, monkeypatch, capsys, tamper):
    from test_paths_only_secrets import lr
    job = trl.make_job(tmp_path)
    tamper(job)
    started = _main_until_popen(monkeypatch, lr)
    assert lr.main(_launch_argv(job)) == 1
    assert started == [] and "launch gate" in capsys.readouterr().err
    assert not os.path.exists(sj.journal_path(str(job), 2, "launch"))


def test_launcher_main_refuses_the_old_stage_position(tmp_path, monkeypatch, capsys):
    from test_paths_only_secrets import lr
    job = trl.make_job(tmp_path)
    started = _main_until_popen(monkeypatch, lr)
    argv = _launch_argv(job)[:-4] + ["--stage-index", "1", "--previous-stage",
                                     "database-create"]
    assert lr.main(argv) == 1 and started == []


def test_launch_gate_refuses_a_consistent_but_foreign_role(job):
    # name == role, both well formed, but not database_name(core): another job's.
    root = job(upto="database-create-exit",
               created_change=lambda c: c.update(name="alrust_scaffold_999_1",
                                                 role="alrust_scaffold_999_1"))
    with pytest.raises(ds.DatabaseStageError, match="database_role_unexpected"):
        ds.launch_prerequisites(str(root), LAUNCH_PREFIX, EXPECTED)


def test_launcher_main_reaches_the_supervisor_with_the_binding(tmp_path, monkeypatch):
    # The converted fixture passes every launcher check in-process; the
    # first Popen is the supervisor's, refused by the spy before it starts.
    from test_paths_only_secrets import lr
    job = trl.make_job(tmp_path)
    started = _main_until_popen(monkeypatch, lr)
    monkeypatch.setattr(lr, "listeners", lambda port: {})
    version = tmp_path / "VERSION"
    version.write_text("9.1.0\n")

    def reference_open(path, *a, **k):      # this checkout has no reference/alerta
        if str(path).endswith(os.path.join("reference", "alerta", "VERSION")):
            path = version
        return open(path, *a, **k)
    monkeypatch.setattr(lr, "open", reference_open, raising=False)
    with pytest.raises(RuntimeError, match="stop before the supervisor"):
        lr.main(_launch_argv(job))
    [argv] = [list(a[0]) for a in started]
    info = os.stat(job / "database")
    assert any(str(arg).endswith("reference-supervisor.py") for arg in argv)
    i = argv.index("--expected-role")
    assert argv[i:i + 4] == ["--expected-role", trl.TEST_ROLE, "--database-dir-identity",
                             "%d:%d" % (info.st_dev, info.st_ino)]


@pytest.mark.parametrize("data", [b"{not json", b"\xff\xfe", b"[1, 2]"])
def test_malformed_pins_refuse_with_a_fixed_message(tmp_path, monkeypatch, capsys, data):
    from test_paths_only_secrets import lr
    job = trl.make_job(tmp_path)
    (job.parent / "pins.json").write_bytes(data)
    started = _main_until_popen(monkeypatch, lr)
    assert lr.main(_launch_argv(job)) == 1 and started == []
    assert "expected pins are not" in capsys.readouterr().err
