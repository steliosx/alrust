"""Offline response predicates for the approved SF-AN1/SF-CRON refusals.

These functions do not select a case, authorize a request, or prove absence of
side effects. An empty failure tuple satisfies only the response stage; state,
audit and cleanup assertions remain mandatory in CaseEvidence.
"""
from enum import Enum

from .client import Response


class AuthRefusal(Enum):
    MISSING_AUTHORIZATION = (
        401, 'Missing authorization API Key or Bearer Token',
    )
    MISSING_WRITE_ALERTS = (403, 'Missing required scope: write:alerts')


_ERROR_FIELDS = frozenset({'status', 'message', 'code', 'errors', 'requestId'})


def check_auth_refusal(response: Response, expected: AuthRefusal) -> tuple[str, ...]:
    """Check one known refusal independently of the other server's outcome.

    The caller chooses expected from the reviewed case and attested profile,
    never from the received status. Dispatch/success cases and unqualified
    reference behavior must not be fed into this checker as refusals.

    Failure labels contain no observed body, message or credential. Compare raw
    synthetic responses before export redaction. requestId is required and must
    be a nonempty string; its value varies per request and is not compared across
    servers. This does not check content type or trace-header correlation, which
    the current Response representation does not retain.
    """
    if type(expected) is not AuthRefusal:
        raise ValueError('expected refusal must be an AuthRefusal')
    if type(response) is not Response:
        return ('response_type',)
    code, message = expected.value
    failures = []
    if type(response.status) is not int or response.status != code:
        failures.append('http_status')
    body = response.body
    if type(body) is not dict:
        return tuple(failures + ['body_type'])
    if body.keys() != _ERROR_FIELDS:
        failures.append('envelope_fields')
    if type(body.get('status')) is not str or body.get('status') != 'error':
        failures.append('envelope_status')
    if type(body.get('message')) is not str or body.get('message') != message:
        failures.append('message')
    if type(body.get('code')) is not int or body.get('code') != code:
        failures.append('envelope_code')
    # Missing errors is separately rejected by the exact field check.
    if body.get('errors') is not None:
        failures.append('errors')
    if type(body.get('requestId')) is not str or not body.get('requestId'):
        failures.append('request_id')
    return tuple(failures)
