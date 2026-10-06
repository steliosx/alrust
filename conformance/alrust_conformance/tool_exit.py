"""Stage-tool exit proofs: R-EP plan P2, contract conformance/tool-exit-contract.md.

Every credential-holding stage tool runs as the child C_T of its own
supervisor generation P_T, so the whole driver process, not only its psql
children, is supervised before it generates or reads a secret. P_T writes a
launch and a terminal receipt, as the entrypoint supervisor does, plus the
tool binding {name, stage_index}. The tool's own stage journal is opened and
closed by C_T; after P_T has exited, the workflow opens the tool's exit
stage, "<tool>-exit", immediately after it, and this module appends exactly
one tool_exit_verdict there. Nothing is ever written into the tool's closed
journal, and the verdict depends only on receipts and closed journals, so
the proof is not cyclic.

The gates (P3) call holder_proofs(): the required holders are derived from
the stage names and the gate's own requirement, never from the verdict
records offered, and every verdict is recomputed from the receipts and
compared whole. This module only reads immutable proof inputs and /proc.
It never signals.
"""
import json
import os

from . import stage_journal as _sj
from .entrypoint_exit import (_StageNotComplete, _sha, verify_receipts,
                              LAUNCH_NAME, TERMINAL_NAME)

SCHEMA_LAUNCH = "alrust-tool-launch/1"
SCHEMA_TERMINAL = "alrust-tool-terminal/1"
RECEIPT_DIR = "tools"
EXIT_SUFFIX = "-exit"
# The credential-holding stage tools (contract section 2). database-drop
# connects only as the service superuser, whose password is public, so it is
# not a holder.
HOLDER_TOOLS = ("database-create", "registry")
EXPECTED_KEYS = frozenset({"driver_sha256", "interpreter_path"})
VERDICT_KEYS = frozenset({"verdict", "reason", "tool", "stage_index",
                          "launch_receipt_sha256", "terminal_receipt_sha256",
                          "stage_journal_sha256", "stage_state", "execution", "trigger"})


class HolderError(Exception):
    """A holder-proof refusal with a fixed code."""


def _require(condition, code):
    if not condition:
        raise HolderError(code)


def exit_stage(tool):
    return tool + EXIT_SUFFIX


def spec(tool, stage_index):
    return {"launch": SCHEMA_LAUNCH, "terminal": SCHEMA_TERMINAL,
            "journal_key": "stage_journal",
            "tool": {"name": tool, "stage_index": stage_index},
            "script_reason": "tool_driver_mismatch"}


def receipt_paths(job_root, tool, stage_index):
    """Fixed, per-run receipt paths: one directory per tool stage index."""
    base = os.path.join(job_root, RECEIPT_DIR, "%02d-%s" % (stage_index, tool))
    return os.path.join(base, LAUNCH_NAME), os.path.join(base, TERMINAL_NAME)


def _expected_ok(expected):
    return (type(expected) is dict and set(expected) == EXPECTED_KEYS
            and _sha(expected["driver_sha256"])
            and type(expected["interpreter_path"]) is str
            and os.path.isabs(expected["interpreter_path"]))


def verify_tool_exit(job_root, tool, stage_index, expected):
    """Return one tool_exit_verdict record for the run of TOOL whose stage
    journal has index STAGE_INDEX. EXPECTED holds the workflow head's pins
    for this tool: {"driver_sha256", "interpreter_path"}. verdict is
    COMPLETE (closure proven), FAILED (tree_not_empty, valid linkage) or
    UNPROVEN, as for the entrypoint."""
    record = {"verdict": "UNPROVEN", "reason": "expected_pins_invalid", "tool": tool,
              "stage_index": stage_index, "launch_receipt_sha256": None,
              "terminal_receipt_sha256": None, "stage_journal_sha256": None,
              "stage_state": None, "execution": "FAIL", "trigger": None}
    if tool not in HOLDER_TOOLS or type(stage_index) is not int \
            or not 0 <= stage_index < _sj.MAX_STAGES - 1:
        record["reason"] = "tool_invalid"
        return record
    if not _expected_ok(expected):
        return record
    launch_path, terminal_path = receipt_paths(job_root, tool, stage_index)
    shared = verify_receipts(job_root, spec(tool, stage_index),
                             _sj.journal_path(job_root, stage_index, tool),
                             launch_path, terminal_path, expected["driver_sha256"],
                             expected["interpreter_path"])
    record.update({key: shared[key] for key in (
        "verdict", "reason", "launch_receipt_sha256", "terminal_receipt_sha256",
        "execution", "trigger")})
    record["stage_journal_sha256"] = shared["journal_sha256"]
    record["stage_state"] = shared["journal_state"]
    return record


def run_tool_exit_stage(job_root, prefix, tool, expected):
    """Open "<tool>-exit" right after TOOL's stage (the last in PREFIX) and
    append exactly one tool_exit_verdict. The stage closes COMPLETE only for
    verdict COMPLETE; otherwise FAILED with the reason. Returns the record."""
    name = exit_stage(tool) if tool in HOLDER_TOOLS else None
    if not (name and type(prefix) is list and prefix and prefix[-1] == tool
            and prefix.count(tool) == 1 and name not in prefix):
        raise _sj.JournalError("tool_exit_prefix_invalid")
    index = len(prefix)
    record = verify_tool_exit(job_root, tool, index - 1, expected)
    journal = _sj.StageJournal(job_root, index, name, tool)
    try:
        with journal:
            journal.record("tool_exit_verdict", record)
            if record["verdict"] != "COMPLETE":
                journal.fail("tool_exit_" + record["verdict"].lower())
                raise _StageNotComplete()
    except _StageNotComplete:
        pass
    return record


_VERDICTS = ("COMPLETE", "FAILED", "UNPROVEN")
_STATES = ("COMPLETE", "FAILED", "INCOMPLETE")


def _typed_verdict(payload):
    """Exact keys and exact JSON types of an offered tool_exit_verdict
    (P2A-SOURCE-R1): bool is not an int and a float is not an int, so a
    record cannot match a recomputed one through Python's loose equality."""
    _require(type(payload) is dict and set(payload) == VERDICT_KEYS,
             "holder_verdict_malformed")
    hashes = ("launch_receipt_sha256", "terminal_receipt_sha256", "stage_journal_sha256")
    _require(type(payload["verdict"]) is str and payload["verdict"] in _VERDICTS
             and type(payload["reason"]) is str and type(payload["tool"]) is str
             and type(payload["stage_index"]) is int
             and all(payload[k] is None or _sha(payload[k]) for k in hashes)
             and (payload["stage_state"] is None
                  or (type(payload["stage_state"]) is str
                      and payload["stage_state"] in _STATES))
             and type(payload["execution"]) is str
             and payload["execution"] in ("PASS", "FAIL")
             and (payload["trigger"] is None or type(payload["trigger"]) is str),
             "holder_verdict_malformed")


def _canonical(record):
    """Type-sensitive comparison form: JSON keeps false, 0 and 0.0 distinct."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def required_holders(stage_names, required):
    """[(tool, stage_index)] for every holder tool in STAGE_NAMES, from the
    names alone. REQUIRED lists the tools the calling gate needs (for
    example both, before database-drop on the launched path); each must be
    present. Every holder present, required or not, must appear once and be
    immediately followed by its exit stage; an exit stage without its tool,
    or repeated, refuses."""
    _require(type(stage_names) is list and all(type(n) is str for n in stage_names)
             and type(required) is tuple and len(set(required)) == len(required)
             and all(t in HOLDER_TOOLS for t in required), "holder_request_invalid")
    holders = []
    for tool in HOLDER_TOOLS:
        count, exits = stage_names.count(tool), stage_names.count(exit_stage(tool))
        _require(count <= 1, "holder_stage_repeated")
        _require(count == 1 or tool not in required, "holder_stage_missing")
        _require(exits == count, "holder_exit_stage_missing_or_unexpected")
        if count:
            index = stage_names.index(tool)
            _require(index + 1 < len(stage_names)
                     and stage_names[index + 1] == exit_stage(tool),
                     "holder_exit_stage_not_adjacent")
            holders.append((tool, index))
    return holders


def holder_proofs(job_root, aggregation, records, required, expected):
    """The gates' holder check (P3 wires it into writer_quiescence, finalize
    and failed_entrypoint_quiescence). AGGREGATION and RECORDS are the gate's
    own validated aggregation and its (stage, kind, payload) records;
    EXPECTED maps each tool in HOLDER_TOOLS to its workflow-head pins.
    The aggregation must be structurally complete (VALID, or FAILED on the
    failed-entrypoint path), never INCOMPLETE. For every required holder:
    its exit stage closed COMPLETE in that aggregation; exactly one
    tool_exit_verdict, in its exit stage, exactly typed and equal as a whole
    (type-sensitive) to a verdict
    recomputed now from the receipts, and COMPLETE; and the recomputed tool
    journal digest equal to the aggregation's. No tool_exit_verdict may
    appear anywhere else. P2a takes the pins from the caller only; P2b also
    requires core /3 and EXPECTED equal to its tool_pins. Returns the
    verified records; raises HolderError."""
    _require(type(expected) is dict and set(expected) == set(HOLDER_TOOLS)
             and all(_expected_ok(expected[t]) for t in HOLDER_TOOLS),
             "holder_pins_invalid")
    _require(aggregation.get("verdict") in ("VALID", "FAILED"), "holder_aggregation_incomplete")
    stages = aggregation["stages"]
    names = [stage["name"] for stage in stages]
    _require([stage["index"] for stage in stages] == list(range(len(stages))),
             "holder_stage_index_invalid")
    holders = required_holders(names, required)
    offered = [(stage, payload) for stage, kind, payload in records
               if kind == "tool_exit_verdict"]
    _require(sorted(stage for stage, _ in offered)
             == sorted(exit_stage(tool) for tool, _ in holders), "holder_verdict_set_invalid")
    proven = []
    for tool, index in holders:
        _require(stages[index + 1]["result"] == "COMPLETE", "holder_exit_stage_not_complete")
        payload = [p for stage, p in offered if stage == exit_stage(tool)][0]
        _typed_verdict(payload)
        fresh = verify_tool_exit(job_root, tool, index, expected[tool])
        _typed_verdict(fresh)
        _require(_canonical(fresh) == _canonical(payload), "holder_verdict_mismatch")
        _require(fresh["verdict"] == "COMPLETE", "holder_exit_not_proven")
        _require(fresh["stage_journal_sha256"] == stages[index]["sha256"],
                 "holder_journal_mismatch")
        proven.append(fresh)
    return proven
