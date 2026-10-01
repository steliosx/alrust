from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import traceback

import httpx
import pytest

from alrust_conformance.api_key_session import shared_evidence_safe
from alrust_conformance.client import Client, Response
from alrust_conformance.config_session import EXPECTED_CONFIG, REFERENCE_HEAD, REFERENCE_TARGET
from alrust_conformance.webhook_unknown_session import (
    CHECKS, MAX_RECORD_BYTES, NAME, RECORD_FIELDS, REQUEST_ID, TASK,
    UnknownWebhookSession, load_unknown_prerequisites)

WINDOW = 'alrust-webhook-unknown-reference-20260930-01'
PID = '12345'
SENTINEL = 'SYNTHETIC-PRIVATE-ERROR-SENTINEL'


def write(path, data):
    path.write_text(json.dumps(data)); path.chmod(0o600)


def identity():
    return dict(project='alrust', fixture='SYNTHETIC', task=TASK,
                reference_head=REFERENCE_HEAD, target=REFERENCE_TARGET,
                window_id=WINDOW, server_pid=int(PID))


def expected_body():
    # Literal response expectation, independent of production comparator.
    return {'status': 'error', 'message': "Custom webhook 'alrust-synthetic-unregistered' not found.",
            'code': 404, 'errors': None, 'requestId': 'alrust-synthetic-unknown-01'}


def rechain(paths, credentials):
    data = json.loads(paths['provenance'].read_text())
    data['record_sha256'] = {k: hashlib.sha256(v.read_bytes()).hexdigest()
                             for k, v in paths.items() if k != 'provenance'}
    data['credentials_sha256'] = hashlib.sha256(credentials.read_bytes()).hexdigest()
    write(paths['provenance'], data)


def records(directory):
    directory.chmod(0o700)
    credentials = directory / 'alrust-synthetic-keys.json'
    ca, cb = 'alrust-synthetic-customer-a', 'alrust-synthetic-customer-b'
    roles = [('unscoped_admin', None, ['admin']), ('a_admin', ca, ['admin']),
             ('a_writer', ca, ['write:alerts']), ('b_writer', cb, ['write:alerts'])]
    rows = {role: dict(key='SYNTHETIC-' + str(i) * 48, customer=customer, scopes=scopes)
            for i, (role, customer, scopes) in enumerate(roles, 1)}
    write(credentials, dict(fixture='SYNTHETIC', target=REFERENCE_TARGET,
        auth_required=False, customer_views=False, customers={'a': ca, 'b': cb}, keys=rows))
    paths = {k: directory / (k + '.json') for k in RECORD_FIELDS}
    payloads = {
        'connection': {'backend': 'postgresql', **{k: True for k in CHECKS}},
        'ready': {'ready': True},
        'config': {'effective_config': {**EXPECTED_CONFIG, 'LOG_CONFIG_FILE': '',
                                       'LOG_LEVEL': 'ERROR', 'LOG_HANDLERS': ['console']}},
        'registry': {'loaded_names': ['synthetic_loaded']},
        'logging': {'logger_name': 'alerta.app', 'effective_level': 40, 'info_enabled': False,
                    'app_debug': False, 'filters': [], 'handlers': [
                        {'class': 'logging.StreamHandler', 'level': 40,
                         'filters': ['ContextFilter', 'RequestFilter']}]},
        'keyproof': {'before_case_only': True, 'role': 'unscoped_admin',
                     'key_sha256': hashlib.sha256(rows['unscoped_admin']['key'].encode()).hexdigest(),
                     'customer': None, 'scopes': ['admin'], 'stored_key_matches': True,
                     'valid': True, 'unexpired': True, 'write_webhooks_allowed': True},
        'provenance': {'after_readiness': True, 'same_served_connection': True,
                       'record_sha256': {}, 'credentials_sha256': ''},
    }
    for k, data in payloads.items(): write(paths[k], {**identity(), **data})
    rechain(paths, credentials)
    return paths, credentials


def validate(paths, credentials, target=REFERENCE_TARGET, window=WINDOW, pid=PID):
    return load_unknown_prerequisites(paths, credentials, target, window, pid)


class StubClient:
    def __init__(self, response):
        self.response, self.calls, self.closed = response, [], False
    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if isinstance(self.response, Exception): raise self.response
        return self.response
    def close(self): self.closed = True


def private_error(caught, session=None):
    value = ''.join(traceback.format_exception(caught.type, caught.value, caught.tb))
    if session: value += json.dumps(session.safe_summary())
    shared_evidence_safe(value, (SENTINEL,))


def test_fixed_header_preserves_authorization_and_empty_get_body():
    seen = []
    def handler(request):
        seen.append(request); return httpx.Response(404, json=expected_body())
    client = Client(REFERENCE_TARGET, SENTINEL, request_id=REQUEST_ID,
                    transport=httpx.MockTransport(handler))
    try:
        assert client.request('GET', '/webhooks/' + NAME) == Response(404, expected_body())
        assert len(seen) == 1 and seen[0].headers['Authorization'] == 'Key ' + SENTINEL
        assert seen[0].headers['X-Request-ID'] == REQUEST_ID
        assert seen[0].url.query == b'' and seen[0].content == b''
    finally: client.close()


@pytest.mark.parametrize('request_id', [True, 1, b'abc', '', 'other', REQUEST_ID + '\n',
    REQUEST_ID + '\r\nAuthorization: Key forged', 'ａｌｒｕｓｔ', 'a' * 10000])
def test_trace_option_rejects_all_other_values(request_id):
    with pytest.raises(ValueError): Client(REFERENCE_TARGET, request_id=request_id)


def test_default_client_has_no_added_trace_header():
    seen = []
    c = Client(REFERENCE_TARGET, transport=httpx.MockTransport(
        lambda r: (seen.append(r), httpx.Response(200, json={}))[1]))
    try: c.request('GET', '/'); assert 'X-Request-ID' not in seen[0].headers
    finally: c.close()


def test_one_attempt_exact_error_no_capture_or_normalization(tmp_path, capsys):
    p = validate(*records(tmp_path)); c = StubClient(Response(404, expected_body()))
    s = UnknownWebhookSession(c, p); s.read(); s.close()
    assert c.calls == [('GET', '/webhooks/alrust-synthetic-unregistered', None)] and c.closed
    assert s.completed and s.attempted and s.safe_summary()['captured_response_records'] == 0
    assert 'SOURCE_EXPECTED_ONE_UPDATE_NOT_COUNTED' in json.dumps(s.safe_summary())
    assert capsys.readouterr() == ('', '')
    with pytest.raises(AssertionError, match='already attempted'): s.read()


@pytest.mark.parametrize('field,value', [('status', 'ok'), ('message', SENTINEL), ('code', 404.0),
    ('code', '404'), ('errors', []), ('requestId', 'server-generated'), ('requestId', None)])
def test_exact_fields_never_normalized(tmp_path, field, value):
    body = expected_body(); body[field] = value
    c = StubClient(Response(404, body)); s = UnknownWebhookSession(c, validate(*records(tmp_path)))
    with pytest.raises(AssertionError) as caught: s.read()
    private_error(caught, s); assert not s.completed and len(c.calls) == 1
    with pytest.raises(AssertionError): s.read()
    assert len(c.calls) == 1


@pytest.mark.parametrize('fault', ['extra', 'missing', 'array', 'string', 'null', 'status-float',
    'status-string', 'status-bool', 'status200', 'exception'])
def test_invalid_response_shape_or_transport_stays_private(tmp_path, fault):
    body = expected_body(); status = 404
    if fault == 'extra': body['private'] = SENTINEL
    elif fault == 'missing': body.pop('requestId')
    elif fault == 'array': body = [SENTINEL]
    elif fault == 'string': body = SENTINEL
    elif fault == 'null': body = None
    elif fault == 'status-float': status = 404.0
    elif fault == 'status-string': status = '404'
    elif fault == 'status-bool': status = True
    elif fault == 'status200': status = 200
    c = StubClient(ValueError(SENTINEL) if fault == 'exception' else Response(status, body))
    s = UnknownWebhookSession(c, validate(*records(tmp_path)))
    with pytest.raises(AssertionError) as caught: s.read()
    private_error(caught, s); assert not s.completed and len(c.calls) == 1


@pytest.mark.parametrize('record', list(RECORD_FIELDS) + ['credentials'])
@pytest.mark.parametrize('fault', ['missing', 'public', 'symlink', 'malformed', 'oversize',
                                   'duplicate', 'extra', 'missing-field'])
def test_private_record_bypasses_fail(tmp_path, record, fault):
    paths, cred = records(tmp_path); p = cred if record == 'credentials' else paths[record]
    if fault == 'missing': p.unlink()
    elif fault == 'public': p.chmod(0o644)
    elif fault == 'symlink':
        link = tmp_path / 'link.json'; link.symlink_to(p)
        if record == 'credentials': cred = link
        else: paths[record] = link
    elif fault == 'malformed': p.write_text(SENTINEL)
    elif fault == 'oversize': p.write_bytes(b' ' * (MAX_RECORD_BYTES + 1))
    elif fault == 'duplicate': p.write_text('{"fixture":"SYNTHETIC","fixture":"SYNTHETIC"}')
    else:
        data = json.loads(p.read_text())
        if fault == 'extra': data['unrecognized'] = SENTINEL
        else: data.pop('fixture')
        write(p, data)
    with pytest.raises(AssertionError) as caught: validate(paths, cred)
    private_error(caught)


@pytest.mark.parametrize('record', list(RECORD_FIELDS))
@pytest.mark.parametrize('field,value', [('project', 'other'), ('fixture', 'PRODUCTION'),
    ('task', 'ALRUST-P0-ROOT-INDEX-READONLY-01'), ('reference_head', 'a' * 40),
    ('target', 'http://localhost:18081'), ('window_id', 'alrust-root-index-reference-20260930-01'),
    ('server_pid', True), ('server_pid', 12345.0), ('server_pid', 9999)])
def test_instance_mismatch_even_rehashed(tmp_path, record, field, value):
    paths, cred = records(tmp_path); data = json.loads(paths[record].read_text())
    data[field] = value; write(paths[record], data); rechain(paths, cred)
    with pytest.raises(AssertionError) as caught: validate(paths, cred)
    private_error(caught)


@pytest.mark.parametrize('field,value', [('LOG_CONFIG_FILE', '/missing/override.yml'),
    ('DEBUG', True), ('DEBUG', 0), ('LOG_LEVEL', 'INFO'), ('LOG_HANDLERS', ['file']),
    ('LOG_HANDLERS', ['console', 'wsgi']), ('AUTH_REQUIRED', True)])
def test_logging_config_fails_before_case(tmp_path, field, value):
    paths, cred = records(tmp_path); data = json.loads(paths['config'].read_text())
    data['effective_config'][field] = value; write(paths['config'], data); rechain(paths, cred)
    with pytest.raises(AssertionError): validate(paths, cred)


@pytest.mark.parametrize('field,value', [('logger_name', 'constructed.test.logger'),
    ('logger_name', 'alertabad'), ('effective_level', 20), ('effective_level', '40'),
    ('info_enabled', True), ('info_enabled', 0), ('app_debug', True), ('filters', [SENTINEL]),
    ('handlers', []), ('handlers', [{'class': 'logging.StreamHandler', 'level': 20,
                                  'filters': ['ContextFilter', 'RequestFilter']}])])
def test_actual_logger_chain_required(tmp_path, field, value):
    paths, cred = records(tmp_path); data = json.loads(paths['logging'].read_text())
    data[field] = value; write(paths['logging'], data); rechain(paths, cred)
    with pytest.raises(AssertionError) as caught: validate(paths, cred)
    private_error(caught)


@pytest.mark.parametrize('names', [[NAME], ['a', 'a'], 'metadata-entrypoints', [True], [SENTINEL + '/']])
def test_loaded_registry_absence_not_metadata_or_response(tmp_path, names):
    paths, cred = records(tmp_path); data = json.loads(paths['registry'].read_text())
    data['loaded_names'] = names; write(paths['registry'], data); rechain(paths, cred)
    with pytest.raises(AssertionError): validate(paths, cred)


@pytest.mark.parametrize('field,value', [('before_case_only', False), ('valid', False),
    ('unexpired', False), ('stored_key_matches', False), ('write_webhooks_allowed', False),
    ('scopes', ['read:alerts']), ('role', 'a_admin'), ('customer', 'alrust-synthetic-customer-a'),
    ('key_sha256', 'b' * 64), ('count', 0), ('last_used_time', 'unchanged')])
def test_pre_case_permission_only_no_usage_count_or_post_key_proof(tmp_path, field, value):
    paths, cred = records(tmp_path); data = json.loads(paths['keyproof'].read_text())
    data[field] = value; write(paths['keyproof'], data); rechain(paths, cred)
    with pytest.raises(AssertionError): validate(paths, cred)


@pytest.mark.parametrize('record', list(RECORD_FIELDS) + ['credentials'])
def test_whole_bytes_changed_after_freeze_preserves_partial_result(tmp_path, record):
    paths, cred = records(tmp_path); p = validate(paths, cred)
    file = cred if record == 'credentials' else paths[record]
    c = StubClient(Response(404, expected_body())); original = c.request
    def request(*args):
        response = original(*args); file.write_bytes(file.read_bytes() + b'\n'); return response
    c.request = request; s = UnknownWebhookSession(c, p)
    with pytest.raises(AssertionError, match='frozen prerequisite changed'): s.read()
    assert s.completed and s.safe_summary()['completed'] and len(c.calls) == 1
    with pytest.raises(AssertionError): s.read()


@pytest.mark.parametrize('record', list(RECORD_FIELDS) + ['credentials'])
def test_changed_frozen_bytes_before_request_no_http(tmp_path, record):
    paths, cred = records(tmp_path); p = validate(paths, cred)
    f = cred if record == 'credentials' else paths[record]; f.write_bytes(f.read_bytes() + b'\n')
    c = StubClient(Response(404, expected_body())); s = UnknownWebhookSession(c, p)
    with pytest.raises(AssertionError): s.read()
    assert c.calls == [] and s.attempted and not s.completed


@pytest.mark.parametrize('name', ['connection', 'ready', 'config', 'registry', 'logging', 'keyproof'])
def test_hash_chain_covers_whitespace(tmp_path, name):
    paths, cred = records(tmp_path); paths[name].write_bytes(paths[name].read_bytes() + b'\n')
    with pytest.raises(AssertionError, match='whole-byte'): validate(paths, cred)


@pytest.mark.parametrize('target,window,pid', [(None, WINDOW, PID), (REFERENCE_TARGET, None, PID),
    (REFERENCE_TARGET, 'alrust-root-index-reference-20260930-01', PID),
    (REFERENCE_TARGET, WINDOW, None), (REFERENCE_TARGET, WINDOW, '1'),
    (REFERENCE_TARGET, WINDOW, '１２３４５'), (REFERENCE_TARGET, WINDOW, '2147483648')])
def test_missing_or_old_window_not_usable(tmp_path, target, window, pid):
    with pytest.raises(AssertionError): validate(*records(tmp_path), target, window, pid)


def http_module(paths, cred, monkeypatch):
    file = Path(__file__).parents[1] / 'http/test_webhook_unknown.py'
    spec = importlib.util.spec_from_file_location('unknown_http_candidate', file)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    values = {'ALRUST_TARGET': REFERENCE_TARGET, 'ALRUST_SYNTHETIC_KEYS_FILE': str(cred),
        'ALRUST_WEBHOOK_UNKNOWN_WINDOW_ID': WINDOW, 'ALRUST_WEBHOOK_UNKNOWN_SERVER_PID': PID,
        **{'ALRUST_WEBHOOK_UNKNOWN_' + k.upper() + '_FILE': str(v) for k, v in paths.items()}}
    for k, v in values.items(): monkeypatch.setenv(k, v)
    return module


@pytest.mark.parametrize('name', list(RECORD_FIELDS))
def test_failed_prerequisite_never_constructs_client(tmp_path, monkeypatch, name):
    paths, cred = records(tmp_path); m = http_module(paths, cred, monkeypatch)
    paths[name].write_text(SENTINEL); seen = []
    monkeypatch.setattr(m, 'Client', lambda *args, **kwargs: seen.append((args, kwargs)))
    with pytest.raises(AssertionError) as caught: next(m.webhook_unknown_session.__wrapped__())
    private_error(caught); assert seen == []


def test_fixture_closes_failed_case_and_passes_only_fixed_trace(tmp_path, monkeypatch):
    paths, cred = records(tmp_path); m = http_module(paths, cred, monkeypatch)
    c = StubClient(Response(404, SENTINEL)); options = []
    def create(*args, **kwargs): options.append(kwargs); return c
    monkeypatch.setattr(m, 'Client', create)
    fixture = m.webhook_unknown_session.__wrapped__(); s = next(fixture)
    with pytest.raises(AssertionError) as caught: s.read()
    private_error(caught, s); fixture.close(); assert c.closed
    assert options == [{'request_id': REQUEST_ID}]


def test_private_parent_required(tmp_path):
    paths, cred = records(tmp_path); tmp_path.chmod(0o755)
    with pytest.raises(AssertionError): validate(paths, cred)


def test_exception_during_close_never_echoes_transport_data(tmp_path):
    c = StubClient(Response(404, expected_body()))
    def fail(): raise ValueError(SENTINEL)
    c.close = fail
    with pytest.raises(AssertionError) as caught: UnknownWebhookSession(c, validate(*records(tmp_path))).close()
    private_error(caught)


@pytest.mark.parametrize('name,field,value', [('connection', 'backend', 'mongodb'),
    *[('connection', k, v) for k in sorted(CHECKS) for v in [False, 1]],
    ('ready', 'ready', 1), ('provenance', 'after_readiness', 1),
    ('provenance', 'same_served_connection', False)])
def test_connection_readiness_and_provenance_flags_strict(tmp_path, name, field, value):
    paths, cred = records(tmp_path); data = json.loads(paths[name].read_text())
    data[field] = value; write(paths[name], data); rechain(paths, cred)
    with pytest.raises(AssertionError): validate(paths, cred)


def test_duplicate_credential_member_cannot_hide_role(tmp_path):
    paths, cred = records(tmp_path); raw = cred.read_text()
    raw = raw.replace('"fixture": "SYNTHETIC"', '"fixture":"SYNTHETIC","fixture":"SYNTHETIC"')
    cred.write_text(raw); rechain(paths, cred)
    with pytest.raises(AssertionError): validate(paths, cred)


def test_unattested_credential_substitution_rejected(tmp_path):
    paths, cred = records(tmp_path); data = json.loads(cred.read_text())
    data['keys']['unscoped_admin']['key'] = 'SYNTHETIC-' + 'a' * 48
    write(cred, data); rechain(paths, cred)
    with pytest.raises(AssertionError): validate(paths, cred)
