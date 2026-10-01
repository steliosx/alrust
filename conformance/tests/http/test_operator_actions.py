"""Source-derived A1-A5 vectors at f431272; live execution remains NOT_RUN."""
from datetime import datetime, timezone
import os
import time
import uuid

import pytest

from alrust_conformance.client import Client
from alrust_conformance.customer_fixtures import load_credentials


CREATE_TIME = '2000-01-01T00:00:00.000Z'


def timestamp(value):
    assert isinstance(value, str), 'server timestamp missing'
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    assert parsed.tzinfo is not None, 'server timestamp has no timezone'
    return parsed.astimezone(timezone.utc)


def expect_status(response, wanted):
    # Do not include a response or credential-bearing client in the assertion.
    assert response.status == wanted, 'synthetic action response status mismatch'


class OwnedAlert:
    def __init__(self, client, identifier):
        self.client = client
        self.path = '/alert/' + identifier

    def read(self):
        response = self.client.request('GET', self.path)
        expect_status(response, 200)
        assert isinstance(response.body, dict) and response.body.get('status') == 'ok'
        return response.body['alert']

    def update(self, endpoint, payload, status=200):
        # PostgreSQL sorts history by timestamp without a tie-breaker (A0/R7).
        # Separate writes beyond the serialized millisecond precision, retaining
        # their raw timestamps and checking strict order after successful writes.
        time.sleep(0.05)
        response = self.client.request('PUT', self.path + '/' + endpoint, payload)
        expect_status(response, status)
        if status == 200:
            assert response.body == {'status': 'ok'}
        return self.read()


@pytest.fixture
def owned_alert():
    target = os.environ.get('ALRUST_TARGET')
    if not target:
        pytest.fail('NOT_RUN: isolated synthetic action target required')
    probe = Client(target)
    try:
        credentials = load_credentials(os.environ.get('ALRUST_SYNTHETIC_KEYS_FILE'), probe.base_url)
    finally:
        probe.close()
    client = Client(credentials.target, credentials.keys['unscoped_admin'])
    identifier = None
    try:
        payload = {'id': str(uuid.uuid4()), 'resource': 'alrust-synthetic-' + uuid.uuid4().hex,
                   'event': 'SyntheticOperatorActions', 'environment': 'Development',
                   'severity': 'major', 'service': ['alrust-conformance'],
                   'origin': 'alrust-synthetic', 'tags': ['SYNTHETIC'],
                   'createTime': CREATE_TIME, 'timeout': 86400}
        response = client.request('POST', '/alert', payload)
        if response.status == 201 and isinstance(response.body, dict):
            # Retain the owned ID before checking the remainder of the response.
            identifier = str(uuid.UUID(response.body['id']))
        expect_status(response, 201)
        assert identifier is not None
        alert = OwnedAlert(client, identifier)
        created = alert.read()
        assert created['resource'] == payload['resource']
        assert created['customer'] is None
        assert (created['status'], created['severity']) == ('open', 'major')
        assert len(created['history']) == 1
        assert created['history'][0]['type'] == 'new'
        assert timestamp(created['history'][0]['updateTime']) == timestamp(CREATE_TIME)
        # Fixed old client time avoids an unobserved client/server clock offset.
        # No receive/action interleaving or housekeeping job belongs to this run.
        assert timestamp(created['receiveTime']) > timestamp(CREATE_TIME)
        yield alert
    finally:
        try:
            if identifier is not None:
                expect_status(client.request('DELETE', '/alert/' + identifier), 200)
        finally:
            client.close()


def latest_write(before, after, change_type):
    old_history, history = before['history'], after['history']
    assert len(history) == len(old_history) + 1
    # Preserve every earlier history entry, including all timestamps and order.
    assert history[:-1] == old_history
    entry = history[-1]
    assert entry['type'] == change_type
    assert timestamp(entry['updateTime']) > timestamp(old_history[-1]['updateTime'])
    assert timestamp(after['updateTime']) == timestamp(entry['updateTime'])
    assert (entry['status'], entry['severity']) == (after['status'], after['severity'])
    return entry


def test_missing_operator_fields_leave_alert_unchanged(owned_alert):
    # views/alerts.py:104-156, A1.1/A2.1: validation precedes lookup/writes.
    before = owned_alert.read()
    for endpoint in ('status', 'action'):
        for payload in ({}, {endpoint: ''}):
            assert owned_alert.update(endpoint, payload, 400) == before


def test_arbitrary_status_is_stored_without_severity_transition(owned_alert):
    # alert.py:616-642; A1.2-A1.5. Raw database NULL is NOT asserted by HTTP.
    before = owned_alert.read()
    after = owned_alert.update('status', {'status': 'synthetic-outside-enum', 'text': 'SYNTHETIC status'})
    assert after['status'] == 'synthetic-outside-enum'
    assert (after['severity'], after['previousSeverity']) == (before['severity'], before['previousSeverity'])
    assert after['timeout'] == 86400  # hydration, not proof of raw database NULL
    entry = latest_write(before, after, 'status')
    assert entry['text'] == 'SYNTHETIC status'
    assert entry['timeout'] == 86400


def test_unknown_action_succeeds_and_records_unchanged_state(owned_alert):
    # alarms/alerta.py:141-144; alert.py:669-694; A3 ACT-1/A5.
    before = owned_alert.read()
    after = owned_alert.update('action', {'action': 'synthetic-unknown-action'})
    for field in ('status', 'severity', 'previousSeverity', 'timeout'):
        assert after[field] == before[field]
    entry = latest_write(before, after, 'action')
    assert entry['text'] == 'synthetic-unknown-action operator action'


def test_close_and_reopen_restore_previous_severity(owned_alert):
    # alarms/alerta.py OPEN-3/CLS-1; alert.py:693-694; A3.3.
    before = owned_alert.read()
    closed = owned_alert.update('action', {'action': 'close'})
    assert (closed['severity'], closed['status'], closed['previousSeverity']) == ('normal', 'closed', 'major')
    latest_write(before, closed, 'close')
    opened = owned_alert.update('action', {'action': 'open'})
    assert (opened['severity'], opened['status'], opened['previousSeverity']) == ('major', 'open', 'normal')
    latest_write(closed, opened, 'open')


def test_ack_history_timeout_unack_and_redundant_conflict(owned_alert):
    # alert.py:256-280,649-657,693; A2.3/A2.4/A4/A5.
    before = owned_alert.read()
    acknowledged = owned_alert.update('action', {'action': 'ack'})
    assert (acknowledged['severity'], acknowledged['status']) == ('major', 'ack')
    assert acknowledged['timeout'] == 86400  # row/model timeout remains unchanged
    entry = latest_write(before, acknowledged, 'ack')
    assert entry['timeout'] == 0  # default ACK_TIMEOUT in history only
    assert owned_alert.update('action', {'action': 'ack'}, 409) == acknowledged
    restored = owned_alert.update('action', {'action': 'unack'})
    assert (restored['severity'], restored['status'], restored['timeout']) == ('major', 'open', 86400)
    entry = latest_write(acknowledged, restored, 'unack')
    assert entry['timeout'] == 86400  # previous new entry's timeout
