"""Real private temporary writer/journal + synthetic stub; no network/service."""
from datetime import datetime, timezone
import json

import pytest

from alrust_conformance import api_key_session as legacy
from alrust_conformance.api_key_capture import ApiKeyCapture, CaptureError
from alrust_conformance.captured_api_key_session import CapturedApiKeySession
from alrust_conformance.client import Response
from alrust_conformance import private_secret_writer as w
from test_private_secret_writer import job, stage, completion
from test_api_key_privacy_cleanup import StubClient


ABSENT = {"status": "error", "message": "not found", "code": 404,
          "errors": None, "requestId": "synthetic-request-id"}


def discoveries(root):
    return [json.loads(line)["secret"] for line in
            (root / w.EVENTS_NAME).read_bytes().splitlines()[1:]]


def start(root, core, writer, client=None):
    client = client or StubClient()
    capture = ApiKeyCapture(writer, client)
    return CapturedApiKeySession(client, str(root), capture=capture, core_sha256=core), client


def entity(session, text="SYNTHETIC ordinary key"):
    return {"id": session.identifier, "key": session._secret, "status": "active",
            "href": session.client.base_url + "/key/" + session._secret,
            "user": "alrust-synthetic-admin", "scopes": ["read:alerts"],
            "type": "read-only", "text": text, "expireTime": legacy.EXPIRY,
            "count": 0, "lastUsedTime": None, "customer": None}


def test_capture_precedes_private_persistence_and_first_use(job, monkeypatch):
    root, core = job
    original = legacy.persist_private_material
    seen = []
    def persist(directory, identifier, secret):
        assert discoveries(root) == [secret]
        assert not (root / "owned-api-key-material.json").exists()
        seen.append("durable")
        original(directory, identifier, secret)
    monkeypatch.setattr(legacy, "persist_private_material", persist)
    with stage(root) as journal:
        with w.PrivateSecretWriter(str(root), core, journal) as writer:
            session, client = start(root, core, writer)
            assert not client.calls and seen == ["durable"]
            assert json.loads((root / "owned-api-key-material.json").read_bytes())["key"] == session._secret
            session.close()
            writer.finish()
    assert client.closed and completion(root)[0]["count"] == 1


def test_same_ordinary_crud_oracle_and_declared_shapes(job, monkeypatch):
    root, core = job
    declared = []
    with stage(root) as journal:
        with w.PrivateSecretWriter(str(root), core, journal) as writer:
            session, client = start(root, core, writer)
            original = session._capture.request
            def request(method, path, payload=None, *, shape, expected):
                declared.append((shape, expected))
                return original(method, path, payload, shape=shape, expected=expected)
            monkeypatch.setattr(session._capture, "request", request)
            updated = entity(session, "SYNTHETIC ordinary key updated")
            client.responses = [Response(201, {"status": "ok", "key": session._secret, "data": entity(session)}),
                Response(200, {"status": "ok", "total": 1, "key": entity(session)}),
                Response(200, {"status": "ok", "key": updated}),
                Response(200, {"status": "ok", "total": 1, "key": updated}),
                Response(200, {"status": "ok"}), Response(404, ABSENT)]
            session.create(); session.read("SYNTHETIC ordinary key"); session.update()
            session.read("SYNTHETIC ordinary key updated"); session.delete()
            session.safe_summary(); session.close(); writer.finish()
    assert declared == [("create", (201,)), ("entity", (200,)), ("entity", (200,)),
                        ("entity", (200,)), ("deleted", (200,)), ("absent", (404,))]
    assert client.closed and completion(root)[0]["count"] == 1


@pytest.mark.parametrize("field,value", [("customer", "other-customer"),
    ("scopes", ["write:alerts"]), ("expireTime", "2098-01-01T00:00:00.000Z"),
    ("count", False), ("status", "inactive"), ("text", "other"), ("user", "other")])
def test_caught_unchanged_entity_oracle_failure_poison_and_no_http_cleanup(job, field, value):
    root, core = job
    with pytest.raises(w.SecretWriterError):
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                session, client = start(root, core, writer)
                body = entity(session); body[field] = value
                client.responses = [Response(201, {"status": "ok", "key": session._secret, "data": body})]
                with pytest.raises(AssertionError):
                    session.create()
                assert session.identifier in session.owned_ids
                with pytest.raises(CaptureError, match="capture_failed"):
                    session.close()
                assert client.closed and client.calls == [("POST", "/key")]
                writer.finish()
    assert not completion(root)


@pytest.mark.parametrize("fault", ["existing-material", "clock", "persistence-interrupt"])
def test_caught_constructor_failure_permanently_refuses_completion(job, monkeypatch, fault):
    root, core = job
    with pytest.raises(w.SecretWriterError):
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                client = StubClient(); capture = ApiKeyCapture(writer, client)
                kwargs = {}
                expected = AssertionError
                if fault == "existing-material":
                    (root / "owned-api-key-material.json").write_text("SYNTHETIC-old-file\n")
                elif fault == "clock":
                    kwargs["now"] = datetime(2099, 1, 1, tzinfo=timezone.utc)
                else:
                    expected = KeyboardInterrupt
                    def interrupted(*args):
                        raise KeyboardInterrupt("private persistence interrupted")
                    monkeypatch.setattr(legacy, "persist_private_material", interrupted)
                with pytest.raises(expected):
                    CapturedApiKeySession(client, str(root), capture=capture, core_sha256=core, **kwargs)
                assert not client.calls
                writer.finish()
    assert not completion(root)


@pytest.mark.parametrize("fault", ["client", "root", "core", "mutated-core"])
def test_wrong_constructor_binding_refuses_before_material_or_http(job, tmp_path, fault):
    root, core = job
    with pytest.raises(w.SecretWriterError):
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                client = StubClient(); capture = ApiKeyCapture(writer, client)
                given_client, given_root, given_core = client, str(root), core
                if fault == "client": given_client = StubClient()
                elif fault == "root": given_root = str(tmp_path)
                elif fault == "core": given_core = "d" * 64
                else:
                    p = root / "core-identity.json"
                    p.write_bytes(p.read_bytes() + b" ")
                with pytest.raises(CaptureError):
                    CapturedApiKeySession(given_client, given_root, capture=capture, core_sha256=given_core)
                assert not client.calls and not (root / "owned-api-key-material.json").exists()
                writer.finish()
    assert not completion(root)


@pytest.mark.parametrize("fault", ["client", "base", "root", "core", "replaced-root"])
def test_binding_change_refused_and_actual_client_closed(job, fault):
    root, core = job
    with pytest.raises(w.SecretWriterError):
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                session, actual = start(root, core, writer)
                if fault == "client": session.client = StubClient()
                elif fault == "base": actual.base_url = "http://127.0.0.1:18082"
                elif fault == "root": session._job_root += "/other"
                elif fault == "core": session._core_sha256 = "d" * 64
                else:
                    moved = root.with_name("moved")
                    root.rename(moved); root.mkdir(mode=0o700)
                with pytest.raises(CaptureError): session.create()
                with pytest.raises(CaptureError): session.close()
                assert actual.closed and not actual.calls
                if fault == "replaced-root":
                    root.rmdir(); moved.rename(root)
                writer.finish()
    assert not completion(root)


@pytest.mark.parametrize("status,body", [(200, {"status": "ok"}), (404, ABSENT)])
def test_unpoisoned_close_still_uses_exact_owned_id_cleanup(job, status, body):
    root, core = job
    with stage(root) as journal:
        with w.PrivateSecretWriter(str(root), core, journal) as writer:
            session, client = start(root, core, writer)
            session.owned_ids.add(session.identifier)
            client.responses = [Response(status, body)]
            session.close(); writer.finish()
    assert client.closed and client.calls == [("DELETE", "/key/" + session.identifier)]
    assert completion(root)[0]["count"] == 1


@pytest.mark.parametrize("primary_type", [KeyboardInterrupt, SystemExit])
def test_oracle_interrupt_retains_primary_and_poison_even_if_caught(job, monkeypatch, primary_type):
    root, core = job
    primary = primary_type("private oracle interrupt")
    with pytest.raises(w.SecretWriterError):
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                session, client = start(root, core, writer)
                def interrupted(*args): raise primary
                monkeypatch.setattr(session, "_entity", interrupted)
                client.responses = [Response(200, {"status": "ok", "total": 1, "key": entity(session)})]
                with pytest.raises(primary_type) as caught: session.read("SYNTHETIC ordinary key")
                assert caught.value is primary
                with pytest.raises(CaptureError): session.close()
                assert client.closed and len(client.calls) == 1
                writer.finish()
    assert not completion(root)


def test_owned_endpoint_whitelist_still_precedes_client(job):
    root, core = job
    with pytest.raises(w.SecretWriterError):
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                session, client = start(root, core, writer)
                session.identifier = "other-not-owned-uuid"
                with pytest.raises(AssertionError): session.read("SYNTHETIC ordinary key")
                assert not client.calls
                writer.finish()
    assert not completion(root)


@pytest.mark.parametrize("capture", [None, object(), "path"])
def test_capture_is_explicit_and_actual_type_only(tmp_path, capture):
    client = StubClient()
    with pytest.raises(CaptureError, match="session_capture_required"):
        CapturedApiKeySession(client, str(tmp_path), capture=capture, core_sha256="a" * 64)
    assert not client.calls and not list(tmp_path.iterdir())
