"""Process controls for the supervisor's tool mode (R-EP plan P2b-2).

These run real local processes: the real supervisor in tool mode as P_T
around a synthetic pinned driver C_T (python; one case also starts a
detached /bin/sleep). They need a signed local window. Nothing touches a
database, a socket or a credential. C_T writes the real stage journal; the
real receipts are then verified with tool_exit, end to end. A process that
outlives its test is never signalled by bare PID: it is reported, and the
window's read-only check holds.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from alrust_conformance import finalizer as fz
from alrust_conformance import stage_journal as sj
from alrust_conformance import tool_exit as te

ROOT = Path(__file__).resolve().parents[3]
SUPERVISOR = ROOT / "conformance/scripts/entrypoint-supervisor.py"
CONFORMANCE = ROOT / "conformance"
PY = [sys.executable, "-I", "-S", "-B"]

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="Linux /proc and prctl")

# A synthetic driver: the real StageJournal for database-create at index 0,
# then MODE. It never holds or prints a secret.
DRIVER = r'''
import os, subprocess, sys
sys.path.insert(0, %(conformance)r)
from alrust_conformance import stage_journal as sj
sj.EXTERNAL_CONFIG = %(external)r
job_root, mode, pid_file = sys.argv[1], sys.argv[2], sys.argv[3]
journal = sj.StageJournal(job_root, 0, "database-create")
try:
    with journal:
        journal.record("owned_resource", {"path": job_root, "dev": 1, "ino": 2, "type": "dir"})
        if mode == "failed":
            journal.fail("name_already_exists")
            raise RuntimeError("stage failed")
        if mode == "escape":
            child = subprocess.Popen(["/bin/sleep", "300"], start_new_session=True,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
            with open(pid_file, "w") as f:
                f.write(str(child.pid))
except RuntimeError:
    sys.exit(2)
'''


def make_job(tmp_path, monkeypatch):
    external = str(tmp_path / "absent.conf")
    monkeypatch.setattr(sj, "EXTERNAL_CONFIG", external)
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER % {"conformance": str(CONFORMANCE), "external": external})
    root = tmp_path / "job"
    root.mkdir(mode=0o700)
    resolved = os.path.realpath(sys.executable)
    sup_sha = sj.file_sha256(str(SUPERVISOR))
    drv_sha = sj.file_sha256(str(driver))
    entry = {"supervisor": {"path": str(SUPERVISOR), "sha256": sup_sha},
             "entrypoint_script": {"path": str(driver), "sha256": drv_sha},
             "interpreter": {"path": sys.executable, "resolved_path": resolved}}
    sha = "a" * 64
    digest = sj.write_core_identity(
        str(root), profile="scaffold", event_head_sha="b" * 40, checkout_sha="b" * 40,
        reference_sha="c" * 40, reference_pin="c" * 40, lock_sha256={"lock": sha},
        lock_pins={"lock": sha}, manifest_sha256=sha, manifest_pin=sha,
        env={"GITHUB_REPOSITORY": "steliosx/alrust", "GITHUB_RUN_ID": "123",
             "GITHUB_RUN_ATTEMPT": "1", "GITHUB_JOB": "profile", "ImageOS": "ubuntu24",
             "ImageVersion": "20261001.1"},
        entrypoint_pins=entry,
        entrypoint_measured={"supervisor": sup_sha, "entrypoint_script": drv_sha,
                             "interpreter": sj.file_sha256(resolved)},
        tool_pins={t: {"driver": {"path": str(driver), "sha256": drv_sha}}
                   for t in sj.TOOL_PIN_NAMES},
        tool_measured={t: drv_sha for t in sj.TOOL_PIN_NAMES})
    sj.prepare_journal_dir(str(root))
    expected = {"driver_sha256": drv_sha, "interpreter_path": sys.executable}
    return root, digest, driver, expected


def run_tool(root, digest, driver, mode, tmp_path, deadline_s=60.0):
    launch, terminal = te.receipt_paths(str(root), "database-create", 0)
    journal = sj.journal_path(str(root), 0, "database-create")
    args = PY + [str(SUPERVISOR), "--receipt", launch, "--terminal", terminal,
                 "--core", str(root / sj.CORE_NAME), "--core-sha256", digest,
                 "--script", str(driver), "--entrypoint-journal", journal,
                 "--deadline-monotonic-ns", str(time.monotonic_ns() + int(deadline_s * 1e9)),
                 "--tool", "database-create", "--stage-index", "0", "--",
                 sys.executable, "-I", str(driver), str(root), mode,
                 str(tmp_path / "escaped.pid")]
    proc = subprocess.Popen(args, process_group=0, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    rc = proc.wait(timeout=60)
    return rc, proc.stderr.read().decode(errors="replace")


def alive(pid):
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            return f.read().rsplit(b")", 1)[1].split()[0] != b"Z"
    except FileNotFoundError:
        return False


def test_complete_run_is_proven_end_to_end(tmp_path, monkeypatch):
    root, digest, driver, expected = make_job(tmp_path, monkeypatch)
    rc, err = run_tool(root, digest, driver, "complete", tmp_path)
    assert rc == 0, err
    record = te.run_tool_exit_stage(str(root), ["database-create"], "database-create",
                                    expected)
    assert (record["verdict"], record["execution"], record["stage_state"]) == \
        ("COMPLETE", "PASS", "COMPLETE")
    names = ["database-create", "database-create-exit"]
    aggregation = sj.aggregate(str(root), names)
    assert aggregation["verdict"] == "VALID"
    proven = te.holder_proofs(str(root), aggregation, fz._journal_records(str(root), aggregation),
                              ("database-create",),
                              {t: dict(expected) for t in te.HOLDER_TOOLS})
    assert [p["tool"] for p in proven] == ["database-create"]


def test_failed_stage_keeps_closure_with_execution_fail(tmp_path, monkeypatch):
    root, digest, driver, expected = make_job(tmp_path, monkeypatch)
    rc, err = run_tool(root, digest, driver, "failed", tmp_path)
    assert rc == 0, err                     # the tree was emptied
    record = te.verify_tool_exit(str(root), "database-create", 0, expected)
    assert (record["verdict"], record["execution"], record["stage_state"]) == \
        ("COMPLETE", "FAIL", "FAILED")


def test_escaped_descendant_is_killed_and_counted(tmp_path, monkeypatch):
    root, digest, driver, expected = make_job(tmp_path, monkeypatch)
    rc, err = run_tool(root, digest, driver, "escape", tmp_path)
    assert rc == 0, err
    escaped = int((tmp_path / "escaped.pid").read_text())
    # Not signalled here on failure: a survivor is reported for the window's
    # read-only check (no bare-PID signal).
    assert not alive(escaped), "escaped descendant survived: HOLD"
    terminal = json.loads(Path(te.receipt_paths(str(root), "database-create", 0)[1])
                          .read_text())
    assert terminal["other_descendant_signals"] >= 1
    assert terminal["tool"] == {"name": "database-create", "stage_index": 0}
    assert te.verify_tool_exit(str(root), "database-create", 0, expected)["verdict"] == \
        "COMPLETE"


def test_second_run_for_the_same_stage_refuses_before_fork(tmp_path, monkeypatch):
    root, digest, driver, expected = make_job(tmp_path, monkeypatch)
    assert run_tool(root, digest, driver, "complete", tmp_path)[0] == 0
    before = sorted(os.listdir(root / "tools" / "00-database-create"))
    rc, err = run_tool(root, digest, driver, "complete", tmp_path)
    # The existing launch receipt refuses first; a run directory without
    # receipts refuses too (pure control in test_tool_supervisor_pure.py).
    assert rc == 70 and "already exists" in err
    assert sorted(os.listdir(root / "tools" / "00-database-create")) == before
