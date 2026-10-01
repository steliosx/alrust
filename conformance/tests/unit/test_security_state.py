from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest

from alrust_conformance.security_evidence import CaseEvidence
from alrust_conformance.security_state import (
    SnapshotPhase, StatePurpose, StateScope, StoredSnapshot, check_stored_state,
)


def scope(purpose=StatePurpose.ANONYMOUS_REFUSAL):
    return StateScope('AN1-anonymous-write', 'attempt-1', 'candidate', 'a' * 64, purpose,
                      frozenset({'alerts', 'keys'}))


def rows():
    return {'alerts': {'owned': {'resource': 'synthetic', 'count': 1,
                                'history': ['first', 'second'], 'optional': None}},
            'keys': {}}


def snapshots(before=None, after=None, expected=None):
    expected = expected or scope()
    return (StoredSnapshot(expected, SnapshotPhase.BASELINE, rows() if before is None else before),
            StoredSnapshot(expected, SnapshotPhase.OBSERVED, rows() if after is None else after))


@pytest.mark.parametrize('purpose', list(StatePurpose))
def test_complete_unchanged_inventory(purpose):
    expected = scope(purpose)
    assert check_stored_state(*snapshots(expected=expected), expected_scope=expected) == ()


def test_frozen_baseline_survives_in_place_collector_mutation():
    mutable = rows()
    baseline = StoredSnapshot(scope(), SnapshotPhase.BASELINE, mutable)
    mutable['alerts']['owned']['history'].append('mutation')
    observed = StoredSnapshot(scope(), SnapshotPhase.OBSERVED, mutable)
    assert check_stored_state(baseline, observed, expected_scope=scope()) == ('stored_state_changed',)
    with pytest.raises(FrozenInstanceError):
        baseline.phase = SnapshotPhase.OBSERVED


@pytest.mark.parametrize('change', ['insert', 'delete', 'update', 'reorder_array',
                                   'null_to_absent', 'bool_for_int', 'float_for_int',
                                   'new_field', 'key_usage'])
def test_rejects_every_observed_change(change):
    after = rows()
    row = after['alerts']['owned']
    if change == 'insert':
        after['alerts']['unexpected'] = deepcopy(row)
    elif change == 'delete':
        del after['alerts']['owned']
    elif change == 'update':
        row['resource'] = 'changed'
    elif change == 'reorder_array':
        row['history'].reverse()
    elif change == 'null_to_absent':
        del row['optional']
    elif change == 'bool_for_int':
        row['count'] = True
    elif change == 'float_for_int':
        row['count'] = 1.0
    elif change == 'new_field':
        row['unexpected'] = None
    else:
        after['keys']['synthetic-key-id'] = {'lastUsedTime': 'changed'}
    assert check_stored_state(*snapshots(after=after), expected_scope=scope()) == ('stored_state_changed',)


def test_mapping_order_is_not_a_change():
    after = dict(reversed(list(rows().items())))
    after['alerts']['owned'] = dict(reversed(list(after['alerts']['owned'].items())))
    assert check_stored_state(*snapshots(after=after), expected_scope=scope()) == ()


def test_explicit_complete_empty_inventory_is_valid():
    empty = {'alerts': {}, 'keys': {}}
    assert check_stored_state(*snapshots(empty, empty), expected_scope=scope()) == ()


@pytest.mark.parametrize('value', [None, {}, {'alerts': {}}, {'alerts': {}, 'keys': {}, 'users': {}}])
def test_missing_or_extra_inventory_is_not_empty_success(value):
    with pytest.raises(ValueError, match='inventory'):
        StoredSnapshot(scope(), SnapshotPhase.BASELINE, value)


@pytest.mark.parametrize('records', [[], None, {'x': None}, {'': {}}, {1: {}}, {'x': []}])
def test_malformed_rows_fail(records):
    with pytest.raises(ValueError, match='state rows'):
        StoredSnapshot(scope(), SnapshotPhase.BASELINE, {'alerts': records, 'keys': {}})


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf'), (1, 2),
                                  b'private', {1: 'coerced'}, object()])
def test_non_json_or_lossy_values_are_refused(value):
    data = rows()
    data['alerts']['owned']['value'] = value
    with pytest.raises(ValueError, match='invalid stored state values'):
        StoredSnapshot(scope(), SnapshotPhase.BASELINE, data)


def test_cyclic_data_is_refused():
    data = rows()
    data['alerts']['owned']['cycle'] = data
    with pytest.raises(ValueError, match='invalid stored state values'):
        StoredSnapshot(scope(), SnapshotPhase.BASELINE, data)


@pytest.mark.parametrize('field,value', [
    ('case_id', ''), ('case_id', 'bad\nlabel'), ('attempt_id', ''), ('side', 'other'),
    ('fixture_digest', 'a' * 63), ('fixture_digest', 'A' * 64),
    ('purpose', 'anonymous_refusal'), ('collections', frozenset()),
    ('collections', {'alerts', 'keys'}), ('collections', frozenset({'bad/name'})),
])
def test_invalid_expected_scopes_fail(field, value):
    with pytest.raises(ValueError, match='invalid reviewed state scope'):
        replace(scope(), **{field: value})


@pytest.mark.parametrize('field,value', [
    ('case_id', 'other-case'), ('attempt_id', 'other-attempt'),
    ('side', 'reference'), ('fixture_digest', 'b' * 64),
    ('purpose', StatePurpose.OWNED_CLEANUP), ('collections', frozenset({'users'})),
])
@pytest.mark.parametrize('which', [0, 1])
def test_equal_data_from_wrong_scope_cannot_pass(field, value, which):
    pair = list(snapshots())
    different = replace(scope(), **{field: value})
    data = rows() if field != 'collections' else {'users': {}}
    pair[which] = StoredSnapshot(different, pair[which].phase, data)
    assert check_stored_state(*pair, expected_scope=scope()) == (
        ('baseline_scope' if which == 0 else 'observed_scope'),)


def test_missing_observations_and_wrong_phase_cannot_pass():
    before, after = snapshots()
    assert check_stored_state(None, after, expected_scope=scope()) == ('baseline_missing_or_invalid',)
    assert check_stored_state(before, None, expected_scope=scope()) == ('observed_missing_or_invalid',)
    assert check_stored_state(before, before, expected_scope=scope()) == ('observed_phase',)
    assert check_stored_state(after, before, expected_scope=scope()) == ('baseline_phase', 'observed_phase')
    with pytest.raises(ValueError, match='expected scope'):
        check_stored_state(before, after, expected_scope=None)


def test_failure_and_repr_do_not_export_private_rows():
    data = rows()
    secret = 'SYNTHETIC-private-row-value'
    data['alerts']['owned'][secret] = secret
    before, after = snapshots(after=data)
    assert secret not in repr(after)
    failures = check_stored_state(before, after, expected_scope=scope())
    assert failures == ('stored_state_changed',)
    assert secret not in repr(failures)


def test_state_pass_does_not_complete_case_and_cleanup_failure_is_sticky():
    case = CaseEvidence('AN1-anonymous-write', reference_qualified=True)
    case.start('candidate')
    assert check_stored_state(*snapshots(), expected_scope=scope()) == ()
    case.record('candidate', 'state', 'PASS')
    assert case.snapshot()['differential'] == 'INCOMPLETE'
    cleanup = scope(StatePurpose.OWNED_CLEANUP)
    before = {'alerts': {}, 'keys': {}}
    failures = check_stored_state(*snapshots(before, rows(), cleanup), expected_scope=cleanup)
    assert failures == ('stored_state_changed',)
    case.record('candidate', 'cleanup', 'FAIL')
    assert case.snapshot()['differential'] == 'FAIL'
    with pytest.raises(ValueError, match='invalid or repeated'):
        case.record('candidate', 'cleanup', 'PASS')
