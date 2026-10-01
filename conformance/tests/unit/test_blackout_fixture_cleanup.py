"""Fault injection into the actual owned fixture; no network or server."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

path = Path(__file__).parents[1] / 'http/test_blackouts.py'
spec = importlib.util.spec_from_file_location('blackout_cases', path)
cases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cases)


def setup(monkeypatch, *, fail_load=False, fail_delete=False):
    clients, deletes = [], []
    class FakeClient:
        def __init__(self, target, key=None):
            self.base_url, self.closed = target, False
            clients.append(self)
        def close(self): self.closed = True
        def request(self, method, path, payload=None):
            if method == 'POST':
                return SimpleNamespace(status=201, body={})  # Persisted, broken response.
            assert method == 'DELETE' and path.startswith('/blackout/')
            deletes.append(path)
            return SimpleNamespace(status=500 if fail_delete and len(deletes) == 1 else 200,
                                   body={'status': 'ok'})
    credentials = SimpleNamespace(target='http://synthetic.invalid',
        keys={'unscoped_admin': 'SYNTHETIC-offline-admin'})
    def load(*args):
        if fail_load: raise ValueError('SYNTHETIC invalid fixture')
        return credentials
    monkeypatch.setenv('ALRUST_TARGET', credentials.target)
    monkeypatch.setattr(cases, 'Client', FakeClient)
    monkeypatch.setattr(cases, 'load_credentials', load)
    return clients, deletes


def test_created_owned_uuid_is_cleaned_after_malformed_201_body(monkeypatch):
    clients, deletes = setup(monkeypatch)
    generator = cases.blackout_session.__wrapped__()
    session = next(generator)
    with pytest.raises(KeyError): session.create()
    assert len(session.owned_ids) == 1
    expected = ['/blackout/' + identifier for identifier in session.owned_ids]
    generator.close()
    assert deletes == expected and all(client.closed for client in clients)


def test_cleanup_attempts_all_owned_ids_and_closes_client_after_failure(monkeypatch):
    clients, deletes = setup(monkeypatch, fail_delete=True)
    generator = cases.blackout_session.__wrapped__()
    session = next(generator)
    session.owned_ids.update({str(uuid.UUID(int=1)), str(uuid.UUID(int=2))})
    expected = ['/blackout/' + identifier for identifier in sorted(session.owned_ids)]
    with pytest.raises(pytest.fail.Exception, match='synthetic blackout cleanup failed'):
        generator.close()
    assert deletes == expected and all(client.closed for client in clients)


def test_invalid_fixture_closes_probe_without_any_delete(monkeypatch):
    clients, deletes = setup(monkeypatch, fail_load=True)
    generator = cases.blackout_session.__wrapped__()
    with pytest.raises(ValueError): next(generator)
    assert len(clients) == 1 and clients[0].closed and not deletes
