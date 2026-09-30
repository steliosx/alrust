"""Ordinary owned blackout cases at f431272; SOURCE_DERIVED / HTTP NOT_RUN."""
from datetime import datetime
from decimal import Decimal
import json
import os
import re
from urllib.parse import urlencode
import uuid

import pytest

from alrust_conformance.client import Client, redact_secrets
from alrust_conformance.customer_fixtures import load_credentials


class BlackoutSession:
    def __init__(self, client, keys):
        self.client, self.keys = client, keys
        self.owned_ids = set()

    def request(self, method, path, payload=None, expected=200):
        response = self.client.request(method, path, payload)
        if method == 'POST' and response.status == 201:
            # The caller supplied this fresh UUID. Retain it even if the
            # returned body is malformed and a subsequent assertion fails.
            self.owned_ids.add(str(uuid.UUID(payload['id'])))
        print(json.dumps({'fixture': 'SYNTHETIC', 'method': method, 'path': path,
                          'status': response.status,
                          'body': redact_secrets(response.body, self.keys)}, sort_keys=True))
        assert response.status == expected, 'synthetic blackout status mismatch'
        return response.body  # Original values; evidence redaction changes only a copy.

    def create(self, **overrides):
        payload = {'id': str(uuid.uuid4()), 'environment': 'Development',
                   'resource': 'alrust-synthetic-blackout-' + uuid.uuid4().hex,
                   'service': [], 'tags': [], 'startTime': '2000-01-01T00:00:00.000Z',
                   'endTime': '2000-01-01T00:02:00.000Z',
                   'duration': 999, 'text': 'SYNTHETIC ordinary blackout'}
        payload.update(overrides)
        body = self.request('POST', '/blackout', payload, 201)
        assert body['status'] == 'ok'
        assert body['id'] == body['blackout']['id'] == payload['id']
        return '/blackout/' + body['id'], body['blackout']

    def read(self, path):
        body = self.request('GET', path)
        assert body['status'] == 'ok' and body['total'] == 1
        return body['blackout']

    def delete(self, path):
        assert self.request('DELETE', path) == {'status': 'ok'}
        self.owned_ids.remove(path.rsplit('/', 1)[1])


@pytest.fixture
def blackout_session():
    target = os.environ.get('ALRUST_TARGET')
    if not target:
        pytest.fail('NOT_RUN: isolated synthetic blackout target required')
    probe = Client(target)
    try:
        credentials = load_credentials(os.environ.get('ALRUST_SYNTHETIC_KEYS_FILE'), probe.base_url)
    finally:
        probe.close()
    client = Client(credentials.target, credentials.keys['unscoped_admin'])
    session = BlackoutSession(client, list(credentials.keys.values()))
    try:
        yield session
    finally:
        failures = []
        try:
            for identifier in sorted(session.owned_ids):
                try:
                    session.request('DELETE', '/blackout/' + identifier)
                except Exception as error:
                    failures.append(type(error).__name__)
        finally:
            client.close()
        if failures:
            pytest.fail('synthetic blackout cleanup failed: ' + ','.join(failures))


def timestamp(value):
    assert isinstance(value, str) and re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z', value)
    return datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%fZ')


def assert_expired_read(record):
    # base.py:757-781,803-839 numeric EXTRACT; format.py:39-42 Decimal -> str.
    assert record['status'] == 'expired'
    assert type(record['remaining']) is str and Decimal(record['remaining']) == 0
    assert type(record['duration']) is int


def test_owned_blackout_duration_read_update_list_and_delete(blackout_session):
    # models/blackout.py:25-61,92-137,190-210,236-246;
    # views/blackouts.py:17-144; postgres/base.py:745-852.
    path, created = blackout_session.create()
    assert set(created) == {'id', 'href', 'priority', 'environment', 'service',
                            'resource', 'event', 'group', 'tags', 'origin', 'customer',
                            'startTime', 'endTime', 'duration', 'status', 'remaining',
                            'user', 'createTime', 'text'}
    assert created['href'].endswith(path)
    assert created['startTime'] == '2000-01-01T00:00:00.000Z'
    assert created['endTime'] == '2000-01-01T00:02:00.000Z'
    assert type(created['duration']) is int and created['duration'] == 120
    assert type(created['remaining']) is int and created['remaining'] == 120
    assert created['status'] == 'expired' and created['priority'] == 2
    assert created['customer'] is None and created['user'] == 'alrust-synthetic-admin'
    timestamp(created['createTime'])
    before = blackout_session.read(path)
    assert_expired_read(before)
    assert {k: v for k, v in before.items() if k != 'remaining'} == {
        k: v for k, v in created.items() if k != 'remaining'}
    query = '/blackouts?' + urlencode({'resource': before['resource']})
    listed = blackout_session.request('GET', query)
    assert listed['status'] == 'ok' and listed['total'] == 1 and listed['blackouts'] == [before]
    updated = blackout_session.request('PUT', path, {'duration': 999})
    # Customer was already null and login stays identical in this fixture.
    # This does not claim duration-only requests never affect other customers.
    assert updated == {'status': 'ok', 'blackout': before}
    assert blackout_session.read(path) == before
    changed = blackout_session.request('PUT', path, {'endTime': '2000-01-01T00:03:00.000Z'})
    assert changed == {'status': 'ok', 'blackout': {
        **before, 'endTime': '2000-01-01T00:03:00.000Z', 'duration': 180}}
    assert blackout_session.read(path) == changed['blackout']
    blackout_session.delete(path)
    assert blackout_session.request('GET', path, expected=404)['message'] == 'not found'
    empty = blackout_session.request('GET', query)
    assert empty['status'] == 'ok' and empty['total'] == 0 and empty['blackouts'] == []


def test_owned_blackout_origin_and_field_priority_recomputed_on_update(blackout_session):
    # models/blackout.py:63-80; no stored-priority column assertion or SQL access.
    path, created = blackout_session.create(origin='SYNTHETIC-origin',
        event='SYNTHETIC-event', service=['SYNTHETIC-service'], group='SYNTHETIC-group')
    assert created['priority'] == 8
    current = blackout_session.read(path)
    assert_expired_read(current)
    for change, priority in [({'origin': None}, 3), ({'service': []}, 5), ({'group': None}, 6)]:
        updated = blackout_session.request('PUT', path, change)
        expected = {**current, **change, 'priority': priority}
        assert updated == {'status': 'ok', 'blackout': expected}
        assert blackout_session.read(path) == expected
        current = expected
