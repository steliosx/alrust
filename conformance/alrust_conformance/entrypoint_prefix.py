"""Pure E1b preparation: exact independently supplied prefix expectations.

This validates declarations, not files or current processes. No reader, journal,
plugin, clock, service or supervisor is imported or invoked. Valid inputs stay
NOT_RUN; a constructed result is never an authorization or measurement token.
"""
from dataclasses import dataclass
import re

STAGES = ("database-create", "database-create-exit", "launch", "launch-confirm")
ENTRYPOINT_INDEX = 4
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class PrefixInputError(ValueError):
    """Fixed diagnostics only, without input values."""


def _require(condition, code):
    if not condition:
        raise PrefixInputError(code) from None


def _digest(value):
    return type(value) is str and _DIGEST.fullmatch(value) is not None


@dataclass(frozen=True)
class PrefixInputs:
    stages: tuple
    core_sha256: str
    launch_sha256: str
    launcher_exit_code: int
    confirmation_exit_code: int
    entrypoint_index: int = ENTRYPOINT_INDEX
    status: str = "NOT_RUN"
    execution_enabled: bool = False
    scope: str = "PREFIX_EXPECTATIONS_ONLY_NOT_CLOSED_BYTES_OR_PROCESS_QUALIFICATION"


def validate_prefix_inputs(stages, *, core_sha256, launch_sha256,
                           launcher_exit_code, confirmation_exit_code):
    """Freeze the four-stage expectations supplied by the authenticated caller.

    Each digest must be retained independently from the original producer, not
    recomputed from evidence and then selected as its own expected value. The
    exit codes must come from the actual invocations, not a journal or receipt.
    This pure function cannot authenticate either source. It deliberately
    excludes the legacy two-stage prefix allowed by read_closed_launch.

    A later reviewed adapter must first read bindings and the exact closed
    prefix under held no-follow ancestors, compare the independent core/launch
    digests and actual exits, and only then open entrypoint index4. No adapter
    selects this component yet. Success never opens or executes anything.
    """
    _require(type(stages) is tuple and len(stages) == len(STAGES)
             and all(type(item) is tuple and len(item) == 2
                     and type(item[0]) is str and _digest(item[1])
                     for item in stages), "prefix_shape_invalid")
    _require(tuple(item[0] for item in stages) == STAGES, "prefix_order_invalid")
    _require(_digest(core_sha256) and _digest(launch_sha256), "binding_digest_invalid")
    _require(type(launcher_exit_code) is int and launcher_exit_code == 0
             and type(confirmation_exit_code) is int and confirmation_exit_code == 0,
             "invocation_not_successful")
    return PrefixInputs(tuple((name, digest) for name, digest in stages),
                        core_sha256, launch_sha256,
                        launcher_exit_code, confirmation_exit_code)
