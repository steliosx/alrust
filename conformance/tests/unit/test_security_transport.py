import json

import httpx
import pytest

from alrust_conformance.security_transport import Identity, SyntheticKey, security_request


KEY = 'SYNTHETIC-' + 'a' * 48


def invoke(transport, **kwargs):
    values = dict(identity=Identity.ANONYMOUS, method='GET', path='/alerts', transport=transport)
    values.update(kwargs)
    return security_request('http://fixture.invalid/api', **values)


def test_anonymous_does_not_inherit_environment_netrc_proxy_or_response_cookies(monkeypatch, tmp_path):
    monkeypatch.setenv('ALRUST_API_KEY', KEY)
    monkeypatch.setenv('ALRUST_REFERENCE_API_KEY', KEY)
    monkeypatch.setenv('HTTP_PROXY', 'http://proxy-user:proxy-secret@proxy.invalid:8888')
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy-user:proxy-secret@proxy.invalid:8888')
    monkeypatch.setenv('ALL_PROXY', 'http://proxy-user:proxy-secret@proxy.invalid:8888')
    netrc = tmp_path / 'netrc'
    netrc.write_text('machine fixture.invalid login netrc-user password netrc-secret\n')
    monkeypatch.setenv('NETRC', str(netrc))
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={'status': 'ok'}, headers={'set-cookie': 'auth=secret; Path=/'})
    transport = httpx.MockTransport(handler)
    invoke(transport, identity=SyntheticKey(KEY))
    invoke(transport)
    invoke(transport)
    assert seen[0].headers['authorization'] == 'Key ' + KEY
    for request in seen[1:]:
        assert not {'authorization', 'proxy-authorization', 'cookie'} & request.headers.keys()
        assert request.url == 'http://fixture.invalid/api/alerts'


@pytest.mark.parametrize('identity', [Identity.ANONYMOUS, SyntheticKey(KEY)])
def test_cron_pair_is_fixed_and_does_not_replace_explicit_identity(identity):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(401, json={'status': 'error', 'message': 'denied'})
    transport = httpx.MockTransport(handler)
    invoke(transport, identity=identity, method='POST', path='/alert',
           payload={'resource': 'synthetic'}, cron_headers=True)
    invoke(transport, identity=identity)
    assert seen[0].headers['x-appengine-cron'] == 'true'
    assert seen[0].headers['x-forwarded-for'] == '0.1.0.1'
    assert json.loads(seen[0].content) == {'resource': 'synthetic'}
    assert 'x-request-id' not in seen[0].headers
    assert 'x-appengine-cron' not in seen[1].headers
    assert 'x-forwarded-for' not in seen[1].headers
    assert seen[0].headers.get('authorization') == ('Key ' + KEY if type(identity) is SyntheticKey else None)


@pytest.mark.parametrize('identity', [None, '', KEY, 'anonymous', False, {}])
def test_missing_or_implicit_identity_is_rejected_before_transport(identity):
    calls = []
    with pytest.raises(ValueError):
        invoke(httpx.MockTransport(lambda request: calls.append(request)), identity=identity)
    assert calls == []


@pytest.mark.parametrize('key', [None, '', 'operator-secret', 'SYNTHETIC-short', 1])
def test_invalid_credential_does_not_echo_value(key):
    with pytest.raises(ValueError) as error:
        SyntheticKey(key)
    assert str(error.value) == 'security cases require a labeled synthetic credential'
    assert KEY not in repr(SyntheticKey(KEY))


@pytest.mark.parametrize('value', [None, 0, 1, 'true', {'X-Appengine-Cron': 'true'}])
def test_cron_control_cannot_smuggle_headers(value):
    with pytest.raises(ValueError):
        invoke(httpx.MockTransport(lambda request: pytest.fail('unexpected request')), cron_headers=value)


def test_arbitrary_headers_and_inherited_clients_are_not_parameters():
    for kwargs in ({'headers': {'Authorization': 'secret'}}, {'client': object()}, {'cookies': {}}):
        with pytest.raises(TypeError):
            invoke(None, **kwargs)
    with pytest.raises(TypeError):
        security_request('http://fixture.invalid', method='GET', path='/')


@pytest.mark.parametrize('url', ['http://user:secret@fixture.invalid', 'http://@fixture.invalid',
                               'file:///tmp/a', 'http://fixture.invalid?key=secret',
                               'http://fixture.invalid/#secret', 'http://fixture.invalid:bad',
                               'http://fixture.invalid:99999', 'http://fixture.invalid\\@other',
                               ' http://fixture.invalid'])
def test_unsafe_base_rejected_before_transport(url):
    with pytest.raises(ValueError):
        security_request(url, identity=Identity.ANONYMOUS, method='GET', path='/',
                         transport=httpx.MockTransport(lambda request: pytest.fail('unexpected request')))


@pytest.mark.parametrize('path', ['//other.invalid', 'http://other.invalid', '/\\other',
                                '/alerts#fragment', '/alert\nheader'])
def test_unsafe_path_rejected_before_transport(path):
    with pytest.raises(ValueError):
        invoke(httpx.MockTransport(lambda request: pytest.fail('unexpected request')), path=path)


def test_redirects_are_retained_and_not_followed():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={'location': 'http://other.invalid/'}, text='redirect')
    response = invoke(httpx.MockTransport(handler), identity=SyntheticKey(KEY))
    assert response.status == 302 and response.body == 'redirect'
    assert len(seen) == 1


def test_json_errors_remain_visible_and_transport_closes():
    class TrackingTransport(httpx.MockTransport):
        closed = False
        def close(self):
            self.closed = True
    transport = TrackingTransport(lambda request: httpx.Response(
        401, headers={'content-type': 'application/problem+json'}, text='broken-json'))
    with pytest.raises(ValueError):
        invoke(transport)
    assert transport.closed


def test_transport_error_propagates_and_closes():
    class TrackingTransport(httpx.MockTransport):
        closed = False
        def close(self):
            self.closed = True
    def handler(request):
        raise httpx.ReadTimeout('synthetic timeout')
    transport = TrackingTransport(handler)
    with pytest.raises(httpx.ReadTimeout):
        invoke(transport)
    assert transport.closed


@pytest.mark.parametrize('path', ['/../escape', '/./alerts', '/x/../../escape',
                                '/%2e%2e/escape', '/%2E/alerts', '/.%2e/escape',
                                '/%2e./escape', '/x%2f..%2f..%2fescape',
                                '/x%5c..%5cescape'])
def test_dot_segments_and_encoded_separators_refused_before_transport(path):
    with pytest.raises(ValueError):
        invoke(httpx.MockTransport(lambda request: pytest.fail('unexpected request')), path=path)


@pytest.mark.parametrize('suffix', ['/api/..', '/api/.', '/api/%2e%2e', '/api%2f..'])
def test_base_dot_segments_refused_before_transport(suffix):
    with pytest.raises(ValueError):
        security_request('http://fixture.invalid' + suffix, identity=Identity.ANONYMOUS,
                         method='GET', path='/alerts',
                         transport=httpx.MockTransport(lambda request: pytest.fail('unexpected request')))


def test_query_dots_and_ordinary_dotted_segments_keep_base_prefix():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={'status': 'ok'})
    invoke(httpx.MockTransport(handler), path='/alerts.v1?...=../escape')
    assert seen[0].url.path == '/api/alerts.v1'
    assert seen[0].url.query == b'...=../escape'
