"""Named E1a pure controls: bytes/JSON only, no files, processes or transports.

Not auto-discovered by pytest's default test filename patterns. Run only this
explicit file after classifying imports, with autoload disabled/confcutdir here.
"""
from dataclasses import FrozenInstanceError
import hashlib
import json

import pytest

from alrust_conformance import entrypoint_plan as ep


def document():
    identity = dict(repository="steliosx/alrust", job="executing", profile="api-keys",
                    run_id="123", run_attempt="1", event_head_sha="a" * 40,
                    checkout_sha="b" * 40, reference_sha=ep.REFERENCE,
                    manifest_sha256="c" * 64)
    files = {ep.CONFIG: "d" * 64, ep.SELECTION[0]: "e" * 64,
             ep.MANIFEST: identity["manifest_sha256"],
             "conformance/alrust_conformance/proposed_adapter.py": "f" * 64}
    return dict(schema=ep.SCHEMA, identity=identity, target=ep.TARGET,
                execution_enabled=False, files=files,
                pytest=dict(config=ep.CONFIG, selection=list(ep.SELECTION),
                            plugins=["alrust_conformance.proposed_adapter"],
                            plugins_autoload=False, clear_inherited_addopts=True,
                            cwd="repository root"),
                interpreter=dict(path="/job/venv/bin/python", resolved_path="/usr/bin/python3.12",
                                 sha256="0" * 64),
                entrypoint_budget_ns=60_000_000_000, required_roles=list(ep.ROLES))


def wire(doc):
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()


def check(doc, *, data=None, digest=None, identity=None, measured=None):
    raw = wire(doc) if data is None else data
    return ep.validate_plan(raw, hashlib.sha256(raw).hexdigest() if digest is None else digest,
                            expected_identity=document()["identity"] if identity is None else identity,
                            measured_files=doc["files"] if measured is None else measured)


def test_valid_plan_stays_inert_and_detached_from_mutable_inputs():
    doc = document(); measured = dict(doc["files"])
    result = check(doc, measured=measured)
    assert result.status == "NOT_RUN" and result.execution_enabled is False
    assert result.scope == "INPUT_DECLARATIONS_ONLY_NOT_EXECUTION_OR_MEASUREMENT_PROOF"
    assert result.files == tuple(sorted(doc["files"].items()))
    old = result.identity, result.files, result.plugins, result.interpreter
    doc["identity"]["job"] = "changed"; doc["files"].clear(); measured.clear()
    doc["pytest"]["plugins"].clear(); doc["interpreter"].clear()
    assert (result.identity, result.files, result.plugins, result.interpreter) == old
    with pytest.raises(FrozenInstanceError): result.execution_enabled = True


@pytest.mark.parametrize("field", ["job", "run_id", "run_attempt", "event_head_sha", "checkout_sha",
                                   "manifest_sha256"])
def test_other_invocation_identity_refuses(field):
    doc = document(); doc["identity"][field] = "2" if field in ("run_id", "run_attempt") else (
        "otherjob" if field == "job" else "9" * (64 if field == "manifest_sha256" else 40))
    with pytest.raises(ep.PlanError, match="^identity_mismatch$"): check(doc)


@pytest.mark.parametrize("fault", ["schema", "extra", "enabled", "numeric_enabled", "target",
                                   "roles_empty", "roles_repeat", "roles_wrong_order",
                                   "reference", "profile", "bool_attempt", "zero_run",
                                   "budget_bool", "budget_small", "budget_huge"])
def test_unapproved_or_malformed_policy_never_completes(fault):
    doc = document()
    if fault == "schema": doc["schema"] = "unknown"
    elif fault == "extra": doc["command"] = "private-untrusted-text"
    elif fault == "enabled": doc["execution_enabled"] = True
    elif fault == "numeric_enabled": doc["execution_enabled"] = 0
    elif fault == "target": doc["target"] = "http://127.0.0.1:9999"
    elif fault == "roles_empty": doc["required_roles"] = []
    elif fault == "roles_repeat": doc["required_roles"][1] = doc["required_roles"][0]
    elif fault == "roles_wrong_order": doc["required_roles"].reverse()
    elif fault == "reference": doc["identity"]["reference_sha"] = "9" * 40
    elif fault == "profile": doc["identity"]["profile"] = "scaffold"
    elif fault == "bool_attempt": doc["identity"]["run_attempt"] = True
    elif fault == "zero_run": doc["identity"]["run_id"] = "0"
    elif fault == "budget_bool": doc["entrypoint_budget_ns"] = True
    elif fault == "budget_small": doc["entrypoint_budget_ns"] = ep.MIN_TEARDOWN_NS
    elif fault == "budget_huge": doc["entrypoint_budget_ns"] = ep.MAX_BUDGET_NS + 1
    with pytest.raises(ep.PlanError): check(doc)


@pytest.mark.parametrize("field,value", [("config", "conformance/pytest.ini"),
    ("selection", []), ("selection", [ep.SELECTION[0], ep.SELECTION[0]]),
    ("plugins_autoload", True), ("plugins_autoload", 0), ("clear_inherited_addopts", False),
    ("clear_inherited_addopts", 1), ("cwd", "runner home"),
    ("plugins", []), ("plugins", ["os"]),
    ("plugins", ["alrust_conformance.proposed-adapter"]),
    ("plugins", ["alrust_conformance.proposed_adapter"] * 2)])
def test_exact_selection_and_explicit_plugin_policy(field, value):
    doc = document(); doc["pytest"][field] = value
    with pytest.raises(ep.PlanError): check(doc)


@pytest.mark.parametrize("path", ["../escape.py", "conformance/../escape.py", "/conformance/a.py",
                                   "conformance//a.py", "conformance/./a.py", "other/a.py",
                                   "conformance/a\\b.py", "conformance/a\x00b.py"])
def test_outside_or_ambiguous_file_paths_refuse(path):
    doc = document(); doc["files"][path] = "a" * 64
    with pytest.raises(ep.PlanError, match="^files_invalid$"): check(doc)


@pytest.mark.parametrize("fault", ["changed", "missing", "extra", "boolean", "uppercase"])
def test_independent_measurement_declarations_must_be_exact(fault):
    doc = document(); measured = dict(doc["files"])
    if fault == "changed": measured[ep.CONFIG] = "a" * 64
    elif fault == "missing": measured.pop(ep.CONFIG)
    elif fault == "extra": measured["conformance/extra.py"] = "a" * 64
    elif fault == "boolean": measured[ep.CONFIG] = True
    else: measured[ep.CONFIG] = "A" * 64
    with pytest.raises(ep.PlanError): check(doc, measured=measured)


@pytest.mark.parametrize("file", [ep.CONFIG, ep.SELECTION[0], ep.MANIFEST,
                                  "conformance/alrust_conformance/proposed_adapter.py"])
def test_required_pinned_file_not_omitted(file):
    doc = document(); del doc["files"][file]
    with pytest.raises(ep.PlanError): check(doc)


@pytest.mark.parametrize("field,value", [("path", "relative/python"), ("path", "/a/../python"),
    ("resolved_path", "/a//python"), ("sha256", "hash-of-path"), ("sha256", True)])
def test_interpreter_declarations_are_typed_and_normalized(field, value):
    doc = document(); doc["interpreter"][field] = value
    with pytest.raises(ep.PlanError, match="^interpreter_invalid$"): check(doc)


@pytest.mark.parametrize("raw", [b'{"schema":1,"schema":2}', b'{"a":NaN}', b'{"a":Infinity}',
                                 b'{"a":1.0}', b'\xff', b'[' * 1500 + b']' * 1500],
                         ids=['duplicate', 'nan', 'infinity', 'float', 'utf8', 'depth'])
def test_strict_json_rejects_duplicate_nonfinite_float_encoding_depth(raw):
    with pytest.raises(ep.PlanError, match="^plan_json_invalid$"): check(document(), data=raw)


def test_plan_pin_and_size_precede_json_interpretation():
    with pytest.raises(ep.PlanError, match="^plan_digest_mismatch$"):
        check(document(), data=b'not json', digest="a" * 64)
    for data in [b'', b'a' * (ep.LIMIT + 1), bytearray(b'{}')]:
        with pytest.raises(ep.PlanError, match="^plan_input_invalid$"):
            ep.validate_plan(data, "a" * 64, expected_identity={}, measured_files={})


def deadlines():
    return dict(start_ns=100_000_000_000, job_deadline_ns=1000_000_000_000,
                cleanup_reserve_ns=480_000_000_000, exit_reserve_ns=30_000_000_000,
                deadline_ns=150_000_000_000, client_deadline_ns=145_000_000_000)


def test_declared_work_deadline_does_not_extend_or_create_admission():
    plan = check(document()); args = deadlines()
    assert ep.validate_work_deadline(plan, **args) == args["deadline_ns"]
    assert plan.status == "NOT_RUN" and plan.execution_enabled is False


@pytest.mark.parametrize("field,value", [("start_ns", True), ("deadline_ns", 0),
    ("exit_reserve_ns", ep.MIN_TEARDOWN_NS - 1), ("client_deadline_ns", 151_000_000_000),
    ("client_deadline_ns", 100_000_000_000), ("deadline_ns", 161_000_000_000),
    ("deadline_ns", 115_000_000_000), ("job_deadline_ns", 520_000_000_000),
    ("cleanup_reserve_ns", ep.MIN_CLEANUP_RESERVE_NS - 1),
    ("job_deadline_ns", 100_000_000_000 + ep.MAX_BUDGET_NS + 1)])
def test_late_client_expired_work_budget_or_missing_reserve_refuses(field, value):
    plan = check(document()); args = deadlines(); args[field] = value
    with pytest.raises(ep.PlanError): ep.validate_work_deadline(plan, **args)


def test_plain_fixed_diagnostics_do_not_echo_untrusted_data():
    doc = document(); doc["target"] = "SYNTHETIC-private-untrusted-credential"
    with pytest.raises(ep.PlanError) as caught: check(doc)
    assert str(caught.value) == "target_invalid"
    assert "SYNTHETIC" not in repr(caught.value)


def test_depth_bound_ignores_escaped_quotes_and_brackets_in_strings():
    # Independent wire-parser control, not an approved executable-plan field.
    raw = b'{"s":"[ { \\" ] }"}'
    ep._wire_depth(raw)
    assert json.loads(raw)["s"] == '[ { " ] }'
    with pytest.raises(ep.PlanError, match="^plan_json_invalid$"):
        ep._wire_depth(b'[' * (ep.MAX_DEPTH + 1) + b']' * (ep.MAX_DEPTH + 1))
