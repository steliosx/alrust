"""The S0 custody gates (W5b; W5B-R1, W5B-R2). Reads only; no removal, copy
or process. dispose-credentials.py and export-evidence.py call one of these
before they touch anything.

holder_proven(job_root, expected): create was attempted. The journals
present must be exactly [database-create, database-create-exit] or those
plus database-drop, structurally closed (VALID or FAILED); one core
snapshot bound to that aggregation, /3, with the caller's pins
(finalizer._gate_core); and the database-create holder's exit proof
recomputed now and COMPLETE (tool_exit.holder_proofs, the same proof the
drop's no_launch_quiescence uses). No launch evidence (reference/,
observer/) may exist. Returns the database directory's recorded identity
(the create stage's owned_resource for <job_root>/database), or None if
none was recorded. A closed FAILED create is admitted when its holder's
exit is proven; an unproven holder refuses.

not_launched(job_root): create was never attempted. Proves no holder could
exist: no journal file, no tool receipt directory, no database directory
and no launch evidence. The core and J may exist and are original evidence.

Fixed-code CustodyError only; never a path or value."""
import os
import re
import stat

from . import database_stage as ds
from . import finalizer as fz
from . import stage_journal as sj
from . import tool_exit as te

PREFIXES = ([ds.CREATE_STAGE, te.exit_stage(ds.CREATE_STAGE)],
            [ds.CREATE_STAGE, te.exit_stage(ds.CREATE_STAGE), ds.DROP_STAGE])
TOOL_DIR = "tools"
_JOURNAL_FILE = re.compile(r"([0-9]{2})-([a-z0-9][a-z0-9-]{0,62})\.jsonl\Z")


class CustodyError(Exception):
    """Fixed codes only."""


def _require(condition, code):
    if not condition:
        raise CustodyError(code)


def _entries(job_root, name):
    """Names in job_root/name (not followed if a link), or None if absent."""
    path = os.path.join(job_root, name)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise CustodyError("custody_unreadable") from None
    _require(stat.S_ISDIR(info.st_mode), "custody_not_directory")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise CustodyError("custody_unreadable") from None
    try:
        return sorted(os.listdir(fd))
    finally:
        os.close(fd)


def present_stages(job_root):
    """The stage names of the journal files present, in index order; every
    entry must be a journal file name with indices 0..n-1."""
    names = _entries(job_root, sj.JOURNAL_DIR) or []
    stages = []
    for index, entry in enumerate(names):
        match = _JOURNAL_FILE.fullmatch(entry)
        _require(match is not None and int(match.group(1)) == index, "custody_journals_unexpected")
        stages.append(match.group(2))
    return stages


def _no_launch_evidence(job_root):
    for name in ("reference", "observer"):
        try:
            os.lstat(os.path.join(job_root, name))
        except FileNotFoundError:
            continue
        except OSError:
            raise CustodyError("custody_unreadable") from None
        raise CustodyError("launch_evidence_present")


def holder_proven(job_root, expected):
    stages = present_stages(job_root)
    _require(stages in PREFIXES, "custody_journals_unexpected")
    try:
        aggregation = sj.aggregate(job_root, stages)
    except (sj.JournalError, OSError):
        raise CustodyError("custody_journal_invalid") from None
    _require(aggregation.get("verdict") in ("VALID", "FAILED"), "custody_prefix_not_closed")
    try:
        core_bytes, _core = fz._gate_core(job_root, aggregation, expected)
        records = fz._journal_records(job_root, aggregation)
    except (fz.FinalizerError, sj.JournalError, OSError):
        raise CustodyError("custody_core_refused") from None
    _require(type(expected) is dict and "tools" in expected, "custody_pins_invalid")
    try:
        te.holder_proofs(job_root, aggregation, records, (ds.CREATE_STAGE,), expected["tools"],
                         core_bytes)
    except te.HolderError:
        raise CustodyError("holder_unproven") from None
    _no_launch_evidence(job_root)
    path = os.path.join(job_root, ds.DATABASE_DIR)
    owned = [payload for stage, kind, payload in records
             if kind == "owned_resource" and stage == ds.CREATE_STAGE
             and type(payload) is dict and payload.get("path") == path]
    if not owned:
        return None
    _require(len(owned) == 1 and owned[0].get("type") == "dir"
             and type(owned[0].get("dev")) is int and type(owned[0].get("ino")) is int,
             "database_dir_record_invalid")
    return {"dev": owned[0]["dev"], "ino": owned[0]["ino"]}


def not_launched(job_root):
    _require(present_stages(job_root) == [], "custody_journal_present")
    _require(_entries(job_root, TOOL_DIR) is None, "custody_receipts_present")
    _require(_entries(job_root, ds.DATABASE_DIR) is None, "custody_database_present")
    _no_launch_evidence(job_root)
