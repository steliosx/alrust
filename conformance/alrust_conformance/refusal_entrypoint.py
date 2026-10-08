"""Refusal-only infrastructure probe; never a conformance executor.

Run under the separately reviewed P_E. Original core/prefix expectations and
the frozen deadline come from the workflow, not from this reader. A valid
FAILED journal proves only these bytes; process closure belongs to P_E/A2.
Production file/journal paths require separate runtime admission. Pure controls
use an explicit memory runtime and never construct FileRuntime.
"""
import hashlib
import json
import os
import re
import stat
import time

STAGES = ("database-create", "database-create-exit", "launch", "launch-confirm")
OPTIONS = frozenset({"--job-root", "--core-sha256", "--prefix-digests",
                     "--launcher-exit", "--confirmation-exit", "--probe",
                     "--deadline-monotonic-ns"})
MAX_INT = (1 << 63) - 1
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_NUMBER = re.compile(r"[1-9][0-9]{0,18}\Z")
PUBLIC_ENV = frozenset({"PATH", "LANG", "LC_ALL"})


class RefusalError(ValueError):
    """Only fixed public diagnostic codes."""


class _IntentionalFailure(Exception):
    """Internal exception for StageJournal's actual FAILED close mechanism."""


def _need(condition, code):
    if not condition:
        raise RefusalError(code)


def parse_cli(argv):
    """Strict public declarations; no file/clock/environment access."""
    _need(type(argv) in (list, tuple) and len(argv) == 2 * len(OPTIONS)
          and all(type(v) is str and 0 < len(v) <= 4096 for v in argv), "usage")
    opts = {}
    for name, value in zip(argv[::2], argv[1::2]):
        _need(name in OPTIONS and name not in opts, "usage")
        opts[name] = value
    _need(set(opts) == OPTIONS, "usage")
    root = opts["--job-root"]
    _need(root.startswith("/") and root != "/" and not root.startswith("//")
          and all(part not in ("", ".", "..") for part in root[1:].split("/"))
          and all(32 <= ord(c) < 127 for c in root), "job_root_invalid")
    _need(_SHA.fullmatch(opts["--core-sha256"]) is not None, "core_digest_invalid")
    digests = tuple(opts["--prefix-digests"].split(","))
    _need(len(digests) == 4 and all(_SHA.fullmatch(v) is not None for v in digests),
          "prefix_digests_invalid")
    _need(opts["--launcher-exit"] == opts["--confirmation-exit"] == "0",
          "declared_invocation_failed")
    _need(opts["--probe"] == "refusal-cleanup", "probe_invalid")
    value = opts["--deadline-monotonic-ns"]
    _need(_NUMBER.fullmatch(value) is not None and int(value) <= MAX_INT,
          "deadline_invalid")
    return {"root": root, "core_sha256": opts["--core-sha256"],
            "digests": digests, "deadline": int(value)}


def _live(runtime, deadline, floor):
    now = runtime.now()
    _need(type(now) is int and floor <= now < deadline <= MAX_INT,
          "deadline_elapsed_or_clock_invalid")
    runtime.check()


def run_refusal(argv, runtime):
    """Core/prefix measured through RUNTIME before index4; always non-success.

    RUNTIME is explicit: production uses held no-follow reads and StageJournal;
    pure controls supply byte maps, a clock value and a recording journal.
    Exit declarations are labeled declared; never used as A2 closure proof.
    Unexpected/interrupt/secondary-close failures are NOT intentional refusal.
    """
    opts = parse_cli(argv)
    _need(set(runtime.environment_names()) <= PUBLIC_ENV, "environment_not_public")
    runtime.bind(opts["root"])
    core_bytes = runtime.read("core-identity.json", 65536)
    _need(hashlib.sha256(core_bytes).hexdigest() == opts["core_sha256"], "core_changed")
    core = runtime.validate_core(core_bytes, opts["root"])
    _need(core["schema"] == "alrust-hosted-core/3", "core_schema_invalid")
    _need(core["boot_id"] == runtime.boot(), "core_boot_changed")
    floor = core["captured_monotonic_ns"]
    _live(runtime, opts["deadline"], floor)
    runtime.expect_names(tuple("%02d-%s.jsonl" % (i, n) for i, n in enumerate(STAGES)))
    previous = previous_name = None
    for index, (name, expected) in enumerate(zip(STAGES, opts["digests"])):
        data = runtime.read("journal/%02d-%s.jsonl" % (index, name), 1 << 20)
        _need(hashlib.sha256(data).hexdigest() == expected, "prefix_changed")
        result, count, floor = runtime.parse_journal(
            data, index, name, core, opts["core_sha256"], previous, previous_name, floor)
        _need(result == "COMPLETE" and count >= 2, "prefix_not_complete")
        previous, previous_name = expected, name
    _live(runtime, opts["deadline"], floor)
    # StageJournal rereads the core and previous path when opening. Recheck all
    # held ancestor/leaf identities both before and after, and inspect the final
    # journal's actual bindings below. Same-UID mutation is not a security fence.
    try:
        with runtime.journal(opts["root"], 4, "entrypoint", "launch-confirm") as journal:
            _live(runtime, opts["deadline"], floor)
            journal.record("infrastructure_probe", {
                "probe": "refusal-cleanup", "execution": "FAIL",
                "launcher_exit_declared": 0, "confirmation_exit_declared": 0,
                "conformance_executed": False})
            _live(runtime, opts["deadline"], floor)
            journal.fail("infrastructure_refusal")
            raise _IntentionalFailure()
    except _IntentionalFailure:
        pass
    # StageJournal can leave an incomplete tail after a close write failure
    # while propagating the deliberate exception. Never report ordinary F1
    # until the actual bounded closed file has been parsed and rechecked.
    runtime.check()
    data = runtime.read("journal/04-entrypoint.jsonl", 1 << 20)
    result, count, _ = runtime.parse_journal(
        data, 4, "entrypoint", core, opts["core_sha256"], previous, previous_name, floor)
    _need(result == "FAILED" and count == 3
          and runtime.terminal_payload(data) == {
              "result": "FAILED", "code": "infrastructure_refusal"},
          "refusal_journal_unproven")
    runtime.check()
    return {"execution": "FAIL", "journal": "FAILED_VALID",
            "reason": "infrastructure_refusal"}, 2


def _identity(info, *, leaf=False):
    basic = (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode))
    return basic + (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink) if leaf else basic


class HeldReader:
    """Read public core/journals with every ancestor descriptor held.

    No process, socket, SQL, signal, external-config or neighboring-job read.
    Root and journal directory must be caller-owned0700. Public leaves must
    be caller-owned regular0600, singly linked, bounded and stable to EOF.
    All relevant path identities are rechecked until the caller closes us.
    """
    def __init__(self, root):
        self.fds, self.links, self.leaves = [], [], {}
        self.root = self.directory = None
        try:
            fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            self.fds.append(fd)
            for part in root[1:].split("/"):
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                                | os.O_CLOEXEC, dir_fd=fd)
                self.fds.append(child)
                self.links.append((fd, part, _identity(os.fstat(child))))
                fd = child
            self.root = fd
            self._private(fd)
            child = os.open("journal", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                            | os.O_CLOEXEC, dir_fd=fd)
            self.fds.append(child)
            self.links.append((fd, "journal", _identity(os.fstat(child))))
            self._private(child)
            self.directory = child
            self.check()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _private(fd):
        info = os.fstat(fd)
        _need(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
              "directory_not_private")

    def read(self, relative, limit):
        parts = relative.split("/")
        if len(parts) == 1:
            _need(relative == "core-identity.json", "reader_path_invalid")
            parent, name = self.root, relative
        else:
            _need(len(parts) == 2 and parts[0] == "journal"
                  and re.fullmatch(r"0[0-4]-[a-z-]+\.jsonl", parts[1]) is not None,
                  "reader_path_invalid")
            parent, name = self.directory, parts[1]
        self.check()
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                     | os.O_NONBLOCK | os.O_NOCTTY, dir_fd=parent)
        try:
            info = os.fstat(fd)
            _need(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                  and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
                  and 0 <= info.st_size <= limit, "file_not_private_bounded")
            identity = _identity(info, leaf=True)
            chunks, size = [], 0
            while size <= limit:
                chunk = os.read(fd, min(65536, limit + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            _need(size <= limit and size == info.st_size
                  and _identity(os.fstat(fd), leaf=True) == identity, "file_changed")
        finally:
            os.close(fd)
        key = (parent, name)
        _need(key not in self.leaves or self.leaves[key] == identity, "file_changed")
        self.leaves[key] = identity
        self.check()
        return b"".join(chunks)

    def check(self):
        for parent, name, expected in self.links:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            _need(stat.S_ISDIR(info.st_mode) and _identity(info) == expected,
                  "ancestor_changed")
        for (parent, name), expected in self.leaves.items():
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            _need(stat.S_ISREG(info.st_mode) and _identity(info, leaf=True) == expected,
                  "file_changed")

    def expect_names(self, names):
        self.check()
        _need(sorted(os.listdir(self.directory)) == sorted(names), "journal_set_invalid")

    def close(self):
        error = None
        while self.fds:
            try:
                os.close(self.fds.pop())
            except BaseException as exc:
                if error is None:
                    error = exc
        if error is not None:
            raise error


class FileRuntime:
    """Actual adapter: no invocation in pure controls or before admission."""
    def __init__(self):
        from . import stage_journal
        self.sj = stage_journal
        self.reader = None

    def environment_names(self):
        return tuple(os.environ)

    def bind(self, root):
        self.reader = HeldReader(root)

    def now(self):
        return time.monotonic_ns()

    def boot(self):
        return self.sj.boot_id()

    def read(self, relative, limit):
        return self.reader.read(relative, limit)

    def check(self):
        self.reader.check()

    def expect_names(self, names):
        self.reader.expect_names(names)

    def validate_core(self, data, root):
        return self.sj.validate_core(data, root)

    def parse_journal(self, *args):
        return self.sj._parse_journal(*args)

    def journal(self, *args):
        return self.sj.StageJournal(*args)

    def terminal_payload(self, data):
        return self.sj.strict_json(data.rstrip(b"\n").split(b"\n")[-1],
                                   "refusal_journal_unproven")["evidence"]["payload"]

    def close(self):
        if self.reader is not None:
            self.reader.close()


def main(argv):
    runtime = None
    try:
        parse_cli(argv)  # Invalid invocation imports no journal or file adapter.
        runtime = FileRuntime()
        report, code = run_refusal(argv, runtime)
    except RefusalError:
        report, code = {"execution": "FAIL", "journal": "UNPROVEN", "reason": "probe_refused"}, 65
    except KeyboardInterrupt:
        report, code = {"execution": "FAIL", "journal": "UNPROVEN", "reason": "interrupted"}, 130
    except Exception:
        # Never print raw input, paths or exception text; an unknown close/write
        # failure is distinct from the deliberately closed FAILED probe.
        report, code = {"execution": "FAIL", "journal": "UNPROVEN", "reason": "runtime_error"}, 75
    finally:
        if runtime is not None:
            try:
                runtime.close()
            except KeyboardInterrupt:
                report, code = {"execution": "FAIL", "journal": "UNPROVEN", "reason": "interrupted"}, 130
            except Exception:
                report, code = {"execution": "FAIL", "journal": "UNPROVEN", "reason": "runtime_error"}, 75
    print(json.dumps(report, sort_keys=True))
    return code
