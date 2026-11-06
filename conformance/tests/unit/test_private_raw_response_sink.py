"""Synthetic private files only; no sockets, SQL, children, services or VM."""
import hashlib
import os
import threading
from pathlib import Path

import pytest

from alrust_conformance import private_raw_response_sink as raw
from alrust_conformance import private_secret_writer as writer
from alrust_conformance import bounded_capture_client as bc
from alrust_conformance import stage_journal as sj
from test_private_secret_writer import job, stage, records, completion


def sink(root, core, journal, capture, **limits):
    budget = bc.RawBudget(max_bytes=limits.get("total", 20),
                          max_responses=limits.get("count", 3))
    return raw.PrivateRawResponseSink(writer=capture, stage=journal,
                                     job_root=str(root), core_sha256=core,
                                     budget=budget,
                                     max_response_bytes=limits.get("response", 10)), budget


def name(root, number=1):
    return root / f"entrypoint-raw-response-{number:06d}.bin"


def test_durable_owned_before_bytes_exact_counts_and_private_evidence(job, monkeypatch):
    root, core = job
    value = b"SYNTHETIC"
    with stage(root) as journal:
        with writer.PrivateSecretWriter(str(root), core, journal) as capture:
            s, budget = sink(root, core, journal, capture)
            real_write, real_fsync = os.write, os.fsync
            synced = []

            def fsync(fd):
                if fd == s._root_fd:
                    synced.append(fd)
                return real_fsync(fd)

            def write(fd, data):
                if fd == s._fd:
                    assert synced
                    owned = [r["payload"] for r in records(root) if r["kind"] == "owned_resource"]
                    assert str(name(root)) in [r["path"] for r in owned]
                    assert budget.used_bytes == len(value)
                return real_write(fd, data)

            monkeypatch.setattr(raw.os, "fsync", fsync)
            monkeypatch.setattr(raw.os, "write", write)
            with s:
                s.begin()
                s.reserve_and_append(value)
                s.reserve_and_append(b"")
                assert s.remaining() == 1
                s.finish()
                s.require_idle()
            capture.finish()
    assert name(root).read_bytes() == value
    assert budget.used_bytes == len(value) and budget.responses == 1
    assert name(root).stat().st_mode & 0o777 == 0o600
    data = Path(sj.journal_path(str(root), 0, "entrypoint")).read_bytes()
    assert value not in data and hashlib.sha256(value).hexdigest().encode() not in data
    assert all(r["kind"] != "export_candidate" for r in records(root))
    assert len(completion(root)) == 1
    assert sj.aggregate(str(root), ["entrypoint"])["verdict"] == "VALID"


@pytest.mark.parametrize("failure", ["byte_cap", "response_cap", "open_finish", "idle_finish",
                                     "budget_change", "root_mode", "core_change", "other_stage"])
def test_failure_poison_refuses_snapshot_and_retains_private_files(job, failure):
    root, core = job
    with pytest.raises((raw.RawSinkError, writer.SecretWriterError)):
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                s, budget = sink(root, core, journal, capture, total=5, response=5, count=1)
                s.begin()
                s.reserve_and_append(b"abc")
                if failure == "byte_cap":
                    s.reserve_and_append(b"XYZ")
                elif failure == "response_cap":
                    s.finish()
                    s.begin()
                elif failure == "open_finish":
                    capture.finish()
                elif failure == "idle_finish":
                    s.finish()
                    capture.finish()  # still-held root descriptor must close first
                elif failure == "budget_change":
                    budget.used_bytes = 0
                    s.reserve_and_append(b"x")
                elif failure == "root_mode":
                    root.chmod(0o755)
                    s.finish()
                elif failure == "core_change":
                    (root / sj.CORE_NAME).write_bytes(b"{}"); s.finish()
                else:
                    capture.check_stage_binding(sj.StageJournal(str(root), 0, "entrypoint"), str(root), core)
    assert name(root).read_bytes() == b"abc"
    assert not (root / writer.SNAPSHOT_NAME).exists()
    assert completion(root) == []
    assert capture._failed is not None


@pytest.mark.parametrize("occupied", ["file", "symlink", "hardlink", "fifo"])
def test_exclusive_occupied_path_not_written(job, occupied):
    root, core = job
    target = root / "existing"
    target.write_bytes(b"SYNTHETIC-untouched")
    target.chmod(0o600)
    path = name(root)
    if occupied == "file": path.write_bytes(b"occupied")
    elif occupied == "symlink": path.symlink_to(target)
    elif occupied == "hardlink": os.link(target, path)
    else: os.mkfifo(path, 0o600)
    with pytest.raises(raw.RawSinkError):
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                s, budget = sink(root, core, journal, capture)
                s.begin()
    assert target.read_bytes() == b"SYNTHETIC-untouched"
    assert budget.used_bytes == 0
    assert completion(root) == []


@pytest.mark.parametrize("change", ["replace", "symlink", "hardlink", "mode", "size"])
def test_open_file_changed_refuses_more_bytes(job, change):
    root, core = job
    with pytest.raises(raw.RawSinkError):
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                s, _ = sink(root, core, journal, capture)
                s.begin(); s.reserve_and_append(b"abc")
                path = name(root)
                if change in ("replace", "symlink"):
                    path.rename(root / "original")
                    if change == "replace": path.write_bytes(b"xyz"); path.chmod(0o600)
                    else: path.symlink_to(root / "original")
                elif change == "hardlink": os.link(path, root / "extra")
                elif change == "mode": path.chmod(0o644)
                else: path.write_bytes(b"larger")
                s.reserve_and_append(b"x")
    assert completion(root) == []


@pytest.mark.parametrize("fault", ["record", "dir_fsync", "write_zero", "write_interrupt",
                                  "file_fsync", "file_close", "root_close"])
def test_io_failures_poison_and_never_replace_interrupt(job, monkeypatch, fault):
    root, core = job
    primary = KeyboardInterrupt("SYNTHETIC-private-detail")
    with pytest.raises(KeyboardInterrupt if fault == "write_interrupt" else raw.RawSinkError) as caught:
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                s, budget = sink(root, core, journal, capture)
                if fault == "record":
                    monkeypatch.setattr(journal, "record", lambda *a: (_ for _ in ()).throw(OSError("private")))
                real_fsync, real_write, real_close = os.fsync, os.write, os.close

                def fsync(fd):
                    if (fault == "dir_fsync" and fd == s._root_fd or
                            fault == "file_fsync" and fd == s._fd):
                        raise OSError("private")
                    return real_fsync(fd)

                def write(fd, data):
                    if fd == s._fd:
                        if fault == "write_zero": return 0
                        if fault == "write_interrupt": raise primary
                    return real_write(fd, data)

                def close(fd):
                    fail = (fault == "file_close" and fd == file_fd or
                            fault == "root_close" and fd == root_fd)
                    real_close(fd)
                    if fail: raise OSError("private")

                monkeypatch.setattr(raw.os, "fsync", fsync)
                monkeypatch.setattr(raw.os, "write", write)
                s.begin()
                file_fd, root_fd = s._fd, s._root_fd
                monkeypatch.setattr(raw.os, "close", close)
                s.reserve_and_append(b"abc")
                s.finish()
                s.close()
    if fault == "write_interrupt": assert caught.value is primary
    assert budget._failed and capture._failed is not None
    assert s._fd is None and s._root_fd is None
    assert completion(root) == []
    assert name(root).exists()  # failure never unlinks even an empty allocation
    if fault in ("record", "dir_fsync"): assert name(root).read_bytes() == b""


def test_short_writes_complete_and_total_budget_not_reset(job, monkeypatch):
    root, core = job
    with stage(root) as journal:
        with writer.PrivateSecretWriter(str(root), core, journal) as capture:
            s, budget = sink(root, core, journal, capture, total=5, response=4)
            real_write = os.write
            monkeypatch.setattr(raw.os, "write", lambda fd, data: real_write(fd, data[:1]) if fd == s._fd else real_write(fd, data))
            with s:
                s.begin(); s.reserve_and_append(b"abcd"); s.finish()
                s.begin(); assert s.remaining() == 1
                s.reserve_and_append(b"e"); s.reserve_and_append(b""); s.finish()
            capture.finish()
    assert budget.used_bytes == 5 and budget.responses == 2
    assert name(root).read_bytes() == b"abcd" and name(root, 2).read_bytes() == b"e"


@pytest.mark.parametrize("bad", ["digits", "spent", "wrong_core", "second_sink", "git"])
def test_bad_constructor_and_legacy_client_injection_refuse(job, bad):
    root, core = job
    with pytest.raises(raw.RawSinkError):
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                budget = bc.RawBudget(max_bytes=10, max_responses=1000000 if bad == "digits" else 2)
                if bad == "spent": budget.begin()
                if bad == "git":
                    (root / ".git").mkdir()
                    (root / ".git" / "HEAD").write_text("ref: refs/heads/synthetic\n")
                if bad == "second_sink": sink(root, core, journal, capture)
                raw.PrivateRawResponseSink(writer=capture, stage=journal, job_root=str(root),
                                           core_sha256="a" * 64 if bad == "wrong_core" else core,
                                           budget=budget, max_response_bytes=5)
    assert completion(root) == []


def test_actual_mailbox_ancestor_refuses_before_raw_allocation(tmp_path, monkeypatch):
    parent = tmp_path / "synthetic-mailbox"
    parent.mkdir(mode=0o700)
    root, core = job.__wrapped__(parent, monkeypatch)
    with pytest.raises(raw.RawSinkError, match="raw_sink_placement_invalid"):
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                sink(root, core, journal, capture)
    assert not name(root).exists() and completion(root) == []


@pytest.mark.parametrize("kind", ["gitfile", "gitlink", "bare", "empty_placeholder"])
def test_placement_metadata_distinguishes_real_metadata_from_empty_directory(job, kind):
    root, core = job
    if kind == "gitfile": (root / ".git").write_text("gitdir: /SYNTHETIC-unused\n")
    elif kind == "gitlink": (root / ".git").symlink_to(root / "SYNTHETIC-absent")
    elif kind == "bare":
        (root / "HEAD").write_text("ref: refs/heads/synthetic\n")
        (root / "objects").mkdir(); (root / "refs").mkdir()
    else: (root / ".git").mkdir()
    def run():
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                s, _ = sink(root, core, journal, capture)
                s.close(); capture.finish()
    if kind == "empty_placeholder": run(); assert len(completion(root)) == 1
    else:
        with pytest.raises(raw.RawSinkError, match="raw_sink_placement_invalid"): run()
        assert not name(root).exists() and completion(root) == []


def test_root_replacement_refuses_without_writing_replacement(job):
    root, core = job
    saved = root.parent / "original-root"
    with pytest.raises(raw.RawSinkError):
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                s, _ = sink(root, core, journal, capture)
                s.begin(); s.reserve_and_append(b"abc")
                root.rename(saved); root.mkdir(mode=0o700)
                s.reserve_and_append(b"x")
    assert name(saved).read_bytes() == b"abc" and not list(root.iterdir())
    assert completion(saved) == []


def test_foreign_thread_poison_refuses_completion(job):
    root, core = job
    caught = []
    with pytest.raises(writer.SecretWriterError):
        with stage(root) as journal:
            with writer.PrivateSecretWriter(str(root), core, journal) as capture:
                s, budget = sink(root, core, journal, capture)
                s.begin()
                def other_owner():
                    try: s.reserve_and_append(b"abc")
                    except raw.RawSinkError as error: caught.append(error)
                thread = threading.Thread(target=other_owner)
                thread.start(); thread.join()
                assert len(caught) == 1 and budget._failed
                capture.finish()
    assert name(root).read_bytes() == b"" and completion(root) == []


def test_concrete_sink_is_not_accidentally_accepted_by_old_double_charging_client(job):
    root, core = job
    with stage(root) as journal:
        with writer.PrivateSecretWriter(str(root), core, journal) as capture:
            s, budget = sink(root, core, journal, capture)
            with s:
                with pytest.raises(bc.BoundedClientError):
                    bc.BoundedCaptureClient("http://unused", sink=s, budget=budget,
                                            max_response_bytes=5, deadline_ns=1)
            capture.finish()
