"""Pure controls for the failure record as a non-holder (R-EP plan P3d,
Q-P2c-1): it takes no secret value or path, and admits only fixed codes.

Synthetic jobs from test_finalizer_orchestration.build with a synthetic ended
generation (absent PIDs); stop-reference.py is parsed, never run. No process,
socket or database is used."""
import ast
import inspect
import json
import os
import re
from pathlib import Path

import pytest

from alrust_conformance import finalizer as fz
from alrust_conformance import stage_journal as sj
from test_finalizer_orchestration import DB_URL, ROLE_KEY, SIGNING, STAGES, build

SYNTHETIC_SUPERVISOR = (4194293, 333)
STOP_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "stop-reference.py"
UNPROVEN_STOP = dict(verdict="UNPROVEN", reason="no_terminal_receipt")


@pytest.fixture
def job(tmp_path, monkeypatch):
    if os.path.exists("/proc/%d" % SYNTHETIC_SUPERVISOR[0]):
        pytest.skip("synthetic ended PID is in use on this host")

    def make(**kw):
        kw.setdefault("generation", SYNTHETIC_SUPERVISOR)
        return build(tmp_path, monkeypatch, **kw)
    return make


def _stop(**change):
    return lambda s: s.update(dict(UNPROVEN_STOP, **change))


def _nothing_written(root):
    assert fz.failure_record(str(root), STAGES) is None
    assert not (root / fz.EXPORT_DIR).exists()


def test_the_record_takes_no_secret_input():
    assert list(inspect.signature(fz.failure_record).parameters) == ["job_root", "stages"]


def test_the_record_is_written_from_fixed_codes_and_hashes(job):
    root = job(stop_change=_stop())
    path = fz.failure_record(str(root), STAGES)
    record = json.loads(Path(path).read_text())
    assert record["stop"] == UNPROVEN_STOP and record["aggregation"]["reason"] == "complete"
    text = Path(path).read_text()
    for secret in (ROLE_KEY, SIGNING, DB_URL):
        assert secret not in text


def test_the_record_never_opens_a_secret_file(job, monkeypatch):
    root = job(stop_change=_stop())
    (root / "database").mkdir(mode=0o700, exist_ok=True)
    (root / "database" / "url").write_text(DB_URL + "\n")
    (root / "secret-key").write_text(SIGNING)
    opened = []
    real_open = os.open

    def spy(path, *a, **k):
        opened.append(str(path))
        return real_open(path, *a, **k)
    monkeypatch.setattr(os, "open", spy)
    assert fz.failure_record(str(root), STAGES) is not None
    assert not any(p.endswith("/url") or p.endswith("secret-key") for p in opened)


def _stop_reference_verdicts():
    """Every verdict(NAME, REASON, ...) call in stop-reference.py; both
    arguments must be string literals."""
    found = set()
    for node in ast.walk(ast.parse(STOP_SCRIPT.read_text())):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "verdict":
            name, reason = node.args[:2]
            assert isinstance(name, ast.Constant) and isinstance(reason, ast.Constant)
            found.add((name.value, reason.value))
    return found


def test_the_allowed_stop_verdicts_are_exactly_stop_references():
    assert _stop_reference_verdicts() == fz.STOP_VERDICTS


@pytest.mark.parametrize("pair", sorted(fz.STOP_VERDICTS))
def test_every_stop_reference_verdict_is_recorded(job, pair):
    verdict, reason = pair
    root = job(stop_change=_stop(verdict=verdict, reason=reason))
    record = json.loads(Path(fz.failure_record(str(root), STAGES)).read_text())
    assert record["stop"] == {"verdict": verdict, "reason": reason}


@pytest.mark.parametrize("change", [
    dict(reason="f" * 64),                        # a 64-hex secret is a valid code shape
    dict(reason="a" + "0123456789abcdef" * 3 + "0123456789abcde"),
    dict(reason="not_a_stop_code"),
    dict(verdict="COMPLETE", reason="no_terminal_receipt"),     # mismatched pair
    dict(verdict="UNPROVEN", reason="tree_empty"),
    dict(reason=["no_terminal_receipt"]),
])
def test_any_other_stop_text_writes_nothing(job, change):
    _nothing_written(job(stop_change=_stop(**change)))


def test_a_generic_pattern_match_still_writes_nothing(job, monkeypatch):
    root = job(stop_change=_stop())
    monkeypatch.setattr(fz, "GENERIC_PATTERNS", (re.compile(rb"no_terminal_receipt"),))
    _nothing_written(root)


def test_an_unexpected_aggregation_reason_writes_nothing(job, monkeypatch):
    root = job(stop_change=_stop())
    real = sj.aggregate

    def other_reason(*a, **k):
        return dict(real(*a, **k), reason="file_missing")
    monkeypatch.setattr(fz._sj, "aggregate", other_reason)
    _nothing_written(root)


def test_a_failed_stage_is_still_recorded(job):
    root = job(fail_stage="database-drop", stop_change=_stop())
    record = json.loads(Path(fz.failure_record(str(root), STAGES)).read_text())
    assert (record["aggregation"]["verdict"], record["aggregation"]["reason"]) == (
        "FAILED", "stage_failed")


# --- P3D-STAGE-R1: stage names come from a fixed set -----------------------------------

SYNTHETIC_KEY_NAME = "synthetic-signing-token"       # a valid _STAGE name, labelled synthetic


def test_a_validly_chained_extra_stage_named_like_a_secret_writes_nothing(job):
    # Codex's f73cd1a3 reproducer shape: a genuine closed journal chained after
    # database-drop, VALID/complete, with linked stop and launch receipts.
    root = job(stop_change=_stop())
    (root / "secret-key").write_text(SYNTHETIC_KEY_NAME)
    assert sj._STAGE.fullmatch(SYNTHETIC_KEY_NAME)
    with sj.StageJournal(str(root), len(STAGES), SYNTHETIC_KEY_NAME, STAGES[-1]):
        pass
    stages = STAGES + [SYNTHETIC_KEY_NAME]
    aggregation = sj.aggregate(str(root), stages)
    assert (aggregation["verdict"], aggregation["reason"]) == ("VALID", "complete")
    _nothing_written_for(root, stages)


def _nothing_written_for(root, stages):
    assert fz.failure_record(str(root), stages) is None
    assert not (root / fz.EXPORT_DIR).exists()


def test_the_failure_stage_set_is_the_pipeline():
    assert list(fz.FAILURE_STAGES) == STAGES


def test_the_a2_drop_path_without_registry_is_admitted(job):
    # A launched drop with no registry (A2): not a prefix, but in order.
    a2 = [s for s in STAGES if s not in ("registry", "registry-exit")]
    root = job(stop_change=_stop(), upto="stop")
    with sj.StageJournal(str(root), len(a2) - 1, "database-drop", "stop"):
        pass
    assert fz.failure_record(str(root), a2) is not None
