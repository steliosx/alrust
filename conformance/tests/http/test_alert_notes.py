"""Ordinary owned note cases at reference f431272; HTTP NOT_RUN at this head."""
from datetime import datetime
import re
import time
import uuid

from test_metadata_edges import metadata_session


def add_owned_note(session, path, payload, owned):
    body = session.request('PUT', path + '/note', payload, 201)
    identifier = str(uuid.UUID(body['id']))
    owned.add(identifier)  # Track successful creation before further assertions.
    assert body['status'] == 'ok' and body['note']['id'] == identifier
    return body['note']


def timestamp(value):
    assert isinstance(value, str) and re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z', value)
    return datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%fZ')


def assert_created_note(note, alert, text):
    # models/note.py:18-28,44-67,110-126; utils/format.py:64-65.
    assert set(note) == {'id', 'href', 'text', 'user', 'attributes', 'type',
                         'createTime', 'updateTime', '_links', 'related', 'customer'}
    assert uuid.UUID(note['id']).version == 4
    assert note['text'] == text and note['type'] == 'alert'
    assert note['user'] == 'alrust-synthetic-admin'
    assert note['customer'] == alert['customer']
    assert note['related'] == {'alert': alert['id']}
    assert note['href'].endswith('/note/' + note['id'])
    assert set(note['_links']) == {'alert'}
    assert note['_links']['alert'].endswith('/alert/' + alert['id'])
    assert note['attributes'] == {field: alert[field] for field in (
        'resource', 'event', 'environment', 'severity', 'status')}
    timestamp(note['createTime'])
    assert note['updateTime'] is None


def assert_note_history(entry, alert, note, kind, text):
    # models/history.py:8-34; the history href also uses the note ID.
    timestamp(entry['updateTime'])
    assert {k: v for k, v in entry.items() if k != 'updateTime'} == {
        'id': note['id'],
        'href': note['href'].rsplit('/note/', 1)[0] + '/alert/' + note['id'],
        'event': alert['event'], 'severity': alert['severity'],
        'status': alert['status'], 'value': alert['value'],
        'text': text, 'type': kind, 'user': note['user'], 'timeout': None}


def test_owned_note_create_update_delete_preserves_alert_fields(metadata_session):
    # views/alerts.py:596-722; models/alert.py:572-607;
    # postgres/base.py:401-408,1368-1386,1397-1402,1411-1441.
    path, _ = metadata_session.create()
    before = metadata_session.read(path)
    assert metadata_session.request('GET', path + '/notes') == {
        'status': 'ok', 'notes': [], 'total': 0, 'message': 'not found'}
    owned = set()
    try:
        time.sleep(0.05)  # Distinct app-clock history entries; no normalization.
        note = add_owned_note(metadata_session, path, {'text': 'SYNTHETIC note create'}, owned)
        assert_created_note(note, before, 'SYNTHETIC note create')
        assert metadata_session.request('GET', path + '/notes') == {
            'status': 'ok', 'notes': [note], 'total': 1}
        with_note = metadata_session.read(path)
        assert with_note['history'][:-1] == before['history']
        assert len(with_note['history']) == len(before['history']) + 1
        entry = with_note['history'][-1]
        assert_note_history(entry, before, note, 'note', note['text'])
        assert {k: v for k, v in with_note.items() if k != 'history'} == {
            k: v for k, v in before.items() if k != 'history'}
        attrs = {'resource': 'SYNTHETIC-note-only-resource',
                 'SYNTHETIC-note-extra': 'retained', 'SYNTHETIC-note-null': None}
        updated = metadata_session.request('PUT', path + '/note/' + note['id'], {
            'text': 'SYNTHETIC note update', 'attributes': attrs})
        assert updated['status'] == 'ok'
        changed = updated['note']
        assert changed['text'] == 'SYNTHETIC note update'
        assert changed['attributes'] == {**note['attributes'], **attrs}
        timestamp(changed['updateTime'])
        assert {k: v for k, v in changed.items() if k not in (
            'text', 'attributes', 'updateTime')} == {
            k: v for k, v in note.items() if k not in ('text', 'attributes', 'updateTime')}
        assert metadata_session.request('GET', path + '/notes') == {
            'status': 'ok', 'notes': [changed], 'total': 1}
        assert metadata_session.read(path) == with_note  # Update adds no alert history.
        time.sleep(0.05)
    finally:
        for identifier in sorted(owned):
            assert metadata_session.request('DELETE', path + '/note/' + identifier) == {'status': 'ok'}
    assert metadata_session.request('GET', path + '/notes') == {
        'status': 'ok', 'notes': [], 'total': 0, 'message': 'not found'}
    dismissed = metadata_session.read(path)
    assert dismissed['history'][:-1] == with_note['history']
    assert len(dismissed['history']) == len(with_note['history']) + 1
    entry = dismissed['history'][-1]
    assert_note_history(entry, before, note, 'dismiss', 'note dismissed')
    assert timestamp(entry['updateTime']) > timestamp(with_note['history'][-1]['updateTime'])
    assert {k: v for k, v in dismissed.items() if k != 'history'} == {
        k: v for k, v in before.items() if k != 'history'}


def test_missing_note_text_is_read_only_and_alias_fallback_works(metadata_session):
    # views/alerts.py:601-613,637-662; models/note.py:110-126.
    path, _ = metadata_session.create()
    before = metadata_session.read(path)
    for payload in ({}, {'text': ''}, {'text': '', 'note': ''}):
        body = metadata_session.request('PUT', path + '/note', payload, 400)
        assert body['status'] == 'error' and body['code'] == 400
        assert body['message'] == "must supply 'note' text"
        assert metadata_session.read(path) == before
        assert metadata_session.request('GET', path + '/notes') == {
            'status': 'ok', 'notes': [], 'total': 0, 'message': 'not found'}
    owned = set()
    try:
        note = add_owned_note(metadata_session, path, {
            'text': '', 'note': 'SYNTHETIC fallback note'}, owned)
        assert_created_note(note, before, 'SYNTHETIC fallback note')
        assert metadata_session.request('GET', path + '/notes') == {
            'status': 'ok', 'notes': [note], 'total': 1}
    finally:
        for identifier in sorted(owned):
            assert metadata_session.request('DELETE', path + '/note/' + identifier) == {'status': 'ok'}
