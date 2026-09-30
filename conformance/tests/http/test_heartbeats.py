"""HB0/HB2/HB4 at pinned f431272; source-derived, heartbeat HTTP NOT_RUN."""
from datetime import datetime, timezone
import json
import os
import time
from urllib.parse import urlencode
import uuid

import pytest

from alrust_conformance.client import Client, redact_secrets
from alrust_conformance.customer_fixtures import load_credentials


class HeartbeatSession:
    def __init__(self, credentials, clients):
        self.credentials = credentials
        self.clients = clients
        self.keys = list(credentials.keys.values())
        self.origin = 'alrust-synthetic-heartbeat-' + uuid.uuid4().hex
        self.owned_ids = set()

    def request(self, role, method, path, payload=None, expected=200):
        response = self.clients[role].request(method, path, payload)
        if method == 'POST' and response.status == 201 and isinstance(response.body, dict):
            self.owned_ids.add(str(uuid.UUID(response.body['id'])))
        print(json.dumps({'fixture': 'SYNTHETIC', 'method': method, 'path': path,
                          'status': response.status,
                          'body': redact_secrets(response.body, self.keys)}, sort_keys=True))
        assert response.status == expected, 'synthetic heartbeat response status mismatch'
        return response.body

    def create(self, role='unscoped_admin', **fields):
        payload = {'id': str(uuid.uuid4()), 'origin': self.origin,
                   'tags': ['SYNTHETIC', 'SYNTHETIC'],
                   'attributes': {'SYNTHETIC-marker': 'initial'},
                   'createTime': '2000-01-01T00:00:00.000Z', 'timeout': 86400}
        payload.update(fields)
        body = self.request(role, 'POST', '/heartbeat', payload, 201)
        assert body['status'] == 'ok'
        heartbeat = body['heartbeat']
        assert heartbeat['id'] == body['id']
        assert heartbeat['origin'] == self.origin
        assert heartbeat['type'] == 'Heartbeat'
        assert heartbeat['tags'] == payload['tags']
        assert heartbeat['attributes'] == payload['attributes']
        assert heartbeat['timeout'] == payload['timeout']
        assert heartbeat['createTime'] == payload['createTime']
        assert heartbeat['maxLatency'] == 2000
        assert heartbeat['latency'] > 2000
        assert heartbeat['status'] == 'slow'
        assert isinstance(heartbeat['since'], int) and heartbeat['since'] >= 0
        return heartbeat

    def delete(self, role, identifier):
        assert identifier in self.owned_ids, 'delete only owned synthetic heartbeat'
        assert self.request(role, 'DELETE', '/heartbeat/' + identifier) == {'status': 'ok'}
        self.owned_ids.remove(identifier)

    def list(self, role, **fields):
        return self.request(role, 'GET', '/heartbeats?' + urlencode({'origin': self.origin, **fields}))


@pytest.fixture
def heartbeats():
    target = os.environ.get('ALRUST_TARGET')
    if not target:
        pytest.fail('NOT_RUN: isolated synthetic heartbeat target required')
    probe = Client(target)
    try:
        credentials = load_credentials(os.environ.get('ALRUST_SYNTHETIC_KEYS_FILE'), probe.base_url)
    finally:
        probe.close()
    clients = {}
    session = None
    try:
        # Existing actual-record fixture: both roles have admin, but only one
        # carries customer A. write:alerts roles are not heartbeat writers.
        for role in ['unscoped_admin', 'a_admin']:
            clients[role] = Client(credentials.target, credentials.keys[role])
        session = HeartbeatSession(credentials, clients)
        yield session
    finally:
        try:
            cleanup_errors = []
            if session is not None:
                for identifier in sorted(session.owned_ids):
                    try:
                        session.delete('unscoped_admin', identifier)
                    except Exception as error:
                        cleanup_errors.append(type(error).__name__)
            if cleanup_errors:
                pytest.fail('synthetic heartbeat cleanup failed for owned IDs')
        finally:
            for client in clients.values():
                client.close()


def moment(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    assert result.tzinfo is not None
    return result.astimezone(timezone.utc)


def test_upsert_keeps_id_within_origin_customer_and_replaces_payload(heartbeats):
    # HB0: pg.py:851-861; assignment hbv.py:27, api.py:14-28.
    a = heartbeats.create('a_admin')
    b = heartbeats.create(customer=heartbeats.credentials.customer_b)
    unscoped = heartbeats.create()
    assert [a['customer'], b['customer'], unscoped['customer']] == [
        heartbeats.credentials.customer_a, heartbeats.credentials.customer_b, None]
    assert len({a['id'], b['id'], unscoped['id']}) == 3
    time.sleep(0.05)
    replacement_id = str(uuid.uuid4())
    repeated = heartbeats.create('a_admin', id=replacement_id,
        tags=['SYNTHETIC-replacement'], attributes={'SYNTHETIC-marker': 'replaced'},
        createTime='2001-01-01T00:00:00.000Z', timeout=7200)
    assert repeated['id'] == a['id'] and repeated['id'] != replacement_id
    assert repeated['customer'] == a['customer']
    assert moment(repeated['receiveTime']) > moment(a['receiveTime'])
    listed = heartbeats.list('unscoped_admin')
    assert listed['status'] == 'ok' and listed['total'] == 3
    assert {row['id'] for row in listed['heartbeats']} == {a['id'], b['id'], unscoped['id']}


def test_listing_filters_customer_and_empty_status_subset_reports_zero(heartbeats):
    # HB4.1/HB4.1a: hbv.py:62-87; pg.py:886-918; pg/utils.py:291-303.
    a = heartbeats.create('a_admin')
    b = heartbeats.create(customer=heartbeats.credentials.customer_b)
    unscoped = heartbeats.create()
    own = heartbeats.list('a_admin')
    assert own['status'] == 'ok' and own['total'] == 1
    assert [row['id'] for row in own['heartbeats']] == [a['id']]
    slow = heartbeats.list('unscoped_admin', status='slow')
    assert slow['total'] == 3
    assert {row['id'] for row in slow['heartbeats']} == {a['id'], b['id'], unscoped['id']}
    assert all(row['status'] == 'slow' for row in slow['heartbeats'])
    empty = heartbeats.list('unscoped_admin', status='ok')
    assert empty['status'] == 'ok' and empty['total'] == 0
    assert empty['heartbeats'] == [] and empty['message'] == 'not found'


def test_per_id_read_and_delete_do_not_apply_listing_customer_filter(heartbeats):
    # HB4.2/C19: hbv.py:48,96 uses unset g.customer; decorator sets
    # plural g.customers, so per-id SQL has no customer predicate.
    a = heartbeats.create('a_admin')
    b = heartbeats.create(customer=heartbeats.credentials.customer_b)
    listed = heartbeats.list('a_admin')
    assert listed['total'] == 1 and [row['id'] for row in listed['heartbeats']] == [a['id']]
    body = heartbeats.request('a_admin', 'GET', '/heartbeat/' + b['id'])
    assert body['status'] == 'ok' and body['total'] == 1
    assert body['heartbeat']['id'] == b['id']
    assert body['heartbeat']['customer'] == heartbeats.credentials.customer_b
    heartbeats.delete('a_admin', b['id'])
    missing = heartbeats.request('unscoped_admin', 'GET', '/heartbeat/' + b['id'], expected=404)
    assert missing['status'] == 'error' and missing['message'] == 'not found'
    retained = heartbeats.request('a_admin', 'GET', '/heartbeat/' + a['id'])
    assert retained['heartbeat']['id'] == a['id']
