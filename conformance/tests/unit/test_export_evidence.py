"""Pure controls for export-evidence.py (W5b; S0PLAN-R1, W5B-R1/R2/R3,
DECISIONS Y24). The script is loaded as a module and main() called on
private temporary directories, with its custody gate replaced by a
stand-in (the gate has its own controls in test_custody.py); it is never
executed as a process. No process, signal or network."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

from alrust_conformance import custody
from alrust_conformance import database_stage as ds

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
spec = importlib.util.spec_from_file_location("export_evidence", SCRIPTS / "export-evidence.py")
ex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ex)


class Gate:
    CustodyError = custody.CustodyError

    def __init__(self, refuse=None):
        self.refuse, self.calls = refuse, []

    def holder_proven(self, job_root, expected):
        self.calls.append("holder_proven")
        if self.refuse:
            raise custody.CustodyError(self.refuse)

    def not_launched(self, job_root):
        self.calls.append("not_launched")
        if self.refuse:
            raise custody.CustodyError(self.refuse)


@pytest.fixture(autouse=True)
def gate(monkeypatch):
    holder = {}

    def install(**kwargs):
        holder["gate"] = Gate(**kwargs)
        monkeypatch.setattr(ex, "modules", lambda: holder["gate"])
        return holder["gate"]
    install()
    return install


def tree(tmp_path, credentials=()):
    job, out = tmp_path / "job", tmp_path / "out"
    for rel in ex.JOB_FILES:
        (job / rel).parent.mkdir(parents=True, exist_ok=True)
        (job / rel).write_text('{"evidence": "%s"}\n' % rel)
    out.mkdir()
    for rel in ex.OUT_FILES:
        (out / rel).write_text('{"result": "COMPLETE"}\n')
    (job / ds.DATABASE_DIR).mkdir(mode=0o700)
    for name in credentials:
        (job / ds.DATABASE_DIR / name).write_text("x")
    return job, out, tmp_path / "staging"


def args(job, out, staging, attempted=1):
    pins = out / "expected-pins.json"
    tail = ["--attempted", "1", "--expected-pins", str(pins)] if attempted else ["--attempted", "0"]
    return [str(job), str(out), str(staging)] + tail


def run(capsys, argv):
    if len(argv) == 3:
        argv = args(Path(argv[0]), Path(argv[1]), Path(argv[2]))
    code = ex.main(argv)
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    return code, lines


def test_every_allowlisted_file_is_staged_with_its_manifest(tmp_path, capsys):
    job, out, staging = tree(tmp_path)
    code, lines = run(capsys, [str(job), str(out), str(staging)])
    summary = lines[-1]
    assert (code, summary["result"], summary["state"], summary["absent"]) == (
        0, "STAGED", "HOLDER_PROVEN", [])
    assert summary["files"] == len(ex.JOB_FILES) + len(ex.OUT_FILES) == len(lines) - 1
    manifest = (staging / "manifest.json").read_bytes()
    assert summary["manifest_sha256"] == hashlib.sha256(manifest).hexdigest()
    assert json.loads(manifest) == lines[:-1]
    for line in lines[:-1]:
        staged = staging / line["file"].replace("/", "__")
        assert hashlib.sha256(staged.read_bytes()).hexdigest() == line["sha256"]
        assert oct(staged.stat().st_mode & 0o777) == "0o600"
    assert oct(staging.stat().st_mode & 0o777) == "0o700"


@pytest.mark.parametrize("name", ["url", "pgpass-postgres-0a1b2c3d", "psql-stderr-0a1b2c3d-1"])
def test_a_remaining_credential_refuses_before_staging(tmp_path, capsys, name):
    job, out, staging = tree(tmp_path, credentials=(name,))
    code, lines = run(capsys, [str(job), str(out), str(staging)])
    assert (code, lines) == (2, [{"result": "REFUSED", "code": "credentials_present"}])
    assert not staging.exists()


@pytest.mark.parametrize("marker", [b"postgresql://u:p@127.0.0.1:5432/d", b"SCRAM-SHA-256$4096:x",
                                    b"127.0.0.1:5432:*:postgres:postgres", b"PGPASSWORD=x"])
def test_a_credential_marker_refuses_the_whole_export(tmp_path, capsys, marker):
    job, out, staging = tree(tmp_path)
    (job / "journal" / "00-database-create.jsonl").write_bytes(b'{"x": "' + marker + b'"}\n')
    code, lines = run(capsys, [str(job), str(out), str(staging)])
    assert (code, lines) == (2, [{"result": "REFUSED", "code": "credential_marker"}])
    assert not staging.exists()


def test_absent_files_are_reported_not_invented(tmp_path, capsys):
    job, out, staging = tree(tmp_path)
    (job / "tools" / "00-database-create" / "terminal.json").unlink()
    (out / "drop.out").unlink()
    code, lines = run(capsys, [str(job), str(out), str(staging)])
    assert code == 0
    assert lines[-1]["absent"] == ["job/tools/00-database-create/terminal.json", "out/drop.out"]


def test_a_linked_evidence_file_refuses(tmp_path, capsys):
    job, out, staging = tree(tmp_path)
    target = tmp_path / "elsewhere"
    target.write_text("{}")
    (job / "core-identity.json").unlink()
    os.symlink(target, job / "core-identity.json")
    code, lines = run(capsys, [str(job), str(out), str(staging)])
    assert (code, lines) == (2, [{"result": "REFUSED", "code": "evidence_unreadable"}])


def test_a_linked_parent_directory_refuses(tmp_path, capsys):
    job, out, staging = tree(tmp_path)
    real = tmp_path / "realjournal"
    os.rename(job / "journal", real)
    os.symlink(real, job / "journal")
    code, lines = run(capsys, [str(job), str(out), str(staging)])
    assert (code, lines) == (2, [{"result": "REFUSED", "code": "evidence_unreadable"}])


def test_an_oversized_file_refuses(tmp_path, capsys):
    job, out, staging = tree(tmp_path)
    (out / "core.out").write_bytes(b"#" * (ex.MAX_BYTES + 1))
    code, lines = run(capsys, [str(job), str(out), str(staging)])
    assert (code, lines) == (2, [{"result": "REFUSED", "code": "evidence_unreadable"}])


def test_an_existing_staging_directory_refuses(tmp_path, capsys):
    job, out, staging = tree(tmp_path)
    staging.mkdir()
    code, lines = run(capsys, [str(job), str(out), str(staging)])
    assert (code, lines) == (2, [{"result": "REFUSED", "code": "staging_unavailable"}])


def test_nothing_to_stage_refuses(tmp_path, capsys):
    job, out = tmp_path / "job", tmp_path / "out"
    job.mkdir()
    out.mkdir()
    code, lines = run(capsys, args(job, out, tmp_path / "staging", attempted=0))
    assert (code, lines) == (2, [{"result": "REFUSED", "code": "nothing_to_stage"}])


@pytest.mark.parametrize("argv", [[], ["/a", "/b"], ["/a", "/b", "rel", "--attempted", "0"],
                                  ["/a", "/b/../c", "/d", "--attempted", "0"],
                                  ["/a", "/b", "/c", "--attempted", "1"],
                                  ["/a", "/b", "/c", "--attempted", "2"],
                                  ["/a", "/b", "/c", "--attempted", "1", "--expected-pins", "rel"]])
def test_usage(capsys, argv):
    assert ex.main(argv) == 64
    assert json.loads(capsys.readouterr().out) == {"result": "NOT_RUN", "reason": "usage"}


@pytest.mark.parametrize("code_", ["holder_unproven", "custody_prefix_not_closed", "launch_evidence_present"])
def test_an_unproven_holder_refuses_and_stages_nothing(tmp_path, capsys, gate, code_):
    job, out, staging = tree(tmp_path)
    gate(refuse=code_)
    code, lines = run(capsys, [str(job), str(out), str(staging)])
    assert (code, lines) == (2, [{"result": "REFUSED", "code": code_}])
    assert not staging.exists()


def test_no_create_attempted_stages_the_early_evidence(tmp_path, capsys, gate):
    job, out = tmp_path / "job", tmp_path / "out"
    job.mkdir()
    out.mkdir()
    (job / "core-identity.json").write_text('{"core": 1}\n')
    (job / "job-deadline.json").write_text('{"j": 1}\n')
    for name in ("core.out", "deadline-fix.out", "plan.out", "create-pin.out"):
        (out / name).write_text('{"result": "x"}\n')
    g = gate()
    code, lines = run(capsys, args(job, out, tmp_path / "staging", attempted=0))
    assert g.calls == ["not_launched"]
    assert (code, lines[-1]["result"], lines[-1]["state"]) == (0, "STAGED", "NO_CREATE_ATTEMPTED")
    assert lines[-1]["files"] == 6


def test_no_create_attempted_refusal_stages_nothing(tmp_path, capsys, gate):
    job, out, staging = tree(tmp_path)
    gate(refuse="custody_journal_present")
    code, lines = run(capsys, args(job, out, staging, attempted=0))
    assert (code, lines) == (2, [{"result": "REFUSED", "code": "custody_journal_present"}])
    assert not staging.exists()


def test_a_short_manifest_write_is_completed(tmp_path, capsys, monkeypatch):
    job, out, staging = tree(tmp_path)
    real = os.write
    monkeypatch.setattr(ex.os, "write", lambda fd, data: real(fd, bytes(data[:7])))
    code, lines = run(capsys, [str(job), str(out), str(staging)])
    assert (code, lines[-1]["result"]) == (0, "STAGED")
    manifest = (staging / "manifest.json").read_bytes()
    assert hashlib.sha256(manifest).hexdigest() == lines[-1]["manifest_sha256"]


def test_a_zero_byte_write_is_unknown_and_never_staged(tmp_path, capsys, monkeypatch):
    job, out, staging = tree(tmp_path)
    monkeypatch.setattr(ex.os, "write", lambda fd, data: 0)
    code, lines = run(capsys, [str(job), str(out), str(staging)])
    assert (code, lines) == (3, [{"result": "UNKNOWN", "code": "staging_write_failed"}])


def test_a_staged_file_that_reads_back_different_is_unknown(tmp_path, capsys, monkeypatch):
    job, out, staging = tree(tmp_path)
    real = ex._read

    def tampered(fd, rel):
        data = real(fd, rel)
        if rel == "manifest.json":
            return data + b" "
        return data
    monkeypatch.setattr(ex, "_read", tampered)
    code, lines = run(capsys, [str(job), str(out), str(staging)])
    assert (code, lines) == (3, [{"result": "UNKNOWN", "code": "staging_verify_failed"}])


def test_output_never_carries_content_or_absolute_paths(tmp_path, capsys):
    job, out, staging = tree(tmp_path)
    ex.main([str(job), str(out), str(staging)])
    text = capsys.readouterr().out
    assert str(tmp_path) not in text and "evidence" not in text.replace('"manifest_sha256"', "")


def test_the_script_gates_through_the_custody_module_and_makes_no_process_call():
    source = (SCRIPTS / "export-evidence.py").read_text()
    assert "custody.holder_proven(job_root, expected)" in source
    assert "custody.not_launched(job_root)" in source
    for forbidden in ("subprocess", "os.kill", "os.system", "os.exec", "os.fork", "import signal",
                      "urllib", "socket"):
        assert forbidden not in source, forbidden
