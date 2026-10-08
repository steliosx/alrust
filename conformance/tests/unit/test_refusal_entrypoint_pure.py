"""Named memory-only controls: no FileRuntime/HeldReader/PrivateJournal calls.

Imports: refusal policy (stdlib definitions); stage_journal and its
security_evidence dependency (stdlib definitions). Only stage_journal's pure
_parse_journal/strict_json path is called. Core validation and file identity
operations are explicit memory adapters, not filesystem qualification.
"""
import copy
import contextlib
import hashlib
import io
import json
import os
import stat
import types
import unittest

from alrust_conformance import refusal_entrypoint as probe
from alrust_conformance import stage_journal as sj


def digest(data):
    return hashlib.sha256(data).hexdigest()


def wire(index, name, core_hash, previous, previous_name, payloads):
    records = [{"kind": "stage_begin", "payload": {
        "core_identity_sha256": core_hash,
        "previous_stage": None if index == 0 else {
            "index": index - 1, "name": previous_name, "sha256": previous}}}]
    records += payloads
    out = []
    for sequence, record in enumerate(records):
        evidence = {"schema": sj.SCHEMA_RECORD, **record,
                    "stage": {"index": index, "name": name},
                    "utc_ns": 1000, "monotonic_ns": 100 + index * 10 + sequence,
                    "boot_id": "memory-boot", "pid": 10}
        out.append(json.dumps({"sequence": sequence, "evidence": evidence}).encode())
    return b"\n".join(out) + b"\n"


class MemoryJournal:
    def __init__(self, runtime):
        self.runtime, self.payloads, self.code = runtime, [], None

    def __enter__(self):
        self.runtime.opens += 1
        if self.runtime.failure == "enter":
            raise OSError("private text must never become output")
        return self

    def record(self, kind, payload):
        if self.runtime.failure == "record":
            raise OSError("record failed")
        if self.runtime.failure == "interrupt":
            raise KeyboardInterrupt()
        self.payloads.append({"kind": kind, "payload": payload})

    def fail(self, code):
        self.code = code

    def __exit__(self, exc_type, exc, traceback):
        if self.runtime.failure == "close":
            raise OSError("close failed")
        if self.runtime.failure != "missing-terminal":
            self.payloads.append({"kind": "stage_complete", "payload": {
                "result": "FAILED" if exc_type else "COMPLETE",
                "code": self.code or "stage_failed"}})
        self.runtime.files["journal/04-entrypoint.jsonl"] = wire(
            4, "entrypoint", digest(self.runtime.files["core-identity.json"]),
            digest(self.runtime.files["journal/03-launch-confirm.jsonl"]),
            "launch-confirm", self.payloads)
        return False


class MemoryRuntime:
    def __init__(self):
        self.core = {"schema": "alrust-hosted-core/3", "boot_id": "memory-boot",
                     "captured_monotonic_ns": 100}
        self.files = {"core-identity.json": json.dumps(self.core).encode()}
        self.clock, self.opens, self.checks = 1000, 0, 0
        self.failure, self.change_at, self.environment = None, None, ("PATH", "LANG")
        previous = previous_name = None
        for index, name in enumerate(probe.STAGES):
            data = wire(index, name, digest(self.files["core-identity.json"]),
                        previous, previous_name,
                        [{"kind": "stage_complete", "payload": {"result": "COMPLETE"}}])
            self.files["journal/%02d-%s.jsonl" % (index, name)] = data
            previous, previous_name = digest(data), name
        self.argv = ["--job-root", "/synthetic/job", "--core-sha256",
                     digest(self.files["core-identity.json"]), "--prefix-digests",
                     ",".join(digest(self.files["journal/%02d-%s.jsonl" % (i, n)])
                              for i, n in enumerate(probe.STAGES)),
                     "--launcher-exit", "0", "--confirmation-exit", "0",
                     "--probe", "refusal-cleanup", "--deadline-monotonic-ns", "2000"]

    def environment_names(self):
        return self.environment

    def bind(self, root):
        assert root == "/synthetic/job"

    def now(self):
        return self.clock

    def boot(self):
        return "memory-boot"

    def read(self, path, limit):
        data = self.files[path]
        assert len(data) <= limit
        return data

    def validate_core(self, data, root):
        return json.loads(data)  # Explicit stub; actual root/pin validation NOT tested.

    def check(self):
        self.checks += 1
        if self.change_at == self.checks:
            raise probe.RefusalError("ancestor_changed")

    def expect_names(self, names):
        probe._need(sorted(k.split("/")[1] for k in self.files if k.startswith("journal/"))
                    == sorted(names), "journal_set_invalid")

    def parse_journal(self, *args):
        return sj._parse_journal(*args)  # Actual pure parser, no reader or procfs.

    def journal(self, root, index, name, previous):
        assert (root, index, name, previous) == ("/synthetic/job", 4, "entrypoint", "launch-confirm")
        return MemoryJournal(self)

    def terminal_payload(self, data):
        return json.loads(data.rstrip(b"\n").split(b"\n")[-1])["evidence"]["payload"]

    def close(self):
        if self.failure == "descriptor-close":
            raise OSError("private close detail")


class MemoryOS:
    """Descriptor-relative in-memory directory/file objects; no OS calls."""
    def __init__(self):
        self.nodes, self.fds, self.next_fd, self.chunk = {}, {}, 10, 1
        self.root = self.node(stat.S_IFDIR | 0o755)
        parent = self.root
        for name in ("synthetic", "job", "journal"):
            child = self.node(stat.S_IFDIR | 0o700)
            parent.children[name] = child
            self.nodes[name] = child
            parent = child
        self.nodes["job"].children["core-identity.json"] = self.node(stat.S_IFREG | 0o600, b"public core bytes")

    def node(self, mode, data=b""):
        info = types.SimpleNamespace(st_dev=1, st_ino=len(self.nodes) + self.next_fd,
                                     st_uid=100, st_mode=mode, st_size=len(data),
                                     st_mtime_ns=1, st_ctime_ns=1, st_nlink=1)
        self.next_fd += 1
        return types.SimpleNamespace(info=info, data=data, children={})

    def __getattr__(self, name):
        # Only flag constants, not functions, are taken from the real module.
        if name.startswith("O_"):
            return getattr(os, name)
        raise AttributeError(name)

    def geteuid(self):
        return 100

    def open(self, path, flags, *, dir_fd=None):
        node = self.root if path == "/" else self.fds[dir_fd][0].children[path]
        if stat.S_ISLNK(node.info.st_mode):
            raise OSError("nofollow")
        if flags & os.O_DIRECTORY and not stat.S_ISDIR(node.info.st_mode):
            raise OSError("not_directory")
        fd, self.next_fd = self.next_fd, self.next_fd + 1
        self.fds[fd] = [node, 0]
        return fd

    def fstat(self, fd):
        return copy.copy(self.fds[fd][0].info)

    def stat(self, name, *, dir_fd, follow_symlinks):
        assert follow_symlinks is False
        return copy.copy(self.fds[dir_fd][0].children[name].info)

    def read(self, fd, size):
        node, offset = self.fds[fd]
        data = node.data[offset:offset + min(size, self.chunk)]
        self.fds[fd][1] += len(data)
        return data

    def listdir(self, fd):
        return list(self.fds[fd][0].children)

    def close(self, fd):
        del self.fds[fd]


class RefusalPureTests(unittest.TestCase):
    def test_real_closed_failed_bytes_never_return_conformance_or_cleanup_success(self):
        runtime = MemoryRuntime()
        report, code = probe.run_refusal(runtime.argv, runtime)
        self.assertEqual((report, code), ({"execution": "FAIL", "journal": "FAILED_VALID",
                                         "reason": "infrastructure_refusal"}, 2))
        self.assertEqual(runtime.opens, 1)
        records = [json.loads(line)["evidence"] for line in
                   runtime.files["journal/04-entrypoint.jsonl"].splitlines()]
        self.assertEqual(records[1]["payload"]["launcher_exit_declared"], 0)
        self.assertEqual(records[1]["payload"]["confirmation_exit_declared"], 0)
        self.assertFalse(records[1]["payload"]["conformance_executed"])
        self.assertEqual(records[-1]["payload"], {"result": "FAILED", "code": "infrastructure_refusal"})

    def test_cli_rejects_missing_duplicate_unknown_and_non_string_options(self):
        good = MemoryRuntime().argv
        cases = [good[:-2], good + ["--force", "1"], ["--unknown"] + good[1:],
                 [good[2]] + good[1:], [True] + good[1:]]
        for argv in cases:
            with self.subTest(argv_shape=len(argv)), self.assertRaises(probe.RefusalError):
                probe.parse_cli(argv)

    def test_cli_rejects_bad_public_declarations(self):
        values = {"--job-root": ["/", "relative", "//job", "/a/../b", "/a/", "/a\n"],
                  "--core-sha256": ["a" * 63, "A" * 64, "z" * 64],
                  "--prefix-digests": ["a" * 64, ",".join(["a" * 64] * 5)],
                  "--launcher-exit": ["1", "false", "00"],
                  "--confirmation-exit": ["1", "false", "00"],
                  "--probe": ["scaffold", "pytest", "refusal-cleanup --force"],
                  "--deadline-monotonic-ns": ["0", "-1", "1.5", "01", str(1 << 63)]}
        for option, bads in values.items():
            for bad in bads:
                argv = MemoryRuntime().argv
                argv[argv.index(option) + 1] = bad
                with self.subTest(option=option, category="invalid"), self.assertRaises(probe.RefusalError):
                    probe.parse_cli(argv)

    def test_no_open_for_secret_environment_core_or_boot_mismatch(self):
        for kind in ("env", "core", "schema", "boot"):
            r = MemoryRuntime()
            if kind == "env":
                r.environment += ("SECRET_KEY",)
            elif kind == "core":
                r.files["core-identity.json"] += b" "
            else:
                core = copy.deepcopy(r.core)
                core["schema" if kind == "schema" else "boot_id"] = "wrong"
                r.files["core-identity.json"] = json.dumps(core).encode()
                r.argv[r.argv.index("--core-sha256") + 1] = digest(r.files["core-identity.json"])
            with self.subTest(kind=kind), self.assertRaises(probe.RefusalError):
                probe.run_refusal(r.argv, r)
            self.assertEqual(r.opens, 0)

    def test_each_changed_prefix_refuses_before_open(self):
        for index, name in enumerate(probe.STAGES):
            r = MemoryRuntime()
            r.files["journal/%02d-%s.jsonl" % (index, name)] += b" "
            with self.subTest(index=index), self.assertRaises(probe.RefusalError):
                probe.run_refusal(r.argv, r)
            self.assertEqual(r.opens, 0)

    def test_original_malformed_prefix_is_rejected_by_actual_pure_parser(self):
        r = MemoryRuntime()
        path = "journal/03-launch-confirm.jsonl"
        data = r.files[path][:-1]
        r.files[path] = data
        digests = r.argv[5].split(",")
        digests[3] = digest(data)  # Faulty original producer; syntax still must fail.
        r.argv[5] = ",".join(digests)
        with self.assertRaises(sj.JournalError):
            probe.run_refusal(r.argv, r)
        self.assertEqual(r.opens, 0)

    def test_extra_or_existing_entrypoint_journal_refuses_before_open(self):
        r = MemoryRuntime()
        r.files["journal/04-entrypoint.jsonl"] = b"already exists"
        with self.assertRaises(probe.RefusalError):
            probe.run_refusal(r.argv, r)
        self.assertEqual(r.opens, 0)

    def test_deadline_boundary_and_invalid_clock_refuse_before_open(self):
        for now in (99, 2000, 2001, True, None):
            r = MemoryRuntime()
            r.clock = now
            with self.subTest(now_type=type(now).__name__), self.assertRaises(probe.RefusalError):
                probe.run_refusal(r.argv, r)
            self.assertEqual(r.opens, 0)

    def test_ancestor_change_before_open_or_during_stage_never_returns_f1(self):
        for check in (1, 2, 3, 4, 5, 6):
            r = MemoryRuntime()
            r.change_at = check
            with self.subTest(check=check), self.assertRaises(probe.RefusalError):
                probe.run_refusal(r.argv, r)
            if check <= 2:
                self.assertEqual(r.opens, 0)

    def test_write_close_and_interrupt_failures_never_return_ordinary_refusal(self):
        for failure, error in [("enter", OSError), ("record", OSError),
                               ("close", OSError), ("missing-terminal", sj.JournalError),
                               ("interrupt", KeyboardInterrupt)]:
            r = MemoryRuntime()
            r.failure = failure
            with self.subTest(failure=failure), self.assertRaises(error):
                probe.run_refusal(r.argv, r)

    def test_main_redacts_unexpected_errors_and_preserves_unknown_close_or_interrupt(self):
        for failure, code, reason in [("record", 75, "runtime_error"),
                                      ("descriptor-close", 75, "runtime_error"),
                                      ("interrupt", 130, "interrupted")]:
            r = MemoryRuntime()
            r.failure = failure
            original = probe.FileRuntime
            probe.FileRuntime = lambda: r  # Entire adapter replaced; no real constructor.
            try:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    actual = probe.main(r.argv)
                with self.subTest(failure=failure):
                    self.assertEqual(actual, code)
                    self.assertEqual(json.loads(output.getvalue()), {
                        "execution": "FAIL", "journal": "UNPROVEN", "reason": reason})
                    self.assertNotIn("private", output.getvalue())
            finally:
                probe.FileRuntime = original

    def test_held_reader_short_reads_reach_eof_with_all_descriptors_closed(self):
        fake, original = MemoryOS(), probe.os
        probe.os = fake
        try:
            reader = probe.HeldReader("/synthetic/job")
            self.assertEqual(reader.read("core-identity.json", 100), b"public core bytes")
            self.assertEqual(len(reader.leaves), 1)
            reader.close()
            self.assertFalse(fake.fds)
        finally:
            probe.os = original

    def test_held_reader_detects_replaced_ancestor_leaf_and_in_place_change(self):
        for change in ("ancestor", "leaf", "mtime", "size", "mode", "uid", "links"):
            fake, original = MemoryOS(), probe.os
            probe.os = fake
            try:
                reader = probe.HeldReader("/synthetic/job")
                reader.read("core-identity.json", 100)
                leaf = fake.nodes["job"].children["core-identity.json"]
                if change == "ancestor":
                    fake.nodes["synthetic"].children["job"] = fake.node(stat.S_IFDIR | 0o700)
                elif change == "leaf":
                    fake.nodes["job"].children["core-identity.json"] = fake.node(stat.S_IFREG | 0o600, leaf.data)
                else:
                    attribute = {"mtime": "st_mtime_ns", "size": "st_size", "mode": "st_mode",
                                 "uid": "st_uid", "links": "st_nlink"}[change]
                    setattr(leaf.info, attribute, getattr(leaf.info, attribute) + 1)
                with self.subTest(change=change), self.assertRaises(probe.RefusalError):
                    reader.check()
                reader.close()
                self.assertFalse(fake.fds)
            finally:
                probe.os = original

    def test_held_reader_refuses_nonprivate_hardlinked_or_oversize_files(self):
        for attribute, value in [("st_mode", stat.S_IFREG | 0o644), ("st_uid", 999),
                                 ("st_nlink", 2), ("st_size", 101),
                                 ("st_mode", stat.S_IFIFO | 0o600)]:
            fake, original = MemoryOS(), probe.os
            probe.os = fake
            try:
                leaf = fake.nodes["job"].children["core-identity.json"]
                setattr(leaf.info, attribute, value)
                reader = probe.HeldReader("/synthetic/job")
                with self.subTest(attribute=attribute, value=value), self.assertRaises(probe.RefusalError):
                    reader.read("core-identity.json", 100)
                reader.close()
                self.assertFalse(fake.fds)
            finally:
                probe.os = original

    def test_held_reader_constructor_failure_closes_all_opened_descriptors(self):
        fake, original = MemoryOS(), probe.os
        fake.nodes["job"].info.st_mode = stat.S_IFDIR | 0o755
        probe.os = fake
        try:
            with self.assertRaises(probe.RefusalError):
                probe.HeldReader("/synthetic/job")
            self.assertFalse(fake.fds)
        finally:
            probe.os = original


if __name__ == "__main__":
    unittest.main()
