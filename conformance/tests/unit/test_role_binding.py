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

    passwords = []

    def __init__(self, *a, user=None, password=None, **k):
        FakePsql.calls.append(("psql", user))
        FakePsql.passwords.append(password)

    def query_json(self, sql, database, params):
        return {"keys": []}


def _url(role, password):
    return "postgresql://%s:%s@127.0.0.1:5432/%s\n" % (role, password, role)


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
    FakePsql.passwords = []
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



# --- P3C-DIR-R1: the read goes through the held, recorded directory -------------------

@pytest.mark.parametrize("symlink", [False, True])
def test_registry_swap_after_the_check_never_uses_substituted_bytes(job, fake_psql,
                                                                     monkeypatch, symlink):
    # Codex's 7be77962 scenario: the database directory is replaced (renamed
    # away, then a new directory or a symlink to one takes its path) at the
    # moment the URL is read. The read stays on the held, recorded object, so
    # the original password is used, or the stage refuses; the substituted
    # one never reaches Psql.
    root = _registry_job(job)
    original = "f" * 64
    real_read = ds._read_private_file
    swapped = []

    def swap_then_read(path, limit, dir_fd=None):
        if path == ds.URL_NAME and not swapped:
            os.rename(root / "database", root / "original-database")
            replacement = root / ("replacement-database" if symlink else "database")
            replacement.mkdir(mode=0o700)
            (replacement / "url").write_text(_url(ROLE, "b" * 64))
            (replacement / "url").chmod(0o600)
            if symlink:
                (root / "database").symlink_to(replacement, target_is_directory=True)
            swapped.append(True)
        return real_read(path, limit, dir_fd=dir_fd)
    monkeypatch.setattr(ds, "_read_private_file", swap_then_read)
    try:
        _run_registry(root)
    except ds.DatabaseStageError:
        assert FakePsql.passwords == []
    else:
        assert FakePsql.passwords == [original]
    assert swapped == [True] and "b" * 64 not in FakePsql.passwords


def test_registry_refuses_a_symlinked_database_directory(job, fake_psql, tmp_path):
    root = _registry_job(job)
    os.rename(root / "database", tmp_path / "moved-database")
    (root / "database").symlink_to(tmp_path / "moved-database", target_is_directory=True)
    with pytest.raises(ds.DatabaseStageError, match="database_dir_not_bound"):
        _run_registry(root)
    assert fake_psql == []


def test_registry_refuses_a_symlinked_url_file(job, fake_psql, tmp_path):
    root = _registry_job(job)
    os.rename(root / "database" / "url", tmp_path / "url")
    (root / "database" / "url").symlink_to(tmp_path / "url")
    with pytest.raises(ds.DatabaseStageError, match="url_invalid"):
        _run_registry(root)
    assert fake_psql == []


# --- P3C-FIXTURE-R2: each stage-preflight variant refuses for its own reason ----------

@pytest.mark.parametrize("variant,expected", [
    ("no_core", sj.CORE_NAME),
    ("no_previous", "previous stage journal missing"),
    ("launch_exists", "launch stage journal already exists"),
    ("valid", None),
])
def test_stage_preflight_variants_in_process(tmp_path, monkeypatch, capsys, variant, expected):
    # In-process twin of test_reference_launcher's (NOT_RUN) preflight variants.
    from test_paths_only_secrets import lr
    if variant == "no_core":
        job = trl.make_job(tmp_path, stage=False)
        assert not (job / sj.CORE_NAME).exists()
    else:
        job = trl.stage_job(tmp_path, previous=variant != "no_previous")
    if variant == "launch_exists":
        open(sj.journal_path(str(job), 2, "launch"), "w").close()
    shutil.rmtree(job / "venv")
    started = _main_until_popen(monkeypatch, lr)
    rc = lr.main(["--preflight"] + _launch_argv(job))
    err = capsys.readouterr().err
    assert started == []
    if expected is None:
        assert rc == 0, err                 # the positive control: nothing else refuses
    else:
        assert rc == 1 and expected in err, err


@pytest.mark.parametrize("symlink", [False, True])
def test_bound_url_read_returns_the_held_directory_bytes(job, monkeypatch, symlink):
    # The read itself (before the Psql path recheck): swapped at the moment
    # of the URL open, it still returns the recorded directory's bytes.
    root = _registry_job(job)
    info = os.stat(root / "database")
    core = (root / sj.CORE_NAME).read_bytes()
    binding = {"role": ROLE, "database_dir": {"dev": info.st_dev, "ino": info.st_ino},
               "core_sha256": __import__("hashlib").sha256(core).hexdigest()}
    real_read = ds._read_private_file

    swapped = []

    def swap_then_read(path, limit, dir_fd=None):
        if path == ds.URL_NAME:                 # the URL open, after the directory check
            os.rename(root / "database", root / "original-database")
            replacement = root / ("replacement-database" if symlink else "database")
            replacement.mkdir(mode=0o700)
            (replacement / "url").write_text(_url(ROLE, "b" * 64))
            (replacement / "url").chmod(0o600)
            if symlink:
                (root / "database").symlink_to(replacement, target_is_directory=True)
            swapped.append(True)
        return real_read(path, limit, dir_fd=dir_fd)
    monkeypatch.setattr(ds, "_read_private_file", swap_then_read)
    assert ds._read_bound_url(str(root), binding).decode() == _url(ROLE, "f" * 64)
    assert swapped == [True]


def test_bound_url_read_refuses_another_core(job):
    root = _registry_job(job)
    info = os.stat(root / "database")
    binding = {"role": ROLE, "database_dir": {"dev": info.st_dev, "ino": info.st_ino},
               "core_sha256": "0" * 64}
    with pytest.raises(ds.DatabaseStageError, match="inventory_root_changed"):
        ds._read_bound_url(str(root), binding)


def test_registry_rechecks_the_psql_directory_after_the_read(job, fake_psql, monkeypatch,
                                                             tmp_path):
    # Replaced after the bound read, before Psql's passfile path is used.
    root = _registry_job(job)
    real = ds._read_bound_url

    def read_then_swap(job_root, binding):
        data = real(job_root, binding)
        os.rename(root / "database", tmp_path / "old-database")
        (root / "database").mkdir(mode=0o700)
        return data
    monkeypatch.setattr(ds, "_read_bound_url", read_then_swap)
    with pytest.raises(ds.DatabaseStageError, match="database_dir_not_bound"):
        _run_registry(root)
    assert fake_psql == []


def test_held_directory_identity_is_checked_not_only_the_later_path(job, fake_psql,
                                                                    monkeypatch):
    # An unbound directory holds the path at the read and the recorded one is
    # put back before the path recheck: only the held fstat check refuses.
    root = _registry_job(job)
    os.rename(root / "database", root / "original-database")
    (root / "database").mkdir(mode=0o700)
    (root / "database" / "url").write_text(_url(ROLE, "b" * 64))
    (root / "database" / "url").chmod(0o600)
    real = ds._database_dir

    def restore_then_check(job_root, create):
        os.rename(root / "database", root / "unbound-database")
        os.rename(root / "original-database", root / "database")
        return real(job_root, create)
    monkeypatch.setattr(ds, "_database_dir", restore_then_check)
    with pytest.raises(ds.DatabaseStageError, match="database_dir_not_bound"):
        _run_registry(root)
    assert "b" * 64 not in FakePsql.passwords and fake_psql == []
