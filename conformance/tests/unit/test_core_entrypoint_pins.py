"""Core identity /2 entrypoint pins (R-EP gate plan P1). Pure: private
temporary files only; no subprocess, git, fork or signal. The supervisor
script is imported as a module, never run."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

from alrust_conformance import observer_binding_reader as br
from alrust_conformance import stage_journal as sj
from test_observer_binding_reader import fixture, dump  # noqa: F401

ROOT = Path(__file__).resolve().parents[3]
SUPERVISOR = ROOT / "conformance/scripts/entrypoint-supervisor.py"
spec = importlib.util.spec_from_file_location("entrypoint_supervisor_pins", SUPERVISOR)
sup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sup)

ENV = {"GITHUB_REPOSITORY": "steliosx/alrust", "GITHUB_RUN_ID": "123",
       "GITHUB_RUN_ATTEMPT": "1", "GITHUB_JOB": "profile", "ImageOS": "ubuntu24",
       "ImageVersion": "20261001.1"}


def files(tmp_path):
    supervisor = tmp_path / "supervisor.py"
    supervisor.write_bytes(b"# synthetic supervisor\n")
    script = tmp_path / "entrypoint.py"
    script.write_bytes(b"# synthetic entrypoint\n")
    interp = tmp_path / "python-real"
    interp.write_bytes(b"\x7fELF synthetic interpreter\n")
    link = tmp_path / "python3"
    link.symlink_to(interp)
    return supervisor, script, interp, link


def pins_for(supervisor, script, link, interp):
    return ({"supervisor": {"path": str(supervisor), "sha256": sj.file_sha256(str(supervisor))},
             "entrypoint_script": {"path": str(script), "sha256": sj.file_sha256(str(script))},
             "interpreter": {"path": str(link), "resolved_path": str(interp)}},
            {"supervisor": sj.file_sha256(str(supervisor)),
             "entrypoint_script": sj.file_sha256(str(script)),
             "interpreter": sj.file_sha256(str(interp))})


def write(tmp_path, monkeypatch, pins=None, measured=None):
    monkeypatch.setattr(sj, "EXTERNAL_CONFIG", str(tmp_path / "absent.conf"))
    root = tmp_path / "job"
    root.mkdir(mode=0o700)
    sha = "a" * 64
    digest = sj.write_core_identity(
        str(root), profile="scaffold", event_head_sha="b" * 40, checkout_sha="b" * 40,
        reference_sha="c" * 40, reference_pin="c" * 40, lock_sha256={"lock": sha},
        lock_pins={"lock": sha}, manifest_sha256=sha, manifest_pin=sha, env=ENV,
        entrypoint_pins=pins, entrypoint_measured=measured)
    data = (root / sj.CORE_NAME).read_bytes()
    assert hashlib.sha256(data).hexdigest() == digest
    return root, data


def test_core_v2_written_and_validated(tmp_path, monkeypatch):
    supervisor, script, interp, link = files(tmp_path)
    pins, measured = pins_for(supervisor, script, link, interp)
    root, data = write(tmp_path, monkeypatch, pins, measured)
    core = sj.validate_core(data, str(root))
    assert core["schema"] == sj.SCHEMA_CORE_V2 and set(core) == sj.CORE_KEYS_V2
    assert core["entrypoint_pins"]["interpreter"] == {
        "path": str(link), "resolved_path": str(interp),
        "measured_sha256": measured["interpreter"]}


def test_core_v1_unchanged_without_pins(tmp_path, monkeypatch):
    root, data = write(tmp_path, monkeypatch)
    core = sj.validate_core(data, str(root))
    assert core["schema"] == sj.SCHEMA_CORE and set(core) == sj.CORE_KEYS


@pytest.mark.parametrize("change,code", [
    (lambda p, m: m.update(supervisor="0" * 64), "entrypoint_not_pinned"),
    (lambda p, m: m.update(entrypoint_script="0" * 64), "entrypoint_not_pinned"),
    (lambda p, m: p["interpreter"].update(path="relative/python"), "entrypoint_pins_invalid"),
    (lambda p, m: p["interpreter"].update(measured_sha256="0" * 64), "entrypoint_pins_invalid"),
    (lambda p, m: p.pop("interpreter"), "entrypoint_pins_invalid"),
    (lambda p, m: m.update(interpreter="not-a-sha"), "entrypoint_pins_invalid"),
])
def test_core_v2_refusals_write_nothing(tmp_path, monkeypatch, change, code):
    supervisor, script, interp, link = files(tmp_path)
    pins, measured = pins_for(supervisor, script, link, interp)
    change(pins, measured)
    with pytest.raises(sj.JournalError, match=code):
        write(tmp_path, monkeypatch, pins, measured)
    assert not (tmp_path / "job" / sj.CORE_NAME).exists()


def test_only_one_of_pins_or_measured_refuses(tmp_path, monkeypatch):
    supervisor, script, interp, link = files(tmp_path)
    pins, measured = pins_for(supervisor, script, link, interp)
    with pytest.raises(sj.JournalError, match="entrypoint_pins_invalid"):
        write(tmp_path, monkeypatch, pins, None)


@pytest.mark.parametrize("mutate", [
    lambda c: c.update(schema=sj.SCHEMA_CORE),             # /1 label with a pins key
    lambda c: c.pop("entrypoint_pins"),                    # /2 label without pins
    lambda c: c["entrypoint_pins"]["supervisor"].update(sha256="x"),
    lambda c: c["entrypoint_pins"]["interpreter"].pop("resolved_path"),
    lambda c: c["entrypoint_pins"].update(extra={}),
    lambda c: c["entrypoint_pins"]["entrypoint_script"].update(path="rel/path"),
])
def test_validate_refuses_malformed_v2(tmp_path, monkeypatch, mutate):
    supervisor, script, interp, link = files(tmp_path)
    pins, measured = pins_for(supervisor, script, link, interp)
    root, data = write(tmp_path, monkeypatch, pins, measured)
    core = json.loads(data)
    mutate(core)
    with pytest.raises(sj.JournalError):
        sj.validate_core(sj._dumps(core), str(root))


def test_file_sha256_refuses_symlink_and_fifo(tmp_path):
    supervisor, script, interp, link = files(tmp_path)
    with pytest.raises(OSError):
        sj.file_sha256(str(link))
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(sj.JournalError):
        sj.file_sha256(str(fifo))


# Observer reader: the expectation set follows the expected schema.
def _as_v2(root, core, args, pins):
    core2 = dict(core, schema=sj.SCHEMA_CORE_V2, entrypoint_pins=pins)
    args["core_sha256"] = dump(root / sj.CORE_NAME, core2)
    return core2


def _v2_pins(tmp_path):
    supervisor, script, interp, link = files(tmp_path)
    pins, measured = pins_for(supervisor, script, link, interp)
    return {"supervisor": pins["supervisor"], "entrypoint_script": pins["entrypoint_script"],
            "interpreter": dict(pins["interpreter"], measured_sha256=measured["interpreter"])}


def test_observer_reader_accepts_matching_v2_expectations(fixture, tmp_path):
    root, core, launch, args = fixture
    core2 = _as_v2(root, core, args, _v2_pins(tmp_path))
    args["expected_core"] = {k: copy.deepcopy(core2[k]) for k in br.CORE_EXPECTATIONS_V2}
    result = br.read_binding_inputs(**args)
    assert json.loads(result.core_bytes)["schema"] == sj.SCHEMA_CORE_V2


def test_observer_reader_refuses_v1_expectation_for_v2_core(fixture, tmp_path):
    root, core, launch, args = fixture
    _as_v2(root, core, args, _v2_pins(tmp_path))
    with pytest.raises(br.BindingReadError):
        br.read_binding_inputs(**args)


def test_observer_reader_refuses_v2_expectation_for_v1_core(fixture, tmp_path):
    root, core, launch, args = fixture
    args["expected_core"] = dict(args["expected_core"], schema=sj.SCHEMA_CORE_V2,
                                 entrypoint_pins=_v2_pins(tmp_path))
    with pytest.raises(br.BindingReadError):
        br.read_binding_inputs(**args)


def test_observer_reader_refuses_changed_v2_pin(fixture, tmp_path):
    root, core, launch, args = fixture
    core2 = _as_v2(root, core, args, _v2_pins(tmp_path))
    expected = {k: copy.deepcopy(core2[k]) for k in br.CORE_EXPECTATIONS_V2}
    expected["entrypoint_pins"]["supervisor"]["sha256"] = "0" * 64
    args["expected_core"] = expected
    with pytest.raises(br.BindingReadError):
        br.read_binding_inputs(**args)


# Supervisor pre-fork checks, called in-process (nothing is run).
def _core_bytes(pins):
    return json.dumps({"schema": sj.SCHEMA_CORE_V2, "entrypoint_pins": pins}).encode()


def test_supervisor_pins_match(tmp_path):
    pins = _v2_pins(tmp_path)
    parsed = sup.core_pins(_core_bytes(pins))
    script = pins["entrypoint_script"]["path"]
    assert sup.check_pins(parsed, pins["supervisor"]["path"], script,
                          Path(script).read_bytes(),
                          [pins["interpreter"]["path"], script]) is None


@pytest.mark.parametrize("which,expected", [
    ("own_path", "supervisor does not match its pin"),
    ("own_bytes", "supervisor does not match its pin"),
    ("script_path", "entrypoint script does not match its pin"),
    ("script_bytes", "entrypoint script does not match its pin"),
    ("interp_path", "interpreter does not match the core's measurement"),
    ("interp_bytes", "interpreter does not match the core's measurement"),
])
def test_supervisor_pin_mismatches(tmp_path, which, expected):
    pins = _v2_pins(tmp_path)
    own = pins["supervisor"]["path"]
    script = pins["entrypoint_script"]["path"]
    script_bytes = Path(script).read_bytes()
    command = [pins["interpreter"]["path"], script]
    if which == "own_path":
        own = str(tmp_path / "elsewhere.py")
    elif which == "own_bytes":
        Path(own).write_bytes(b"# changed\n")
    elif which == "script_path":
        script = str(tmp_path / "other.py")
    elif which == "script_bytes":
        script_bytes = b"# changed\n"
    elif which == "interp_path":
        command[0] = str(tmp_path / "python-real")
    else:
        Path(pins["interpreter"]["resolved_path"]).write_bytes(b"changed\n")
    assert sup.check_pins(sup.core_pins(_core_bytes(pins)), own, script, script_bytes,
                          command) == expected


@pytest.mark.parametrize("core", [
    b'{"schema": "alrust-hosted-core/1"}',
    b'{"schema": "alrust-hosted-core/2"}',
    b'{"schema": "alrust-hosted-core/2", "entrypoint_pins": {}}',
    b"[]",
])
def test_supervisor_requires_v2_pins(core):
    with pytest.raises(ValueError):
        sup.core_pins(core)
