"""One source-bound reference config read; originals stay in memory."""
import json
from pathlib import Path
import stat

from .api_key_session import require


REFERENCE_HEAD = 'f4312727783dad64198b62ed778bbca98a5144a3'
REFERENCE_TARGET = 'http://127.0.0.1:18081'
TOP_KEYS = frozenset('''debug endpoint alarm_model auth_required allow_readonly
readonly_scopes provider customer_views signup_enabled email_verification
client_id azure_tenant aws_region cognito_domain github_url gitlab_url
keycloak_url keycloak_realm oidc_auth_url site_logo_url severity colors timeouts
blackouts dates font audio columns sort_by filter indicators actions tracking_id
refresh_interval environments clipboard_template'''.split())
NESTED_KEYS = {
    'alarm_model': {'name', 'severity', 'colors', 'status', 'defaults'},
    'timeouts': {'alert', 'heartbeat', 'ack', 'shelve'},
    'blackouts': {'duration'},
    'dates': {'shortTime', 'mediumDate', 'longDate'},
    'audio': {'new'},
    'indicators': {'severity', 'queries'},
}
EXPECTED_CONFIG = {
    'BASE_URL': '', 'AUTH_REQUIRED': False, 'CUSTOMER_VIEWS': False,
    'DEBUG': False, 'ALARM_MODEL': 'ALERTA', 'ALERT_TIMEOUT': 86400,
    'ACK_TIMEOUT': 0,
}


def load_config_attestation(path, target):
    """Validate the runner's factory-config record before the first HTTP call.

    This file alone does not prove a running server's configuration. The reviewed
    runner must produce it from actual effective configuration at the pinned
    source, bind it to its owned instance, and attest existing fixture records.
    No configuration/environment values or exception text are exported here.
    """
    require(target == REFERENCE_TARGET, 'NOT_RUN: exact isolated config target required')
    require(bool(path), 'NOT_RUN: effective config attestation required')
    try:
        item = Path(path)
        info = item.lstat()
        require(stat.S_ISREG(info.st_mode) and not info.st_mode & 0o077
                and info.st_size <= 16384,
                'config attestation must be a private regular bounded file')
        data = json.loads(item.read_text())
    except AssertionError:
        raise
    except Exception:
        raise AssertionError('config attestation is unavailable or malformed') from None
    require(type(data) is dict and set(data) == {
        'project', 'fixture', 'reference_head', 'target', 'effective_config'},
        'config attestation shape mismatch')
    require(data['project'] == 'alrust' and data['fixture'] == 'SYNTHETIC'
            and data['reference_head'] == REFERENCE_HEAD and data['target'] == target,
            'config attestation source or target mismatch')
    actual = data['effective_config']
    require(type(actual) is dict and set(actual) == set(EXPECTED_CONFIG),
            'effective config prerequisite shape mismatch')
    require(all(type(actual[key]) is type(value) and actual[key] == value
                for key, value in EXPECTED_CONFIG.items()),
            'effective config prerequisites mismatch')


class ConfigSession:
    """No created objects, response export, normalization or authorization probe."""

    def __init__(self, client):
        self.client = client
        self.attempted = False
        self.completed = False

    def read(self):
        require(not self.attempted, 'config read already attempted')
        self.attempted = True
        try:
            response = self.client.request('GET', '/config')
        except Exception:
            raise AssertionError('config HTTP request or JSON parse failed') from None
        require(response.status == 200, 'config HTTP status mismatch')
        body = response.body
        require(type(body) is dict and set(body) == TOP_KEYS,
                'config top-level object or keys mismatch')
        require(body['endpoint'] == self.client.base_url.rstrip('/'),
                'config endpoint mismatch')
        require(all(body[key] is False for key in ('debug', 'auth_required', 'customer_views')),
                'config controlled flags mismatch')
        for key, keys in NESTED_KEYS.items():
            require(type(body[key]) is dict and set(body[key]) == keys,
                    'config nested keyset mismatch')
        model = body['alarm_model']
        require(type(model['defaults']) is dict and set(model['defaults']) == {
            'status', 'normal_severity', 'previous_severity'},
            'config model defaults keyset mismatch')
        require(model['name'] == 'Alerta 9.1.0', 'config model display name mismatch')
        require(body['severity'] == model['severity'] and body['colors'] == model['colors'],
                'config model aliases mismatch')
        require(type(body['timeouts']['alert']) is int and body['timeouts']['alert'] == 86400
                and type(body['timeouts']['ack']) is int and body['timeouts']['ack'] == 0,
                'config controlled timeouts mismatch')
        self.completed = True

    def safe_summary(self):
        # Fixed metadata only, including on a partial/failed comparison.
        return {'project': 'alrust', 'case': 'config-readonly-01',
                'completed': self.completed, 'captured_response_records': 0}

    def close(self):
        try:
            self.client.close()
        except Exception:
            raise AssertionError('config client close failed') from None
