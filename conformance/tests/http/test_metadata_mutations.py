"""Source-derived per-ID metadata mutation cases; HTTP NOT_RUN at this head."""
from test_metadata_edges import metadata_session


def test_missing_or_empty_tag_inputs_return_500_without_changing_alert(metadata_session):
    # views/alerts.py:200-245; exceptions.py:10-19,64-65 (ApiError defaults500).
    path, _ = metadata_session.create()
    before = metadata_session.read(path)
    for endpoint in ('tag', 'untag'):
        for payload in ({}, {'tags': []}):
            error = metadata_session.request('PUT', path + '/' + endpoint, payload, 500)
            assert error['status'] == 'error'
            assert error['code'] == 500
            assert error['message'] == "must supply 'tags' as json list"
            assert metadata_session.read(path) == before


def test_untag_all_returns_empty_api_tags_and_preserves_other_fields(metadata_session):
    # views/alerts.py:226-245; postgres/base.py:321-328; alert.py:62,216.
    # API [] does not by itself establish whether the database stores NULL.
    path, _ = metadata_session.create()
    before = metadata_session.read(path)
    assert len(before['tags']) == 2
    assert metadata_session.request('PUT', path + '/untag', {
        'tags': before['tags']}) == {'status': 'ok'}
    after = metadata_session.read(path)
    assert after['tags'] == []
    assert {k: v for k, v in after.items() if k != 'tags'} == {
        k: v for k, v in before.items() if k != 'tags'}


def test_removing_last_attribute_returns_500_but_change_is_observable(metadata_session):
    # views/alerts.py:254-273; postgres/base.py:339-349,:1568-1576.
    # Nonempty input removes every attested stored attribute; the returned map is
    # falsy after the write commits. An HTTP error is not a rollback guarantee.
    path, _ = metadata_session.create()
    before = metadata_session.read(path)
    # remote_ip derives ip from the request route; no forwarding header is sent.
    # Attest both known keys and their value prerequisites before removing all.
    attributes = before['attributes']
    assert set(attributes) == {'SYNTHETIC-keep', 'ip'}
    assert attributes['SYNTHETIC-keep'] == 'retained'
    assert isinstance(attributes['ip'], str) and attributes['ip']
    error = metadata_session.request('PUT', path + '/attributes', {
        'attributes': {key: None for key in attributes}}, 500)
    assert error['status'] == 'error'
    assert error['code'] == 500
    assert error['message'] == 'failed to update attributes'
    after = metadata_session.read(path)
    assert after['attributes'] == {}
    # No history or timestamp normalization; SQL changes only attributes.
    assert {k: v for k, v in after.items() if k != 'attributes'} == {
        k: v for k, v in before.items() if k != 'attributes'}
