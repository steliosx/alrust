"""Synthetic offline gates and independent comparison cases; no HTTP or SQL."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import traceback

import pytest

from alrust_conformance.api_key_session import shared_evidence_safe
from alrust_conformance.client import Response
from alrust_conformance.config_session import EXPECTED_CONFIG
from alrust_conformance.root_index_session import (
    CONNECTION_CHECKS, MAX_RECORD_BYTES, REFERENCE_HEAD, REFERENCE_TARGET,
    TASK, RootIndexSession, load_root_index_attestation,
)

WINDOW = 'alrust-root-index-reference-20260930-01'
PID = '12345'
SENTINEL = 'SYNTHETIC-' + 'a' * 48


class StubClient:
    def __init__(self, response):
        self.response, self.calls, self.closed = response, [], False

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def close(self):
        self.closed = True


def identity():
    return {'project': 'alrust', 'fixture': 'SYNTHETIC', 'task': TASK,
            'reference_head': REFERENCE_HEAD, 'target': REFERENCE_TARGET,
            'window_id': WINDOW, 'server_pid': int(PID)}


def inventory():
    # Synthetic observed map order is deliberately not href sorted, and includes
    # equal-href ties, an ordinary/custom tie, an exact duplicate and an empty method.
    return {**identity(), 'uri': REFERENCE_TARGET + '/', 'ordinary_rules': [
        {'endpoint': 'z', 'rule': '/z/<id>', 'methods': ['OPTIONS', 'GET', 'HEAD', 'POST']},
        {'endpoint': 'tie.first', 'rule': '/webhooks/synthetic', 'methods': ['GET', 'HEAD']},
        {'endpoint': 'tie.second', 'rule': '/webhooks/synthetic', 'methods': ['POST', 'OPTIONS']},
        {'endpoint': 'api.index', 'rule': '/', 'methods': ['OPTIONS', 'GET', 'HEAD']},
        {'endpoint': 'empty', 'rule': '/empty', 'methods': ['HEAD', 'OPTIONS']},
        {'endpoint': 'tie.first', 'rule': '/webhooks/synthetic', 'methods': ['GET', 'HEAD']},
    ], 'custom_rules': [
        {'endpoint': 'webhooks.synthetic', 'rule': '/webhooks/synthetic', 'methods': ['POST', 'GET']},
    ]}


def body():
    # Hand-written expected response, independent of the production oracle.
    return {'status': 'ok', 'uri': REFERENCE_TARGET + '/', 'data': {'description': 'Alerta API'},
            'links': [
        # absolute_url('/') joins an empty path against request.url_root + '/'.
        {'rel': 'api.index', 'href': REFERENCE_TARGET + '//', 'method': 'GET'},
        {'rel': 'empty', 'href': REFERENCE_TARGET + '/empty', 'method': ''},
        {'rel': 'tie.first', 'href': REFERENCE_TARGET + '/webhooks/synthetic', 'method': 'GET'},
        {'rel': 'tie.second', 'href': REFERENCE_TARGET + '/webhooks/synthetic', 'method': 'POST'},
        {'rel': 'tie.first', 'href': REFERENCE_TARGET + '/webhooks/synthetic', 'method': 'GET'},
        {'rel': 'webhooks.synthetic', 'href': REFERENCE_TARGET + '/webhooks/synthetic', 'method': 'POST,GET'},
        {'rel': 'z', 'href': REFERENCE_TARGET + '/z/<id>', 'method': 'GET,POST'},
    ]}


def write(path, data):
    path.write_text(json.dumps(data)); path.chmod(0o600)


def rechain(paths):
    p = json.loads(paths['provenance'].read_text())
    for field, name in [('connection_attestation_sha256', 'connection'),
                        ('served_config_sha256', 'config'), ('readiness_marker_sha256', 'ready'),
                        ('route_inventory_sha256', 'inventory')]:
        p[field] = hashlib.sha256(paths[name].read_bytes()).hexdigest()
    write(paths['provenance'], p)


def records(tmp_path):
    paths = {n: tmp_path / (n + '.json') for n in ['connection', 'provenance', 'ready', 'config', 'inventory']}
    write(paths['connection'], {**identity(), 'backend': 'postgresql', **{k: True for k in CONNECTION_CHECKS}})
    write(paths['ready'], {'project': 'alrust', 'fixture': 'SYNTHETIC', 'window_id': WINDOW,
                          'server_pid': int(PID), 'ready': True})
    write(paths['config'], {'project': 'alrust', 'fixture': 'SYNTHETIC', 'reference_head': REFERENCE_HEAD,
                           'target': REFERENCE_TARGET, 'effective_config': dict(EXPECTED_CONFIG)})
    write(paths['inventory'], inventory())
    write(paths['provenance'], {**identity(), 'after_readiness': True, 'same_served_connection': True})
    rechain(paths)
    return paths


def validate(paths, target=REFERENCE_TARGET, window=WINDOW, pid=PID):
    return load_root_index_attestation(paths['connection'], target, window, pid,
        paths['provenance'], paths['ready'], paths['config'], paths['inventory'])


def assert_safe(caught, session=None):
    capture = ''.join(traceback.format_exception(caught.type, caught.value, caught.tb))
    if session:
        capture += json.dumps(session.safe_summary())
    shared_evidence_safe(capture, (SENTINEL,))


def test_one_read_preserves_full_duplicate_inventory_without_output(tmp_path, capsys):
    oracle = validate(records(tmp_path))
    client = StubClient(Response(200, body())); session = RootIndexSession(client, oracle)
    session.read(); session.close()
    assert client.calls == [('GET', '/', None)] and client.closed
    assert session.safe_summary() == {'project': 'alrust', 'case': 'root-index-readonly-01',
                                     'completed': True, 'captured_response_records': 0}
    assert capsys.readouterr() == ('', '')


def test_ordinary_method_order_only_is_relaxed(tmp_path):
    actual = body(); actual['links'][-1]['method'] = 'POST,GET'
    RootIndexSession(StubClient(Response(200, actual)), validate(records(tmp_path))).read()


def test_json_object_member_order_does_not_change_values(tmp_path):
    actual = body()
    actual = dict(reversed(list(actual.items())))
    actual['links'] = [dict(reversed(list(link.items()))) for link in actual['links']]
    RootIndexSession(StubClient(Response(200, actual)), validate(records(tmp_path))).read()


@pytest.mark.parametrize('fault', ['duplicate-removed', 'extra-row', 'tie-reordered', 'list-reversed',
    'custom-method-reversed', 'registered-method-added', 'registered-method-missing',
    'registered-method-duplicate', 'method-whitespace', 'method-lowercase', 'method-head',
    'method-options', 'empty-method-comma', 'link-extra-key', 'link-missing-key', 'link-not-object',
    'method-not-string', 'literal-path-placeholder', 'uri-no-slash', 'href-trailing-slash',
    'rel-altered', 'description-altered', 'data-extra-key', 'root-extra-key', 'status-altered',
    'links-not-list', 'uri-leak', 'link-leak'])
def test_comparison_failures_are_private_and_never_retry(tmp_path, fault):
    actual = body(); links = actual['links']
    if fault == 'duplicate-removed': links.pop(4)
    elif fault == 'extra-row': links.append(deepcopy(links[0]))
    elif fault == 'tie-reordered': links[2], links[3] = links[3], links[2]
    elif fault == 'list-reversed': links.reverse()
    elif fault == 'custom-method-reversed': links[5]['method'] = 'GET,POST'
    elif fault == 'registered-method-added': links[-1]['method'] = 'GET,POST,DELETE'
    elif fault == 'registered-method-missing': links[-1]['method'] = 'GET'
    elif fault == 'registered-method-duplicate': links[-1]['method'] = 'GET,GET'
    elif fault == 'method-whitespace': links[-1]['method'] = 'GET, POST'
    elif fault == 'method-lowercase': links[-1]['method'] = 'get,POST'
    elif fault == 'method-head': links[0]['method'] = 'GET,HEAD'
    elif fault == 'method-options': links[0]['method'] = 'GET,OPTIONS'
    elif fault == 'empty-method-comma': links[1]['method'] = ','
    elif fault == 'link-extra-key': links[0]['secret'] = SENTINEL
    elif fault == 'link-missing-key': links[0].pop('method')
    elif fault == 'link-not-object': links[0] = SENTINEL
    elif fault == 'method-not-string': links[0]['method'] = ['GET']
    elif fault == 'literal-path-placeholder': links[-1]['href'] = REFERENCE_TARGET + '/z/123'
    elif fault == 'uri-no-slash': actual['uri'] = REFERENCE_TARGET
    elif fault == 'href-trailing-slash': links[-1]['href'] += '/'
    elif fault == 'rel-altered': links[0]['rel'] = 'other'
    elif fault == 'description-altered': actual['data']['description'] = 'Other'
    elif fault == 'data-extra-key': actual['data']['extra'] = SENTINEL
    elif fault == 'root-extra-key': actual['extra'] = SENTINEL
    elif fault == 'status-altered': actual['status'] = 'OK'
    elif fault == 'links-not-list': actual['links'] = {'secret': SENTINEL}
    elif fault == 'uri-leak': actual['uri'] = SENTINEL
    else: links[-1]['href'] = SENTINEL
    client = StubClient(Response(200, actual)); session = RootIndexSession(client, validate(records(tmp_path)))
    with pytest.raises(AssertionError) as caught: session.read()
    assert_safe(caught, session)
    with pytest.raises(AssertionError, match='already attempted'): session.read()
    session.close()
    assert session.attempted and not session.completed and client.closed and len(client.calls) == 1


@pytest.mark.parametrize('response', [Response(200, x) for x in [None, [], SENTINEL, b'{}']] +
    [Response(True, body()), Response('200', body()), Response(503, SENTINEL), ValueError(SENTINEL)])
def test_invalid_response_or_exception_does_not_export_or_retry(tmp_path, response):
    client = StubClient(response); session = RootIndexSession(client, validate(records(tmp_path)))
    with pytest.raises(AssertionError) as caught: session.read()
    assert_safe(caught, session)
    with pytest.raises(AssertionError, match='already attempted'): session.read()
    assert len(client.calls) == 1


def test_close_exception_is_fixed_and_private(tmp_path):
    client = StubClient(Response(200, body()))
    def fail(): raise ValueError(SENTINEL)
    client.close = fail
    with pytest.raises(AssertionError) as caught: RootIndexSession(client, validate(records(tmp_path))).close()
    assert_safe(caught)


@pytest.mark.parametrize('name', ['connection', 'provenance', 'ready', 'config', 'inventory'])
@pytest.mark.parametrize('fault', ['missing', 'public', 'symlink', 'malformed', 'oversize', 'not-object', 'duplicate-json-key'])
def test_all_private_records_reject_bad_files(tmp_path, name, fault):
    paths = records(tmp_path); p = paths[name]
    if fault == 'missing': p.unlink()
    elif fault == 'public': p.chmod(0o644)
    elif fault == 'symlink':
        link = tmp_path / 'link.json'; link.symlink_to(p); paths[name] = link
    elif fault == 'malformed': p.write_text(SENTINEL)
    elif fault == 'oversize': p.write_text(' ' * (MAX_RECORD_BYTES + 1))
    elif fault == 'duplicate-json-key': p.write_text('{"project":"alrust","project":"alrust"}')
    else: p.write_text(json.dumps([SENTINEL]))
    with pytest.raises(AssertionError) as caught: validate(paths)
    assert_safe(caught)


@pytest.mark.parametrize('name', ['connection', 'provenance', 'inventory'])
@pytest.mark.parametrize('field,value', [('project', 'other'), ('fixture', 'PRODUCTION'), ('task', 'other'),
    ('reference_head', 'other'), ('target', 'http://localhost:18081'), ('window_id', 'other'),
    ('server_pid', True), ('server_pid', 9999)])
def test_identity_mismatch_rejected_even_with_updated_hashes(tmp_path, name, field, value):
    paths = records(tmp_path); data = json.loads(paths[name].read_text()); data[field] = value
    write(paths[name], data); rechain(paths)
    with pytest.raises(AssertionError) as caught: validate(paths)
    assert_safe(caught)


@pytest.mark.parametrize('name,field,value', [('connection', 'backend', 'mongodb'),
    *[('connection', key, value) for key in sorted(CONNECTION_CHECKS) for value in [False, 1]],
    ('provenance', 'after_readiness', 1), ('provenance', 'same_served_connection', False),
    *[('provenance', key, value) for key in ['connection_attestation_sha256', 'served_config_sha256',
        'readiness_marker_sha256', 'route_inventory_sha256'] for value in ['b' * 64, SENTINEL]],
    ('ready', 'project', 'other'), ('ready', 'fixture', 'PRODUCTION'), ('ready', 'server_pid', True),
    ('ready', 'server_pid', 9999), ('ready', 'window_id', 'other'), ('ready', 'ready', 1),
    ('config', 'reference_head', 'other')])
def test_connection_readiness_provenance_requirements(tmp_path, name, field, value):
    paths = records(tmp_path); data = json.loads(paths[name].read_text()); data[field] = value; write(paths[name], data)
    with pytest.raises(AssertionError) as caught: validate(paths)
    assert_safe(caught)


@pytest.mark.parametrize('name', ['connection', 'provenance', 'ready', 'config', 'inventory'])
@pytest.mark.parametrize('fault', ['extra', 'missing-key'])
def test_shapes_are_exact(tmp_path, name, fault):
    paths = records(tmp_path); data = json.loads(paths[name].read_text())
    if fault == 'extra': data['unrecognized'] = SENTINEL
    else: data.pop('project')
    write(paths[name], data); rechain(paths)
    with pytest.raises(AssertionError) as caught: validate(paths)
    assert_safe(caught)


@pytest.mark.parametrize('name', ['connection', 'ready', 'config', 'inventory'])
def test_hashes_cover_whole_original_bytes_including_whitespace(tmp_path, name):
    paths = records(tmp_path); paths[name].write_bytes(paths[name].read_bytes() + b'\n')
    with pytest.raises(AssertionError, match='complete-record hash chain'): validate(paths)


@pytest.mark.parametrize('fault', ['uri', 'rules-not-list', 'custom-not-list', 'row-not-object',
    'missing-field', 'extra-field', 'endpoint-type', 'path-type', 'relative-path', 'methods-not-list',
    'method-type', 'duplicate-method', 'lowercase-method', 'custom-reordered', 'custom-head'])
def test_bad_route_inventory_rejected_even_with_updated_hash(tmp_path, fault):
    paths = records(tmp_path); data = inventory(); rule = data['ordinary_rules'][0]
    if fault == 'uri': data['uri'] = REFERENCE_TARGET
    elif fault == 'rules-not-list': data['ordinary_rules'] = {}
    elif fault == 'custom-not-list': data['custom_rules'] = {}
    elif fault == 'row-not-object': data['ordinary_rules'][0] = SENTINEL
    elif fault == 'missing-field': rule.pop('endpoint')
    elif fault == 'extra-field': rule['extra'] = SENTINEL
    elif fault == 'endpoint-type': rule['endpoint'] = 5
    elif fault == 'path-type': rule['rule'] = 5
    elif fault == 'relative-path': rule['rule'] = 'z'
    elif fault == 'methods-not-list': rule['methods'] = 'GET'
    elif fault == 'method-type': rule['methods'] = [True]
    elif fault == 'duplicate-method': rule['methods'] = ['GET', 'GET']
    elif fault == 'lowercase-method': rule['methods'] = ['get']
    elif fault == 'custom-reordered': data['custom_rules'][0]['methods'] = ['GET', 'POST']
    else: data['custom_rules'][0]['methods'] = ['POST', 'GET', 'HEAD']
    write(paths['inventory'], data); rechain(paths)
    with pytest.raises(AssertionError) as caught: validate(paths)
    assert_safe(caught)


def test_loaded_customs_and_static_are_observed_not_assumed(tmp_path):
    paths = records(tmp_path); data = inventory(); data['custom_rules'] = []
    # No static row initially; adding an actually observed static rule changes the oracle.
    write(paths['inventory'], data); rechain(paths); oracle = validate(paths)
    assert len(oracle.rows) == 6 and all(row[0] != 'static' for row in oracle.rows)
    data['ordinary_rules'].append({'endpoint': 'static', 'rule': '/static/<path:filename>',
                                  'methods': ['GET', 'HEAD', 'OPTIONS']})
    write(paths['inventory'], data); rechain(paths); oracle = validate(paths)
    assert len(oracle.rows) == 7 and any(row[0] == 'static' for row in oracle.rows)


def test_oracle_is_frozen_before_response_not_mutable_file(tmp_path):
    paths = records(tmp_path); oracle = validate(paths)
    data = inventory(); data['ordinary_rules'].clear(); write(paths['inventory'], data)
    RootIndexSession(StubClient(Response(200, body())), oracle).read()
    # A future runner must detect changed served inventory; the session never reloads
    # expected values from either the file or the response to make a failure pass.
    assert len(oracle.rows) == 7


@pytest.mark.parametrize('target,window,pid', [(None, WINDOW, PID), (REFERENCE_TARGET, None, PID),
    (REFERENCE_TARGET, 'other', PID), (REFERENCE_TARGET, WINDOW, None), (REFERENCE_TARGET, WINDOW, '1'),
    (REFERENCE_TARGET, WINDOW, '2147483648'), (REFERENCE_TARGET, WINDOW, '１２３４５'),
    (REFERENCE_TARGET, 'alrust-root-index-reference-２０２６０９３０-01', PID)])
def test_missing_or_invalid_expected_identity(tmp_path, target, window, pid):
    with pytest.raises(AssertionError): validate(records(tmp_path), target, window, pid)


def fixture_module(tmp_path, monkeypatch):
    paths = records(tmp_path)
    p = Path(__file__).parents[1] / 'http/test_root_index.py'
    spec = importlib.util.spec_from_file_location('root_index_http_candidate', p)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    for key, value in {'ALRUST_TARGET': REFERENCE_TARGET, 'ALRUST_CONFIG_ATTESTATION_FILE': str(paths['config']),
        'ALRUST_ROOT_INDEX_WINDOW_ID': WINDOW, 'ALRUST_ROOT_INDEX_SERVER_PID': PID,
        'ALRUST_ROOT_INDEX_ATTESTATION_FILE': str(paths['connection']),
        'ALRUST_ROOT_INDEX_PROVENANCE_FILE': str(paths['provenance']),
        'ALRUST_ROOT_INDEX_READINESS_FILE': str(paths['ready']),
        'ALRUST_SERVED_CONFIG_ATTESTATION_FILE': str(paths['config']),
        'ALRUST_ROOT_INDEX_INVENTORY_FILE': str(paths['inventory'])}.items(): monkeypatch.setenv(key, value)
    monkeypatch.setattr(module, 'load_credentials', lambda *a: type(
        'SyntheticCredentials', (), {'keys': {'unscoped_admin': 'SYNTHETIC-placeholder'}})())
    return module, paths


@pytest.mark.parametrize('fault', ['connection', 'provenance', 'ready', 'config', 'inventory'])
def test_bad_prerequisite_constructs_no_client(tmp_path, monkeypatch, fault):
    module, paths = fixture_module(tmp_path, monkeypatch); paths[fault].write_text(SENTINEL)
    constructed = []; monkeypatch.setattr(module, 'Client', lambda *a: constructed.append(a))
    with pytest.raises(AssertionError) as caught: next(module.root_index_session.__wrapped__())
    assert not constructed; assert_safe(caught)


def test_bad_credentials_construct_no_client(tmp_path, monkeypatch):
    module, _ = fixture_module(tmp_path, monkeypatch)
    def bad(*args): raise AssertionError('synthetic credential prerequisite failed')
    monkeypatch.setattr(module, 'load_credentials', bad)
    constructed = []; monkeypatch.setattr(module, 'Client', lambda *a: constructed.append(a))
    with pytest.raises(AssertionError): next(module.root_index_session.__wrapped__())
    assert not constructed


def test_fixture_always_closes_after_comparison_failure(tmp_path, monkeypatch):
    module, _ = fixture_module(tmp_path, monkeypatch); client = StubClient(Response(200, SENTINEL))
    monkeypatch.setattr(module, 'Client', lambda *a: client)
    fixture = module.root_index_session.__wrapped__(); session = next(fixture)
    with pytest.raises(AssertionError) as caught: session.read()
    assert_safe(caught, session); fixture.close(); assert client.closed


@pytest.mark.parametrize('old_task,old_window', [
    ('ALRUST-P0-ROOT-DEBUG-READONLY-01', WINDOW),
    (TASK, 'alrust-root-debug-reference-20260930-01'),
    ('ALRUST-P0-ROOT-DEBUG-READONLY-01', 'alrust-root-debug-reference-20260930-01'),
    ('ALRUST-P0-MANAGEMENT-GTG-READONLY-01', 'alrust-management-gtg-reference-20260930-01')])
def test_old_scope_cannot_authorize_index_even_with_updated_chain(tmp_path, monkeypatch, old_task, old_window):
    module, paths = fixture_module(tmp_path, monkeypatch)
    for name in ['connection', 'provenance', 'inventory']:
        data = json.loads(paths[name].read_text()); data.update(task=old_task, window_id=old_window); write(paths[name], data)
    data = json.loads(paths['ready'].read_text()); data['window_id'] = old_window; write(paths['ready'], data)
    rechain(paths); monkeypatch.setenv('ALRUST_ROOT_INDEX_WINDOW_ID', old_window)
    constructed = []; monkeypatch.setattr(module, 'Client', lambda *a: constructed.append(a))
    with pytest.raises(AssertionError) as caught: next(module.root_index_session.__wrapped__())
    assert not constructed; assert_safe(caught)
