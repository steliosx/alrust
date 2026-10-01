from copy import deepcopy
import json
import os
import stat

import pytest

from alrust_conformance.security_evidence import (
    CaseEvidence, PrivateJournal, STAGES, check_fixture,
)


@pytest.fixture
def profile():
    return {
        'profile_id': 'AN1-default', 'lifecycle_id': 'invocation-1',
        'server_commit': 'f4312727783dad64198b62ed778bbca98a5144a3',
        'database_id': 'synthetic-db-1', 'workspace_id': 'synthetic-work-1',
        'settings': {
            'AUTH_REQUIRED': True, 'ALLOW_READONLY': True, 'AUTH_PROXY': False,
            'EMAIL_VERIFICATION': False, 'READONLY_SCOPES': ['read'],
            'DELETE_SCOPES': [], 'AUTH_PROVIDER': 'basic', 'ADMIN_ROLES': ['admin'],
            'ADMIN_USERS': [], 'USER_ROLES': ['user'], 'GUEST_ROLES': ['guest'],
            'USER_DEFAULT_SCOPES': ['read', 'write'], 'GUEST_DEFAULT_SCOPES': ['read:alerts'],
            'ALLOWED_EMAIL_DOMAINS': ['*'],
        },
        'credentials': {'limited': {'key_sha256': 'a' * 64, 'scopes': ['read'], 'roles': []}},
    }


def observation(profile):
    return dict(deepcopy(profile), schema='alrust-security-fixture/1', observed_at=100, expires_at=120)


def test_exact_fresh_identity_is_only_offline_validation(profile):
    seen = observation(profile)
    result = check_fixture(seen, profile, now=110, max_age=15)
    assert result.status == 'VALIDATED_OFFLINE'
    assert len(result.digest) == 64
    assert result == check_fixture(dict(reversed(list(seen.items()))), profile, now=110, max_age=15)
    assert seen == observation(profile)


@pytest.mark.parametrize('now,max_age', [(99, 15), (120, 15), (116, 15), (110, 0),
                                         (True, 15), (110, True), (110.0, 15)])
def test_freshness_refuses_future_expired_stale_and_bad_policy(profile, now, max_age):
    assert check_fixture(observation(profile), profile, now=now, max_age=max_age).status == 'NOT_RUN'


@pytest.mark.parametrize('field', ['profile_id', 'lifecycle_id', 'server_commit', 'database_id',
                                 'workspace_id', 'settings', 'credentials', 'schema', 'observed_at', 'expires_at'])
def test_every_missing_observation_field_refuses(profile, field):
    seen = observation(profile)
    del seen[field]
    assert check_fixture(seen, profile, now=110, max_age=15).status == 'NOT_RUN'


@pytest.mark.parametrize('change', [
    lambda x: x.update(lifecycle_id='restarted'),
    lambda x: x.update(database_id='other-database'),
    lambda x: x.update(workspace_id='other-workspace'),
    lambda x: x.update(server_commit='0' * 40),
    lambda x: x['settings'].update(READONLY_SCOPES=[]),
    lambda x: x['settings'].update(AUTH_REQUIRED=1),
    lambda x: x['settings'].pop('USER_DEFAULT_SCOPES'),
    lambda x: x['credentials'].clear(),
    lambda x: x['credentials']['limited'].update(scopes=['admin']),
    lambda x: x['credentials']['limited'].update(key_sha256='b' * 64),
    lambda x: x['credentials']['limited'].update(secret='SYNTHETIC-DO-NOT-EXPORT'),
    lambda x: x.update(observed_at=True),
    lambda x: x.update(unexpected='value'),
])
def test_contradictory_or_malformed_observation_refuses_without_echo(profile, change):
    seen = observation(profile)
    change(seen)
    result = check_fixture(seen, profile, now=110, max_age=15)
    assert result.status == 'NOT_RUN' and result.digest is None
    assert 'SYNTHETIC-DO-NOT-EXPORT' not in repr(result)


def test_no_permissive_default_for_missing_expected_profile(profile):
    for expected in (None, {}, {'settings': {}}):
        assert check_fixture(observation(profile), expected, now=110, max_age=15).status == 'NOT_RUN'
    assert check_fixture(None, profile, now=110, max_age=15).status == 'NOT_RUN'


def test_anonymous_profile_requires_explicit_empty_credentials(profile):
    profile['credentials'] = {}
    assert check_fixture(observation(profile), profile, now=110, max_age=15).status == 'VALIDATED_OFFLINE'
    del profile['credentials']
    assert check_fixture(observation(profile), profile, now=110, max_age=15).status == 'NOT_RUN'


def complete(case, side):
    case.start(side)
    for stage in STAGES:
        case.record(side, stage, 'PASS')


def test_equal_responses_without_state_audit_and_cleanup_cannot_pass():
    case = CaseEvidence('SF-AN1-delete', reference_qualified=True)
    assert case.snapshot()['differential'] == 'NOT_RUN'
    for side in ('reference', 'candidate'):
        case.start(side)
        case.record(side, 'response', 'PASS')
    with pytest.raises(ValueError):
        case.compare('PASS')
    assert case.snapshot()['differential'] == 'INCOMPLETE'
    for side in ('reference', 'candidate'):
        for stage in ('state', 'audit', 'cleanup'):
            case.record(side, stage, 'PASS')
    assert case.snapshot()['differential'] == 'INCOMPLETE'
    case.compare('PASS')
    assert case.snapshot()['differential'] == 'LOCAL_PASS'


@pytest.mark.parametrize('stage', STAGES)
def test_failure_is_sticky_and_cannot_be_overwritten(stage):
    case = CaseEvidence('SF-USERS-create', reference_qualified=True)
    case.start('reference')
    case.record('reference', stage, 'FAIL')
    with pytest.raises(ValueError):
        case.record('reference', stage, 'PASS')
    complete(case, 'candidate')
    assert case.snapshot()['differential'] == 'FAIL'
    with pytest.raises(ValueError):
        case.compare('PASS')


def test_pending_reference_never_contributes_pass_or_hides_failure():
    case = CaseEvidence('SF-USERS-put-null', reference_qualified=False)
    snap = case.snapshot()
    assert snap['differential'] == 'SKIPPED_REFERENCE_UNQUALIFIED'
    assert snap['sides']['candidate']['status'] == 'NOT_RUN'
    complete(case, 'candidate')
    assert case.snapshot()['differential'] == 'SKIPPED_REFERENCE_UNQUALIFIED'
    with pytest.raises(ValueError):
        case.compare('PASS')
    case.start('reference')
    case.record('reference', 'response', 'FAIL')
    assert case.snapshot()['differential'] == 'FAIL'


def test_comparison_failure_cannot_be_retried_as_pass():
    case = CaseEvidence('SF-CRON-default', reference_qualified=True)
    for side in ('reference', 'candidate'):
        complete(case, side)
    case.compare('FAIL')
    with pytest.raises(ValueError):
        case.compare('PASS')
    assert case.snapshot()['differential'] == 'FAIL'


def test_unstarted_unknown_and_repeated_operations_refuse():
    case = CaseEvidence('SF-AN1-default', reference_qualified=True)
    with pytest.raises(ValueError):
        case.record('candidate', 'response', 'PASS')
    with pytest.raises(ValueError):
        case.start('other')
    case.start('candidate')
    with pytest.raises(ValueError):
        case.start('candidate')
    with pytest.raises(ValueError):
        case.record('candidate', 'unknown', 'PASS')
    with pytest.raises(ValueError):
        case.record('candidate', 'response', True)
    snap = case.snapshot()
    snap['sides']['candidate']['assertions']['response'] = 'PASS'
    assert case.snapshot()['sides']['candidate']['assertions'] == {}


def test_journal_private_exclusive_and_incremental(tmp_path):
    path = tmp_path / 'evidence.jsonl'
    case = CaseEvidence('SF-AN1-default', reference_qualified=True)
    with PrivateJournal(path) as journal:
        journal.append(case.snapshot())
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with pytest.raises(FileExistsError):
            PrivateJournal(path)
        case.start('reference')
        journal.append(case.snapshot())
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert [r['sequence'] for r in records] == [0, 1]
        assert [r['evidence']['differential'] for r in records] == ['NOT_RUN', 'INCOMPLETE']
    with pytest.raises(ValueError):
        journal.append({})
    alias = tmp_path / 'symlink'
    alias.symlink_to(path)
    with pytest.raises(FileExistsError):
        PrivateJournal(alias)


def test_partial_io_error_seals_journal_and_preserves_evidence(tmp_path, monkeypatch):
    path = tmp_path / 'evidence.jsonl'
    journal = PrivateJournal(path)
    journal.append({'status': 'NOT_RUN'})
    real_write = os.write
    calls = []
    def failing_write(fd, data):
        calls.append(True)
        if len(calls) == 1:
            return real_write(fd, data[:9])
        raise OSError('synthetic full disk')
    monkeypatch.setattr(os, 'write', failing_write)
    with pytest.raises(OSError):
        journal.append({'status': 'INCOMPLETE'})
    with pytest.raises(ValueError):
        journal.append({'status': 'LOCAL_PASS'})
    assert json.loads(path.read_text().splitlines()[0])['evidence']['status'] == 'NOT_RUN'
    assert not path.read_bytes().endswith(b'\n')


def test_journal_rejects_shared_parent_and_fsync_failure(tmp_path, monkeypatch):
    shared = tmp_path / 'shared'
    shared.mkdir(mode=0o755)
    with pytest.raises(ValueError):
        PrivateJournal(shared / 'evidence')
    assert not (shared / 'evidence').exists()
    path = tmp_path / 'private-evidence'
    journal = PrivateJournal(path)
    def fail_sync(fd):
        raise OSError('synthetic sync failure')
    monkeypatch.setattr(os, 'fsync', fail_sync)
    with pytest.raises(OSError):
        journal.append({'status': 'INCOMPLETE'})
    with pytest.raises(ValueError):
        journal.append({'status': 'LOCAL_PASS'})


def test_empty_scope_control_and_exact_extra_settings(profile):
    profile['settings']['READONLY_SCOPES'] = []
    profile['settings']['CUSTOM_SCOPES'] = ['synthetic']
    assert check_fixture(observation(profile), profile, now=110, max_age=15).status == 'VALIDATED_OFFLINE'
    seen = observation(profile)
    seen['settings']['CUSTOM_SCOPES'] = ['other']
    assert check_fixture(seen, profile, now=110, max_age=15).status == 'NOT_RUN'
    seen['settings']['CUSTOM_SCOPES'] = float('nan')
    assert check_fixture(seen, profile, now=110, max_age=15).status == 'NOT_RUN'


def test_new_journal_syncs_directory_before_record_data(tmp_path, monkeypatch):
    sync_kinds = []
    real_sync = os.fsync
    def track(fd):
        sync_kinds.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
        real_sync(fd)
    monkeypatch.setattr(os, 'fsync', track)
    with PrivateJournal(tmp_path / 'durable') as journal:
        assert sync_kinds == ['directory']
        journal.append({'status': 'NOT_RUN'})
        assert sync_kinds == ['directory', 'file']


def test_directory_sync_failure_aborts_and_closes_both_descriptors(tmp_path, monkeypatch):
    opened = []
    real_open = os.open
    def track(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd
    def fail(fd):
        assert stat.S_ISDIR(os.fstat(fd).st_mode)
        raise OSError('synthetic directory sync failure')
    monkeypatch.setattr(os, 'open', track)
    monkeypatch.setattr(os, 'fsync', fail)
    path = tmp_path / 'failed-create'
    with pytest.raises(OSError):
        PrivateJournal(path)
    assert path.read_bytes() == b''
    assert len(opened) == 2
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)
    with pytest.raises(FileExistsError):
        PrivateJournal(path)
