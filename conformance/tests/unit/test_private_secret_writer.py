"""Private temporary files and journals only; no server, SQL, process or HTTP."""
import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from alrust_conformance import private_secret_inventory as inventory
from alrust_conformance import private_secret_writer as writer
from alrust_conformance import stage_journal as sj


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(sj, "EXTERNAL_CONFIG", str(tmp_path / "absent.conf"))
    root = tmp_path / "job"
    root.mkdir(mode=0o700)
    sha = "a" * 64
    core = sj.write_core_identity(
        str(root), profile="scaffold", event_head_sha="b" * 40,
        checkout_sha="b" * 40, reference_sha="c" * 40, reference_pin="c" * 40,
        lock_sha256={"lock": sha}, lock_pins={"lock": sha},
        manifest_sha256=sha, manifest_pin=sha,
        env={"GITHUB_REPOSITORY": "steliosx/alrust", "GITHUB_RUN_ID": "123",
             "GITHUB_RUN_ATTEMPT": "1", "GITHUB_JOB": "profile",
             "ImageOS": "ubuntu24", "ImageVersion": "20261001.1"})
    sj.prepare_journal_dir(str(root))
    return root, core


def records(root):
    return [json.loads(line)["evidence"] for line in
            Path(sj.journal_path(str(root), 0, "entrypoint")).read_bytes().splitlines()]


def completion(root):
    return [r["payload"] for r in records(root) if r["kind"] == "private_secret_inventory"]


def stage(root, name="entrypoint"):
    return sj.StageJournal(str(root), 0, name)


def test_success_retains_revoked_and_repeated_discoveries_and_closes_handles(job):
    root, core = job
    values = ["revoked-" + "x" * 24, "active-" + "y" * 24]
    with stage(root) as journal:
        with writer.PrivateSecretWriter(str(root), core, journal) as capture:
            fds = [capture._root_fd, capture._events_fd]
            capture.remember(values[0])
            capture.remember(values[0])
            capture.remember(values[1])
            capture.finish()
            assert completion(root) == []  # existence/finish alone is not completion
        assert capture._values == [] and capture._closed
        for fd in fds:
            with pytest.raises(OSError):
                os.fstat(fd)
    data = (root / writer.SNAPSHOT_NAME).read_bytes()
    assert inventory.decode_inventory(data, expected_core_sha256=core, expected_count=2) == values
    assert completion(root)[0]["sha256"] == hashlib.sha256(data).hexdigest()
    events = [json.loads(line) for line in (root / writer.EVENTS_NAME).read_bytes().splitlines()]
    assert events[0] == {"schema": writer.EVENTS_SCHEMA, "sequence": 0,
                         "core_identity_sha256": core}
    assert events[1:] == [{"sequence": 1, "secret": values[0]},
                         {"sequence": 2, "secret": values[1]}]
    owned = [r["payload"] for r in records(root) if r["kind"] == "owned_resource"]
    assert len(owned) == 2
    for resource in owned:
        info = Path(resource["path"]).stat()
        assert (resource["dev"], resource["ino"]) == (info.st_dev, info.st_ino)
        assert info.st_mode & 0o777 == 0o600 and info.st_nlink == 1
    journal_bytes = Path(sj.journal_path(str(root), 0, "entrypoint")).read_bytes()
    assert all(value.encode() not in journal_bytes for value in values)
    assert sj.aggregate(str(root), ["entrypoint"])["verdict"] == "VALID"


def test_explicit_empty_snapshot_and_completion_after_handles_close(job, monkeypatch):
    root, core = job
    with stage(root) as journal:
        original = journal.record
        with writer.PrivateSecretWriter(str(root), core, journal) as capture:
            def record(kind, payload):
                if kind == "private_secret_inventory":
                    assert capture._closed and capture._root_fd is None
                    assert capture._events_fd is None
                original(kind, payload)
            monkeypatch.setattr(journal, "record", record)
            capture.finish()
    assert completion(root)[0]["count"] == 0


@pytest.mark.parametrize("phase", ["unfinished", "body_failed", "capture_after_finish",
                                   "finish_twice"])
def test_failed_context_never_commits_completion(job, phase):
    root, core = job
    with pytest.raises(writer.SecretWriterError):
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                capture.remember("x" * 16)
                if phase == "body_failed":
                    raise RuntimeError("untrusted detail")
                if phase in {"capture_after_finish", "finish_twice"}:
                    capture.finish()
                    if phase == "finish_twice":
                        capture.finish()
                    else:
                        capture.remember("y" * 16)
    assert completion(root) == []
    assert (root / writer.EVENTS_NAME).exists()
    assert sj.aggregate(str(root), ["entrypoint"])["verdict"] == "FAILED"


@pytest.mark.parametrize("bad_pin", ["b" * 64, False, "A" * 64])
def test_wrong_core_pin_allocates_no_secret_file(job, bad_pin):
    root, _ = job
    with pytest.raises(writer.SecretWriterError):
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), bad_pin, journal):
                pass
    assert not (root / writer.EVENTS_NAME).exists()
    assert not (root / writer.SNAPSHOT_NAME).exists()


@pytest.mark.parametrize("kind", ["closed", "wrong_name", "wrong_root", "fake"])
def test_writer_requires_this_open_entrypoint_stage(job, tmp_path, kind):
    root, core = job
    if kind == "fake":
        journal = object()
        with pytest.raises(writer.SecretWriterError):
            with writer.PrivateSecretWriter(str(root), core, journal):
                pass
    elif kind == "closed":
        with stage(root) as journal:
            pass
        with pytest.raises(writer.SecretWriterError):
            with writer.PrivateSecretWriter(str(root), core, journal):
                pass
    else:
        with pytest.raises(writer.SecretWriterError):
            with stage(root, "launch" if kind == "wrong_name" else "entrypoint") as journal:
                with writer.PrivateSecretWriter(
                        str(tmp_path) if kind == "wrong_root" else str(root), core, journal):
                    pass
    assert not (root / writer.EVENTS_NAME).exists()


@pytest.mark.parametrize("name", [writer.EVENTS_NAME, writer.SNAPSHOT_NAME])
@pytest.mark.parametrize("kind", ["file", "symlink", "fifo"])
def test_occupied_paths_are_never_adopted_overwritten_or_removed(job, tmp_path, name, kind):
    root, core = job
    target = tmp_path / "foreign"
    target.write_text("keep")
    path = root / name
    if kind == "file":
        path.write_text("keep")
    elif kind == "symlink":
        path.symlink_to(target)
    else:
        os.mkfifo(path)
    inode = path.lstat().st_ino
    with pytest.raises(writer.SecretWriterError):
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                capture.remember("x" * 16)
                capture.finish()
    assert path.lstat().st_ino == inode and target.read_text() == "keep"
    if kind == "file":
        assert path.read_text() == "keep"
    assert completion(root) == []


@pytest.mark.parametrize("kind", ["replace", "mode", "hardlink", "unlink"])
def test_events_replacement_or_alias_refuses_without_completion(job, tmp_path, kind):
    root, core = job
    with pytest.raises(writer.SecretWriterError):
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                path = root / writer.EVENTS_NAME
                if kind == "replace":
                    path.rename(root / "retained-original")
                    path.write_text("keep")
                    path.chmod(0o600)
                elif kind == "mode":
                    path.chmod(0o644)
                elif kind == "hardlink":
                    os.link(path, tmp_path / "alias")
                else:
                    path.unlink()
                capture.remember("x" * 16)
    assert completion(root) == []
    if kind == "replace":
        assert (root / writer.EVENTS_NAME).read_text() == "keep"


def test_root_replacement_is_not_a_destination(job, tmp_path):
    root, core = job
    moved = tmp_path / "old-job"
    with pytest.raises(writer.SecretWriterError):
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                root.rename(moved)
                root.mkdir(mode=0o700)
                capture.remember("x" * 16)
    assert not (root / writer.EVENTS_NAME).exists()
    assert (moved / writer.EVENTS_NAME).exists()
    assert completion(moved) == []


@pytest.mark.parametrize("kind", ["bytes", "replace", "mode", "hardlink"])
def test_snapshot_changed_after_finish_is_not_complete(job, tmp_path, kind):
    root, core = job
    with pytest.raises(writer.SecretWriterError):
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                capture.remember("x" * 16)
                capture.finish()
                path = root / writer.SNAPSHOT_NAME
                if kind == "bytes":
                    path.write_text("{}")
                elif kind == "replace":
                    data = path.read_bytes()
                    path.rename(root / "retained-snapshot")
                    path.write_bytes(data)
                    path.chmod(0o600)
                elif kind == "mode":
                    path.chmod(0o644)
                else:
                    os.link(path, tmp_path / "snapshot-alias")
    assert completion(root) == []


def test_partial_event_write_is_poisoned_even_if_caller_catches_error(job, monkeypatch):
    root, core = job
    with pytest.raises(writer.SecretWriterError, match="^secret_capture_failed$"):
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                capture.remember("x" * 16)
                original = os.write
                partial = []
                def write(fd, data):
                    if fd == capture._events_fd:
                        if not partial:
                            partial.append(True)
                            return original(fd, data[:3])
                        raise OSError("untrusted detail")
                    return original(fd, data)
                monkeypatch.setattr(os, "write", write)
                with pytest.raises(writer.SecretWriterError):
                    capture.remember("y" * 16)
                assert capture._values == ["x" * 16]
    assert completion(root) == []
    assert not (root / writer.SNAPSHOT_NAME).exists()
    assert not (root / writer.EVENTS_NAME).read_bytes().endswith(b"\n")
    assert sj.aggregate(str(root), ["entrypoint"])["verdict"] == "FAILED"


def test_event_fsync_failure_prevents_using_new_discovery(job, monkeypatch):
    root, core = job
    with pytest.raises(writer.SecretWriterError, match="^secret_capture_failed$"):
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                original = os.fsync
                def sync(fd):
                    if fd == capture._events_fd:
                        raise OSError("untrusted detail")
                    return original(fd)
                monkeypatch.setattr(os, "fsync", sync)
                capture.remember("x" * 16)
    assert completion(root) == []
    assert not (root / writer.SNAPSHOT_NAME).exists()


@pytest.mark.parametrize("kind", ["owned", "complete"])
def test_stage_record_failures_keep_private_files_and_no_completion(job, monkeypatch, kind):
    root, core = job
    with pytest.raises(writer.SecretWriterError):
        with stage(root) as journal:
            original = journal.record
            def record(name, payload):
                if name == ("owned_resource" if kind == "owned" else "private_secret_inventory"):
                    raise OSError("untrusted detail")
                return original(name, payload)
            monkeypatch.setattr(journal, "record", record)
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                capture.remember("x" * 16)
                capture.finish()
    assert (root / writer.EVENTS_NAME).exists()
    assert completion(root) == []


def test_close_failure_is_not_a_completion_record(job, monkeypatch):
    root, core = job
    with pytest.raises(writer.SecretWriterError, match="^secret_close_failed$"):
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                capture.finish()
                events_fd = capture._events_fd
                original = os.close
                def close(fd):
                    original(fd)
                    if fd == events_fd:
                        raise OSError("close status uncertain")
                monkeypatch.setattr(os, "close", close)
    assert completion(root) == []


def test_different_thread_is_refused_and_poisons_writer(job):
    root, core = job
    failures = []
    with pytest.raises(writer.SecretWriterError, match="^secret_writer_changed$"):
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                def foreign_writer():
                    try:
                        capture.remember("x" * 16)
                    except writer.SecretWriterError as error:
                        failures.append(str(error))
                other = threading.Thread(target=foreign_writer)
                other.start()
                other.join(timeout=2)
                assert not other.is_alive()
    assert failures == ["secret_writer_changed"]
    assert completion(root) == []


def test_live_core_rebinding_before_exit_refuses(job):
    root, core = job
    with pytest.raises(writer.SecretWriterError):
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                capture.remember("x" * 16)
                capture.finish()
                path = root / sj.CORE_NAME
                value = json.loads(path.read_text())
                value["checkout_sha"] = "d" * 40
                path.write_text(json.dumps(value))
    assert completion(root) == []


@pytest.mark.parametrize("value", [False, "x" * 15, "\ud800" * 16, "x" * 4097])
def test_invalid_discovery_poisoned_even_when_caught(job, value):
    root, core = job
    with pytest.raises(writer.SecretWriterError, match="^secret_capture_failed$"):
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                with pytest.raises(writer.SecretWriterError):
                    capture.remember(value)
                assert capture._values == []
    assert completion(root) == []


@pytest.mark.parametrize("operation", ["write", "fsync", "close", "parent_fsync"])
def test_snapshot_failure_retains_partial_files_without_completion(job, monkeypatch, operation):
    root, core = job
    with pytest.raises(writer.SecretWriterError, match="^secret_snapshot_failed$"):
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                capture.remember("x" * 16)
                opened = []
                open_original = os.open
                original = getattr(os, operation if operation != "parent_fsync" else "fsync")
                def open_file(path, *args, **kwargs):
                    fd = open_original(path, *args, **kwargs)
                    if path == writer.SNAPSHOT_NAME:
                        opened.append(fd)
                    return fd
                monkeypatch.setattr(os, "open", open_file)
                def fault(fd, *args):
                    if (operation == "parent_fsync" and fd == capture._root_fd
                            or operation != "parent_fsync" and opened and fd == opened[0]):
                        if operation == "close":
                            original(fd)  # status failure after kernel close; never retry this fd
                        raise OSError("untrusted detail")
                    return original(fd, *args)
                monkeypatch.setattr(os, operation if operation != "parent_fsync" else "fsync", fault)
                capture.finish()
    assert (root / writer.SNAPSHOT_NAME).exists()
    assert (root / writer.EVENTS_NAME).exists()
    assert completion(root) == []
    assert sj.aggregate(str(root), ["entrypoint"])["verdict"] == "FAILED"


def test_valid_rebound_core_is_not_this_stage_core(job):
    root, _ = job
    with pytest.raises(writer.SecretWriterError, match="^secret_stage_core_mismatch$"):
        with stage(root) as journal:
            path = root / sj.CORE_NAME
            value = json.loads(path.read_text())
            value["checkout_sha"] = "d" * 40
            data = (json.dumps(value) + "\n").encode()
            path.write_bytes(data)
            current_digest = hashlib.sha256(data).hexdigest()
            with writer.PrivateSecretWriter(str(root), current_digest, journal):
                pass
    assert not (root / writer.EVENTS_NAME).exists()


def test_entrypoint_closed_mid_capture_refuses(job):
    root, core = job
    with pytest.raises(writer.SecretWriterError, match="^secret_stage_not_active$"):
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                capture.remember("x" * 16)
                journal._journal.close()
                capture.finish()
    assert completion(root) == []
    assert sj.aggregate(str(root), ["entrypoint"])["verdict"] == "INCOMPLETE"
