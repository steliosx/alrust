from copy import deepcopy
import json
import os
import re

import httpx
from hypothesis import given, strategies as st
import pytest

from alrust_conformance.client import Client, redact_secrets
from alrust_conformance.differential import replay, sequence
from alrust_conformance import differential
from alrust_conformance.normalize import normalize


def test_target_selection_and_credential_exclusion():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(201, json={'id': 'fixture'})
    client = Client('http://localhost:8080/api', transport=httpx.MockTransport(handler))
    try:
        assert client.request('POST', '/alert', {'resource': 'SYNTHETIC'}).status == 201
        assert str(seen[0].url) == 'http://localhost:8080/api/alert'
        assert 'authorization' not in seen[0].headers
        with pytest.raises(ValueError):
            client.request('GET', '//another-host/alert')
    finally:
        client.close()
    for url in ['http://user:password@localhost', 'file:///tmp/a', 'http://host?q=x']:
        with pytest.raises(ValueError):
            Client(url)


def test_plain_text_healthcheck_and_malformed_declared_json():
    responses = iter([httpx.Response(200, text='OK'),
                      httpx.Response(200, text='not JSON', headers={'content-type': 'application/json'})])
    client = Client('http://localhost', transport=httpx.MockTransport(lambda request: next(responses)))
    try:
        assert client.request('GET', '/management/healthcheck').body == 'OK'
        with pytest.raises(ValueError):
            client.request('GET', '/alert/synthetic')
    finally:
        client.close()


def test_server_credential_echo_is_redacted_only_in_export():
    original = {'responses': [{'status': 401, 'body': {
        'message': "API key parameter 'SYNTHETIC-SECRET' is invalid"}}]}
    exported = redact_secrets(original, ['SYNTHETIC-SECRET', None])
    assert 'SYNTHETIC-SECRET' not in json.dumps(exported)
    assert exported['responses'][0]['status'] == 401
    assert 'SYNTHETIC-SECRET' in json.dumps(original)


def test_normalization_preserves_semantic_differences_and_unknown_data():
    body = {'id': 'a', 'alert': {'id': 'a', 'href': 'http://left/alert/a',
            'customer': None, 'duplicateCount': 1, 'status': 'open',
            'receiveTime': '2026-09-29T20:00:00Z',
            'attributes': {'href': 'http://left/alert/a'},
            'history': [{'id': 'a', 'href': 'http://left/alert/a', 'status': 'open'},
                        {'id': 'b', 'status': 'closed'}]}}
    before = deepcopy(body)
    normalized = normalize(body, 'http://left')
    assert body == before
    assert normalized['alert']['href'] == '/alert/a'
    assert normalized['alert']['attributes']['href'] == body['alert']['attributes']['href']
    for field, value in [('customer', 'other'), ('duplicateCount', 2), ('status', 'closed'),
                         ('id', 'different'), ('receiveTime', None), ('extra', 1)]:
        changed = deepcopy(body)
        changed['alert'][field] = value
        assert normalize(changed, 'http://left') != normalized
    changed = deepcopy(body)
    changed['alert']['history'].reverse()
    assert normalize(changed, 'http://left') != normalized


@given(st.dictionaries(st.text(min_size=1), st.one_of(st.none(), st.booleans(), st.integers(), st.text())))
def test_arbitrary_attribute_data_is_not_removed(attributes):
    body = {'alert': {'attributes': attributes}}
    assert normalize(body, 'http://left') == body


def test_seeded_sequence_is_identical_and_labeled():
    left = list(sequence(42, 10, 'alrust-synthetic-unit', '2026-09-29T20:00:00Z'))
    assert left == list(sequence(42, 10, 'alrust-synthetic-unit', '2026-09-29T20:00:00Z'))
    assert all(item['payload']['resource'].startswith('alrust-synthetic-') for item in left)
    assert all(a['payload']['createTime'] < b['payload']['createTime'] for a, b in zip(left, left[1:]))


def test_equal_http_errors_are_not_a_success():
    def handler(request):
        return httpx.Response(500, json={'status': 'error'})
    clients = [Client(url, transport=httpx.MockTransport(handler)) for url in ['http://left', 'http://right']]
    try:
        result = replay(*clients, list(sequence(0, 2, 'alrust-synthetic-unit', '2026-09-29T20:00:00Z')))
        assert result['status'] == 'FAIL'
        assert result['requests'][0]['equal'] is True
        assert len(result['requests']) == 1
    finally:
        for client in clients:
            client.close()


def test_divergence_is_retained_and_created_alerts_are_cleaned():
    calls = []
    identifier = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
    def handler(request):
        calls.append((request.url.host, request.method))
        if request.method == 'DELETE':
            return httpx.Response(200, json={'status': 'ok'})
        return httpx.Response(201, json={'status': 'ok', 'id': identifier, 'alert': {
            'id': identifier, 'resource': json.loads(request.content)['resource'],
            'customer': 'left' if request.url.host == 'left' else 'right'}})
    clients = [Client(url, transport=httpx.MockTransport(handler)) for url in ['http://left', 'http://right']]
    try:
        result = replay(*clients, list(sequence(0, 2, 'alrust-synthetic-unit', '2026-09-29T20:00:00Z')))
        assert result['status'] == 'FAIL'
        assert result['requests'][0]['equal'] is False
        assert len(result['cleanup']) == 2
        assert calls == [('left', 'POST'), ('right', 'POST'), ('left', 'DELETE'), ('right', 'DELETE')]
    finally:
        for client in clients:
            client.close()


def test_empty_replay_and_equal_malformed_success_are_failures():
    def handler(request):
        return httpx.Response(201, json={'status': 'ok'})
    clients = [Client(url, transport=httpx.MockTransport(handler)) for url in ['http://left', 'http://right']]
    try:
        assert replay(*clients, [])['status'] == 'FAIL'
        result = replay(*clients, list(sequence(0, 2, 'alrust-synthetic-unit', '2026-09-29T20:00:00Z')))
        assert result['status'] == 'FAIL'
        assert result['requests'][0]['transport_or_schema_error'] == 'ValueError'
        assert result['cleanup'] == []
    finally:
        for client in clients:
            client.close()


def test_equivalent_responses_with_different_target_hrefs_pass():
    identifier = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
    def handler(request):
        if request.method == 'DELETE':
            return httpx.Response(200, json={'status': 'ok'})
        return httpx.Response(201, json={'status': 'ok', 'id': identifier, 'alert': {
            'id': identifier, 'resource': json.loads(request.content)['resource'],
            'href': f'http://{request.url.host}/alert/{identifier}'}})
    clients = [Client(url, transport=httpx.MockTransport(handler)) for url in ['http://left', 'http://right']]
    try:
        result = replay(*clients, list(sequence(0, 2, 'alrust-synthetic-unit', '2026-09-29T20:00:00Z')))
        assert result['status'] == 'LOCAL_PASS'
        assert len(result['requests']) == 2
        assert len(result['cleanup']) == 2
    finally:
        for client in clients:
            client.close()


def test_missing_git_retains_private_not_run_report_without_http(monkeypatch, tmp_path):
    report = tmp_path / 'private-report.json'
    monkeypatch.setattr('sys.argv', ['differential', '--reference', 'http://left',
                                   '--candidate', 'http://right', '--report', str(report)])
    def missing_git(*args, **kwargs):
        raise FileNotFoundError('synthetic missing git')
    def forbidden_client(*args, **kwargs):
        raise AssertionError('HTTP must not be attempted without source provenance')
    monkeypatch.setattr(differential.subprocess, 'check_output', missing_git)
    monkeypatch.setattr(differential, 'Client', forbidden_client)
    previous_umask = os.umask(0)
    try:
        assert differential.main() == 1
    finally:
        os.umask(previous_umask)
    evidence = json.loads(report.read_text())
    assert evidence['status'] == 'NOT_RUN'
    assert evidence['requests'] == evidence['cleanup'] == []
    assert evidence['planned_sequence']
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z',
                        evidence['planned_sequence'][0]['payload']['createTime'])
    assert len(evidence['provenance_errors']) == 2
    assert report.stat().st_mode & 0o777 == 0o600
    before = report.read_bytes()
    with pytest.raises(SystemExit):
        differential.main()
    assert report.read_bytes() == before


def test_client_setup_failure_retains_evidence_and_closes_first_client(monkeypatch, tmp_path):
    report = tmp_path / 'setup-failure.json'
    monkeypatch.setattr('sys.argv', ['differential', '--reference', 'http://left',
                                   '--candidate', 'http://right', '--report', str(report)])
    closed = []
    class FirstClient:
        def close(self):
            closed.append(True)
    def factory(url, key):
        if url == 'http://left':
            return FirstClient()
        raise ValueError('SYNTHETIC-SECRET must not enter error evidence')
    monkeypatch.setattr(differential, 'Client', factory)
    assert differential.main() == 1
    evidence = json.loads(report.read_text())
    assert closed == [True]
    assert evidence['status'] == 'FAIL'
    assert evidence['runner_error_type'] == 'ValueError'
    assert evidence['harness_head']
    assert evidence['planned_sequence']
    assert 'SYNTHETIC-SECRET' not in report.read_text()


def test_late_git_failure_preserves_run_rows_cleanup_and_http_outcome(monkeypatch, tmp_path):
    report = tmp_path / 'late-metadata.json'
    monkeypatch.setattr('sys.argv', ['differential', '--reference', 'http://127.0.0.1:18081',
                                   '--candidate', 'http://127.0.0.1:18082', '--report', str(report)])
    calls = 0
    original = differential.subprocess.check_output
    def late_git(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 2:
            raise FileNotFoundError('synthetic late metadata loss')
        return original(*args, **kwargs)
    monkeypatch.setattr(differential.subprocess, 'check_output', late_git)
    closed = []
    class FakeClient:
        def __init__(self, url, key):
            self.base_url = url
        def close(self):
            closed.append(self.base_url)
    monkeypatch.setattr(differential, 'Client', FakeClient)
    def completed(left, right, requests, **kwargs):
        partial = {'status': 'FAIL', 'requests': [{'request': requests[0], 'responses': []}],
                   'cleanup': [{'side': 0, 'status': 200}]}
        kwargs['on_progress'](partial)
        partial['status'] = 'LOCAL_PASS'
        return partial
    monkeypatch.setattr(differential, 'replay', completed)
    assert differential.main() == 1
    evidence = json.loads(report.read_text())
    assert evidence['status'] == 'FAIL'
    assert evidence['http_result'] == 'LOCAL_PASS'  # injected result, no actual HTTP
    assert len(evidence['requests']) == len(evidence['cleanup']) == 1
    assert len(evidence['postflight_provenance']['provenance_errors']) == 2
    assert evidence['metadata_failure'] == 'postflight provenance unavailable'
    assert len(closed) == 2


def test_interruption_keeps_partial_effects_and_is_not_not_run(monkeypatch, tmp_path):
    report = tmp_path / 'interrupted.json'
    monkeypatch.setattr('sys.argv', ['differential', '--reference', 'http://127.0.0.1:18081',
                                   '--candidate', 'http://127.0.0.1:18082', '--report', str(report)])
    closed = []
    class FakeClient:
        def __init__(self, url, key):
            self.base_url = url
        def close(self):
            closed.append(self.base_url)
    monkeypatch.setattr(differential, 'Client', FakeClient)
    def interrupted(left, right, requests, **kwargs):
        kwargs['on_progress']({'status': 'FAIL', 'requests': [{'request': requests[0],
                                    'responses': [{'status': 201, 'body': {'fixture': 'SYNTHETIC'}}]}],
                               'cleanup': [{'side': 0, 'status': 200}]})
        raise KeyboardInterrupt()
    monkeypatch.setattr(differential, 'replay', interrupted)
    assert differential.main() == 1
    evidence = json.loads(report.read_text())
    assert evidence['status'] == 'FAIL' and evidence['stage'] == 'interrupted'
    assert evidence['requests'][0]['responses'][0]['status'] == 201
    assert evidence['cleanup']
    assert evidence['runner_error_type'] == 'KeyboardInterrupt'
    assert len(closed) == 2
