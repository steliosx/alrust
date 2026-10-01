"""Offline stored-state comparisons for anonymous refusals and owned cleanup.

These snapshots do not collect or authenticate database observations. They
neither prove absence of transient writes nor satisfy audit/confirmation gates.
"""
from dataclasses import InitVar, dataclass, field
from enum import Enum
import json
import math
import re


class StatePurpose(Enum):
    ANONYMOUS_REFUSAL = 'anonymous_refusal'
    OWNED_CLEANUP = 'owned_cleanup'


class SnapshotPhase(Enum):
    BASELINE = 'baseline'
    OBSERVED = 'observed'


def _label(value):
    return type(value) is str and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', value)


@dataclass(frozen=True)
class StateScope:
    case_id: str
    attempt_id: str
    side: str
    fixture_digest: str
    purpose: StatePurpose
    collections: frozenset[str]

    def __post_init__(self):
        if (not _label(self.case_id) or not _label(self.attempt_id) or type(self.side) is not str
                or self.side not in ('reference', 'candidate')
                or type(self.fixture_digest) is not str
                or not re.fullmatch('[0-9a-f]{64}', self.fixture_digest)
                or type(self.purpose) is not StatePurpose
                or type(self.collections) is not frozenset or not self.collections
                or not all(_label(name) for name in self.collections)):
            raise ValueError('invalid reviewed state scope')


def _strict_json(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _strict_json(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _strict_json(item)
        return
    raise ValueError('stored state must use lossless JSON values')


@dataclass(frozen=True)
class StoredSnapshot:
    scope: StateScope
    phase: SnapshotPhase
    rows: InitVar[dict]
    _canonical_rows: str = field(init=False, repr=False)

    def __post_init__(self, rows):
        if type(self.scope) is not StateScope or type(self.phase) is not SnapshotPhase:
            raise ValueError('invalid state snapshot identity')
        if type(rows) is not dict or rows.keys() != self.scope.collections:
            raise ValueError('state collection inventory mismatch')
        for records in rows.values():
            if type(records) is not dict or not all(
                    type(key) is str and key and type(row) is dict
                    for key, row in records.items()):
                raise ValueError('state rows must be objects keyed by record identity')
        try:
            _strict_json(rows)
            frozen = json.dumps(rows, sort_keys=True, separators=(',', ':'), allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            raise ValueError('invalid stored state values') from None
        object.__setattr__(self, '_canonical_rows', frozen)


def check_stored_state(baseline, observed, *, expected_scope: StateScope) -> tuple[str, ...]:
    """Compare independently collected snapshots for one reviewed purpose.

    Scope comes from the reviewed case and trusted fixture binding, never from
    the response or snapshots under test. Baselines are captured before the
    request for refusal checks, and before fixture setup for cleanup checks.
    Snapshot creation freezes data immediately; later mutation of a collector's
    dictionary cannot retroactively change the baseline.

    Anonymous refusals allow no API-key usage exception. Credentialed cases
    need a separate reviewed checker; never remove usage fields to make them
    pass this one. All fields and rows in the named inventory compare exactly.
    An empty result covers only the observed stored-state stage. Completeness,
    authenticated collection, audit, confirmation and execution gates remain
    the future executor's responsibility. Failure labels contain no row data.
    """
    if type(expected_scope) is not StateScope:
        raise ValueError('expected scope must be a reviewed StateScope')
    failures = []
    for label, snapshot, phase in (
            ('baseline', baseline, SnapshotPhase.BASELINE),
            ('observed', observed, SnapshotPhase.OBSERVED)):
        if type(snapshot) is not StoredSnapshot:
            failures.append(label + '_missing_or_invalid')
        else:
            if snapshot.scope != expected_scope:
                failures.append(label + '_scope')
            if snapshot.phase is not phase:
                failures.append(label + '_phase')
    if failures:
        return tuple(failures)
    if baseline._canonical_rows != observed._canonical_rows:
        return ('stored_state_changed',)
    return ()
