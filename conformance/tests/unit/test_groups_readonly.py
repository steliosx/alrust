"""Labeled synthetic model fixtures; no SQL, server, HTTP or auth execution."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import traceback

import pytest

from alrust_conformance.api_key_session import shared_evidence_safe
from alrust_conformance.client import Response
from alrust_conformance.groups_session import (
    EXPECTED, GroupsSession, PAGE_PATH, REFERENCE_HEAD, REFERENCE_TARGET,
    TASK, load_empty_groups_attestation,
)

WINDOW = 'alrust-groups-reference-20260930-01'
PID = '12345'


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


def record():
    return {'project': 'alrust', 'fixture': 'SYNTHETIC', 'task': TASK,
            'reference_head': REFERENCE_HEAD, 'target': REFERENCE_TARGET,
            'window_id': WINDOW, 'server_pid': int(PID), 'groups_count': 0}


def write_record(tmp_path, data):
    p = tmp_path / 'groups-record.json'
    p.write_text(json.dumps(data))
    p.chmod(0o600)
    return p


def test_success_is_one_original_read_no_exports_and_closed(capsys):
    body = deepcopy(EXPECTED)
    original = deepcopy(body)
    client = StubClient(Response(200, body))
    session = GroupsSession(client)
    session.read()
    assert body == original and client.calls == [('GET', PAGE_PATH, None)]
    assert session.safe_summary() == {'project': 'alrust', 'case': 'groups-readonly-01',
                                     'completed': True, 'captured_response_records': 0}
    session.close()
    assert client.closed and capsys.readouterr() == ('', '')


@pytest.mark.parametrize('fault', ['nonobject', 'missing', 'extra', 'page-bool',
                                  'size-string', 'pages-one', 'more-int', 'message',
                                  'group-present', 'total-bool', 'status'])
def test_detects_semantic_or_type_differences_without_value_export(fault):
    secret = 'SYNTHETIC-' + 'e' * 48
    body = deepcopy(EXPECTED)
    changes = {'page-bool': ('page', True), 'size-string': ('pageSize', '1'),
               'pages-one': ('pages', 1), 'more-int': ('more', 0),
               'message': ('message', secret), 'group-present': ('groups', [secret]),
               'total-bool': ('total', False), 'status': ('status', secret)}
    if fault == 'nonobject':
        body = [secret]
    elif fault == 'missing':
        body.pop('message')
    elif fault == 'extra':
        body[secret] = secret
    else:
        key, value = changes[fault]
        body[key] = value
    session = GroupsSession(StubClient(Response(200, body)))
    with pytest.raises(AssertionError) as caught:
        session.read()
    shared_evidence_safe(''.join(traceback.format_exception(caught.type, caught.value, caught.tb))
                         + json.dumps(session.safe_summary()), (secret,))
    assert session.attempted and not session.completed


@pytest.mark.parametrize('response', [Response(500, 'unexported'), Response(200, None),
                                     ValueError('SYNTHETIC-' + 'f' * 48)])
def test_http_failures_are_not_retried_or_exported_and_client_closes(response):
    client = StubClient(response)
    session = GroupsSession(client)
    with pytest.raises(AssertionError) as caught:
        session.read()
    shared_evidence_safe(''.join(traceback.format_exception(caught.type, caught.value, caught.tb)))
    with pytest.raises(AssertionError, match='already attempted'):
        session.read()
    session.close()
    assert len(client.calls) == 1 and client.closed


def test_exact_private_empty_record_passes(tmp_path):
    load_empty_groups_attestation(write_record(tmp_path, record()), REFERENCE_TARGET, WINDOW, PID)


@pytest.mark.parametrize('fault', ['nonempty', 'count-bool', 'pid-bool', 'pid-other',
                                  'project', 'label', 'task', 'source', 'target', 'window',
                                  'extra', 'missing', 'public', 'symlink', 'malformed', 'oversize'])
def test_attestation_refuses_dataset_source_instance_and_custody_mismatches(tmp_path, fault):
    data = record()
    changes = {'nonempty': ('groups_count', 1), 'count-bool': ('groups_count', False),
               'pid-bool': ('server_pid', True), 'pid-other': ('server_pid', 98765),
               'project': ('project', 'other'), 'label': ('fixture', 'PRODUCTION'),
               'task': ('task', 'other'), 'source': ('reference_head', 'other'),
               'target': ('target', 'http://localhost:18081'), 'window': ('window_id', 'other')}
    if fault in changes:
        key, value = changes[fault]
        data[key] = value
    elif fault == 'extra':
        data['unrecognized'] = 'SYNTHETIC-' + 'a' * 48
    elif fault == 'missing':
        data.pop('groups_count')
    p = write_record(tmp_path, data)
    if fault == 'public':
        p.chmod(0o644)
    elif fault == 'symlink':
        link = tmp_path / 'link.json'
        link.symlink_to(p)
        p = link
    elif fault == 'malformed':
        p.write_text('SYNTHETIC-' + 'a' * 48)
    elif fault == 'oversize':
        p.write_text(' ' * 16385)
    with pytest.raises(AssertionError) as caught:
        load_empty_groups_attestation(p, REFERENCE_TARGET, WINDOW, PID)
    shared_evidence_safe(''.join(traceback.format_exception(caught.type, caught.value, caught.tb)))


@pytest.mark.parametrize('target,window,pid', [(None, WINDOW, PID), (REFERENCE_TARGET, None, PID),
                                              (REFERENCE_TARGET, 'other', PID),
                                              (REFERENCE_TARGET, WINDOW, None),
                                              (REFERENCE_TARGET, WINDOW, '1'),
                                              (REFERENCE_TARGET, WINDOW, 'True')])
def test_no_expected_instance_identity_fails_before_record(tmp_path, target, window, pid):
    with pytest.raises(AssertionError):
        load_empty_groups_attestation(write_record(tmp_path, record()), target, window, pid)


def http_module():
    p = Path(__file__).parents[1] / 'http/test_groups.py'
    spec = importlib.util.spec_from_file_location('groups_http_candidate', p)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setup_fixture(tmp_path, monkeypatch, data):
    module = http_module()
    monkeypatch.setenv('ALRUST_TARGET', REFERENCE_TARGET)
    monkeypatch.setenv('ALRUST_GROUPS_WINDOW_ID', WINDOW)
    monkeypatch.setenv('ALRUST_GROUPS_SERVER_PID', PID)
    monkeypatch.setenv('ALRUST_GROUPS_ATTESTATION_FILE', str(write_record(tmp_path, data)))
    monkeypatch.setattr(module, 'load_config_attestation', lambda *a: None)
    monkeypatch.setattr(module, 'load_credentials', lambda *a: type(
        'SyntheticCredentials', (), {'keys': {'unscoped_admin': 'SYNTHETIC-placeholder'}})())
    return module


def test_nonempty_dataset_constructs_no_client(tmp_path, monkeypatch):
    data = record()
    data['groups_count'] = 1
    module = setup_fixture(tmp_path, monkeypatch, data)
    constructed = []
    monkeypatch.setattr(module, 'Client', lambda *a: constructed.append(a))
    with pytest.raises(AssertionError, match='attested empty'):
        next(module.groups_session.__wrapped__())
    assert constructed == []


def test_fixture_finally_closes_after_body_failure(tmp_path, monkeypatch):
    module = setup_fixture(tmp_path, monkeypatch, record())
    client = StubClient(Response(200, None))
    monkeypatch.setattr(module, 'Client', lambda *a: client)
    fixture = module.groups_session.__wrapped__()
    session = next(fixture)
    with pytest.raises(AssertionError):
        session.read()
    fixture.close()
    assert client.closed
