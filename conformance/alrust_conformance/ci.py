"""Offline hosted-profile preflight. This revision contains no executor."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat

REFERENCE = "f4312727783dad64198b62ed778bbca98a5144a3"
WEBUI = "2687e4afa93b41c4fa1d46201bba9ea38be0d491"
TARGET = "http://127.0.0.1:18081"
LIMIT = 512 * 1024


def _file(root: Path, name: str) -> bytes:
    """Bounded regular-file reads without following any repository symlink."""
    if not isinstance(name, str) or "\\" in name or any(
        part in {"", ".", ".."} for part in name.split("/")
    ):
        raise ValueError("invalid_repository_path")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = name.split("/")
        for part in parts[:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
            os.close(directory)
            directory = following
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                     dir_fd=directory)
        try:
            observed = os.fstat(fd)
            if not stat.S_ISREG(observed.st_mode) or observed.st_size > LIMIT:
                raise ValueError("input_not_bounded_regular_file")
            data = bytearray()
            while len(data) <= LIMIT:
                chunk = os.read(fd, min(65536, LIMIT + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > LIMIT:
                raise ValueError("input_not_bounded_regular_file")
            return bytes(data)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_field")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("non_json_number")


def inspect_profile(root: Path, manifest: str, expected_sha256: str,
                    profile: str, target: str = TARGET) -> dict:
    """Validate inert inventory inputs; never authorize or execute a profile."""
    if not isinstance(profile, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", profile):
        raise ValueError("invalid_profile_id")
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("invalid_manifest_digest")
    raw = _file(root, manifest)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("manifest_digest_mismatch")
    try:
        document = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("invalid_manifest_json") from exc
    if not isinstance(document, dict) or document.get("schema") != "alrust-hosted-profile-inventory/1":
        raise ValueError("invalid_manifest_schema")
    if document.get("reference_commit") != REFERENCE or document.get("reference_webui_commit") != WEBUI:
        raise ValueError("reference_pin_mismatch")
    if any(document.get(key) is not False for key in
           ("execution_enabled", "executor_implemented", "phase0_complete")):
        raise ValueError("inert_entrypoint_only")
    policy = document.get("gate_policy")
    if not isinstance(policy, dict) or any(policy.get(key) is not True for key in
            ("all_listed_modules_required", "not_run_keeps_gate_open")) or policy.get(
                "existing_modules_pass_sufficient") is not False:
        raise ValueError("gate_policy_mismatch")
    profiles = document.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("empty_profile_inventory")
    by_id, by_module = {}, {}
    for item in profiles:
        if not isinstance(item, dict):
            raise ValueError("invalid_profile")
        ident, module = item.get("id"), item.get("module")
        if not isinstance(ident, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", ident):
            raise ValueError("invalid_profile_id")
        if ident in by_id or not isinstance(module, str) or module in by_module:
            raise ValueError("ambiguous_profile_inventory")
        if not module.startswith("conformance/tests/http/") or not module.endswith(".py"):
            raise ValueError("invalid_test_selection")
        if item.get("gate_required") is not True or any(item.get(key) is not False for key in
                ("execution_enabled", "hosted_runnable_as_is", "test_changes_proposed")) or item.get(
                    "hosted_result") != "NOT_RUN":
            raise ValueError("inert_entrypoint_only")
        if item.get("target") != TARGET:
            raise ValueError("profile_target_mismatch")
        if hashlib.sha256(_file(root, module)).hexdigest() != item.get("module_sha256"):
            raise ValueError("module_digest_mismatch")
        selection = item.get("pytest")
        if not isinstance(selection, dict) or selection.get("selection") != [module] or selection.get(
                "plugins_autoload") is not False or selection.get("clear_inherited_addopts") is not True or selection.get(
                    "cwd") != "repository root":
            raise ValueError("invalid_test_selection")
        _file(root, selection.get("config"))
        _file(root, item.get("contract"))
        by_id[ident], by_module[module] = item, item
    for item in profiles:
        dependencies = item.get("fixture_imports")
        if not isinstance(dependencies, list) or any(
            not isinstance(path, str) or path not in by_module for path in dependencies
        ):
            raise ValueError("unbound_fixture_dependency")
    if profile not in by_id:
        raise ValueError("unknown_profile")
    if target != TARGET:
        raise ValueError("target_mismatch")
    item = by_id[profile]
    return {"schema": "alrust-ci-preflight/1", "status": "NOT_RUN",
            "reason": "profile_disabled_executor_not_implemented", "profile": profile,
            "manifest_sha256": expected_sha256, "module_sha256": item["module_sha256"],
            "selection": item["pytest"]["selection"], "target": TARGET,
            "execution_started": False, "phase0_complete": False, "artifact_allowlist": []}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--manifest", default="conformance/hosted-profiles.json")
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--target", default=TARGET)
    args = parser.parse_args(argv)
    try:
        result = inspect_profile(Path(__file__).resolve().parents[2], args.manifest,
                                 args.manifest_sha256, args.profile, args.target)
    except (OSError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else "input_unreadable"
        result = {"schema": "alrust-ci-preflight/1", "status": "FAIL", "reason": reason,
                  "execution_started": False, "artifact_allowlist": []}
        print(json.dumps(result, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
