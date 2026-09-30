"""Pinned parser/query expectations; new cases are source-derived, HTTP NOT_RUN."""
import uuid

import pytest

from test_heartbeats import heartbeats  # Reuse reviewed fixture and owned cleanup.


@pytest.mark.parametrize('field,message', [
    ('tags', 'tags must be a list'),
    ('attributes', 'attributes must be a JSON object'),
])
def test_present_null_container_is_rejected_before_storage(heartbeats, field, message):
    # models/heartbeat.py:74-79; views/heartbeats.py:22-25.
    payload = {'id': str(uuid.uuid4()), 'origin': heartbeats.origin,
               'tags': ['SYNTHETIC'], 'attributes': {'SYNTHETIC-marker': 'null-probe'},
               'createTime': '2000-01-01T00:00:00.000Z', 'timeout': 86400,
               field: None}
    error = heartbeats.request('unscoped_admin', 'POST', '/heartbeat', payload, 400)
    assert error['status'] == 'error' and error['code'] == 400
    assert error['message'] == message
    listed = heartbeats.list('unscoped_admin')
    assert listed['status'] == 'ok' and listed['total'] == 0
    assert listed['heartbeats'] == [] and listed['message'] == 'not found'


def test_unknown_only_status_is_ignored(heartbeats):
    # postgres/utils.py:302-307; postgres/base.py:890-908.
    a = heartbeats.create('a_admin')
    unscoped = heartbeats.create()
    listed = heartbeats.list('unscoped_admin', status='SYNTHETIC-unknown')
    assert listed['status'] == 'ok' and listed['total'] == 2
    assert {row['id'] for row in listed['heartbeats']} == {a['id'], unscoped['id']}
    assert all(row['status'] == 'slow' for row in listed['heartbeats'])
    scoped = heartbeats.list('a_admin', status='SYNTHETIC-unknown')
    assert scoped['total'] == 1 and [row['id'] for row in scoped['heartbeats']] == [a['id']]


def test_nonempty_status_page_uses_prefilter_total_and_paging(heartbeats):
    # views/heartbeats.py:62-87; utils/paging.py:9-33;
    # postgres/base.py:886-927: COUNT before status, LIMIT/OFFSET after status.
    slow = heartbeats.create('a_admin')
    future = '2100-01-01T00:00:00.000Z'
    payload = {'id': str(uuid.uuid4()), 'origin': heartbeats.origin,
               'tags': ['SYNTHETIC'], 'attributes': {'SYNTHETIC-marker': 'future-clock'},
               'createTime': future, 'timeout': 86400}
    body = heartbeats.request('unscoped_admin', 'POST', '/heartbeat', payload, 201)
    assert body['status'] == 'ok' and body['heartbeat']['id'] == body['id']
    ok = body['heartbeat']
    assert ok['origin'] == heartbeats.origin and ok['customer'] is None
    assert ok['createTime'] == future and ok['timeout'] == 86400
    assert ok['maxLatency'] == 2000 and ok['latency'] < 0 and ok['status'] == 'ok'
    assert ok['tags'] == payload['tags'] and ok['attributes'] == payload['attributes']
    assert slow['id'] != ok['id']
    fields = {'status': 'slow', 'page-size': 1}
    first = heartbeats.list('unscoped_admin', **fields)
    assert first['status'] == 'ok' and first['total'] == 2
    assert first['page'] == 1 and first['pageSize'] == 1
    assert first['pages'] == 2 and first['more'] is True
    assert [row['id'] for row in first['heartbeats']] == [slow['id']]
    assert first['heartbeats'][0]['status'] == 'slow'
    second = heartbeats.list('unscoped_admin', page=2, **fields)
    assert second['status'] == 'ok' and second['total'] == 0
    assert second['page'] == 2 and second['pageSize'] == 1
    assert second['pages'] == 2 and second['more'] is False
    assert second['heartbeats'] == [] and second['message'] == 'not found'
