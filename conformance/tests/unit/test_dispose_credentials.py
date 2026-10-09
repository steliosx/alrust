"""Pure controls for dispose-credentials.py (W5b; S0PLAN-R3, W5B-R1/R2).
The script is loaded as a module and main() called on private temporary job
roots, with its custody gate replaced by a stand-in (the gate itself has
its own controls in test_custody.py); it is never executed as a process.
No process, signal or network."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

from alrust_conformance import custody
from alrust_conformance import database_stage as ds

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
spec = importlib.util.spec_from_file_location("dispose_credentials",
                                              SCRIPTS / "dispose-credentials.py")
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)
NAMES = ("url", "pgpass-postgres-0a1b2c3d", "pgpass-postgres-deadbeef", "psql-stderr-0123abcd-2")


class Gate:
    """A stand-in custody module: proven with the real directory identity,
    or refusing with a fixed code."""
    CustodyError = custody.CustodyError

    def __init__(self, refuse=None, identity="real"):
        self.refuse, self.identity, self.calls = refuse, identity, []

    def holder_proven(self, job_root, expected):
        self.calls.append(("holder_proven", expected))
        if self.refuse:
            raise custody.CustodyError(self.refuse)
        if self.identity == "real":
            info = os.stat(os.path.join(job_root, ds.DATABASE_DIR))
            return {"dev": info.st_dev, "ino": info.st_ino}
        return self.identity

    def not_launched(self, job_root):
        self.calls.append(("not_launched",))
        if self.refuse:
            raise custody.CustodyError(self.refuse)


@pytest.fixture
def gate(monkeypatch):
    def install(**kwargs):
        g = Gate(**kwargs)
        monkeypatch.setattr(dc, "modules", lambda: g)
        return g
    return install


def job(tmp_path, names=NAMES):
    root = tmp_path / "job"
    (root / ds.DATABASE_DIR).mkdir(parents=True, mode=0o700)
    for name in names:
        (root / ds.DATABASE_DIR / name).write_text("secret-material-for-the-test\n")
    pins = tmp_path / "pins.json"
    pins.write_text('{"entrypoint": {}, "tools": {}}')
    return root, pins


def run(capsys, argv):
    code = dc.main(argv)
    return code, json.loads(capsys.readouterr().out)


def proven(root, pins):
    return [str(root), "--attempted", "1", "--expected-pins", str(pins)]


def test_names_match_the_producer():
    assert dc.DATABASE_DIR == ds.DATABASE_DIR and dc.classify(ds.URL_NAME) == "url"


def test_a_proven_holder_disposes_every_credential_file(tmp_path, capsys, gate):
    root, pins = job(tmp_path)
    g = gate()
    code, out = run(capsys, proven(root, pins))
    assert (code, out["result"], out["holder"]) == (0, "DISPOSED", "PROVEN")
    assert out["removed"] == {"url": 1, "pgpass": 2, "psql_stderr": 1}
    assert list((root / ds.DATABASE_DIR).iterdir()) == []
    assert g.calls == [("holder_proven", {"entrypoint": {}, "tools": {}})]


@pytest.mark.parametrize("code_", ["holder_unproven", "custody_prefix_not_closed",
                                   "custody_journals_unexpected", "launch_evidence_present"])
def test_an_unproven_holder_refuses_and_touches_nothing(tmp_path, capsys, gate, code_):
    root, pins = job(tmp_path)
    gate(refuse=code_)
    code, out = run(capsys, proven(root, pins))
    assert (code, out) == (2, {"result": "REFUSED", "code": code_})
    assert sorted(p.name for p in (root / ds.DATABASE_DIR).iterdir()) == sorted(NAMES)


@pytest.mark.parametrize("identity", [None, {"dev": 0, "ino": 0}])
def test_a_directory_that_is_not_the_recorded_one_refuses(tmp_path, capsys, gate, identity):
    root, pins = job(tmp_path)
    gate(identity=identity)
    code, out = run(capsys, proven(root, pins))
    assert (code, out) == (2, {"result": "REFUSED", "code": "database_identity"})
    assert sorted(p.name for p in (root / ds.DATABASE_DIR).iterdir()) == sorted(NAMES)


def test_not_launched_with_no_database_is_none_present(tmp_path, capsys, gate):
    root = tmp_path / "job"
    root.mkdir()
    g = gate()
    code, out = run(capsys, [str(root), "--attempted", "0"])
    assert (code, out["result"], out["holder"]) == (0, "NONE_PRESENT", "NOT_LAUNCHED")
    assert g.calls == [("not_launched",)]


def test_not_launched_refusal_touches_nothing(tmp_path, capsys, gate):
    root, _ = job(tmp_path)
    gate(refuse="custody_database_present")
    code, out = run(capsys, [str(root), "--attempted", "0"])
    assert (code, out) == (2, {"result": "REFUSED", "code": "custody_database_present"})
    assert sorted(p.name for p in (root / ds.DATABASE_DIR).iterdir()) == sorted(NAMES)


def test_other_entries_are_kept_and_counted(tmp_path, capsys, gate):
    root, pins = job(tmp_path, names=("url", "notes.txt"))
    gate()
    code, out = run(capsys, proven(root, pins))
    assert (code, out["result"], out["other_entries"]) == (0, "DISPOSED", 1)
    assert [p.name for p in (root / ds.DATABASE_DIR).iterdir()] == ["notes.txt"]


def test_a_linked_credential_is_unknown_and_its_target_untouched(tmp_path, capsys, gate):
    root, pins = job(tmp_path, names=())
    target = tmp_path / "elsewhere"
    target.write_text("keep")
    os.symlink(target, root / ds.DATABASE_DIR / "url")
    gate()
    code, out = run(capsys, proven(root, pins))
    assert (code, out) == (3, {"result": "UNKNOWN", "code": "credential_not_regular"})
    assert target.read_text() == "keep"


def test_a_linked_database_directory_is_refused(tmp_path, capsys, gate):
    root = tmp_path / "job"
    root.mkdir()
    real = tmp_path / "real"
    real.mkdir()
    (real / "url").write_text("x")
    os.symlink(real, root / ds.DATABASE_DIR)
    pins = tmp_path / "pins.json"
    pins.write_text("{}")
    gate(identity={"dev": 0, "ino": 0})
    code, out = run(capsys, proven(root, pins))
    assert (code, out) == (2, {"result": "REFUSED", "code": "database_dir_invalid"})
    assert (real / "url").exists()


def test_a_failed_unlink_is_unknown(tmp_path, capsys, gate, monkeypatch):
    root, pins = job(tmp_path, names=("url",))
    gate()

    def refuse(*args, **kwargs):
        raise PermissionError
    monkeypatch.setattr(dc.os, "unlink", refuse)
    code, out = run(capsys, proven(root, pins))
    assert (code, out) == (3, {"result": "UNKNOWN", "code": "credential_unlink_failed"})


def test_a_credential_that_reappears_is_unknown(tmp_path, capsys, gate, monkeypatch):
    root, pins = job(tmp_path, names=("url",))
    gate()
    monkeypatch.setattr(dc.os, "unlink", lambda name, dir_fd: None)
    code, out = run(capsys, proven(root, pins))
    assert (code, out) == (3, {"result": "UNKNOWN", "code": "credential_still_present"})


def test_unreadable_pins_refuse_before_the_gate(tmp_path, capsys, gate):
    root, _ = job(tmp_path)
    g = gate()
    code, out = run(capsys, proven(root, tmp_path / "absent.json"))
    assert (code, out) == (2, {"result": "REFUSED", "code": "pins_unreadable"})
    assert g.calls == []


@pytest.mark.parametrize("name,label", [
    ("url", "url"), ("pgpass-postgres-0a1b2c3d", "pgpass"),
    ("pgpass-alrust_s0_123_1-ffffffff", "pgpass"), ("psql-stderr-0a1b2c3d-17", "psql_stderr"),
    ("url.bak", None), ("pgpass-postgres-0A1B2C3D", None), ("psql-stderr-x", None), ("URL", None)])
def test_classification(name, label):
    assert dc.classify(name) == label


@pytest.mark.parametrize("argv", [[], ["/j"], ["rel", "--attempted", "0"], ["/j", "--attempted", "1"],
                                  ["/j", "--attempted", "2"], ["/j", "--attempted", "1", "--expected-pins", "rel"],
                                  ["/a/../b", "--attempted", "0"]])
def test_usage(capsys, argv):
    assert run(capsys, argv) == (64, {"result": "NOT_RUN", "reason": "usage"})


def test_output_never_carries_a_path_or_content(tmp_path, capsys, gate):
    root, pins = job(tmp_path)
    gate()
    dc.main(proven(root, pins))
    out = capsys.readouterr().out
    assert str(tmp_path) not in out and "secret-material" not in out


def test_the_script_gates_through_the_custody_module_and_makes_no_process_call():
    source = (SCRIPTS / "dispose-credentials.py").read_text()
    assert "custody.holder_proven(job_root, expected)" in source
    assert "custody.not_launched(job_root)" in source
    for forbidden in ("subprocess", "os.kill", "os.system", "os.exec", "os.fork", "import signal",
                      "shutil.rmtree"):
        assert forbidden not in source, forbidden
