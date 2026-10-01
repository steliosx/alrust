"""Offline building blocks, not an execution gate or a security HTTP runner.

The future reviewed fixture runner must supply trusted effective observations;
matching JSON does not authenticate a collector, acquire a lock, or grant access.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any


REQUIRED_SETTINGS = frozenset({
    'AUTH_REQUIRED', 'ALLOW_READONLY', 'READONLY_SCOPES', 'DELETE_SCOPES',
    'AUTH_PROVIDER', 'AUTH_PROXY', 'ADMIN_ROLES', 'ADMIN_USERS',
    'USER_ROLES', 'GUEST_ROLES', 'USER_DEFAULT_SCOPES', 'GUEST_DEFAULT_SCOPES',
    'ALLOWED_EMAIL_DOMAINS', 'EMAIL_VERIFICATION',
})
IDENTITY_FIELDS = frozenset({
    'profile_id', 'lifecycle_id', 'server_commit', 'database_id', 'workspace_id',
    'settings', 'credentials',
})


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _label(value):
    return type(value) is str and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', value)


def _strings(value):
    return type(value) is list and all(type(item) is str for item in value)


def _identity_valid(value):
    if type(value) is not dict or set(value) != IDENTITY_FIELDS:
        return False
    if not all(_label(value[name]) for name in (
            'profile_id', 'lifecycle_id', 'database_id', 'workspace_id')):
        return False
    if type(value['server_commit']) is not str or not re.fullmatch(
            '[0-9a-f]{40}', value['server_commit']):
        return False
    settings = value['settings']
    if type(settings) is not dict or not REQUIRED_SETTINGS <= settings.keys():
        return False
    for name in ('AUTH_REQUIRED', 'ALLOW_READONLY', 'AUTH_PROXY', 'EMAIL_VERIFICATION'):
        if type(settings[name]) is not bool:
            return False
    for name in ('READONLY_SCOPES', 'DELETE_SCOPES', 'ADMIN_ROLES', 'ADMIN_USERS',
                 'USER_ROLES', 'GUEST_ROLES', 'USER_DEFAULT_SCOPES', 'GUEST_DEFAULT_SCOPES',
                 'ALLOWED_EMAIL_DOMAINS'):
        if not _strings(settings[name]):
            return False
    if type(settings['AUTH_PROVIDER']) is not str:
        return False
    credentials = value['credentials']
    if type(credentials) is not dict:
        return False
    for label, credential in credentials.items():
        if not _label(label) or type(credential) is not dict or set(credential) != {
                'key_sha256', 'scopes', 'roles'}:
            return False
        if type(credential['key_sha256']) is not str or not re.fullmatch(
                '[0-9a-f]{64}', credential['key_sha256']):
            return False
        if not _strings(credential['scopes']) or not _strings(credential['roles']):
            return False
    return True


@dataclass(frozen=True)
class FixtureCheck:
    status: str
    reason: str
    digest: str | None = None


def check_fixture(observed: Any, expected: Any, *, now: int, max_age: int) -> FixtureCheck:
    """Check one lifecycle's effective profile; never default missing fields.

    expected is a reviewed, complete fixture identity, not inferred from HTTP.
    observed adds schema/observed_at/expires_at to that identity. Caller must
    recheck immediately before use; this result is neither a lease nor permission.
    Errors contain no rejected values (which could contain credentials).
    """
    reject = lambda reason: FixtureCheck('NOT_RUN', reason)
    try:
        if type(now) is not int or type(max_age) is not int or max_age <= 0:
            return reject('invalid freshness policy')
        if not _identity_valid(expected):
            return reject('invalid expected profile')
        if type(observed) is not dict or set(observed) != IDENTITY_FIELDS | {
                'schema', 'observed_at', 'expires_at'}:
            return reject('missing or malformed observation')
        if observed['schema'] != 'alrust-security-fixture/1':
            return reject('unknown observation schema')
        start, end = observed['observed_at'], observed['expires_at']
        if type(start) is not int or type(end) is not int:
            return reject('invalid observation timestamps')
        if not 0 <= start <= now < end or now - start > max_age:
            return reject('stale or future observation')
        identity = {name: observed[name] for name in IDENTITY_FIELDS}
        if not _identity_valid(identity):
            return reject('invalid observed profile')
        # JSON comparison distinguishes True from 1; dict equality does not.
        if _json(identity) != _json(expected):
            return reject('effective profile or lifecycle mismatch')
        digest = hashlib.sha256(_json(observed).encode()).hexdigest()
        return FixtureCheck('VALIDATED_OFFLINE', 'fixture identity matched', digest)
    except (TypeError, ValueError, RecursionError):
        return reject('non-JSON fixture data')


STAGES = ('response', 'state', 'audit', 'cleanup')
SIDES = ('reference', 'candidate')


class CaseEvidence:
    """Monotonic evidence states: response status alone cannot earn a pass.

    Each stage represents assertions performed by the future case executor,
    not an unchecked observation. Case-specific assertion code is still needed.
    No transports are constructed here. Unknown reference expectations stay
    skipped; known assertions still have to pass wherever a side is executed.
    """
    def __init__(self, case_id, *, reference_qualified):
        if not _label(case_id) or type(reference_qualified) is not bool:
            raise ValueError('invalid case definition')
        self.case_id = case_id
        self.reference_qualified = reference_qualified
        self._started = set()
        self._checks = {side: {} for side in SIDES}
        self._comparison = None

    def start(self, side):
        if side not in SIDES or side in self._started:
            raise ValueError('invalid or repeated side start')
        self._started.add(side)

    def record(self, side, stage, outcome):
        if (side not in self._started or stage not in STAGES
                or outcome not in ('PASS', 'FAIL') or stage in self._checks[side]):
            raise ValueError('invalid or repeated assertion')
        self._checks[side][stage] = outcome

    def compare(self, outcome):
        if (not self.reference_qualified or self._comparison is not None
                or outcome not in ('PASS', 'FAIL')
                or not all(self._complete(side) for side in SIDES)):
            raise ValueError('comparison requires complete qualified side assertions')
        self._comparison = outcome

    def _complete(self, side):
        return self._checks[side] == dict.fromkeys(STAGES, 'PASS')

    def snapshot(self):
        sides = {}
        for side in SIDES:
            checks = self._checks[side]
            state = ('FAIL' if 'FAIL' in checks.values() else
                     'PASS' if self._complete(side) else
                     'INCOMPLETE' if side in self._started else 'NOT_RUN')
            sides[side] = {'status': state, 'assertions': dict(checks)}
        if any(side['status'] == 'FAIL' for side in sides.values()) or self._comparison == 'FAIL':
            result = 'FAIL'
        elif not self.reference_qualified:
            result = 'SKIPPED_REFERENCE_UNQUALIFIED'
        elif self._comparison == 'PASS':
            result = 'LOCAL_PASS'
        else:
            result = 'INCOMPLETE' if self._started else 'NOT_RUN'
        return {'case_id': self.case_id, 'differential': result,
                'sides': sides, 'comparison': self._comparison,
                'reference_qualified': self.reference_qualified}


class PrivateJournal:
    """Reserve an exclusive 0600 JSONL report and fsync each complete record.

    Inputs must be secret-free metadata/assertion summaries. Raw responses and
    credential redaction belong in the later executor, after comparisons. A
    partial trailing record after interruption must never be read as success.
    Parent directory must already be private and trusted by the fixture runner.
    """
    def __init__(self, path):
        path = Path(path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(parent)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError('journal parent must be private and owned by the caller')
            self._fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                               | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        finally:
            os.close(parent)
        self._sequence = 0

    def append(self, value):
        if self._fd is None:
            raise ValueError('journal is closed')
        data = (_json({'sequence': self._sequence, 'evidence': value}) + '\n').encode()
        try:
            view = memoryview(data)
            while view:
                written = os.write(self._fd, view)
                if written == 0:
                    raise OSError('journal write made no progress')
                view = view[written:]
            os.fsync(self._fd)
        except OSError:
            self.close()  # A later success cannot hide a partial failed write.
            raise
        self._sequence += 1

    def close(self):
        if self._fd is not None:
            descriptor, self._fd = self._fd, None
            os.close(descriptor)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
