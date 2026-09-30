"""Synthetic offline oracle and output containment checks; no service or HTTP."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback

import pytest

from alrust_conformance.api_key_session import shared_evidence_safe
from alrust_conformance.client import Response
from alrust_conformance.config_session import (
    ConfigSession, EXPECTED_CONFIG, NESTED_KEYS, REFERENCE_HEAD, REFERENCE_TARGET,
    TOP_KEYS, load_config_attestation,
)


def synthetic_body():
    # Labeled shape fixture, not captured data or assumed reference defaults.
    body = dict.fromkeys(TOP_KEYS)
    body.update({key: dict.fromkeys(keys) for key, keys in NESTED_KEYS.items()})
    body.update(endpoint=REFERENCE_TARGET, debug=False, auth_required=False,
                customer_views=False, severity={'SYNTHETIC': 1},
                colors={'SYNTHETIC': '#123456'})
    body['alarm_model'].update(name='Alerta 9.1.0', severity=deepcopy(body['severity']),
                               colors=deepcopy(body['colors']), defaults={
                                   'status': 'SYNTHETIC', 'normal_severity': 'SYNTHETIC',
                                   'previous_severity': 'SYNTHETIC'})
    body['timeouts'].update(alert=86400, ack=0)
    return body


class StubClient:
    base_url = REFERENCE_TARGET

    def __init__(self, response):
        self.response, self.calls, self.closed = response, [], False

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def close(self):
        self.closed = True


def test_success_is_one_read_originals_unchanged_and_no_body_export(capsys):
    body = synthetic_body()
    original = deepcopy(body)
    client = StubClient(Response(200, body))
    session = ConfigSession(client)
    session.read()
    assert client.calls == [('GET', '/config', None)]
    assert body == original
    assert session.safe_summary() == {
        'project': 'alrust', 'case': 'config-readonly-01',
        'completed': True, 'captured_response_records': 0}
    session.close()
    assert client.closed and capsys.readouterr() == ('', '')


@pytest.mark.parametrize('fault', [
    'wrapped', 'missing', 'extra', 'endpoint', 'alias-host', 'flag-int',
    'flag-true', 'model-selector', 'model-version', 'model-defaults',
    'severity-alias', 'colors-alias', 'alert-timeout', 'ack-timeout-bool',
    *['nested-' + key for key in NESTED_KEYS],
])
def test_detects_relevant_shape_and_semantic_mismatches_without_values(fault):
    body = synthetic_body()
    secret = 'SYNTHETIC-' + 'c' * 48
    if fault == 'wrapped':
        body = {'status': 'ok', 'data': body}
    elif fault == 'missing':
        body.pop('columns')
    elif fault == 'extra':
        body[secret] = secret
    elif fault == 'endpoint':
        body['endpoint'] = REFERENCE_TARGET + '/' + secret
    elif fault == 'alias-host':
        body['endpoint'] = 'http://localhost:18081'
    elif fault == 'flag-int':
        body['debug'] = 0
    elif fault == 'flag-true':
        body['customer_views'] = True
    elif fault == 'model-selector':
        body['alarm_model']['name'] = 'ALERTA'
    elif fault == 'model-version':
        body['alarm_model']['name'] = secret
    elif fault == 'model-defaults':
        body['alarm_model']['defaults'] = {secret: secret}
    elif fault == 'severity-alias':
        body['severity'] = {secret: 99}
    elif fault == 'colors-alias':
        body['colors'] = {secret: secret}
    elif fault == 'alert-timeout':
        body['timeouts']['alert'] = '86400'
    elif fault == 'ack-timeout-bool':
        body['timeouts']['ack'] = False
    else:
        body[fault.removeprefix('nested-')][secret] = secret
    session = ConfigSession(StubClient(Response(200, body)))
    with pytest.raises(AssertionError) as caught:
        session.read()
    report = ''.join(traceback.format_exception(caught.type, caught.value, caught.tb))
    shared_evidence_safe(report + json.dumps(session.safe_summary()), (secret,))
    assert not session.completed


@pytest.mark.parametrize('response', [Response(500, 'SYNTHETIC failure'), Response(200, None),
                                     ValueError('SYNTHETIC-' + 'd' * 48)])
def test_failure_does_not_retry_or_export_exception_and_closes(response):
    client = StubClient(response)
    session = ConfigSession(client)
    with pytest.raises(AssertionError) as caught:
        session.read()
    shared_evidence_safe(''.join(traceback.format_exception(caught.type, caught.value, caught.tb)))
    with pytest.raises(AssertionError, match='already attempted'):
        session.read()
    session.close()
    assert client.closed and len(client.calls) == 1


def attestation():
    return {'project': 'alrust', 'fixture': 'SYNTHETIC', 'reference_head': REFERENCE_HEAD,
            'target': REFERENCE_TARGET, 'effective_config': dict(EXPECTED_CONFIG)}


def write_attestation(tmp_path, data):
    path = tmp_path / 'config-attestation.json'
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path


def test_exact_private_attestation_passes(tmp_path):
    load_config_attestation(write_attestation(tmp_path, attestation()), REFERENCE_TARGET)


@pytest.mark.parametrize('fault', ['base-url', 'source', 'target', 'label', 'project',
                                  'missing-config', 'extra-config', 'debug-int',
                                  'timeout-bool', 'world-readable', 'symlink', 'malformed'])
def test_preflight_rejects_source_config_and_file_mismatch(tmp_path, fault):
    data = attestation()
    if fault == 'base-url':
        data['effective_config']['BASE_URL'] = 'SYNTHETIC-' + 'e' * 48
    elif fault in ('source', 'target', 'label', 'project'):
        data[{'source': 'reference_head', 'target': 'target', 'label': 'fixture',
              'project': 'project'}[fault]] = 'SYNTHETIC-invalid'
    elif fault == 'missing-config':
        data['effective_config'].pop('BASE_URL')
    elif fault == 'extra-config':
        data['effective_config']['EXTRA'] = True
    elif fault == 'debug-int':
        data['effective_config']['DEBUG'] = 0
    elif fault == 'timeout-bool':
        data['effective_config']['ACK_TIMEOUT'] = False
    path = write_attestation(tmp_path, data)
    if fault == 'world-readable':
        path.chmod(0o644)
    elif fault == 'symlink':
        link = tmp_path / 'link.json'
        link.symlink_to(path)
        path = link
    elif fault == 'malformed':
        path.write_text('SYNTHETIC-' + 'e' * 48)
    with pytest.raises(AssertionError) as caught:
        load_config_attestation(path, REFERENCE_TARGET)
    shared_evidence_safe(''.join(traceback.format_exception(caught.type, caught.value, caught.tb)))


def http_module():
    path = Path(__file__).parents[1] / 'http' / 'test_config.py'
    spec = importlib.util.spec_from_file_location('config_http_candidate', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_failed_preflight_constructs_no_client(tmp_path, monkeypatch):
    module = http_module()
    data = attestation()
    data['effective_config']['BASE_URL'] = 'SYNTHETIC-invalid'
    path = write_attestation(tmp_path, data)
    monkeypatch.setenv('ALRUST_TARGET', REFERENCE_TARGET)
    monkeypatch.setenv('ALRUST_CONFIG_ATTESTATION_FILE', str(path))
    constructed = []
    monkeypatch.setattr(module, 'Client', lambda *args: constructed.append(args))
    fixture = module.config_session.__wrapped__()
    with pytest.raises(AssertionError, match='prerequisites mismatch'):
        next(fixture)
    assert constructed == []


def test_fixture_finally_closes_after_body_failure(tmp_path, monkeypatch):
    module = http_module()
    path = write_attestation(tmp_path, attestation())
    monkeypatch.setenv('ALRUST_TARGET', REFERENCE_TARGET)
    monkeypatch.setenv('ALRUST_CONFIG_ATTESTATION_FILE', str(path))
    client = StubClient(Response(200, None))
    monkeypatch.setattr(module, 'Client', lambda *args: client)
    monkeypatch.setattr(module, 'load_credentials', lambda *args: type(
        'SyntheticCredentials', (), {'keys': {'unscoped_admin': 'SYNTHETIC-placeholder'}})())
    fixture = module.config_session.__wrapped__()
    session = next(fixture)
    with pytest.raises(AssertionError):
        session.read()
    with pytest.raises(StopIteration):
        next(fixture)
    assert client.closed and len(client.calls) == 1


def test_plain_pytest_failure_report_excludes_reflected_secret(tmp_path):
    path = tmp_path / 'test_deliberate_config_failure.py'
    path.write_text('''from alrust_conformance.config_session import ConfigSession
from alrust_conformance.client import Response
class Stub:
    base_url = 'http://127.0.0.1:18081'
    def request(self, *args):
        return Response(200, {'SYNTHETIC-' + 'f' * 48: 'SYNTHETIC-' + 'f' * 48})
def test_deliberate():
    ConfigSession(Stub()).read()
''')
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    env.pop('PYTEST_ADDOPTS', None)
    root = Path(__file__).parents[2]
    result = subprocess.run([sys.executable, '-m', 'pytest', '-c',
                             str(root / 'config-pytest.ini'), str(path), '-q'],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    shared_evidence_safe(result.stdout + result.stderr)
    assert 'config top-level object or keys mismatch' in result.stdout
    assert '1 failed' in result.stdout
