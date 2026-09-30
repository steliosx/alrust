"""One original virtual-permissions page; no response or credential export."""
import json
from pathlib import Path
import re
import stat
from uuid import UUID, RFC_4122

from .api_key_session import require
from .config_session import REFERENCE_HEAD, REFERENCE_TARGET


TASK = 'ALRUST-P0-PERMISSIONS-READONLY-01'
PAGE_PATH = '/perms?page=1&page-size=1'
EXPECTED_PAGE = {'status': 'ok', 'page': 1, 'pageSize': 1, 'pages': 3,
                 'more': True, 'total': 3}
EXPECTED_ROLES = {
    'DEFAULT_ADMIN_ROLE': 'admin', 'DEFAULT_USER_ROLE': 'user',
    'DEFAULT_GUEST_ROLE': 'guest', 'USER_DEFAULT_SCOPES': ['read', 'write'],
    'GUEST_DEFAULT_SCOPES': ['read:alerts'],
}
# ADMIN_SCOPES is a pinned source constant, not a served configuration setting.
EXPECTED_ROWS = [('admin', ['admin', 'read', 'write']),
                 ('user', ['read', 'write']), ('guest', ['read:alerts'])]


def load_empty_permissions_attestation(path, target, window, server_pid):
    """Validate a reviewed runner's private source/count/served-role record.

    Structural validation alone proves neither the count nor the served config.
    A separately reviewed runner must observe the actual owned server's app and
    database after readiness, binding fresh records to the same PID/window and
    served-config/readiness hashes. No groups or old config record is reusable.
    """
    require(target == REFERENCE_TARGET, 'NOT_RUN: exact isolated permissions target required')
    require(isinstance(window, str) and re.fullmatch(
        r'alrust-permissions-reference-[0-9]{8}-[0-9]{2}', window) is not None,
        'NOT_RUN: permissions window identity required')
    require(isinstance(server_pid, str) and server_pid.isascii() and server_pid.isdecimal()
            and 1 < int(server_pid) < 2**31, 'NOT_RUN: owned permissions server PID required')
    require(bool(path), 'NOT_RUN: actual empty permissions attestation required')
    try:
        item = Path(path)
        info = item.lstat()
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_size <= 16384,
                'permissions attestation must be a private regular bounded file')
        data = json.loads(item.read_text())
    except AssertionError:
        raise
    except Exception:
        raise AssertionError('permissions attestation is unavailable or malformed') from None
    require(type(data) is dict and set(data) == {
        'project', 'fixture', 'task', 'reference_head', 'target', 'window_id',
        'server_pid', 'permissions_count', 'role_configuration'},
        'permissions attestation shape mismatch')
    require(data['project'] == 'alrust' and data['fixture'] == 'SYNTHETIC'
            and data['task'] == TASK and data['reference_head'] == REFERENCE_HEAD
            and data['target'] == target and data['window_id'] == window,
            'permissions attestation source or instance mismatch')
    require(type(data['server_pid']) is int and data['server_pid'] == int(server_pid),
            'permissions attestation owned PID mismatch')
    require(type(data['permissions_count']) is int and data['permissions_count'] == 0,
            'NOT_RUN: actual permissions dataset must be attested empty')
    roles = data['role_configuration']
    require(type(roles) is dict and set(roles) == set(EXPECTED_ROLES),
            'permissions served role configuration shape mismatch')
    require(all(type(roles[key]) is type(value) and roles[key] == value
                for key, value in EXPECTED_ROLES.items()),
            'permissions served role configuration mismatch')


class PermissionsSession:
    def __init__(self, client):
        self.client = client
        self.attempted = self.completed = False

    def read(self):
        require(not self.attempted, 'permissions read already attempted')
        self.attempted = True
        try:
            response = self.client.request('GET', PAGE_PATH)
        except Exception:
            raise AssertionError('permissions HTTP request or JSON parse failed') from None
        require(response.status == 200, 'permissions HTTP status mismatch')
        body = response.body
        require(type(body) is dict and set(body) == set(EXPECTED_PAGE) | {'permissions'},
                'permissions object or keyset mismatch')
        require(all(type(body[key]) is type(value) and body[key] == value
                    for key, value in EXPECTED_PAGE.items()),
                'permissions original page values mismatch')
        rows = body['permissions']
        require(type(rows) is list and len(rows) == 3, 'permissions virtual row count mismatch')
        identifiers = set()
        for row, (name, scopes) in zip(rows, EXPECTED_ROWS):
            require(type(row) is dict and set(row) == {'id', 'href', 'match', 'scopes'},
                    'permissions row object or keyset mismatch')
            require(type(row['match']) is str and row['match'] == name
                    and type(row['scopes']) is list and row['scopes'] == scopes,
                    'permissions original row order or values mismatch')
            identifier = row['id']
            require(type(identifier) is str, 'permissions original UUID type mismatch')
            try:
                parsed = UUID(identifier)
            except Exception:
                raise AssertionError('permissions original UUID format mismatch') from None
            require(str(parsed) == identifier and parsed.version == 4
                    and parsed.variant == RFC_4122 and identifier not in identifiers,
                    'permissions original UUID format or distinctness mismatch')
            identifiers.add(identifier)
            # Pinned Python urljoin collapses Flask url_root + '/' to this form.
            require(type(row['href']) is str
                    and row['href'] == REFERENCE_TARGET + '/perm/' + identifier,
                    'permissions original href relation mismatch')
        self.completed = True

    def safe_summary(self):
        return {'project': 'alrust', 'case': 'permissions-readonly-01',
                'completed': self.completed, 'captured_response_records': 0}

    def close(self):
        try:
            self.client.close()
        except Exception:
            raise AssertionError('permissions client close failed') from None
