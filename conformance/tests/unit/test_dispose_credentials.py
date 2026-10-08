"""Pure controls for dispose-credentials.py (W5b; S0PLAN-R3). The script is
loaded as a module and main() called on private temporary job roots; it is
never executed as a process. No process, signal or network."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

from alrust_conformance import database_stage as ds

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
spec = importlib.util.spec_from_file_location("dispose_credentials",
                                              SCRIPTS / "dispose-credentials.py")
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)


def run(capsys, argv):
    code = dc.main(argv)
    return code, json.loads(capsys.readouterr().out)


def job(tmp_path, names=("url", "pgpass-postgres-0a1b2c3d", "pgpass-postgres-deadbeef",
                         "psql-stderr-0123abcd-2")):
    root = tmp_path / "job"
    (root / ds.DATABASE_DIR).mkdir(parents=True, mode=0o700)
    for name in names:
        (root / ds.DATABASE_DIR / name).write_text("secret-material-for-the-test\n")
    return root


def test_names_match_the_producer():
    assert dc.DATABASE_DIR == ds.DATABASE_DIR and dc.classify(ds.URL_NAME) == "url"


def test_every_credential_file_is_removed_and_verified(tmp_path, capsys):
    root = job(tmp_path)
    code, out = run(capsys, [str(root)])
    assert (code, out["result"]) == (0, "DISPOSED")
    assert out["removed"] == {"url": 1, "pgpass": 2, "psql_stderr": 1}
    assert out["other_entries"] == 0
    assert list((root / ds.DATABASE_DIR).iterdir()) == []


def test_other_entries_are_kept_and_counted(tmp_path, capsys):
    root = job(tmp_path, names=("url", "notes.txt"))
    code, out = run(capsys, [str(root)])
    assert (code, out["result"], out["other_entries"]) == (0, "DISPOSED", 1)
    assert [p.name for p in (root / ds.DATABASE_DIR).iterdir()] == ["notes.txt"]


def test_an_absent_database_directory_is_none_present(tmp_path, capsys):
    root = tmp_path / "job"
    root.mkdir()
    code, out = run(capsys, [str(root)])
    assert (code, out["result"]) == (0, "NONE_PRESENT")


def test_a_linked_credential_is_unknown_and_its_target_untouched(tmp_path, capsys):
    root = job(tmp_path, names=())
    target = tmp_path / "elsewhere"
    target.write_text("keep")
    os.symlink(target, root / ds.DATABASE_DIR / "url")
    code, out = run(capsys, [str(root)])
    assert (code, out) == (3, {"result": "UNKNOWN", "code": "credential_not_regular"})
    assert target.read_text() == "keep"


def test_a_linked_database_directory_is_refused(tmp_path, capsys):
    root = tmp_path / "job"
    root.mkdir()
    real = tmp_path / "real"
    real.mkdir()
    (real / "url").write_text("x")
    os.symlink(real, root / ds.DATABASE_DIR)
    code, out = run(capsys, [str(root)])
    assert (code, out) == (2, {"result": "REFUSED", "code": "database_dir_invalid"})
    assert (real / "url").exists()


def test_a_failed_unlink_is_unknown(tmp_path, capsys, monkeypatch):
    root = job(tmp_path, names=("url",))

    def refuse(*args, **kwargs):
        raise PermissionError
    monkeypatch.setattr(dc.os, "unlink", refuse)
    code, out = run(capsys, [str(root)])
    assert (code, out) == (3, {"result": "UNKNOWN", "code": "credential_unlink_failed"})


def test_a_credential_that_reappears_is_unknown(tmp_path, capsys, monkeypatch):
    root = job(tmp_path, names=("url",))
    real = os.unlink
    monkeypatch.setattr(dc.os, "unlink", lambda name, dir_fd: None if name == "url" else real(name, dir_fd=dir_fd))
    code, out = run(capsys, [str(root)])
    assert (code, out) == (3, {"result": "UNKNOWN", "code": "credential_still_present"})


@pytest.mark.parametrize("name,label", [
    ("url", "url"), ("pgpass-postgres-0a1b2c3d", "pgpass"),
    ("pgpass-alrust_s0_123_1-ffffffff", "pgpass"), ("psql-stderr-0a1b2c3d-17", "psql_stderr"),
    ("url.bak", None), ("pgpass-postgres-0A1B2C3D", None), ("psql-stderr-x", None), ("URL", None)])
def test_classification(name, label):
    assert dc.classify(name) == label


@pytest.mark.parametrize("argv", [[], ["rel"], ["/a/../b"], ["/a", "/b"]])
def test_usage(capsys, argv):
    assert run(capsys, argv) == (64, {"result": "NOT_RUN", "reason": "usage"})


def test_output_never_carries_a_path_or_content(tmp_path, capsys):
    root = job(tmp_path)
    dc.main([str(root)])
    out = capsys.readouterr().out
    assert str(tmp_path) not in out and "secret-material" not in out


def test_the_script_makes_no_process_or_signal_call():
    source = (SCRIPTS / "dispose-credentials.py").read_text()
    for forbidden in ("subprocess", "os.kill", "os.system", "os.exec", "os.fork", "import signal",
                      "shutil.rmtree"):
        assert forbidden not in source, forbidden
