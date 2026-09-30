"""Ordinary owned-key oracle. Never export response bodies or credentials."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import stat
import uuid


EXPIRY = '2099-01-01T00:00:00.000Z'
SECRET_PATTERN = re.compile(r'SYNTHETIC-[0-9a-f]{48}')


def require(condition, message):
    # Fixed messages plus --assert=plain/--tb=line: no value repr on failure.
    if not condition:
        raise AssertionError(message)


def shared_evidence_safe(data, private_keys=()):
    """Check every proposed shared artifact; never include rejected bytes."""
    text = data.decode('utf-8', errors='replace') if isinstance(data, bytes) else data
    if SECRET_PATTERN.search(text) or any(key and key in text for key in private_keys):
        raise ValueError('credential pattern found in proposed shared evidence')


def persist_private_material(directory, identifier, secret):
    """Keep the generated key available for the runner's exclusion check only."""
    path = Path(directory)
    try:
        info = path.lstat()
        require(path.is_absolute() and path.resolve() == path,
                'private material requires a resolved absolute directory')
        require(stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700,
                'private material requires a 0700 directory')
        # Empty protected sandbox .git placeholders do not establish a
        # checkout. Files/symlinks/populated dirs and unreadable metadata
        # still fail closed, as do paths inside .git or a mailbox.
        def git_metadata(parent):
            marker = parent / '.git'
            return marker.is_symlink() or marker.is_file() or (
                marker.is_dir() and any(marker.iterdir()))
        require(not any(git_metadata(parent) or parent.name == '.git'
                        or parent.name.endswith('-mailbox')
                        for parent in (path, *path.parents)),
                'private material must stay outside Git and mailboxes')
        destination = path / 'owned-api-key-material.json'
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump({'fixture': 'SYNTHETIC', 'id': identifier, 'key': secret}, handle)
            handle.write('\n')
    except Exception:
        raise AssertionError('private key material persistence failed') from None


class ApiKeySession:
    """Only the fresh supplied UUID is eligible for cleanup; no secret paths."""

    def __init__(self, client, private_directory, *, now=None):
        self.client = client
        self.owned_ids = set()
        self.events = []
        clock = now or datetime.now(timezone.utc)
        require(clock.tzinfo is not None and clock < datetime(2099, 1, 1, tzinfo=timezone.utc),
                'ordinary key requires a clock before the explicit expiry')
        self.identifier = str(uuid.uuid4())
        self._secret = 'SYNTHETIC-' + secrets.token_hex(24)
        persist_private_material(private_directory, self.identifier, self._secret)

    def _request(self, method, path, payload=None, expected=(200,)):
        require((method == 'POST' and path == '/key') or
                (method in ('GET', 'PUT', 'DELETE') and
                 path in {'/key/' + identifier for identifier in self.owned_ids | {self.identifier}}),
                'ordinary key request is outside its owned endpoint scope')
        try:
            response = self.client.request(method, path, payload)
        except Exception:
            raise AssertionError('ordinary key HTTP request failed') from None
        # No body, payload, actual path, URL, headers or exception text emitted.
        self.events.append({'case': 'SYNTHETIC-owned-key', 'method': method,
                            'endpoint': '/key' if path == '/key' else '/key/<owned-uuid>',
                            'status': response.status})
        require(type(response.status) is int and response.status in expected,
                'ordinary key HTTP status mismatch')
        return response.body, response.status

    def _entity(self, body, text):
        expected = {'id': self.identifier, 'key': self._secret, 'status': 'active',
                    'href': self.client.base_url + '/key/' + self._secret,
                    'user': 'alrust-synthetic-admin', 'scopes': ['read:alerts'],
                    'type': 'read-only', 'text': text, 'expireTime': EXPIRY,
                    'count': 0, 'lastUsedTime': None, 'customer': None}
        require(type(body) is dict and body == expected, 'ordinary key entity mismatch')
        require(type(body['count']) is int, 'ordinary key count type mismatch')

    def create(self):
        payload = {'id': self.identifier, 'key': self._secret,
                   'user': 'alrust-synthetic-admin', 'scopes': ['read:alerts'],
                   'text': 'SYNTHETIC ordinary key', 'customer': None, 'expireTime': EXPIRY}
        # Retain the requested UUID before sending: a malformed JSON response
        # or transport failure may occur after the server commits the key.
        self.owned_ids.add(self.identifier)
        body, _ = self._request('POST', '/key', payload, (201,))
        require(type(body) is dict and set(body) == {'status', 'key', 'data'},
                'ordinary key create response shape mismatch')
        require(body['status'] == 'ok' and body['key'] == self._secret,
                'ordinary key create response mismatch')
        self._entity(body['data'], 'SYNTHETIC ordinary key')

    def read(self, text):
        body, _ = self._request('GET', '/key/' + self.identifier)
        require(type(body) is dict and set(body) == {'status', 'total', 'key'}
                and body['status'] == 'ok' and type(body['total']) is int and body['total'] == 1,
                'ordinary key detail response mismatch')
        self._entity(body['key'], text)

    def update(self):
        payload = {'text': 'SYNTHETIC ordinary key updated', 'scopes': ['read:alerts'],
                   'customer': None, 'expireTime': EXPIRY}
        body, _ = self._request('PUT', '/key/' + self.identifier, payload)
        require(type(body) is dict and set(body) == {'status', 'key'} and body['status'] == 'ok',
                'ordinary key update response mismatch')
        self._entity(body['key'], payload['text'])

    def delete(self):
        body, _ = self._request('DELETE', '/key/' + self.identifier)
        require(body == {'status': 'ok'}, 'ordinary key delete response mismatch')
        self.owned_ids.remove(self.identifier)
        body, _ = self._request('GET', '/key/' + self.identifier, expected=(404,))
        require(type(body) is dict and body.get('status') == 'error'
                and type(body.get('code')) is int and body['code'] == 404,
                'ordinary key absence response mismatch')

    def close(self):
        failures = 0
        try:
            for identifier in sorted(self.owned_ids):
                try:
                    body, status = self._request('DELETE', '/key/' + identifier, expected=(200, 404))
                    require(status == 404 or body == {'status': 'ok'},
                            'ordinary key cleanup response mismatch')
                except Exception:
                    failures += 1
        finally:
            try:
                self.client.close()
            except Exception:
                failures += 1
        require(failures == 0, 'ordinary key owned cleanup failed')

    def safe_summary(self):
        # Retain only the fixed metadata generated above; no private material.
        result = json.dumps({'fixture': 'SYNTHETIC', 'case': 'ordinary-key-crud',
                             'events': self.events}, sort_keys=True)
        shared_evidence_safe(result, (self._secret,))
        return result
