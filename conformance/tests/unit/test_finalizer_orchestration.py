"""Offline controls for the finalizer orchestration and the failure record.

Each test builds a complete synthetic job (core identity, stage journals,
receipts, registry, candidate and owned resources) in a private temporary
directory. No process, network, database or reference server.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from alrust_conformance import finalizer as fz
from alrust_conformance import stage_journal as sj

GIT = "a" * 40
SHA = "b" * 64
ENV = {"GITHUB_REPOSITORY": "steliosx/alrust", "GITHUB_RUN_ID": "123",
       "GITHUB_RUN_ATTEMPT": "1", "GITHUB_JOB": "profile",
       "ImageOS": "ubuntu24", "ImageVersion": "20261001.1"}
STAGES = ["database-create", "launch", "launch-confirm", "entrypoint", "stop", "registry",
          "database-drop"]
DB_IDENTITY = {"oid": 16384, "role": "alrust_scaffold_123_1", "role_oid": 16383,
               "system_identifier": "7123456789012345678",
               "postmaster_start_time": "2026-10-03T01:02:03.456789Z"}
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


def ended_generation():
    """(pid, start_ticks) of a real child that has exited and been reaped,
    so the finalizer's positive exit observation has a genuine subject."""
    import subprocess
    child = subprocess.Popen(["/bin/sleep", "30"])
    with open("/proc/%d/stat" % child.pid, "rb") as f:
        ticks = int(f.read().rsplit(b")", 1)[1].split()[19])
    child.kill()
    child.wait()
    return child.pid, ticks


def stat_record(path, kind):
    info = os.lstat(path)
    return {"path": str(path), "dev": info.st_dev, "ino": info.st_ino, "type": kind}


def build(tmp_path, monkeypatch, *, report=b'{"result": "passed"}\n',
          stop_change=None, database_change=None, candidate_change=None,
          extra_records=(), registry_secrets=None, fail_stage=None,
          terminal_change=None, obtained_change=None, leaky_dir=False,
          generation=None, omit_stop=False, created_change=None, handoff_change=None,
          omit_handoff=False, upto=None, inventory=None, obtained_stage="registry",
          inventory_core=None, inventory_record_change=None, inventory_stage="entrypoint"):
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
    pid, ticks = generation if generation is not None else ended_generation()
    launch = {"schema": fz.SCHEMA_LAUNCH,
              "supervisor": {"pid": pid, "pgid": pid, "start_ticks": ticks,
                             "uid": os.getuid(), "subreaper": 1},
              "boot_id": core["boot_id"]}
    terminal = {"schema": fz.SCHEMA_STOP_TERMINAL, "result": "tree_empty",
                "supervisor": {"pid": pid, "start_ticks": ticks},
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
    created = dict(DB_IDENTITY, name="alrust_scaffold_123_1", server_version_num=160004,
                   version="PostgreSQL 16.4 (synthetic)",
                   psql={"path": "/usr/bin/psql", "version": "psql (PostgreSQL) 16.4"})
    if created_change:
        created_change(created)
    database = dict(DB_IDENTITY, verdict="COMPLETE", reason="dropped_and_verified_absent",
                    database="alrust_scaffold_123_1")
    if database_change:
        database_change(database)

    def handoff(job_root):
        # Built when the launch journal exists, so its digest is real.
        value = {"launch_journal_sha256": hashlib.sha256(
                     Path(sj.journal_path(str(job_root), 1, "launch")).read_bytes()).hexdigest(),
                 "receipt_sha256": stop["launch_receipt_sha256"],
                 "supervisor": {"pid": pid, "start_ticks": ticks},
                 "child": {"pid": pid + 1, "start_ticks": ticks + 1},
                 "observed_monotonic_ns": 1}
        if handoff_change:
            handoff_change(value)
        return value
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
        "database-create": [
            ("database_role_attempted", {"role": DB_IDENTITY["role"]}),
            ("database_role_created", {k: DB_IDENTITY[k] for k in (
                "role", "role_oid", "system_identifier", "postmaster_start_time")}),
            ("database_create_attempted", {"name": "alrust_scaffold_123_1"}),
            ("database_created", created)],
        "launch": launch_records,
        "launch-confirm": [] if omit_handoff else [("launch_handoff", handoff)],
        "entrypoint": [("export_candidate", cand)],
        "stop": [("stop_verdict", stop)],
        "registry": [("credential_registry", dict(
            {k: v for k, v in stat_record(registry, "file").items() if k != "type"},
            sha256=registry_sha)),
                     ("obtained_secret_count", obtained)],
        "database-drop": [("database_drop", database)],
    }
    if omit_stop:
        per_stage["stop"] = []
    if obtained_stage != "registry":
        per_stage["registry"] = [r for r in per_stage["registry"]
                                 if r[0] != "obtained_secret_count"]
        per_stage[obtained_stage].append(("obtained_secret_count", obtained))
    if inventory is not None:
        # Q1: the entrypoint's frozen private snapshot and its record.
        from alrust_conformance import private_secret_inventory as inv
        core_sha = inventory_core or hashlib.sha256(
            (job / "core-identity.json").read_bytes()).hexdigest()
        snapshot = job / "entrypoint-secrets.json"
        data = inv.encode_inventory(core_sha, list(inventory))
        snapshot.write_bytes(data)
        snapshot.chmod(0o600)
        info = os.lstat(snapshot)
        record = {"path": str(snapshot), "dev": info.st_dev, "ino": info.st_ino,
                  "sha256": hashlib.sha256(data).hexdigest(), "count": len(inventory)}
        if inventory_record_change:
            inventory_record_change(record)
        per_stage["entrypoint"].append(("owned_resource", stat_record(snapshot, "file")))
        per_stage[inventory_stage].append(("private_secret_inventory", record))
    for stage, kind, payload in extra_records:
        per_stage[stage].append((kind, payload))
    sj.prepare_journal_dir(str(job))
    previous = None
    for index, name in enumerate(STAGES[:STAGES.index(upto) + 1] if upto else STAGES):
        try:
            with sj.StageJournal(str(job), index, name, previous) as journal:
                for kind, payload in per_stage[name]:
                    journal.record(kind, payload(job) if callable(payload) else payload)
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
    digest = report.pop("manifest_sha256")
    assert report == {"outcome": "SEALED", "cleanup": "COMPLETE", "reason": "sealed"}
    assert fz.verify_manifest(str(job / "export"), digest) is True
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
    lambda t: t["supervisor"].update(start_ticks=t["supervisor"]["start_ticks"] + 1),
    lambda t: t["supervisor"].update(pid=t["supervisor"]["pid"] + 1),
    lambda t: t["supervisor"].update(start_ticks=float(t["supervisor"]["start_ticks"])),
    lambda t: t["supervisor"].update(extra=1),
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


@pytest.mark.parametrize("change,reason", [
    (lambda d: d.update(verdict="UNPROVEN"), "database_drop_not_proven"),
    (lambda d: d.update(verdict="ROLE_ONLY_COMPLETE"), "database_drop_not_proven"),
    (lambda d: d.update(verdict="NONE_ALLOCATED"), "database_drop_not_proven"),
    (lambda d: d.pop("oid"), "database_drop_not_proven"),
    (lambda d: d.update(extra=1), "database_drop_not_proven"),
    (lambda d: d.update(oid=True), "database_linkage_mismatch"),
    (lambda d: d.update(oid=16385), "database_linkage_mismatch"),
    (lambda d: d.update(role_oid=1), "database_linkage_mismatch"),
    (lambda d: d.update(database="alrust_other_1_1"), "database_linkage_mismatch"),
    (lambda d: d.update(system_identifier="1"), "database_linkage_mismatch"),
    (lambda d: d.update(postmaster_start_time="2026-10-03T09:00:00.000000Z"),
     "database_linkage_mismatch"),
])
def test_database_drop_must_be_proven_and_linked(tmp_path, monkeypatch, change, reason):
    job = build(tmp_path, monkeypatch, database_change=change)
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == ("NOT_FINALIZED", reason)
    assert not (job / "export").exists()


def test_database_created_must_be_recorded_once(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch, extra_records=[
        ("database-create", "database_created", dict(DB_IDENTITY, name="x"))])
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == (
        "NOT_FINALIZED", "database_created_missing_or_repeated")


@pytest.mark.parametrize("change,omit,reason", [
    (None, True, "launch_handoff_missing"),
    (lambda h: h.update(launch_journal_sha256="0" * 64), False, "launch_handoff_mismatch"),
    (lambda h: h.update(receipt_sha256="0" * 64), False, "launch_handoff_mismatch"),
    (lambda h: h.update(supervisor={"pid": 1, "start_ticks": 1}), False,
     "launch_handoff_mismatch"),
    (lambda h: h.update(extra=1), False, "launch_handoff_mismatch"),
])
def test_finalizer_requires_the_launch_handoff(tmp_path, monkeypatch, change, omit, reason):
    job = build(tmp_path, monkeypatch, handoff_change=change, omit_handoff=omit)
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == ("NOT_FINALIZED", reason)


def test_failed_entrypoint_is_named(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch, fail_stage="entrypoint")
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == ("NOT_FINALIZED",
                                                     "entrypoint_not_complete")
    assert not (job / "export").exists()


def test_other_failed_stage_is_not_finalized(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch, fail_stage="launch")
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == ("NOT_FINALIZED", "aggregation_failed")


def test_entrypoint_stage_must_be_named(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch)
    report = fz.finalize(str(job), STAGES, entrypoint_stage="executor")
    assert (report["outcome"], report["reason"]) == ("NOT_FINALIZED",
                                                     "entrypoint_stage_missing")


@pytest.mark.parametrize("change,reason", [
    (lambda o: o.update(registry_sha256="0" * 64), "obtained_secrets_unlinked"),
    (lambda o: o.update(count=True), "obtained_secrets_unlinked"),
    (lambda o: o.update(count=4), "registry_incomplete"),
])
def test_obtained_secret_count_must_link_registry(tmp_path, monkeypatch, change, reason):
    # All of these are refused before the export directory exists.
    job = build(tmp_path, monkeypatch, obtained_change=change)
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == ("NOT_FINALIZED", reason)
    assert not (job / "export").exists()


def test_required_record_repeated_is_refused(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch, extra_records=[
        ("stop", "database_drop", {"verdict": "COMPLETE", "database": "x",
                                   "oid": 1, "system_identifier": "1"})])
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == (
        "NOT_FINALIZED", "database_drop_missing_or_repeated")


@pytest.mark.parametrize("change,outcome,reason", [
    (lambda c: c.update(name="anything.json"), "NOT_FINALIZED", "candidate_record_invalid"),
    (lambda c: c.update(path="/etc/hostname"), "NOT_FINALIZED", "candidate_record_invalid"),
    # The identity is only knowable when the file is opened for copying.
    (lambda c: c.update(ino=c["ino"] + 1), "FAILED", "candidate_identity_mismatch"),
])
def test_candidate_must_be_allowlisted_and_identified(tmp_path, monkeypatch, change,
                                                      outcome, reason):
    job = build(tmp_path, monkeypatch, candidate_change=change)
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == (outcome, reason)
    assert not (job / "export" / "manifest.json").exists()


def test_second_invalid_candidate_means_nothing_is_copied(tmp_path, monkeypatch):
    # Every candidate record is validated before the first copy.
    job = build(tmp_path, monkeypatch, extra_records=[
        ("entrypoint", "export_candidate", {"path": "/etc/hostname", "dev": 1,
                                            "ino": 1, "name": "other.json"})])
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == ("NOT_FINALIZED",
                                                     "candidate_record_invalid")
    assert not (job / "export").exists()


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


@pytest.mark.parametrize("tamper", ["content", "extra_file", "missing_file"])
def test_verify_manifest_detects_later_tampering(tmp_path, monkeypatch, tamper):
    job = build(tmp_path, monkeypatch)
    report = fz.finalize(str(job), STAGES)
    assert report["outcome"] == "SEALED"
    export = job / "export"
    digest = report["manifest_sha256"]
    assert fz.verify_manifest(str(export), digest) is True
    export.chmod(0o700)
    report_file = export / "conformance-report.json"
    if tamper == "content":
        report_file.chmod(0o600)
        report_file.write_bytes(b'{"result": "tampered"}\n')
    elif tamper == "extra_file":
        (export / "added.json").write_text("{}")
    else:
        report_file.chmod(0o600)
        report_file.unlink()
    assert fz.verify_manifest(str(export), digest) is False


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
    assert record["stop"] == {"verdict": "UNPROVEN", "reason": "no_terminal_receipt"}
    assert set(record["hashes"]) == {"core_identity", "launch_receipt", "terminal_receipt"}
    assert all(fz._sha(v) for v in record["hashes"].values())
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


def test_repeated_candidate_name_is_refused(tmp_path, monkeypatch):
    # A second, well-formed record for the same allowlisted name.
    job = build(tmp_path, monkeypatch, extra_records=[
        ("entrypoint", "export_candidate", {"path": str(tmp_path / "job" / "x.json"),
                                            "dev": 1, "ino": 1,
                                            "name": "conformance-report.json"})])
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == ("NOT_FINALIZED", "candidate_repeated")
    assert not (job / "export").exists()


def test_verify_manifest_requires_a_clean_scan_record(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch)
    report = fz.finalize(str(job), STAGES)
    export = job / "export"
    export.chmod(0o700)
    manifest_path = export / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["scan"] = "finding"
    manifest_path.chmod(0o600)
    data = json.dumps(manifest)
    manifest_path.write_text(data)
    # Even with the matching digest of the changed manifest, a non-clean
    # scan record is refused.
    assert fz.verify_manifest(str(export), hashlib.sha256(data.encode()).hexdigest()) is False
    assert fz.verify_manifest(str(export), report["manifest_sha256"]) is False



# F2FE89-R1: positive generation-exit observation, not labels.
def test_live_supervisor_is_not_quiescence(tmp_path, monkeypatch):
    with open("/proc/self/stat", "rb") as f:
        ticks = int(f.read().rsplit(b")", 1)[1].split()[19])
    job = build(tmp_path, monkeypatch, generation=(os.getpid(), ticks))
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == ("NOT_FINALIZED",
                                                     "supervisor_generation_live_or_unproven")
    assert not (job / "export").exists()
    assert (job / "venv").exists() and (job / fz.REGISTRY_NAME).exists()


def test_generation_ended_observations():
    pid, ticks = ended_generation()
    with open("/proc/sys/kernel/random/boot_id") as f:
        boot = f.read().strip()
    assert fz.generation_ended(pid, ticks, boot) is True
    with open("/proc/self/stat", "rb") as f:
        own = int(f.read().rsplit(b")", 1)[1].split()[19])
    assert fz.generation_ended(os.getpid(), own, boot) is False
    assert fz.generation_ended(os.getpid(), own + 1, boot) is True   # PID reused
    assert fz.generation_ended(pid, ticks, "0" * 8 + "-0000-0000-0000-" + "0" * 12) is False


# F2FE89-R2: the bytes scanned are the bytes hashed; a later change fails
# the upload gate.
def test_change_after_scan_fails_the_upload_gate(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch)
    target = job / "export" / "conformance-report.json"
    original = fz.read_bounded
    calls = []

    def change_on_second_read(path, limit):
        if path == str(target):
            calls.append(True)
            if len(calls) == 2:
                target.chmod(0o600)
                target.write_bytes(SIGNING.encode())
        return original(path, limit)
    monkeypatch.setattr(fz, "read_bounded", change_on_second_read)
    report = fz.finalize(str(job), STAGES)
    assert len(calls) == 1          # finalize read the export file once
    assert report["outcome"] == "SEALED"
    assert fz.verify_manifest(str(job / "export"), report["manifest_sha256"]) is False
    assert len(calls) == 2


# F2FE89-R3: the upload gate accepts only the sealed, complete evidence.
def test_verify_manifest_refuses_invented_or_incomplete_manifests(tmp_path, monkeypatch):
    export = tmp_path / "export"
    export.mkdir(mode=0o700)
    data = json.dumps({"schema": "invalid", "files": [], "scan": "clean"})
    (export / "manifest.json").write_text(data)
    assert fz.verify_manifest(str(export), hashlib.sha256(data.encode()).hexdigest()) is False
    job = build(tmp_path, monkeypatch)
    report = fz.finalize(str(job), STAGES)
    real = job / "export"
    assert fz.verify_manifest(str(real), "0" * 64) is False
    assert fz.verify_manifest(str(real), "not-a-digest") is False
    manifest = json.loads((real / "manifest.json").read_text())
    for change in (lambda m: m["files"][0].update(size=True),
                   lambda m: m.update(files=[e for e in m["files"]
                                             if e["name"] != "finalizer.jsonl"]),
                   lambda m: m["files"][0].update(name="other.json"),
                   lambda m: m.update(terminal_receipt_sha256="0" * 64)):
        changed = json.loads(json.dumps(manifest))
        change(changed)
        real.chmod(0o700)
        (real / "manifest.json").chmod(0o600)
        text = json.dumps(changed)
        (real / "manifest.json").write_text(text)
        assert fz.verify_manifest(str(real), hashlib.sha256(text.encode()).hexdigest()) is False


# F2FE89-R4: no failure record when a required input is unavailable.
@pytest.mark.parametrize("missing", ["core-identity.json", "reference/launch.json"])
def test_failure_record_requires_its_inputs(tmp_path, monkeypatch, missing):
    job = build(tmp_path, monkeypatch, stop_change=lambda s: s.update(
        verdict="UNPROVEN", reason="no_terminal_receipt"))
    (job / missing).unlink()
    env = {"SECRET_KEY": SIGNING, "DATABASE_URL": DB_URL}
    assert fz.failure_record(str(job), STAGES, env) is None
    assert not (job / "export").exists()


# F2FE89-R5: wrong types and invalid UTF-8 are fixed refusals.
def test_candidate_name_of_wrong_type(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch, candidate_change=lambda c: c.update(name=[]))
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == ("NOT_FINALIZED",
                                                     "candidate_record_invalid")


def test_failure_record_env_with_surrogate(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch)
    env = {"SECRET_KEY": "x" * 20 + "\ud800", "DATABASE_URL": DB_URL}
    assert fz.failure_record(str(job), STAGES, env) is None


def test_verify_manifest_requires_the_mandatory_evidence(tmp_path, monkeypatch):
    # The finalizer journal removed from both the manifest AND the directory:
    # the file set is consistent, so only the mandatory-evidence rule refuses.
    job = build(tmp_path, monkeypatch)
    fz.finalize(str(job), STAGES)
    export = job / "export"
    export.chmod(0o700)
    manifest = json.loads((export / "manifest.json").read_text())
    manifest["files"] = [e for e in manifest["files"] if e["name"] != "finalizer.jsonl"]
    (export / "finalizer.jsonl").chmod(0o600)
    (export / "finalizer.jsonl").unlink()
    (export / "manifest.json").chmod(0o600)
    text = json.dumps(manifest)
    (export / "manifest.json").write_text(text)
    assert fz.verify_manifest(str(export), hashlib.sha256(text.encode()).hexdigest()) is False



# F2FE89-R4a: the failure record needs complete closed journals and exactly
# one linked stop verdict; "stop": null never satisfies section 9.1a.
FAILURE_ENV = {"SECRET_KEY": SIGNING, "DATABASE_URL": DB_URL}
UNPROVEN_STOP = dict(verdict="UNPROVEN", reason="no_terminal_receipt")


def _no_failure_output(job):
    assert fz.failure_record(str(job), STAGES, FAILURE_ENV) is None
    assert not (job / "export").exists()


def test_failure_record_refuses_missing_journals(tmp_path, monkeypatch):
    import shutil
    job = build(tmp_path, monkeypatch, stop_change=lambda s: s.update(UNPROVEN_STOP))
    shutil.rmtree(job / sj.JOURNAL_DIR)
    _no_failure_output(job)


def test_failure_record_refuses_an_unclosed_journal(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch, stop_change=lambda s: s.update(UNPROVEN_STOP))
    path = Path(sj.journal_path(str(job), 6, "database-drop"))
    lines = path.read_bytes().splitlines(keepends=True)
    path.chmod(0o600)
    path.write_bytes(b"".join(lines[:-1]))          # drop stage_complete
    _no_failure_output(job)


def test_failure_record_refuses_a_missing_stop_verdict(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch, omit_stop=True)
    assert sj.aggregate(str(job), STAGES)["verdict"] == "VALID"
    _no_failure_output(job)


def test_failure_record_refuses_a_repeated_stop_verdict(tmp_path, monkeypatch):
    # Two identical, individually valid stop verdicts: ambiguous, refused.
    extra = []

    def stop_change(s):
        s.update(UNPROVEN_STOP)
        extra.append(("stop", "stop_verdict", dict(s)))
    job = build(tmp_path, monkeypatch, stop_change=stop_change, extra_records=extra)
    kinds = [k for _, k, _ in fz._journal_records(str(job), sj.aggregate(str(job), STAGES))]
    assert kinds.count("stop_verdict") == 2
    _no_failure_output(job)


@pytest.mark.parametrize("change", [
    lambda s: s.update(launch_receipt_sha256="0" * 64),     # unlinked launch
    lambda s: s.update(terminal_receipt_sha256="0" * 64),   # unlinked terminal
    lambda s: s.update(reason="Not A Code"),
    lambda s: s.update(verdict="PASSED"),
    lambda s: s.pop("terminal_receipt_sha256"),
    lambda s: s.update(extra=1),
])
def test_failure_record_refuses_a_malformed_stop_verdict(tmp_path, monkeypatch, change):
    def stop_change(s):
        s.update(UNPROVEN_STOP)
        change(s)
    job = build(tmp_path, monkeypatch, stop_change=stop_change)
    _no_failure_output(job)


def test_failure_record_without_a_terminal_receipt(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch, stop_change=lambda s: s.update(
        UNPROVEN_STOP, terminal_receipt_sha256=None))
    (job / "reference" / "terminal.json").unlink()
    path = fz.failure_record(str(job), STAGES, FAILURE_ENV)
    record = json.loads(Path(path).read_text())
    assert record["stop"] == UNPROVEN_STOP
    assert set(record["hashes"]) == {"core_identity", "launch_receipt"}


def test_failure_record_from_a_closed_failed_stage(tmp_path, monkeypatch):
    # A stage that closed FAILED is failure evidence, not an unavailable input.
    job = build(tmp_path, monkeypatch, fail_stage="database-drop",
                stop_change=lambda s: s.update(UNPROVEN_STOP))
    assert sj.aggregate(str(job), STAGES)["verdict"] == "FAILED"
    path = fz.failure_record(str(job), STAGES, FAILURE_ENV)
    record = json.loads(Path(path).read_text())
    assert record["aggregation"]["verdict"] == "FAILED"
    assert set(record["hashes"]) == {"core_identity", "launch_receipt", "terminal_receipt"}


# F2213-R6: I/O failures after the gate are fixed reports, inputs preserved.
def _inputs_preserved(job):
    assert (job / "venv").exists() and (job / fz.REGISTRY_NAME).exists()
    assert (job / sj.JOURNAL_DIR).exists() and (job / "reference").exists()


def test_export_creation_io_failure_is_a_fixed_report(tmp_path, monkeypatch):
    import errno
    job = build(tmp_path, monkeypatch)
    real_mkdir = os.mkdir

    def no_space(path, *args, **kwargs):
        if path == str(job / "export"):
            raise OSError(errno.ENOSPC, "synthetic")
        return real_mkdir(path, *args, **kwargs)
    monkeypatch.setattr(fz.os, "mkdir", no_space)
    assert fz.finalize(str(job), STAGES) == {"outcome": "FAILED", "cleanup": "UNPROVEN",
                                             "reason": "export_unavailable"}
    _inputs_preserved(job)


def test_journal_construction_io_failure_is_a_fixed_report(tmp_path, monkeypatch):
    import errno

    def broken(path):
        raise OSError(errno.EIO, "synthetic")
    job = build(tmp_path, monkeypatch)
    monkeypatch.setattr(fz, "PrivateJournal", broken)
    assert fz.finalize(str(job), STAGES) == {"outcome": "FAILED", "cleanup": "UNPROVEN",
                                             "reason": "finalizer_journal_unavailable"}
    _inputs_preserved(job)
    assert not (job / "export" / "manifest.json").exists()


def test_closing_failure_never_masks_or_seals(tmp_path, monkeypatch):
    import errno
    real = fz.PrivateJournal

    class CloseFails(real):
        def close(self):
            super().close()
            raise OSError(errno.EIO, "synthetic")
    job = build(tmp_path, monkeypatch)
    monkeypatch.setattr(fz, "PrivateJournal", CloseFails)
    report = fz.finalize(str(job), STAGES)
    # The explicit close inside the steps fails first; that report stands.
    assert report == {"outcome": "FAILED", "cleanup": "COMPLETE",
                      "reason": "finalizer_io_failed"}
    assert not (job / "export" / "manifest.json").exists()


def test_a_write_failure_after_removals_reports_the_actual_cleanup(tmp_path, monkeypatch):
    import errno
    job = build(tmp_path, monkeypatch)
    real_open = os.open

    def fail_receipt(path, *args, **kwargs):
        if str(path).endswith("terminal-cleanup.json"):
            raise OSError(errno.ENOSPC, "synthetic")
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(fz.os, "open", fail_receipt)
    report = fz.finalize(str(job), STAGES)
    assert report == {"outcome": "FAILED", "cleanup": "COMPLETE",
                      "reason": "finalizer_io_failed"}
    assert not (job / "export" / "manifest.json").exists()



# writer_quiescence: the gate the registry and database-drop stages run
# BEFORE opening their own journals (database design section 4, DBC023-R1).
PREFIX = STAGES[:STAGES.index("stop") + 1]


def test_writer_gate_passes_on_a_quiescent_prefix(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch, upto="stop")
    evidence = fz.writer_quiescence(str(job), PREFIX)
    assert evidence["aggregation"]["verdict"] == "VALID"
    assert evidence["stop"]["verdict"] == "COMPLETE"
    # The registry's own prefix (through registry) also passes.
    (tmp_path / "second").mkdir()
    job2 = build(tmp_path / "second", monkeypatch, upto="registry")
    fz.writer_quiescence(str(job2), STAGES[:STAGES.index("registry") + 1])


@pytest.mark.parametrize("prefix", [
    ["database-create", "launch", "entrypoint", "stop"],          # no launch-confirm
    ["launch", "launch-confirm", "entrypoint", "stop"],           # no database-create
    ["database-create", "launch", "entrypoint", "launch-confirm", "stop"],  # not adjacent
    ["database-create", "launch", "launch-confirm", "entrypoint"],          # no stop
    "database-create,launch",
])
def test_writer_gate_refuses_an_invalid_prefix(tmp_path, monkeypatch, prefix):
    job = build(tmp_path, monkeypatch, upto="stop")
    with pytest.raises(fz.FinalizerError, match="writer_gate_stages_invalid"):
        fz.writer_quiescence(str(job), prefix)


@pytest.mark.parametrize("kwargs,code", [
    ({"fail_stage": "launch"}, "writer_gate_failed"),
    ({"fail_stage": "entrypoint"}, "writer_gate_failed"),
    ({"omit_stop": True}, "stop_verdict_missing_or_repeated"),
    ({"stop_change": lambda s: s.update(reason="tree_not_empty")}, "stop_not_proven"),
    ({"terminal_change": lambda t: t.update(result="tree_not_empty")},
     "stop_receipt_mismatch"),
    ({"omit_handoff": True}, "launch_handoff_missing"),
    ({"handoff_change": lambda h: h.update(launch_journal_sha256="0" * 64)},
     "launch_handoff_mismatch"),
])
def test_writer_gate_refusals(tmp_path, monkeypatch, kwargs, code):
    job = build(tmp_path, monkeypatch, upto="stop", **kwargs)
    with pytest.raises(fz.FinalizerError, match=code):
        fz.writer_quiescence(str(job), PREFIX)


def test_writer_gate_refuses_a_live_supervisor(tmp_path, monkeypatch):
    with open("/proc/self/stat", "rb") as f:
        ticks = int(f.read().rsplit(b")", 1)[1].split()[19])
    job = build(tmp_path, monkeypatch, upto="stop", generation=(os.getpid(), ticks))
    with pytest.raises(fz.FinalizerError, match="supervisor_generation_live_or_unproven"):
        fz.writer_quiescence(str(job), PREFIX)


def test_writer_gate_refuses_an_unclosed_or_extra_journal(tmp_path, monkeypatch):
    job = build(tmp_path, monkeypatch, upto="stop")
    # An already-open next journal (the order N4 forbids) is an extra file.
    Path(sj.journal_path(str(job), len(PREFIX), "registry")).write_text("")
    with pytest.raises(fz.FinalizerError, match="writer_gate_incomplete"):
        fz.writer_quiescence(str(job), PREFIX)


def test_writer_gate_reads_no_registry_or_credential(tmp_path, monkeypatch):
    # DBC023-N3: only immutable proof inputs are read.
    job = build(tmp_path, monkeypatch, upto="stop")
    read = []
    real = fz.read_bounded

    def spy(path, limit):
        read.append(os.path.basename(path))
        return real(path, limit)
    monkeypatch.setattr(fz, "read_bounded", spy)
    fz.writer_quiescence(str(job), PREFIX)
    assert fz.REGISTRY_NAME not in read and "server.log" not in read
    assert set(read) <= {"core-identity.json", "launch.json", "terminal.json"} | {
        os.path.basename(sj.journal_path(str(job), i, n)) for i, n in enumerate(PREFIX)}



def test_writer_gate_requires_a_closed_entrypoint(tmp_path, monkeypatch):
    # DBFE-R1 (Codex's control): a valid closed prefix with no entrypoint.
    prefix = ["database-create", "launch", "launch-confirm", "stop"]
    monkeypatch.setattr(sys.modules[__name__], "STAGES", prefix)
    job = build(tmp_path, monkeypatch, upto="stop")
    assert sj.aggregate(str(job), prefix)["verdict"] == "VALID"
    with pytest.raises(fz.FinalizerError, match="writer_gate_stages_invalid"):
        fz.writer_quiescence(str(job), prefix)


def test_writer_gate_requires_entrypoint_before_stop(tmp_path, monkeypatch):
    prefix = ["database-create", "launch", "launch-confirm", "stop", "entrypoint"]
    with pytest.raises(fz.FinalizerError, match="writer_gate_stages_invalid"):
        fz.writer_quiescence(str(tmp_path), prefix)



def test_obtained_count_must_come_from_the_registry_stage(tmp_path, monkeypatch):
    # Q1 (b): an entrypoint-authored count is refused, even if well formed.
    job = build(tmp_path, monkeypatch, extra_records=[
        ("entrypoint", "obtained_secret_count", {"count": 0, "registry_sha256": "0" * 64})])
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == (
        "NOT_FINALIZED", "obtained_secret_count_missing_or_repeated")


def test_obtained_count_only_in_the_entrypoint_is_refused(tmp_path, monkeypatch):
    # Q1 (b): the count authored by the entrypoint (the old layout) refuses.
    job = build(tmp_path, monkeypatch, obtained_stage="entrypoint")
    report = fz.finalize(str(job), STAGES)
    assert (report["outcome"], report["reason"]) == (
        "NOT_FINALIZED", "obtained_secret_count_not_from_registry")
    assert not (job / "export").exists()
