"""Shared synthetic exit-proof fixtures for the gate controls (R-EP plan P3a).

Pure: private files only. Supervisor generations are modelled with PIDs that
do not exist, so the verifiers' generation_ended() sees them as ended; no
process is created or signalled. Each *_verdict() writes the receipts a
supervisor would have written for an already closed stage journal and
returns the verdict recomputed from them, ready to be recorded in the exit
stage (as run_*_exit_stage would)."""
import hashlib
import json
import os
from pathlib import Path

from alrust_conformance import entrypoint_exit as ee
from alrust_conformance import stage_journal as sj
from alrust_conformance import tool_exit as te

SCRIPT_SHA = "d" * 64
DRIVER_SHA = "c" * 64
PYTHON = "/usr/bin/python3"
ENDED = {"entrypoint": 4194297, "database-create": 4194301, "registry": 4194299}
EXPECTED = {"entrypoint": {"entrypoint_script_sha256": SCRIPT_SHA, "interpreter_path": PYTHON},
            "tools": {tool: {"driver_sha256": DRIVER_SHA, "interpreter_path": PYTHON}
                      for tool in te.HOLDER_TOOLS}}


def core_pins():
    """Keyword arguments for write_core_identity that make a /3 core whose
    pins equal EXPECTED (measured values equal to the pins)."""
    return dict(
        entrypoint_pins={"supervisor": {"path": "/job/sup.py", "sha256": "a" * 64},
                         "entrypoint_script": {"path": "/job/entry.py", "sha256": SCRIPT_SHA},
                         "interpreter": {"path": PYTHON, "resolved_path": "/usr/bin/python3.12"}},
        entrypoint_measured={"supervisor": "a" * 64, "entrypoint_script": SCRIPT_SHA,
                             "interpreter": "e" * 64},
        tool_pins={tool: {"driver": {"path": "/job/stage-tool.py", "sha256": DRIVER_SHA}}
                   for tool in te.HOLDER_TOOLS},
        tool_measured={tool: DRIVER_SHA for tool in te.HOLDER_TOOLS})


def _dump(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(obj, sort_keys=True) + "\n").encode()
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _ended(pid):
    assert not os.path.exists("/proc/%d" % pid), "synthetic ended PID is in use on this host"
    return pid


def _common(root, pid, ticks, journal, terminal_path, script_sha, binding=None):
    core_bytes = (Path(root) / sj.CORE_NAME).read_bytes()
    core = json.loads(core_bytes)
    launch = {
        "supervisor": {"pid": pid, "pgid": pid, "start_ticks": ticks, "uid": os.getuid(),
                       "subreaper": 1},
        "child": {"pid": pid + 1, "start_ticks": ticks + 1},
        "boot_id": core["boot_id"],
        "core_identity_sha256": hashlib.sha256(core_bytes).hexdigest(),
        "command": {"argv_sha256": "e" * 64,
                    "executable": {"path": PYTHON, "dev": 1, "ino": 2},
                    "script": {"path": "/job/script.py", "dev": 1, "ino": 3,
                               "sha256": script_sha}},
        "terminal_receipt": terminal_path,
        "deadline": {"monotonic_ns": 10**12, "teardown_budget_ns": ee.TEARDOWN_BUDGET_NS,
                     "boot_id": core["boot_id"]},
        "launched_utc_ns": 1, "monotonic_ns": 1}
    terminal = {
        "result": "tree_empty", "supervisor": {"pid": pid, "start_ticks": ticks},
        "boot_id": core["boot_id"], "trigger": "child_exited", "child_wait_status": 0,
        "child_signalled": False, "other_descendant_signals": 0,
        "other_descendants_reaped": 0, "signals_sent": 0, "children_reaped": 1,
        "teardown_ns": 1, "finished_utc_ns": 2, "finished_monotonic_ns": 2}
    if binding is not None:
        launch["tool"] = dict(binding)
        terminal["tool"] = dict(binding)
    return launch, terminal, ee._journal_now(journal)


def entrypoint_verdict(root, index, *, execution=None, other_signals=0, change=None):
    """Receipts for the closed entrypoint journal at INDEX, then the verdict.
    EXECUTION defaults to what the journal implies; CHANGE(launch, terminal)
    may alter the receipts before they are written."""
    root = str(root)
    pid = _ended(ENDED["entrypoint"])
    journal = sj.journal_path(root, index, ee.ENTRYPOINT_STAGE)
    launch_path, terminal_path = ee.receipt_paths(root)
    launch, terminal, observed = _common(root, pid, 111, journal, terminal_path, SCRIPT_SHA)
    launch.update(schema=ee.SCHEMA_LAUNCH, entrypoint_journal=journal)
    terminal.update(schema=ee.SCHEMA_TERMINAL, entrypoint_journal=observed,
                    other_descendant_signals=other_signals)
    terminal["execution"] = execution or ("PASS" if observed["state"] == "COMPLETE" else "FAIL")
    if change:
        change(launch, terminal)
    terminal["launch_receipt_sha256"] = _dump(launch_path, launch)
    _dump(terminal_path, terminal)
    return ee.verify_entrypoint_exit(root, index, EXPECTED["entrypoint"])


def tool_verdict(root, tool, index):
    """Receipts for the closed tool journal at INDEX, then the verdict."""
    root = str(root)
    pid = _ended(ENDED[tool])
    journal = sj.journal_path(root, index, tool)
    launch_path, terminal_path = te.receipt_paths(root, tool, index)
    binding = {"name": tool, "stage_index": index}
    launch, terminal, observed = _common(root, pid, 222, journal, terminal_path, DRIVER_SHA,
                                         binding)
    launch.update(schema=te.SCHEMA_LAUNCH, stage_journal=journal)
    terminal.update(schema=te.SCHEMA_TERMINAL, stage_journal=observed)
    terminal["execution"] = "PASS" if observed["state"] == "COMPLETE" else "FAIL"
    terminal["launch_receipt_sha256"] = _dump(launch_path, launch)
    _dump(terminal_path, terminal)
    return te.verify_tool_exit(root, tool, index, EXPECTED["tools"][tool])
