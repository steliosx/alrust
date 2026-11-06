"""Pure controls for check-pin.py (W5A-R1): the stop helper's literal digest
is checked on its actual bytes before it runs. The script is loaded as a
module and its main() called on private temporary files; it is never
executed. No process, signal or network."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
spec = importlib.util.spec_from_file_location("check_pin", SCRIPTS / "check-pin.py")
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)

HELPER = (SCRIPTS / "stop-supervisor.py").read_bytes()


def run(capsys, argv):
    code = cp.main(argv)
    return code, json.loads(capsys.readouterr().out)


@pytest.fixture
def helper(tmp_path):
    path = tmp_path / "stop-supervisor.py"
    path.write_bytes(HELPER)
    return path, hashlib.sha256(HELPER).hexdigest()


def test_the_committed_helper_matches_its_digest(helper, capsys):
    path, digest = helper
    assert run(capsys, [str(path), digest]) == (0, {"result": "MATCH"})


@pytest.mark.parametrize("position", [0, len(HELPER) // 2, len(HELPER) - 1])
def test_a_helper_changed_by_one_byte_is_refused(helper, capsys, position):
    path, digest = helper
    data = bytearray(HELPER)
    data[position] ^= 0x01
    path.write_bytes(bytes(data))
    assert run(capsys, [str(path), digest]) == (2, {"result": "REFUSED", "code": "pin_mismatch"})


def test_an_appended_byte_is_refused(helper, capsys):
    path, digest = helper
    path.write_bytes(HELPER + b"\n")
    assert run(capsys, [str(path), digest])[1]["code"] == "pin_mismatch"


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "missing"])
def test_a_helper_that_is_not_a_regular_file_is_refused(helper, capsys, tmp_path, kind):
    path, digest = helper
    target = tmp_path / ("target-" + kind)
    if kind == "symlink":
        target.symlink_to(path)
    elif kind == "fifo":
        os.mkfifo(target, 0o600)
    elif kind == "directory":
        target.mkdir()
    assert run(capsys, [str(target), digest]) == (
        2, {"result": "REFUSED", "code": "pin_unreadable"})


def test_a_file_over_the_one_mib_bound_is_refused(capsys, tmp_path):
    # A real file one byte over MAX_BYTES (the bound is a default argument,
    # fixed at definition, so it is exercised with real size, not patched).
    path = tmp_path / "big.py"
    data = b"#" * (cp.MAX_BYTES + 1)
    path.write_bytes(data)
    assert run(capsys, [str(path), hashlib.sha256(data).hexdigest()]) == (
        2, {"result": "REFUSED", "code": "pin_unreadable"})


def test_a_file_exactly_at_the_bound_is_read(capsys, tmp_path):
    path = tmp_path / "edge.py"
    data = b"#" * cp.MAX_BYTES
    path.write_bytes(data)
    assert run(capsys, [str(path), hashlib.sha256(data).hexdigest()]) == (0, {"result": "MATCH"})


def test_short_reads_still_hash_the_complete_file(helper, capsys, monkeypatch):
    path, digest = helper
    real = os.read
    monkeypatch.setattr(cp.os, "read", lambda fd, n: real(fd, min(n, 7)))
    assert run(capsys, [str(path), digest]) == (0, {"result": "MATCH"})


@pytest.mark.parametrize("argv", [
    [], ["/x"], ["rel/path", "0" * 64], ["/a/../b", "0" * 64], ["/x", "0" * 63],
    ["/x", "A" * 64], ["/x", "0" * 64, "extra"]])
def test_usage(capsys, argv):
    assert run(capsys, argv) == (64, {"result": "NOT_RUN", "reason": "usage"})


def test_the_script_makes_no_process_or_signal_call():
    source = (SCRIPTS / "check-pin.py").read_text()
    for forbidden in ("subprocess", "os.kill", "os.system", "os.exec", "os.fork", "import signal"):
        assert forbidden not in source, forbidden
