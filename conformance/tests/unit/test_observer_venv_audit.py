"""Synthetic executable files and aliases only; no interpreter is executed."""
import os
from pathlib import Path
import time

import pytest

from alrust_conformance import observer_venv_audit as va


@pytest.fixture
def layout(tmp_path):
    root = tmp_path / "job"
    root.mkdir(mode=0o700)
    venv = root / "venv"
    venv.mkdir(mode=0o700)
    bin_dir = venv / "bin"
    bin_dir.mkdir()
    base = tmp_path / "base"
    base.mkdir()
    executable = base / "synthetic-python"
    executable.write_bytes(b"SYNTHETIC-NOT-AN-INTERPRETER\n")
    executable.chmod(0o755)
    (bin_dir / "python3").symlink_to(executable)
    (bin_dir / "python").symlink_to("python3")
    return root, venv, executable


def identity(path):
    s = path.stat()
    return s.st_dev, s.st_ino


def window(layout, **changes):
    root, venv, _ = layout
    args = dict(job_root=str(root), root_identity=identity(root),
                venv_path=str(venv), venv_identity=identity(venv),
                runtime_alias=str(venv / "bin/python"),
                deadline_ns=time.monotonic_ns() + 5_000_000_000)
    args.update(changes)
    return va.VenvExecutableWindow(**args)


@pytest.mark.parametrize("alias", ["python", "python3"])
def test_external_target_and_different_alias_strings(layout, alias):
    _, venv, target = layout
    with window(layout, runtime_alias=str(venv / "bin" / alias)) as held:
        audit = held.check()
        assert audit.interpreter_identity == identity(target)
        assert audit.venv_identity == identity(venv)
        assert audit.runtime_alias == str(venv / "bin" / alias)
        assert audit.scope == "VENV_ALIASES_AND_TARGET_ONLY_NOT_PROCESS_OR_REFERENCE_QUALIFICATION"
        assert target.read_bytes() == b"SYNTHETIC-NOT-AN-INTERPRETER\n"
    with pytest.raises(va.VenvAuditError, match="window_not_active"):
        held.check()
    with pytest.raises(va.VenvAuditError, match="window_not_new"):
        held.__enter__()


def test_alias_linked_target_directory_with_parent_component(layout):
    # 'link/../target' follows link first. Lexical normpath would select a
    # different target under base and is therefore insufficient.
    _, venv, target = layout
    (target.parent / "other").mkdir()
    (target.parent / "other/deep").mkdir()
    (target.parent / "other/synthetic-python").write_bytes(b"different")
    (target.parent / "other/synthetic-python").chmod(0o755)
    (target.parent / "link").symlink_to("other/deep")
    p = venv / "bin/python3"
    p.unlink()
    p.symlink_to(target.parent / "link/../synthetic-python")
    with window(layout) as held:
        assert held.check().interpreter_identity == identity(target.parent / "other/synthetic-python")


@pytest.mark.parametrize("where", ["root", "venv", "bin", "alias", "base_target", "target_content", "base_directory"])
def test_replacement_or_edit_during_held_window_refused(layout, where):
    root, venv, target = layout
    with pytest.raises(va.VenvAuditError, match="path_changed"):
        with window(layout):
            if where in ("root", "venv", "bin", "base_directory"):
                p = {"root": root, "venv": venv, "bin": venv / "bin",
                     "base_directory": target.parent}[where]
                p.rename(p.with_name(p.name + "-old"))
                p.mkdir(mode=0o700)
            elif where == "alias":
                p = venv / "bin/python"
                p.unlink()
                p.symlink_to("python3")  # Same target but a different alias generation.
            elif where == "base_target":
                target.rename(target.with_name("old-target"))
                target.write_bytes(b"replacement")
                target.chmod(0o755)
            else:
                target.write_bytes(b"changed executable metadata")


@pytest.mark.parametrize("where", ["root", "venv", "bin"])
def test_symlinked_layout_directory_refused(layout, where):
    root, venv, _ = layout
    p = {"root": root, "venv": venv, "bin": venv / "bin"}[where]
    before = window(layout)
    old = p.with_name(p.name + "-old")
    p.rename(old)
    p.symlink_to(old, target_is_directory=True)
    with pytest.raises(va.VenvAuditError, match="ancestor_not_directory"):
        before.__enter__()


@pytest.mark.parametrize("kind", ["fifo", "directory", "non_executable", "cycle", "mismatch", "missing"])
def test_invalid_or_disagreeing_targets(layout, kind):
    _, venv, target = layout
    p = venv / "bin/python3"
    if kind == "non_executable":
        target.chmod(0o600)
    elif kind == "mismatch":
        p = venv / "bin/python"
        p.unlink()
        p.write_bytes(b"SYNTHETIC-DIFFERENT")
        p.chmod(0o755)
    else:
        p.unlink()
        if kind == "fifo":
            os.mkfifo(p)
        elif kind == "directory":
            p.mkdir()
        elif kind == "cycle":
            p.symlink_to("python")
    with pytest.raises(va.VenvAuditError):
        window(layout).__enter__()


@pytest.mark.parametrize("changes", [
    {"root_identity": (False, 1)}, {"venv_identity": (0, True)},
    {"root_identity": (0, 1)}, {"venv_identity": (0, 1)},
    {"deadline_ns": True}, {"deadline_ns": 0},
    {"runtime_alias": "/usr/bin/python3"},
])
def test_bad_independent_inputs(layout, changes):
    with pytest.raises(va.VenvAuditError):
        window(layout, **changes).__enter__()


@pytest.mark.parametrize("directory", ["root", "venv"])
def test_private_directory_mode(layout, directory):
    root, venv, _ = layout
    {"root": root, "venv": venv}[directory].chmod(0o755)
    with pytest.raises(va.VenvAuditError, match="private_directory_mismatch"):
        window(layout).__enter__()


def test_foreign_venv_owner_synthetic(layout, monkeypatch):
    _, venv, _ = layout
    wanted = identity(venv)
    original = os.fstat
    def foreign(fd):
        s = original(fd)
        if (s.st_dev, s.st_ino) == wanted:
            fields = list(s)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        return s
    monkeypatch.setattr(va.os, "fstat", foreign)
    with pytest.raises(va.VenvAuditError, match="private_directory_mismatch"):
        window(layout).__enter__()


def test_deadline_not_refreshed(layout, monkeypatch):
    clock = [1_000_000_000]
    monkeypatch.setattr(va.time, "monotonic_ns", lambda: clock[0])
    w = window(layout, deadline_ns=2_000_000_000)
    w.__enter__()
    clock[0] = 2_000_000_000
    with pytest.raises(va.VenvAuditError, match="deadline_expired"):
        w.check()
    assert w._state == "closed" and not w._fds


def test_deadline_too_far(layout):
    with pytest.raises(va.VenvAuditError, match="deadline_invalid"):
        window(layout, deadline_ns=time.monotonic_ns() + 10_000_000_000).__enter__()


def test_replacement_during_setup_detected_by_final_check(layout, monkeypatch):
    _, venv, _ = layout
    original = va.VenvExecutableWindow._resolve
    calls = []
    def replace_after_resolving(self, alias):
        result = original(self, alias)
        calls.append(alias)
        if len(calls) == 2:
            p = venv / "bin/python"
            p.unlink()
            p.symlink_to("python3")
        return result
    monkeypatch.setattr(va.VenvExecutableWindow, "_resolve", replace_after_resolving)
    w = window(layout)
    with pytest.raises(va.VenvAuditError, match="path_changed"):
        w.__enter__()
    assert not w._fds and w._state == "closed"


def test_final_deadline_check_required(layout, monkeypatch):
    now = [1_000_000_000]
    monkeypatch.setattr(va.time, "monotonic_ns", lambda: now[0])
    w = window(layout, deadline_ns=2_000_000_000)
    w.__enter__()
    original = va.os.stat
    def expires_after_metadata(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] = 2_000_000_000
        return result
    monkeypatch.setattr(va.os, "stat", expires_after_metadata)
    with pytest.raises(va.VenvAuditError, match="deadline_expired"):
        w.check()
    assert not w._fds and w._state == "closed"


def test_unexpected_check_failure_closes_and_preserves_primary(layout, monkeypatch):
    w = window(layout)
    w.__enter__()
    owned = list(w._fds)
    original_fstat, original_close = va.os.fstat, va.os.close
    def fail_stat(fd):
        raise RuntimeError("primary")
    def fail_secondary(fd):
        original_close(fd)
        raise RuntimeError("secondary")
    monkeypatch.setattr(va.os, "fstat", fail_stat)
    monkeypatch.setattr(va.os, "close", fail_secondary)
    with pytest.raises(RuntimeError, match="primary"):
        w.check()
    assert not w._fds and w._state == "closed"
    for fd in owned:
        with pytest.raises(OSError):
            original_fstat(fd)


@pytest.mark.parametrize("error", [RuntimeError("injected"), KeyboardInterrupt(), OSError("private-detail")])
def test_setup_failure_closes_every_owned_handle(layout, monkeypatch, error):
    w = window(layout)
    original = va.os.fstat
    opened = []
    original_open = va.os.open
    def spy_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd
    def fail_fourth(fd):
        if len(opened) == 4:
            raise error
        return original(fd)
    monkeypatch.setattr(va.os, "open", spy_open)
    monkeypatch.setattr(va.os, "fstat", fail_fourth)
    expected = va.VenvAuditError if isinstance(error, OSError) else type(error)
    with pytest.raises(expected):
        w.__enter__()
    assert w._state == "closed" and not w._fds
    for fd in opened:
        with pytest.raises(OSError):
            original(fd)


def test_normal_exit_close_failure_reported(layout, monkeypatch):
    w = window(layout)
    w.__enter__()
    original = va.os.close
    def close_and_fail(fd):
        original(fd)
        raise OSError("private-detail")
    monkeypatch.setattr(va.os, "close", close_and_fail)
    with pytest.raises(va.VenvAuditError, match="descriptor_close_failed"):
        w.__exit__(None, None, None)
    assert not w._fds and w._state == "closed"


def test_caller_failure_not_masked_by_cleanup(layout, monkeypatch):
    original = va.os.close
    def close_and_fail(fd):
        original(fd)
        raise RuntimeError("secondary")
    with pytest.raises(KeyboardInterrupt, match="primary"):
        with window(layout):
            monkeypatch.setattr(va.os, "close", close_and_fail)
            raise KeyboardInterrupt("primary")
