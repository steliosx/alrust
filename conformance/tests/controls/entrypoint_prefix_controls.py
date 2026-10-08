"""Named pure prefix controls; no closed reader or process-capable imports."""
from dataclasses import FrozenInstanceError

import pytest

from alrust_conformance import entrypoint_prefix as ep


def stages():
    return tuple(zip(ep.STAGES, ("a" * 64, "b" * 64, "c" * 64, "d" * 64)))


def check(prefix=None, **overrides):
    args = dict(core_sha256="e" * 64, launch_sha256="f" * 64,
                launcher_exit_code=0, confirmation_exit_code=0)
    args.update(overrides)
    return ep.validate_prefix_inputs(stages() if prefix is None else prefix, **args)


def test_exact_expectations_remain_inert_and_immutable():
    result = check()
    assert result.stages == stages()
    assert result.core_sha256 == "e" * 64 and result.launch_sha256 == "f" * 64
    assert result.launcher_exit_code == result.confirmation_exit_code == 0
    assert result.entrypoint_index == 4
    assert result.status == "NOT_RUN" and result.execution_enabled is False
    assert result.scope == "PREFIX_EXPECTATIONS_ONLY_NOT_CLOSED_BYTES_OR_PROCESS_QUALIFICATION"
    with pytest.raises(FrozenInstanceError):
        result.execution_enabled = True


@pytest.mark.parametrize("prefix", [(), stages()[-2:], stages()[:3],
    stages() + (("entrypoint", "1" * 64),), list(stages()),
    tuple(list(x) for x in stages()), (("launch",),) + stages()[1:],
    ((True, "a" * 64),) + stages()[1:]])
def test_missing_extra_legacy_and_malformed_stages_refuse(prefix):
    with pytest.raises(ep.PrefixInputError, match="^prefix_shape_invalid$"):
        check(prefix)


@pytest.mark.parametrize("prefix", [stages()[::-1],
    (stages()[1], stages()[0], *stages()[2:]),
    (stages()[0], stages()[0], *stages()[2:]),
    (("private-untrusted-stage", "a" * 64), *stages()[1:])])
def test_reordered_duplicate_or_other_stage_cannot_choose_index(prefix):
    with pytest.raises(ep.PrefixInputError, match="^prefix_order_invalid$"):
        check(prefix)


@pytest.mark.parametrize("digest", [None, True, 1, b"a" * 64, "A" * 64,
                                    "a" * 63, "a" * 65, "a" * 64 + "\n"])
def test_bad_original_stage_digest_refuses(digest):
    with pytest.raises(ep.PrefixInputError, match="^prefix_shape_invalid$"):
        check((("database-create", digest), *stages()[1:]))


@pytest.mark.parametrize("field", ["core_sha256", "launch_sha256"])
@pytest.mark.parametrize("digest", [False, b"a" * 64, "A" * 64, "a" * 64 + "\n"])
def test_bad_original_binding_digest_refuses(field, digest):
    with pytest.raises(ep.PrefixInputError, match="^binding_digest_invalid$"):
        check(**{field: digest})


@pytest.mark.parametrize("field", ["launcher_exit_code", "confirmation_exit_code"])
@pytest.mark.parametrize("exit_code", [False, True, "0", None, -9, 1])
def test_unsuccessful_or_coerced_actual_exit_refuses(field, exit_code):
    with pytest.raises(ep.PrefixInputError, match="^invocation_not_successful$"):
        check(**{field: exit_code})


def test_fixed_refusal_never_echoes_untrusted_values():
    with pytest.raises(ep.PrefixInputError) as error:
        check((("SYNTHETIC_PRIVATE_MARKER", "a" * 64), *stages()[1:]))
    assert str(error.value) == "prefix_order_invalid"
