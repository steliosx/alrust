"""One healthy GTG read; private provenance checked before HTTP construction."""
import hashlib
import json
from pathlib import Path
import re
import stat

from .api_key_session import require
from .config_session import REFERENCE_HEAD, REFERENCE_TARGET, load_config_attestation

TASK = 'ALRUST-P0-MANAGEMENT-GTG-READONLY-01'
GTG_PATH = '/management/gtg'
IDENTITY_KEYS = {'project', 'fixture', 'task', 'reference_head', 'target',
                 'window_id', 'server_pid'}
CONNECTION_CHECKS = {'backend_uri_matches', 'database_name_matches',
                     'schema_matches', 'connection_destination_matches'}


def _private_record(path):
    require(bool(path), 'NOT_RUN: GTG private prerequisite file required')
    try:
        item = Path(path)
        info = item.lstat()
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_size <= 16384, 'GTG prerequisite must be private regular bounded file')
        raw = item.read_bytes()
        data = json.loads(raw)
    except AssertionError:
        raise
    except Exception:
        raise AssertionError('GTG prerequisite unavailable or malformed') from None
    require(type(data) is dict, 'GTG prerequisite object required')
    return raw, data


def _identity(data, target, window, server_pid):
    require(data['project'] == 'alrust' and data['fixture'] == 'SYNTHETIC'
            and data['task'] == TASK and data['reference_head'] == REFERENCE_HEAD
            and data['target'] == target and data['window_id'] == window,
            'GTG prerequisite source or instance mismatch')
    require(type(data['server_pid']) is int and data['server_pid'] == int(server_pid),
            'GTG prerequisite owned PID mismatch')


def load_gtg_connection_attestation(path, target, window, server_pid,
                                    provenance_path, readiness_path, served_config_path):
    """Validate frozen private record bytes and instance/config/readiness bindings.

    The records are accident guards, not proof that their producer observed a
    connection. A separately reviewed runner must produce them on the actual
    served PostgreSQL connection after healthcheck readiness, comparing URI,
    name, schema and live destination against the owned instance. No heartbeat
    dataset/count is required or inferred, and no response body is captured.
    """
    require(target == REFERENCE_TARGET, 'NOT_RUN: exact isolated GTG target required')
    require(type(window) is str and re.fullmatch(
        r'alrust-management-gtg-reference-[0-9]{8}-[0-9]{2}', window) is not None,
        'NOT_RUN: GTG window identity required')
    require(type(server_pid) is str and server_pid.isascii() and server_pid.isdecimal()
            and 1 < int(server_pid) < 2**31, 'NOT_RUN: owned GTG server PID required')
    raw, connection = _private_record(path)
    require(set(connection) == IDENTITY_KEYS | CONNECTION_CHECKS | {'backend'},
            'GTG connection record shape mismatch')
    _identity(connection, target, window, server_pid)
    require(connection['backend'] == 'postgresql'
            and all(connection[k] is True for k in CONNECTION_CHECKS),
            'NOT_RUN: actual served PostgreSQL identity must be attested')
    _, provenance = _private_record(provenance_path)
    hash_fields = {'connection_attestation_sha256', 'served_config_sha256', 'readiness_marker_sha256'}
    require(set(provenance) == IDENTITY_KEYS | hash_fields | {'after_readiness', 'same_served_connection'},
            'GTG provenance shape mismatch')
    _identity(provenance, target, window, server_pid)
    require(provenance['after_readiness'] is True and provenance['same_served_connection'] is True,
            'GTG served connection provenance mismatch')
    require(all(type(provenance[k]) is str and re.fullmatch(r'[0-9a-f]{64}', provenance[k])
                for k in hash_fields), 'GTG provenance hash shape mismatch')
    ready_raw, ready = _private_record(readiness_path)
    require(set(ready) == {'project', 'fixture', 'window_id', 'server_pid', 'ready'},
            'GTG readiness marker shape mismatch')
    require(ready['project'] == 'alrust' and ready['fixture'] == 'SYNTHETIC'
            and ready['window_id'] == window and ready['ready'] is True
            and type(ready['server_pid']) is int and ready['server_pid'] == int(server_pid),
            'GTG readiness marker instance mismatch')
    config_raw, _ = _private_record(served_config_path)
    load_config_attestation(served_config_path, target)
    require(Path(served_config_path).read_bytes() == config_raw,
            'GTG served config changed during validation')
    require(provenance['connection_attestation_sha256'] == hashlib.sha256(raw).hexdigest()
            and provenance['served_config_sha256'] == hashlib.sha256(config_raw).hexdigest()
            and provenance['readiness_marker_sha256'] == hashlib.sha256(ready_raw).hexdigest(),
            'GTG complete-record hash chain mismatch')


class GtgSession:
    def __init__(self, client):
        self.client = client
        self.attempted = self.completed = False

    def read(self):
        require(not self.attempted, 'GTG read already attempted')
        self.attempted = True
        try:
            response = self.client.request_text('GET', GTG_PATH)
        except Exception:
            raise AssertionError('GTG HTTP text request failed') from None
        require(type(response.status) is int and response.status == 200, 'GTG HTTP status mismatch')
        require(type(response.body) is str and response.body == 'OK', 'GTG original text mismatch')
        self.completed = True

    def safe_summary(self):
        return {'project': 'alrust', 'case': 'management-gtg-readonly-01',
                'completed': self.completed, 'captured_response_records': 0}

    def close(self):
        try:
            self.client.close()
        except Exception:
            raise AssertionError('GTG client close failed') from None
