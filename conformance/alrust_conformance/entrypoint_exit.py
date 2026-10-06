"""The entrypoint-exit stage: verify the entrypoint supervisor's receipts.

Design: conformance/entrypoint-exit-proof-design.md, approved by Codex at
409eabfa (branch claude/ci-entrypoint-exit-proof), section 4; item 2 of its
section 8, verifier and stage only. The gate rechecks in writer_quiescence,
finalize() and failed_entrypoint_quiescence are a later head.

verify_entrypoint_exit() reads only immutable proof inputs: the core file,
the two receipts written by entrypoint-supervisor.py at fixed paths under
the job root, the closed entrypoint journal, and /proc. Expected values come
from the core, the job root and the caller's pins, never from the receipt
being checked. It never signals: a still-live supervisor generation is
UNPROVEN here. (The design's "TERM via pidfd" row for a timed-out step is a
separate, later stop helper.)

The receipt checks are shared with the stage-tool verifier (tool_exit.py,
R-EP plan P2) through a receipt spec; ENTRYPOINT is this stage's spec, and
the entrypoint receipts, checks, reasons and verdict record are unchanged.
"""
import hashlib
import json
import os

from . import stage_journal as _sj
from .finalizer import generation_ended

SCHEMA_LAUNCH = "alrust-entrypoint-launch/1"
SCHEMA_TERMINAL = "alrust-entrypoint-terminal/1"
RECEIPT_DIR = "entrypoint"
LAUNCH_NAME = "launch.json"
TERMINAL_NAME = "terminal.json"
STAGE = "entrypoint-exit"
ENTRYPOINT_STAGE = "entrypoint"
MAX_RECEIPT = 1 << 16
TEARDOWN_BUDGET_NS = 16_000_000_000
_PID_LIMIT = 4194304

# What differs between the entrypoint receipts and a stage tool's: the schema
# labels, the key naming the observed journal, the tool binding (None for the
# entrypoint) and the reason given for a script mismatch.
ENTRYPOINT = {"launch": SCHEMA_LAUNCH, "terminal": SCHEMA_TERMINAL,
              "journal_key": "entrypoint_journal", "tool": None,
              "script_reason": "entrypoint_script_mismatch"}

VERDICT_KEYS = frozenset({"verdict", "reason", "launch_receipt_sha256",
                          "terminal_receipt_sha256", "entrypoint_journal_sha256",
                          "entrypoint_state", "execution", "trigger"})


class _Refusal(Exception):
    def __init__(self, verdict, reason):
        super().__init__(reason)
        self.verdict, self.reason = verdict, reason


def _need(condition, reason, verdict="UNPROVEN"):
    if not condition:
        raise _Refusal(verdict, reason)


def _sha(value):
    return type(value) is str and len(value) == 64 and not value.strip("0123456789abcdef")


def _int(value, low, high):
    return type(value) is int and low <= value <= high


_TRIGGERS = ("child_exited", "deadline", "stop_requested")
_MAX = (1 << 63) - 1


def _is_int(value, low=0, high=_MAX):
    # bool is a subclass of int; exact type only (REP3-R1).
    return type(value) is int and low <= value <= high


def _tool_keys(spec):
    return {"tool"} if spec["tool"] is not None else set()


def _check_tool_binding(value):
    _need(type(value) is dict and set(value) == {"name", "stage_index"}
          and type(value["name"]) is str
          and _is_int(value["stage_index"], 0, _sj.MAX_STAGES - 2),
          "tool_binding_malformed")


def _check_launch_schema(launch, spec=ENTRYPOINT):
    """Exact keys and types of alrust-entrypoint-launch/1 (REP3-R1), or of
    the stage-tool launch receipt named by SPEC."""
    _need(type(launch) is dict and set(launch) == {
        "schema", "supervisor", "child", "boot_id", "core_identity_sha256", "command",
        spec["journal_key"], "terminal_receipt", "deadline", "launched_utc_ns",
        "monotonic_ns"} | _tool_keys(spec) and launch["schema"] == spec["launch"],
        "launch_receipt_malformed")
    if spec["tool"] is not None:
        _check_tool_binding(launch["tool"])
    sup, child = launch["supervisor"], launch["child"]
    _need(type(sup) is dict and set(sup) == {"pid", "pgid", "start_ticks", "uid", "subreaper"}
          and all(_is_int(sup[k]) for k in sup), "launch_receipt_malformed")
    _need(type(child) is dict and set(child) == {"pid", "start_ticks"}
          and _is_int(child["pid"], 2, _PID_LIMIT)
          and (child["start_ticks"] is None or _is_int(child["start_ticks"], 1)),
          "launch_receipt_malformed")
    command = launch["command"]
    _need(type(command) is dict and set(command) == {"argv_sha256", "executable", "script"}
          and _sha(command["argv_sha256"])
          and type(command["executable"]) is dict
          and set(command["executable"]) == {"path", "dev", "ino"}
          and type(command["executable"]["path"]) is str
          and _is_int(command["executable"]["dev"]) and _is_int(command["executable"]["ino"])
          and type(command["script"]) is dict
          and set(command["script"]) == {"path", "dev", "ino", "sha256"}
          and type(command["script"]["path"]) is str
          and _is_int(command["script"]["dev"]) and _is_int(command["script"]["ino"])
          and _sha(command["script"]["sha256"]), "launch_receipt_malformed")
    deadline = launch["deadline"]
    _need(type(deadline) is dict and set(deadline) == {"monotonic_ns", "teardown_budget_ns",
                                                       "boot_id"}
          and _is_int(deadline["monotonic_ns"], 1) and _is_int(deadline["teardown_budget_ns"])
          and type(deadline["boot_id"]) is str, "launch_receipt_malformed")
    _need(all(type(launch[k]) is str for k in ("boot_id", "core_identity_sha256",
                                               spec["journal_key"], "terminal_receipt"))
          and _is_int(launch["launched_utc_ns"]) and _is_int(launch["monotonic_ns"]),
          "launch_receipt_malformed")


def _check_terminal_schema(terminal, spec=ENTRYPOINT):
    """Exact keys and types of alrust-entrypoint-terminal/1 (REP3-R1), or of
    the stage-tool terminal receipt named by SPEC. Nullable fields stay
    nullable only where the producer legitimately writes null."""
    _need(type(terminal) is dict and set(terminal) == {
        "schema", "result", "supervisor", "boot_id", "launch_receipt_sha256", "trigger",
        "child_wait_status", "child_signalled", "other_descendant_signals",
        "other_descendants_reaped", spec["journal_key"], "execution", "signals_sent",
        "children_reaped", "teardown_ns", "finished_utc_ns", "finished_monotonic_ns"}
        | _tool_keys(spec) and terminal["schema"] == spec["terminal"],
        "terminal_receipt_malformed")
    if spec["tool"] is not None:
        _check_tool_binding(terminal["tool"])
    _need(terminal["result"] in ("tree_empty", "tree_not_empty")
          and terminal["trigger"] in _TRIGGERS
          and terminal["execution"] in ("PASS", "FAIL")
          and type(terminal["child_signalled"]) is bool
          and (terminal["child_wait_status"] is None or _is_int(terminal["child_wait_status"],
                                                                0, 65535))
          and all(_is_int(terminal[k]) for k in (
              "other_descendant_signals", "other_descendants_reaped", "signals_sent",
              "children_reaped", "teardown_ns", "finished_utc_ns", "finished_monotonic_ns"))
          and type(terminal["boot_id"]) is str and _sha(terminal["launch_receipt_sha256"]),
          "terminal_receipt_malformed")
    observed = terminal[spec["journal_key"]]
    if terminal["result"] == "tree_not_empty":
        # No ECHILD: the journal is never read, and C_E may be unreaped.
        _need(observed is None, "terminal_receipt_malformed")
        return
    _need(type(observed) is dict and set(observed) == {"sha256", "state", "code"}
          and observed["state"] in ("COMPLETE", "FAILED", "INCOMPLETE")
          and (observed["sha256"] is None or _sha(observed["sha256"]))
          and ((observed["state"] == "FAILED") == (type(observed["code"]) is str))
          and (observed["code"] is None or type(observed["code"]) is str),
          "journal_observation_invalid")
    # ECHILD means every child, C_E included, was reaped with a status.
    _need(_is_int(terminal["child_wait_status"], 0, 65535), "receipt_contradiction")


def _journal_now(path):
    """The supervisor's journal observation, derived here from the current
    bytes (same rules as journal_observation() in entrypoint-supervisor.py):
    COMPLETE or FAILED only from a well-formed final stage_complete record,
    otherwise INCOMPLETE; a missing journal is (None, INCOMPLETE, None).
    A journal that exists but cannot be read is never treated as absent."""
    if not os.path.lexists(path):
        return {"sha256": None, "state": "INCOMPLETE", "code": None}
    try:
        data = _sj.read_bounded(path, _sj.MAX_JOURNAL)
    except (OSError, _sj.JournalError):
        raise _Refusal("UNPROVEN", "journal_changed") from None
    digest = hashlib.sha256(data).hexdigest()
    incomplete = {"sha256": digest, "state": "INCOMPLETE", "code": None}
    if not data.endswith(b"\n"):
        return incomplete
    try:
        last = json.loads(data[:-1].split(b"\n")[-1])
        if type(last) is not dict or set(last) != {"sequence", "evidence"}:
            return incomplete
        evidence = last["evidence"]
        if type(evidence) is not dict or evidence.get("kind") != "stage_complete":
            return incomplete
        payload = evidence.get("payload")
    except (ValueError, KeyError, TypeError, IndexError):
        return incomplete
    if payload == {"result": "COMPLETE"}:
        return {"sha256": digest, "state": "COMPLETE", "code": None}
    if (type(payload) is dict and set(payload) == {"result", "code"}
            and payload["result"] == "FAILED" and type(payload["code"]) is str):
        return {"sha256": digest, "state": "FAILED", "code": payload["code"]}
    return incomplete


def _derived_execution(terminal, spec=ENTRYPOINT):
    """Execution from the evidence, never from the terminal's own label."""
    status = terminal["child_wait_status"]
    if (terminal["trigger"] == "child_exited" and status is not None
            and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
            and not terminal["child_signalled"]
            and terminal[spec["journal_key"]]["state"] == "COMPLETE"):
        return "PASS"
    return "FAIL"


def _generation(value):
    _need(type(value) is dict and _int(value.get("pid"), 2, _PID_LIMIT)
          and _int(value.get("start_ticks"), 1, (1 << 63) - 1), "receipt_generation_invalid")
    return value["pid"], value["start_ticks"]


def receipt_paths(job_root):
    base = os.path.join(job_root, RECEIPT_DIR)
    return os.path.join(base, LAUNCH_NAME), os.path.join(base, TERMINAL_NAME)


def verify_receipts(job_root, spec, journal, launch_path, terminal_path,
                    script_sha256, interpreter_path, core_bytes=None):
    """The receipt verification shared by the entrypoint-exit and tool-exit
    stages. SCRIPT_SHA256 and INTERPRETER_PATH are the caller's validated
    pins; JOURNAL and the receipt paths are derived by the caller from the
    job root and stage index, never from a receipt. Returns {"verdict",
    "reason", "launch_receipt_sha256", "terminal_receipt_sha256",
    "journal_sha256", "journal_state", "execution", "trigger"}.

    CORE_BYTES, when given, is the caller's single core snapshot: every core
    check here then uses those exact bytes, never a second read
    (P2B1-SOURCE-R1). The entrypoint stage passes none and reads it once."""
    record = {"verdict": "UNPROVEN", "reason": "not_checked",
              "launch_receipt_sha256": None, "terminal_receipt_sha256": None,
              "journal_sha256": None, "journal_state": None,
              "execution": "FAIL", "trigger": None}
    try:
        if core_bytes is None:
            core_bytes = _sj.read_bounded(os.path.join(job_root, _sj.CORE_NAME),
                                          _sj.MAX_CORE)
        core = _sj.validate_core(core_bytes, job_root)
        core_sha256 = hashlib.sha256(core_bytes).hexdigest()
        try:
            launch_bytes = _sj.read_bounded(launch_path, MAX_RECEIPT)
        except (OSError, _sj.JournalError):
            raise _Refusal("UNPROVEN", "launch_receipt_unavailable") from None
        record["launch_receipt_sha256"] = hashlib.sha256(launch_bytes).hexdigest()
        launch = _sj.strict_json(launch_bytes, "launch_receipt_malformed")
        _check_launch_schema(launch, spec)
        if spec["tool"] is not None:
            # The receipt names the tool run it was written for (P2): a
            # receipt copied from another tool or stage index refuses.
            _need(launch["tool"] == spec["tool"], "tool_mismatch")
        launched = _generation(launch.get("supervisor"))
        sup = launch["supervisor"]
        # Independent expectations: the core file, the job root, the pins.
        _need(launch.get("boot_id") == core["boot_id"], "boot_mismatch")
        _need(launch.get("core_identity_sha256") == core_sha256, "core_mismatch")
        _need(sup.get("pgid") == sup["pid"] and sup.get("uid") == os.getuid()
              and sup.get("subreaper") == 1, "supervisor_identity_invalid")
        command = launch.get("command")
        _need(type(command) is dict and type(command.get("script")) is dict
              and command["script"].get("sha256") == script_sha256,
              spec["script_reason"])
        _need(type(command.get("executable")) is dict
              and command["executable"].get("path") == interpreter_path,
              "interpreter_mismatch")
        _need(launch.get(spec["journal_key"]) == journal
              and launch.get("terminal_receipt") == terminal_path, "receipt_paths_mismatch")
        deadline = launch.get("deadline")
        _need(type(deadline) is dict and deadline.get("boot_id") == core["boot_id"]
              and deadline.get("teardown_budget_ns") == TEARDOWN_BUDGET_NS
              and _int(deadline.get("monotonic_ns"), 1, (1 << 63) - 1),
              "deadline_invalid")
        try:
            terminal_bytes = _sj.read_bounded(terminal_path, MAX_RECEIPT)
        except (OSError, _sj.JournalError):
            # No terminal receipt: killed from outside, still running, or an
            # interrupted write. Never a proof of exit.
            raise _Refusal("UNPROVEN", "terminal_receipt_unavailable") from None
        record["terminal_receipt_sha256"] = hashlib.sha256(terminal_bytes).hexdigest()
        terminal = _sj.strict_json(terminal_bytes, "terminal_receipt_malformed")
        _check_terminal_schema(terminal, spec)
        _need(type(terminal.get("supervisor")) is dict
              and set(terminal["supervisor"]) == {"pid", "start_ticks"}
              and _generation(terminal["supervisor"]) == launched
              and terminal.get("boot_id") == core["boot_id"]
              and terminal.get("launch_receipt_sha256") == record["launch_receipt_sha256"]
              and (spec["tool"] is None or terminal["tool"] == spec["tool"]),
              "terminal_link_mismatch")
        record["trigger"] = terminal.get("trigger")
        if terminal.get("result") == "tree_not_empty":
            raise _Refusal("FAILED", "tree_not_empty")
        # REP4-R1: the whole (sha256, state, code) tuple is derived from the
        # current journal bytes and must equal the receipt's observation;
        # only then is it used to derive execution.
        observed = terminal[spec["journal_key"]]
        _need(_journal_now(journal) == observed, "journal_changed")
        # The child is signalled only before its status is collected, so a
        # child that exited on its own was never signalled (REP1-R1 fix).
        _need(not (terminal["trigger"] == "child_exited" and terminal["child_signalled"]),
              "receipt_contradiction")
        _need(terminal["execution"] == _derived_execution(terminal, spec),
              "execution_contradiction")
        record["journal_state"] = observed["state"]
        record["journal_sha256"] = observed["sha256"]
        # The receipts are expected bindings; this is the positive observation.
        _need(generation_ended(launched[0], launched[1], core["boot_id"]),
              "supervisor_generation_live_or_unproven")
        record.update(verdict="COMPLETE", reason="tree_empty",
                      execution=terminal["execution"])
    except _Refusal as refusal:
        record.update(verdict=refusal.verdict, reason=refusal.reason, execution="FAIL")
    except (_sj.JournalError, OSError, ValueError, TypeError, KeyError):
        record.update(verdict="UNPROVEN", reason="evidence_unreadable", execution="FAIL")
    return record


def verify_entrypoint_exit(job_root, journal_index, expected):
    """Return one entrypoint_exit_verdict record (design section 4).

    EXPECTED holds the workflow head's pins: {"entrypoint_script_sha256",
    "interpreter_path"}. verdict is COMPLETE (closure proven), FAILED
    (terminal tree_not_empty with valid linkage) or UNPROVEN. execution is
    the terminal's value only when verdict is COMPLETE, otherwise FAIL."""
    if not (type(expected) is dict and set(expected) == {
            "entrypoint_script_sha256", "interpreter_path"}
            and _sha(expected["entrypoint_script_sha256"])
            and type(expected["interpreter_path"]) is str
            and os.path.isabs(expected["interpreter_path"])):
        shared = {"verdict": "UNPROVEN", "reason": "expected_pins_invalid",
                  "launch_receipt_sha256": None, "terminal_receipt_sha256": None,
                  "journal_sha256": None, "journal_state": None,
                  "execution": "FAIL", "trigger": None}
    else:
        launch_path, terminal_path = receipt_paths(job_root)
        shared = verify_receipts(
            job_root, ENTRYPOINT, _sj.journal_path(job_root, journal_index, ENTRYPOINT_STAGE),
            launch_path, terminal_path, expected["entrypoint_script_sha256"],
            expected["interpreter_path"])
    record = {key: shared[key] for key in ("verdict", "reason", "launch_receipt_sha256",
                                           "terminal_receipt_sha256", "execution", "trigger")}
    record["entrypoint_journal_sha256"] = shared["journal_sha256"]
    record["entrypoint_state"] = shared["journal_state"]
    return record


def run_entrypoint_exit_stage(job_root, prefix, expected):
    """Open the entrypoint-exit stage right after the entrypoint stage and
    append exactly one entrypoint_exit_verdict. The stage closes COMPLETE
    only for verdict COMPLETE; otherwise it closes FAILED with the reason.
    Returns the record."""
    if not (type(prefix) is list and prefix and prefix[-1] == ENTRYPOINT_STAGE
            and prefix.count(ENTRYPOINT_STAGE) == 1 and STAGE not in prefix):
        raise _sj.JournalError("entrypoint_exit_prefix_invalid")
    index = len(prefix)
    record = verify_entrypoint_exit(job_root, index - 1, expected)
    journal = _sj.StageJournal(job_root, index, STAGE, ENTRYPOINT_STAGE)
    try:
        with journal:
            journal.record("entrypoint_exit_verdict", record)
            if record["verdict"] != "COMPLETE":
                journal.fail("entrypoint_exit_" + record["verdict"].lower())
                # StageJournal closes FAILED only on an exception.
                raise _StageNotComplete()
    except _StageNotComplete:
        pass
    return record


class _StageNotComplete(Exception):
    pass
