"""Credential containment and partial-failure ownership tests; no service."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import traceback
import uuid

import pytest

from alrust_conformance.api_key_session import ApiKeySession, shared_evidence_safe
from alrust_conformance.client import Response


class StubClient:
    base_url = 'http://127.0.0.1:18081'

    def __init__(self):
        self.responses = []
        self.calls = []
        self.closed = False

    def request(self, method, path, payload=None):
        self.calls.append((method, path))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed = True


@pytest.fixture
def session(tmp_path):
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    client = StubClient()
    return ApiKeySession(client, str(directory)), directory


def test_reflected_unexpected_body_and_href_never_exported(session, capsys):
    s, _ = session
    unexpected = 'SYNTHETIC-' + 'b' * 48
    s.client.responses = [Response(201, {'status': 'ok', 'key': unexpected,
                                       'data': {'nested': unexpected, 'href': '/key/' + unexpected}})]
    with pytest.raises(AssertionError, match='ordinary key create response mismatch'):
        s.create()
    assert s.identifier in s.owned_ids
    summary = s.safe_summary()
    assert unexpected not in summary and s._secret not in summary
    assert 'body' not in summary and 'href' not in summary and 'payload' not in summary
    assert capsys.readouterr() == ('', '')


def test_malformed_success_response_still_cleans_requested_uuid(session):
    s, _ = session
    s.client.responses = [Response(201, None), Response(200, {'status': 'ok'})]
    with pytest.raises(AssertionError, match='shape mismatch'):
        s.create()
    s.close()
    assert s.client.calls == [('POST', '/key'), ('DELETE', '/key/' + s.identifier)]
    assert s.client.closed


def test_late_transport_parse_error_keeps_uuid_and_sanitizes_traceback(session):
    s, _ = session
    s.client.responses = [ValueError('reflected ' + s._secret), Response(404, {'status': 'error'})]
    with pytest.raises(AssertionError) as caught:
        s.create()
    report = ''.join(traceback.format_exception(caught.type, caught.value, caught.tb))
    shared_evidence_safe(report, (s._secret,))
    assert 'ordinary key HTTP request failed' in report
    s.close()
    assert s.client.closed and len(s.client.calls) == 2


def test_cleanup_attempts_every_owned_uuid_and_closes_after_first_failure(session):
    s, _ = session
    second = str(uuid.uuid4())
    s.owned_ids = {s.identifier, second}
    s.client.responses = [RuntimeError('private ' + s._secret), Response(200, {'status': 'ok'})]
    with pytest.raises(AssertionError, match='ordinary key owned cleanup failed'):
        s.close()
    assert s.client.calls == [('DELETE', '/key/' + i) for i in sorted(s.owned_ids)]
    assert s.client.closed


def test_generic_pattern_excludes_unknown_secret_in_any_shared_artifact():
    value = 'SYNTHETIC-' + 'c' * 48
    for data in [value, json.dumps({'nested': ['/key/' + value]}), ('error: ' + value).encode()]:
        with pytest.raises(ValueError) as caught:
            shared_evidence_safe(data)
        assert value not in str(caught.value)


def test_explicit_known_key_exclusion_also_covers_nonpattern_values():
    with pytest.raises(ValueError, match='credential pattern found'):
        shared_evidence_safe('UNITTEST-private-marker', ('UNITTEST-private-marker',))


def test_generated_material_only_in_private_file_not_environment_or_stdout(session, capsys):
    s, directory = session
    material = directory / 'owned-api-key-material.json'
    assert stat.S_IMODE(material.stat().st_mode) == 0o600
    content = json.loads(material.read_text())
    assert content == {'fixture': 'SYNTHETIC', 'id': s.identifier, 'key': s._secret}
    assert s._secret not in '\n'.join(os.environ.values())
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('mode,git_marker', [(0o755, False), (0o700, True)])
def test_material_preconditions_fail_before_http(tmp_path, mode, git_marker):
    directory = tmp_path / 'private'
    directory.mkdir(mode=mode)
    directory.chmod(mode)
    if git_marker:
        (directory / '.git').write_text('gitdir: unused\n')
    c = StubClient()
    with pytest.raises(AssertionError, match='private key material persistence failed'):
        ApiKeySession(c, str(directory))
    assert not c.calls and not (directory / 'owned-api-key-material.json').exists()


def test_existing_private_material_preserved(tmp_path):
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    material = directory / 'owned-api-key-material.json'
    material.write_text('UNITTEST-original\n')
    material.chmod(0o600)
    c = StubClient()
    with pytest.raises(AssertionError, match='private key material persistence failed'):
        ApiKeySession(c, str(directory))
    assert material.read_text() == 'UNITTEST-original\n' and not c.calls


def test_populated_git_directory_rejected_but_empty_placeholder_is_not_checkout(tmp_path):
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    marker = directory / '.git'
    marker.mkdir()
    (marker / 'HEAD').write_text('ref: refs/heads/unused\n')
    c = StubClient()
    with pytest.raises(AssertionError, match='private key material persistence failed'):
        ApiKeySession(c, str(directory))
    assert not c.calls and not (directory / 'owned-api-key-material.json').exists()
    (marker / 'HEAD').unlink()
    s = ApiKeySession(c, str(directory))
    assert (directory / 'owned-api-key-material.json').exists() and not c.calls
    assert s.identifier


def test_future_expiry_prerequisite_fails_before_persistence_or_http(tmp_path):
    c = StubClient()
    with pytest.raises(AssertionError, match='clock before the explicit expiry'):
        ApiKeySession(c, str(tmp_path), now=datetime(2099, 1, 1, tzinfo=timezone.utc))
    assert not c.calls and not (tmp_path / 'owned-api-key-material.json').exists()


def test_pinned_plain_pytest_failure_report_does_not_disclose_secret(tmp_path):
    # One deliberately failed OFFLINE nested test proves the exact reporting
    # flags. Its expected exit1 is not an HTTP or harness-suite failure.
    secret = 'SYNTHETIC-' + 'd' * 48
    test = tmp_path / 'test_private_comparison.py'
    test.write_text('def test_fixed_failure():\n'
                    '    secret = "SYNTHETIC-" + "d" * 48\n'
                    '    assert secret == "unexpected", "ordinary key entity mismatch"\n')
    config = Path(__file__).parents[2] / 'api-keys-pytest.ini'
    child_env = {k: v for k, v in os.environ.items()
                 if not k.startswith(('PYTEST_', 'ALRUST_'))}
    child_env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    child = subprocess.run([sys.executable, '-m', 'pytest', '-c', str(config),
                            str(test), '-q'], capture_output=True, text=True,
                           timeout=20, env=child_env)
    report = child.stdout + child.stderr
    assert child.returncode == 1 and '1 failed' in report
    shared_evidence_safe(report, (secret,))
    assert 'ordinary key entity mismatch' in report
    assert not (tmp_path / '.pytest_cache').exists()
