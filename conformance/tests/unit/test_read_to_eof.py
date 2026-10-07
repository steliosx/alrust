"""Pure controls for the five-reader follow-up to W4-READ-R1: each reader
reads its regular file to EOF and refuses once the accumulated bytes pass
its bound, so nothing parses or hashes a partial file.

Readers: stop_recorder._read_receipt (W1), run-stage.read_pins (W2),
stage-tool.read_pins, launch-reference.read_pins and
stop-supervisor.read_bounded. Each is called directly on private temporary
files, with os.read replaced by a short-read adapter around the real read
(legal for a regular file). The scripts are loaded as modules, never run:
no process, signal, journal stage or socket."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

from alrust_conformance import stop_recorder as sr

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rs = _load("run_stage_eof", "run-stage.py")
st = _load("stage_tool_eof", "stage-tool.py")
lr = _load("launch_reference_eof", "launch-reference.py")
ss = _load("stop_supervisor_eof", "stop-supervisor.py")

PINS = {"entrypoint": {"entrypoint_script_sha256": "d" * 64, "interpreter_path": "/p"},
        "tools": {"database-create": {"driver_sha256": "c" * 64}}}
PIN_READERS = {
    "run-stage": (lambda p: rs.read_pins(p), rs, (ValueError,)),
    "stage-tool": (lambda p: st.read_pins(p), st, (ValueError,)),
    "launch-reference": (lambda p: lr.read_pins(p), lr, (lr.Refused,)),
}


def _short_reads(monkeypatch, size, fail_on=None, grow=None):
    """os.read returns at most SIZE bytes per call. FAIL_ON=n raises OSError
    on the n-th call; GROW=(path, extra) appends EXTRA to PATH just before
    the first read (a file that grows after its fstat). Returns the log."""
    real_read, calls = os.read, []

    def read(fd, n):
        calls.append(n)
        if grow is not None and len(calls) == 1:
            with open(grow[0], "ab") as f:
                f.write(grow[1])
        if fail_on is not None and len(calls) == fail_on:
            raise OSError("synthetic mid-read failure")
        return real_read(fd, min(n, size))
    monkeypatch.setattr(os, "read", read)
    return calls


def _fds():
    return set(os.listdir("/proc/self/fd"))


# --- the three pins readers --------------------------------------------------------------

@pytest.fixture(params=sorted(PIN_READERS))
def pins_reader(request):
    return PIN_READERS[request.param]


def test_pins_short_reads_parse_the_complete_object(pins_reader, tmp_path, monkeypatch):
    read, _, _ = pins_reader
    path = tmp_path / "pins.json"
    path.write_text(json.dumps(PINS))
    calls = _short_reads(monkeypatch, 3)
    assert read(str(path)) == PINS
    assert len(calls) > len(json.dumps(PINS)) // 3


def test_pins_trailing_bytes_after_a_valid_first_read_are_refused(pins_reader, tmp_path,
                                                                  monkeypatch):
    read, _, refused = pins_reader
    prefix = json.dumps(PINS).encode()
    path = tmp_path / "pins.json"
    path.write_bytes(prefix + b" trailing")
    _short_reads(monkeypatch, len(prefix))
    with pytest.raises(refused):
        read(str(path))


def test_pins_over_the_bound_across_chunks_are_refused(pins_reader, tmp_path, monkeypatch):
    read, module, refused = pins_reader
    data = json.dumps(PINS).encode()
    path = tmp_path / "pins.json"
    path.write_bytes(data)
    monkeypatch.setattr(module, "MAX_PINS", len(data) - 1)
    _short_reads(monkeypatch, 4)
    with pytest.raises(refused):
        read(str(path))


def test_pins_exactly_at_the_bound_are_read(pins_reader, tmp_path, monkeypatch):
    read, module, _ = pins_reader
    data = json.dumps(PINS).encode()
    path = tmp_path / "pins.json"
    path.write_bytes(data)
    monkeypatch.setattr(module, "MAX_PINS", len(data))
    _short_reads(monkeypatch, 4)
    assert read(str(path)) == PINS


def test_pins_mid_read_error_propagates_and_closes(pins_reader, tmp_path, monkeypatch):
    read, _, _ = pins_reader
    path = tmp_path / "pins.json"
    path.write_text(json.dumps(PINS))
    before = _fds()
    _short_reads(monkeypatch, 4, fail_on=3)
    with pytest.raises(OSError):
        read(str(path))
    assert _fds() == before


# --- stop_recorder._read_receipt (W1) ------------------------------------------------------

RECEIPT = json.dumps({"schema": "alrust-reference-launch/1", "pid": 4194293,
                      "start_ticks": 333}).encode() + b"\n"


def test_receipt_short_reads_return_the_complete_bytes(tmp_path, monkeypatch):
    path = tmp_path / "launch.json"
    path.write_bytes(RECEIPT)
    calls = _short_reads(monkeypatch, 5)
    assert sr._read_receipt(str(path)) == RECEIPT
    assert len(calls) > len(RECEIPT) // 5


def test_receipt_growing_past_the_bound_after_fstat_is_refused(tmp_path, monkeypatch):
    # fstat saw an in-bound size; the bytes read pass the bound.
    path = tmp_path / "launch.json"
    path.write_bytes(RECEIPT)
    monkeypatch.setattr(sr, "MAX_RECEIPT", len(RECEIPT))
    _short_reads(monkeypatch, 4, grow=(path, b"x" * 8))
    with pytest.raises(sr.StopRecorderError) as error:
        sr._read_receipt(str(path))
    assert error.value.code == "receipt_unreadable"


def test_receipt_mid_read_error_is_a_fixed_code_and_closes(tmp_path, monkeypatch):
    path = tmp_path / "launch.json"
    path.write_bytes(RECEIPT)
    before = _fds()
    _short_reads(monkeypatch, 4, fail_on=2)
    with pytest.raises(sr.StopRecorderError) as error:
        sr._read_receipt(str(path))
    assert error.value.code == "receipt_unreadable" and _fds() == before


# --- stop-supervisor.read_bounded (P4) -----------------------------------------------------

def test_bounded_short_reads_return_the_complete_bytes(tmp_path, monkeypatch):
    path = tmp_path / "launch.json"
    path.write_bytes(RECEIPT)
    _short_reads(monkeypatch, 5)
    assert ss.read_bounded(str(path), 4096) == RECEIPT


def test_bounded_growing_past_the_bound_after_fstat_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "launch.json"
    path.write_bytes(RECEIPT)
    _short_reads(monkeypatch, 4, grow=(path, b"x" * 8))
    with pytest.raises(ValueError):
        ss.read_bounded(str(path), len(RECEIPT))


def test_bounded_mid_read_error_propagates_and_closes(tmp_path, monkeypatch):
    path = tmp_path / "launch.json"
    path.write_bytes(RECEIPT)
    before = _fds()
    _short_reads(monkeypatch, 4, fail_on=2)
    with pytest.raises(OSError):
        ss.read_bounded(str(path), 4096)
    assert _fds() == before


# --- the regular-file and link checks are unchanged ---------------------------------------

@pytest.mark.parametrize("reader", ["run-stage", "stage-tool", "launch-reference",
                                    "stop_recorder", "stop-supervisor"])
@pytest.mark.parametrize("kind", ["fifo", "link"])
def test_non_regular_or_linked_inputs_are_still_refused(tmp_path, reader, kind):
    target = tmp_path / "real.json"
    target.write_text(json.dumps(PINS))
    path = tmp_path / "input.json"
    if kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.symlink_to(target)
    if reader == "stop_recorder":
        with pytest.raises(sr.StopRecorderError):
            sr._read_receipt(str(path))
    elif reader == "stop-supervisor":
        with pytest.raises((ValueError, OSError)):
            ss.read_bounded(str(path), 4096)
    else:
        read, _, refused = PIN_READERS[reader]
        with pytest.raises(refused + (OSError,)):
            read(str(path))
