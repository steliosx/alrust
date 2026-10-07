"""Finalizer building blocks: credential registry, final-byte scanner and
guarded removal of owned resources.

Contract: docs/ci-launch-attestation-contract.md, sections 8 and 9. This
head holds library pieces only: no orchestration, no workflow wiring, and
nothing that runs on import. The orchestrator (quiescence gate, staging,
terminal receipt, manifest and seal) is a separate head built on these.

- The registry is the private list of every actual credential of the job.
  It is verified against the digest that its stage journal recorded, and
  every secret must be at least MIN_SECRET bytes, so that a weak value such
  as a default database password cannot turn the scan into noise. Python
  cannot guarantee that memory is erased; the registry is never written
  anywhere after loading.
- The scanner looks for each secret in its raw form and its JSON-escaped
  form, plus a fixed list of generic credential patterns. It reports the
  file and the kind of match, never the matched bytes.
- Guarded removal deletes an owned resource only when its path lies inside
  the job root and its device and inode equal those recorded when it was
  created; a directory tree is removed with the symlink-safe rmtree. Any
  mismatch leaves the resource untouched and UNPROVEN.
"""
import hashlib
import json
import os
import re
import shutil
import stat

from .stage_journal import JournalError, read_bounded, strict_json

# The removal below relies on rmtree not following symlinks inside a tree.
assert shutil.rmtree.avoids_symlink_attacks, "symlink-safe rmtree required"

SCHEMA_REGISTRY = "alrust-credential-registry/1"
REGISTRY_NAME = "credential-registry.json"
MAX_REGISTRY = 65536
MAX_SECRETS = 256
MIN_SECRET = 16
MAX_SECRET = 4096
MAX_SCANNED_FILE = 16 << 20

# Generic credential shapes, matched on raw bytes. Indexes are reported,
# never the matched text.
GENERIC_PATTERNS = (
    re.compile(rb"SYNTHETIC-[0-9a-f]{48}"),
    re.compile(rb"postgres(?:ql)?://[^\s\"']+:[^\s\"'@]+@"),
    re.compile(rb"Authorization:\s*\S", re.IGNORECASE),
    re.compile(rb"Bearer\s+\S"),
    re.compile(rb"-----BEGIN"),
)


class FinalizerError(ValueError):
    """Fixed codes only."""


def _require(condition, code):
    if not condition:
        raise FinalizerError(code)


def load_registry(job_root, expected_sha256):
    """Return the job's secrets as bytes, from job_root/credential-registry.json.

    The file must be a bounded regular file whose sha256 equals the digest
    its stage journal recorded. It must hold exactly
    {"schema": SCHEMA_REGISTRY, "secrets": [str, ...]}, with each secret a
    unique string of MIN_SECRET to MAX_SECRET UTF-8 bytes.
    """
    _require(type(expected_sha256) is str and len(expected_sha256) == 64
             and not expected_sha256.strip("0123456789abcdef"), "registry_digest_invalid")
    try:
        data = read_bounded(os.path.join(job_root, REGISTRY_NAME), MAX_REGISTRY)
    except JournalError:
        raise FinalizerError("registry_unreadable") from None
    except OSError:
        raise FinalizerError("registry_unreadable") from None
    _require(hashlib.sha256(data).hexdigest() == expected_sha256, "registry_digest_mismatch")
    try:
        record = strict_json(data, "registry_invalid")
    except JournalError:
        raise FinalizerError("registry_invalid") from None
    _require(type(record) is dict and set(record) == {"schema", "secrets"}
             and record["schema"] == SCHEMA_REGISTRY
             and type(record["secrets"]) is list
             and 1 <= len(record["secrets"]) <= MAX_SECRETS, "registry_invalid")
    secrets = []
    for value in record["secrets"]:
        _require(type(value) is str, "registry_invalid")
        try:
            encoded = value.encode("utf-8")
        except UnicodeError:
            # For example an escaped unpaired surrogate: valid JSON text,
            # but not a UTF-8 string. Refused without echoing it.
            raise FinalizerError("registry_invalid") from None
        _require(MIN_SECRET <= len(encoded) <= MAX_SECRET, "registry_secret_too_short"
                 if len(encoded) < MIN_SECRET else "registry_invalid")
        secrets.append(encoded)
    _require(len(set(secrets)) == len(secrets), "registry_invalid")
    return secrets


def secret_forms(secret):
    """The byte forms searched for one secret.

    Supported forms: the raw UTF-8 bytes; the JSON string form with ASCII
    escaping (json.dumps default, \\uXXXX for non-ASCII); and the JSON
    string form without ASCII escaping (ensure_ascii=False, UTF-8). Other
    encodings (for example base64 or URL encoding) are not searched; the
    generic patterns and the export allowlist must not rely on them.
    """
    text = secret.decode("utf-8")
    return {secret,
            json.dumps(text)[1:-1].encode("utf-8"),
            json.dumps(text, ensure_ascii=False)[1:-1].encode("utf-8")}


def scan_bytes(data, secrets):
    """Findings in DATA as fixed tuples: ("secret", index) for the registry
    entry at index, or ("pattern", index) for GENERIC_PATTERNS[index]."""
    findings = []
    for index, secret in enumerate(secrets):
        if any(form in data for form in secret_forms(secret)):
            findings.append(("secret", index))
    for index, pattern in enumerate(GENERIC_PATTERNS):
        if pattern.search(data):
            findings.append(("pattern", index))
    return findings


def scan_file(path, secrets):
    """Scan one bounded regular file's final bytes. An unreadable file is a
    finding too (("unreadable", 0)), never a clean result."""
    try:
        data = read_bounded(path, MAX_SCANNED_FILE)
    except (JournalError, OSError):
        return [("unreadable", 0)]
    return scan_bytes(data, secrets)


def _inside(job_root, path):
    return (type(path) is str and os.path.isabs(path)
            and os.path.normpath(path) == path
            and path.startswith(job_root + os.sep))


def _open_dir(name, dir_fd=None):
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                   dir_fd=dir_fd)


def remove_owned(job_root, resource, root_identity):
    """Remove one owned resource recorded as {"path", "dev", "ino", "type"}.

    Every step is anchored to directory descriptors (F1465-R3):
    - the job root is opened without following symlinks, and must have
      root_identity (its dev and ino from the core identity), the caller's
      uid and mode 0700;
    - each parent component is opened relative to the previous descriptor
      with O_NOFOLLOW, so a symlinked component is refused;
    - the final entry is inspected, removed (unlink, or the symlink-safe
      rmtree with dir_fd) and verified absent relative to the held parent
      descriptor. Replacing an ancestor with a symlink after it was opened
      cannot redirect any of these operations out of the verified hierarchy.
    Limitation, stated rather than claimed away: another process of the same
    uid can still substitute the final entry inside the verified parent
    between its inspection and its removal. That stays inside the job's own
    hierarchy; it cannot reach outside it.

    Returns {"path", "type", "outcome", "reason"}: REMOVED, ABSENT (already
    gone; not a proven removal) or UNPROVEN, with a fixed reason.
    """
    result = {"path": None, "type": None, "outcome": "UNPROVEN"}
    if not (type(resource) is dict and set(resource) == {"path", "dev", "ino", "type"}
            and resource["type"] in ("file", "dir")
            and type(resource["dev"]) is int and type(resource["ino"]) is int):
        return dict(result, reason="resource_record_invalid")
    path, kind = resource["path"], resource["type"]
    result.update(path=path if type(path) is str else None, type=kind)
    if not (type(root_identity) is tuple and len(root_identity) == 2
            and all(type(v) is int for v in root_identity)):
        return dict(result, reason="root_identity_invalid")
    if not _inside(job_root, path):
        return dict(result, reason="outside_job_root")
    parts = path[len(job_root) + 1:].split(os.sep)
    if any(part in ("", ".", "..") for part in parts):
        return dict(result, reason="outside_job_root")
    opened = []
    try:
        try:
            fd = _open_dir(job_root)
        except OSError:
            return dict(result, reason="root_unavailable")
        opened.append(fd)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != root_identity or info.st_uid != os.getuid() \
                or stat.S_IMODE(info.st_mode) != 0o700:
            return dict(result, reason="root_identity_mismatch")
        for part in parts[:-1]:
            try:
                fd = _open_dir(part, dir_fd=fd)
            except OSError:
                # Missing, not a directory, or a symlinked component.
                return dict(result, reason="ancestor_unavailable")
            opened.append(fd)
        name = parts[-1]
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return dict(result, outcome="ABSENT", reason="already_absent")
        except OSError:
            return dict(result, reason="inspection_failed")
        expected_type = stat.S_ISDIR if kind == "dir" else stat.S_ISREG
        if not expected_type(info.st_mode):
            return dict(result, reason="type_mismatch")
        if (info.st_dev, info.st_ino) != (resource["dev"], resource["ino"]):
            return dict(result, reason="identity_mismatch")
        if info.st_uid != os.getuid():
            return dict(result, reason="owner_mismatch")
        try:
            if kind == "dir":
                shutil.rmtree(name, dir_fd=fd)
            else:
                os.unlink(name, dir_fd=fd)
        except OSError:
            return dict(result, reason="removal_failed")
        try:
            os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return dict(result, outcome="REMOVED", reason="removed_and_verified_absent")
        except OSError:
            return dict(result, reason="verification_failed")
        return dict(result, reason="still_present")
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


# ---------------------------------------------------------------------------
# Orchestration (contract section 9). The order is acyclic: every proof is
# written after the action it attests, and the scan reads final bytes only:
#
#   quiescence gate (validated journals only)
#   -> export directory created exclusively (0700)
#   -> finalizer journal opened in it
#   -> registry loaded and verified against its journal record
#   -> allowlisted candidates copied byte for byte (identity-checked)
#   -> owned resources removed and verified (descriptor-anchored)
#   -> terminal cleanup receipt written
#   -> finalizer journal's final record, then closed
#   -> every export file's final bytes scanned
#   -> manifest built in memory, scanned, written only if clean
#   -> files 0400, directory 0500 (custody convention, not a boundary)
#
# The receipt does not hash the finalizer journal; the journal does not hash
# itself; the manifest hashes both.

from . import stage_journal as _sj
from .security_evidence import PrivateJournal

EXPORT_DIR = "export"
SCHEMA_TERMINAL_RECEIPT = "alrust-terminal-cleanup/1"
SCHEMA_MANIFEST = "alrust-export-manifest/1"
SCHEMA_FAILURE = "alrust-failure-record/1"
SCHEMA_LAUNCH = "alrust-reference-launch/1"
SCHEMA_STOP_TERMINAL = "alrust-reference-terminal/1"
# Reviewed export names and their byte bounds. A record's own max_bytes
# never widens these; a new name needs its own reviewed change.
EXPORT_ALLOWLIST = {"conformance-report.json": 1 << 20}
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _sha(value):
    return type(value) is str and _SHA.fullmatch(value) is not None


def _journal_records(job_root, aggregation):
    """(stage, kind, payload) of every record, from journal bytes that hash
    to the values the aggregation validated."""
    records = []
    for stage in aggregation["stages"]:
        data = read_bounded(_sj.journal_path(job_root, stage["index"], stage["name"]),
                            _sj.MAX_JOURNAL)
        _require(hashlib.sha256(data).hexdigest() == stage["sha256"], "journal_changed")
        for line in data[:-1].split(b"\n"):
            evidence = _sj.strict_json(line, "journal_changed")["evidence"]
            records.append((stage["name"], evidence["kind"], evidence["payload"]))
    return records


def _one(records, kind):
    found = [(stage, payload) for stage, k, payload in records if k == kind]
    _require(len(found) == 1, "%s_missing_or_repeated" % kind)
    return found[0]


def _read_identified(path, dev, ino, limit):
    """A regular file's bytes, only if the opened descriptor has dev/ino."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == (dev, ino)
                 and info.st_size <= limit, "candidate_identity_mismatch")
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(fd, limit + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    _require(size <= limit, "candidate_identity_mismatch")
    return b"".join(chunks)


_PID_LIMIT = 4194304


def _generation(value):
    """(pid, start_ticks) from a receipt's supervisor dict, exact ints only."""
    _require(type(value) is dict and type(value.get("pid")) is int
             and type(value.get("start_ticks")) is int
             and 1 < value["pid"] <= _PID_LIMIT and 0 < value["start_ticks"] < (1 << 63),
             "stop_receipt_mismatch")
    return value["pid"], value["start_ticks"]


def generation_ended(pid, start_ticks, boot):
    """Positively observe that the process generation (pid, start_ticks) on
    BOOT has ended (section 9.1): the PID is absent, held by another
    start_ticks, or the generation is a zombie (its pidfd reports exit).
    A live generation, a different boot or any failed inspection is False.
    No signal is ever sent."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            if f.read(64).strip() != boot:
                return False
    except OSError:
        return False
    try:
        fd = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        return True
    except (OSError, AttributeError, OverflowError):
        return False
    try:
        try:
            with open("/proc/%d/stat" % pid, "rb") as f:
                fields = f.read(4096).rsplit(b")", 1)[1].split()
        except FileNotFoundError:
            return True
        if int(fields[19]) != start_ticks:
            return True
        import select
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        return bool(poller.poll(0))
    except (OSError, ValueError, IndexError):
        return False
    finally:
        os.close(fd)


def _stop_evidence(job_root, records, core):
    """The stop half of positive quiescence (section 9 step 1), from journal
    records only. The stop_verdict record must be COMPLETE/tree_empty and
    link the launch and terminal receipts by sha256; both are re-read and
    re-hashed, the terminal must be tree_empty for the launch receipt's
    supervisor generation, both on the core's boot, and that generation must
    be positively observed to have ended. Returns (stop record, generation)."""
    _, stop = _one(records, "stop_verdict")
    _require(type(stop) is dict and set(stop) == {
        "verdict", "reason", "launch_receipt_sha256", "terminal_receipt_sha256"}
        and stop["verdict"] == "COMPLETE" and stop["reason"] == "tree_empty"
        and _sha(stop["launch_receipt_sha256"]) and _sha(stop["terminal_receipt_sha256"]),
        "stop_not_proven")
    launch_bytes = read_bounded(os.path.join(job_root, "reference", "launch.json"), MAX_REGISTRY)
    terminal_bytes = read_bounded(os.path.join(job_root, "reference", "terminal.json"),
                                  MAX_REGISTRY)
    _require(hashlib.sha256(launch_bytes).hexdigest() == stop["launch_receipt_sha256"]
             and hashlib.sha256(terminal_bytes).hexdigest()
             == stop["terminal_receipt_sha256"], "stop_receipt_mismatch")
    launch = _sj.strict_json(launch_bytes, "stop_receipt_mismatch")
    terminal = _sj.strict_json(terminal_bytes, "stop_receipt_mismatch")
    _require(type(launch) is dict and type(terminal) is dict, "stop_receipt_mismatch")
    launched = _generation(launch.get("supervisor"))
    _require(type(terminal.get("supervisor")) is dict
             and set(terminal["supervisor"]) == {"pid", "start_ticks"}
             and _generation(terminal["supervisor"]) == launched
             and launch.get("schema") == SCHEMA_LAUNCH
             and terminal.get("schema") == SCHEMA_STOP_TERMINAL
             and terminal.get("result") == "tree_empty"
             and type(launch.get("boot_id")) is str
             and launch["boot_id"] == terminal.get("boot_id") == core["boot_id"],
             "stop_receipt_mismatch")
    # The labels and hashes above are expected bindings; this is the
    # positive observation that the supervisor generation actually ended.
    _require(generation_ended(launched[0], launched[1], core["boot_id"]),
             "supervisor_generation_live_or_unproven")
    return stop, launched


def _launch_handoff(records, aggregation, stop, launched):
    """The launch counts only with a COMPLETE launch-confirm stage (LB4E-R2):
    exactly one launch_handoff, recorded in launch-confirm, naming the
    validated launch journal's sha256, the stop verdict's launch receipt and
    the same supervisor generation."""
    found = [(stage, payload) for stage, kind, payload in records if kind == "launch_handoff"]
    _require(len(found) == 1 and found[0][0] == "launch-confirm", "launch_handoff_missing")
    handoff = found[0][1]
    hashes = {s["name"]: s["sha256"] for s in aggregation["stages"]}
    _require(type(handoff) is dict and set(handoff) == {
        "launch_journal_sha256", "receipt_sha256", "supervisor", "child",
        "observed_monotonic_ns"}
        and handoff["launch_journal_sha256"] == hashes.get("launch")
        and handoff["receipt_sha256"] == stop["launch_receipt_sha256"]
        and type(handoff["supervisor"]) is dict
        and set(handoff["supervisor"]) == {"pid", "start_ticks"}
        and all(type(v) is int for v in handoff["supervisor"].values())
        and (handoff["supervisor"]["pid"], handoff["supervisor"]["start_ticks"]) == launched
        and type(handoff["child"]) is dict and set(handoff["child"]) == {"pid", "start_ticks"},
        "launch_handoff_mismatch")


def writer_quiescence(job_root, stages, entrypoint_stage="entrypoint"):
    """The writer gate for the registry and database-drop stages (database
    design section 4, DBC023-R1). STAGES is the prefix written so far, ending
    with the stage just before the caller's (at least through `stop`).
    The prefix must contain database-create < launch, launch-confirm
    immediately after launch, then the entrypoint stage, then stop
    (DBFE-R1). Requires: aggregation over the prefix VALID (every journal
    closed and COMPLETE, including database-create, launch, launch-confirm and
    entrypoint); exactly one linked COMPLETE/tree_empty stop verdict whose
    supervisor generation is positively observed to have ended; and the
    launch-confirm hand-off. It reads only immutable proof inputs (core,
    closed journals, launch and terminal receipts) and never the registry,
    a credential, a log or the database (DBC023-N3). It does not require
    database_drop. Raises FinalizerError or JournalError on refusal."""
    _require(type(stages) is list and all(
        name in stages for name in ("database-create", "launch", "launch-confirm",
                                    entrypoint_stage, "stop"))
        and stages.index("launch-confirm") == stages.index("launch") + 1
        and stages.index("database-create") < stages.index("launch")
        < stages.index(entrypoint_stage) < stages.index("stop"),
        "writer_gate_stages_invalid")
    aggregation = _sj.aggregate(job_root, stages)
    _require(aggregation["verdict"] == "VALID", "writer_gate_" + aggregation["verdict"].lower())
    core_bytes = read_bounded(os.path.join(job_root, _sj.CORE_NAME), _sj.MAX_CORE)
    core = _sj.validate_core(core_bytes, job_root)
    records = _journal_records(job_root, aggregation)
    stop, launched = _stop_evidence(job_root, records, core)
    _launch_handoff(records, aggregation, stop, launched)
    return {"aggregation": aggregation, "stop": stop}


_DATABASE_IDENTITY = ("oid", "role", "role_oid", "system_identifier", "postmaster_start_time")


_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}\Z")
_ENDPOINT_HOSTS = ("0.0.0.0", "127.0.0.1")


def _database_container(records):
    """Exactly one database_container record, in database-create, of the
    exact shape container_identity writes, with a 5432/tcp mapping whose
    host port is its database_port on an address reachable from 127.0.0.1
    (PR #100 review, comments 4174199627 and 4174199632)."""
    found = [(stage, payload) for stage, kind, payload in records
             if kind == "database_container"]
    _require(len(found) == 1 and found[0][0] == "database-create",
             "database_container_missing_or_repeated")
    c = found[0][1]
    _require(type(c) is dict and set(c) == {
        "container_id", "image_id", "repo_digests", "port", "database_port"}
        and type(c["container_id"]) is str and _CONTAINER_ID.fullmatch(c["container_id"])
        and type(c["image_id"]) is str
        and re.fullmatch(r"sha256:[0-9a-f]{64}", c["image_id"]) is not None
        and type(c["repo_digests"]) is list and len(c["repo_digests"]) <= 16
        and all(type(d) is str and len(d) <= 256 for d in c["repo_digests"])
        and c["repo_digests"] == sorted(c["repo_digests"])
        and type(c["port"]) is list and 1 <= len(c["port"]) <= 16
        and all(type(m) is dict and set(m) == {"container", "host_ip", "host_port"}
                and m["container"] == "5432/tcp" and type(m["host_ip"]) is str
                and len(m["host_ip"]) <= 45 and type(m["host_port"]) is str
                and re.fullmatch(r"[0-9]{1,5}", m["host_port"]) is not None
                for m in c["port"])
        and type(c["database_port"]) is int and 0 < c["database_port"] < 65536,
        "database_container_invalid")
    _require(any(m["host_port"] == str(c["database_port"]) and m["host_ip"] in _ENDPOINT_HOSTS
                 for m in c["port"]), "database_endpoint_unbound")
    return c


def _database_linkage(records):
    """Closes the deferred database linkage (database design section 7):
    exactly one database_created record, in database-create; exactly one
    database_container record there that binds the database port
    (_database_container); and exactly one database_drop record, in
    database-drop, whose verdict is COMPLETE and whose identity equals the
    created one. ROLE_ONLY_COMPLETE, NONE_ALLOCATED or UNPROVEN refuse."""
    created = [(stage, payload) for stage, kind, payload in records
               if kind == "database_created"]
    _require(len(created) == 1 and created[0][0] == "database-create",
             "database_created_missing_or_repeated")
    _database_container(records)
    drop_stage, drop = _one(records, "database_drop")
    # PR #100 review, comment 4174199634: the drop verdict counts only from
    # the database-drop stage itself.
    _require(drop_stage == "database-drop", "database_drop_misplaced")
    created = created[0][1]
    _require(type(created) is dict and type(drop) is dict and set(drop) == {
        "verdict", "reason", "database", "oid", "role", "role_oid",
        "system_identifier", "postmaster_start_time"}
        and drop["verdict"] == "COMPLETE", "database_drop_not_proven")
    _require(drop["database"] == created.get("name")
             and all(drop[key] == created.get(key) for key in _DATABASE_IDENTITY)
             and type(drop["oid"]) is int and type(drop["role_oid"]) is int,
             "database_linkage_mismatch")
    return drop


def finalize(job_root, stages, entrypoint_stage="entrypoint"):
    """Run the finalizer for a closed job. Returns a report dict:
    {"outcome": "SEALED"|"FAILED"|"NOT_FINALIZED", "cleanup": ..., "reason"}.

    NOT_FINALIZED means the quiescence gate did not pass: nothing mutable
    was read, removed or exported, and the caller must take the failure-only
    path (failure_record). FAILED means the gate passed but a later step
    did not; no manifest exists. SEALED means the manifest was written after
    a clean scan; "cleanup" says whether every removal was proven."""
    if type(stages) is not list or entrypoint_stage not in stages:
        return {"outcome": "NOT_FINALIZED", "cleanup": "UNPROVEN",
                "reason": "entrypoint_stage_missing"}
    aggregation = _sj.aggregate(job_root, stages)
    entry = [s for s in aggregation["stages"] if s["name"] == entrypoint_stage]
    if entry and entry[0]["result"] != "COMPLETE":
        # Section 9.1: the entrypoint must have exited with its journal
        # closed COMPLETE; named explicitly rather than inferred.
        return {"outcome": "NOT_FINALIZED", "cleanup": "UNPROVEN",
                "reason": "entrypoint_not_complete"}
    if aggregation["verdict"] != "VALID":
        return {"outcome": "NOT_FINALIZED", "cleanup": "UNPROVEN",
                "reason": "aggregation_" + aggregation["verdict"].lower()}
    try:
        core_bytes = read_bounded(os.path.join(job_root, _sj.CORE_NAME), _sj.MAX_CORE)
        core = _sj.validate_core(core_bytes, job_root)
        records = _journal_records(job_root, aggregation)
        stop, launched = _stop_evidence(job_root, records, core)
        _launch_handoff(records, aggregation, stop, launched)
        evidence = {"stop": stop, "database": _database_linkage(records)}
        _, registry = _one(records, "credential_registry")
        _require(type(registry) is dict and set(registry) == {"path", "dev", "ino", "sha256"}
                 and registry["path"] == os.path.join(job_root, REGISTRY_NAME)
                 and type(registry["dev"]) is int and type(registry["ino"]) is int
                 and _sha(registry["sha256"]), "registry_record_invalid")
        found = [(stage, payload) for stage, kind, payload in records
                 if kind == "obtained_secret_count"]
        _require(len(found) == 1, "obtained_secret_count_missing_or_repeated")
        # Q1 (b): only the registry stage, after the registry digest exists,
        # may author the count; never the entrypoint or a backfill.
        _require(found[0][0] == "registry", "obtained_secret_count_not_from_registry")
        obtained = found[0][1]
        _require(type(obtained) is dict and set(obtained) == {"count", "registry_sha256"}
                 and type(obtained["count"]) is int and obtained["count"] >= 0
                 and obtained["registry_sha256"] == registry["sha256"],
                 "obtained_secrets_unlinked")
        # The registry and every candidate record are verified BEFORE the
        # first write, so a failure here leaves nothing behind.
        info = os.lstat(registry["path"])
        _require((info.st_dev, info.st_ino) == (registry["dev"], registry["ino"]),
                 "registry_identity_mismatch")
        secrets = load_registry(job_root, registry["sha256"])
        _require(len(secrets) >= obtained["count"], "registry_incomplete")
        candidates = []
        for stage, kind, payload in records:
            if kind != "export_candidate":
                continue
            _require(type(payload) is dict and set(payload) == {"path", "dev", "ino", "name"}
                     and type(payload["name"]) is str
                     and payload["name"] in EXPORT_ALLOWLIST
                     and _inside(job_root, payload["path"])
                     and type(payload["dev"]) is int and type(payload["ino"]) is int,
                     "candidate_record_invalid")
            _require(all(c["name"] != payload["name"] for _, c in candidates),
                     "candidate_repeated")
            candidates.append((stage, payload))
    except (FinalizerError, JournalError) as error:
        return {"outcome": "NOT_FINALIZED", "cleanup": "UNPROVEN", "reason": str(error)}
    except OSError:
        return {"outcome": "NOT_FINALIZED", "cleanup": "UNPROVEN", "reason": "evidence_unreadable"}
    except (TypeError, KeyError, ValueError, UnicodeError):
        # A record of the wrong type anywhere in the evidence: fixed code,
        # nothing echoed.
        return {"outcome": "NOT_FINALIZED", "cleanup": "UNPROVEN", "reason": "evidence_malformed"}

    root_identity = (core["job_root"]["dev"], core["job_root"]["ino"])
    export = os.path.join(job_root, EXPORT_DIR)
    # From here on every outcome is a fixed report (F2213-R6). Nothing has
    # been removed yet, so a failure before the removals is cleanup UNPROVEN
    # with all mutable inputs still in place; what was written stays private
    # (0700) and unsealed, and no manifest digest is ever returned.
    try:
        os.mkdir(export, 0o700)
    except FileExistsError:
        return {"outcome": "FAILED", "cleanup": "UNPROVEN", "reason": "export_exists"}
    except OSError:
        return {"outcome": "FAILED", "cleanup": "UNPROVEN", "reason": "export_unavailable"}
    try:
        journal = PrivateJournal(os.path.join(export, "finalizer.jsonl"))
    except (OSError, ValueError, JournalError):
        return {"outcome": "FAILED", "cleanup": "UNPROVEN",
                "reason": "finalizer_journal_unavailable"}
    report = _sealing_steps(job_root, aggregation, core_bytes, records, evidence, registry,
                            secrets, candidates, root_identity, export, journal)
    try:
        journal.close()
    except OSError:
        # A closing failure never replaces an earlier report; it can only
        # withdraw a seal (the journal bytes in the manifest were read after
        # the explicit close inside the steps, so this is a second close).
        if report["outcome"] == "SEALED":
            return {"outcome": "FAILED", "cleanup": report["cleanup"],
                    "reason": "finalizer_journal_close_failed"}
    return report


def _unaccounted_root_entries(job_root, root_identity):
    """HOOK8-N1: count what is left in the job root after the removals,
    other than the retained export directory. Names are never recorded (a
    name could carry anything). The listing goes through a handle bound to
    the core's recorded root identity; any failure returns None, which the
    caller treats as not proven."""
    try:
        fd = os.open(job_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != root_identity:
            return None
        names = os.listdir(fd)
    except OSError:
        return None
    finally:
        os.close(fd)
    return sum(1 for name in names if name != EXPORT_DIR)


def _sealing_steps(job_root, aggregation, core_bytes, records, evidence, registry,
                   secrets, candidates, root_identity, export, journal):
    """Steps 3-8 after the gate; always returns a report dict."""
    cleanup = "UNPROVEN"
    try:
        journal.append({"step": "begin", "aggregation_stages": aggregation["stages"]})
        copied = []
        for stage, payload in candidates:
            data = _read_identified(payload["path"], payload["dev"], payload["ino"],
                                    EXPORT_ALLOWLIST[payload["name"]])
            target = os.path.join(export, payload["name"])
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                         | os.O_CLOEXEC, 0o600)
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            copied.append({"name": payload["name"], "size": len(data), "source_stage": stage,
                           "sha256": hashlib.sha256(data).hexdigest(),
                           "source": {"path": payload["path"], "dev": payload["dev"],
                                      "ino": payload["ino"], "type": "file"}})
        journal.append({"step": "candidates_copied", "count": len(copied)})

        # Removal inventory: every owned_resource, the registry, the stage
        # journals and the launch receipts. Identities come from the
        # records, or from this finalizer's own observation before removal.
        inventory = [payload for _, kind, payload in records if kind == "owned_resource"]
        # Copied candidate sources are owned files too; their recorded
        # identity is the one checked when they were read.
        inventory.extend(c.pop("source") for c in copied)
        inventory.append({"path": registry["path"], "dev": registry["dev"],
                          "ino": registry["ino"], "type": "file"})
        for name, kind in ((_sj.JOURNAL_DIR, "dir"), ("reference", "dir"),
                           (_sj.CORE_NAME, "file")):
            path = os.path.join(job_root, name)
            info = os.lstat(path)
            inventory.append({"path": path, "dev": info.st_dev, "ino": info.st_ino,
                              "type": kind})
        removals = [remove_owned(job_root, resource, root_identity)
                    for resource in inventory]
        # An entry nobody recorded is never removed (no glob deletion), but
        # it must not let the verdict read COMPLETE either (HOOK8-N1).
        unaccounted = _unaccounted_root_entries(job_root, root_identity)
        cleanup = "COMPLETE" if (all(r["outcome"] == "REMOVED" for r in removals)
                                 and unaccounted == 0) else "UNPROVEN"
        receipt = {"schema": SCHEMA_TERMINAL_RECEIPT,
                   "core_identity_sha256": hashlib.sha256(core_bytes).hexdigest(),
                   "aggregation": {"verdict": aggregation["verdict"],
                                   "stages": aggregation["stages"]},
                   "stop": evidence["stop"], "database": evidence["database"],
                   "candidates": copied, "removals": removals,
                   "unaccounted_root_entries": unaccounted, "cleanup": cleanup}
        receipt_bytes = (json.dumps(receipt, sort_keys=True) + "\n").encode()
        fd = os.open(os.path.join(export, "terminal-cleanup.json"),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600)
        try:
            view = memoryview(receipt_bytes)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        journal.append({"step": "terminal_receipt_written", "cleanup": cleanup})
        journal.close()

        names = sorted(os.listdir(export))
        expected = sorted(["finalizer.jsonl", "terminal-cleanup.json"]
                          + [c["name"] for c in copied])
        _require(names == expected, "export_set_changed")
        files = []
        for name in names:
            # One read per file: the bytes scanned are exactly the bytes
            # hashed into the manifest (F2FE89-R2).
            try:
                data = read_bounded(os.path.join(export, name), MAX_SCANNED_FILE)
            except (JournalError, OSError):
                return {"outcome": "FAILED", "cleanup": cleanup, "reason": "scan_finding"}
            if scan_bytes(data, secrets):
                return {"outcome": "FAILED", "cleanup": cleanup, "reason": "scan_finding"}
            files.append({"name": name, "size": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()})
        manifest = {"schema": SCHEMA_MANIFEST, "files": files, "scan": "clean",
                    "terminal_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
                    "cleanup": cleanup}
        manifest_bytes = (json.dumps(manifest, sort_keys=True) + "\n").encode()
        if scan_bytes(manifest_bytes, secrets):
            return {"outcome": "FAILED", "cleanup": cleanup, "reason": "scan_finding"}
        fd = os.open(os.path.join(export, "manifest.json"),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600)
        try:
            view = memoryview(manifest_bytes)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        for name in names + ["manifest.json"]:
            os.chmod(os.path.join(export, name), 0o400, follow_symlinks=False)
        os.chmod(export, 0o500)
        # The upload step must verify against THIS digest (verify_manifest).
        return {"outcome": "SEALED", "cleanup": cleanup, "reason": "sealed",
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest()}
    except (FinalizerError, JournalError) as error:
        return {"outcome": "FAILED", "cleanup": cleanup, "reason": str(error)}
    except OSError:
        return {"outcome": "FAILED", "cleanup": cleanup, "reason": "finalizer_io_failed"}
    except (TypeError, KeyError, ValueError, UnicodeError):
        return {"outcome": "FAILED", "cleanup": cleanup, "reason": "finalizer_step_malformed"}


def failure_record(job_root, stages, env):
    """The bounded failure-only record (section 9 step 1a).

    Used when finalize() returned NOT_FINALIZED. Required inputs, each
    independently closed (F2FE89-R4, R4a): the core identity (validated for
    this job root); the launch receipt; a structurally complete set of
    closed stage journals (aggregation VALID or FAILED; a closed FAILED
    stage is failure evidence, INCOMPLETE is unavailable); and exactly one
    stop_verdict record in those journals, of the exact shape, whose launch
    digest links the launch receipt and whose terminal digest, when not
    null, links the terminal receipt. If any input is unavailable, missing,
    repeated or malformed, nothing is written. The record holds the outcome,
    the stop verdict and reason, the aggregation outcome, and the inputs'
    hashes; only fixed identifiers, never payload, path or log content. It
    never builds the registry. It is scanned against SECRET_KEY and
    DATABASE_URL from ENV (each valid UTF-8 of at least MIN_SECRET bytes)
    plus the generic patterns; if they are unavailable or anything matches,
    nothing is written. Returns the record path, or None."""
    try:
        secrets = []
        for key in ("SECRET_KEY", "DATABASE_URL"):
            value = env.get(key)
            _require(type(value) is str, "failure_secret_unavailable")
            encoded = value.encode("utf-8")
            _require(len(encoded) >= MIN_SECRET, "failure_secret_unavailable")
            secrets.append(encoded)
        core_bytes = read_bounded(os.path.join(job_root, _sj.CORE_NAME), _sj.MAX_CORE)
        _sj.validate_core(core_bytes, job_root)
        launch_bytes = read_bounded(os.path.join(job_root, "reference", "launch.json"),
                                    MAX_REGISTRY)
        aggregation = _sj.aggregate(job_root, stages)
        _require(aggregation["verdict"] in ("VALID", "FAILED"), "failure_journals_unavailable")
        _, payload = _one(_journal_records(job_root, aggregation), "stop_verdict")
        _require(type(payload) is dict and set(payload) == {
            "verdict", "reason", "launch_receipt_sha256", "terminal_receipt_sha256"}
            and payload["verdict"] in ("COMPLETE", "FAILED", "UNPROVEN")
            and type(payload["reason"]) is str
            and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", payload["reason"]) is not None
            and payload["launch_receipt_sha256"] == hashlib.sha256(launch_bytes).hexdigest()
            and (payload["terminal_receipt_sha256"] is None
                 or _sha(payload["terminal_receipt_sha256"])),
            "failure_stop_unavailable")
        hashes = {"core_identity": hashlib.sha256(core_bytes).hexdigest(),
                  "launch_receipt": payload["launch_receipt_sha256"]}
        if payload["terminal_receipt_sha256"] is not None:
            terminal_bytes = read_bounded(os.path.join(job_root, "reference", "terminal.json"),
                                          MAX_REGISTRY)
            _require(hashlib.sha256(terminal_bytes).hexdigest()
                     == payload["terminal_receipt_sha256"], "failure_stop_unavailable")
            hashes["terminal_receipt"] = payload["terminal_receipt_sha256"]
        stop = {"verdict": payload["verdict"], "reason": payload["reason"]}
        record = {"schema": SCHEMA_FAILURE, "outcome": "FAIL", "cleanup": "UNPROVEN",
                  "stop": stop,
                  "aggregation": {"verdict": aggregation["verdict"],
                                  "reason": aggregation["reason"],
                                  "stages": [{"index": s["index"], "name": s["name"],
                                              "result": s["result"], "sha256": s["sha256"]}
                                             for s in aggregation["stages"]]},
                  "hashes": hashes}
        data = (json.dumps(record, sort_keys=True) + "\n").encode()
        if scan_bytes(data, secrets):
            return None
        export = os.path.join(job_root, EXPORT_DIR)
        os.mkdir(export, 0o700)
        path = os.path.join(export, "failure-record.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                     | os.O_CLOEXEC, 0o400)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(export, 0o500)
        return path
    except (FinalizerError, JournalError, OSError, TypeError, KeyError, ValueError,
            UnicodeError):
        return None


_REQUIRED_EXPORT = ("finalizer.jsonl", "terminal-cleanup.json")


def verify_manifest(export, expected_manifest_sha256):
    """Upload gate (section 9.5): True only if manifest.json is EXACTLY the
    sealed manifest (its sha256 equals the digest finalize() returned), has
    the exact schema and typed entries, lists the mandatory evidence plus
    only allowlisted names, links the terminal receipt, and every listed
    file re-hashes to its entry with nothing else present."""
    try:
        _require(_sha(expected_manifest_sha256), "manifest_invalid")
        raw = read_bounded(os.path.join(export, "manifest.json"), MAX_REGISTRY)
        _require(hashlib.sha256(raw).hexdigest() == expected_manifest_sha256,
                 "manifest_invalid")
        manifest = _sj.strict_json(raw, "manifest_invalid")
        _require(type(manifest) is dict and set(manifest) == {
            "schema", "files", "scan", "terminal_receipt_sha256", "cleanup"}
            and manifest["schema"] == SCHEMA_MANIFEST and manifest["scan"] == "clean"
            and manifest["cleanup"] in ("COMPLETE", "UNPROVEN")
            and _sha(manifest["terminal_receipt_sha256"])
            and type(manifest["files"]) is list, "manifest_invalid")
        names = []
        for entry in manifest["files"]:
            _require(type(entry) is dict and set(entry) == {"name", "size", "sha256"}
                     and type(entry["name"]) is str and type(entry["size"]) is int
                     and entry["size"] >= 0 and _sha(entry["sha256"])
                     and (entry["name"] in _REQUIRED_EXPORT
                          or entry["name"] in EXPORT_ALLOWLIST), "manifest_invalid")
            names.append(entry["name"])
        _require(len(set(names)) == len(names)
                 and all(n in names for n in _REQUIRED_EXPORT), "manifest_invalid")
        _require(sorted(os.listdir(export)) == sorted(names + ["manifest.json"]),
                 "manifest_invalid")
        for entry in manifest["files"]:
            data = read_bounded(os.path.join(export, entry["name"]), MAX_SCANNED_FILE)
            _require(len(data) == entry["size"]
                     and hashlib.sha256(data).hexdigest() == entry["sha256"],
                     "manifest_invalid")
            if entry["name"] == "terminal-cleanup.json":
                _require(entry["sha256"] == manifest["terminal_receipt_sha256"],
                         "manifest_invalid")
        return True
    except (FinalizerError, JournalError, OSError, KeyError, TypeError, ValueError):
        return False
