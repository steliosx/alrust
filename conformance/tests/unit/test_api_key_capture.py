"""Explicit client stub + real private writer; no service/process/HTTP/SQL."""
import json
import threading
import traceback

import pytest

from alrust_conformance.api_key_capture import ApiKeyCapture, CaptureError
from alrust_conformance.api_key_session import ApiKeySession, EXPIRY, shared_evidence_safe
from alrust_conformance.client import Response
from alrust_conformance import private_secret_writer as w
from alrust_conformance import private_secret_inventory as inv
from alrust_conformance import stage_journal as sj
from test_private_secret_writer import job, stage, completion


KEY = "generated-test-key-" + "g" * 24
OTHER = "unexpected-returned-key-" + "r" * 24
UUID = "12345678-1234-1234-1234-123456789abc"
BASE = "http://127.0.0.1:18081"


def entity(key=KEY):
    return {"id": UUID, "key": key, "status": "active", "href": BASE + "/key/" + key,
            "user": "alrust-synthetic-admin", "scopes": ["read:alerts"], "type": "read-only",
            "text": "SYNTHETIC ordinary key", "expireTime": EXPIRY, "count": 0,
            "lastUsedTime": None, "customer": None}


class Stub:
    base_url = BASE

    def __init__(self, root, replies):
        self.root, self.replies, self.calls = root, list(replies), []

    def request(self, method, path, payload):
        # Actual private event bytes exist before a generated key is used.
        data = (self.root / w.EVENTS_NAME).read_bytes()
        if payload and "key" in payload:
            assert payload["key"] in [json.loads(l).get("secret") for l in data.splitlines()]
        self.calls.append((method, path))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def start(root, capture, replies):
    client = Stub(root, replies)
    bridge = ApiKeyCapture(capture, client)
    return bridge, client


def create(bridge):
    return bridge.request("POST", "/key", {"key": KEY}, shape="create", expected=(201,))


def values(root):
    return [json.loads(line)["secret"] for line in
            (root / w.EVENTS_NAME).read_bytes().splitlines()[1:]]


def test_capture_durable_before_request_and_revoked_history_survives(job):
    root, core = job
    with stage(root) as journal:
        with w.PrivateSecretWriter(str(root), core, journal) as writer:
            bridge, client = start(root, writer, [Response(201, {"status": "ok", "key": KEY,
                "data": entity()}), Response(200, {"status": "ok"})])
            bridge.generated(KEY)
            bridge.generated(KEY)
            with bridge.oracle_scope():
                response = create(bridge)
                # Use the existing entity oracle without changing its predicates.
                oracle = ApiKeySession.__new__(ApiKeySession)
                oracle.client, oracle.identifier, oracle._secret = client, UUID, KEY
                oracle._entity(response.body["data"], "SYNTHETIC ordinary key")
                bridge.request("DELETE", "/key/" + UUID, shape="deleted", expected=(200,))
            assert values(root) == [KEY]
            writer.finish()
    assert inv.decode_inventory((root / w.SNAPSHOT_NAME).read_bytes(),
        expected_core_sha256=core, expected_count=1) == [KEY]
    assert len(completion(root)) == 1
    assert sj.aggregate(str(root), ["entrypoint"])["verdict"] == "VALID"


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_detail_and_update_capture_unexpected_key_before_original_oracle(job, method):
    root, core = job
    body = {"status": "ok", "key": entity(OTHER)}
    if method == "GET":
        body["total"] = 1
    with pytest.raises(AssertionError, match="ordinary key entity mismatch"):
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                bridge, client = start(root, writer, [Response(200, body)])
                bridge.generated(KEY)
                with bridge.oracle_scope():
                    response = bridge.request(method, "/key/" + UUID,
                                              shape="entity", expected=(200,))
                    assert values(root) == [KEY, OTHER]
                    oracle = ApiKeySession.__new__(ApiKeySession)
                    oracle.client, oracle.identifier, oracle._secret = client, UUID, KEY
                    oracle._entity(response.body["key"], "SYNTHETIC ordinary key")
    assert not completion(root) and not (root / w.SNAPSHOT_NAME).exists()


def test_create_keys_captured_even_on_wrong_status(job):
    root, core = job
    with pytest.raises(CaptureError, match="capture_status_invalid"):
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                bridge, _ = start(root, writer, [Response(500, {"status": "ok",
                    "key": OTHER, "data": entity(OTHER)})])
                bridge.generated(KEY)
                create(bridge)
    assert values(root) == [KEY, OTHER]
    assert not completion(root)


@pytest.mark.parametrize("href", [None, 123, BASE + "/key/" + OTHER,
                                BASE + "/key/generated%2Dtest%2Dkey", "missing"])
def test_divergent_href_fails_before_assertion_next_request_or_completion(job, href):
    root, core = job
    data = entity()
    if href == "missing":
        del data["href"]
    else:
        data["href"] = href
    asserted = False
    with pytest.raises(w.SecretWriterError):
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                bridge, client = start(root, writer, [Response(201, {"status": "ok",
                    "key": KEY, "data": data})])
                bridge.generated(KEY)
                with pytest.raises(CaptureError, match="capture_(href|entity)_invalid"):
                    create(bridge)
                    asserted = True
                with pytest.raises(CaptureError, match="capture_failed"):
                    create(bridge)
                assert not asserted and client.calls == [("POST", "/key")]
                # Catching the capture refusal cannot make writer completion valid.
                with pytest.raises(w.SecretWriterError):
                    writer.finish()
    assert not completion(root) and not (root / w.SNAPSHOT_NAME).exists()
    assert sj.aggregate(str(root), ["entrypoint"])["verdict"] == "FAILED"


def test_generated_capture_required_before_use(job):
    root, core = job
    with pytest.raises(CaptureError, match="capture_generated_key_missing"):
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                bridge, client = start(root, writer, [])
                create(bridge)
    assert not client.calls and not completion(root)


def test_fsync_failure_prevents_request_and_keeps_private_error(job, monkeypatch, capsys):
    root, core = job
    with pytest.raises(CaptureError, match="capture_persistence_failed") as caught:
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                bridge, client = start(root, writer, [])
                real = w.os.fsync

                def fail(fd):
                    if fd == writer._events_fd:
                        raise OSError(OTHER)
                    return real(fd)
                monkeypatch.setattr(w.os, "fsync", fail)
                bridge.generated(KEY)
    assert not client.calls and not completion(root)
    shared_evidence_safe("".join(traceback.format_exception(caught.value)), (KEY, OTHER))
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("reply", [None, Response(201, None),
    Response(201, {"status": "ok", "key": KEY, "data": entity(), "extra": OTHER}),
    ValueError("malformed private JSON " + OTHER), RuntimeError("private transport " + OTHER)])
def test_unknown_or_malformed_response_fails_closed_with_private_traceback(job, reply):
    root, core = job
    with pytest.raises(CaptureError) as caught:
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                bridge, _ = start(root, writer, [reply])
                bridge.generated(KEY)
                create(bridge)
    shared_evidence_safe("".join(traceback.format_exception(caught.value)), (KEY, OTHER))
    assert not completion(root) and not (root / w.SNAPSHOT_NAME).exists()


@pytest.mark.parametrize("primary_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("secondary", [False, True])
def test_client_interrupt_keeps_exact_primary(job, monkeypatch, primary_type, secondary):
    root, core = job
    primary = primary_type("private primary " + OTHER)
    with pytest.raises(primary_type) as caught:
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                bridge, _ = start(root, writer, [primary])
                bridge.generated(KEY)
                if secondary:
                    monkeypatch.setattr(writer, "abort", lambda: (_ for _ in ()).throw(
                        RuntimeError("private secondary")))
                create(bridge)
    assert caught.value is primary and not completion(root)


def test_caught_original_oracle_failure_still_poisons_writer(job):
    root, core = job
    primary = AssertionError("fixed ordinary oracle failure")
    with pytest.raises(w.SecretWriterError):
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                bridge, _ = start(root, writer, [])
                try:
                    with bridge.oracle_scope():
                        raise primary
                except AssertionError as caught:
                    assert caught is primary
                with pytest.raises(w.SecretWriterError):
                    writer.finish()
    assert not completion(root)


@pytest.mark.parametrize("primary_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("operation", ["guard", "remember"])
def test_caught_capture_interrupt_still_prevents_completion(job, monkeypatch,
                                                          primary_type, operation):
    root, core = job
    primary = primary_type("private primary " + OTHER)
    with pytest.raises(w.SecretWriterError):
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                bridge, client = start(root, writer, [])

                def interrupted(*args):
                    raise primary
                method = "check_collecting" if operation == "guard" else "remember"
                monkeypatch.setattr(writer, method, interrupted)
                with pytest.raises(primary_type) as caught:
                    bridge.generated(KEY)
                assert caught.value is primary
                with pytest.raises(w.SecretWriterError):
                    writer.finish()
                assert not client.calls
    assert not completion(root)


@pytest.mark.parametrize("mode", ["not_entered", "finished", "aborted"])
def test_public_writer_guard_and_abort_fail_completion(job, mode):
    root, core = job
    with pytest.raises(w.SecretWriterError):
        with stage(root) as journal:
            writer = w.PrivateSecretWriter(str(root), core, journal)
            if mode == "not_entered":
                writer.check_collecting()
            with writer:
                writer.check_collecting()
                if mode == "finished":
                    writer.finish()
                    writer.check_collecting()
                else:
                    writer.abort()
    assert not completion(root)


@pytest.mark.parametrize("base", [None, "http://u:p@127.0.0.1", "http://127.0.0.1/",
                                  "http://127.0.0.1?query", "file:///tmp/x"])
def test_invalid_client_base_fails_before_request(job, base):
    root, core = job
    with pytest.raises(CaptureError, match="capture_client_invalid"):
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                client = Stub(root, [])
                client.base_url = base
                ApiKeyCapture(writer, client)
    assert not client.calls and not completion(root)


@pytest.mark.parametrize("shape,body", [
    ("absent", {"status": "error", "code": 404, "message": "not found"}),
    ("deleted", {"status": "ok"}),
])
def test_supported_cleanup_shapes_stay_capture_neutral(job, shape, body):
    root, core = job
    with stage(root) as journal:
        with w.PrivateSecretWriter(str(root), core, journal) as writer:
            bridge, _ = start(root, writer, [Response(404 if shape == "absent" else 200, body)])
            bridge.generated(KEY)
            bridge.request("DELETE", "/key/" + UUID, shape=shape,
                           expected=(404,) if shape == "absent" else (200,))
            writer.finish()
    assert values(root) == [KEY]
    assert completion(root)[0]["count"] == 1


@pytest.mark.parametrize("state", ["not_entered", "finished", "closed", "foreign_thread"])
def test_wrong_writer_lifetime_blocks_client(job, state):
    root, core = job
    with pytest.raises(w.SecretWriterError):
        with stage(root) as journal:
            writer = w.PrivateSecretWriter(str(root), core, journal)
            if state == "not_entered":
                with pytest.raises(CaptureError, match="capture_writer_unavailable"):
                    ApiKeyCapture(writer, Stub(root, []))
                raise w.SecretWriterError("test_stage_refusal")
            with writer:
                bridge, client = start(root, writer, [])
                if state == "finished":
                    writer.finish()
                elif state == "closed":
                    writer._close_handles()  # simulate an ended writer lifetime
                if state == "foreign_thread":
                    errors = []

                    def call():
                        try:
                            bridge.generated(KEY)
                        except CaptureError as error:
                            errors.append(str(error))
                    child = threading.Thread(target=call)
                    child.start()
                    child.join()
                    assert errors == ["capture_writer_unavailable"]
                else:
                    with pytest.raises(CaptureError, match="capture_writer_unavailable"):
                        bridge.generated(KEY)
                assert not client.calls
    assert not completion(root)


@pytest.mark.parametrize("writer", [None, object(), "path-is-not-a-writer"])
def test_missing_or_wrong_writer_refuses_without_client_use(writer):
    class NoClientUse:
        @property
        def base_url(self):
            raise AssertionError("client touched")
    with pytest.raises(CaptureError, match="capture_writer_required"):
        ApiKeyCapture(writer, NoClientUse())


@pytest.mark.parametrize("path", ["/key/" + OTHER, "//key", "/unreviewed"])
def test_secret_paths_and_other_endpoints_refuse_without_request(job, path):
    root, core = job
    with pytest.raises(CaptureError, match="capture_request_invalid"):
        with stage(root) as journal:
            with w.PrivateSecretWriter(str(root), core, journal) as writer:
                bridge, client = start(root, writer, [])
                bridge.request("GET", path, shape="entity", expected=(200,))
    assert not client.calls and not completion(root)
