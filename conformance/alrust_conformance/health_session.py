"""One literal management health response; no response or credential export."""
import json
from pathlib import Path
import re
import stat

from .api_key_session import require
from .config_session import REFERENCE_HEAD, REFERENCE_TARGET

TASK = 'ALRUST-P0-MANAGEMENT-HEALTH-READONLY-01'
HEALTH_PATH = '/management/healthcheck'


def load_empty_heartbeats_attestation(path, target, window, server_pid):
    """Validate the reviewed runner's private instance record before Client.

    Shape validation cannot prove the observation. A separately reviewed runner
    must count on the actual served connection after readiness, validate the
    backend URI/name/schema/destination, and bind record bytes to the same
    PID/window/readiness/served-configuration hash chain. Old records cannot
    replace that observation.
    """
    require(target == REFERENCE_TARGET, 'NOT_RUN: exact isolated health target required')
    require(isinstance(window, str) and re.fullmatch(
        r'alrust-management-health-reference-[0-9]{8}-[0-9]{2}', window) is not None,
        'NOT_RUN: health window identity required')
    require(isinstance(server_pid, str) and server_pid.isascii() and server_pid.isdecimal()
            and 1 < int(server_pid) < 2**31, 'NOT_RUN: owned health server PID required')
    require(bool(path), 'NOT_RUN: actual empty heartbeats attestation required')
    try:
        item = Path(path)
        info = item.lstat()
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_size <= 16384,
                'health attestation must be a private regular bounded file')
        data = json.loads(item.read_text())
    except AssertionError:
        raise
    except Exception:
        raise AssertionError('health attestation is unavailable or malformed') from None
    require(type(data) is dict and set(data) == {
        'project', 'fixture', 'task', 'reference_head', 'target',
        'window_id', 'server_pid', 'heartbeats_count'}, 'health attestation shape mismatch')
    require(data['project'] == 'alrust' and data['fixture'] == 'SYNTHETIC'
            and data['task'] == TASK and data['reference_head'] == REFERENCE_HEAD
            and data['target'] == target and data['window_id'] == window,
            'health attestation source or instance mismatch')
    require(type(data['server_pid']) is int and data['server_pid'] == int(server_pid),
            'health attestation owned PID mismatch')
    require(type(data['heartbeats_count']) is int and data['heartbeats_count'] == 0,
            'NOT_RUN: actual heartbeat dataset must be attested empty')


class HealthSession:
    def __init__(self, client):
        self.client = client
        self.attempted = self.completed = False

    def read(self):
        require(not self.attempted, 'health read already attempted')
        self.attempted = True
        try:
            response = self.client.request_text('GET', HEALTH_PATH)
        except Exception:
            raise AssertionError('health HTTP text request failed') from None
        require(type(response.status) is int and response.status == 200,
                'health HTTP status mismatch')
        require(type(response.body) is str and response.body == 'OK',
                'health original text mismatch')
        self.completed = True

    def safe_summary(self):
        return {'project': 'alrust', 'case': 'management-health-readonly-01',
                'completed': self.completed, 'captured_response_records': 0}

    def close(self):
        try:
            self.client.close()
        except Exception:
            raise AssertionError('health client close failed') from None
