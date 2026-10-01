"""Benign filtered alert queries at pinned f431; HTTP NOT_RUN at this head."""
from urllib.parse import urlencode

from test_metadata_edges import metadata_session


def query_path(endpoint, pairs):
    return endpoint + '?' + urlencode(pairs)


def test_filtered_list_matches_counts_and_page_last_receive_time(metadata_session):
    # views/alerts.py:318-361,388-402; postgres/base.py:410-440.
    path, _ = metadata_session.create()
    before = metadata_session.read(path)
    filters = [('resource', before['resource'])]
    listed = metadata_session.request('GET', query_path('/alerts', filters + [('page-size', '1')]))
    counts = metadata_session.request('GET', query_path('/alerts/count', filters))
    assert listed['status'] == counts['status'] == 'ok'
    assert type(listed['total']) is int and type(counts['total']) is int
    assert listed['total'] == counts['total'] == 1
    assert listed['severityCounts'] == counts['severityCounts'] == {'major': 1}
    assert listed['statusCounts'] == counts['statusCounts'] == {'open': 1}
    assert (listed['page'], listed['pageSize'], listed['pages'], listed['more']) == (1, 1, 1, False)
    assert listed['more'] is False
    assert listed['lastTime'] == before['lastReceiveTime']
    assert isinstance(listed['autoRefresh'], bool)
    assert listed['autoRefresh'] == counts['autoRefresh']
    assert len(listed['alerts']) == 1
    expected = {**before, 'history': []}
    assert before['rawData'] is None  # This fixture does not cover non-null rawData display.
    assert listed['alerts'][0] == expected
    assert metadata_session.read(path) == before


def test_history_display_flag_preserves_saved_alert_fields(metadata_session):
    # views/alerts.py:319-320; postgres/base.py:412-423; alert.py:204-232.
    path, _ = metadata_session.create()
    before = metadata_session.read(path)
    assert len(before['history']) == 1
    for flag, expected in [('true', before), ('false', {**before, 'history': []})]:
        listed = metadata_session.request('GET', query_path('/alerts', [
            ('resource', before['resource']), ('page-size', '1'), ('show-history', flag)]))
        assert listed['status'] == 'ok' and listed['total'] == 1
        assert listed['alerts'] == [expected]
        assert listed['lastTime'] == before['lastReceiveTime']
    assert metadata_session.read(path) == before


def test_repeated_plain_resource_filter_and_event_negation_counts(metadata_session):
    # postgres/utils.py:55-104,:109-198; views/alerts.py:388-402.
    path, _ = metadata_session.create()
    before = metadata_session.read(path)
    # The synthetic nonmatching value cannot equal the owned resource.
    filters = [('resource', before['resource'] + '-missing'), ('resource', before['resource'])]
    counts = metadata_session.request('GET', query_path('/alerts/count', filters))
    assert counts['status'] == 'ok' and counts['total'] == 1
    assert counts['severityCounts'] == {'major': 1} and counts['statusCounts'] == {'open': 1}
    assert isinstance(counts['autoRefresh'], bool)
    empty = metadata_session.request('GET', query_path('/alerts/count', filters + [
        ('event!', before['event'])]))
    assert empty == {'status': 'ok', 'total': 0, 'severityCounts': {},
                     'statusCounts': {}, 'autoRefresh': counts['autoRefresh']}
    assert metadata_session.read(path) == before
