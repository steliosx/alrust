"""C7-C10/C14/C16-C17 from pinned f431272; live results remain NOT_RUN."""
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from urllib.parse import urlencode
import uuid

import pytest

from alrust_conformance.client import Client
from alrust_conformance.customer_fixtures import load_credentials


@pytest.fixture
def customers():
    target = os.environ.get('ALRUST_TARGET')
    if not target:
        pytest.fail('NOT_RUN: set ALRUST_TARGET to an isolated synthetic test instance')
    # Validate the base URL before consuming any credential file.
    probe = Client(target)
    try:
        return load_credentials(os.environ.get('ALRUST_SYNTHETIC_KEYS_FILE'), probe.base_url)
    finally:
        probe.close()


def expect_status(response, wanted):
    # Assert scalar status only: a failed auth response may echo its key.
    status = response.status
    assert status == wanted, 'synthetic contract response status mismatch'


@contextmanager
def scoped_clients(credentials):
    clients = {}
    try:
        for role, key in credentials.keys.items():
            clients[role] = Client(credentials.target, key)
        clients['unauthenticated'] = Client(credentials.target)
        yield clients
    finally:
        for client in clients.values():
            client.close()


@contextmanager
def alerts(credentials, clients):
    resource = 'alrust-synthetic-' + uuid.uuid4().hex
    owned_ids = set()
    def create(role, **fields):
        payload = {'id': str(uuid.uuid4()), 'resource': resource,
                   'event': 'SyntheticCustomerConformance', 'environment': 'Development',
                   'severity': 'major', 'service': ['alrust-conformance'],
                   'origin': 'alrust-synthetic', 'tags': ['SYNTHETIC'],
                   'createTime': datetime.now(timezone.utc).isoformat(
                       timespec='milliseconds').replace('+00:00', 'Z'), 'timeout': 86400}
        payload.update(fields)
        response = clients[role].request('POST', '/alert', payload)
        expect_status(response, 201)
        body = response.body
        assert isinstance(body, dict) and body.get('status') == 'ok'
        alert = body.get('alert')
        assert isinstance(alert, dict) and alert.get('resource') == resource
        identifier = str(uuid.UUID(body['id']))
        owned_ids.add(identifier)  # retain cleanup even if later assertions fail
        assert alert.get('id') == identifier
        return alert
    try:
        yield resource, create
    finally:
        for identifier in sorted(owned_ids):
            expect_status(clients['unscoped_admin'].request('DELETE', '/alert/' + identifier), 200)


def test_writer_customer_assignment_and_mismatch(customers):
    # assign_customer exact scope/customer tests: utils/api.py:14-28.
    with scoped_clients(customers) as clients, alerts(customers, clients) as (_, create):
        first = create('a_writer')
        assert first['customer'] == customers.customer_a
        repeated = create('a_writer', customer=customers.customer_a)
        assert repeated['id'] == first['id']
        assert repeated['duplicateCount'] == 1
        denied = clients['a_writer'].request('POST', '/alert', {
            'resource': 'alrust-synthetic-' + uuid.uuid4().hex,
            'event': 'SyntheticCustomerMismatch', 'customer': customers.customer_b})
        expect_status(denied, 400)  # assignment fails before default reject plugin
        assert denied.body['message'] == f"not allowed to set customer to '{customers.customer_b}'"


def test_uncredentialed_explicit_customer_is_rejected(customers):
    # permission skips scope check but sets g.scopes=[]: decorators.py:156-161.
    with scoped_clients(customers) as clients:
        response = clients['unauthenticated'].request('POST', '/alert', {
            'resource': 'alrust-synthetic-' + uuid.uuid4().hex,
            'event': 'SyntheticUnauthenticatedCustomer', 'customer': customers.customer_a})
        expect_status(response, 400)
        assert response.body['message'] == f"not allowed to set customer to '{customers.customer_a}'"


def test_customer_is_part_of_dedup_identity(customers):
    # postgres/base.py:179-198,216-240; no admin exemption on request filtering.
    with scoped_clients(customers) as clients, alerts(customers, clients) as (_, create):
        a = create('a_writer')
        b = create('b_writer')
        unscoped = create('unscoped_admin')
        assert [a['customer'], b['customer'], unscoped['customer']] == [
            customers.customer_a, customers.customer_b, None]
        assert len({a['id'], b['id'], unscoped['id']}) == 3
        assert [a['duplicateCount'], b['duplicateCount'], unscoped['duplicateCount']] == [0, 0, 0]
        for role, original in [('a_writer', a), ('b_writer', b), ('unscoped_admin', unscoped)]:
            repeat = create(role)
            assert repeat['id'] == original['id']
            assert repeat['duplicateCount'] == 1


def test_scoped_admin_filters_reads_counts_and_mutations(customers):
    # decorators.py:48; postgres/utils.py:161-163; base.py:293-299;
    # views/alerts.py:88-121,145-162,279-306,309-397.
    with scoped_clients(customers) as clients, alerts(customers, clients) as (resource, create):
        a = create('a_writer')
        b = create('b_writer')
        unscoped = create('unscoped_admin')
        reader = clients['a_admin']
        overridden = create('a_admin', customer=customers.customer_b)
        assert overridden['customer'] == customers.customer_b
        assert overridden['id'] == b['id']
        own = reader.request('GET', '/alert/' + a['id'])
        expect_status(own, 200)
        assert own.body['alert']['customer'] == customers.customer_a
        query = '?' + urlencode({'resource': resource})
        listed = reader.request('GET', '/alerts' + query)
        expect_status(listed, 200)
        assert listed.body['total'] == 1
        assert [row['id'] for row in listed.body['alerts']] == [a['id']]
        counted = reader.request('GET', '/alerts/count' + query)
        expect_status(counted, 200)
        assert counted.body['total'] == 1
        for hidden in [b, unscoped]:
            path = '/alert/' + hidden['id']
            before = clients['unscoped_admin'].request('GET', path)
            expect_status(before, 200)
            expect_status(reader.request('GET', path), 404)
            for method, suffix, payload in [('PUT', '/status', {'status': 'ack'}),
                                            ('PUT', '/action', {'action': 'ack'}),
                                            ('DELETE', '', None)]:
                expect_status(reader.request(method, path + suffix, payload), 404)
                # A denied mutation must leave all observable alert data intact.
                after = clients['unscoped_admin'].request('GET', path)
                expect_status(after, 200)
                assert after.body['alert'] == before.body['alert']
