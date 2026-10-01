"""Labeled synthetic fixtures; no SQL, server, HTTP or authorization execution."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import traceback
from urllib.parse import urljoin

import pytest

from alrust_conformance.api_key_session import shared_evidence_safe
from alrust_conformance.client import Response
from alrust_conformance.permissions_session import (
    EXPECTED_PAGE, EXPECTED_ROLES, EXPECTED_ROWS, PAGE_PATH, PermissionsSession,
    REFERENCE_HEAD, REFERENCE_TARGET, TASK, load_empty_permissions_attestation,
)

WINDOW = 'alrust-permissions-reference-20260930-01'
PID = '12345'
IDS = ['11111111-1111-4111-8111-111111111111',
       '22222222-2222-4222-8222-222222222222',
       '33333333-3333-4333-8333-333333333333']
SECRET = 'SYNTHETIC-' + 'e' * 48


class StubClient:
    def __init__(self, response):
        self.response, self.calls, self.closed = response, [], False

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def close(self):
        self.closed = True


def body():
    rows = [{'id': identifier, 'href': REFERENCE_TARGET + '/perm/' + identifier,
             'match': name, 'scopes': deepcopy(scopes)}
            for identifier, (name, scopes) in zip(IDS, EXPECTED_ROWS)]
    return {**EXPECTED_PAGE, 'permissions': rows}


def record():
    return {'project': 'alrust', 'fixture': 'SYNTHETIC', 'task': TASK,
            'reference_head': REFERENCE_HEAD, 'target': REFERENCE_TARGET,
            'window_id': WINDOW, 'server_pid': int(PID), 'permissions_count': 0,
            'role_configuration': deepcopy(EXPECTED_ROLES)}


def write_record(tmp_path, data):
    p = tmp_path / 'permissions-record.json'
    p.write_text(json.dumps(data))
    p.chmod(0o600)
    return p


def safe_error(caught, session=None):
    text = ''.join(traceback.format_exception(caught.type, caught.value, caught.tb))
    if session is not None:
        text += json.dumps(session.safe_summary())
    shared_evidence_safe(text, (SECRET, *IDS))


def test_one_original_page_keeps_three_rows_and_no_exports(capsys):
    original = body()
    unchanged = deepcopy(original)
    client = StubClient(Response(200, original))
    session = PermissionsSession(client)
    session.read()
    assert original == unchanged and client.calls == [('GET', PAGE_PATH, None)]
    assert session.safe_summary() == {'project': 'alrust', 'case': 'permissions-readonly-01',
                                     'completed': True, 'captured_response_records': 0}
    session.close()
    assert client.closed and capsys.readouterr() == ('', '')


def test_object_key_order_does_not_change_original_list_order():
    original = body()
    original['permissions'] = [dict(reversed(list(row.items()))) for row in original['permissions']]
    original = dict(reversed(list(original.items())))
    unchanged = deepcopy(original)
    session = PermissionsSession(StubClient(Response(200, original)))
    session.read()
    assert original == unchanged and session.completed


def test_pinned_library_double_slash_collapses_to_original_href_relation():
    # The actual pinned absolute_url expression uses Flask's trailing-slash root.
    assert urljoin(REFERENCE_TARGET + '//', 'perm/' + IDS[0]) == REFERENCE_TARGET + '/perm/' + IDS[0]


@pytest.mark.parametrize('fault', [
    'nonobject', 'missing', 'extra', 'page-bool', 'size-string', 'pages-one',
    'more-int', 'total-bool', 'status', 'rows-object', 'one-row', 'four-rows',
    'row-nonobject', 'row-missing', 'row-extra', 'row-order', 'name', 'scopes-string',
    'scopes-order', 'scopes-extra', 'id-type', 'id-malformed', 'id-noncanonical',
    'id-version', 'id-variant', 'duplicate-id', 'href-other-id', 'href-double-slash',
    'href-other-target', 'href-type',
])
def test_rejects_page_row_uuid_and_original_href_differences_without_export(fault):
    original = body()
    row = original['permissions'][0]
    changes = {'page-bool': ('page', True), 'size-string': ('pageSize', '1'),
               'pages-one': ('pages', 1), 'more-int': ('more', 1),
               'total-bool': ('total', True), 'status': ('status', SECRET)}
    row_changes = {'name': ('match', SECRET), 'scopes-string': ('scopes', SECRET),
                   'scopes-order': ('scopes', ['write', 'read', 'admin']),
                   'scopes-extra': ('scopes', ['admin', 'read', 'write', SECRET]),
                   'id-type': ('id', [SECRET]), 'id-malformed': ('id', SECRET),
                   'id-noncanonical': ('id', IDS[0].replace('-', '')),
                   'id-version': ('id', IDS[0].replace('-4111-', '-5111-')),
                   'id-variant': ('id', IDS[0].replace('-8111-', '-7111-')),
                   'href-other-id': ('href', REFERENCE_TARGET + '/perm/' + IDS[1]),
                   'href-double-slash': ('href', REFERENCE_TARGET + '//perm/' + IDS[0]),
                   'href-other-target': ('href', 'http://localhost:18081/perm/' + IDS[0]),
                   'href-type': ('href', [SECRET])}
    if fault in changes:
        key, value = changes[fault]; original[key] = value
    elif fault in row_changes:
        key, value = row_changes[fault]; row[key] = value
    elif fault == 'nonobject':
        original = [SECRET]
    elif fault == 'missing':
        original.pop('total')
    elif fault == 'extra':
        original['message'] = SECRET
    elif fault == 'rows-object':
        original['permissions'] = {'unexported': SECRET}
    elif fault == 'one-row':
        original['permissions'] = [row]
    elif fault == 'four-rows':
        original['permissions'].append(deepcopy(row))
    elif fault == 'row-nonobject':
        original['permissions'][0] = [SECRET]
    elif fault == 'row-missing':
        row.pop('href')
    elif fault == 'row-extra':
        row[SECRET] = SECRET
    elif fault == 'row-order':
        original['permissions'].reverse()
    elif fault == 'duplicate-id':
        original['permissions'][1]['id'] = IDS[0]
        original['permissions'][1]['href'] = row['href']
    unchanged = deepcopy(original)
    client = StubClient(Response(200, original))
    session = PermissionsSession(client)
    with pytest.raises(AssertionError) as caught:
        session.read()
    safe_error(caught, session)
    assert original == unchanged and session.attempted and not session.completed
    with pytest.raises(AssertionError, match='already attempted'):
        session.read()
    session.close()
    assert len(client.calls) == 1 and client.closed


@pytest.mark.parametrize('response', [Response(500, SECRET), Response(200, None),
                                     ValueError(SECRET)])
def test_failed_http_or_parse_is_not_retried_and_closes(response):
    client = StubClient(response)
    session = PermissionsSession(client)
    with pytest.raises(AssertionError) as caught:
        session.read()
    safe_error(caught, session)
    with pytest.raises(AssertionError, match='already attempted'):
        session.read()
    session.close()
    assert len(client.calls) == 1 and client.closed


def test_close_failure_has_fixed_error():
    client = StubClient(Response(200, body()))
    def fail():
        raise ValueError(SECRET)
    client.close = fail
    with pytest.raises(AssertionError) as caught:
        PermissionsSession(client).close()
    safe_error(caught)


def test_exact_fresh_private_empty_and_served_roles_record_passes(tmp_path):
    load_empty_permissions_attestation(write_record(tmp_path, record()), REFERENCE_TARGET, WINDOW, PID)


@pytest.mark.parametrize('fault', [
    'nonempty', 'count-bool', 'pid-bool', 'pid-other', 'project', 'label', 'task',
    'source', 'target', 'window', 'extra', 'missing', 'public', 'symlink',
    'malformed', 'oversize', 'roles-object', 'roles-missing', 'roles-extra',
    'admin-role', 'user-role', 'guest-role', 'user-scopes-order', 'guest-scopes-type',
])
def test_record_refuses_dataset_role_source_instance_and_custody_mismatches(tmp_path, fault):
    data = record()
    changes = {'nonempty': ('permissions_count', 1), 'count-bool': ('permissions_count', False),
               'pid-bool': ('server_pid', True), 'pid-other': ('server_pid', 98765),
               'project': ('project', SECRET), 'label': ('fixture', 'PRODUCTION'),
               'task': ('task', 'ALRUST-P0-GROUPS-READONLY-01'),
               'source': ('reference_head', SECRET),
               'target': ('target', 'http://localhost:18081'),
               'window': ('window_id', 'alrust-groups-reference-20260930-01')}
    role_changes = {'admin-role': ('DEFAULT_ADMIN_ROLE', SECRET),
                    'user-role': ('DEFAULT_USER_ROLE', SECRET),
                    'guest-role': ('DEFAULT_GUEST_ROLE', SECRET),
                    'user-scopes-order': ('USER_DEFAULT_SCOPES', ['write', 'read']),
                    'guest-scopes-type': ('GUEST_DEFAULT_SCOPES', 'read:alerts')}
    if fault in changes:
        key, value = changes[fault]; data[key] = value
    elif fault in role_changes:
        key, value = role_changes[fault]; data['role_configuration'][key] = value
    elif fault == 'extra':
        data['unrecognized'] = SECRET
    elif fault == 'missing':
        data.pop('permissions_count')
    elif fault == 'roles-object':
        data['role_configuration'] = [SECRET]
    elif fault == 'roles-missing':
        data['role_configuration'].pop('DEFAULT_ADMIN_ROLE')
    elif fault == 'roles-extra':
        data['role_configuration']['ADMIN_SCOPES'] = ['admin', 'read', 'write']
    p = write_record(tmp_path, data)
    if fault == 'public':
        p.chmod(0o644)
    elif fault == 'symlink':
        link = tmp_path / 'link.json'; link.symlink_to(p); p = link
    elif fault == 'malformed':
        p.write_text(SECRET)
    elif fault == 'oversize':
        p.write_text(' ' * 16385)
    with pytest.raises(AssertionError) as caught:
        load_empty_permissions_attestation(p, REFERENCE_TARGET, WINDOW, PID)
    safe_error(caught)


@pytest.mark.parametrize('target,window,pid', [
    (None, WINDOW, PID), (REFERENCE_TARGET, None, PID),
    (REFERENCE_TARGET, 'alrust-groups-reference-20260930-01', PID),
    (REFERENCE_TARGET, WINDOW, None), (REFERENCE_TARGET, WINDOW, '1'),
    (REFERENCE_TARGET, WINDOW, 'True'),
])
def test_absent_or_other_expected_instance_fails_before_reading_record(tmp_path, target, window, pid):
    with pytest.raises(AssertionError):
        load_empty_permissions_attestation(write_record(tmp_path, record()), target, window, pid)


def http_module():
    p = Path(__file__).parents[1] / 'http/test_permissions.py'
    spec = importlib.util.spec_from_file_location('permissions_http_candidate', p)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setup_fixture(tmp_path, monkeypatch, data):
    module = http_module()
    monkeypatch.setenv('ALRUST_TARGET', REFERENCE_TARGET)
    monkeypatch.setenv('ALRUST_PERMISSIONS_WINDOW_ID', WINDOW)
    monkeypatch.setenv('ALRUST_PERMISSIONS_SERVER_PID', PID)
    monkeypatch.setenv('ALRUST_PERMISSIONS_ATTESTATION_FILE', str(write_record(tmp_path, data)))
    monkeypatch.setattr(module, 'load_config_attestation', lambda *a: None)
    monkeypatch.setattr(module, 'load_credentials', lambda *a: type(
        'SyntheticCredentials', (), {'keys': {'unscoped_admin': 'SYNTHETIC-placeholder'}})())
    return module


@pytest.mark.parametrize('fault', ['nonempty', 'roles', 'count', 'config', 'credentials'])
def test_failed_prerequisite_constructs_no_client(tmp_path, monkeypatch, fault):
    data = record()
    if fault == 'nonempty':
        data['permissions_count'] = 1
    elif fault == 'roles':
        data['role_configuration']['DEFAULT_ADMIN_ROLE'] = SECRET
    module = setup_fixture(tmp_path, monkeypatch, data)
    def fail(*args):
        raise AssertionError('fixed prerequisite failure')
    if fault == 'count':
        monkeypatch.setattr(module, 'load_empty_permissions_attestation', fail)
    elif fault == 'config':
        monkeypatch.setattr(module, 'load_config_attestation', fail)
    elif fault == 'credentials':
        monkeypatch.setattr(module, 'load_credentials', fail)
    constructed = []
    monkeypatch.setattr(module, 'Client', lambda *a: constructed.append(a))
    with pytest.raises(AssertionError):
        next(module.permissions_session.__wrapped__())
    assert constructed == []


def test_fixture_finally_closes_after_body_failure(tmp_path, monkeypatch):
    module = setup_fixture(tmp_path, monkeypatch, record())
    client = StubClient(Response(200, None))
    monkeypatch.setattr(module, 'Client', lambda *a: client)
    fixture = module.permissions_session.__wrapped__()
    session = next(fixture)
    with pytest.raises(AssertionError):
        session.read()
    fixture.close()
    assert client.closed
