"""Core identity /3 tool pins (R-EP plan P2b-1). Pure: private temporary
files only; no subprocess, git, fork or signal. CLI cases stop before the
git measurement, which is replaced so that reaching it fails the test."""
import copy
import json

import pytest

from alrust_conformance import observer_binding_reader as br
from alrust_conformance import stage_journal as sj
from test_observer_binding_reader import fixture, dump  # noqa: F401
from test_tool_exit import (ENTRY_MEASURED, ENTRY_PINS, TOOL_MEASURED, TOOL_PINS,
                            make_job)


def test_v3_written_and_validated(tmp_path, monkeypatch):
    root, digest = make_job(tmp_path, monkeypatch)
    data = (root / sj.CORE_NAME).read_bytes()
    core = sj.validate_core(data, str(root))
    assert core["schema"] == sj.SCHEMA_CORE_V3 and set(core) == sj.CORE_KEYS_V3
    assert core["tool_pins"] == TOOL_PINS
    assert core["entrypoint_pins"]["interpreter"]["measured_sha256"] == \
        ENTRY_MEASURED["interpreter"]


@pytest.mark.parametrize("schema,keys", [(1, sj.CORE_KEYS), (2, sj.CORE_KEYS_V2)])
def test_v1_and_v2_unchanged(tmp_path, monkeypatch, schema, keys):
    root, digest = make_job(tmp_path, monkeypatch, schema)
    core = sj.validate_core((root / sj.CORE_NAME).read_bytes(), str(root))
    assert set(core) == keys


def _write(tmp_path, monkeypatch, **changes):
    args = dict(entrypoint_pins=copy.deepcopy(ENTRY_PINS),
                entrypoint_measured=dict(ENTRY_MEASURED),
                tool_pins=copy.deepcopy(TOOL_PINS), tool_measured=dict(TOOL_MEASURED))
    for key, change in changes.items():
        args[key] = change(args[key]) if callable(change) else change
    monkeypatch.setattr(sj, "EXTERNAL_CONFIG", str(tmp_path / "absent.conf"))
    root = tmp_path / "job"
    root.mkdir(mode=0o700, exist_ok=True)
    sha = "a" * 64
    try:
        sj.write_core_identity(
            str(root), profile="scaffold", event_head_sha="b" * 40,
            checkout_sha="b" * 40, reference_sha="c" * 40, reference_pin="c" * 40,
            lock_sha256={"lock": sha}, lock_pins={"lock": sha},
            manifest_sha256=sha, manifest_pin=sha,
            env={"GITHUB_REPOSITORY": "steliosx/alrust", "GITHUB_RUN_ID": "123",
                 "GITHUB_RUN_ATTEMPT": "1", "GITHUB_JOB": "profile",
                 "ImageOS": "ubuntu24", "ImageVersion": "20261001.1"}, **args)
    finally:
        written = (root / sj.CORE_NAME).exists()
    return written


def _drop(d, key):
    d.pop(key)
    return d


@pytest.mark.parametrize("changes,code", [
    ({"tool_measured": lambda m: dict(m, registry="0" * 64)}, "tool_not_pinned"),
    ({"tool_pins": lambda p: dict(p, registry={"driver": dict(p["registry"]["driver"],
                                                              sha256="0" * 64)})},
     "tool_not_pinned"),
    ({"entrypoint_pins": None, "entrypoint_measured": None}, "tool_pins_invalid"),
    ({"tool_measured": None}, "tool_pins_invalid"),
    ({"tool_pins": None}, "tool_pins_invalid"),
    ({"tool_pins": lambda p: _drop(p, "registry"),
      "tool_measured": lambda m: _drop(m, "registry")}, "tool_pins_invalid"),
    ({"tool_pins": lambda p: dict(p, extra=p["registry"]),
      "tool_measured": lambda m: dict(m, extra="c" * 64)}, "tool_pins_invalid"),
    ({"tool_pins": lambda p: dict(p, registry={"driver": {"path": "rel/x.py",
                                                          "sha256": "c" * 64}})},
     "tool_pins_invalid"),
    ({"tool_pins": lambda p: dict(p, registry=dict(p["registry"], extra=1))},
     "tool_pins_invalid"),
    ({"tool_pins": lambda p: dict(p, registry="c" * 64)}, "tool_pins_invalid"),
])
def test_v3_refusals_write_nothing(tmp_path, monkeypatch, changes, code):
    with pytest.raises(sj.JournalError, match=code):
        _write(tmp_path, monkeypatch, **changes)
    assert not (tmp_path / "job" / sj.CORE_NAME).exists()


@pytest.mark.parametrize("mutate", [
    lambda c: c.update(schema=sj.SCHEMA_CORE_V2),          # /2 label with tool pins
    lambda c: c.pop("tool_pins"),                          # /3 label without them
    lambda c: c.pop("entrypoint_pins"),
    lambda c: c["tool_pins"].pop("registry"),
    lambda c: c["tool_pins"].update(extra={"driver": c["tool_pins"]["registry"]["driver"]}),
    lambda c: c["tool_pins"]["registry"]["driver"].update(sha256="C" * 64),
    lambda c: c["tool_pins"]["registry"]["driver"].update(path="stage-tool.py"),
    lambda c: c["tool_pins"]["registry"].update(interpreter="/usr/bin/python3"),
])
def test_validate_refuses_malformed_v3(tmp_path, monkeypatch, mutate):
    root, digest = make_job(tmp_path, monkeypatch)
    core = json.loads((root / sj.CORE_NAME).read_bytes())
    mutate(core)
    with pytest.raises(sj.JournalError):
        sj.validate_core(sj._dumps(core), str(root))


def test_tool_pin_names_match_the_holder_tools():
    from alrust_conformance import tool_exit
    assert sj.TOOL_PIN_NAMES == tool_exit.HOLDER_TOOLS


# Observer reader: the expectation set follows the expected schema.
def _as_v3(root, core, args):
    core3 = dict(core, schema=sj.SCHEMA_CORE_V3, entrypoint_pins=copy.deepcopy(ENTRY_PINS),
                 tool_pins=copy.deepcopy(TOOL_PINS))
    core3["entrypoint_pins"]["interpreter"]["measured_sha256"] = ENTRY_MEASURED["interpreter"]
    args["core_sha256"] = dump(root / sj.CORE_NAME, core3)
    return core3


def test_observer_reader_accepts_matching_v3_expectations(fixture):
    root, core, launch, args = fixture
    core3 = _as_v3(root, core, args)
    args["expected_core"] = {k: copy.deepcopy(core3[k]) for k in br.CORE_EXPECTATIONS_V3}
    result = br.read_binding_inputs(**args)
    assert json.loads(result.core_bytes)["schema"] == sj.SCHEMA_CORE_V3


def test_observer_reader_refuses_v2_expectation_for_v3_core(fixture):
    root, core, launch, args = fixture
    core3 = _as_v3(root, core, args)
    expected = {k: copy.deepcopy(core3[k]) for k in br.CORE_EXPECTATIONS_V2}
    args["expected_core"] = dict(expected, schema=sj.SCHEMA_CORE_V2)
    with pytest.raises(br.BindingReadError):
        br.read_binding_inputs(**args)


def test_observer_reader_refuses_v3_expectation_for_v1_core(fixture):
    root, core, launch, args = fixture
    pins = copy.deepcopy(ENTRY_PINS)
    pins["interpreter"]["measured_sha256"] = ENTRY_MEASURED["interpreter"]
    args["expected_core"] = dict(args["expected_core"], schema=sj.SCHEMA_CORE_V3,
                                 entrypoint_pins=pins, tool_pins=copy.deepcopy(TOOL_PINS))
    with pytest.raises(br.BindingReadError):
        br.read_binding_inputs(**args)


def test_observer_reader_refuses_changed_tool_pin(fixture):
    root, core, launch, args = fixture
    core3 = _as_v3(root, core, args)
    expected = {k: copy.deepcopy(core3[k]) for k in br.CORE_EXPECTATIONS_V3}
    expected["tool_pins"]["registry"]["driver"]["sha256"] = "0" * 64
    args["expected_core"] = expected
    with pytest.raises(br.BindingReadError):
        br.read_binding_inputs(**args)


# CLI: tool pins are all-or-nothing and need the entrypoint pins. These
# refusals happen before the git measurement.
BASE = ["--repo-root", "/repo", "--job-root", "/job", "--profile", "scaffold",
        "--event-head-sha", "b" * 40, "--reference-pin", "c" * 40,
        "--manifest-pin", "a" * 64, "--lock-pin", "Cargo.lock=" + "a" * 64]
ENTRY = ["--supervisor-pin", "conformance/scripts/entrypoint-supervisor.py=" + "a" * 64,
         "--entrypoint-script-pin", "entry.py=" + "b" * 64, "--interpreter", "/usr/bin/python3"]
TOOLS = ["--tool-pin", "database-create=conformance/scripts/stage-tool.py=" + "c" * 64,
         "--tool-pin", "registry=conformance/scripts/stage-tool.py=" + "c" * 64]


@pytest.fixture
def no_git(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("git measurement reached")
    monkeypatch.setattr(sj, "_git_head", refuse)


@pytest.mark.parametrize("argv", [
    BASE + TOOLS,                                     # tool pins without entrypoint pins
    BASE + ENTRY + TOOLS[:2],                         # one tool only
    BASE + ENTRY + TOOLS + TOOLS[:2],                 # a repeated tool
    BASE + ENTRY + ["--tool-pin", "database-drop=x.py=" + "c" * 64] + TOOLS[2:],
    BASE + ENTRY + ["--tool-pin", "registry"] + TOOLS[:2],
])
def test_core_cli_tool_pin_usage(no_git, capsys, argv):
    assert sj.core_main(argv) == 64
    assert json.loads(capsys.readouterr().out)["reason"] == "usage"


@pytest.mark.parametrize("rel", ["/abs/stage-tool.py", "../stage-tool.py", "a b.py"])
def test_core_cli_tool_pin_path_refusals(no_git, capsys, monkeypatch, rel):
    monkeypatch.setattr(sj, "file_sha256", lambda path, *a: "a" * 64)
    argv = BASE + ENTRY + ["--tool-pin", "database-create=%s=%s" % (rel, "c" * 64)] + TOOLS[2:]
    assert sj.core_main(argv) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "tool_pins_invalid"


@pytest.mark.parametrize("drop", ["tool_pins", "entrypoint_pins"])
def test_observer_reader_v3_label_needs_the_exact_v3_set(fixture, drop):
    root, core, launch, args = fixture
    core3 = _as_v3(root, core, args)
    expected = {k: copy.deepcopy(core3[k]) for k in br.CORE_EXPECTATIONS_V3}
    expected.pop(drop)
    args["expected_core"] = expected
    with pytest.raises(br.BindingReadError, match="expected_core_invalid"):
        br.read_binding_inputs(**args)
