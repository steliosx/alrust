"""One synthetic unknown-name GET; permission verification has a usage write.

File validation is an accident guard, not proof of a running server. A separately
reviewed producer must observe current_app.logger, successfully loaded registry,
connection and readiness in the actual served process, and recheck before the
original handler and after the completed case. No production credentials or SQL.
"""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .api_key_session import require
from .config_session import EXPECTED_CONFIG, REFERENCE_HEAD, REFERENCE_TARGET
from .customer_fixtures import load_credentials

TASK = 'ALRUST-P0-WEBHOOK-UNKNOWN-01'
NAME = 'alrust-synthetic-unregistered'
REQUEST_ID = 'alrust-synthetic-unknown-01'
MAX_RECORD_BYTES = 131072
IDENTITY = {'project', 'fixture', 'task', 'reference_head', 'target', 'window_id', 'server_pid'}
CHECKS = {'backend_uri_matches', 'database_name_matches', 'schema_matches',
          'connection_destination_matches'}
RECORD_FIELDS = {
    'connection': {'backend', *CHECKS},
    'ready': {'ready'},
    'config': {'effective_config'},
    'registry': {'loaded_names'},
    'logging': {'logger_name', 'effective_level', 'info_enabled', 'app_debug',
                'handlers', 'filters'},
    'keyproof': {'before_case_only', 'role', 'key_sha256', 'customer', 'scopes',
                 'stored_key_matches', 'valid', 'unexpired', 'write_webhooks_allowed'},
    'provenance': {'after_readiness', 'same_served_connection', 'record_sha256',
                   'credentials_sha256'},
}


def _object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'unknown-webhook duplicate JSON key')
        result[key] = value
    return result


def _private_bytes(path):
    require(bool(path), 'NOT_RUN: unknown-webhook private prerequisite required')
    try:
        item = Path(path)
        require(stat.S_IMODE(item.parent.stat().st_mode) == 0o700,
                'unknown-webhook prerequisite parent must be private')
        # Refuse a final-component symlink; check the opened descriptor as well.
        fd = os.open(item, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                    and info.st_size <= MAX_RECORD_BYTES,
                    'unknown-webhook prerequisite must be private regular bounded file')
            raw = stream.read(MAX_RECORD_BYTES + 1)
        require(len(raw) <= MAX_RECORD_BYTES, 'unknown-webhook prerequisite exceeds bound')
        return raw
    except AssertionError:
        raise
    except Exception:
        raise AssertionError('unknown-webhook prerequisite unavailable') from None


def _record(path):
    raw = _private_bytes(path)
    try:
        data = json.loads(raw, object_pairs_hook=_object)
    except Exception:
        raise AssertionError('unknown-webhook prerequisite malformed') from None
    require(type(data) is dict, 'unknown-webhook prerequisite object required')
    return raw, data


@dataclass(frozen=True, repr=False)
class UnknownPrerequisites:
    # Original bytes stay private. Rereading these is not a live stored-key query.
    frozen: tuple = field(repr=False)
    api_key: str = field(repr=False)

    def check_frozen(self):
        for path, raw in self.frozen:
            require(_private_bytes(path) == raw,
                    'unknown-webhook frozen prerequisite changed')


def load_unknown_prerequisites(paths, credentials_path, target, window, server_pid):
    require(target == REFERENCE_TARGET, 'NOT_RUN: exact isolated unknown-webhook target required')
    require(type(window) is str and re.fullmatch(
        r'alrust-webhook-unknown-reference-[0-9]{8}-[0-9]{2}', window) is not None,
        'NOT_RUN: fresh unknown-webhook window required')
    require(type(server_pid) is str and server_pid.isascii() and server_pid.isdecimal()
            and 1 < int(server_pid) < 2**31, 'NOT_RUN: owned unknown-webhook PID required')
    require(type(paths) is dict and set(paths) == set(RECORD_FIELDS),
            'unknown-webhook prerequisite paths mismatch')
    expected = {'project': 'alrust', 'fixture': 'SYNTHETIC', 'task': TASK,
                'reference_head': REFERENCE_HEAD, 'target': target,
                'window_id': window, 'server_pid': int(server_pid)}
    raw, records = {}, {}
    for name, fields in RECORD_FIELDS.items():
        raw[name], data = _record(paths[name])
        require(set(data) == IDENTITY | fields, 'unknown-webhook prerequisite shape mismatch')
        require(all(type(data[k]) is type(v) and data[k] == v for k, v in expected.items()),
                'unknown-webhook prerequisite source or instance mismatch')
        records[name] = data
    c = records['connection']
    require(c['backend'] == 'postgresql' and all(c[k] is True for k in CHECKS),
            'NOT_RUN: actual served PostgreSQL identity required')
    require(records['ready']['ready'] is True, 'unknown-webhook readiness mismatch')
    config = records['config']['effective_config']
    wanted = {**EXPECTED_CONFIG, 'LOG_CONFIG_FILE': '', 'LOG_LEVEL': 'ERROR',
              'LOG_HANDLERS': ['console']}
    require(type(config) is dict and set(config) == set(wanted)
            and all(type(config[k]) is type(v) and config[k] == v for k, v in wanted.items()),
            'unknown-webhook served config mismatch')
    names = records['registry']['loaded_names']
    require(type(names) is list and all(type(n) is str and re.fullmatch(r'[A-Za-z0-9_-]+', n)
            for n in names) and len(names) == len(set(names)) and NAME not in names,
            'unknown-webhook loaded registry absence mismatch')
    log = records['logging']
    require(type(log['logger_name']) is str and log['logger_name'] == 'alerta.app'
            and type(log['effective_level']) is int and log['effective_level'] == 40
            and log['info_enabled'] is False and log['app_debug'] is False,
            'unknown-webhook actual served logger must disable INFO')
    # The producer reports the actual applicable logger/ancestor handler chain.
    handlers = log['handlers']
    require(type(handlers) is list and len(handlers) == 1 and type(handlers[0]) is dict
            and handlers[0] == {'class': 'logging.StreamHandler', 'level': 40,
                                'filters': ['ContextFilter', 'RequestFilter']}
            and type(handlers[0]['level']) is int and log['filters'] == [],
            'unknown-webhook served handler or filter identity mismatch')
    credentials_raw, credential_data = _record(credentials_path)
    require(set(credential_data) == {'fixture', 'target', 'auth_required',
            'customer_views', 'customers', 'keys'},
            'unknown-webhook credential record shape mismatch')
    try:
        credentials = load_credentials(credentials_path, target)
    except Exception:
        raise AssertionError('unknown-webhook synthetic credentials invalid') from None
    require(_private_bytes(credentials_path) == credentials_raw,
            'unknown-webhook credentials changed during validation')
    key = credentials.keys['unscoped_admin']
    proof = records['keyproof']
    require(proof['before_case_only'] is True and proof['role'] == 'unscoped_admin'
            and proof['key_sha256'] == hashlib.sha256(key.encode()).hexdigest()
            and proof['customer'] is None and proof['scopes'] == ['admin']
            and all(proof[k] is True for k in ['stored_key_matches', 'valid', 'unexpired',
                                              'write_webhooks_allowed']),
            'unknown-webhook pre-case stored permission prerequisite mismatch')
    provenance = records['provenance']
    hashes = provenance['record_sha256']
    require(provenance['after_readiness'] is True and provenance['same_served_connection'] is True
            and type(hashes) is dict and set(hashes) == set(RECORD_FIELDS) - {'provenance'}
            and all(type(hashes[k]) is str and hashes[k] == hashlib.sha256(raw[k]).hexdigest()
                    for k in hashes)
            and provenance['credentials_sha256'] == hashlib.sha256(credentials_raw).hexdigest(),
            'unknown-webhook whole-byte prerequisite chain mismatch')
    prerequisite = UnknownPrerequisites(tuple((paths[k], raw[k]) for k in raw)
                                       + ((credentials_path, credentials_raw),), key)
    prerequisite.check_frozen()
    return prerequisite


class UnknownWebhookSession:
    def __init__(self, client, prerequisites):
        self.client, self.prerequisites = client, prerequisites
        self.attempted = self.completed = False

    def read(self):
        require(not self.attempted, 'unknown-webhook case already attempted')
        self.attempted = True
        self.prerequisites.check_frozen()
        try:
            response = self.client.request('GET', '/webhooks/' + NAME)
        except Exception:
            raise AssertionError('unknown-webhook HTTP request or JSON parse failed') from None
        require(type(response.status) is int and response.status == 404,
                'unknown-webhook HTTP status mismatch')
        body = response.body
        expected = {'status': 'error', 'message': "Custom webhook '" + NAME + "' not found.",
                    'code': 404, 'errors': None, 'requestId': REQUEST_ID}
        require(type(body) is dict and set(body) == set(expected)
                and type(body['code']) is int and body == expected,
                'unknown-webhook exact error envelope mismatch')
        # Preserve the completed outcome even if later prerequisites/metadata fail.
        self.completed = True
        self.prerequisites.check_frozen()

    def safe_summary(self):
        return {'project': 'alrust', 'case': 'webhook-unknown-01',
                'attempted': self.attempted, 'completed': self.completed,
                'captured_response_records': 0,
                'authentication_usage': 'SOURCE_EXPECTED_ONE_UPDATE_NOT_COUNTED'}

    def close(self):
        try:
            self.client.close()
        except Exception:
            raise AssertionError('unknown-webhook client close failed') from None
