"""Offline denial controls: no server, socket, credential or test executor."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess

import pytest

from alrust_conformance.ci import inspect_profile, main, WEBUI

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = "conformance/hosted-profiles.json"


def fixture(tmp_path):
    document = json.loads((ROOT / MANIFEST).read_text())
    # Two real selections also test that an unselected module is bound.
    document["profiles"] = deepcopy(document["profiles"][:2])
    for item in document["profiles"]:
        for name in (item["module"], item["contract"], item["pytest"]["config"]):
            destination = tmp_path / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((ROOT / name).read_bytes())
    return document


def write_manifest(root, document):
    data = json.dumps(document).encode()
    (root / MANIFEST).write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def test_all_real_profiles_inert_even_with_hostile_environment(monkeypatch):
    secret = "SYNTHETIC-" + "f" * 48
    monkeypatch.setenv("ALRUST_TARGET", "http://example.invalid")
    monkeypatch.setenv("ALRUST_API_KEY", secret)
    monkeypatch.setenv("PYTEST_ADDOPTS", "conformance/tests/security --trace")
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("network attempted"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("process attempted"))
    raw = (ROOT / MANIFEST).read_bytes()
    document = json.loads(raw)
    assert len(document["profiles"]) == 19
    for item in document["profiles"]:
        report = inspect_profile(ROOT, MANIFEST, hashlib.sha256(raw).hexdigest(), item["id"])
        assert report["status"] == "NOT_RUN" and report["execution_started"] is False
        assert report["artifact_allowlist"] == [] and report["phase0_complete"] is False
        assert secret not in json.dumps(report)


def test_cli_has_no_pass_result_or_private_environment_export(monkeypatch, capsys):
    monkeypatch.setenv("ALRUST_PRIVATE", "PRIVATE-CANARY")
    digest = hashlib.sha256((ROOT / MANIFEST).read_bytes()).hexdigest()
    assert main(["--profile", "scaffold", "--manifest-sha256", digest]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "NOT_RUN"
    assert main(["--profile", "PRIVATE-CANARY", "--manifest-sha256", digest]) == 1
    output = capsys.readouterr().out
    assert "PRIVATE-CANARY" not in output
    assert json.loads(output)["status"] == "FAIL"


def test_manifest_digest_and_unselected_module_tamper_refused(tmp_path):
    document = fixture(tmp_path)
    digest = write_manifest(tmp_path, document)
    with pytest.raises(ValueError, match="manifest_digest_mismatch"):
        inspect_profile(tmp_path, MANIFEST, "0" * 64, "scaffold")
    (tmp_path / document["profiles"][1]["module"]).write_text("# tampered unselected module")
    with pytest.raises(ValueError, match="module_digest_mismatch"):
        inspect_profile(tmp_path, MANIFEST, digest, "scaffold")


@pytest.mark.parametrize("field", ["execution_enabled", "executor_implemented", "phase0_complete"])
@pytest.mark.parametrize("value", [True, 0, None])
def test_enablement_and_false_like_values_do_not_authorize(tmp_path, field, value):
    document = fixture(tmp_path)
    document[field] = value
    digest = write_manifest(tmp_path, document)
    with pytest.raises(ValueError, match="inert_entrypoint_only"):
        inspect_profile(tmp_path, MANIFEST, digest, "scaffold")


@pytest.mark.parametrize("mutation", ["duplicate_id", "duplicate_module", "enable_profile",
                                     "drop_gate", "empty_inventory", "extra_selection",
                                     "autoload", "inherited_opts", "unbound_import", "wrong_pin"])
def test_gate_selection_and_dependency_changes_refused(tmp_path, mutation):
    document = fixture(tmp_path)
    first, second = document["profiles"]
    if mutation == "duplicate_id": second["id"] = first["id"]
    elif mutation == "duplicate_module": second["module"] = first["module"]
    elif mutation == "enable_profile": second["execution_enabled"] = True
    elif mutation == "drop_gate": second["gate_required"] = False
    elif mutation == "empty_inventory": document["profiles"] = []
    elif mutation == "extra_selection": first["pytest"]["selection"].append("conformance/tests/security")
    elif mutation == "autoload": first["pytest"]["plugins_autoload"] = True
    elif mutation == "inherited_opts": first["pytest"]["clear_inherited_addopts"] = False
    elif mutation == "unbound_import": first["fixture_imports"] = ["conformance/tests/security/fault.py"]
    else: document["reference_commit"] = WEBUI
    digest = write_manifest(tmp_path, document)
    with pytest.raises(ValueError):
        inspect_profile(tmp_path, MANIFEST, digest, "scaffold")


@pytest.mark.parametrize("kind", ["duplicate_json_field", "non_json_number", "broken_json"])
def test_ambiguous_json_refused(tmp_path, kind):
    document = fixture(tmp_path)
    raw = json.dumps(document)
    if kind == "duplicate_json_field": raw = raw.replace('"execution_enabled": false',
                                                          '"execution_enabled": true, "execution_enabled": false', 1)
    elif kind == "non_json_number": raw = raw.replace('"phase0_complete": false', '"phase0_complete": NaN')
    else: raw = raw[:-1]
    (tmp_path / MANIFEST).write_text(raw)
    with pytest.raises(ValueError):
        inspect_profile(tmp_path, MANIFEST, hashlib.sha256(raw.encode()).hexdigest(), "scaffold")


@pytest.mark.parametrize("kind", ["traversal", "absolute", "symlink_file", "symlink_parent", "fifo"])
def test_path_and_blocking_input_refused(tmp_path, kind):
    document = fixture(tmp_path)
    digest = write_manifest(tmp_path, document)
    name = MANIFEST
    if kind == "traversal": name = "../conformance/hosted-profiles.json"
    elif kind == "absolute": name = str(tmp_path / MANIFEST)
    elif kind == "symlink_parent":
        (tmp_path / "alias").symlink_to(tmp_path / "conformance", target_is_directory=True)
        name = "alias/hosted-profiles.json"
    else:
        original = tmp_path / MANIFEST
        original.rename(tmp_path / "original.json")
        if kind == "fifo": os.mkfifo(original)
        else: original.symlink_to(tmp_path / "original.json")
    with pytest.raises((ValueError, OSError)):
        inspect_profile(tmp_path, name, digest, "scaffold")


def test_wrong_target_or_missing_config_refused(tmp_path):
    document = fixture(tmp_path)
    digest = write_manifest(tmp_path, document)
    with pytest.raises(ValueError, match="target_mismatch"):
        inspect_profile(tmp_path, MANIFEST, digest, "scaffold", "http://127.0.0.1:8080")
    (tmp_path / document["profiles"][0]["pytest"]["config"]).unlink()
    with pytest.raises(OSError):
        inspect_profile(tmp_path, MANIFEST, digest, "scaffold")
