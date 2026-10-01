"""Labeled synthetic model fixtures; no SQL, server, HTTP or auth execution."""
import importlib.util
import json
from pathlib import Path
import traceback

import pytest

from alrust_conformance.api_key_session import shared_evidence_safe
from alrust_conformance.client import Response
from alrust_conformance.health_session import (
     HealthSession, HEALTH_PATH, REFERENCE_HEAD, REFERENCE_TARGET,
    TASK, load_empty_heartbeats_attestation,
)

WINDOW = 'alrust-management-health-reference-20260930-01'
PID = '12345'


class StubClient:
    def __init__(self, response):
        self.response, self.calls, self.closed = response, [], False

    def request_text(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def close(self):
        self.closed = True


def record():
    return {'project': 'alrust', 'fixture': 'SYNTHETIC', 'task': TASK,
            'reference_head': REFERENCE_HEAD, 'target': REFERENCE_TARGET,
            'window_id': WINDOW, 'server_pid': int(PID), 'heartbeats_count': 0}


def write_record(tmp_path, data):
    p = tmp_path / 'health-record.json'
    p.write_text(json.dumps(data))
    p.chmod(0o600)
    return p


def test_success_is_one_original_text_read_no_exports_and_closed(capsys):
    client = StubClient(Response(200, 'OK'))
    session = HealthSession(client)
    session.read()
    assert client.calls == [('GET', HEALTH_PATH, None)]
    assert session.safe_summary() == {'project': 'alrust', 'case': 'management-health-readonly-01',
                                     'completed': True, 'captured_response_records': 0}
    session.close()
    assert client.closed and capsys.readouterr() == ('', '')


@pytest.mark.parametrize('body', [None, {'status': 'OK'}, ['OK'], b'OK',
                                  'OK\n', ' OK', 'ok', '"OK"', 'SYNTHETIC-' + 'a' * 48])
def test_original_text_differences_fail_without_export(body):
    secret = 'SYNTHETIC-' + 'a' * 48
    session = HealthSession(StubClient(Response(200, body)))
    with pytest.raises(AssertionError) as caught:
        session.read()
    shared_evidence_safe(''.join(traceback.format_exception(caught.type, caught.value, caught.tb))
                         + json.dumps(session.safe_summary()), (secret,))
    assert session.attempted and not session.completed


@pytest.mark.parametrize('response', [Response(500, 'unexported'), Response(200, None),
                                     ValueError('SYNTHETIC-' + 'f' * 48)])
def test_http_failures_are_not_retried_or_exported_and_client_closes(response):
    client = StubClient(response)
    session = HealthSession(client)
    with pytest.raises(AssertionError) as caught:
        session.read()
    shared_evidence_safe(''.join(traceback.format_exception(caught.type, caught.value, caught.tb)))
    with pytest.raises(AssertionError, match='already attempted'):
        session.read()
    session.close()
    assert len(client.calls) == 1 and client.closed


def test_exact_private_empty_record_passes(tmp_path):
    load_empty_heartbeats_attestation(write_record(tmp_path, record()), REFERENCE_TARGET, WINDOW, PID)


@pytest.mark.parametrize('fault', ['nonempty', 'count-bool', 'pid-bool', 'pid-other',
                                  'project', 'label', 'task', 'source', 'target', 'window',
                                  'extra', 'missing', 'public', 'symlink', 'malformed', 'oversize'])
def test_attestation_refuses_dataset_source_instance_and_custody_mismatches(tmp_path, fault):
    data = record()
    changes = {'nonempty': ('heartbeats_count', 1), 'count-bool': ('heartbeats_count', False),
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
        data.pop('heartbeats_count')
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
        load_empty_heartbeats_attestation(p, REFERENCE_TARGET, WINDOW, PID)
    shared_evidence_safe(''.join(traceback.format_exception(caught.type, caught.value, caught.tb)))


@pytest.mark.parametrize('target,window,pid', [(None, WINDOW, PID), (REFERENCE_TARGET, None, PID),
                                              (REFERENCE_TARGET, 'other', PID),
                                              (REFERENCE_TARGET, WINDOW, None),
                                              (REFERENCE_TARGET, WINDOW, '1'),
                                              (REFERENCE_TARGET, WINDOW, 'True'),
                                              (REFERENCE_TARGET, WINDOW, '2147483648'),
                                              (REFERENCE_TARGET, WINDOW, '１２３４５'),
                                              (REFERENCE_TARGET, 'alrust-management-health-reference-２０２６０９３０-01', PID)])
def test_no_expected_instance_identity_fails_before_record(tmp_path, target, window, pid):
    with pytest.raises(AssertionError):
        load_empty_heartbeats_attestation(write_record(tmp_path, record()), target, window, pid)


def http_module():
    p = Path(__file__).parents[1] / 'http/test_management_health.py'
    spec = importlib.util.spec_from_file_location('health_http_candidate', p)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setup_fixture(tmp_path, monkeypatch, data):
    module = http_module()
    monkeypatch.setenv('ALRUST_TARGET', REFERENCE_TARGET)
    monkeypatch.setenv('ALRUST_HEALTH_WINDOW_ID', WINDOW)
    monkeypatch.setenv('ALRUST_HEALTH_SERVER_PID', PID)
    monkeypatch.setenv('ALRUST_HEALTH_ATTESTATION_FILE', str(write_record(tmp_path, data)))
    monkeypatch.setattr(module, 'load_config_attestation', lambda *a: None)
    monkeypatch.setattr(module, 'load_credentials', lambda *a: type(
        'SyntheticCredentials', (), {'keys': {'unscoped_admin': 'SYNTHETIC-placeholder'}})())
    return module


def test_nonempty_dataset_constructs_no_client(tmp_path, monkeypatch):
    data = record()
    data['heartbeats_count'] = 1
    module = setup_fixture(tmp_path, monkeypatch, data)
    constructed = []
    monkeypatch.setattr(module, 'Client', lambda *a: constructed.append(a))
    with pytest.raises(AssertionError, match='attested empty'):
        next(module.health_session.__wrapped__())
    assert constructed == []


def test_fixture_finally_closes_after_body_failure(tmp_path, monkeypatch):
    module = setup_fixture(tmp_path, monkeypatch, record())
    client = StubClient(Response(200, None))
    monkeypatch.setattr(module, 'Client', lambda *a: client)
    fixture = module.health_session.__wrapped__()
    session = next(fixture)
    with pytest.raises(AssertionError):
        session.read()
    fixture.close()
    assert client.closed


@pytest.mark.parametrize('media,text', [('text/html; charset=utf-8', 'OK'),
                                       ('text/plain', 'OK'),
                                       ('application/json', '"OK"'),
                                       ('text/plain', 'OK\n')])
def test_real_client_mock_transport_preserves_text_without_json_parsing(media, text):
    import httpx
    from alrust_conformance.client import Client
    calls = []
    secret = 'SYNTHETIC-' + 'a' * 48
    def respond(request):
        calls.append((request.method, str(request.url), request.content))
        return httpx.Response(200, text=text, headers={'content-type': media})
    client = Client(REFERENCE_TARGET, secret, transport=httpx.MockTransport(respond))
    session = HealthSession(client)
    try:
        if text == 'OK':
            session.read()
            assert session.completed
        else:
            with pytest.raises(AssertionError) as caught:
                session.read()
            shared_evidence_safe(''.join(traceback.format_exception(caught.type, caught.value, caught.tb)), (secret,))
    finally:
        session.close()
    assert calls == [('GET', REFERENCE_TARGET + HEALTH_PATH, b'')]
    assert client.http.is_closed


@pytest.mark.parametrize('path', ['relative', '//other.invalid/health'])
def test_text_transport_rejects_target_escape_without_a_request(path):
    import httpx
    from alrust_conformance.client import Client
    calls = []
    client = Client(REFERENCE_TARGET, transport=httpx.MockTransport(lambda request: calls.append(request)))
    try:
        with pytest.raises(ValueError):
            client.request_text('GET', path)
    finally:
        client.close()
    assert calls == []


def test_text_status_must_be_original_integer():
    session = HealthSession(StubClient(Response(200.0, 'OK')))
    with pytest.raises(AssertionError, match='status mismatch'):
        session.read()
    assert session.attempted and not session.completed
