from copy import deepcopy

import pytest

from alrust_conformance.client import Response
from alrust_conformance.security_assertions import AuthRefusal, check_auth_refusal
from alrust_conformance.security_evidence import CaseEvidence


def refusal(expected=AuthRefusal.MISSING_AUTHORIZATION):
    code, message = expected.value
    return Response(code, {'status': 'error', 'message': message, 'code': code,
                           'errors': None, 'requestId': 'synthetic-trace'})


@pytest.mark.parametrize('expected', list(AuthRefusal))
def test_exact_refusals(expected):
    assert check_auth_refusal(refusal(expected), expected) == ()


@pytest.mark.parametrize('status', [200, 400, 403, 404, 500, 401.0, '401', True, None])
def test_status_is_not_inferred_from_envelope(status):
    assert 'http_status' in check_auth_refusal(
        Response(status, refusal().body), AuthRefusal.MISSING_AUTHORIZATION)


@pytest.mark.parametrize('body', [None, [], 'unauthorized', 401, True])
def test_error_status_without_error_envelope_fails(body):
    assert check_auth_refusal(Response(401, body), AuthRefusal.MISSING_AUTHORIZATION) == ('body_type',)


@pytest.mark.parametrize('field', ['status', 'message', 'code', 'errors', 'requestId'])
def test_every_error_field_is_required(field):
    body = refusal().body
    del body[field]
    assert 'envelope_fields' in check_auth_refusal(Response(401, body), AuthRefusal.MISSING_AUTHORIZATION)


@pytest.mark.parametrize('field,value,label', [
    ('status', 'ok', 'envelope_status'),
    ('status', True, 'envelope_status'),
    ('message', 'Missing authorization API Key or Bearer Token ', 'message'),
    ('message', None, 'message'),
    ('code', 403, 'envelope_code'),
    ('code', 401.0, 'envelope_code'),
    ('code', '401', 'envelope_code'),
    ('errors', [], 'errors'),
    ('errors', ['diagnostic'], 'errors'),
    ('requestId', None, 'request_id'),
    ('requestId', '', 'request_id'),
    ('requestId', 4, 'request_id'),
])
def test_mutated_envelope_does_not_pass(field, value, label):
    body = refusal().body
    body[field] = value
    assert label in check_auth_refusal(Response(401, body), AuthRefusal.MISSING_AUTHORIZATION)


def test_distinct_trace_ids_are_valid_without_projecting_other_fields():
    left, right = refusal(), refusal()
    right.body['requestId'] = 'other-synthetic-trace'
    assert check_auth_refusal(left, AuthRefusal.MISSING_AUTHORIZATION) == ()
    assert check_auth_refusal(right, AuthRefusal.MISSING_AUTHORIZATION) == ()
    right.body['unexpected'] = True
    assert check_auth_refusal(right, AuthRefusal.MISSING_AUTHORIZATION) == ('envelope_fields',)


def test_two_equal_wrong_refusals_are_not_parity_success():
    wrong = refusal(AuthRefusal.MISSING_WRITE_ALERTS)
    assert check_auth_refusal(wrong, AuthRefusal.MISSING_AUTHORIZATION) == (
        'http_status', 'message', 'envelope_code')


def test_failure_labels_do_not_export_untrusted_data_and_input_is_unchanged():
    secret = 'SYNTHETIC-credential-must-not-appear'
    body = {'status': secret, 'message': secret, 'code': secret,
            'errors': [secret], 'requestId': [secret], secret: secret}
    original = deepcopy(body)
    failures = check_auth_refusal(Response(401, body), AuthRefusal.MISSING_AUTHORIZATION)
    assert failures
    assert secret not in repr(failures)
    assert body == original


@pytest.mark.parametrize('expected', [None, 401, 'MISSING_AUTHORIZATION', (401, 'error')])
def test_missing_or_self_reported_expectation_is_not_allowed(expected):
    with pytest.raises(ValueError, match='expected refusal must be an AuthRefusal'):
        check_auth_refusal(refusal(), expected)


def test_missing_response_cannot_pass():
    assert check_auth_refusal(None, AuthRefusal.MISSING_AUTHORIZATION) == ('response_type',)


def test_response_only_does_not_complete_case():
    case = CaseEvidence('AN1-anonymous-write', reference_qualified=True)
    case.start('candidate')
    assert check_auth_refusal(refusal(), AuthRefusal.MISSING_AUTHORIZATION) == ()
    case.record('candidate', 'response', 'PASS')
    result = case.snapshot()
    assert result['sides']['candidate']['status'] == 'INCOMPLETE'
    assert result['sides']['reference']['status'] == 'NOT_RUN'
    assert result['differential'] == 'INCOMPLETE'
