"""Held, read-only venv aliases and executable identities; no execution.

The caller supplies independently authenticated root/venv identities and the
reviewed runtime alias. Actual target identity is observed here, not obtained
from a launch receipt, runtime declaration or /proc/C/exe. Same-user mutation
and executable history between observations are not security guarantees.
"""
from dataclasses import dataclass
import os
import stat
import time


class VenvAuditError(ValueError):
    """Fixed private codes; never include paths, link text or OS diagnostics."""


def _require(value, code):
    if not value:
        raise VenvAuditError(code)


def _path(value):
    return (type(value) is str and 0 < len(value) <= 4096
            and "\x00" not in value and value.startswith("/")
            and not value.startswith("//") and value != "/"
            and os.path.normpath(value) == value
            and len(value.split("/")) <= 128)


def _identity(value):
    return (type(value) is tuple and len(value) == 2
            and all(type(v) is int and 0 <= v < 2**63 for v in value)
            and value[1] > 0)


def _stamp(info):
    # Directory contents may change without replacing the directory. For
    # links and executable files include metadata to detect ordinary edits.
    base = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)
    return base if stat.S_ISDIR(info.st_mode) else base + (
        info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@dataclass(frozen=True)
class VenvExecutableAudit:
    venv_identity: tuple[int, int]
    interpreter_identity: tuple[int, int]
    runtime_alias: str
    observed_ns: int
    scope: str = "VENV_ALIASES_AND_TARGET_ONLY_NOT_PROCESS_OR_REFERENCE_QUALIFICATION"


class VenvExecutableWindow:
    """One-use handles for bin/python, bin/python3 and a pinned runtime alias.

    The private root and venv ancestors must be real directories. Symlinks
    are resolved only while inspecting the reviewed executable aliases; they
    may lead to an external base interpreter. Every traversed directory/link
    and final file is held and rechecked, including named entry replacement.
    Nothing is read from or executed by the target. Normal exit checks again;
    failed caller work closes handles without reporting an audit success.

    deadline_ns is a caller-provided absolute local monotonic deadline at most
    five seconds away on entry. It is never refreshed. Future composition must
    use the original capture deadline; this class makes no capture or admission.
    """

    def __init__(self, job_root, root_identity, venv_path, venv_identity, *,
                 runtime_alias, deadline_ns):
        _require(_path(job_root) and _path(venv_path)
                 and venv_path.startswith(job_root + "/")
                 and _identity(root_identity) and _identity(venv_identity),
                 "expected_identity_invalid")
        aliases = (venv_path + "/bin/python", venv_path + "/bin/python3")
        # Runtime declarations cannot nominate arbitrary new paths to audit.
        _require(type(runtime_alias) is str and runtime_alias in aliases,
                 "expected_alias_invalid")
        _require(type(deadline_ns) is int and 0 < deadline_ns < 2**63,
                 "deadline_invalid")
        self._root_path, self._root_identity = job_root, root_identity
        self._venv_path, self._venv_identity = venv_path, venv_identity
        self._runtime_alias, self._aliases = runtime_alias, aliases
        self._deadline = deadline_ns
        self._fds, self._nodes = [], []
        self._state = "new"
        self._target_identity = None

    def _clock(self):
        now = time.monotonic_ns()
        _require(now < self._deadline, "deadline_expired")
        return now

    def _open(self, name, parent=None):
        fd = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=parent)
        try:
            self._fds.append(fd)
        except BaseException:
            try:
                os.close(fd)
            except BaseException:
                pass  # Preserve the primary failure; never claim cleanup.
            raise
        info = os.fstat(fd)
        self._nodes.append((fd, parent, name, _stamp(info)))
        return fd, info

    def _directory(self, path):
        current, info = self._open("/")
        for name in path[1:].split("/"):
            current, info = self._open(name, current)
            _require(stat.S_ISDIR(info.st_mode), "ancestor_not_directory")
        return current, info

    def _private_directory(self, path, expected):
        fd, info = self._directory(path)
        _require((info.st_dev, info.st_ino) == expected
                 and info.st_uid == os.getuid()
                 and stat.S_IMODE(info.st_mode) == 0o700,
                 "private_directory_mismatch")
        return fd

    def _resolve(self, alias):
        # Explicit bounded link traversal keeps each link itself inspectable.
        # '..' in link targets traverses the held directory, never normpath's
        # lexical shortcut across an earlier symlink.
        current, _ = self._open("/")
        todo = alias[1:].split("/")
        links, steps = 0, 0
        while todo:
            self._clock()
            steps += 1
            _require(steps <= 256, "resolution_limit")
            name = todo.pop(0)
            if not name or name == ".":
                continue
            fd, info = self._open(name, current)
            if stat.S_ISLNK(info.st_mode):
                links += 1
                _require(links <= 40, "resolution_limit")
                # Linux O_PATH+O_NOFOLLOW supports reading the held symlink,
                # avoiding a second name lookup which could read another link.
                target = os.readlink("", dir_fd=fd)
                _require(0 < len(target) <= 4096 and "\x00" not in target,
                         "link_invalid")
                if target.startswith("/"):
                    current, _ = self._open("/")
                todo = target.split("/") + todo
            elif todo:
                _require(stat.S_ISDIR(info.st_mode), "target_ancestor_invalid")
                current = fd
            else:
                _require(stat.S_ISREG(info.st_mode)
                         and info.st_mode & 0o111, "target_not_executable_file")
                return info.st_dev, info.st_ino
        raise VenvAuditError("target_invalid")

    def _close(self):
        self._state = "closed"
        fds, self._fds = self._fds, []
        self._nodes.clear()
        first = None
        for fd in reversed(fds):
            try:
                os.close(fd)
            except BaseException as error:
                if first is None:
                    first = error
        if first is not None:
            raise first

    def _failed(self, error):
        try:
            self._close()
        except BaseException:
            pass  # Preserve a setup/check/caller failure over secondary close.
        if isinstance(error, OSError):
            raise VenvAuditError("venv_unavailable") from None
        raise error

    def __enter__(self):
        _require(self._state == "new", "window_not_new")
        try:
            now = self._clock()
            _require(self._deadline - now <= 5_000_000_000, "deadline_invalid")
            self._private_directory(self._root_path, self._root_identity)
            self._private_directory(self._venv_path, self._venv_identity)
            self._directory(self._venv_path + "/bin")
            targets = [self._resolve(alias) for alias in self._aliases]
            _require(targets[0] == targets[1], "alias_target_mismatch")
            self._target_identity = targets[0]
            self._state = "active"
            self.check()
            return self
        except BaseException as error:
            self._failed(error)

    def check(self):
        _require(self._state == "active", "window_not_active")
        try:
            self._clock()
            for fd, parent, name, stamp in self._nodes:
                _require(_stamp(os.fstat(fd)) == stamp
                         and _stamp(os.stat(name, dir_fd=parent,
                                            follow_symlinks=False)) == stamp,
                         "path_changed")
            return VenvExecutableAudit(self._venv_identity, self._target_identity,
                                       self._runtime_alias, self._clock())
        except BaseException as error:
            self._failed(error)

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is not None:
            try:
                self._close()
            except BaseException:
                pass  # The caller's failure remains the primary result.
            return False
        try:
            self.check()
        except BaseException as error:
            self._failed(error)
        try:
            self._close()
        except OSError:
            raise VenvAuditError("descriptor_close_failed") from None
        return False
