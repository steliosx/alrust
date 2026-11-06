"""Synthetic controls for the entrypoint-exit verifier and stage.

Private temporary files, a real core identity and stage journals only. No
process is created or signalled: an ended generation is a PID that does not
exist, and a live one is this test process itself.
"""
import hashlib
import json
import os

import pytest

from alrust_conformance import entrypoint_exit as ee
from alrust_conformance import stage_journal as sj
from test_private_secret_writer import job

SCRIPT_SHA = "d" * 64
PYTHON = "/usr/bin/python3"
INDEX = 3
PREFIX = ["database-create", "launch", "launch-confirm", "entrypoint"]
ENDED_PID = 4194301
EXPECTED = {"entrypoint_script_sha256": SCRIPT_SHA, "interpreter_path": PYTHON}


def _dump(path, obj):
    data = (json.dumps(obj, sort_keys=True) + "\n").encode()
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _own_ticks():
    with open("/proc/self/stat", "rb") as f:
        return int(f.read().rsplit(b")", 1)[1].split()[19])


def build(root, *, journal_state="COMPLETE", result="tree_empty", execution="PASS",
          trigger="child_exited", pid=ENDED_PID, ticks=111, mutate_launch=None,
          mutate_terminal=None, write_terminal=True, write_journal=True):
    if os.path.exists("/proc/%d" % ENDED_PID):
        pytest.skip("synthetic ended PID is in use on this host")
    core = json.loads((root / sj.CORE_NAME).read_bytes())
    core_sha = hashlib.sha256((root / sj.CORE_NAME).read_bytes()).hexdigest()
    journal = sj.journal_path(str(root), INDEX, "entrypoint")
    journal_sha = None
    if write_journal:
        data = (json.dumps({"sequence": 0, "evidence": {"kind": "stage_begin"}}) + "\n"
                + json.dumps({"sequence": 1, "evidence": {
                    "kind": "stage_complete", "payload": {"result": journal_state} if
                    journal_state != "FAILED" else {"result": "FAILED", "code": "x"}}})
                + "\n").encode()
        with open(journal, "wb") as f:
            f.write(data)
        journal_sha = hashlib.sha256(data).hexdigest()
    (root / ee.RECEIPT_DIR).mkdir(exist_ok=True)
    launch_path, terminal_path = ee.receipt_paths(str(root))
    launch = {
        "schema": ee.SCHEMA_LAUNCH,
        "supervisor": {"pid": pid, "pgid": pid, "start_ticks": ticks,
                       "uid": os.getuid(), "subreaper": 1},
        "child": {"pid": pid + 1, "start_ticks": ticks + 1},
        "boot_id": core["boot_id"], "core_identity_sha256": core_sha,
        "command": {"argv_sha256": "e" * 64,
                    "executable": {"path": PYTHON, "dev": 1, "ino": 2},
                    "script": {"path": "/job/entrypoint.py", "dev": 1, "ino": 3,
                               "sha256": SCRIPT_SHA}},
        "entrypoint_journal": journal, "terminal_receipt": terminal_path,
        "deadline": {"monotonic_ns": 10**12, "teardown_budget_ns": ee.TEARDOWN_BUDGET_NS,
                     "boot_id": core["boot_id"]},
        "launched_utc_ns": 1, "monotonic_ns": 1}
    if mutate_launch:
        mutate_launch(launch)
    launch_sha = _dump(root / ee.RECEIPT_DIR / ee.LAUNCH_NAME, launch)
    terminal = {
        "schema": ee.SCHEMA_TERMINAL, "result": result,
        "supervisor": {"pid": pid, "start_ticks": ticks}, "boot_id": core["boot_id"],
        "launch_receipt_sha256": launch_sha, "trigger": trigger,
        "child_wait_status": 0, "child_signalled": False, "other_descendant_signals": 0,
        "other_descendants_reaped": 0,
        "entrypoint_journal": None if result != "tree_empty" else {
            "sha256": journal_sha,
            "state": journal_state if write_journal else "INCOMPLETE",
            "code": "x" if (write_journal and journal_state == "FAILED") else None},
        "execution": execution, "signals_sent": 0, "children_reaped": 1,
        "teardown_ns": 1, "finished_utc_ns": 2, "finished_monotonic_ns": 2}
    if mutate_terminal:
        mutate_terminal(terminal)
    if write_terminal:
        _dump(root / ee.RECEIPT_DIR / ee.TERMINAL_NAME, terminal)
    return journal


def verify(root, expected=EXPECTED):
    record = ee.verify_entrypoint_exit(str(root), INDEX, expected)
    assert set(record) == ee.VERDICT_KEYS
    return record


def test_complete_and_pass(job):
    root, core = job
    build(root)
    record = verify(root)
    assert (record["verdict"], record["reason"], record["execution"]) == \
        ("COMPLETE", "tree_empty", "PASS")
    assert record["entrypoint_state"] == "COMPLETE" and record["trigger"] == "child_exited"


def test_closure_proven_but_execution_fail_on_deadline(job):
    root, core = job
    build(root, execution="FAIL", trigger="deadline")
    record = verify(root)
    assert (record["verdict"], record["execution"], record["trigger"]) == \
        ("COMPLETE", "FAIL", "deadline")


def test_failed_journal_is_reported_with_closure(job):
    root, core = job
    build(root, journal_state="FAILED", execution="FAIL")
    record = verify(root)
    assert (record["verdict"], record["entrypoint_state"], record["execution"]) == \
        ("COMPLETE", "FAILED", "FAIL")


def test_missing_journal_observed_incomplete_is_accepted_only_if_still_absent(job):
    root, core = job
    journal = build(root, write_journal=False, execution="FAIL")
    assert verify(root)["verdict"] == "COMPLETE"
    with open(journal, "wb") as f:
        f.write(b"appeared later\n")
    assert verify(root)["reason"] == "journal_changed"


@pytest.mark.parametrize("kwargs", [
    {"execution": "FAIL", "trigger": "deadline",
     "mutate_terminal": lambda t: t.update(child_signalled=True, child_wait_status=15)},
    {"execution": "FAIL", "mutate_terminal": lambda t: t.update(child_wait_status=256)},
    {"execution": "FAIL", "trigger": "stop_requested",
     "mutate_terminal": lambda t: t.update(child_signalled=True, child_wait_status=9)},
    {"execution": "FAIL", "journal_state": "INCOMPLETE"},
])
def test_legitimate_fail_evidence_keeps_closure(job, kwargs):
    # A deadline, a nonzero or signal status, a stop request or an
    # incomplete journal: closure COMPLETE, execution FAIL (REP3-R1 scope).
    root, core = job
    build(root, **kwargs)
    record = verify(root)
    assert (record["verdict"], record["execution"]) == ("COMPLETE", "FAIL")


def test_tree_not_empty_is_failed(job):
    root, core = job
    build(root, result="tree_not_empty", execution="FAIL")
    record = verify(root)
    assert (record["verdict"], record["reason"], record["execution"]) == \
        ("FAILED", "tree_not_empty", "FAIL")


@pytest.mark.parametrize("kwargs,reason", [
    ({"write_terminal": False}, "terminal_receipt_unavailable"),
    ({"mutate_terminal": lambda t: t.update(launch_receipt_sha256="0" * 64)},
     "terminal_link_mismatch"),
    ({"mutate_terminal": lambda t: t["supervisor"].update(start_ticks=999)},
     "terminal_link_mismatch"),
    ({"mutate_terminal": lambda t: t.update(boot_id="x")}, "terminal_link_mismatch"),
    ({"mutate_launch": lambda l: l.update(core_identity_sha256="0" * 64)}, "core_mismatch"),
    ({"mutate_launch": lambda l: l.update(boot_id="00000000-0000-0000-0000-000000000000")},
     "boot_mismatch"),
    ({"mutate_launch": lambda l: l["command"]["script"].update(sha256="0" * 64)},
     "entrypoint_script_mismatch"),
    ({"mutate_launch": lambda l: l["command"]["executable"].update(path="/tmp/python")},
     "interpreter_mismatch"),
    ({"mutate_launch": lambda l: l.update(entrypoint_journal="/elsewhere.jsonl")},
     "receipt_paths_mismatch"),
    ({"mutate_launch": lambda l: l["deadline"].update(teardown_budget_ns=1)},
     "deadline_invalid"),
    ({"mutate_launch": lambda l: l["supervisor"].update(pgid=7)},
     "supervisor_identity_invalid"),
    ({"mutate_launch": lambda l: l["supervisor"].update(subreaper=0)},
     "supervisor_identity_invalid"),
    ({"mutate_launch": lambda l: l.update(schema="alrust-reference-launch/1")},
     "launch_receipt_malformed"),
    ({"mutate_terminal": lambda t: t.update(result="maybe")}, "terminal_receipt_malformed"),
    ({"mutate_terminal": lambda t: t.update(execution="OK")}, "terminal_receipt_malformed"),
    # REP3-R1 (Codex's counterexamples, 4182a0e8): contradictory or malformed
    # receipts never become COMPLETE/PASS.
    ({"mutate_terminal": lambda t: t.update(trigger="deadline")}, "execution_contradiction"),
    ({"mutate_terminal": lambda t: t.update(child_wait_status=256)}, "execution_contradiction"),
    ({"mutate_terminal": lambda t: t.update(child_signalled=True)}, "receipt_contradiction"),
    ({"mutate_terminal": lambda t: t.update(trigger="nonsense")}, "terminal_receipt_malformed"),
    ({"mutate_terminal": lambda t: [t.pop(k) for k in (
        "trigger", "child_wait_status", "child_signalled", "other_descendant_signals",
        "other_descendants_reaped", "signals_sent", "children_reaped", "teardown_ns",
        "finished_utc_ns", "finished_monotonic_ns")]}, "terminal_receipt_malformed"),
    ({"mutate_launch": lambda l: l.pop("child")}, "launch_receipt_malformed"),
    ({"mutate_launch": lambda l: l["supervisor"].update(subreaper=True)},
     "launch_receipt_malformed"),
    ({"mutate_terminal": lambda t: t.update(child_wait_status=None)}, "receipt_contradiction"),
    ({"mutate_terminal": lambda t: t.update(signals_sent=False)}, "terminal_receipt_malformed"),
    ({"mutate_terminal": lambda t: t.update(trigger="receipt_failed")},
     "terminal_receipt_malformed"),
    ({"mutate_terminal": lambda t: t.update(extra=1)}, "terminal_receipt_malformed"),
    ({"mutate_launch": lambda l: l.update(extra=1)}, "launch_receipt_malformed"),
    ({"mutate_terminal": lambda t: t["entrypoint_journal"].update(code="x")},
     "journal_observation_invalid"),
    ({"execution": "FAIL"}, "execution_contradiction"),
    ({"mutate_terminal": lambda t: t["entrypoint_journal"].update(state="DONE")},
     "journal_observation_invalid"),
])
def test_refusals_are_unproven_with_fixed_reasons(job, kwargs, reason):
    root, core = job
    build(root, **kwargs)
    record = verify(root)
    assert (record["verdict"], record["reason"], record["execution"]) == \
        ("UNPROVEN", reason, "FAIL")


def test_missing_launch_receipt(job):
    root, core = job
    build(root)
    os.unlink(ee.receipt_paths(str(root))[0])
    assert verify(root)["reason"] == "launch_receipt_unavailable"


def test_journal_changed_after_observation(job):
    root, core = job
    journal = build(root)
    with open(journal, "ab") as f:
        f.write(b"late write\n")
    assert verify(root)["reason"] == "journal_changed"


def test_live_generation_is_unproven(job):
    root, core = job
    build(root, pid=os.getpid(), ticks=_own_ticks())
    record = verify(root)
    assert (record["verdict"], record["reason"]) == \
        ("UNPROVEN", "supervisor_generation_live_or_unproven")


@pytest.mark.parametrize("expected", [
    None, {}, {"entrypoint_script_sha256": "x", "interpreter_path": PYTHON},
    {"entrypoint_script_sha256": SCRIPT_SHA, "interpreter_path": "relative"},
    dict(EXPECTED, extra=1),
])
def test_bad_expected_pins_refuse(job, expected):
    root, core = job
    build(root)
    assert verify(root, expected)["reason"] == "expected_pins_invalid"


def _stage_records(root, index):
    data = open(sj.journal_path(str(root), index, ee.STAGE), "rb").read()
    lines = [json.loads(line)["evidence"] for line in data.splitlines()]
    return lines


def test_stage_complete_records_one_verdict(job):
    root, core = job
    build(root)
    record = ee.run_entrypoint_exit_stage(str(root), PREFIX, EXPECTED)
    assert record["verdict"] == "COMPLETE"
    lines = _stage_records(root, INDEX + 1)
    verdicts = [l["payload"] for l in lines if l["kind"] == "entrypoint_exit_verdict"]
    assert verdicts == [record]
    assert lines[-1]["kind"] == "stage_complete" and lines[-1]["payload"] == {"result": "COMPLETE"}


def test_stage_closes_failed_for_unproven(job):
    root, core = job
    build(root, write_terminal=False)
    record = ee.run_entrypoint_exit_stage(str(root), PREFIX, EXPECTED)
    assert record["verdict"] == "UNPROVEN"
    lines = _stage_records(root, INDEX + 1)
    assert lines[-1]["payload"] == {"result": "FAILED", "code": "entrypoint_exit_unproven"}


@pytest.mark.parametrize("prefix", [
    [], ["database-create", "launch", "launch-confirm"],
    PREFIX + ["stop"], PREFIX + ["entrypoint"], "entrypoint",
    ["entrypoint-exit", "entrypoint"],
])
def test_stage_prefix_must_end_with_the_single_entrypoint(job, prefix):
    root, core = job
    build(root)
    with pytest.raises(sj.JournalError):
        ee.run_entrypoint_exit_stage(str(root), prefix, EXPECTED)


# REP4-R1 (Codex's counterexamples, b1a4a738): the receipt's journal state
# and code must equal what the current bytes say, not only its sha256.
@pytest.mark.parametrize("journal_state,claim", [
    ("FAILED", {"state": "COMPLETE", "code": None, "execution": "PASS"}),
    ("INCOMPLETE", {"state": "COMPLETE", "code": None, "execution": "PASS"}),
    ("COMPLETE", {"state": "INCOMPLETE", "code": None, "execution": "FAIL"}),
    ("FAILED", {"state": "FAILED", "code": "other", "execution": "FAIL"}),
])
def test_journal_state_and_code_come_from_the_bytes(job, journal_state, claim):
    root, core = job

    def mutate(t):
        t["entrypoint_journal"].update(state=claim["state"], code=claim["code"])
        t["execution"] = claim["execution"]

    build(root, journal_state=journal_state, execution="FAIL", mutate_terminal=mutate)
    record = verify(root)
    assert (record["verdict"], record["reason"], record["execution"]) == \
        ("UNPROVEN", "journal_changed", "FAIL")


@pytest.mark.parametrize("journal_state,execution", [
    ("COMPLETE", "PASS"), ("FAILED", "FAIL"), ("INCOMPLETE", "FAIL")])
def test_matching_observation_controls(job, journal_state, execution):
    root, core = job
    build(root, journal_state=journal_state, execution=execution)
    record = verify(root)
    assert (record["verdict"], record["entrypoint_state"], record["execution"]) == \
        ("COMPLETE", journal_state, execution)


def test_journal_now_matches_the_supervisor_rules(tmp_path):
    import importlib.util
    from pathlib import Path
    script = Path(__file__).resolve().parents[3] / "conformance/scripts/entrypoint-supervisor.py"
    spec = importlib.util.spec_from_file_location("ep_sup_rules", script)
    sup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sup)

    def line(seq, kind, payload):
        return (json.dumps({"sequence": seq, "evidence": {"kind": kind, "payload": payload}})
                + "\n").encode()
    samples = [
        line(0, "stage_begin", {}) + line(1, "stage_complete", {"result": "COMPLETE"}),
        line(0, "stage_begin", {}) + line(1, "stage_complete",
                                          {"result": "FAILED", "code": "c"}),
        line(0, "stage_begin", {}),
        line(0, "stage_complete", {"result": "COMPLETE", "x": 1}),
        b"[1]\n", b"not json\n", b'{"sequence": 0}', b"",
        line(0, "stage_complete", {"result": "FAILED", "code": 3}),
    ]
    for i, data in enumerate(samples):
        path = tmp_path / ("j%d" % i)
        path.write_bytes(data)
        assert ee._journal_now(str(path)) == sup.journal_observation(str(path)), data
    assert ee._journal_now(str(tmp_path / "absent")) == \
        sup.journal_observation(str(tmp_path / "absent"))
