"""Source-derived metadata/suppression cases at f431272; HTTP NOT_RUN."""
from datetime import datetime, timezone
import json
import os
import time
import uuid

import pytest

from alrust_conformance.client import Client, redact_secrets
from alrust_conformance.customer_fixtures import load_credentials


class MetadataSession:
    def __init__(self, client, keys):
        self.client = client
        self.keys = keys
        self.owned_ids = set()

    def request(self, method, path, payload=None, expected=200):
        response = self.client.request(method, path, payload)
        if method == 'POST' and response.status == 201 and isinstance(response.body, dict):
            self.owned_ids.add(str(uuid.UUID(response.body['id'])))
        # Runtime assertions consume the original response. Only evidence output
        # is redacted; lists/timestamps/nulls/order are otherwise unchanged.
        print(json.dumps({'fixture': 'SYNTHETIC', 'method': method, 'path': path,
                          'status': response.status,
                          'body': redact_secrets(response.body, self.keys)}, sort_keys=True))
        assert response.status == expected, 'synthetic metadata response status mismatch'
        return response.body

    def create(self, status='open'):
        payload = {'id': str(uuid.uuid4()), 'resource': 'alrust-synthetic-' + uuid.uuid4().hex,
                   'event': 'SyntheticMetadataEdges', 'environment': 'Development',
                   'severity': 'major', 'status': status, 'service': ['alrust-conformance'],
                   'origin': 'alrust-synthetic',
                   'tags': ['SYNTHETIC-one', 'SYNTHETIC-one', 'SYNTHETIC-two'],
                   'attributes': {'SYNTHETIC-null': None, 'SYNTHETIC-keep': 'retained'},
                   'createTime': '2000-01-01T00:00:00.000Z', 'timeout': 86400}
        body = self.request('POST', '/alert', payload, 201)
        assert body['status'] == 'ok'
        alert = body['alert']
        assert alert['id'] == body['id']
        assert alert['resource'] == payload['resource']
        assert (alert['severity'], alert['status'], alert['customer']) == ('major', status, None)
        return '/alert/' + body['id'], alert

    def read(self, path):
        body = self.request('GET', path)
        assert body['status'] == 'ok'
        return body['alert']


@pytest.fixture
def metadata_session():
    target = os.environ.get('ALRUST_TARGET')
    if not target:
        pytest.fail('NOT_RUN: isolated synthetic metadata target required')
    probe = Client(target)
    try:
        credentials = load_credentials(os.environ.get('ALRUST_SYNTHETIC_KEYS_FILE'), probe.base_url)
    finally:
        probe.close()
    client = Client(credentials.target, credentials.keys['unscoped_admin'])
    session = MetadataSession(client, list(credentials.keys.values()))
    try:
        yield session
    finally:
        try:
            for identifier in sorted(session.owned_ids):
                session.request('DELETE', '/alert/' + identifier)
        finally:
            client.close()


def moment(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    assert result.tzinfo is not None
    return result.astimezone(timezone.utc)


def test_receive_rewrite_removes_null_and_deduplicates_stored_tags(metadata_session):
    # utils/api.py:67-89; forwarder.py:43-67; alert.py:460-465;
    # postgres/base.py:264-277,331-350; spec R1.3.
    path, received = metadata_session.create()
    assert 'SYNTHETIC-null' not in received['attributes']
    assert received['attributes']['SYNTHETIC-keep'] == 'retained'
    # update_tags writes the DB but does not assign back to this loaded model.
    assert received['tags'] == ['SYNTHETIC-one', 'SYNTHETIC-one', 'SYNTHETIC-two']
    stored = metadata_session.read(path)
    assert len(stored['tags']) == 2
    assert set(stored['tags']) == {'SYNTHETIC-one', 'SYNTHETIC-two'}
    # Assert content only where order is deliberately undefined; preserve both
    # complete raw lists in the private captured evidence, never normalize them.
    assert {k: v for k, v in stored.items() if k != 'tags'} == {
        k: v for k, v in received.items() if k != 'tags'}
    assert len(stored['history']) == 1


def test_suppressed_unknown_action_still_runs_status_hook_attribute_rewrite(metadata_session):
    # Pre-action loop skips suppressed alerts (api.py:102-103), but from_action
    # always calls status_change_hook after transition (alert.py:666-667).
    # HookTrigger.process_status_change unconditionally tags/updates attributes
    # even when process_status skipped all plugins (hooks.py:35-48).
    path, received = metadata_session.create('blackout')
    assert received['attributes'] == {'SYNTHETIC-null': None, 'SYNTHETIC-keep': 'retained'}
    assert received['tags'] == ['SYNTHETIC-one', 'SYNTHETIC-one', 'SYNTHETIC-two']
    before = metadata_session.read(path)
    assert before == received
    time.sleep(0.05)
    assert metadata_session.request('PUT', path + '/action', {
        'action': 'synthetic-unknown-action', 'text': 'SYNTHETIC suppressed action'}) == {'status': 'ok'}
    after = metadata_session.read(path)
    assert (after['severity'], after['status']) == ('major', 'blackout')  # ACT-1
    assert after['attributes'] == {'SYNTHETIC-keep': 'retained'}
    assert len(after['tags']) == 2
    assert set(after['tags']) == {'SYNTHETIC-one', 'SYNTHETIC-two'}
    for field in ('createTime', 'receiveTime', 'lastReceiveTime', 'lastReceiveId',
                  'duplicateCount', 'repeat', 'previousSeverity', 'timeout', 'customer'):
        assert after[field] == before[field]
    assert after['history'][:-1] == before['history']
    assert len(after['history']) == len(before['history']) + 1
    entry = after['history'][-1]
    assert (entry['type'], entry['severity'], entry['status']) == ('action', 'major', 'blackout')
    assert entry['text'] == 'SYNTHETIC suppressed action'
    assert moment(entry['updateTime']) > moment(before['history'][-1]['updateTime'])
    assert after['updateTime'] == entry['updateTime']
