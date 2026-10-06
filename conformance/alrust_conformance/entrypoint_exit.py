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
"""
import hashlib
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


def _generation(value):
    _need(type(value) is dict and _int(value.get("pid"), 2, _PID_LIMIT)
          and _int(value.get("start_ticks"), 1, (1 << 63) - 1), "receipt_generation_invalid")
    return value["pid"], value["start_ticks"]


def receipt_paths(job_root):
    base = os.path.join(job_root, RECEIPT_DIR)
    return os.path.join(base, LAUNCH_NAME), os.path.join(base, TERMINAL_NAME)


def verify_entrypoint_exit(job_root, journal_index, expected):
    """Return one entrypoint_exit_verdict record (design section 4).

    EXPECTED holds the workflow head's pins: {"entrypoint_script_sha256",
    "interpreter_path"}. verdict is COMPLETE (closure proven), FAILED
    (terminal tree_not_empty with valid linkage) or UNPROVEN. execution is
    the terminal's value only when verdict is COMPLETE, otherwise FAIL."""
    record = {"verdict": "UNPROVEN", "reason": "not_checked",
              "launch_receipt_sha256": None, "terminal_receipt_sha256": None,
              "entrypoint_journal_sha256": None, "entrypoint_state": None,
              "execution": "FAIL", "trigger": None}
    try:
        _need(type(expected) is dict and set(expected) == {
            "entrypoint_script_sha256", "interpreter_path"}
            and _sha(expected["entrypoint_script_sha256"])
            and type(expected["interpreter_path"]) is str
            and os.path.isabs(expected["interpreter_path"]), "expected_pins_invalid")
        core_bytes = _sj.read_bounded(os.path.join(job_root, _sj.CORE_NAME), _sj.MAX_CORE)
        core = _sj.validate_core(core_bytes, job_root)
        core_sha256 = hashlib.sha256(core_bytes).hexdigest()
        journal = _sj.journal_path(job_root, journal_index, ENTRYPOINT_STAGE)
        launch_path, terminal_path = receipt_paths(job_root)
        try:
            launch_bytes = _sj.read_bounded(launch_path, MAX_RECEIPT)
        except (OSError, _sj.JournalError):
            raise _Refusal("UNPROVEN", "launch_receipt_unavailable") from None
        record["launch_receipt_sha256"] = hashlib.sha256(launch_bytes).hexdigest()
        launch = _sj.strict_json(launch_bytes, "launch_receipt_malformed")
        _need(type(launch) is dict and launch.get("schema") == SCHEMA_LAUNCH,
              "launch_receipt_malformed")
        launched = _generation(launch.get("supervisor"))
        sup = launch["supervisor"]
        # Independent expectations: the core file, the job root, the pins.
        _need(launch.get("boot_id") == core["boot_id"], "boot_mismatch")
        _need(launch.get("core_identity_sha256") == core_sha256, "core_mismatch")
        _need(sup.get("pgid") == sup["pid"] and sup.get("uid") == os.getuid()
              and sup.get("subreaper") == 1, "supervisor_identity_invalid")
        command = launch.get("command")
        _need(type(command) is dict and type(command.get("script")) is dict
              and command["script"].get("sha256") == expected["entrypoint_script_sha256"],
              "entrypoint_script_mismatch")
        _need(type(command.get("executable")) is dict
              and command["executable"].get("path") == expected["interpreter_path"],
              "interpreter_mismatch")
        _need(launch.get("entrypoint_journal") == journal
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
        _need(type(terminal) is dict and terminal.get("schema") == SCHEMA_TERMINAL,
              "terminal_receipt_malformed")
        _need(type(terminal.get("supervisor")) is dict
              and set(terminal["supervisor"]) == {"pid", "start_ticks"}
              and _generation(terminal["supervisor"]) == launched
              and terminal.get("boot_id") == core["boot_id"]
              and terminal.get("launch_receipt_sha256") == record["launch_receipt_sha256"],
              "terminal_link_mismatch")
        record["trigger"] = terminal.get("trigger")
        if terminal.get("result") == "tree_not_empty":
            raise _Refusal("FAILED", "tree_not_empty")
        _need(terminal.get("result") == "tree_empty", "terminal_result_invalid")
        observed = terminal.get("entrypoint_journal")
        _need(type(observed) is dict and set(observed) == {"sha256", "state", "code"}
              and observed["state"] in ("COMPLETE", "FAILED", "INCOMPLETE"),
              "journal_observation_invalid")
        record["entrypoint_state"] = observed["state"]
        if observed["sha256"] is not None:
            _need(_sha(observed["sha256"]), "journal_observation_invalid")
            try:
                current = _sj.read_bounded(journal, _sj.MAX_JOURNAL)
            except (OSError, _sj.JournalError):
                raise _Refusal("UNPROVEN", "journal_changed") from None
            _need(hashlib.sha256(current).hexdigest() == observed["sha256"], "journal_changed")
            record["entrypoint_journal_sha256"] = observed["sha256"]
        else:
            _need(observed["state"] == "INCOMPLETE" and not os.path.lexists(journal),
                  "journal_changed")
        _need(terminal.get("execution") in ("PASS", "FAIL"), "terminal_execution_invalid")
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
