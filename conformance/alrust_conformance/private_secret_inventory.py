"""Private Q1 snapshot codec; no files, provisioning, HTTP or stage execution.

These bytes contain secrets. They must never be printed, published or sent
through the mailbox. The caller supplies independently verified core/closed
stage bindings; successful decoding does not prove completeness or quiescence.
"""
import json
import re

from .stage_journal import JournalError, strict_json

SCHEMA = "alrust-entrypoint-secrets/1"
MAX_SECRETS = 256
MIN_SECRET = 16
MAX_SECRET = 4096
MAX_INVENTORY = 1 << 20
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = frozenset({"schema", "core_identity_sha256", "secrets"})


class InventoryError(ValueError):
    """Fixed codes only; never include rejected values or parser exceptions."""


def _require(condition, code):
    if not condition:
        raise InventoryError(code)


def _core_digest(value):
    _require(type(value) is str and _SHA256.fullmatch(value), "inventory_core_invalid")


def _secrets(values):
    _require(type(values) is list and len(values) <= MAX_SECRETS,
             "inventory_secrets_invalid")
    seen = set()
    for value in values:
        _require(type(value) is str, "inventory_secret_invalid")
        try:
            length = len(value.encode("utf-8"))
        except UnicodeError:
            raise InventoryError("inventory_secret_invalid") from None
        _require(MIN_SECRET <= length <= MAX_SECRET, "inventory_secret_invalid")
        _require(value not in seen, "inventory_secret_repeated")
        seen.add(value)


def encode_inventory(core_identity_sha256, secrets):
    """Encode a complete private snapshot; caller retains first-seen order.

    An empty list is explicit, not a missing-file fallback. Duplicate values
    refuse; the discovery writer must retain each distinct value exactly once.
    This function neither commits a file nor certifies a stage's completion.
    """
    _core_digest(core_identity_sha256)
    _secrets(secrets)
    data = (json.dumps({"schema": SCHEMA,
                        "core_identity_sha256": core_identity_sha256,
                        "secrets": secrets}, sort_keys=True, ensure_ascii=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")
    _require(len(data) <= MAX_INVENTORY, "inventory_too_large")
    return data


def decode_inventory(data, *, expected_core_sha256, expected_count):
    """Decode bounded bytes against independent closed-stage expectations.

    The registry reader must first pass writer quiescence, verify the held
    file identity/hash and independently validate the core and entrypoint
    journal. Passing those checks is not this codec's responsibility.
    Returns private values only; callers must not log the result or repr it.
    """
    _core_digest(expected_core_sha256)
    _require(type(expected_count) is int and 0 <= expected_count <= MAX_SECRETS,
             "inventory_count_invalid")
    _require(type(data) is bytes, "inventory_bytes_invalid")
    _require(len(data) <= MAX_INVENTORY, "inventory_too_large")
    try:
        value = strict_json(data, "inventory_json_invalid")
    except JournalError:
        raise InventoryError("inventory_json_invalid") from None
    _require(type(value) is dict and set(value) == _FIELDS
             and value["schema"] == SCHEMA, "inventory_schema_invalid")
    _core_digest(value["core_identity_sha256"])
    _require(value["core_identity_sha256"] == expected_core_sha256,
             "inventory_core_mismatch")
    _secrets(value["secrets"])
    _require(len(value["secrets"]) == expected_count, "inventory_count_mismatch")
    return value["secrets"]
