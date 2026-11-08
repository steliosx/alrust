"""Receive-only clock projection for the controlled synthetic replay fixture.

Reference f431272: models/alert.py:285-413; postgres/base.py:216-262.
The branch oracle runs before the status hook. Default plugins only; no action,
multi-row, history tie, proxy or custom-hook vector is accepted by this policy.
"""
from copy import deepcopy
from datetime import datetime
import re

from .normalize import canonical, normalize


POLICY = 'receive-clock-v1; checked alert receiveTime,lastReceiveTime,updateTime; selected href bases'
FIELDS = ('receiveTime', 'lastReceiveTime', 'updateTime')
SEVERITIES = {'major', 'minor', 'warning', 'normal'}


class ClockError(ValueError):
    pass


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z', value):
        raise ClockError('required UTC millisecond clock format')
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%fZ')
    except ValueError:
        raise ClockError('invalid calendar clock') from None


class ReceiveClock:
    def __init__(self):
        self.previous = {}
        self.last_client_time = {}

    def project(self, request, body, base_url):
        payload = request['payload']
        if (request['method'] != 'POST' or request['path'] != '/alert'
                or payload.get('environment') != 'Development'
                or payload.get('event') != 'SyntheticP0Probe'
                or not payload.get('resource', '').startswith('alrust-synthetic-')
                or payload.get('severity') not in SEVERITIES
                or payload.get('status', 'open') != 'open'
                or payload.get('correlate', []) != []
                or payload.get('value') is not None):
            raise ClockError('unsupported receive clock vector')
        alert = body['alert']
        key = (payload['environment'], payload['resource'], payload['event'])
        client_time = timestamp(payload['createTime'])
        if key in self.last_client_time and client_time <= self.last_client_time[key]:
            raise ClockError('ordinary client timestamps must strictly increase')
        previous = self.previous.get(key)
        history = alert.get('history')
        if not isinstance(history, list) or not history:
            raise ClockError('history required for the branch oracle')
        history_times = [timestamp(row['updateTime']) for row in history]
        # Alert.serialize returns history ascending by update_time (alert.py:149).
        # Ordinary fixture timestamps are distinct, so the newest entry is last.
        if any(left >= right for left, right in zip(history_times, history_times[1:])):
            raise ClockError('history ties or non-ascending history unsupported')
        for field in FIELDS:
            timestamp(alert.get(field))
        if any(alert.get(field) != payload[field] for field in ('environment', 'resource', 'event', 'severity')):
            raise ClockError('response does not match the controlled identity')
        current = previous['history'][-1]['status'] if previous else 'open'
        if current not in ('open', 'closed'):
            raise ClockError('unsupported history current status')
        # This fixture has only open/closed states, default incoming status and
        # default plugins. Compute transition before any hook, never infer it
        # from the final stored status or duplicateCount alone.
        expected_status = 'closed' if payload['severity'] == 'normal' else 'open'
        changed = expected_status != current
        branch = ('create' if previous is None else
                  'dedup' if previous['severity'] == payload['severity'] else 'correlate')
        expected_count = previous['duplicateCount'] + 1 if branch == 'dedup' else 0
        if (alert.get('status') != expected_status
                or type(alert.get('duplicateCount')) is not int
                or alert['duplicateCount'] != expected_count
                or alert.get('repeat') is not (branch == 'dedup')
                or alert.get('lastReceiveId') != payload['id']
                or alert.get('id') != (payload['id'] if previous is None else previous['id'])):
            raise ClockError('receive branch semantics do not match the fixture')
        if previous and alert.get('customer') != previous.get('customer'):
            raise ClockError('customer identity changed')
        expected_create = previous['createTime'] if branch == 'dedup' else payload['createTime']
        if alert.get('createTime') != expected_create:
            raise ClockError('client createTime retention differs')
        if branch != 'dedup' or changed:
            if history[-1]['updateTime'] != payload['createTime'] or history[-1]['status'] != expected_status:
                raise ClockError('receive history must retain the client timestamp and transition')
        elif canonical(history) != canonical(previous['history']):
            raise ClockError('unchanged duplicate unexpectedly changed history')
        if previous and timestamp(alert['lastReceiveTime']) <= timestamp(previous['lastReceiveTime']):
            raise ClockError('required fresh clock did not advance despite request spacing')
        if branch == 'dedup':
            if alert['receiveTime'] != previous['receiveTime']:
                raise ClockError('duplicate receiveTime must be retained')
        elif alert['receiveTime'] != alert['lastReceiveTime']:
            raise ClockError('create/correlate receive clocks must agree')
        expected_update = alert['lastReceiveTime'] if previous is None or changed else previous['updateTime']
        if alert['updateTime'] != expected_update:
            raise ClockError('updateTime retention/refresh differs from the pre-hook transition')
        projected = normalize(body, base_url)
        for field in FIELDS:
            projected['alert'][field] = '<checked-' + field + '>'
        self.previous[key] = deepcopy(alert)
        self.last_client_time[key] = client_time
        return projected, {'branch': branch, 'history_current_status': current,
                           'computed_pre_hook_status': expected_status,
                           'update_refreshed': previous is None or changed}
