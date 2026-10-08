"""Pure controls for run-stage.py (executing workflow W2).

Synthetic jobs from test_finalizer_orchestration.build with absent synthetic
generations; receipts are written by exit_fixtures, and the real exit
verifiers run in-process. The drop runner either refuses at its real gate
before any SQL, or is replaced by a spy. No process, socket or database."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

import exit_fixtures as xf
from alrust_conformance import database_stage as ds
from alrust_conformance import stage_journal as sj
from test_finalizer_orchestration import STAGES, build

SYNTHETIC_SUPERVISOR = (4194293, 333)
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run-stage.py"
SECRET_TEXT = "synthetic-diagnostic-value"


def _script():
    spec = importlib.util.spec_from_file_location("run_stage", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rs = _script()


@pytest.fixture
def job(tmp_path, monkeypatch):
    if os.path.exists("/proc/%d" % SYNTHETIC_SUPERVISOR[0]):
        pytest.skip("synthetic ended PID is in use on this host")
    for name in rs.SECRET_VARIABLES:
        monkeypatch.delenv(name, raising=False)

    def make(upto):
        root = build(tmp_path, monkeypatch, upto=upto, generation=SYNTHETIC_SUPERVISOR)
        pins = tmp_path / "pins.json"
        pins.write_text(json.dumps(xf.EXPECTED))
        return root, pins
    return make


def run(argv, capsys):
    code = rs.main(argv)
    line = json.loads(capsys.readouterr().out)
    return code, line


def closing(root, index, name):
    data = Path(sj.journal_path(str(root), index, name)).read_bytes()
    return json.loads(data.splitlines()[-1])["evidence"]["payload"]


# --- the exit stages, with the real verifiers -----------------------------------------

def test_entrypoint_exit_complete(job, capsys):
    root, pins = job("entrypoint")
    xf.entrypoint_verdict(root, STAGES.index("entrypoint"))
    argv = ["entrypoint-exit", "--job-root", str(root), "--prefix",
            ",".join(STAGES[:STAGES.index("entrypoint") + 1]), "--expected-pins", str(pins)]
    assert run(argv, capsys) == (0, {"result": "COMPLETE", "stage": "entrypoint-exit"})
    index = STAGES.index("entrypoint-exit")
    assert closing(root, index, "entrypoint-exit") == {"result": "COMPLETE"}


def test_entrypoint_exit_without_receipts_is_failed_with_the_verdict(job, capsys):
    root, pins = job("entrypoint")
    argv = ["entrypoint-exit", "--job-root", str(root), "--prefix",
            ",".join(STAGES[:STAGES.index("entrypoint") + 1]), "--expected-pins", str(pins)]
    code, line = run(argv, capsys)
    assert code == 2 and line == {"result": "FAILED", "stage": "entrypoint-exit",
                                  "code": "entrypoint_exit_unproven"}
    index = STAGES.index("entrypoint-exit")
    assert closing(root, index, "entrypoint-exit")["result"] == "FAILED"


def test_tool_exit_complete(job, capsys):
    root, pins = job("database-create")
    xf.tool_verdict(root, "database-create", 0)
    argv = ["tool-exit", "--job-root", str(root), "--prefix", "database-create",
            "--tool", "database-create", "--expected-pins", str(pins)]
    assert run(argv, capsys) == (0, {"result": "COMPLETE", "stage": "tool-exit"})
    assert closing(root, 1, "database-create-exit") == {"result": "COMPLETE"}


def test_a_wrong_prefix_is_a_fixed_refusal_before_any_journal(job, capsys):
    root, pins = job("launch")
    argv = ["entrypoint-exit", "--job-root", str(root), "--prefix",
            ",".join(STAGES[:STAGES.index("launch") + 1]), "--expected-pins", str(pins)]
    assert run(argv, capsys) == (2, {"result": "REFUSED", "stage": "entrypoint-exit",
                                     "code": "entrypoint_exit_prefix_invalid"})
    assert not Path(sj.journal_path(str(root), STAGES.index("launch") + 1,
                                    "entrypoint-exit")).exists()


def test_a_tool_exit_prefix_mismatch_is_a_fixed_refusal_before_any_journal(job, capsys):
    root, pins = job("database-create")
    argv = ["tool-exit", "--job-root", str(root), "--prefix", "database-create",
            "--tool", "registry", "--expected-pins", str(pins)]
    assert run(argv, capsys) == (2, {"result": "REFUSED", "stage": "tool-exit",
                                     "code": "tool_exit_prefix_invalid"})
    assert not Path(sj.journal_path(str(root), 1, "registry-exit")).exists()


@pytest.mark.parametrize("command", ["entrypoint-exit", "tool-exit"])
def test_a_journal_error_is_never_claimed_failed(job, capsys, monkeypatch, command):
    # The journal cannot close: the line says UNKNOWN journal_failed, and
    # leaves the closure to the journal (compare W1's journal_failed).
    root, pins = job("entrypoint" if command == "entrypoint-exit" else "database-create")

    def broken_exit(self, *exc):
        raise sj.JournalError("journal_close_failed")
    monkeypatch.setattr(sj.StageJournal, "__exit__", broken_exit)
    if command == "entrypoint-exit":
        argv = ["entrypoint-exit", "--job-root", str(root), "--prefix",
                ",".join(STAGES[:STAGES.index("entrypoint") + 1]), "--expected-pins", str(pins)]
    else:
        argv = ["tool-exit", "--job-root", str(root), "--prefix", "database-create",
                "--tool", "database-create", "--expected-pins", str(pins)]
    assert run(argv, capsys) == (3, {"result": "UNKNOWN", "stage": command,
                                     "code": "journal_failed"})


def test_an_unlisted_journal_code_is_not_passed_through(job, capsys, monkeypatch):
    root, pins = job("database-create")

    def refuse(*a, **k):
        raise sj.JournalError("journal_set_mismatch")
    monkeypatch.setattr(ds, "run_drop_stage", refuse)
    argv = (["database-drop", "--job-root", str(root), "--prefix",
             "database-create,database-create-exit", "--expected-pins", str(pins)] + DROP_ARGS)
    assert run(argv, capsys) == (3, {"result": "UNKNOWN", "stage": "database-drop",
                                     "code": "journal_failed"})


# --- the drop stage ----------------------------------------------------------------------

DROP_ARGS = ["--port", "5432", "--superuser", "postgres", "--public-superuser-password",
             "postgres", "--psql", "/usr/bin/psql", "--deadline-monotonic-ns", "1000"]


def test_drop_refuses_at_the_real_gate_before_any_sql(job, capsys, monkeypatch):
    # No-launch prefix while launch evidence exists: the real gate refuses
    # (aggregate's exact journal set) and no Psql is ever constructed.
    root, pins = job("launch-confirm")
    monkeypatch.setattr(ds, "Psql", lambda *a, **k: pytest.fail("SQL attempted"))
    argv = (["database-drop", "--job-root", str(root), "--prefix",
             "database-create,database-create-exit", "--expected-pins", str(pins)] + DROP_ARGS)
    assert run(argv, capsys) == (2, {"result": "FAILED", "stage": "database-drop",
                                     "code": "no_launch_prefix_not_closed"})
    assert closing(root, 2, "database-drop") == {"result": "FAILED",
                                                  "code": "no_launch_prefix_not_closed"}


@pytest.mark.parametrize("prefix,launched", [
    ("database-create,database-create-exit", False),
    (",".join(STAGES[:STAGES.index("stop") + 1]), True),
    (",".join(STAGES[:STAGES.index("registry-exit") + 1]), True),
])
def test_drop_takes_launched_from_the_prefix_never_a_flag(job, capsys, monkeypatch,
                                                          prefix, launched):
    root, pins = job("database-create")
    calls = []
    monkeypatch.setattr(ds, "run_drop_stage", lambda *a, **k: calls.append((a, k)) or "COMPLETE")
    argv = (["database-drop", "--job-root", str(root), "--prefix", prefix,
             "--expected-pins", str(pins)] + DROP_ARGS)
    assert run(argv, capsys) == (0, {"result": "COMPLETE", "stage": "database-drop"})
    [(args, kwargs)] = calls
    assert args == (str(root), prefix.split(","))
    assert kwargs == {"launched": launched, "expected": xf.EXPECTED, "port": 5432,
                      "superuser": "postgres", "superuser_password": "postgres",
                      "psql_path": "/usr/bin/psql", "deadline_ns": 1000}


def test_a_drop_prefix_without_create_is_usage_with_no_journal_or_sql(job, capsys,
                                                                       monkeypatch):
    # W2-R1: the runner would refuse this before opening any journal, so
    # the CLI rejects it as usage and never reaches the runner.
    root, pins = job("launch")
    monkeypatch.setattr(ds, "run_drop_stage", lambda *a, **k: pytest.fail("runner reached"))
    monkeypatch.setattr(ds, "Psql", lambda *a, **k: pytest.fail("SQL attempted"))
    before = sorted(os.listdir(root / sj.JOURNAL_DIR))
    argv = (["database-drop", "--job-root", str(root), "--prefix", "launch",
             "--expected-pins", str(pins)] + DROP_ARGS)
    assert run(argv, capsys) == (64, {"result": "NOT_RUN", "reason": "usage"})
    assert sorted(os.listdir(root / sj.JOURNAL_DIR)) == before


@pytest.mark.parametrize("code", ["drop_prefix_invalid", "drop_path_mismatch"])
def test_a_drop_pre_open_refusal_is_never_claimed_failed(job, capsys, monkeypatch, code):
    # Through the real runner, drop_path_mismatch is unreachable from this
    # CLI and drop_prefix_invalid is usage; if either still arrives, it is
    # REFUSED (no journal), never FAILED (a closed stage).
    root, pins = job("database-create")

    def refuse(*a, **k):
        raise ds.DatabaseStageError(code)
    monkeypatch.setattr(ds, "run_drop_stage", refuse)
    argv = (["database-drop", "--job-root", str(root), "--prefix",
             "database-create,database-create-exit", "--expected-pins", str(pins)] + DROP_ARGS)
    assert run(argv, capsys) == (2, {"result": "REFUSED", "stage": "database-drop",
                                     "code": code})


def test_a_post_open_drop_failure_keeps_its_fixed_code(job, capsys, monkeypatch):
    root, pins = job("database-create")

    def refuse(*a, **k):
        raise ds.DatabaseStageError("drop_allocation_changed")
    monkeypatch.setattr(ds, "run_drop_stage", refuse)
    argv = (["database-drop", "--job-root", str(root), "--prefix",
             "database-create,database-create-exit", "--expected-pins", str(pins)] + DROP_ARGS)
    assert run(argv, capsys) == (2, {"result": "FAILED", "stage": "database-drop",
                                     "code": "drop_allocation_changed"})


# --- setup, usage and unexpected errors ---------------------------------------------------

def test_a_secret_in_the_environment_is_refused_before_anything(job, capsys, monkeypatch):
    root, pins = job("database-create")
    monkeypatch.setenv("DATABASE_URL", SECRET_TEXT)
    monkeypatch.setattr(ds, "run_drop_stage", lambda *a, **k: pytest.fail("ran"))
    argv = (["database-drop", "--job-root", str(root), "--prefix",
             "database-create,database-create-exit", "--expected-pins", str(pins)] + DROP_ARGS)
    code, line = run(argv, capsys)
    assert code == 70 and line == {"result": "NOT_RUN", "reason": "secret_in_environment"}


@pytest.mark.parametrize("pins_content", [None, b"[]", b"{not json", "link"])
def test_bad_pins_are_setup_failures(job, capsys, tmp_path, pins_content):
    root, _ = job("database-create")
    pins = tmp_path / "bad-pins.json"
    if pins_content == "link":
        pins.symlink_to(tmp_path / "pins.json")
    elif pins_content is not None:
        pins.write_bytes(pins_content)
    argv = ["tool-exit", "--job-root", str(root), "--prefix", "database-create", "--tool",
            "database-create", "--expected-pins", str(pins)]
    assert run(argv, capsys) == (70, {"result": "NOT_RUN", "reason": "setup_failed"})


@pytest.mark.parametrize("argv", [
    [], ["finalize", "--job-root", "/j"],
    ["tool-exit", "--job-root", "/j", "--prefix", "database-create", "--expected-pins", "/p"],
    ["entrypoint-exit", "--job-root", "rel", "--prefix", "entrypoint", "--expected-pins", "/p"],
    ["entrypoint-exit", "--job-root", "/j", "--prefix", "Entrypoint", "--expected-pins", "/p"],
    ["entrypoint-exit", "--job-root", "/j", "--prefix", "", "--expected-pins", "/p"],
    ["entrypoint-exit", "--job-root", "/j", "--job-root", "/j", "--expected-pins", "/p"],
    ["database-drop", "--job-root", "/j", "--prefix", "database-create", "--expected-pins",
     "/p", "--port", "0", "--superuser", "postgres", "--public-superuser-password", "postgres",
     "--psql", "/usr/bin/psql", "--deadline-monotonic-ns", "1"],
    ["database-drop", "--job-root", "/j", "--prefix", "database-create", "--expected-pins",
     "/p", "--port", "5432", "--superuser", "postgres", "--public-superuser-password",
     "postgres", "--psql", "/usr/bin/psql", "--deadline-monotonic-ns", "-1"],
])
def test_usage(capsys, argv):
    assert run(argv, capsys) == (64, {"result": "NOT_RUN", "reason": "usage"})


def test_an_unexpected_error_is_a_fixed_unknown(job, capsys, monkeypatch):
    root, pins = job("database-create")

    def boom(*a, **k):
        raise RuntimeError(SECRET_TEXT)
    monkeypatch.setattr(ds, "run_drop_stage", boom)
    argv = (["database-drop", "--job-root", str(root), "--prefix",
             "database-create,database-create-exit", "--expected-pins", str(pins)] + DROP_ARGS)
    code, line = run(argv, capsys)
    assert code == 3 and line == {"result": "UNKNOWN", "stage": "database-drop",
                                  "code": "runner_error"}
    assert SECRET_TEXT not in json.dumps(line)


def test_the_script_makes_no_direct_signal_or_process_call():
    # W2-N1: direct calls only. The drop runner this script delegates to
    # runs psql through a bounded subprocess after its gate admits, so the
    # CLI is not process-free; that classification belongs to W5.
    source = SCRIPT.read_text()
    for forbidden in ("os.kill", "killpg", "pidfd_send_signal", "subprocess", "os.system",
                      "os.fork", "os.exec"):
        assert forbidden not in source, forbidden
