"""Offline controls for the finalizer orchestration and the failure record.

Each test builds a complete synthetic job (core identity, stage journals,
receipts, registry, candidate and owned resources) in a private temporary
directory. No process, network, database or reference server.
"""
import hashlib
import json
import os
from pathlib import Path

import pytest

from alrust_conformance import finalizer as fz
from alrust_conformance import stage_journal as sj

GIT = "a" * 40
SHA = "b" * 64
ENV = {"GITHUB_REPOSITORY": "steliosx/alrust", "GITHUB_RUN_ID": "123",
       "GITHUB_RUN_ATTEMPT": "1", "GITHUB_JOB": "profile",
       "ImageOS": "ubuntu24", "ImageVersion": "20261001.1"}
STAGES = ["launch", "entrypoint", "stop", "registry", "database"]
ROLE_KEY = "SYNTHETIC-" + "0f" * 24
SIGNING = "synthetic-signing-key-" + "c" * 32
DB_URL = "postgres://alrust:" + "d" * 32 + "@127.0.0.1:5432/alrust_scaffold_1_1"


@pytest.fixture(autouse=True)
def unseal_after(tmp_path):
    """Sealed exports are 0500/0400 by design; make them removable again so
    the temporary directory can be cleaned up after each test."""
    yield
    for directory in tmp_path.glob("**/export"):
        if directory.is_dir() and not directory.is_symlink():
            directory.chmod(0o700)
            for entry in directory.iterdir():
                if entry.is_file() and not entry.is_symlink():
                    entry.chmod(0o600)


def stat_record(path, kind):
    info = os.lstat(path)
    return {"path": str(path), "dev": info.st_dev, "ino": info.st_ino, "type": kind}


def build(tmp_path, monkeypatch, *, report=b'{"result": "passed"}\n',
          stop_change=None, database_change=None, candidate_change=None,
          extra_records=(), registry_secrets=None, fail_stage=None,
          terminal_change=None, obtained_change=None, leaky_dir=False):
    monkeypatch.setattr(sj, "EXTERNAL_CONFIG", str(tmp_path / "no-alertad.conf"))
    job = tmp_path / "job"
    job.mkdir(mode=0o700)
    sj.write_core_identity(str(job), profile="scaffold", event_head_sha=GIT,
                           checkout_sha=GIT, reference_sha=GIT, reference_pin=GIT,
                           lock_sha256={"l": SHA}, lock_pins={"l": SHA},
                           manifest_sha256=SHA, manifest_pin=SHA, env=dict(ENV))
    core = json.loads((job / "core-identity.json").read_text())
    (job / "venv" / "lib").mkdir(parents=True)
    (job / "venv" / "lib" / "m.py").write_text("x")
    (job / "settings.py").write_text("DEBUG = False\n")
    reference = job / "reference"
    reference.mkdir(mode=0o700)
    launch = {"schema": fz.SCHEMA_LAUNCH,
              "supervisor": {"pid": 4000, "pgid": 4000, "start_ticks": 99,
                             "uid": os.getuid(), "subreaper": 1},
              "boot_id": core["boot_id"]}
    terminal = {"schema": fz.SCHEMA_STOP_TERMINAL, "result": "tree_empty",
                "supervisor": {"pid": 4000, "start_ticks": 99},
                "boot_id": core["boot_id"]}
    if terminal_change:
        terminal_change(terminal)
    (reference / "launch.json").write_text(json.dumps(launch))
    (reference / "terminal.json").write_text(json.dumps(terminal))
    (reference / "server.log").write_text("raw log stays private\n")
    candidate = job / "conformance-report.json"
    candidate.write_bytes(report)
    secrets = registry_secrets if registry_secrets is not None else [ROLE_KEY, SIGNING, DB_URL]
    registry = job / fz.REGISTRY_NAME
    registry.write_bytes(json.dumps({"schema": fz.SCHEMA_REGISTRY,
                                     "secrets": secrets}).encode())
    registry.chmod(0o600)
    registry_sha = hashlib.sha256(registry.read_bytes()).hexdigest()

    stop = {"verdict": "COMPLETE", "reason": "tree_empty",
            "launch_receipt_sha256": hashlib.sha256(
                (reference / "launch.json").read_bytes()).hexdigest(),
            "terminal_receipt_sha256": hashlib.sha256(
                (reference / "terminal.json").read_bytes()).hexdigest()}
    if stop_change:
        stop_change(stop)
    database = {"verdict": "COMPLETE", "database": "alrust_scaffold_1_1",
                "oid": 16384, "system_identifier": "7123456789012345678"}
    if database_change:
        database_change(database)
    cand = dict(stat_record(candidate, "file"), name="conformance-report.json")
    del cand["type"]
    if candidate_change:
        candidate_change(cand)
    obtained = {"count": 1, "registry_sha256": registry_sha}
    if obtained_change:
        obtained_change(obtained)
    launch_records = [("owned_resource", stat_record(job / "venv", "dir")),
                      ("owned_resource", stat_record(job / "settings.py", "file"))]
    if leaky_dir:
        leak = job / ("dir-" + SIGNING)
        leak.mkdir()
        launch_records.append(("owned_resource", stat_record(leak, "dir")))
    per_stage = {
        "launch": launch_records,
        "entrypoint": [("export_candidate", cand),
                       ("obtained_secret_count", obtained)],
        "stop": [("stop_verdict", stop)],
        "registry": [("credential_registry", dict(
            {k: v for k, v in stat_record(registry, "file").items() if k != "type"},
            sha256=registry_sha))],
        "database": [("database_drop", database)],
    }
    for stage, kind, payload in extra_records:
        per_stage[stage].append((kind, payload))
    sj.prepare_journal_dir(str(job))
    previous = None
    for index, name in enumerate(STAGES):
        try:
            with sj.StageJournal(str(job), index, name, previous) as journal:
                for kind, payload in per_stage[name]:
                    journal.record(kind, payload)
                if name == fail_stage:
                    journal.fail("synthetic_failure")
                    raise RuntimeError("synthetic")
        except RuntimeError:
            pass
        previous = name
    return job


def test_sealed_export_with_complete_cleanup(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch)
    report = fz.finalize(str(job), STAGES)
    assert report == {"outcome": "SEALED", "cleanup": "COMPLETE", "reason": "sealed"}
    export = job / "export"
    names = sorted(p.name for p in export.iterdir())
    assert names == ["conformance-report.json", "finalizer.jsonl", "manifest.json",
                     "terminal-cleanup.json"]
    manifest = json.loads((export / "manifest.json").read_text())
    for entry in manifest["files"]:
        data = (export / entry["name"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    receipt = json.loads((export / "terminal-cleanup.json").read_text())
    assert receipt["cleanup"] == "COMPLETE"
    assert all(r["outcome"] == "REMOVED" for r in receipt["removals"])
    assert hashlib.sha256((export / "terminal-cleanup.json").read_bytes()).hexdigest() \
        == manifest["terminal_receipt_sha256"]
    # Everything owned is gone; only the sealed export remains.
    assert sorted(p.name for p in job.iterdir()) == ["export"]
    assert oct(export.stat().st_mode & 0o777) == "0o500"
    assert all(oct((export / n).stat().st_mode & 0o777) == "0o400" for n in names)
    # No secret and no raw log content anywhere in the export.
    blob = b"".join((export / n).read_bytes() for n in names)
    for secret in (ROLE_KEY, SIGNING, DB_URL, "raw log stays private"):
        assert secret.encode() not in blob


@pytest.mark.parametrize("planted", [
    ROLE_KEY.encode(), json.dumps({"k": SIGNING}).encode(),
    b"Authorization: Key x", b"-----BEGIN KEY"])
def test_secret_in_candidate_blocks_the_manifest(tmp_path, monkeypatch, planted):
    job = build(tmp_path, monkeypatch, report=b'{"leak": "' + planted.replace(b'"', b"'")
                + b'"}\n')
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == ("FAILED", "scan_finding")
    assert not (job / "export" / "manifest.json").exists()
    assert oct((job / "export").stat().st_mode & 0o777) == "0o700"


def test_secret_in_the_receipt_itself_blocks_the_manifest(tmp_path, monkeypatch):
    # An owned resource whose path contains a secret puts that secret into
    # the terminal receipt; the final-byte scan must catch it there.
    job = build(tmp_path, monkeypatch, leaky_dir=True)
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == ("FAILED", "scan_finding")
    assert SIGNING.encode() in (job / "export" / "terminal-cleanup.json").read_bytes()
    assert not (job / "export" / "manifest.json").exists()


def test_not_finalized_without_positive_quiescence(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch, stop_change=lambda s: s.update(
        verdict="UNPROVEN", reason="no_terminal_receipt"))
    candidate = job / "conformance-report.json"
    candidate.chmod(0o000)  # never opened on this path
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == ("NOT_FINALIZED", "stop_not_proven")
    assert not (job / "export").exists()
    assert (job / "venv").exists() and (job / fz.REGISTRY_NAME).exists()
    candidate.chmod(0o600)


@pytest.mark.parametrize("change,reason", [
    (lambda s: s.update(launch_receipt_sha256="0" * 64), "stop_receipt_mismatch"),
    (lambda s: s.update(terminal_receipt_sha256="0" * 64), "stop_receipt_mismatch"),
    (lambda s: s.pop("terminal_receipt_sha256"), "stop_not_proven"),
])
def test_stop_evidence_must_link_receipts(tmp_path, monkeypatch, change, reason):
    job = build(tmp_path, monkeypatch, stop_change=change)
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == ("NOT_FINALIZED", reason)
    assert not (job / "export").exists()


@pytest.mark.parametrize("change", [
    lambda t: t["supervisor"].update(start_ticks=100),   # another generation
    lambda t: t["supervisor"].update(pid=4001),
    lambda t: t.update(result="tree_not_empty"),
    lambda t: t.update(boot_id="00000000-0000-0000-0000-000000000000"),
])
def test_terminal_must_be_tree_empty_for_the_launch_generation(tmp_path, monkeypatch,
                                                                change):
    # The stop record's hashes match these receipts, so the generation and
    # result checks themselves must refuse.
    job = build(tmp_path, monkeypatch, terminal_change=change)
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == ("NOT_FINALIZED",
                                                     "stop_receipt_mismatch")
    assert not (job / "export").exists()


@pytest.mark.parametrize("change", [
    lambda d: d.update(verdict="UNPROVEN"), lambda d: d.pop("oid"),
    lambda d: d.update(oid=True)])
def test_database_drop_must_be_proven(tmp_path, monkeypatch, change):
    job = build(tmp_path, monkeypatch, database_change=change)
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == ("NOT_FINALIZED",
                                                     "database_drop_not_proven")


def test_failed_stage_is_not_finalized(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch, fail_stage="entrypoint")
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == ("NOT_FINALIZED", "aggregation_failed")


@pytest.mark.parametrize("change,reason", [
    (lambda o: o.update(registry_sha256="0" * 64), "obtained_secrets_unlinked"),
    (lambda o: o.update(count=True), "obtained_secrets_unlinked"),
    (lambda o: o.update(count=4), "registry_incomplete"),
])
def test_obtained_secret_count_must_link_registry(tmp_path, monkeypatch, change, reason):
    job = build(tmp_path, monkeypatch, obtained_change=change)
    report = fz.finalize(str(job), STAGES)
    assert report["reason"] == reason
    assert report["outcome"] in ("NOT_FINALIZED", "FAILED")
    assert not (job / "export" / "manifest.json").exists()


def test_repeated_required_record_is_refused(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch, extra_records=[
        ("stop", "database_drop", {"verdict": "COMPLETE", "database": "x",
                                   "oid": 1, "system_identifier": "1"})])
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == (
        "NOT_FINALIZED", "database_drop_missing_or_repeated")


@pytest.mark.parametrize("change,reason", [
    (lambda c: c.update(name="anything.json"), "candidate_record_invalid"),
    (lambda c: c.update(ino=c["ino"] + 1), "candidate_identity_mismatch"),
    (lambda c: c.update(path="/etc/hostname"), "candidate_record_invalid"),
])
def test_candidate_must_be_allowlisted_and_identified(tmp_path, monkeypatch, change, reason):
    job = build(tmp_path, monkeypatch, candidate_change=change)
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == ("FAILED", reason)
    assert not (job / "export" / "manifest.json").exists()


def test_oversized_candidate_is_refused(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch,
                report=b"x" * (fz.EXPORT_ALLOWLIST["conformance-report.json"] + 1))
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == ("FAILED", "candidate_identity_mismatch")


def test_removal_mismatch_is_unproven_but_sealed(tmp_path, monkeypatch):
    # The settings file is replaced after its record was made: it is left
    # in place, the cleanup is UNPROVEN, and the export still seals so the
    # evidence can be read.
    job = build(tmp_path, monkeypatch)
    settings = job / "settings.py"
    settings.unlink()
    settings.write_text("replacement\n")
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["cleanup"]) == ("SEALED", "UNPROVEN")
    receipt = json.loads((job / "export" / "terminal-cleanup.json").read_text())
    reasons = {r["reason"] for r in receipt["removals"]}
    assert "identity_mismatch" in reasons
    assert settings.read_text() == "replacement\n"


def test_export_directory_must_not_exist(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch)
    (job / "export").mkdir(mode=0o700)
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == ("FAILED", "export_exists")
    assert (job / "venv").exists()


def test_manifest_detects_later_tampering(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch)
    assert fz.finalize(str(job), STAGES)["outcome"] == "SEALED"
    export = job / "export"
    manifest = json.loads((export / "manifest.json").read_text())
    report_file = export / "conformance-report.json"
    report_file.chmod(0o600)
    report_file.write_bytes(b'{"result": "tampered"}\n')
    entry = next(e for e in manifest["files"] if e["name"] == "conformance-report.json")
    assert hashlib.sha256(report_file.read_bytes()).hexdigest() != entry["sha256"]


def test_failure_record_reads_only_hashes(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch, stop_change=lambda s: s.update(
        verdict="UNPROVEN", reason="no_terminal_receipt"))
    assert fz.finalize(str(job), STAGES)["outcome"] == "NOT_FINALIZED"
    env = {"SECRET_KEY": SIGNING, "DATABASE_URL": DB_URL}
    path = fz.failure_record(str(job), STAGES, env)
    assert path == str(job / "export" / "failure-record.json")
    record = json.loads(Path(path).read_text())
    assert record["outcome"] == "FAIL" and record["cleanup"] == "UNPROVEN"
    assert record["aggregation"]["verdict"] == "VALID"
    assert set(record["hashes"]) == {"core_identity", "launch_receipt"}
    text = Path(path).read_text()
    for secret in (ROLE_KEY, SIGNING, DB_URL, "raw log stays private"):
        assert secret not in text
    # The registry was never read or removed, and nothing else was removed.
    assert (job / fz.REGISTRY_NAME).exists() and (job / "venv").exists()


def test_failure_record_needs_the_scan_secrets(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch)
    assert fz.failure_record(str(job), STAGES, {"SECRET_KEY": SIGNING}) is None
    assert fz.failure_record(str(job), STAGES, {"SECRET_KEY": "short",
                                                 "DATABASE_URL": DB_URL}) is None
    assert not (job / "export").exists()
