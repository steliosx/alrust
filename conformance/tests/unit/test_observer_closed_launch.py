"""Labeled synthetic files/journals; never launch processes or reference code."""
import copy
import hashlib
import json
import os
import time

import pytest

from alrust_conformance import observer_binding_reader as br
from alrust_conformance import observer_closed_launch as cl
from alrust_conformance import stage_journal as sj
from test_observer_binding_reader import fixture  # Reviewed synthetic byte fixture.


def digest(data):
    return hashlib.sha256(data).hexdigest()


def records(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def write(path, values):
    path.write_bytes(b"".join(sj._dumps(v) for v in values))


@pytest.fixture
def closed(fixture):
    root, _, launch, reader_args = fixture
    bindings = br.read_binding_inputs(**reader_args)
    sj.prepare_journal_dir(str(root))
    with sj.StageJournal(str(root), 0, "database-create"):
        pass
    with sj.StageJournal(str(root), 1, "launch", "database-create") as journal:
        journal.record("launch_receipt", {"path": str(root / "reference/launch.json"),
                                          "sha256": bindings.launch_sha256})
        journal.record("launch_readiness", {
            "receipt_sha256": bindings.launch_sha256,
            "supervisor": {"pid": bindings.supervisor[0], "start_ticks": bindings.supervisor[1]},
            "child": {"pid": bindings.child[0], "start_ticks": bindings.child[1]},
            "child_exe": {"dev": 12, "ino": 34}, "venv_python3": {"dev": 12, "ino": 34},
            "observed_monotonic_ns": time.monotonic_ns()})
    launch_path = root / "journal/01-launch.jsonl"
    with sj.StageJournal(str(root), 2, "launch-confirm", "launch") as journal:
        journal.record("launch_handoff", {
            "launch_journal_sha256": digest(launch_path.read_bytes()),
            "receipt_sha256": bindings.launch_sha256,
            "supervisor": {"pid": bindings.supervisor[0], "start_ticks": bindings.supervisor[1]},
            "child": {"pid": bindings.child[0], "start_ticks": bindings.child[1]},
            "observed_monotonic_ns": time.monotonic_ns()})
    paths = [root / sj.JOURNAL_DIR / ("%02d-%s.jsonl" % (i, name))
             for i, name in enumerate(("database-create", "launch", "launch-confirm"))]
    args = dict(job_root=str(root), root_identity=reader_args["root_identity"], bindings=bindings,
                stages=tuple((name, digest(p.read_bytes())) for name, p in
                             zip(("database-create", "launch", "launch-confirm"), paths)),
                launcher_exit_code=0, confirmation_exit_code=0,
                deadline_ns=time.monotonic_ns() + 4_000_000_000)
    return root, paths, args


def repin(paths, args, *, handoff=True):
    # Synthetic adversarial expectations: preserve a structurally valid chain
    # after a semantic mutation, so neighboring digest checks do not mask it.
    previous = None
    for i, path in enumerate(paths):
        values = records(path)
        if i:
            values[0]["evidence"]["payload"]["previous_stage"]["sha256"] = previous
        if i == 2 and handoff:
            for value in values:
                if value["evidence"]["kind"] == "launch_handoff":
                    value["evidence"]["payload"]["launch_journal_sha256"] = previous
        write(path, values)
        previous = digest(path.read_bytes())
    args["stages"] = tuple((old[0], digest(p.read_bytes())) for old, p in zip(args["stages"], paths))


def test_synthetic_closed_prefix_is_historical_only_and_read_only(closed):
    root, paths, args = closed
    before = {p: p.read_bytes() for p in paths}
    result = cl.read_closed_launch(**args)
    assert result.stages == args["stages"] and result.child == (10001, 124)
    assert result.readiness_interpreter == (12, 34)
    assert result.handoff_monotonic_ns >= result.readiness_monotonic_ns
    assert result.scope == "CLOSED_LAUNCH_BYTES_ONLY_NOT_CURRENT_PROCESS_OR_EXECUTION_QUALIFICATION"
    assert before == {p: p.read_bytes() for p in paths}
    assert not (root / "reference/venv").exists()  # No actual interpreter attestation.


@pytest.mark.parametrize("field,value", [("launcher_exit_code", 1), ("confirmation_exit_code", 1),
    ("launcher_exit_code", False), ("confirmation_exit_code", False), ("launcher_exit_code", "0")])
def test_complete_bytes_never_supply_successful_invocation(closed, field, value):
    _, _, args = closed
    args[field] = value
    with pytest.raises(cl.ClosedLaunchError, match="^invocation_not_successful$"):
        cl.read_closed_launch(**args)


@pytest.mark.parametrize("change", ["missing-confirm", "wrong-name", "duplicate", "list", "bad-digest"])
def test_independent_expected_prefix_required(closed, change):
    _, _, args = closed
    stages = args["stages"]
    args["stages"] = {"missing-confirm": stages[:-1], "wrong-name": stages[:-1] + (("other", stages[-1][1]),),
        "duplicate": (stages[0], stages[0], stages[-1]), "list": list(stages),
        "bad-digest": stages[:-1] + (("launch-confirm", "z" * 64),)}[change]
    with pytest.raises(cl.ClosedLaunchError, match="^expected_stages_invalid$"):
        cl.read_closed_launch(**args)


@pytest.mark.parametrize("index", [0, 1, 2])
def test_original_stage_digests_are_independent(closed, index):
    _, paths, args = closed
    data = records(paths[index]);data[-1]["evidence"]["utc_ns"] += 1;write(paths[index], data)
    with pytest.raises(cl.ClosedLaunchError, match="^journal_digest_mismatch$"):
        cl.read_closed_launch(**args)


@pytest.mark.parametrize("index", [0, 1, 2])
def test_rehashed_failed_stage_refuses(closed, index):
    _, paths, args = closed
    data = records(paths[index]);data[-1]["evidence"]["payload"] = {"result": "FAILED", "code": "synthetic"}
    write(paths[index], data);repin(paths, args)
    with pytest.raises(cl.ClosedLaunchError, match="^stage_not_complete$"):
        cl.read_closed_launch(**args)


@pytest.mark.parametrize("change", ["chain", "core", "sequence", "boot", "truncated"])
def test_rehashed_structural_failures_refuse(closed, change):
    _, paths, args = closed
    data = records(paths[-1])
    if change == "chain": data[0]["evidence"]["payload"]["previous_stage"]["sha256"] = "0" * 64
    if change == "core": data[0]["evidence"]["payload"]["core_identity_sha256"] = "0" * 64
    if change == "sequence": data[1]["sequence"] = False
    if change == "boot": data[1]["evidence"]["boot_id"] = "0" * 36
    write(paths[-1], data)
    if change == "truncated": paths[-1].write_bytes(paths[-1].read_bytes().rstrip())
    args["stages"] = args["stages"][:-1] + (("launch-confirm", digest(paths[-1].read_bytes())),)
    with pytest.raises(cl.ClosedLaunchError, match="^closed_launch_read_failed$"):
        cl.read_closed_launch(**args)


@pytest.mark.parametrize("change", ["receipt", "sup", "child", "bool-ticks", "extra", "exe",
    "bool-dev", "observed-before", "observed-after", "bool-clock", "missing", "duplicate"])
def test_rehashed_readiness_semantics_refuse(closed, change):
    _, paths, args = closed
    data = records(paths[1]);record = next(v for v in data if v["evidence"]["kind"] == "launch_readiness")
    ready = record["evidence"]["payload"]
    if change == "receipt": ready["receipt_sha256"] = "0" * 64
    if change == "sup": ready["supervisor"]["pid"] += 1
    if change == "child": ready["child"]["pid"] += 1
    if change == "bool-ticks": ready["child"]["start_ticks"] = True
    if change == "extra": ready["extra"] = None
    if change == "exe": ready["child_exe"]["ino"] += 1
    if change == "bool-dev": ready["child_exe"]["dev"] = True
    if change == "observed-before": ready["observed_monotonic_ns"] = 199
    if change == "observed-after": ready["observed_monotonic_ns"] = record["evidence"]["monotonic_ns"] + 1
    if change == "bool-clock": ready["observed_monotonic_ns"] = True
    if change == "missing": data.remove(record)
    if change == "duplicate": data.insert(data.index(record), copy.deepcopy(record))
    for i, value in enumerate(data): value["sequence"] = i
    write(paths[1], data);repin(paths, args)
    with pytest.raises(cl.ClosedLaunchError): cl.read_closed_launch(**args)


@pytest.mark.parametrize("change", ["journal", "receipt", "child", "bool-pid", "extra", "before", "after", "missing"])
def test_rehashed_confirmation_semantics_refuse(closed, change):
    _, paths, args = closed
    data = records(paths[2]);record = data[1];handoff = record["evidence"]["payload"]
    if change == "journal": handoff["launch_journal_sha256"] = "0" * 64
    if change == "receipt": handoff["receipt_sha256"] = "0" * 64
    if change == "child": handoff["child"]["start_ticks"] += 1
    if change == "bool-pid": handoff["supervisor"]["pid"] = True
    if change == "extra": handoff["extra"] = None
    if change == "before": handoff["observed_monotonic_ns"] = 0
    if change == "after": handoff["observed_monotonic_ns"] = record["evidence"]["monotonic_ns"] + 1
    if change == "missing": data.pop(1);data[1]["sequence"] = 1
    write(paths[2], data);repin(paths, args, handoff=False)
    with pytest.raises(cl.ClosedLaunchError): cl.read_closed_launch(**args)


@pytest.mark.parametrize("change", ["receipt-path", "receipt-digest", "duplicate-receipt",
                                   "receipt-order", "duplicate-handoff", "extra-confirm-record"])
def test_exact_receipt_count_order_and_confirmation_shape(closed, change):
    _, paths, args = closed
    index = 2 if change in ("duplicate-handoff", "extra-confirm-record") else 1
    data = records(paths[index])
    if change == "receipt-path": data[1]["evidence"]["payload"]["path"] = "/tmp/SYNTHETIC-other"
    if change == "receipt-digest": data[1]["evidence"]["payload"]["sha256"] = "0" * 64
    if change in ("duplicate-receipt", "duplicate-handoff"):
        data.insert(1, copy.deepcopy(data[1]))
    if change == "receipt-order":
        data[1]["evidence"]["payload"], data[2]["evidence"]["payload"] = (
            data[2]["evidence"]["payload"], data[1]["evidence"]["payload"])
        data[1]["evidence"]["kind"], data[2]["evidence"]["kind"] = "launch_readiness", "launch_receipt"
        # Preserve valid capture chronology, isolating record order refusal.
        data[1]["evidence"]["payload"]["observed_monotonic_ns"] = 200
    if change == "extra-confirm-record":
        value = copy.deepcopy(data[1]);value["evidence"]["kind"] = "synthetic-extra";data.insert(1, value)
    for i, value in enumerate(data): value["sequence"] = i
    write(paths[index], data);repin(paths, args)
    with pytest.raises(cl.ClosedLaunchError): cl.read_closed_launch(**args)


@pytest.mark.parametrize("target", ["core", "receipt"])
def test_previously_bound_bytes_must_still_match(closed, target):
    root, _, args = closed
    p = root / (sj.CORE_NAME if target == "core" else "reference/launch.json")
    p.write_bytes(p.read_bytes() + b" ")
    with pytest.raises(cl.ClosedLaunchError, match="^binding_bytes_changed$"):
        cl.read_closed_launch(**args)


@pytest.mark.parametrize("change", ["extra", "symlink", "fifo", "oversize", "public-file", "public-directory"])
def test_unexpected_or_unsafe_files_refuse(closed, change):
    root, paths, args = closed
    path = paths[1]
    if change == "extra": (root / sj.JOURNAL_DIR / "extra").write_text("SYNTHETIC")
    if change in ("symlink", "fifo"):
        old = path.read_bytes();path.unlink()
        if change == "fifo": os.mkfifo(path, 0o600)
        else:
            target = root / "synthetic-copy";target.write_bytes(old);target.chmod(0o600);path.symlink_to(target)
    if change == "oversize": path.write_bytes(b"x" * (sj.MAX_JOURNAL + 1))
    if change == "public-file": path.chmod(0o644)
    if change == "public-directory": (root / sj.JOURNAL_DIR).chmod(0o755)
    with pytest.raises(cl.ClosedLaunchError): cl.read_closed_launch(**args)


@pytest.mark.parametrize("deadline", [False, 0, "future", "expired", "too-far"])
def test_deadline_is_typed_original_and_bounded(closed, deadline):
    _, _, args = closed
    args["deadline_ns"] = {"expired": time.monotonic_ns() - 1,
                           "too-far": time.monotonic_ns() + 6_000_000_000}.get(deadline, deadline)
    with pytest.raises(cl.ClosedLaunchError, match="^deadline_invalid$"): cl.read_closed_launch(**args)


def test_expiry_during_parse_refuses_without_refresh(closed, monkeypatch):
    _, _, args = closed
    real = sj._parse_journal
    def expire(*a, **kw):
        result = real(*a, **kw)
        monkeypatch.setattr(cl.time, "monotonic_ns", lambda: args["deadline_ns"] + 1)
        return result
    monkeypatch.setattr(sj, "_parse_journal", expire)
    with pytest.raises(cl.ClosedLaunchError, match="^deadline_expired$"): cl.read_closed_launch(**args)


def test_named_replacement_after_read_refuses(closed, monkeypatch):
    _, paths, args = closed
    real = sj._parse_journal
    def replace(*a, **kw):
        result = real(*a, **kw)
        if a[2] == "launch-confirm":
            p = paths[1];data = p.read_bytes();p.unlink();p.write_bytes(data);p.chmod(0o600)
        return result
    monkeypatch.setattr(sj, "_parse_journal", replace)
    with pytest.raises(cl.ClosedLaunchError, match="^file_changed$"): cl.read_closed_launch(**args)


def test_close_failure_refuses_and_attempts_every_handle(closed, monkeypatch):
    _, _, args = closed
    real_open, real_close = br._Handles.open, os.close
    opened, closed_fds = [], []
    def track(self, *a, **kw):
        fd = real_open(self, *a, **kw);opened.append(fd);return fd
    def close(fd):
        real_close(fd);closed_fds.append(fd)
        if len(closed_fds) == 1: raise OSError("SYNTHETIC private close failure")
    monkeypatch.setattr(br._Handles, "open", track);monkeypatch.setattr(os, "close", close)
    with pytest.raises(cl.ClosedLaunchError, match="^closed_launch_read_failed$"): cl.read_closed_launch(**args)
    assert len(closed_fds) == len(opened) and sorted(closed_fds) == sorted(opened)


def test_close_time_counts_against_original_deadline(closed, monkeypatch):
    _, _, args = closed
    real = br._Handles.__exit__
    def expire(self, *a):
        result = real(self, *a)
        monkeypatch.setattr(cl.time, "monotonic_ns", lambda: args["deadline_ns"] + 1)
        return result
    monkeypatch.setattr(br._Handles, "__exit__", expire)
    with pytest.raises(cl.ClosedLaunchError, match="^deadline_expired$"): cl.read_closed_launch(**args)


def test_interrupt_preserved_despite_secondary_close_failure(closed, monkeypatch):
    _, _, args = closed
    real_close = os.close
    def interrupt(*a, **kw): raise KeyboardInterrupt("SYNTHETIC primary")
    def close(fd): real_close(fd);raise OSError("SYNTHETIC secondary")
    monkeypatch.setattr(sj, "_parse_journal", interrupt);monkeypatch.setattr(os, "close", close)
    with pytest.raises(KeyboardInterrupt, match="SYNTHETIC primary"): cl.read_closed_launch(**args)
