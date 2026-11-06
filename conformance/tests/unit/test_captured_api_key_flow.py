"""Private temporary custody + streaming MockTransport; no sockets/services."""
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import httpx
import pytest

from alrust_conformance import captured_api_key_flow as flow
from alrust_conformance import private_secret_inventory as inventory
from alrust_conformance import private_secret_writer as w
from alrust_conformance import stage_journal as sj
from alrust_conformance.api_key_capture import CaptureError
from test_private_secret_writer import job, stage, completion, records
from test_captured_api_key_session import entity, ABSENT, discoveries


ADMIN = "SYNTHETIC-admin-key-0001"


def make(root, core, writer, journal, **overrides):
    kwargs = dict(writer=writer, stage=journal, job_root=str(root),
                  core_sha256=core, max_bytes=10000, max_responses=10,
                  max_response_bytes=3000, deadline_ns=time.monotonic_ns() + 10**10)
    kwargs.update(overrides)
    return flow.make_captured_api_key_session("http://reference.invalid", **kwargs)


def no_http(calls):
    def handler(request):
        calls.append(request)
        raise AssertionError("unexpected synthetic HTTP")
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("key", [None, ADMIN])
def test_exact_composition_captures_known_and_generated_before_http(job, key):
    root, core = job
    calls = []
    with stage(root) as journal:
        with w.PrivateSecretWriter(str(root), core, journal) as writer:
            session = make(root, core, writer, journal, api_key=key, transport=no_http(calls))
            client, capture, sink = session.client, session._capture, writer._raw_response_sink
            assert capture._writer is sink.writer is writer
            assert capture._client is sink._client is client and client._sink is sink
            assert sink._stage is journal and session._job_root == str(root)
            assert session._core_sha256 == sink._core == core
            assert discoveries(root) == ([key] if key else []) + [session._secret]
            assert capture._generated == {session._secret} and not calls
            assert not (root / "owned-api-key-material.json").exists()
            session.close()
            assert client.http.is_closed and sink._root_fd is None
            assert not writer._finished and not completion(root)
            writer.finish()
    expected = ([key] if key else []) + [session._secret]
    assert inventory.decode_inventory((root / w.SNAPSHOT_NAME).read_bytes(),
            expected_core_sha256=core, expected_count=len(expected)) == expected
    assert completion(root)[0]["count"] == len(expected)


def test_supplied_key_durable_before_http_client_construction(job, monkeypatch):
    root, core = job
    original = flow.CustodiedCaptureClient.__init__
    def checked(client, *args, **kwargs):
        assert discoveries(root) == [ADMIN]
        return original(client, *args, **kwargs)
    monkeypatch.setattr(flow.CustodiedCaptureClient, "__init__", checked)
    with stage(root) as journal:
        with w.PrivateSecretWriter(str(root), core, journal) as writer:
            session = make(root, core, writer, journal, api_key=ADMIN, transport=no_http([]))
            session.close(); writer.finish()


def test_unchanged_crud_with_raw_custody_and_complete_root_inventory(job):
    root, core = job
    calls, bodies = [], []
    session = None
    def handler(request):
        assert discoveries(root) == [ADMIN, session._secret]
        assert request.headers["Authorization"] == "Key " + ADMIN
        assert request.headers["Accept-Encoding"] == "identity"
        calls.append(request)
        body = ({"status": "ok", "key": session._secret, "data": entity(session)}
                if request.method == "POST" else
                {"status": "ok", "total": 1, "key": entity(session)}
                if len(calls) == 2 else
                {"status": "ok"} if request.method == "DELETE" else ABSENT)
        raw = json.dumps(body).encode(); bodies.append(raw)
        return httpx.Response(201 if request.method == "POST" else
                             404 if len(calls) == 4 else 200,
                             headers={"Content-Type": "application/json"},
                             stream=httpx.ByteStream(raw))
    with stage(root) as journal:
        with w.PrivateSecretWriter(str(root), core, journal) as writer:
            session = make(root, core, writer, journal, api_key=ADMIN,
                           transport=httpx.MockTransport(handler))
            session.create(); session.read("SYNTHETIC ordinary key")
            session.delete(); session.close(); writer.finish()
    assert [r.method for r in calls] == ["POST", "GET", "DELETE", "GET"]
    assert [p.read_bytes() for p in sorted(root.glob("entrypoint-raw-*.bin"))] == bodies
    owned = {Path(r["payload"]["path"]) for r in records(root) if r["kind"] == "owned_resource"}
    assert set(root.iterdir()) == owned | {root / sj.CORE_NAME, root / sj.JOURNAL_DIR}
    assert completion(root)[0]["count"] == 2


@pytest.mark.parametrize("fault", ["root", "core", "stage", "mutated_core",
                                   "deadline", "budget", "per_response", "count"])
def test_bad_binding_or_limit_refuses_before_material_and_http(job, tmp_path, fault):
    root, core = job
    calls = []
    overrides = {"transport": no_http(calls)}
    with pytest.raises(w.SecretWriterError):
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                if fault == "root": overrides["job_root"] = str(tmp_path)
                elif fault == "core": overrides["core_sha256"] = "d" * 64
                elif fault == "stage": overrides["stage"] = object()
                elif fault == "mutated_core":
                    p = root / "core-identity.json"; p.write_bytes(p.read_bytes() + b" ")
                elif fault == "deadline": overrides["deadline_ns"] = 1
                elif fault == "budget": overrides["max_bytes"] = 0
                elif fault == "per_response": overrides["max_response_bytes"] = False
                else: overrides["max_responses"] = 1000000
                with pytest.raises(flow.FlowError): make(root, core, writer, journal, **overrides)
                assert not discoveries(root) and not calls
                assert not list(root.glob("entrypoint-raw-*.bin"))
                writer.finish()
    assert not completion(root)


@pytest.mark.parametrize("key", ["", "short", b"SYNTHETIC-admin-key-0001", "x" * 4097])
def test_unrecordable_auth_key_refuses_before_http_client(job, key, monkeypatch):
    root, core = job
    def forbidden(*args, **kwargs): raise AssertionError("client constructed")
    monkeypatch.setattr(flow.CustodiedCaptureClient, "__init__", forbidden)
    with pytest.raises(w.SecretWriterError):
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                with pytest.raises(flow.FlowError): make(root, core, writer, journal, api_key=key)
                assert not discoveries(root) and writer._raw_response_sink._closed
                writer.finish()
    assert not completion(root)


def test_second_construction_poison_no_second_session_secret_or_http(job):
    root, core = job
    calls = []
    with pytest.raises(w.SecretWriterError):
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                session = make(root, core, writer, journal, transport=no_http(calls))
                first = discoveries(root)
                with pytest.raises(flow.FlowError): make(root, core, writer, journal)
                assert discoveries(root) == first and not calls
                with pytest.raises(CaptureError): session.close()
                assert session.client.http.is_closed
                writer.finish()
    assert not completion(root)


@pytest.mark.parametrize("primary", [KeyboardInterrupt("synthetic"), SystemExit(7),
                                   OSError("synthetic")])
@pytest.mark.parametrize("where", ["remember", "client", "session"])
def test_constructor_failure_closes_handles_preserves_interrupt(job, monkeypatch, primary, where):
    root, core = job
    actual_clients = []
    original_client = flow.CustodiedCaptureClient.__init__
    original_close = flow.CustodiedCaptureClient.close
    def client_init(client, *args, **kwargs):
        actual_clients.append(client)
        original_client(client, *args, **kwargs)
        if where == "client": raise primary
    def close_then_fail(client):
        original_close(client)
        raise KeyboardInterrupt("secondary synthetic close")
    def failed(*args, **kwargs): raise primary
    monkeypatch.setattr(flow.CustodiedCaptureClient, "__init__", client_init)
    monkeypatch.setattr(flow.CustodiedCaptureClient, "close", close_then_fail)
    if where == "session": monkeypatch.setattr(flow, "CapturedApiKeySession", failed)
    with pytest.raises(w.SecretWriterError):
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                if where == "remember": monkeypatch.setattr(writer, "remember", failed)
                expected = flow.FlowError if isinstance(primary, Exception) else type(primary)
                with pytest.raises(expected) as caught:
                    make(root, core, writer, journal, api_key=ADMIN, transport=no_http([]))
                if not isinstance(primary, Exception): assert caught.value is primary
                assert all(c.http.is_closed for c in actual_clients)
                assert writer._raw_response_sink._root_fd is None
                assert not list(root.glob("entrypoint-raw-*.bin"))
                writer.finish()
    assert not completion(root)


def test_oracle_failure_does_not_allow_http_cleanup_or_completion(job):
    root, core = job
    calls = []
    session = None
    def handler(request):
        calls.append(request)
        body = entity(session); body["customer"] = "other-synthetic-customer"
        raw = json.dumps({"status": "ok", "key": session._secret, "data": body}).encode()
        return httpx.Response(201, headers={"Content-Type": "application/json"},
                             stream=httpx.ByteStream(raw))
    with pytest.raises(w.SecretWriterError):
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                session = make(root, core, writer, journal, transport=httpx.MockTransport(handler))
                with pytest.raises(AssertionError): session.create()
                with pytest.raises(CaptureError): session.close()
                assert len(calls) == 1 and session.client.http.is_closed
                writer.finish()
    assert not completion(root)


def test_session_clock_failure_after_client_allocation_closes_and_poisons(job):
    root, core = job
    with pytest.raises(w.SecretWriterError):
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                with pytest.raises(flow.FlowError):
                    make(root, core, writer, journal, now=datetime(2099, 1, 1, tzinfo=timezone.utc),
                         transport=no_http([]))
                assert writer._raw_response_sink._client.http.is_closed
                assert writer._raw_response_sink._root_fd is None
                assert not discoveries(root)
                writer.finish()
    assert not completion(root)
