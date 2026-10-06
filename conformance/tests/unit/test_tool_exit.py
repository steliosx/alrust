"""Synthetic controls for the stage-tool exit verifier, the tool-exit stage
and the gates' holder check (R-EP plan P2).

Private temporary files, a real core identity and real stage journals only.
No process is created or signalled: an ended generation is a PID that does
not exist, and a live one is this test process itself.
"""
import hashlib
import json
import os
import shutil

import pytest

from alrust_conformance import entrypoint_exit as ee
from alrust_conformance import finalizer as fz
from alrust_conformance import stage_journal as sj
from alrust_conformance import tool_exit as te
from test_private_secret_writer import job

DRIVER_SHA = "c" * 64
PYTHON = "/usr/bin/python3"
EXPECTED = {"driver_sha256": DRIVER_SHA, "interpreter_path": PYTHON}
PINS = {tool: dict(EXPECTED) for tool in te.HOLDER_TOOLS}
ENDED = {"database-create": 4194301, "registry": 4194299}


def _dump(path, obj):
    data = (json.dumps(obj, sort_keys=True) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _own_ticks():
    with open("/proc/self/stat", "rb") as f:
        return int(f.read().rsplit(b")", 1)[1].split()[19])


def tool_stage(root, index, tool, previous, fail=None):
    """The tool's own stage journal, closed as C_T would close it."""
    with pytest.raises(RuntimeError) if fail else _nothing():
        with sj.StageJournal(str(root), index, tool, previous) as journal:
            journal.record("owned_resource", {"path": "/x", "dev": 1, "ino": 2, "type": "dir"})
            if fail:
                journal.fail(fail)
                raise RuntimeError(fail)
    return sj.journal_path(str(root), index, tool)


class _nothing:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def receipts(root, tool, index, *, pid=None, ticks=111, result="tree_empty",
             execution="PASS", mutate_launch=None, mutate_terminal=None,
             write_terminal=True, bind=None):
    """P_T's launch and terminal receipts for the run of TOOL at INDEX, as
    contract section 4 specifies, observing the closed stage journal."""
    pid = ENDED[tool] if pid is None else pid
    if os.path.exists("/proc/%d" % pid) and pid != os.getpid():
        pytest.skip("synthetic ended PID is in use on this host")
    core_bytes = (root / sj.CORE_NAME).read_bytes()
    core = json.loads(core_bytes)
    journal = sj.journal_path(str(root), index, tool)
    observed = ee._journal_now(journal)
    launch_path, terminal_path = te.receipt_paths(str(root), tool, index)
    binding = bind or {"name": tool, "stage_index": index}
    launch = {
        "schema": te.SCHEMA_LAUNCH,
        "supervisor": {"pid": pid, "pgid": pid, "start_ticks": ticks,
                       "uid": os.getuid(), "subreaper": 1},
        "child": {"pid": pid + 1, "start_ticks": ticks + 1},
        "boot_id": core["boot_id"],
        "core_identity_sha256": hashlib.sha256(core_bytes).hexdigest(),
        "command": {"argv_sha256": "e" * 64,
                    "executable": {"path": PYTHON, "dev": 1, "ino": 2},
                    "script": {"path": "/job/stage-tool.py", "dev": 1, "ino": 3,
                               "sha256": DRIVER_SHA}},
        "stage_journal": journal, "terminal_receipt": terminal_path,
        "deadline": {"monotonic_ns": 10**12, "teardown_budget_ns": ee.TEARDOWN_BUDGET_NS,
                     "boot_id": core["boot_id"]},
        "launched_utc_ns": 1, "monotonic_ns": 1, "tool": dict(binding)}
    if mutate_launch:
        mutate_launch(launch)
    from pathlib import Path
    launch_sha = _dump(Path(launch_path), launch)
    terminal = {
        "schema": te.SCHEMA_TERMINAL, "result": result,
        "supervisor": {"pid": pid, "start_ticks": ticks}, "boot_id": core["boot_id"],
        "launch_receipt_sha256": launch_sha, "trigger": "child_exited",
        "child_wait_status": 0, "child_signalled": False, "other_descendant_signals": 0,
        "other_descendants_reaped": 0,
        "stage_journal": observed if result == "tree_empty" else None,
        "execution": execution, "signals_sent": 0, "children_reaped": 1,
        "teardown_ns": 1, "finished_utc_ns": 2, "finished_monotonic_ns": 2,
        "tool": dict(binding)}
    if mutate_terminal:
        mutate_terminal(terminal)
    if write_terminal:
        _dump(Path(terminal_path), terminal)


@pytest.fixture
def create_run(job):
    """database-create closed COMPLETE at index 0, with its receipts."""
    root, core = job
    tool_stage(root, 0, "database-create", None)
    receipts(root, "database-create", 0)
    return root


def verify(root, tool="database-create", index=0, expected=EXPECTED):
    record = te.verify_tool_exit(str(root), tool, index, expected)
    assert set(record) == te.VERDICT_KEYS
    return record


# --- verify_tool_exit ------------------------------------------------------

def test_complete_and_pass(create_run):
    record = verify(create_run)
    assert (record["verdict"], record["reason"], record["execution"],
            record["tool"], record["stage_index"], record["stage_state"]) == \
        ("COMPLETE", "tree_empty", "PASS", "database-create", 0, "COMPLETE")
    assert record["stage_journal_sha256"] == hashlib.sha256(open(
        sj.journal_path(str(create_run), 0, "database-create"), "rb").read()).hexdigest()


def test_failed_tool_stage_keeps_closure_with_execution_fail(job):
    root, core = job
    tool_stage(root, 0, "database-create", None, fail="name_already_exists")
    receipts(root, "database-create", 0, execution="FAIL")
    record = verify(root)
    assert (record["verdict"], record["stage_state"], record["execution"]) == \
        ("COMPLETE", "FAILED", "FAIL")


def test_tree_not_empty_is_failed(job):
    root, core = job
    tool_stage(root, 0, "database-create", None)
    receipts(root, "database-create", 0, result="tree_not_empty", execution="FAIL")
    assert (verify(root)["verdict"], verify(root)["reason"]) == ("FAILED", "tree_not_empty")


@pytest.mark.parametrize("kwargs,reason", [
    # A receipt written for another tool or another stage index (copied or
    # replayed) never proves this run.
    ({"bind": {"name": "registry", "stage_index": 0}}, "tool_mismatch"),
    ({"bind": {"name": "database-create", "stage_index": 2}}, "tool_mismatch"),
    ({"mutate_launch": lambda l: l.update(tool={"name": "database-create"})},
     "tool_binding_malformed"),
    ({"mutate_launch": lambda l: l.update(tool={"name": "database-create",
                                                "stage_index": True})},
     "tool_binding_malformed"),
    ({"mutate_launch": lambda l: l.pop("tool")}, "launch_receipt_malformed"),
    ({"mutate_terminal": lambda t: t.update(tool={"name": "registry", "stage_index": 0})},
     "terminal_link_mismatch"),
    ({"mutate_terminal": lambda t: t.pop("tool")}, "terminal_receipt_malformed"),
    # An entrypoint receipt is not a tool receipt.
    ({"mutate_launch": lambda l: l.update(schema=ee.SCHEMA_LAUNCH)},
     "launch_receipt_malformed"),
    ({"mutate_terminal": lambda t: t.update(schema=ee.SCHEMA_TERMINAL)},
     "terminal_receipt_malformed"),
    ({"mutate_launch": lambda l: l.update(entrypoint_journal=l.pop("stage_journal"))},
     "launch_receipt_malformed"),
    # Replay from another run or job: core, boot, journal or path differ.
    ({"mutate_launch": lambda l: l.update(core_identity_sha256="0" * 64)}, "core_mismatch"),
    ({"mutate_launch": lambda l: l.update(boot_id="00000000-0000-0000-0000-000000000000")},
     "boot_mismatch"),
    ({"mutate_launch": lambda l: l.update(stage_journal="/elsewhere/00-database-create.jsonl")},
     "receipt_paths_mismatch"),
    ({"mutate_launch": lambda l: l.update(terminal_receipt="/elsewhere/terminal.json")},
     "receipt_paths_mismatch"),
    # Independent pins.
    ({"mutate_launch": lambda l: l["command"]["script"].update(sha256="0" * 64)},
     "tool_driver_mismatch"),
    ({"mutate_launch": lambda l: l["command"]["executable"].update(path="/tmp/python")},
     "interpreter_mismatch"),
    ({"mutate_launch": lambda l: l["deadline"].update(teardown_budget_ns=1)},
     "deadline_invalid"),
    ({"write_terminal": False}, "terminal_receipt_unavailable"),
    ({"mutate_terminal": lambda t: t["supervisor"].update(start_ticks=999)},
     "terminal_link_mismatch"),
    ({"execution": "FAIL"}, "execution_contradiction"),
    ({"mutate_terminal": lambda t: t["stage_journal"].update(state="FAILED", code="x")},
     "journal_changed"),
])
def test_refusals_are_unproven_with_fixed_reasons(job, kwargs, reason):
    root, core = job
    tool_stage(root, 0, "database-create", None)
    receipts(root, "database-create", 0, **kwargs)
    record = verify(root)
    assert (record["verdict"], record["reason"], record["execution"]) == \
        ("UNPROVEN", reason, "FAIL")


def test_entrypoint_receipts_at_the_tool_path_refuse(job):
    root, core = job
    tool_stage(root, 0, "database-create", None)
    receipts(root, "database-create", 0)
    launch_path, _ = te.receipt_paths(str(root), "database-create", 0)
    data = json.loads(open(launch_path).read())
    data["schema"] = ee.SCHEMA_LAUNCH
    data["entrypoint_journal"] = data.pop("stage_journal")
    data.pop("tool")
    _dump(__import__("pathlib").Path(launch_path), data)
    assert verify(root)["reason"] == "launch_receipt_malformed"


def test_receipts_moved_to_another_stage_index_refuse(job):
    root, core = job
    tool_stage(root, 0, "database-create", None)
    receipts(root, "database-create", 0)
    src = os.path.dirname(te.receipt_paths(str(root), "database-create", 0)[0])
    dst = os.path.dirname(te.receipt_paths(str(root), "database-create", 2)[0])
    shutil.copytree(src, dst)
    assert verify(root, index=2)["reason"] in ("tool_mismatch", "receipt_paths_mismatch")


def test_missing_launch_receipt(create_run):
    os.unlink(te.receipt_paths(str(create_run), "database-create", 0)[0])
    assert verify(create_run)["reason"] == "launch_receipt_unavailable"


def test_binding_index_beyond_the_last_tool_index_is_malformed(job):
    root, core = job
    tool_stage(root, 0, "database-create", None)
    receipts(root, "database-create", 0,
             mutate_launch=lambda l: l["tool"].update(stage_index=sj.MAX_STAGES - 1))
    assert verify(root)["reason"] == "tool_binding_malformed"


def test_journal_changed_after_observation(create_run):
    with open(sj.journal_path(str(create_run), 0, "database-create"), "ab") as f:
        f.write(b"late write\n")
    assert verify(create_run)["reason"] == "journal_changed"


def test_live_generation_is_unproven(job):
    root, core = job
    tool_stage(root, 0, "database-create", None)
    receipts(root, "database-create", 0, pid=os.getpid(), ticks=_own_ticks())
    assert verify(root)["reason"] == "supervisor_generation_live_or_unproven"


@pytest.mark.parametrize("expected", [
    None, {}, {"driver_sha256": "x", "interpreter_path": PYTHON},
    {"driver_sha256": DRIVER_SHA, "interpreter_path": "relative"},
    dict(EXPECTED, extra=1),
    {"entrypoint_script_sha256": DRIVER_SHA, "interpreter_path": PYTHON},
])
def test_bad_expected_pins_refuse(create_run, expected):
    assert verify(create_run, expected=expected)["reason"] == "expected_pins_invalid"


@pytest.mark.parametrize("tool,index", [
    ("database-drop", 0), ("entrypoint", 0), ("database-create", -1),
    ("database-create", True), ("database-create", sj.MAX_STAGES - 1)])
def test_unknown_tool_or_index_refuses(create_run, tool, index):
    assert verify(create_run, tool, index)["reason"] == "tool_invalid"


# --- run_tool_exit_stage ---------------------------------------------------

def _records(root, index, name):
    data = open(sj.journal_path(str(root), index, name), "rb").read()
    return [json.loads(line)["evidence"] for line in data.splitlines()]


def test_exit_stage_records_one_verdict_and_closes_complete(create_run):
    record = te.run_tool_exit_stage(str(create_run), ["database-create"],
                                    "database-create", EXPECTED)
    lines = _records(create_run, 1, "database-create-exit")
    assert [e["kind"] for e in lines] == ["stage_begin", "tool_exit_verdict", "stage_complete"]
    assert lines[1]["payload"] == record and record["verdict"] == "COMPLETE"
    assert lines[2]["payload"] == {"result": "COMPLETE"}
    # The tool's own journal is untouched (no backfill into a closed journal).
    assert verify(create_run)["stage_journal_sha256"] == record["stage_journal_sha256"]


def test_exit_stage_closes_failed_for_unproven(job):
    root, core = job
    tool_stage(root, 0, "database-create", None)
    receipts(root, "database-create", 0, write_terminal=False)
    record = te.run_tool_exit_stage(str(root), ["database-create"], "database-create", EXPECTED)
    lines = _records(root, 1, "database-create-exit")
    assert record["verdict"] == "UNPROVEN"
    assert lines[-1]["payload"] == {"result": "FAILED", "code": "tool_exit_unproven"}


@pytest.mark.parametrize("prefix,tool", [
    (["database-create"], "registry"),
    (["database-create", "launch"], "database-create"),
    (["database-create", "database-create"], "database-create"),
    (["database-create-exit", "database-create"], "database-create"),
    (["database-drop"], "database-drop"),
    ([], "database-create"),
])
def test_exit_stage_prefix_must_end_with_the_single_tool(create_run, prefix, tool):
    with pytest.raises(sj.JournalError, match="tool_exit_prefix_invalid"):
        te.run_tool_exit_stage(str(create_run), prefix, tool, EXPECTED)


# --- required_holders ------------------------------------------------------

NORMAL = ["database-create", "database-create-exit", "launch", "launch-confirm",
          "entrypoint", "entrypoint-exit", "stop", "registry", "registry-exit"]


@pytest.mark.parametrize("names,required,holders", [
    (NORMAL, ("database-create", "registry"), [("database-create", 0), ("registry", 7)]),
    (NORMAL, ("database-create",), [("database-create", 0), ("registry", 7)]),
    (NORMAL[:7], ("database-create",), [("database-create", 0)]),
    (NORMAL[:2], (), [("database-create", 0)]),
    (["launch"], (), []),
])
def test_required_holders_from_names(names, required, holders):
    assert te.required_holders(names, required) == holders


@pytest.mark.parametrize("names,required,code", [
    (NORMAL[:7], ("database-create", "registry"), "holder_stage_missing"),
    (["launch"], ("database-create",), "holder_stage_missing"),
    (["database-create"], (), "holder_exit_stage_missing_or_unexpected"),
    (["database-create", "launch", "database-create-exit"], (),
     "holder_exit_stage_not_adjacent"),
    (["database-create-exit"], (), "holder_exit_stage_missing_or_unexpected"),
    (NORMAL + ["registry-exit"], (), "holder_exit_stage_missing_or_unexpected"),
    (NORMAL + ["registry", "registry-exit"], (), "holder_stage_repeated"),
    (NORMAL, ("database-drop",), "holder_request_invalid"),
    (NORMAL, ["database-create"], "holder_request_invalid"),
    (NORMAL, ("registry", "registry"), "holder_request_invalid"),
    ("database-create", (), "holder_request_invalid"),
])
def test_required_holders_refusals(names, required, code):
    with pytest.raises(te.HolderError, match=code):
        te.required_holders(names, required)


# --- holder_proofs ---------------------------------------------------------

def two_holders(root):
    """database-create, its exit stage, registry, its exit stage: indices 0-3."""
    tool_stage(root, 0, "database-create", None)
    receipts(root, "database-create", 0)
    te.run_tool_exit_stage(str(root), ["database-create"], "database-create", EXPECTED)
    tool_stage(root, 2, "registry", "database-create-exit")
    receipts(root, "registry", 2)
    te.run_tool_exit_stage(str(root), ["database-create", "database-create-exit",
                                       "registry"], "registry", EXPECTED)
    names = ["database-create", "database-create-exit", "registry", "registry-exit"]
    aggregation = sj.aggregate(str(root), names)
    assert aggregation["verdict"] == "VALID"
    return aggregation, fz._journal_records(str(root), aggregation)


def test_holder_proofs_positive(job):
    root, core = job
    aggregation, records = two_holders(root)
    proven = te.holder_proofs(str(root), aggregation, records,
                              ("database-create", "registry"), PINS)
    assert [(p["tool"], p["stage_index"], p["verdict"]) for p in proven] == \
        [("database-create", 0, "COMPLETE"), ("registry", 2, "COMPLETE")]


def _forge(records, stage, change):
    out = []
    for s, kind, payload in records:
        if kind == "tool_exit_verdict" and s == stage:
            payload = dict(payload)
            change(payload)
        out.append((s, kind, payload))
    return out


@pytest.mark.parametrize("mutate,code", [
    # An offered verdict that differs in any field from the recomputation.
    (lambda r: _forge(r, "registry-exit", lambda p: p.update(execution="FAIL")),
     "holder_verdict_mismatch"),
    (lambda r: _forge(r, "registry-exit", lambda p: p.update(stage_index=7)),
     "holder_verdict_mismatch"),
    (lambda r: _forge(r, "database-create-exit", lambda p: p.pop("trigger")),
     "holder_verdict_malformed"),
    # Missing, duplicated or misplaced verdicts.
    (lambda r: [x for x in r if not (x[1] == "tool_exit_verdict" and x[0] == "registry-exit")],
     "holder_verdict_set_invalid"),
    (lambda r: r + [x for x in r if x[1] == "tool_exit_verdict"][:1],
     "holder_verdict_set_invalid"),
    (lambda r: r + [("registry", "tool_exit_verdict",
                     [x for x in r if x[1] == "tool_exit_verdict"][1][2])],
     "holder_verdict_set_invalid"),
])
def test_holder_proofs_refuse_offered_records(job, mutate, code):
    root, core = job
    aggregation, records = two_holders(root)
    with pytest.raises(te.HolderError, match=code):
        te.holder_proofs(str(root), aggregation, mutate(records),
                         ("database-create", "registry"), PINS)


def test_holder_proofs_recompute_receipts_now(job):
    root, core = job
    aggregation, records = two_holders(root)
    # The terminal receipt changes after the verdict was recorded.
    _, terminal = te.receipt_paths(str(root), "registry", 2)
    os.unlink(terminal)
    with pytest.raises(te.HolderError, match="holder_verdict_mismatch"):
        te.holder_proofs(str(root), aggregation, records,
                         ("database-create", "registry"), PINS)


def test_holder_proofs_refuse_a_now_live_generation(job, monkeypatch):
    root, core = job
    aggregation, records = two_holders(root)
    monkeypatch.setattr(ee, "generation_ended", lambda *a: False)
    with pytest.raises(te.HolderError, match="holder_verdict_mismatch"):
        te.holder_proofs(str(root), aggregation, records, ("database-create",), PINS)


def _not_proven_run(root):
    tool_stage(root, 0, "database-create", None)
    receipts(root, "database-create", 0, result="tree_not_empty", execution="FAIL")
    te.run_tool_exit_stage(str(root), ["database-create"], "database-create", EXPECTED)
    aggregation = sj.aggregate(str(root), ["database-create", "database-create-exit"])
    return aggregation, fz._journal_records(str(root), aggregation)


def test_holder_proofs_refuse_a_failed_exit_stage(job):
    root, core = job
    aggregation, records = _not_proven_run(root)
    assert aggregation["verdict"] == "FAILED"
    with pytest.raises(te.HolderError, match="holder_exit_stage_not_complete"):
        te.holder_proofs(str(root), aggregation, records, ("database-create",), PINS)


def test_holder_proofs_refuse_a_non_complete_verdict_even_if_the_stage_says_complete(job):
    root, core = job
    aggregation, records = _not_proven_run(root)
    forged = dict(aggregation, stages=[dict(s, result="COMPLETE")
                                       for s in aggregation["stages"]])
    with pytest.raises(te.HolderError, match="holder_exit_not_proven"):
        te.holder_proofs(str(root), forged, records, ("database-create",), PINS)


def test_holder_proofs_refuse_an_incomplete_aggregation(job):
    root, core = job
    aggregation, records = two_holders(root)
    with pytest.raises(te.HolderError, match="holder_aggregation_incomplete"):
        te.holder_proofs(str(root), dict(aggregation, verdict="INCOMPLETE"), records,
                         ("database-create",), PINS)


def test_holder_proofs_bind_the_tool_journal_to_the_aggregation(job):
    root, core = job
    aggregation, records = two_holders(root)
    stages = [dict(s) for s in aggregation["stages"]]
    stages[2]["sha256"] = "0" * 64
    with pytest.raises(te.HolderError, match="holder_journal_mismatch"):
        te.holder_proofs(str(root), dict(aggregation, stages=stages), records,
                         ("database-create", "registry"), PINS)


@pytest.mark.parametrize("pins", [
    None, {}, {"database-create": EXPECTED},
    dict(PINS, registry={"driver_sha256": "x", "interpreter_path": PYTHON}),
    dict(PINS, extra=EXPECTED),
])
def test_holder_proofs_need_both_pins(job, pins):
    root, core = job
    aggregation, records = two_holders(root)
    with pytest.raises(te.HolderError, match="holder_pins_invalid"):
        te.holder_proofs(str(root), aggregation, records, ("database-create",), pins)


def test_holder_proofs_pins_are_compared_independently(job):
    root, core = job
    aggregation, records = two_holders(root)
    pins = dict(PINS, registry={"driver_sha256": "f" * 64, "interpreter_path": PYTHON})
    with pytest.raises(te.HolderError, match="holder_verdict_mismatch"):
        te.holder_proofs(str(root), aggregation, records, ("database-create",), pins)


def test_holder_proofs_required_registry_absent(job):
    root, core = job
    tool_stage(root, 0, "database-create", None)
    receipts(root, "database-create", 0)
    te.run_tool_exit_stage(str(root), ["database-create"], "database-create", EXPECTED)
    aggregation = sj.aggregate(str(root), ["database-create", "database-create-exit"])
    records = fz._journal_records(str(root), aggregation)
    assert len(te.holder_proofs(str(root), aggregation, records, ("database-create",),
                                PINS)) == 1
    with pytest.raises(te.HolderError, match="holder_stage_missing"):
        te.holder_proofs(str(root), aggregation, records,
                         ("database-create", "registry"), PINS)


# --- P2A-SOURCE-R1: the offered verdict is typed, through real journals ----

def _exit_stage_with(root, index, tool, payload):
    """Record PAYLOAD as the exit stage's single verdict through the actual
    StageJournal, as a forger with write access to the exit stage could."""
    with sj.StageJournal(str(root), index + 1, te.exit_stage(tool), tool) as journal:
        journal.record("tool_exit_verdict", payload)


def _typed_case(root, change):
    tool_stage(root, 0, "database-create", None)
    receipts(root, "database-create", 0)
    payload = te.verify_tool_exit(str(root), "database-create", 0, EXPECTED)
    assert payload["verdict"] == "COMPLETE"
    change(payload)
    _exit_stage_with(root, 0, "database-create", payload)
    aggregation = sj.aggregate(str(root), ["database-create", "database-create-exit"])
    assert aggregation["verdict"] == "VALID"
    return aggregation, fz._journal_records(str(root), aggregation)


def test_typed_offered_verdict_with_exact_int_index_is_accepted(job):
    root, core = job
    aggregation, records = _typed_case(root, lambda p: None)
    assert type([p for s, k, p in records if k == "tool_exit_verdict"][0]["stage_index"]) is int
    assert len(te.holder_proofs(str(root), aggregation, records, ("database-create",),
                                PINS)) == 1


@pytest.mark.parametrize("change", [
    lambda p: p.update(stage_index=False),
    lambda p: p.update(stage_index=0.0),
    lambda p: p.update(stage_index="0"),
    lambda p: p.update(verdict="complete"),
    lambda p: p.update(execution=None),
    lambda p: p.update(stage_state="DONE"),
    lambda p: p.update(trigger=1),
    lambda p: p.update(launch_receipt_sha256="A" * 64),
    lambda p: p.update(tool=["database-create"]),
])
def test_loosely_equal_or_mistyped_offered_verdict_refuses(job, change):
    root, core = job
    aggregation, records = _typed_case(root, change)
    with pytest.raises(te.HolderError, match="holder_verdict_malformed"):
        te.holder_proofs(str(root), aggregation, records, ("database-create",), PINS)


def test_canonical_comparison_is_type_sensitive():
    assert te._canonical({"a": 0}) != te._canonical({"a": False})
    assert te._canonical({"a": 0}) != te._canonical({"a": 0.0})
    assert te._canonical({"a": 1, "b": 2}) == te._canonical({"b": 2, "a": 1})
