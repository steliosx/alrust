"""Durable private Q1 discovery writer; no provisioning, HTTP, SQL or export.

Use inside the executing entrypoint's open StageJournal. Both files stay
private, including after any failure. A completed inventory is recorded only
on a successful writer context exit; file existence alone proves nothing.
"""
import hashlib
import json
import os
import stat
import threading

from . import stage_journal as sj
from .private_secret_inventory import InventoryError, encode_inventory

EVENTS_NAME = "entrypoint-secret-events.jsonl"
SNAPSHOT_NAME = "entrypoint-secrets.json"
EVENTS_SCHEMA = "alrust-entrypoint-secret-events/1"


class SecretWriterError(ValueError):
    """Fixed diagnostics only; never include secrets or exception messages."""


class PrivateSecretWriter:
    """Single-process/single-thread capture under an independently pinned core.

    Explicit finish() is mandatory, including a no-discovery profile. No
    successful publication or writer-generation qualification is implied.
    The reviewed executor must not suppress this context's failures and must
    separately prove that all discovery paths and child writers are complete.
    """

    def __init__(self, job_root, core_sha256, stage):
        self._root = job_root
        self._core_sha256 = core_sha256
        self._stage = stage
        self._root_fd = self._events_fd = None
        self._root_identity = self._events_identity = None
        self._snapshot = None
        self._values = []
        self._failed = None
        self._entered = self._finished = self._closed = False
        self._pid, self._thread = os.getpid(), threading.get_ident()

    def __repr__(self):
        return "<PrivateSecretWriter private>"

    def _refuse(self, code):
        self._failed = self._failed or code
        # StageJournal.fail records only the fixed reason if its context
        # subsequently fails. It is not a substitute for exception propagation.
        if type(self._stage) is sj.StageJournal:
            self._stage.fail(self._failed)
        raise SecretWriterError(self._failed) from None

    def _require(self, condition, code):
        if not condition:
            self._refuse(code)

    def _stage_active(self):
        # StageJournal currently has no public active-state accessor. This
        # explicit source coupling is checked on every action and covered by
        # closed/wrong-stage controls; a future API change needs fresh review.
        stage = self._stage
        self._require(type(stage) is sj.StageJournal
                      and stage._root == self._root and stage._name == "entrypoint"
                      and stage._journal is not None
                      and stage._journal._fd is not None, "secret_stage_not_active")

    def _owner(self):
        self._require(os.getpid() == self._pid
                      and threading.get_ident() == self._thread,
                      "secret_writer_changed")

    @staticmethod
    def _identity(info):
        return info.st_dev, info.st_ino

    def _root_current(self):
        held = os.fstat(self._root_fd)
        current = os.stat(self._root, follow_symlinks=False)
        self._require(stat.S_ISDIR(current.st_mode)
                      and self._identity(current) == self._root_identity
                      and self._identity(held) == self._root_identity
                      and current.st_uid == os.getuid()
                      and stat.S_IMODE(current.st_mode) == 0o700
                      and os.path.realpath(self._root) == self._root,
                      "secret_root_changed")

    def _read_core(self):
        fd = os.open(sj.CORE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                     | os.O_CLOEXEC, dir_fd=self._root_fd)
        try:
            before = os.fstat(fd)
            self._require(stat.S_ISREG(before.st_mode)
                          and before.st_uid == os.getuid()
                          and stat.S_IMODE(before.st_mode) == 0o600
                          and before.st_nlink == 1 and before.st_size <= sj.MAX_CORE,
                          "secret_core_invalid")
            pieces, size = [], 0
            while size <= sj.MAX_CORE:
                chunk = os.read(fd, sj.MAX_CORE + 1 - size)
                if not chunk:
                    break
                pieces.append(chunk)
                size += len(chunk)
            after = os.fstat(fd)
            data = b"".join(pieces)
            self._require(size == before.st_size and size <= sj.MAX_CORE
                          and before.st_size == after.st_size
                          and before.st_mtime_ns == after.st_mtime_ns
                          and before.st_ctime_ns == after.st_ctime_ns,
                          "secret_core_changed")
            self._require(hashlib.sha256(data).hexdigest() == self._core_sha256,
                          "secret_core_mismatch")
            core = sj.validate_core(data, self._root)
            self._require(core["boot_id"] == sj.boot_id(), "secret_core_boot_mismatch")
            # The live stage must have begun with these same core bytes.
            header_bytes = sj.read_bounded(sj.journal_path(
                self._root, self._stage._index, "entrypoint"), sj.MAX_JOURNAL)
            header = sj.strict_json(header_bytes.splitlines()[0], "secret_stage_invalid")
            self._require(header["evidence"]["kind"] == "stage_begin"
                          and header["evidence"]["payload"]["core_identity_sha256"]
                          == self._core_sha256, "secret_stage_core_mismatch")
        finally:
            os.close(fd)

    def _write(self, fd, data):
        view = memoryview(data)
        while view:
            size = os.write(fd, view)
            self._require(size > 0, "secret_write_no_progress")
            view = view[size:]
        os.fsync(fd)

    def _record_owned(self, name, info):
        self._stage.record("owned_resource", {
            "path": os.path.join(self._root, name), "dev": info.st_dev,
            "ino": info.st_ino, "type": "file"})

    def _create(self, name):
        return os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                       | os.O_CLOEXEC, 0o600, dir_fd=self._root_fd)

    def _events_current(self):
        info = os.stat(EVENTS_NAME, dir_fd=self._root_fd, follow_symlinks=False)
        self._require(stat.S_ISREG(info.st_mode)
                      and self._identity(info) == self._events_identity
                      and info.st_uid == os.getuid() and info.st_nlink == 1
                      and stat.S_IMODE(info.st_mode) == 0o600,
                      "secret_events_changed")

    def __enter__(self):
        self._owner()
        self._require(not self._entered and not self._closed, "secret_writer_reused")
        try:
            self._stage_active()
            self._require(type(self._root) is str and os.path.isabs(self._root)
                          and os.path.normpath(self._root) == self._root
                          and os.path.realpath(self._root) == self._root,
                          "secret_root_invalid")
            # Validate the typed core pin before allocating a file.
            encode_inventory(self._core_sha256, [])
            self._root_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY
                                    | os.O_NOFOLLOW | os.O_CLOEXEC)
            self._root_identity = self._identity(os.fstat(self._root_fd))
            self._root_current()
            self._read_core()
            self._events_fd = self._create(EVENTS_NAME)
            info = os.fstat(self._events_fd)
            self._events_identity = self._identity(info)
            self._record_owned(EVENTS_NAME, info)
            os.fsync(self._root_fd)
            self._write(self._events_fd, (json.dumps({
                "schema": EVENTS_SCHEMA, "sequence": 0,
                "core_identity_sha256": self._core_sha256}, sort_keys=True) + "\n").encode())
            self._entered = True
            return self
        except BaseException as error:
            self._close_handles()
            if isinstance(error, SecretWriterError):
                raise
            self._refuse("secret_setup_failed")

    def remember(self, secret):
        """Persist each distinct discovery before the caller uses it again."""
        self._owner()
        self._require(self._entered and not self._finished and not self._closed
                      and self._failed is None, "secret_writer_not_collecting")
        try:
            self._stage_active()
            self._root_current()
            self._events_current()
            # Validate even repeated input, without admitting a new allocation.
            encode_inventory(self._core_sha256, [secret])
            if secret in self._values:
                return
            prospective = self._values + [secret]
            encode_inventory(self._core_sha256, prospective)
            data = (json.dumps({"sequence": len(prospective), "secret": secret},
                               sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")
            self._write(self._events_fd, data)
            self._values.append(secret)
        except SecretWriterError:
            raise
        except (OSError, InventoryError, sj.JournalError, ValueError):
            self._refuse("secret_capture_failed")

    def check_collecting(self):
        """Check custody/lifetime before an operation that might obtain secrets.

        This does not prove profile completeness or writer-generation exit.
        No environment/path lookup chooses this already-entered writer.
        """
        self._owner()
        self._require(self._entered and not self._finished and not self._closed
                      and self._failed is None, "secret_writer_not_collecting")
        try:
            self._stage_active()
            self._root_current()
            self._events_current()
        except (OSError, sj.JournalError, ValueError):
            self._refuse("secret_capture_failed")

    def check_binding(self, job_root, core_sha256):
        """Check an explicit caller pin against this live writer generation.

        The root and exact core bytes must still match the active entrypoint.
        This is custody checking, not proof of child exit or profile coverage.
        """
        self.check_collecting()
        self._require(type(job_root) is str and job_root == self._root
                      and type(core_sha256) is str
                      and core_sha256 == self._core_sha256,
                      "secret_binding_mismatch")
        try:
            self._read_core()
        except (OSError, sj.JournalError, ValueError):
            self._refuse("secret_binding_invalid")

    def abort(self):
        """Permanently refuse completion after a capture/oracle failure."""
        self._owner()
        self._refuse("secret_capture_aborted")

    def finish(self):
        """Freeze private bytes; completion is recorded later on clean exit."""
        self._owner()
        self._require(self._entered and not self._finished and not self._closed
                      and self._failed is None, "secret_writer_not_collecting")
        fd = None
        try:
            self._stage_active()
            self._root_current()
            self._events_current()
            data = encode_inventory(self._core_sha256, list(self._values))
            fd = self._create(SNAPSHOT_NAME)
            info = os.fstat(fd)
            self._record_owned(SNAPSHOT_NAME, info)
            os.fsync(self._root_fd)
            self._write(fd, data)
            closing_fd, fd = fd, None
            os.close(closing_fd)
            self._snapshot = {"path": os.path.join(self._root, SNAPSHOT_NAME),
                              "dev": info.st_dev, "ino": info.st_ino,
                              "sha256": hashlib.sha256(data).hexdigest(),
                              "count": len(self._values)}
            self._finished = True
        except SecretWriterError:
            raise
        except (OSError, InventoryError, sj.JournalError, ValueError):
            self._refuse("secret_snapshot_failed")
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    self._failed = self._failed or "secret_close_failed"

    def _close_handles(self, *, preserve_primary=False):
        for name in ("_events_fd", "_root_fd"):
            fd = getattr(self, name)
            setattr(self, name, None)
            if fd is not None:
                try:
                    os.close(fd)
                except BaseException as error:
                    self._failed = self._failed or "secret_close_failed"
                    if not preserve_primary and not isinstance(error, OSError):
                        raise
        self._closed = True

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is not None:
            self._failed = self._failed or "secret_writer_body_failed"
            # Preserve the caller's exact primary exception, including
            # interrupts. Secondary cleanup/recording failures cannot turn
            # it into a catchable ValueError or permit completion.
            self._close_handles(preserve_primary=True)
            self._values.clear()
            try:
                if type(self._stage) is sj.StageJournal:
                    self._stage.fail(self._failed)
            except BaseException:
                # The surrounding stage still receives the primary exception.
                # No completion/export claim follows a failed failure record.
                pass
            return False
        try:
            if self._failed is None:
                self._owner()
                self._stage_active()
                self._require(self._finished, "secret_snapshot_not_finished")
                self._root_current()
                self._events_current()
                self._read_core()
                data = sj.read_bounded(self._snapshot["path"], 1 << 20)
                info = os.stat(SNAPSHOT_NAME, dir_fd=self._root_fd, follow_symlinks=False)
                self._require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                              and info.st_uid == os.getuid()
                              and stat.S_IMODE(info.st_mode) == 0o600
                              and self._identity(info) == (self._snapshot["dev"],
                                                          self._snapshot["ino"])
                              and hashlib.sha256(data).hexdigest() == self._snapshot["sha256"],
                              "secret_snapshot_changed")
        except (OSError, sj.JournalError, ValueError, KeyError, IndexError, TypeError):
            self._failed = self._failed or "secret_exit_validation_failed"
        finally:
            self._close_handles()
            self._values.clear()
        if self._failed is not None:
            self._refuse(self._failed)
        try:
            self._stage.record("private_secret_inventory", self._snapshot)
        except (OSError, ValueError):
            self._refuse("secret_completion_record_failed")
        return False
