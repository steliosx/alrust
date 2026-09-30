"""Synthetic offline identity/privacy/orchestration cases; no SQL or HTTP."""
import hashlib
import importlib.util
import json
from pathlib import Path
import traceback

import pytest

from alrust_conformance.api_key_session import shared_evidence_safe
from alrust_conformance.client import Response
from alrust_conformance.config_session import EXPECTED_CONFIG
from alrust_conformance.gtg_session import (
    CONNECTION_CHECKS, GTG_PATH, GtgSession, REFERENCE_HEAD, REFERENCE_TARGET,
    TASK, load_gtg_connection_attestation,
)

WINDOW = 'alrust-management-gtg-reference-20260930-01'
PID = '12345'
SENTINEL = 'SYNTHETIC-' + 'a' * 48


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


def identity():
    return {'project': 'alrust', 'fixture': 'SYNTHETIC', 'task': TASK,
            'reference_head': REFERENCE_HEAD, 'target': REFERENCE_TARGET,
            'window_id': WINDOW, 'server_pid': int(PID)}


def write(path, data):
    path.write_text(json.dumps(data))
    path.chmod(0o600)


def records(tmp_path):
    paths = {name: tmp_path / (name + '.json') for name in ['connection', 'provenance', 'ready', 'config']}
    write(paths['connection'], {**identity(), 'backend': 'postgresql',
                               **{key: True for key in CONNECTION_CHECKS}})
    write(paths['ready'], {'project': 'alrust', 'fixture': 'SYNTHETIC', 'window_id': WINDOW,
                          'server_pid': int(PID), 'ready': True})
    write(paths['config'], {'project': 'alrust', 'fixture': 'SYNTHETIC',
                           'reference_head': REFERENCE_HEAD, 'target': REFERENCE_TARGET,
                           'effective_config': dict(EXPECTED_CONFIG)})
    write(paths['provenance'], {**identity(), 'after_readiness': True, 'same_served_connection': True,
          **{field: hashlib.sha256(paths[name].read_bytes()).hexdigest() for field, name in [
              ('connection_attestation_sha256', 'connection'), ('served_config_sha256', 'config'),
              ('readiness_marker_sha256', 'ready')]}})
    return paths


def validate(paths, target=REFERENCE_TARGET, window=WINDOW, pid=PID):
    load_gtg_connection_attestation(paths['connection'], target, window, pid,
                                    paths['provenance'], paths['ready'], paths['config'])


def assert_safe(caught, session=None):
    capture = ''.join(traceback.format_exception(caught.type, caught.value, caught.tb))
    if session:
        capture += json.dumps(session.safe_summary())
    shared_evidence_safe(capture, (SENTINEL,))


def test_success_is_one_exact_text_read_with_no_export(capsys):
    client = StubClient(Response(200, 'OK'))
    session = GtgSession(client)
    session.read()
    assert client.calls == [('GET', GTG_PATH, None)]
    assert session.safe_summary() == {'project': 'alrust', 'case': 'management-gtg-readonly-01',
                                     'completed': True, 'captured_response_records': 0}
    session.close()
    assert client.closed and capsys.readouterr() == ('', '')


@pytest.mark.parametrize('response', [Response(200, body) for body in [
    None, {'status': 'OK'}, ['OK'], b'OK', 'OK\n', ' OK', 'ok', '"OK"', SENTINEL]] + [
    Response(True, 'OK'), Response('200', 'OK'), Response(503, SENTINEL),
    Response(500, {'secret': SENTINEL}), ValueError(SENTINEL)])
def test_failure_never_retries_or_exports_original(response):
    client = StubClient(response)
    session = GtgSession(client)
    with pytest.raises(AssertionError) as caught:
        session.read()
    assert_safe(caught, session)
    with pytest.raises(AssertionError, match='already attempted'):
        session.read()
    session.close()
    assert session.attempted and not session.completed and client.closed and len(client.calls) == 1


def test_close_exception_is_fixed_and_private():
    client = StubClient(Response(200, 'OK'))
    def fail():
        raise ValueError(SENTINEL)
    client.close = fail
    with pytest.raises(AssertionError) as caught:
        GtgSession(client).close()
    assert_safe(caught)


def test_complete_private_source_instance_config_marker_chain_passes(tmp_path):
    validate(records(tmp_path))


@pytest.mark.parametrize('name', ['connection', 'provenance', 'ready', 'config'])
@pytest.mark.parametrize('fault', ['missing', 'public', 'symlink', 'malformed', 'oversize', 'not-object'])
def test_all_chain_files_require_private_regular_bounded_json(tmp_path, name, fault):
    paths = records(tmp_path)
    p = paths[name]
    if fault == 'missing':
        p.unlink()
    elif fault == 'public':
        p.chmod(0o644)
    elif fault == 'symlink':
        link = tmp_path / 'link.json'
        link.symlink_to(p)
        paths[name] = link
    elif fault == 'malformed':
        p.write_text(SENTINEL)
    elif fault == 'oversize':
        p.write_text(' ' * 16385)
    else:
        p.write_text(json.dumps([SENTINEL]))
    with pytest.raises(AssertionError) as caught:
        validate(paths)
    assert_safe(caught)


@pytest.mark.parametrize('name,field,value', [
    ('connection', 'project', 'other'), ('connection', 'fixture', 'PRODUCTION'),
    ('connection', 'task', 'other'), ('connection', 'reference_head', 'other'),
    ('connection', 'target', 'http://localhost:18081'), ('connection', 'window_id', 'other'),
    ('connection', 'server_pid', True), ('connection', 'server_pid', 9999),
    ('connection', 'backend', 'mongodb'),
    *[('connection', key, value) for key in sorted(CONNECTION_CHECKS) for value in [False, 1]],
    ('provenance', 'window_id', 'other'), ('provenance', 'task', 'other'),
    ('provenance', 'server_pid', True), ('provenance', 'server_pid', 9999),
    ('provenance', 'after_readiness', 1), ('provenance', 'same_served_connection', False),
    ('provenance', 'connection_attestation_sha256', 'b' * 64),
    ('provenance', 'served_config_sha256', 'b' * 64),
    ('provenance', 'readiness_marker_sha256', 'b' * 64),
    ('provenance', 'served_config_sha256', SENTINEL),
    ('ready', 'project', 'other'), ('ready', 'fixture', 'PRODUCTION'),
    ('ready', 'server_pid', True), ('ready', 'server_pid', 9999),
    ('ready', 'window_id', 'other'), ('ready', 'ready', 1),
    ('config', 'reference_head', 'other'),
])
def test_source_pid_backend_readiness_and_hash_mismatches_fail(tmp_path, name, field, value):
    paths = records(tmp_path)
    data = json.loads(paths[name].read_text())
    data[field] = value
    write(paths[name], data)
    with pytest.raises(AssertionError) as caught:
        validate(paths)
    assert_safe(caught)


@pytest.mark.parametrize('name', ['connection', 'provenance', 'ready', 'config'])
@pytest.mark.parametrize('fault', ['extra', 'missing-key'])
def test_record_shape_has_no_unrecognized_or_missing_fields(tmp_path, name, fault):
    paths = records(tmp_path)
    data = json.loads(paths[name].read_text())
    if fault == 'extra':
        data['unrecognized'] = SENTINEL
    else:
        data.pop('project')
    write(paths[name], data)
    with pytest.raises(AssertionError) as caught:
        validate(paths)
    assert_safe(caught)


@pytest.mark.parametrize('name', ['connection', 'ready', 'config'])
def test_complete_record_bytes_include_whitespace(tmp_path, name):
    paths = records(tmp_path)
    paths[name].write_bytes(paths[name].read_bytes() + b'\n')
    with pytest.raises(AssertionError, match='complete-record hash chain'):
        validate(paths)


@pytest.mark.parametrize('target,window,pid', [(None, WINDOW, PID),
    (REFERENCE_TARGET, None, PID), (REFERENCE_TARGET, 'other', PID),
    (REFERENCE_TARGET, WINDOW, None), (REFERENCE_TARGET, WINDOW, '1'),
    (REFERENCE_TARGET, WINDOW, '2147483648'), (REFERENCE_TARGET, WINDOW, '１２３４５'),
    (REFERENCE_TARGET, 'alrust-management-gtg-reference-２０２６０９３０-01', PID)])
def test_expected_source_instance_gate_rejects_missing_or_invalid_identity(tmp_path, target, window, pid):
    with pytest.raises(AssertionError):
        validate(records(tmp_path), target, window, pid)


def http_module():
    p = Path(__file__).parents[1] / 'http/test_management_gtg.py'
    spec = importlib.util.spec_from_file_location('gtg_http_candidate', p)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_module(tmp_path, monkeypatch):
    paths = records(tmp_path)
    module = http_module()
    for key, value in {
        'ALRUST_TARGET': REFERENCE_TARGET, 'ALRUST_CONFIG_ATTESTATION_FILE': str(paths['config']),
        'ALRUST_GTG_WINDOW_ID': WINDOW, 'ALRUST_GTG_SERVER_PID': PID,
        'ALRUST_GTG_ATTESTATION_FILE': str(paths['connection']),
        'ALRUST_GTG_PROVENANCE_FILE': str(paths['provenance']),
        'ALRUST_GTG_READINESS_FILE': str(paths['ready']),
        'ALRUST_SERVED_CONFIG_ATTESTATION_FILE': str(paths['config']),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(module, 'load_credentials', lambda *a: type(
        'SyntheticCredentials', (), {'keys': {'unscoped_admin': 'SYNTHETIC-placeholder'}})())
    return module, paths


@pytest.mark.parametrize('fault', ['connection', 'provenance', 'ready', 'config'])
def test_bad_chain_constructs_no_client(tmp_path, monkeypatch, fault):
    module, paths = fixture_module(tmp_path, monkeypatch)
    paths[fault].write_text(SENTINEL)
    constructed = []
    monkeypatch.setattr(module, 'Client', lambda *a: constructed.append(a))
    with pytest.raises(AssertionError) as caught:
        next(module.gtg_session.__wrapped__())
    assert not constructed
    assert_safe(caught)


def test_bad_credentials_construct_no_client(tmp_path, monkeypatch):
    module, _ = fixture_module(tmp_path, monkeypatch)
    def bad_credentials(*args):
        raise AssertionError('synthetic credential prerequisite failed')
    monkeypatch.setattr(module, 'load_credentials', bad_credentials)
    constructed = []
    monkeypatch.setattr(module, 'Client', lambda *a: constructed.append(a))
    with pytest.raises(AssertionError):
        next(module.gtg_session.__wrapped__())
    assert not constructed


def test_fixture_closes_after_body_failure(tmp_path, monkeypatch):
    module, _ = fixture_module(tmp_path, monkeypatch)
    client = StubClient(Response(200, SENTINEL))
    monkeypatch.setattr(module, 'Client', lambda *a: client)
    fixture = module.gtg_session.__wrapped__()
    session = next(fixture)
    with pytest.raises(AssertionError) as caught:
        session.read()
    assert_safe(caught, session)
    fixture.close()
    assert client.closed
