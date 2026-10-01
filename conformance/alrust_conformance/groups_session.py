"""One original empty user-group page; no response or credential export."""
import json
from pathlib import Path
import re
import stat

from .api_key_session import require
from .config_session import REFERENCE_HEAD, REFERENCE_TARGET


TASK = 'ALRUST-P0-GROUPS-READONLY-01'
PAGE_PATH = '/groups?page=1&page-size=1'
EXPECTED = {'status': 'ok', 'page': 1, 'pageSize': 1, 'pages': 0,
            'more': False, 'message': 'not found', 'groups': [], 'total': 0}


def load_empty_groups_attestation(path, target, window, server_pid):
    """Check a reviewed runner's private count/source/instance record before HTTP.

    Structural validation cannot prove its producer actually counted rows. The
    separately reviewed runner must perform the read-only count on its owned
    disposable database and bind the record to the acknowledged window/PID.
    """
    require(target == REFERENCE_TARGET, 'NOT_RUN: exact isolated groups target required')
    require(isinstance(window, str) and re.fullmatch(
        r'alrust-groups-reference-\d{8}-\d{2}', window) is not None,
        'NOT_RUN: groups window identity required')
    require(isinstance(server_pid, str) and server_pid.isascii() and server_pid.isdecimal()
            and 1 < int(server_pid) < 2**31, 'NOT_RUN: owned groups server PID required')
    require(bool(path), 'NOT_RUN: actual empty groups attestation required')
    try:
        item = Path(path)
        info = item.lstat()
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_size <= 16384, 'groups attestation must be a private regular bounded file')
        data = json.loads(item.read_text())
    except AssertionError:
        raise
    except Exception:
        raise AssertionError('groups attestation is unavailable or malformed') from None
    require(type(data) is dict and set(data) == {
        'project', 'fixture', 'task', 'reference_head', 'target',
        'window_id', 'server_pid', 'groups_count'}, 'groups attestation shape mismatch')
    require(data['project'] == 'alrust' and data['fixture'] == 'SYNTHETIC'
            and data['task'] == TASK and data['reference_head'] == REFERENCE_HEAD
            and data['target'] == target and data['window_id'] == window,
            'groups attestation source or instance mismatch')
    require(type(data['server_pid']) is int and data['server_pid'] == int(server_pid),
            'groups attestation owned PID mismatch')
    require(type(data['groups_count']) is int and data['groups_count'] == 0,
            'NOT_RUN: actual groups dataset must be attested empty')


class GroupsSession:
    def __init__(self, client):
        self.client = client
        self.attempted = self.completed = False

    def read(self):
        require(not self.attempted, 'groups read already attempted')
        self.attempted = True
        try:
            response = self.client.request('GET', PAGE_PATH)
        except Exception:
            raise AssertionError('groups HTTP request or JSON parse failed') from None
        require(response.status == 200, 'groups HTTP status mismatch')
        body = response.body
        require(type(body) is dict and set(body) == set(EXPECTED),
                'groups object or keyset mismatch')
        require(all(type(body[k]) is type(v) and body[k] == v for k, v in EXPECTED.items()),
                'groups original empty-page values mismatch')
        self.completed = True

    def safe_summary(self):
        return {'project': 'alrust', 'case': 'groups-readonly-01',
                'completed': self.completed, 'captured_response_records': 0}

    def close(self):
        try:
            self.client.close()
        except Exception:
            raise AssertionError('groups client close failed') from None
