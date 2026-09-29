"""Initial synthetic contract cases, cited to reference f431272 (not full parity)."""
from datetime import datetime, timezone
import uuid


def test_health(target):
    # alerta/management/views.py:137-154.
    response = target.request('GET', '/management/healthcheck')
    assert response.status == 200
    assert response.body == 'OK'


def test_synthetic_create_and_deduplicate(target):
    # alerta/views/alerts.py:38-100; models/alert.py:285-335,382-413.
    namespace = 'alrust-synthetic-' + uuid.uuid4().hex
    payload = {'id': str(uuid.uuid4()), 'resource': namespace,
               'event': 'SyntheticP0Smoke', 'environment': 'Development',
               'severity': 'major', 'service': ['alrust-conformance'],
               'tags': ['SYNTHETIC'], 'origin': 'alrust-synthetic',
               'createTime': datetime.now(timezone.utc).isoformat(), 'timeout': 86400}
    created_ids = set()
    try:
        created = target.request('POST', '/alert', payload)
        if created.status == 201:
            created_ids.add(str(uuid.UUID(created.body['id'])))
        assert created.status == 201
        assert created.body['status'] == 'ok'
        alert = created.body['alert']
        assert alert['resource'] == namespace
        assert alert['event'] == payload['event']
        assert alert['duplicateCount'] == 0
        assert alert['repeat'] is False
        payload['id'] = str(uuid.uuid4())
        duplicate = target.request('POST', '/alert', payload)
        if duplicate.status == 201:
            created_ids.add(str(uuid.UUID(duplicate.body['id'])))
        assert duplicate.status == 201
        assert duplicate.body['id'] == created.body['id']
        assert duplicate.body['alert']['duplicateCount'] == 1
        assert duplicate.body['alert']['repeat'] is True
        detail = target.request('GET', '/alert/' + created.body['id'])
        assert detail.status == 200
        assert detail.body['alert']['duplicateCount'] == 1
    finally:
        for alert_id in sorted(created_ids):
            assert target.request('DELETE', '/alert/' + alert_id).status == 200
