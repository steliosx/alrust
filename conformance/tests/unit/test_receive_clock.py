from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from alrust_conformance.receive_clock import ClockError, ReceiveClock
from alrust_conformance.client import Response
from alrust_conformance import differential


def request(index, severity='major'):
    return {'method': 'POST', 'path': '/alert', 'payload': {
        'id': f'aaaaaaaa-bbbb-4ccc-8ddd-{index:012x}',
        'resource': 'alrust-synthetic-clock', 'environment': 'Development',
        'event': 'SyntheticP0Probe', 'severity': severity,
        'createTime': f'2026-09-29T20:00:00.{index:03d}Z'}}


def clock(second, offset=0):
    return (datetime(2026, 9, 29, 20) + timedelta(seconds=second+offset)).isoformat(
        timespec='milliseconds') + 'Z'


def body(req, previous=None, *, second=0, offset=0):
    payload = req['payload']
    duplicate = previous is not None and previous['alert']['severity'] == payload['severity']
    status = 'closed' if payload['severity'] == 'normal' else 'open'
    changed = previous is not None and previous['alert']['status'] != status
    alert = {field: payload[field] for field in ('environment', 'resource', 'event', 'severity')}
    alert.update(id=payload['id'] if previous is None else previous['id'],
                 customer=None, status=status, duplicateCount=(previous['alert']['duplicateCount']+1 if duplicate else 0),
                 repeat=duplicate, lastReceiveId=payload['id'],
                 createTime=previous['alert']['createTime'] if duplicate else payload['createTime'],
                 receiveTime=previous['alert']['receiveTime'] if duplicate else clock(second, offset),
                 lastReceiveTime=clock(second, offset),
                 updateTime=(clock(second, offset) if previous is None or changed else previous['alert']['updateTime']),
                 attributes={'ip': '127.0.0.1', 'userTime': clock(0)})
    history = deepcopy(previous['alert']['history']) if previous else []
    if not duplicate or changed:
        history.insert(0, {'status': status, 'updateTime': payload['createTime'],
                           'severity': payload['severity']})
    alert['history'] = history
    return {'status': 'ok', 'id': alert['id'], 'alert': alert}


def test_independent_absolute_clocks_keep_receive_retention_and_transition_checks():
    oracles = [ReceiveClock(), ReceiveClock()]
    previous = [None, None]
    branches = []
    for index, severity in enumerate(['major', 'major', 'minor', 'normal', 'normal', 'major']):
        req = request(index, severity)
        raw = [body(req, previous[side], second=index, offset=side*3600) for side in range(2)]
        saved = deepcopy(raw)
        projected = [oracles[side].project(req, raw[side], f'http://side{side}') for side in range(2)]
        assert projected[0][0] == projected[1][0]
        assert projected[0][1] == projected[1][1]
        assert raw == saved  # exported raw evidence is never replaced by tokens
        branches.append(projected[0][1]['branch'])
        previous = raw
    assert branches == ['create', 'dedup', 'correlate', 'correlate', 'dedup', 'correlate']


@pytest.mark.parametrize('fault', ['missing', 'null', 'non-utc', 'stale', 'backwards',
                                  'receive-changed', 'update-changed', 'client-time', 'history-time',
                                  'history-tie', 'wrong-branch', 'wrong-transition'])
def test_invalid_clock_or_branch_cannot_be_projected_to_a_pass(fault):
    oracle = ReceiveClock()
    initial = body(request(0))
    oracle.project(request(0), initial, 'http://left')
    req = request(1)
    changed = body(req, initial, second=1)
    alert = changed['alert']
    if fault == 'missing':
        del alert['lastReceiveTime']
    elif fault == 'null':
        alert['lastReceiveTime'] = None
    elif fault == 'non-utc':
        alert['lastReceiveTime'] = '2026-09-29T20:00:01.000+00:00'
    elif fault == 'stale':
        alert['lastReceiveTime'] = initial['alert']['lastReceiveTime']
    elif fault == 'backwards':
        alert['lastReceiveTime'] = clock(-1)
    elif fault == 'receive-changed':
        alert['receiveTime'] = clock(1)
    elif fault == 'update-changed':
        alert['updateTime'] = clock(1)
    elif fault == 'client-time':
        req['payload']['createTime'] = initial['alert']['createTime']
    elif fault == 'history-time':
        alert['history'][0]['updateTime'] = clock(1)
    elif fault == 'history-tie':
        alert['history'].append(deepcopy(alert['history'][0]))
    elif fault == 'wrong-branch':
        alert['duplicateCount'] = 0
    else:
        alert['status'] = 'closed'
    with pytest.raises(ClockError):
        oracle.project(req, changed, 'http://left')


@pytest.mark.parametrize('field', ['customer', 'unknown', 'createTime', 'attributes', 'history'])
def test_projected_comparison_keeps_semantic_and_unknown_differences(field):
    req = request(0)
    left = body(req)
    right = deepcopy(left)
    if field == 'customer':
        right['alert']['customer'] = 'other'
    elif field == 'unknown':
        right['alert']['extraTime'] = clock(5)
    elif field == 'attributes':
        right['alert']['attributes']['ip'] = 'different'
    elif field == 'history':
        right['alert']['history'][0]['severity'] = 'warning'
    else:
        right['alert']['createTime'] = clock(5)
    a, _ = ReceiveClock().project(req, left, 'http://left')
    if field == 'createTime':
        with pytest.raises(ClockError):
            ReceiveClock().project(req, right, 'http://right')
    else:
        b, _ = ReceiveClock().project(req, right, 'http://right')
        assert a != b


def test_replay_uses_projection_spacing_raw_evidence_and_owned_cleanup(monkeypatch):
    elapsed = [0.0]
    monkeypatch.setattr(differential.time, 'monotonic', lambda: elapsed[0])
    monkeypatch.setattr(differential.time, 'sleep', lambda duration: elapsed.__setitem__(0, elapsed[0]+duration))
    requests = [request(0), request(1), request(2, 'normal')]
    clients = []
    for side in range(2):
        previous = None
        responses = []
        for index, req in enumerate(requests):
            previous = body(req, previous, second=index, offset=side*3600)
            responses.append(previous)
        class FakeClient:
            def __init__(self, side, responses):
                self.base_url = f'http://127.0.0.1:{18081+side}'
                self.responses = iter(responses)
                self.observations = []
                self.deleted = []
            def request(self, method, path, payload=None):
                if method == 'DELETE':
                    self.deleted.append(path)
                    return Response(200, {'status': 'ok'})
                sent = elapsed[0]
                elapsed[0] += 0.005
                self.observations.append((sent, elapsed[0]))
                return Response(201, next(self.responses))
        clients.append(FakeClient(side, responses))
    snapshots = []
    result = differential.replay(*clients, requests, checked_clocks=True,
                                 on_progress=lambda partial: snapshots.append(deepcopy(partial)))
    assert result['status'] == 'LOCAL_PASS'
    assert len(result['requests']) == 3 and len(result['cleanup']) == 2
    for client in clients:
        assert all(next_sent - prior_received >= 0.049999 for (_, prior_received), (next_sent, _) in
                   zip(client.observations, client.observations[1:]))
        assert len(client.deleted) == 1
    row = result['requests'][0]
    assert row['responses'][0]['body']['alert']['receiveTime'] != row['responses'][1]['body']['alert']['receiveTime']
    assert row['responses'][0]['client_send_time'] and row['responses'][0]['client_receive_time']
    assert snapshots[-1]['cleanup']
