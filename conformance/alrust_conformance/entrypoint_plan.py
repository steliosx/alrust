"""E1a: pure, inert input-plan validation; no measurement or execution.

Expected plan hash/identity and measured-file declarations are independent caller
inputs. This function cannot authenticate them or read the named files. Success
always stays NOT_RUN and cannot approve a workflow, profile, current process,
credential provenance, closed prefix or resource window. No import-time I/O.
"""
from dataclasses import dataclass
import hashlib
import json
import re

SCHEMA = "alrust-entrypoint-plan/1"
LIMIT = 65536
MAX_FILES = 64
MAX_DEPTH = 16
REFERENCE = "f4312727783dad64198b62ed778bbca98a5144a3"
TARGET = "http://127.0.0.1:18081"
PROFILE = "api-keys"
CONFIG = "conformance/api-keys-pytest.ini"
SELECTION = ("conformance/tests/http/test_api_keys.py",)
MANIFEST = "conformance/hosted-profiles.json"
ROLES = ("unscoped_admin", "a_admin", "a_writer", "b_writer")
IDENTITY = frozenset({"repository", "job", "profile", "run_id", "run_attempt",
                      "event_head_sha", "checkout_sha", "reference_sha", "manifest_sha256"})
PLAN_KEYS = frozenset({"schema", "identity", "target", "execution_enabled", "files",
                       "pytest", "interpreter", "entrypoint_budget_ns", "required_roles"})
PYTEST_KEYS = frozenset({"config", "selection", "plugins", "plugins_autoload",
                         "clear_inherited_addopts", "cwd"})
MIN_TEARDOWN_NS = 16_000_000_000
MIN_CLEANUP_RESERVE_NS = 8 * 60 * 1_000_000_000
MAX_BUDGET_NS = 30 * 60 * 1_000_000_000
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_GIT = re.compile(r"[0-9a-f]{40}\Z")
_NAME = re.compile(r"[a-zA-Z0-9_.-]{1,128}\Z")
_COMPONENT = re.compile(r"[a-zA-Z0-9_-][a-zA-Z0-9_.-]{0,127}\Z")


class PlanError(ValueError):
    """Fixed diagnostics only; never stringify untrusted input."""


def _require(condition, code):
    if not condition:
        raise PlanError(code) from None


def _digest(value):
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _integer(value, low=1, high=(1 << 63) - 1):
    return type(value) is int and low <= value <= high


def _relative(value):
    return (type(value) is str and 0 < len(value) <= 4096
            and all(_COMPONENT.fullmatch(p) and p not in (".", "..")
                    for p in value.split("/")))


def _absolute(value):
    return (type(value) is str and value.startswith("/") and len(value) <= 4096
            and _relative(value[1:]))


def _identity(value):
    _require(type(value) is dict and len(value) == len(IDENTITY) and set(value) == IDENTITY, "identity_invalid")
    _require(type(value["repository"]) is str
             and value["repository"] == "steliosx/alrust"
             and type(value["job"]) is str and _NAME.fullmatch(value["job"])
             and type(value["profile"]) is str and value["profile"] == PROFILE,
             "identity_invalid")
    for key in ("run_id", "run_attempt"):
        v = value[key]
        _require(type(v) is str and re.fullmatch(r"[1-9][0-9]{0,19}", v), "identity_invalid")
    for key in ("event_head_sha", "checkout_sha", "reference_sha"):
        _require(type(value[key]) is str and _GIT.fullmatch(value[key]), "identity_invalid")
    _require(value["reference_sha"] == REFERENCE and _digest(value["manifest_sha256"]),
             "identity_invalid")
    return tuple(sorted(value.items()))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "plan_json_invalid")
        result[key] = value
    return result


def _number(_):
    raise PlanError("plan_json_invalid")


def _wire_depth(data):
    """Reject excessive structural nesting BEFORE the JSON decoder recurses."""
    depth = 0
    quoted = escaped = False
    for byte in data:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (123, 91):
            depth += 1
            _require(depth <= MAX_DEPTH, "plan_json_invalid")
        elif byte in (125, 93):
            depth -= 1
            _require(depth >= 0, "plan_json_invalid")


def _depth(obj):
    """Bound parser results independently of the interpreter recursion limit."""
    pending = [(obj, 0)]
    while pending:
        value, depth = pending.pop()
        _require(depth <= MAX_DEPTH, "plan_json_invalid")
        if type(value) is dict:
            pending.extend((v, depth + 1) for v in value.values())
        elif type(value) is list:
            pending.extend((v, depth + 1) for v in value)



@dataclass(frozen=True)
class PlanInputs:
    plan_sha256: str
    identity: tuple
    files: tuple
    plugins: tuple
    interpreter: tuple
    entrypoint_budget_ns: int
    status: str = "NOT_RUN"
    execution_enabled: bool = False
    scope: str = "INPUT_DECLARATIONS_ONLY_NOT_EXECUTION_OR_MEASUREMENT_PROOF"


def validate_plan(data, expected_sha256, *, expected_identity, measured_files):
    """Validate exact bytes and independent declarations, without file access.

    Supports only the candidate api-keys plan, always disabled. FILES is the
    complete explicitly reviewed configuration/fixture/plugin dependency list;
    this component does not discover its transitive closure. The caller must
    supply separately observed digests at the later reviewed measurement boundary.
    A disabled valid plan is an inert prepared input, never admission or PASS.
    """
    _require(type(data) is bytes and 0 < len(data) <= LIMIT and _digest(expected_sha256),
             "plan_input_invalid")
    _require(hashlib.sha256(data).hexdigest() == expected_sha256, "plan_digest_mismatch")
    identity = _identity(expected_identity)
    _wire_depth(data)
    try:
        obj = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                         parse_float=_number, parse_constant=_number)
    except (ValueError, UnicodeError, RecursionError):
        raise PlanError("plan_json_invalid") from None
    _depth(obj)
    _require(type(obj) is dict and set(obj) == PLAN_KEYS and obj["schema"] == SCHEMA
             and type(obj["schema"]) is str, "plan_schema_invalid")
    _require(_identity(obj["identity"]) == identity, "identity_mismatch")
    _require(type(obj["target"]) is str and obj["target"] == TARGET, "target_invalid")
    _require(obj["execution_enabled"] is False, "plan_inert_only")
    _require(type(obj["required_roles"]) is list
             and tuple(obj["required_roles"]) == ROLES
             and all(type(v) is str for v in obj["required_roles"]), "roles_invalid")
    files = obj["files"]
    _require(type(files) is dict and 1 <= len(files) <= MAX_FILES
             and all(_relative(k) and k.startswith("conformance/") and _digest(v)
                     for k, v in files.items()), "files_invalid")
    _require(type(measured_files) is dict and len(measured_files) == len(files)
             and set(measured_files) == set(files)
             and all(_digest(v) for v in measured_files.values()), "measurements_invalid")
    _require(files == measured_files, "file_digest_mismatch")
    _require(CONFIG in files and all(p in files for p in SELECTION)
             and files.get(MANIFEST) == obj["identity"]["manifest_sha256"],
             "required_file_missing")
    pt = obj["pytest"]
    _require(type(pt) is dict and set(pt) == PYTEST_KEYS
             and type(pt["config"]) is str and pt["config"] == CONFIG
             and type(pt["selection"]) is list and tuple(pt["selection"]) == SELECTION
             and all(type(v) is str for v in pt["selection"])
             and pt["plugins_autoload"] is False and pt["clear_inherited_addopts"] is True
             and type(pt["cwd"]) is str and pt["cwd"] == "repository root",
             "pytest_policy_invalid")
    plugins = pt["plugins"]
    _require(type(plugins) is list and 1 <= len(plugins) <= 8
             and all(type(v) is str and v.startswith("alrust_conformance.")
                     and all(re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,127}", p)
                             for p in v.split(".")) for v in plugins), "plugins_invalid")
    _require(len(set(plugins)) == len(plugins)
             and all("conformance/" + p.replace(".", "/") + ".py" in files
                     for p in plugins), "plugin_file_missing")
    interp = obj["interpreter"]
    _require(type(interp) is dict and set(interp) == {"path", "resolved_path", "sha256"}
             and _absolute(interp["path"]) and _absolute(interp["resolved_path"])
             and _digest(interp["sha256"]), "interpreter_invalid")
    _require(_integer(obj["entrypoint_budget_ns"], MIN_TEARDOWN_NS + 1, MAX_BUDGET_NS),
             "budget_invalid")
    return PlanInputs(expected_sha256, identity, tuple(sorted(files.items())),
                      tuple(plugins), tuple(sorted(interp.items())), obj["entrypoint_budget_ns"])


def validate_work_deadline(plan, *, start_ns, job_deadline_ns, cleanup_reserve_ns,
                           exit_reserve_ns, deadline_ns, client_deadline_ns):
    """Check declared ENTRYPOINT work-phase deadlines only; never read a clock.

    Registry/drop/finalizer cleanup deadlines need their own phase budget; this
    function must not be reused for them. Inputs need independent same-boot clock
    observation at a later invocation boundary. No deadline/lease is created,
    renewed or extended, and no current generation is attested by this check.
    """
    _require(type(plan) is PlanInputs
             and _integer(plan.entrypoint_budget_ns, MIN_TEARDOWN_NS + 1, MAX_BUDGET_NS),
             "plan_required")
    values = (start_ns, job_deadline_ns, cleanup_reserve_ns, exit_reserve_ns,
              deadline_ns, client_deadline_ns)
    _require(all(_integer(v) for v in values)
             and exit_reserve_ns >= MIN_TEARDOWN_NS
             and cleanup_reserve_ns >= MIN_CLEANUP_RESERVE_NS
             and 0 < job_deadline_ns - start_ns <= MAX_BUDGET_NS, "deadline_invalid")
    ceiling = min(start_ns + plan.entrypoint_budget_ns,
                  job_deadline_ns - cleanup_reserve_ns - exit_reserve_ns)
    _require(start_ns + MIN_TEARDOWN_NS <= deadline_ns <= ceiling
             and start_ns < client_deadline_ns <= deadline_ns, "deadline_reserve_invalid")
    return deadline_ns
