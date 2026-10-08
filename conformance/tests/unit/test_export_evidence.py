"""Pure controls for export-evidence.py (W5b; S0PLAN-R1, DECISIONS Y24). The
script is loaded as a module and main() called on private temporary
directories; it is never executed as a process. No process, signal or
network."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

from alrust_conformance import database_stage as ds

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
spec = importlib.util.spec_from_file_location("export_evidence", SCRIPTS / "export-evidence.py")
ex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ex)


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


def run(capsys, argv):
    code = ex.main(argv)
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    return code, lines


def test_every_allowlisted_file_is_staged_with_its_manifest(tmp_path, capsys):
    job, out, staging = tree(tmp_path)
    code, lines = run(capsys, [str(job), str(out), str(staging)])
    summary = lines[-1]
    assert (code, summary["result"], summary["absent"]) == (0, "STAGED", [])
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
    code, lines = run(capsys, [str(job), str(out), str(tmp_path / "staging")])
    assert (code, lines) == (2, [{"result": "REFUSED", "code": "nothing_to_stage"}])


@pytest.mark.parametrize("argv", [[], ["/a", "/b"], ["/a", "/b", "rel"], ["/a", "/b/../c", "/d"]])
def test_usage(capsys, argv):
    assert run(capsys, argv) == (64, [{"result": "NOT_RUN", "reason": "usage"}])


def test_output_never_carries_content_or_absolute_paths(tmp_path, capsys):
    job, out, staging = tree(tmp_path)
    ex.main([str(job), str(out), str(staging)])
    text = capsys.readouterr().out
    assert str(tmp_path) not in text and "evidence" not in text.replace('"manifest_sha256"', "")


def test_the_script_makes_no_process_or_signal_call():
    source = (SCRIPTS / "export-evidence.py").read_text()
    for forbidden in ("subprocess", "os.kill", "os.system", "os.exec", "os.fork", "import signal",
                      "urllib", "socket"):
        assert forbidden not in source, forbidden
