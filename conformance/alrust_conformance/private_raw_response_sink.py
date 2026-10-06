"""Standalone private custody primitive; NOT wired into an HTTP client.

Does not implement the old PrivateResponseSink interface: that interface
charges in the client, whereas this component owns charge and write together.
Injection into that client must refuse until a freshly reviewed integration.
"""
import os
import stat
import threading

from .bounded_capture_client import RawBudget, _positive
from .private_secret_writer import PrivateSecretWriter
from . import stage_journal as sj


class RawSinkError(ValueError):
    """Fixed diagnostics only; response bytes and exception details are private."""


class PrivateRawResponseSink:
    """One actual writer/stage/core and fresh budget, with explicit close.

    Limits are supplied by the execution plan, never inferred or reset. No
    hostile same-process/UID boundary is claimed. Every direct write charges
    once before bytes; failed or partial writes never refund the budget.
    """
    def __init__(self, *, writer, stage, job_root, core_sha256, budget,
                 max_response_bytes):
        self.writer, self._stage = writer, stage
        self._root, self._core = job_root, core_sha256
        self._budget, self._limit = budget, max_response_bytes
        self._pid, self._thread = os.getpid(), threading.get_ident()
        self._root_fd = self._fd = self._identity = self._name = None
        self._root_identity = None
        self._used = self._responses = self._response_bytes = 0
        self._failed = self._closed = False
        self._client = None
        try:
            self._require(type(writer) is PrivateSecretWriter
                          and type(stage) is sj.StageJournal
                          and type(budget) is RawBudget
                          and _positive(max_response_bytes)
                          and 1 <= budget.max_responses <= 999999
                          and budget.responses == 0 and budget.used_bytes == 0,
                          "raw_sink_binding_invalid")
            self._max_bytes, self._max_responses = budget.max_bytes, budget.max_responses
            self._binding()
            writer.register_raw_response_sink(self)
            self._root_fd = os.open(job_root, os.O_RDONLY | os.O_DIRECTORY
                                    | os.O_NOFOLLOW | os.O_CLOEXEC)
            self._root_identity = self._id(os.fstat(self._root_fd))
            self._root_current()
        except BaseException as error:
            self._raise_failure(error)

    def __repr__(self):
        return "<PrivateRawResponseSink private>"

    @staticmethod
    def _id(info):
        return info.st_dev, info.st_ino

    @staticmethod
    def _require(condition, code):
        if not condition:
            raise RawSinkError(code) from None

    def _placement(self):
        # Accident protection for actual checkout/mailbox ancestors, not a
        # security boundary against a process that can change same-UID paths.
        parent = self._root
        while True:
            marker = os.path.join(parent, ".git")
            # The sandbox exposes empty read-only .git placeholders at its
            # writable roots. An empty directory is not a checkout. Refuse
            # gitfiles, links, occupied metadata directories, and bare repos.
            git_marker = (os.path.lexists(marker) and
                          (os.path.islink(marker) or not os.path.isdir(marker)
                           or bool(os.listdir(marker))))
            bare = (os.path.lexists(os.path.join(parent, "HEAD"))
                    and os.path.isdir(os.path.join(parent, "objects"))
                    and os.path.isdir(os.path.join(parent, "refs")))
            self._require(not git_marker and not bare
                          and not os.path.basename(parent).endswith("-mailbox"),
                          "raw_sink_placement_invalid")
            next_parent = os.path.dirname(parent)
            if next_parent == parent:
                break
            parent = next_parent

    def _budget_current(self):
        budget = self._budget
        self._require(type(budget) is RawBudget and not budget._failed
                      and budget.max_bytes == self._max_bytes
                      and budget.max_responses == self._max_responses
                      and budget.used_bytes == self._used
                      and budget.responses == self._responses,
                      "raw_sink_budget_changed")
        budget.charge(0)  # also verifies the budget's process/thread ownership

    def _binding(self):
        self._require(not self._failed and not self._closed
                      and os.getpid() == self._pid
                      and threading.get_ident() == self._thread,
                      "raw_sink_unavailable")
        self.writer.check_stage_binding(self._stage, self._root, self._core)
        self._placement()
        self._budget_current()

    def _root_current(self):
        current = os.stat(self._root, follow_symlinks=False)
        held = os.fstat(self._root_fd)
        self._require(stat.S_ISDIR(current.st_mode)
                      and self._id(current) == self._root_identity == self._id(held)
                      and current.st_uid == held.st_uid == os.getuid()
                      and stat.S_IMODE(current.st_mode) == stat.S_IMODE(held.st_mode) == 0o700
                      and os.path.realpath(self._root) == self._root,
                      "raw_sink_root_changed")

    def _file_current(self):
        held = os.fstat(self._fd)
        current = os.stat(self._name, dir_fd=self._root_fd, follow_symlinks=False)
        for info in (held, current):
            self._require(stat.S_ISREG(info.st_mode) and self._id(info) == self._identity
                          and stat.S_IMODE(info.st_mode) == 0o600
                          and info.st_uid == os.getuid() and info.st_nlink == 1
                          and info.st_size == self._response_bytes,
                          "raw_sink_file_changed")

    def _close_handles(self):
        for attr in ("_fd", "_root_fd"):
            fd = getattr(self, attr)
            setattr(self, attr, None)  # no ambiguous retry of a failed close
            if fd is not None:
                try:
                    os.close(fd)
                except BaseException:
                    self._failed = True
        self._closed = True

    def abort(self):
        """Retain private files; permanently poison all completion paths."""
        self._failed = True
        if type(self._budget) is RawBudget:
            self._budget.abort()
        if type(self.writer) is PrivateSecretWriter:
            try:
                self.writer.abort()
            except BaseException:
                pass  # writer marks failure before raising; preserve primary
        self._close_handles()

    def _raise_failure(self, error):
        self.abort()
        if not isinstance(error, Exception) or isinstance(error, RawSinkError):
            raise error
        raise RawSinkError("raw_sink_capture_failed") from None

    def begin(self):
        try:
            self._binding()
            self._root_current()
            self._require(self._fd is None, "raw_sink_response_pending")
            self._budget.begin()
            self._responses += 1
            self._response_bytes = 0
            self._name = f"entrypoint-raw-response-{self._responses:06d}.bin"
            self._fd = os.open(self._name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                               | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                               dir_fd=self._root_fd)
            info = os.fstat(self._fd)
            self._identity = self._id(info)
            self._file_current()
            self._stage.record("owned_resource", {
                "path": os.path.join(self._root, self._name),
                "dev": info.st_dev, "ino": info.st_ino, "type": "file"})
            # Directory entry and ownership are durable BEFORE first data.
            os.fsync(self._root_fd)
        except BaseException as error:
            self._raise_failure(error)

    def remaining(self):
        try:
            self._binding()
            self._root_current()
            self._require(self._fd is not None, "raw_sink_response_missing")
            self._file_current()
            return min(self._max_bytes - self._used,
                       self._limit - self._response_bytes)
        except BaseException as error:
            self._raise_failure(error)

    def reserve_and_append(self, data):
        """One charge/write transaction; empty bytes are valid even at cap."""
        try:
            remaining = self.remaining()
            self._require(type(data) is bytes and len(data) <= remaining,
                          "raw_sink_bytes_exceeded")
            self._budget.charge(len(data))
            self._used += len(data)
            view = memoryview(data)
            while view:
                written = os.write(self._fd, view)
                self._require(0 < written <= len(view), "raw_sink_write_no_progress")
                self._response_bytes += written
                view = view[written:]
            self._file_current()
        except BaseException as error:
            self._raise_failure(error)

    def finish(self):
        try:
            self.remaining()
            os.fsync(self._fd)
            self._file_current()
            fd, self._fd = self._fd, None
            os.close(fd)
            self._identity = self._name = None
        except BaseException as error:
            self._raise_failure(error)

    def require_idle(self):
        try:
            self._binding()
            self._root_current()
            self._require(self._fd is None, "raw_sink_response_pending")
        except BaseException as error:
            self._raise_failure(error)

    def register_client(self, client):
        """Bind one exact client generation; no interchangeable callbacks."""
        from .custodied_capture_client import CustodiedCaptureClient
        try:
            self.require_idle()
            self._require(type(client) is CustodiedCaptureClient
                          and self._client is None,
                          "raw_sink_client_binding_invalid")
            self._client = client
        except BaseException as error:
            self._raise_failure(error)

    def check_client_binding(self, client):
        try:
            self._require(client is self._client, "raw_sink_client_changed")
            self.require_idle()
        except BaseException as error:
            self._raise_failure(error)

    def close(self):
        try:
            self.require_idle()
            fd, self._root_fd = self._root_fd, None
            os.close(fd)
            self._closed = True
        except BaseException as error:
            self._raise_failure(error)

    def require_closed(self):
        """Writer finish calls this BEFORE snapshot allocation."""
        try:
            self._require(self._closed and not self._failed and self._fd is None
                          and self._root_fd is None and os.getpid() == self._pid
                          and threading.get_ident() == self._thread,
                          "raw_sink_not_closed")
            self.writer.check_stage_binding(self._stage, self._root, self._core)
            self._placement()
            self._budget_current()
        except BaseException as error:
            self._raise_failure(error)

    def __enter__(self):
        self.require_idle()
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is not None:
            self.abort()
            return False
        self.close()
        return False
