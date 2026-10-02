"""Frozen core identity, per-stage journals and their aggregation.

Contract: docs/ci-launch-attestation-contract.md, sections 2 and 3. This is
offline source: no workflow runs it yet, and it starts no process.

- The core identity (stage 0) is written once, exclusively, before any
  allocation, to job_root/core-identity.json. It holds nothing about a
  resource that does not exist yet.
- Every stage writes its own exclusive journal job_root/journal/NN-NAME.jsonl
  through the unchanged PrivateJournal (fsync per record). Its first record
  binds the core identity's sha256 and the previous stage journal's final
  sha256. Its last record is stage_complete.
- aggregate() validates the whole set without rewriting anything. Anything
  missing, extra, truncated, out of sequence or unchained is INCOMPLETE,
  never a success.

Records carry only values observed at their own capture, with an explicit
null for a resource that does not exist yet. Failure codes are fixed
strings; exception text is never recorded.
"""
import hashlib
import json
import os
import re
import stat
import sys
import time

from .security_evidence import PrivateJournal

SCHEMA_CORE = "alrust-hosted-core/1"
SCHEMA_RECORD = "alrust-stage-record/1"
EXTERNAL_CONFIG = "/etc/alertad.conf"
CORE_NAME = "core-identity.json"
JOURNAL_DIR = "journal"
MAX_CORE = 65536
MAX_JOURNAL = 1 << 20
MAX_STAGES = 32
_STAGE = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
_KIND = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_GIT = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DIGITS = re.compile(r"[1-9][0-9]{0,19}\Z")
_TEXT = re.compile(r"[A-Za-z0-9._/-]{1,128}\Z")
_BOOT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
MAX_INT = (1 << 63) - 1
CORE_KEYS = frozenset({
    "schema", "repository", "run_id", "run_attempt", "job", "profile",
    "event_head_sha", "checkout_sha", "reference_sha", "lock_sha256",
    "manifest_sha256", "runner_image", "boot_id", "uid", "external_config",
    "job_root", "captured_utc_ns", "captured_monotonic_ns"})
ENVELOPE_KEYS = frozenset({"schema", "kind", "stage", "utc_ns", "monotonic_ns",
                           "boot_id", "pid", "payload"})


class JournalError(ValueError):
    """Fixed codes only."""


def _require(condition, code):
    if not condition:
        raise JournalError(code)


def boot_id():
    with open("/proc/sys/kernel/random/boot_id") as f:
        return f.read(64).strip()


def read_bounded(path, limit):
    """A regular file's bytes, read without following links or blocking."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                 | os.O_NONBLOCK | os.O_NOCTTY)
    try:
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_size <= limit,
                 "not_bounded_regular_file")
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(fd, limit + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    _require(size <= limit, "not_bounded_regular_file")
    return b"".join(chunks)


def _private_dir(path):
    info = os.lstat(path)
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
             and stat.S_IMODE(info.st_mode) == 0o700
             and os.path.realpath(path) == path, "directory_not_private")
    return info


def _job_root(job_root):
    _require(type(job_root) is str and os.path.isabs(job_root)
             and os.path.normpath(job_root) == job_root, "job_root_invalid")
    return _private_dir(job_root)


def _write_exclusive(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                 | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            _require(written > 0, "write_no_progress")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    parent = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _int(value, low=0, high=MAX_INT):
    return type(value) is int and low <= value <= high


def strict_json(data, code):
    """Parse JSON refusing duplicate members, non-finite numbers, invalid
    UTF-8 and over-deep nesting, all as the fixed CODE."""
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, code)
            result[key] = value
        return result

    def constant(_):
        raise JournalError(code)
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=constant)
    except JournalError:
        raise
    except (ValueError, RecursionError, UnicodeError):
        raise JournalError(code) from None


def validate_core(data, job_root):
    """The core identity's bytes as a fully typed record for THIS job root."""
    core = strict_json(data, "core_invalid")
    _require(type(core) is dict and set(core) == CORE_KEYS
             and core["schema"] == SCHEMA_CORE, "core_invalid")
    for key in ("repository", "job"):
        _require(type(core[key]) is str and _TEXT.fullmatch(core[key]), "core_invalid")
    for key in ("run_id", "run_attempt"):
        _require(type(core[key]) is str and _DIGITS.fullmatch(core[key]), "core_invalid")
    _require(type(core["profile"]) is str and _STAGE.fullmatch(core["profile"]),
             "core_invalid")
    for key in ("event_head_sha", "checkout_sha", "reference_sha"):
        _require(type(core[key]) is str and _GIT.fullmatch(core[key]), "core_invalid")
    locks = core["lock_sha256"]
    _require(type(locks) is dict and 1 <= len(locks) <= 8 and all(
        type(k) is str and _TEXT.fullmatch(k) and type(v) is str and _SHA256.fullmatch(v)
        for k, v in locks.items()), "core_invalid")
    _require(type(core["manifest_sha256"]) is str
             and _SHA256.fullmatch(core["manifest_sha256"]), "core_invalid")
    image = core["runner_image"]
    _require(type(image) is dict and set(image) == {"os", "version"} and all(
        type(v) is str and _TEXT.fullmatch(v) for v in image.values()), "core_invalid")
    _require(type(core["boot_id"]) is str and _BOOT.fullmatch(core["boot_id"]),
             "core_invalid")
    _require(_int(core["uid"], 0, (1 << 32) - 1) and core["uid"] == os.getuid(),
             "core_invalid")
    _require(core["external_config"] == {"path": EXTERNAL_CONFIG, "state": "absent"},
             "core_invalid")
    _require(_int(core["captured_utc_ns"]) and _int(core["captured_monotonic_ns"]),
             "core_invalid")
    root = core["job_root"]
    actual = _job_root(job_root)
    _require(type(root) is dict and set(root) == {"path", "dev", "ino"}
             and root["path"] == job_root and _int(root["dev"]) and _int(root["ino"])
             and (root["dev"], root["ino"]) == (actual.st_dev, actual.st_ino),
             "core_job_root_mismatch")
    return core


def _dumps(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode()


def write_core_identity(job_root, *, profile, event_head_sha, checkout_sha,
                        reference_sha, reference_pin, lock_sha256, lock_pins,
                        manifest_sha256, manifest_pin, env):
    """Write the frozen core identity once (stage 0) and return its sha256.

    Every input is checked; any unset, malformed or unpinned value refuses,
    which is a NOT_RUN prerequisite failure. Nothing is written on refusal.
    """
    root = _job_root(job_root)
    _require(type(profile) is str and _STAGE.fullmatch(profile), "profile_invalid")
    for value in (event_head_sha, checkout_sha, reference_sha, reference_pin):
        _require(type(value) is str and _GIT.fullmatch(value), "git_sha_invalid")
    _require(reference_sha == reference_pin, "reference_not_pinned")
    _require(type(lock_sha256) is dict and type(lock_pins) is dict
             and 1 <= len(lock_pins) <= 8, "locks_invalid")
    for name, value in list(lock_sha256.items()) + list(lock_pins.items()):
        _require(type(name) is str and _TEXT.fullmatch(name) and type(value) is str
                 and _SHA256.fullmatch(value), "locks_invalid")
    _require(lock_sha256 == lock_pins, "locks_not_pinned")
    for value in (manifest_sha256, manifest_pin):
        _require(type(value) is str and _SHA256.fullmatch(value), "manifest_invalid")
    _require(manifest_sha256 == manifest_pin, "manifest_not_pinned")
    github = {}
    for key in ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
                "GITHUB_JOB", "ImageOS", "ImageVersion"):
        value = env.get(key)
        _require(type(value) is str and _TEXT.fullmatch(value), "environment_invalid")
        github[key] = value
    for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        _require(_DIGITS.fullmatch(github[key]), "environment_invalid")
    try:
        os.lstat(EXTERNAL_CONFIG)
    except FileNotFoundError:
        pass
    except OSError:
        raise JournalError("external_config_unreadable") from None
    else:
        raise JournalError("external_config_present")
    record = {
        "schema": SCHEMA_CORE,
        "repository": github["GITHUB_REPOSITORY"],
        "run_id": github["GITHUB_RUN_ID"],
        "run_attempt": github["GITHUB_RUN_ATTEMPT"],
        "job": github["GITHUB_JOB"],
        "profile": profile,
        "event_head_sha": event_head_sha,
        "checkout_sha": checkout_sha,
        "reference_sha": reference_sha,
        "lock_sha256": dict(sorted(lock_sha256.items())),
        "manifest_sha256": manifest_sha256,
        "runner_image": {"os": github["ImageOS"], "version": github["ImageVersion"]},
        "boot_id": boot_id(),
        "uid": os.getuid(),
        "external_config": {"path": EXTERNAL_CONFIG, "state": "absent"},
        "job_root": {"path": job_root, "dev": root.st_dev, "ino": root.st_ino},
        "captured_utc_ns": time.time_ns(),
        "captured_monotonic_ns": time.monotonic_ns(),
    }
    data = _dumps(record)
    _write_exclusive(os.path.join(job_root, CORE_NAME), data)
    return hashlib.sha256(data).hexdigest()


def journal_path(job_root, index, name):
    return os.path.join(job_root, JOURNAL_DIR, "%02d-%s.jsonl" % (index, name))


def prepare_journal_dir(job_root):
    """Create job_root/journal (0700) exclusively; it must not exist yet."""
    _job_root(job_root)
    os.mkdir(os.path.join(job_root, JOURNAL_DIR), 0o700)


class StageJournal:
    """One stage's exclusive journal, as a context manager.

    On entry: stage_begin, binding the core identity's sha256 and the
    previous stage journal's final sha256 (both recomputed from disk). On a
    clean exit: stage_complete COMPLETE. On an exception: stage_complete
    FAILED with the fixed code set by fail(), or "stage_failed". The
    exception is never recorded and still propagates.

    PrivateJournal closes itself after a failed write. A later
    stage_complete then cannot be written, the stage ends without its final
    record, and aggregate() reports it INCOMPLETE: a failed write is never
    hidden by a later success.
    """

    def __init__(self, job_root, index, name, previous_name=None):
        _require(type(index) is int and 0 <= index < MAX_STAGES, "stage_index_invalid")
        _require(type(name) is str and _STAGE.fullmatch(name), "stage_name_invalid")
        _require((index == 0) == (previous_name is None), "previous_stage_invalid")
        _require(previous_name is None or _STAGE.fullmatch(previous_name),
                 "previous_stage_invalid")
        self._root, self._index, self._name = job_root, index, name
        self._previous = previous_name
        self._journal = None
        self._code = None

    def _envelope(self, kind, payload):
        return {"schema": SCHEMA_RECORD, "kind": kind,
                "stage": {"index": self._index, "name": self._name},
                "utc_ns": time.time_ns(), "monotonic_ns": time.monotonic_ns(),
                "boot_id": boot_id(), "pid": os.getpid(), "payload": payload}

    def __enter__(self):
        _job_root(self._root)
        _private_dir(os.path.join(self._root, JOURNAL_DIR))
        core = read_bounded(os.path.join(self._root, CORE_NAME), MAX_CORE)
        validate_core(core, self._root)
        previous = None
        if self._previous is not None:
            data = read_bounded(journal_path(self._root, self._index - 1,
                                             self._previous), MAX_JOURNAL)
            previous = {"index": self._index - 1, "name": self._previous,
                        "sha256": hashlib.sha256(data).hexdigest()}
        self._journal = PrivateJournal(journal_path(self._root, self._index, self._name))
        try:
            self._journal.append(self._envelope("stage_begin", {
                "core_identity_sha256": hashlib.sha256(core).hexdigest(),
                "previous_stage": previous}))
        except BaseException:
            self._journal.close()
            raise
        return self

    def record(self, kind, payload):
        """Append one record. kind is a fixed identifier; payload is a dict of
        observed values (explicit None for resources that do not exist)."""
        _require(type(kind) is str and _KIND.fullmatch(kind)
                 and kind not in ("stage_begin", "stage_complete"), "kind_invalid")
        _require(type(payload) is dict, "payload_invalid")
        self._journal.append(self._envelope(kind, payload))

    def fail(self, code):
        """Set the fixed failure code recorded if the stage then fails."""
        _require(type(code) is str and _CODE.fullmatch(code), "code_invalid")
        self._code = code

    def __exit__(self, exc_type, exc, traceback):
        result = {"result": "COMPLETE"} if exc_type is None else \
            {"result": "FAILED", "code": self._code or "stage_failed"}
        try:
            self._journal.append(self._envelope("stage_complete", result))
        except (OSError, ValueError):
            # The journal closed after a failed write, or this write failed:
            # the stage stays without stage_complete, which is INCOMPLETE.
            if exc_type is None:
                raise
        finally:
            self._journal.close()
        return False


def _validate_envelope(evidence, index, name, boot, floor):
    """One record's envelope; returns its monotonic time."""
    _require(type(evidence) is dict and set(evidence) == ENVELOPE_KEYS
             and evidence["schema"] == SCHEMA_RECORD, "record_envelope_invalid")
    stage = evidence["stage"]
    _require(type(stage) is dict and set(stage) == {"index", "name"}
             and type(stage["index"]) is int and stage["index"] == index
             and stage["name"] == name, "record_stage_mismatch")
    _require(type(evidence["kind"]) is str and _KIND.fullmatch(evidence["kind"]),
             "record_kind_invalid")
    _require(_int(evidence["utc_ns"]) and _int(evidence["monotonic_ns"])
             and _int(evidence["pid"], 1, 4194304), "record_capture_invalid")
    _require(type(evidence["boot_id"]) is str and evidence["boot_id"] == boot,
             "record_boot_mismatch")
    # Monotonic time never goes backwards on one boot; wall-clock UTC may.
    _require(evidence["monotonic_ns"] >= floor, "record_monotonic_regressed")
    _require(type(evidence["payload"]) is dict, "record_payload_invalid")
    return evidence["monotonic_ns"]


def _parse_journal(data, index, name, core, core_sha256, previous_sha256,
                   previous_name, floor):
    """Validate one journal's bytes; return (result, records, last monotonic)."""
    _require(data.endswith(b"\n"), "truncated_record")
    lines = data[:-1].split(b"\n")
    _require(len(lines) >= 2, "too_few_records")
    records = []
    for expected_sequence, line in enumerate(lines):
        record = strict_json(line, "record_not_json")
        _require(type(record) is dict and set(record) == {"sequence", "evidence"}
                 and type(record["sequence"]) is int
                 and record["sequence"] == expected_sequence, "sequence_invalid")
        floor = _validate_envelope(record["evidence"], index, name,
                                   core["boot_id"], floor)
        records.append(record["evidence"])
    first, last = records[0], records[-1]
    _require(first["kind"] == "stage_begin", "first_not_stage_begin")
    expected_previous = None if previous_name is None else {
        "index": index - 1, "name": previous_name, "sha256": previous_sha256}
    begin = first["payload"]
    _require(set(begin) == {"core_identity_sha256", "previous_stage"}
             and begin["core_identity_sha256"] == core_sha256, "core_identity_mismatch")
    _require(begin["previous_stage"] == expected_previous, "chain_broken")
    _require(last["kind"] == "stage_complete", "no_stage_complete")
    _require(all(r["kind"] not in ("stage_begin", "stage_complete")
                 for r in records[1:-1]), "misplaced_boundary_record")
    payload = last["payload"]
    _require(payload == {"result": "COMPLETE"} or (
        set(payload) == {"result", "code"} and payload["result"] == "FAILED"
        and type(payload["code"]) is str and _CODE.fullmatch(payload["code"])),
        "result_invalid")
    return payload["result"], len(records), floor


def aggregate(job_root, stages):
    """Validate the complete journal set without modifying it.

    Returns {"verdict", "reason", "stages"}:
    - VALID: every expected journal exists, in order, with no other file;
      every record is well formed and chained; and every stage COMPLETED;
    - FAILED: the set is structurally complete, but at least one stage
      recorded FAILED. That is never a success (contract section 3);
    - INCOMPLETE: anything missing, extra, malformed or unchained.
    The per-stage results so far are returned in every case.
    """
    results = []
    try:
        _require(type(stages) is list and 1 <= len(stages) <= MAX_STAGES
                 and len(set(stages)) == len(stages)
                 and all(type(s) is str and _STAGE.fullmatch(s) for s in stages),
                 "expected_stages_invalid")
        _job_root(job_root)
        directory = os.path.join(job_root, JOURNAL_DIR)
        _private_dir(directory)
        expected = ["%02d-%s.jsonl" % (i, name) for i, name in enumerate(stages)]
        _require(sorted(os.listdir(directory)) == sorted(expected), "journal_set_mismatch")
        data = read_bounded(os.path.join(job_root, CORE_NAME), MAX_CORE)
        core = validate_core(data, job_root)
        core_sha256 = hashlib.sha256(data).hexdigest()
        floor = core["captured_monotonic_ns"]
        previous_sha256 = previous_name = None
        for index, name in enumerate(stages):
            data = read_bounded(journal_path(job_root, index, name), MAX_JOURNAL)
            result, count, floor = _parse_journal(
                data, index, name, core, core_sha256, previous_sha256,
                previous_name, floor)
            previous_sha256 = hashlib.sha256(data).hexdigest()
            previous_name = name
            results.append({"index": index, "name": name, "result": result,
                            "records": count, "sha256": previous_sha256})
    except JournalError as error:
        return {"verdict": "INCOMPLETE", "reason": str(error), "stages": results}
    except FileNotFoundError:
        return {"verdict": "INCOMPLETE", "reason": "file_missing", "stages": results}
    except OSError:
        return {"verdict": "INCOMPLETE", "reason": "file_unreadable", "stages": results}
    if any(stage["result"] != "COMPLETE" for stage in results):
        return {"verdict": "FAILED", "reason": "stage_failed", "stages": results,
                "core_identity_sha256": core_sha256}
    return {"verdict": "VALID", "reason": "complete", "stages": results,
            "core_identity_sha256": core_sha256}


MANIFEST_PATH = "conformance/hosted-profiles.json"
REFERENCE_PATH = "reference/alerta"


def _git_head(directory):
    import subprocess
    out = subprocess.run(["git", "-C", directory, "rev-parse", "--verify", "HEAD"],
                         capture_output=True, text=True, timeout=30,
                         env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
    _require(out.returncode == 0, "git_unavailable")
    value = out.stdout.strip()
    _require(_GIT.fullmatch(value), "git_sha_invalid")
    return value


def core_main(argv):
    """core --repo-root R --job-root J --profile P --event-head-sha S
    --reference-pin S --manifest-pin H --lock-pin PATH=H [--lock-pin ...]

    Measures the checkout, the reference submodule, each pinned lock file and
    the profile manifest from the repository itself, then writes the core
    identity. Exit 0 WRITTEN, 2 NOT_RUN (a refused prerequisite), 64 usage."""
    opts, pins = {}, {}
    names = ("--repo-root", "--job-root", "--profile", "--event-head-sha",
             "--reference-pin", "--manifest-pin")
    if len(argv) % 2:
        return _emit({"status": "NOT_RUN", "reason": "usage"}, 64)
    for name, value in zip(argv[0::2], argv[1::2]):
        if name == "--lock-pin" and "=" in value:
            path, _, digest = value.partition("=")
            if path in pins:
                return _emit({"status": "NOT_RUN", "reason": "usage"}, 64)
            pins[path] = digest
        elif name in names and name not in opts:
            opts[name] = value
        else:
            return _emit({"status": "NOT_RUN", "reason": "usage"}, 64)
    if set(opts) != set(names) or not pins:
        return _emit({"status": "NOT_RUN", "reason": "usage"}, 64)
    repo = opts["--repo-root"]
    try:
        _require(os.path.isabs(repo) and os.path.normpath(repo) == repo,
                 "repo_root_invalid")
        for path in pins:
            _require(_TEXT.fullmatch(path) and not path.startswith("/")
                     and ".." not in path.split("/"), "locks_invalid")
        digest = write_core_identity(
            opts["--job-root"], profile=opts["--profile"],
            event_head_sha=opts["--event-head-sha"],
            checkout_sha=_git_head(repo),
            reference_sha=_git_head(os.path.join(repo, REFERENCE_PATH)),
            reference_pin=opts["--reference-pin"],
            lock_sha256={path: hashlib.sha256(read_bounded(
                os.path.join(repo, path), MAX_JOURNAL)).hexdigest() for path in pins},
            lock_pins=pins,
            manifest_sha256=hashlib.sha256(read_bounded(
                os.path.join(repo, MANIFEST_PATH), MAX_JOURNAL)).hexdigest(),
            manifest_pin=opts["--manifest-pin"], env=os.environ)
    except JournalError as error:
        return _emit({"status": "NOT_RUN", "reason": str(error)}, 2)
    except FileExistsError:
        return _emit({"status": "NOT_RUN", "reason": "core_identity_exists"}, 2)
    except OSError:
        return _emit({"status": "NOT_RUN", "reason": "file_unreadable"}, 2)
    return _emit({"status": "WRITTEN", "sha256": digest}, 0)


def _emit(record, code):
    sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    return code


def main(argv):
    """CLI: aggregate --job-root J --stages a,b,c (exit 0 VALID only; 2 for
    FAILED or INCOMPLETE; 64 usage), or core ... (see core_main). One JSON
    line."""
    if argv[:1] == ["core"]:
        return core_main(argv[1:])
    if len(argv) != 5 or argv[0] != "aggregate" or argv[1] != "--job-root" \
            or argv[3] != "--stages":
        return _emit({"verdict": "INCOMPLETE", "reason": "usage"}, 64)
    report = aggregate(argv[2], argv[4].split(","))
    return _emit(report, 0 if report["verdict"] == "VALID" else 2)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
