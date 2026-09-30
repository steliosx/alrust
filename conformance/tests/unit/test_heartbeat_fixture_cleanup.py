"""Offline fault injection into the real heartbeat fixture; no HTTP/service."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest


path = Path(__file__).parents[1] / 'http/test_heartbeats.py'
spec = importlib.util.spec_from_file_location('heartbeat_cases', path)
cases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cases)


def setup(monkeypatch, *, fail_setup=False):
    clients = []
    deletes = []
    class FakeClient:
        def __init__(self, target, key=None):
            if fail_setup and key == 'SYNTHETIC-a-admin':
                raise RuntimeError('offline setup fault')
            self.base_url = target
            self.closed = False
            clients.append(self)
        def close(self):
            self.closed = True
        def request(self, method, path, payload=None):
            assert method == 'DELETE'
            deletes.append(path)
            # Fault the first cleanup; the real fixture must still attempt
            # the remaining owned deletion and close every created client.
            return SimpleNamespace(status=500 if len(deletes) == 1 else 200,
                                   body={'status': 'ok'})
    credentials = SimpleNamespace(target='http://synthetic.invalid',
        keys={'unscoped_admin': 'SYNTHETIC-unscoped', 'a_admin': 'SYNTHETIC-a-admin'})
    monkeypatch.setenv('ALRUST_TARGET', credentials.target)
    monkeypatch.setattr(cases, 'Client', FakeClient)
    monkeypatch.setattr(cases, 'load_credentials', lambda *args: credentials)
    return clients, deletes


def test_cleanup_attempts_every_owned_id_after_failure_and_closes_clients(monkeypatch):
    clients, deletes = setup(monkeypatch)
    generator = cases.heartbeats.__wrapped__()
    session = next(generator)
    identifiers = {str(uuid.UUID(int=1)), str(uuid.UUID(int=2))}
    session.owned_ids.update(identifiers)
    with pytest.raises(pytest.fail.Exception, match='synthetic heartbeat cleanup failed'):
        generator.close()
    assert deletes == ['/heartbeat/' + identifier for identifier in sorted(identifiers)]
    assert len(clients) == 3 and all(client.closed for client in clients)


def test_partial_client_setup_failure_closes_existing_clients(monkeypatch):
    clients, deletes = setup(monkeypatch, fail_setup=True)
    generator = cases.heartbeats.__wrapped__()
    with pytest.raises(RuntimeError, match='offline setup fault'):
        next(generator)
    assert len(clients) == 2 and all(client.closed for client in clients)
    assert deletes == []
