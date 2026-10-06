"""Pure controls for the supervisor's tool mode and the stage-tool driver
(R-EP plan P2b-2). The supervisor's main() is called in-process with fork,
signal registration, prctl and group leadership replaced, so nothing is
started or signalled; the driver's main() is called with the stage runners
replaced. Private temporary files only; no subprocess, git or socket."""
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from alrust_conformance import database_stage as ds
from alrust_conformance import stage_journal as sj
from alrust_conformance import tool_exit as te

ROOT = Path(__file__).resolve().parents[3]
SUPERVISOR = ROOT / "conformance/scripts/entrypoint-supervisor.py"
DRIVER = ROOT / "conformance/scripts/stage-tool.py"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sup = load(SUPERVISOR, "entrypoint_supervisor_tool")
drv = load(DRIVER, "stage_tool_driver")


def _job(tmp_path, monkeypatch, schema=3, driver=None):
    """A real /3 (or /2) core whose pins match this supervisor, the driver
    and this interpreter."""
    monkeypatch.setattr(sj, "EXTERNAL_CONFIG", str(tmp_path / "absent.conf"))
    root = tmp_path / "job"
    root.mkdir(mode=0o700)
    driver = driver or str(DRIVER)
    resolved = os.path.realpath(sys.executable)
    entry = {"supervisor": {"path": str(SUPERVISOR), "sha256": sj.file_sha256(str(SUPERVISOR))},
             "entrypoint_script": {"path": str(DRIVER), "sha256": sj.file_sha256(str(DRIVER))},
             "interpreter": {"path": sys.executable, "resolved_path": resolved}}
    measured = {"supervisor": entry["supervisor"]["sha256"],
                "entrypoint_script": entry["entrypoint_script"]["sha256"],
                "interpreter": sj.file_sha256(resolved)}
    extra = dict(entrypoint_pins=entry, entrypoint_measured=measured)
    if schema == 3:
        pin = {"path": driver, "sha256": sj.file_sha256(driver)}
        extra.update(tool_pins={t: {"driver": dict(pin)} for t in sj.TOOL_PIN_NAMES},
                     tool_measured={t: pin["sha256"] for t in sj.TOOL_PIN_NAMES})
    sha = "a" * 64
    digest = sj.write_core_identity(
        str(root), profile="scaffold", event_head_sha="b" * 40, checkout_sha="b" * 40,
        reference_sha="c" * 40, reference_pin="c" * 40, lock_sha256={"lock": sha},
        lock_pins={"lock": sha}, manifest_sha256=sha, manifest_pin=sha,
        env={"GITHUB_REPOSITORY": "steliosx/alrust", "GITHUB_RUN_ID": "123",
             "GITHUB_RUN_ATTEMPT": "1", "GITHUB_JOB": "profile", "ImageOS": "ubuntu24",
             "ImageVersion": "20261001.1"}, **extra)
    sj.prepare_journal_dir(str(root))
    return root, digest


@pytest.fixture
def mocked(monkeypatch):
    events = []

    class ForkReached(Exception):
        pass

    def fork():
        events.append("fork")
        raise ForkReached()
    monkeypatch.setattr(sup.os, "fork", fork)
    monkeypatch.setattr(sup.os, "getpgid", lambda pid: os.getpid())
    monkeypatch.setattr(sup.signal, "signal", lambda *a: events.append("signal"))
    monkeypatch.setattr(sup, "set_subreaper", lambda: events.append("prctl") or 1)
    return events, ForkReached


def argv_for(root, digest, tool="database-create", index=0, **override):
    run, launch, terminal, journal = sup.tool_paths(str(root), tool, index)
    opts = {"--receipt": launch, "--terminal": terminal, "--core": str(root / sj.CORE_NAME),
            "--core-sha256": digest, "--script": str(DRIVER), "--entrypoint-journal": journal,
            "--deadline-monotonic-ns": str(sup.time.monotonic_ns() + 60 * 10**9),
            "--tool": tool, "--stage-index": str(index)}
    opts.update(override)
    argv = [x for k, v in opts.items() if v is not None for x in (k, v)]
    return argv + ["--", sys.executable, "-I", str(DRIVER), tool]


def test_paths_agree_with_the_verifier(tmp_path):
    run, launch, terminal, journal = sup.tool_paths("/job", "registry", 7)
    assert (launch, terminal) == te.receipt_paths("/job", "registry", 7)
    assert journal == sj.journal_path("/job", 7, "registry")


def test_tool_mode_reaches_fork_and_creates_the_run_directory(tmp_path, monkeypatch, mocked):
    events, reached = mocked
    root, digest = _job(tmp_path, monkeypatch)
    with pytest.raises(reached):
        sup.main(argv_for(root, digest))
    assert events[-1] == "fork"
    assert (root / "tools" / "00-database-create").is_dir()
    assert not (root / "tools" / "00-database-create" / "launch.json").exists()


@pytest.mark.parametrize("override,message", [
    ({"--tool": None}, "go together"),
    ({"--stage-index": None}, "go together"),
    ({"--tool": "database-drop"}, "--tool must be"),
    ({"--tool": "entrypoint"}, "--tool must be"),
    ({"--stage-index": "31"}, "--stage-index"),
    ({"--stage-index": "01"}, "--stage-index"),
    ({"--stage-index": "-1"}, "--stage-index"),
])
def test_tool_mode_usage_refuses_before_anything(tmp_path, monkeypatch, mocked, capsys,
                                                 override, message):
    events, reached = mocked
    root, digest = _job(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exit_:
        sup.main(argv_for(root, digest, **override))
    assert exit_.value.code == sup.EXIT_USAGE and events == []
    assert message in capsys.readouterr().err


def _setup_refusal(tmp_path, monkeypatch, mocked, capsys, argv, message):
    events, reached = mocked
    with pytest.raises(SystemExit) as exit_:
        sup.main(argv)
    assert exit_.value.code == sup.EXIT_SETUP and "fork" not in events
    assert message in capsys.readouterr().err


def test_legacy_v2_core_refuses_in_tool_mode(tmp_path, monkeypatch, mocked, capsys):
    root, digest = _job(tmp_path, monkeypatch, schema=2)
    _setup_refusal(tmp_path, monkeypatch, mocked, capsys, argv_for(root, digest),
                   "core is not core/3")
    assert not (root / "tools").exists()


def test_driver_mismatch_refuses(tmp_path, monkeypatch, mocked, capsys):
    other = tmp_path / "other-driver.py"
    other.write_bytes(b"# not the driver\n")
    root, digest = _job(tmp_path, monkeypatch, driver=str(other))
    _setup_refusal(tmp_path, monkeypatch, mocked, capsys, argv_for(root, digest),
                   "tool driver does not match its pin")
    assert not (root / "tools").exists()


@pytest.mark.parametrize("key", ["--receipt", "--terminal", "--entrypoint-journal"])
def test_paths_other_than_the_fixed_ones_refuse(tmp_path, monkeypatch, mocked, capsys, key):
    root, digest = _job(tmp_path, monkeypatch)
    argv = argv_for(root, digest, **{key: str(tmp_path / "elsewhere.json")})
    _setup_refusal(tmp_path, monkeypatch, mocked, capsys, argv, "not the fixed ones")


def test_paths_for_another_index_refuse(tmp_path, monkeypatch, mocked, capsys):
    root, digest = _job(tmp_path, monkeypatch)
    run, launch, terminal, journal = sup.tool_paths(str(root), "database-create", 2)
    argv = argv_for(root, digest, **{"--receipt": launch, "--terminal": terminal,
                                     "--entrypoint-journal": journal})
    _setup_refusal(tmp_path, monkeypatch, mocked, capsys, argv, "not the fixed ones")


def test_an_existing_run_directory_refuses(tmp_path, monkeypatch, mocked, capsys):
    root, digest = _job(tmp_path, monkeypatch)
    (root / "tools" / "00-database-create").mkdir(parents=True)
    _setup_refusal(tmp_path, monkeypatch, mocked, capsys, argv_for(root, digest),
                   "tool run directory")


def test_entrypoint_mode_still_requires_v2(tmp_path, monkeypatch, mocked, capsys):
    root, digest = _job(tmp_path, monkeypatch)            # a /3 core
    argv = argv_for(root, digest, **{"--tool": None, "--stage-index": None})
    argv[argv.index(str(DRIVER), argv.index("--")) + 1:] = []
    _setup_refusal(tmp_path, monkeypatch, mocked, capsys, argv, "core is not core/2")


def test_core_pins_tool_shapes():
    base = {"schema": sup.SCHEMA_CORE_V3, "job_root": {"path": "/job"},
            "entrypoint_pins": {"supervisor": {"path": "/s", "sha256": "a" * 64},
                                "entrypoint_script": {"path": "/e", "sha256": "b" * 64},
                                "interpreter": {"path": "/p", "resolved_path": "/p",
                                                "measured_sha256": "c" * 64}},
            "tool_pins": {t: {"driver": {"path": "/d", "sha256": "d" * 64}}
                          for t in sup.TOOL_NAMES}}
    pins = sup.core_pins(json.dumps(base).encode(), "registry")
    assert pins["entrypoint_script"] == {"path": "/d", "sha256": "d" * 64}
    assert pins["job_root"] == "/job"
    for mutate in (lambda c: c["tool_pins"].pop("registry"),
                   lambda c: c["tool_pins"]["registry"].update(extra=1),
                   lambda c: c["tool_pins"]["registry"]["driver"].update(sha256="D" * 64),
                   lambda c: c.pop("job_root"),
                   lambda c: c.update(schema=sup.SCHEMA_CORE_V2)):
        core = json.loads(json.dumps(base))
        mutate(core)
        with pytest.raises(ValueError):
            sup.core_pins(json.dumps(core).encode(), "registry")


# --- the driver ------------------------------------------------------------

CREATE = ["database-create", "--job-root", "/job", "--profile", "scaffold", "--run-id", "123",
          "--attempt", "1", "--port", "5432", "--superuser", "postgres",
          "--public-superuser-password", "postgres", "--container-id", "a" * 12,
          "--psql", "/usr/bin/psql", "--docker", "/usr/bin/docker",
          "--deadline-monotonic-ns", "1000"]
REGISTRY = ["registry", "--job-root", "/job", "--prefix", "database-create,stop",
            "--secret-key-file", "/job/secret-key", "--port", "5432", "--psql",
            "/usr/bin/psql", "--deadline-monotonic-ns", "1000"]


@pytest.fixture
def runners(monkeypatch):
    calls = []
    monkeypatch.setattr(ds, "run_create_stage", lambda *a, **k: calls.append(("create", a, k))
                        or "/job/database/url")
    monkeypatch.setattr(ds, "run_registry_stage", lambda *a, **k: calls.append(
        ("registry", a, k)) or "/job/credential-registry.json")
    return calls


def test_driver_create_passes_exact_arguments(runners, capsys):
    assert drv.main(CREATE) == 0
    (name, args, kwargs), = runners
    assert name == "create" and args == ("/job",)
    assert kwargs == {"profile": "scaffold", "run_id": "123", "attempt": "1", "port": 5432,
                      "superuser": "postgres", "superuser_password": "postgres",
                      "container_id": "a" * 12, "psql_path": "/usr/bin/psql",
                      "docker_path": "/usr/bin/docker", "deadline_ns": 1000}
    assert json.loads(capsys.readouterr().out) == {"result": "COMPLETE",
                                                   "tool": "database-create"}


def test_driver_registry_passes_paths_only(runners, capsys):
    assert drv.main(REGISTRY) == 0
    (name, args, kwargs), = runners
    assert args == ("/job", ["database-create", "stop"])
    assert kwargs == {"secret_key_path": "/job/secret-key", "port": 5432,
                      "psql_path": "/usr/bin/psql", "deadline_ns": 1000}
    out = capsys.readouterr().out
    assert "credential-registry" not in out and "url" not in out


@pytest.mark.parametrize("argv", [
    [], ["database-drop"], CREATE[:-2], CREATE + ["--port", "1"],
    CREATE[:1] + ["--job-root", "relative"] + CREATE[3:],
    [x if x != "5432" else "0" for x in CREATE],
    [x if x != "5432" else "05432" for x in CREATE],
    [x if x != "1000" else "-1" for x in REGISTRY],
    REGISTRY + ["--secret-key", "value"],
    CREATE[:1] + ["--password", "x"] + CREATE[1:],
])
def test_driver_usage(runners, capsys, argv):
    assert drv.main(argv) == 64 and runners == []


def test_driver_stage_failure_reports_the_code_only(monkeypatch, capsys):
    def refuse(*a, **k):
        raise ds.DatabaseStageError("name_already_exists")
    monkeypatch.setattr(ds, "run_create_stage", refuse)
    assert drv.main(CREATE) == 2
    assert json.loads(capsys.readouterr().out) == {
        "result": "FAILED", "tool": "database-create", "code": "name_already_exists"}


def test_driver_other_errors_never_print_a_traceback(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("postgresql://alrust_x:SECRETVALUE@127.0.0.1:5432/alrust_x")
    monkeypatch.setattr(ds, "run_registry_stage", boom)
    assert drv.main(REGISTRY) == 3
    captured = capsys.readouterr()
    assert "SECRETVALUE" not in captured.out + captured.err
    # Not a FAILED claim: the journal says whether the stage closed.
    assert json.loads(captured.out) == {"result": "UNKNOWN", "tool": "registry",
                                        "code": "driver_error"}


@pytest.mark.parametrize("error", [RuntimeError("SECRETVALUE"), ImportError("SECRETVALUE"),
                                   SyntaxError("SECRETVALUE"), KeyboardInterrupt()])
def test_driver_loader_failure_is_not_run_without_text(runners, monkeypatch, capsys, error):
    """P2B2-SOURCE-R1: setup fails before any stage is opened."""
    def fail():
        raise error
    monkeypatch.setattr(drv, "_modules", fail)
    assert drv.main(CREATE) == 70
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"result": "NOT_RUN", "reason": "driver_setup_failed"}
    assert "SECRETVALUE" not in captured.out + captured.err and runners == []


@pytest.mark.parametrize("name", ["DATABASE_URL", "SECRET_KEY", "PGPASSWORD"])
def test_driver_refuses_a_secret_bearing_environment(runners, monkeypatch, capsys, name):
    monkeypatch.setenv(name, "SECRETVALUE")
    assert drv.main(CREATE) == 70
    out = capsys.readouterr().out
    assert json.loads(out) == {"result": "NOT_RUN", "reason": "secret_in_environment"}
    assert "SECRETVALUE" not in out and runners == []


def test_driver_positive_still_loads_the_real_module(runners, capsys):
    assert drv._modules() is ds
    assert drv.main(CREATE) == 0


def test_run_create_stage_closes_failed_with_the_code(tmp_path, monkeypatch):
    root, digest = _job(tmp_path, monkeypatch)

    def refuse(job_root, journal, **kwargs):
        journal.record("owned_resource", {"path": "/x", "dev": 1, "ino": 2, "type": "dir"})
        raise ds.DatabaseStageError("name_already_exists")
    monkeypatch.setattr(ds, "create_database", refuse)
    with pytest.raises(ds.DatabaseStageError):
        ds.run_create_stage(str(root), profile="scaffold")
    aggregation = sj.aggregate(str(root), ["database-create"])
    assert aggregation["verdict"] == "FAILED"
    last = sj.read_bounded(sj.journal_path(str(root), 0, "database-create"),
                           sj.MAX_JOURNAL).splitlines()[-1]
    assert json.loads(last)["evidence"]["payload"] == {"result": "FAILED",
                                                       "code": "name_already_exists"}


# --- P2B2-TEST-R1: the process harness's run_tool, with a fake owned child ---

import signal as _signal                                   # noqa: E402
import subprocess as _subprocess                           # noqa: E402

import test_tool_supervisor_process as tsp                 # noqa: E402


class FakeProc:
    """An owned Popen stand-in. WAITS lists what successive wait() calls do:
    an int return code, or an exception to raise."""

    def __init__(self, log, waits, signal_error=None, stderr=None, payload=b""):
        self.log, self.waits, self.signal_error = log, list(waits), signal_error
        self.returncode = None
        if stderr is not None:
            stderr.write(payload)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.log.append(("wait", timeout))
        step = self.waits.pop(0)
        if isinstance(step, BaseException):
            raise step
        self.returncode = step
        return step

    def send_signal(self, sig):
        self.log.append(("signal", sig))
        if self.signal_error:
            raise self.signal_error

    def kill(self):
        self.log.append(("kill",))


@pytest.fixture
def harness(tmp_path, monkeypatch):
    log, plan = [], {"waits": [0], "signal_error": None, "payload": b""}
    monkeypatch.setattr(tsp.subprocess, "Popen", lambda args, **kw: FakeProc(
        log, plan["waits"], plan["signal_error"], kw.get("stderr"), plan["payload"]))
    monkeypatch.setattr(os, "kill", lambda *a: log.append(("bare_kill",) + a))

    def run():
        return tsp.run_tool(tmp_path, "a" * 64, tmp_path / "driver.py", "complete", tmp_path)
    return log, plan, run


def test_budgets_cover_deadline_and_teardown():
    assert tsp.TEARDOWN_S * 1e9 == sup.TEARDOWN_BUDGET_NS
    assert tsp.WAIT_S > tsp.TOOL_DEADLINE_S + tsp.TEARDOWN_S


def test_run_tool_positive_reads_bounded_stderr(harness):
    log, plan, run = harness
    plan["payload"] = b"x" * (tsp.MAX_STDERR + 100)
    rc, err = run()
    assert rc == 0 and len(err) == tsp.MAX_STDERR
    assert log == [("wait", tsp.WAIT_S)]


@pytest.mark.parametrize("error", [_subprocess.TimeoutExpired("sup", 51), KeyboardInterrupt()])
def test_run_tool_timeout_or_interrupt_stops_the_owned_supervisor(harness, error):
    log, plan, run = harness
    plan["waits"] = [error, -15]
    with pytest.raises(type(error)):
        run()
    assert log == [("wait", tsp.WAIT_S), ("signal", _signal.SIGTERM),
                   ("wait", tsp.TEARDOWN_S + tsp.MARGIN_S)]


def test_run_tool_kills_only_the_owned_handle_if_teardown_stalls(harness):
    log, plan, run = harness
    plan["waits"] = [_subprocess.TimeoutExpired("sup", 51),
                     _subprocess.TimeoutExpired("sup", 21), -9]
    with pytest.raises(_subprocess.TimeoutExpired):
        run()
    assert log == [("wait", tsp.WAIT_S), ("signal", _signal.SIGTERM),
                   ("wait", tsp.TEARDOWN_S + tsp.MARGIN_S), ("kill",),
                   ("wait", tsp.MARGIN_S)]
    assert not any(e[0] == "bare_kill" for e in log)


def test_run_tool_keeps_the_primary_failure_when_cleanup_fails(harness):
    log, plan, run = harness
    plan["waits"] = [KeyboardInterrupt()]
    plan["signal_error"] = OSError("synthetic signal failure")
    with pytest.raises(BaseExceptionGroup) as group:
        run()
    kinds = [type(e) for e in group.value.exceptions]
    assert kinds == [KeyboardInterrupt, OSError]


def test_run_tool_cleanup_failure_alone_is_raised(harness, monkeypatch):
    log, plan, run = harness
    plan["waits"] = [0]
    monkeypatch.setattr(tsp, "stop_owned", lambda proc: (_ for _ in ()).throw(
        OSError("synthetic cleanup failure")))
    with pytest.raises(OSError, match="synthetic cleanup failure"):
        run()


def test_stop_owned_does_nothing_for_an_exited_child():
    log = []
    proc = FakeProc(log, [0])
    proc.returncode = 0
    tsp.stop_owned(proc)
    assert log == []
