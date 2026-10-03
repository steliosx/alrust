"""Pure offline Q1 codec controls; no files, processes, SQL or HTTP."""
import json

import pytest

from alrust_conformance import private_secret_inventory as inventory

CORE = "a" * 64
OTHER_CORE = "b" * 64


def decode(data, count=1, core=CORE):
    return inventory.decode_inventory(data, expected_core_sha256=core,
                                      expected_count=count)


def raw(**changes):
    value = {"schema": inventory.SCHEMA, "core_identity_sha256": CORE,
             "secrets": ["x" * 16]}
    value.update(changes)
    return json.dumps(value).encode()


@pytest.mark.parametrize("values", [[], ["x" * 16], ["x" * 16, "y" * 32]])
def test_complete_private_snapshots(values):
    data = inventory.encode_inventory(CORE, values)
    assert data.endswith(b"\n")
    assert decode(data, len(values)) == values


def test_revoked_key_remains_in_inventory():
    # The codec is not supplied a current database key list: both discoveries
    # persist even when the caller's test subsequently revoked the first key.
    discoveries = ["revoked-" + "x" * 24, "active-" + "y" * 24]
    assert decode(inventory.encode_inventory(CORE, discoveries), 2) == discoveries


@pytest.mark.parametrize("changes,code", [
    ({"schema": "other"}, "inventory_schema_invalid"),
    ({"extra": 1}, "inventory_schema_invalid"),
    ({"core_identity_sha256": OTHER_CORE}, "inventory_core_mismatch"),
    ({"core_identity_sha256": False}, "inventory_core_invalid"),
    ({"secrets": None}, "inventory_secrets_invalid"),
    ({"secrets": [False]}, "inventory_secret_invalid"),
    ({"secrets": [16]}, "inventory_secret_invalid"),
    ({"secrets": ["x" * 15]}, "inventory_secret_invalid"),
    ({"secrets": ["x" * 4097]}, "inventory_secret_invalid"),
    ({"secrets": ["x" * 16, "x" * 16]}, "inventory_secret_repeated"),
    ({"secrets": []}, "inventory_count_mismatch"),
])
def test_declared_snapshot_refusals(changes, code):
    with pytest.raises(inventory.InventoryError, match="^" + code + "$"):
        decode(raw(**changes))


@pytest.mark.parametrize("data", [
    b"", b"null", b"[]", b"{}", b"{", b"\xff",
    b'{"schema":1,"schema":2}', b'{"secrets":[NaN]}',
    b'{"secrets":[Infinity]}', b'{"secrets":[1e999]}',
    b'[' * 1100 + b']' * 1100,
])
def test_malformed_bytes_are_fixed_errors(data):
    with pytest.raises(inventory.InventoryError) as error:
        decode(data)
    assert str(error.value) in {"inventory_json_invalid", "inventory_schema_invalid"}


@pytest.mark.parametrize("value", [False, -1, 257, 1.0, "1", None])
def test_expected_count_is_independent_exact_integer(value):
    with pytest.raises(inventory.InventoryError, match="^inventory_count_invalid$"):
        inventory.decode_inventory(raw(), expected_core_sha256=CORE, expected_count=value)


@pytest.mark.parametrize("value", [False, None, "A" * 64, "a" * 63, "a" * 65])
def test_expected_and_encoded_core_are_typed_pins(value):
    for action in [lambda: inventory.encode_inventory(value, []),
                   lambda: decode(raw(), core=value)]:
        with pytest.raises(inventory.InventoryError, match="^inventory_core_invalid$"):
            action()


@pytest.mark.parametrize("value", [None, (), [False], ["x" * 15], ["\ud800" * 16],
                                   ["x" * 16, "x" * 16]])
def test_writer_refuses_invalid_values_without_echoing(value):
    with pytest.raises(inventory.InventoryError) as error:
        inventory.encode_inventory(CORE, value)
    assert str(error.value) in {"inventory_secrets_invalid", "inventory_secret_invalid",
                                "inventory_secret_repeated"}


def test_unicode_limits_are_bytes_not_characters():
    assert decode(inventory.encode_inventory(CORE, ["é" * 8])) == ["é" * 8]
    with pytest.raises(inventory.InventoryError, match="^inventory_secret_invalid$"):
        inventory.encode_inventory(CORE, ["é" * 2049])
    with pytest.raises(inventory.InventoryError, match="^inventory_secret_invalid$"):
        decode(raw(secrets=["\ud800" * 16]))


def test_all_256_distinct_values_and_oversize_entry_list():
    values = [str(index).zfill(16) for index in range(256)]
    assert decode(inventory.encode_inventory(CORE, values), 256) == values
    with pytest.raises(inventory.InventoryError, match="^inventory_secrets_invalid$"):
        inventory.encode_inventory(CORE, values + ["z" * 16])
    with pytest.raises(inventory.InventoryError, match="^inventory_secrets_invalid$"):
        decode(raw(secrets=values + ["z" * 16]), 256)


def test_whole_byte_budget_refuses_even_valid_individual_values():
    values = [str(index).zfill(4) + "x" * 4092 for index in range(256)]
    with pytest.raises(inventory.InventoryError, match="^inventory_too_large$"):
        inventory.encode_inventory(CORE, values)
    with pytest.raises(inventory.InventoryError, match="^inventory_too_large$"):
        decode(b" " * (inventory.MAX_INVENTORY + 1))


@pytest.mark.parametrize("data", [None, "private text", bytearray(b"{}")])
def test_reader_requires_immutable_bounded_bytes(data):
    with pytest.raises(inventory.InventoryError, match="^inventory_bytes_invalid$"):
        decode(data)


def test_same_count_different_values_are_not_an_inclusion_proof():
    # This deliberately succeeds as a codec test; registry set inclusion is
    # a separate mandatory reader gate. Count equality alone cannot attest it.
    first = decode(inventory.encode_inventory(CORE, ["x" * 16]))
    other_registry = ["y" * 16]
    assert len(first) == len(other_registry)
    assert not set(first) <= set(other_registry)
