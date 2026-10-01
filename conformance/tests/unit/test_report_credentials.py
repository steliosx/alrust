"""HF-5: private reports retain metadata without exporting credential inputs."""
import json
import re

import httpx
import pytest

from alrust_conformance import differential
from alrust_conformance.client import Client


@pytest.mark.parametrize('value', ['a', '', 'SYNTHETIC-short',
                                 'SYNTHETIC-' + 'a' * 48 + '\n',
                                 'unlabeled-' + 'a' * 48])
def test_rejected_inputs_preserve_private_not_run_without_http(monkeypatch, tmp_path, value):
    report = tmp_path / 'rejected.json'
    monkeypatch.delenv('ALRUST_REFERENCE_API_KEY', raising=False)
    monkeypatch.setenv('ALRUST_API_KEY', value)
    monkeypatch.setattr('sys.argv', ['differential', '--reference', 'http://127.0.0.1:18081',
                                   '--candidate', 'http://127.0.0.1:18082', '--report', str(report)])
    def forbidden_client(*args, **kwargs):
        raise AssertionError('credential preflight must precede client construction')
    monkeypatch.setattr(differential, 'Client', forbidden_client)
    assert differential.main() == 1
    evidence = json.loads(report.read_text())
    assert evidence['status'] == 'NOT_RUN' and evidence['stage'] == 'preflight'
    assert evidence['not_run_reason'] == 'unsupported synthetic credential input'
    assert evidence['invalid_credential_inputs'] == ['ALRUST_API_KEY']
    assert evidence['requests'] == evidence['cleanup'] == []
    assert re.fullmatch('[0-9a-f]{40}', evidence['harness_head'])
    assert all(re.fullmatch('[0-9a-f]{64}', digest)
               for digest in evidence['harness_files_sha256'].values())
    assert evidence['namespace'].startswith('alrust-synthetic-')
    assert evidence['planned_sequence'][0]['payload']['resource'].startswith(evidence['namespace'])
    assert '<redacted-api-key>' not in report.read_text()
    # No rejected value is copied into evidence. A one-character value may
    # occur incidentally in ordinary metadata, which must remain intact.
    if len(value) > 1:
        assert value not in report.read_text()
    assert report.stat().st_mode & 0o777 == 0o600


def test_captured_keys_drive_both_clients_and_export_even_if_environment_changes(monkeypatch, tmp_path):
    report = tmp_path / 'captured.json'
    key = 'SYNTHETIC-' + 'a' * 48
    monkeypatch.setenv('ALRUST_REFERENCE_API_KEY', key)
    monkeypatch.delenv('ALRUST_API_KEY', raising=False)
    monkeypatch.setattr('sys.argv', ['differential', '--reference', 'http://127.0.0.1:18081',
                                   '--candidate', 'http://127.0.0.1:18082', '--report', str(report)])
    supplied, closed, raw = [], [], []
    class FakeClient:
        def __init__(self, url, api_key):
            self.base_url = url
            supplied.append(api_key)
            monkeypatch.setenv('ALRUST_REFERENCE_API_KEY', 'a')
        def close(self):
            closed.append(self.base_url)
    def fake_replay(left, right, requests, **kwargs):
        partial = {'status': 'FAIL', 'requests': [{'request': requests[0], 'responses': [
                    {'status': 401, 'body': {'message': f"API key parameter '{key}' is invalid"}}]}],
                   'cleanup': []}
        raw.append(partial)
        kwargs['on_progress'](partial)
        return partial
    monkeypatch.setattr(differential, 'Client', FakeClient)
    monkeypatch.setattr(differential, 'replay', fake_replay)
    assert differential.main() == 1
    evidence = json.loads(report.read_text())
    assert supplied == [key, None] and len(closed) == 2
    assert evidence['status'] == evidence['http_result'] == 'FAIL'
    assert key not in report.read_text()
    assert '<redacted-api-key>' in evidence['requests'][0]['responses'][0]['body']['message']
    assert re.fullmatch('[0-9a-f]{40}', evidence['harness_head'])
    assert key in json.dumps(raw)  # raw response is preserved for comparison


def test_general_client_preserves_short_key_for_negative_authentication_cases():
    seen = []
    def handler(request):
        seen.append(request.headers['authorization'])
        return httpx.Response(401, json={'status': 'error'})
    client = Client('http://localhost', 'a', transport=httpx.MockTransport(handler))
    try:
        assert client.request('GET', '/alerts').status == 401
        assert seen == ['Key a']
    finally:
        client.close()
