"""Actual private file custody + in-process MockTransport; no live HTTP/SQL."""
from contextlib import contextmanager
import time

import httpx
import pytest

from alrust_conformance.custodied_capture_client import CustodiedCaptureClient
from alrust_conformance.bounded_capture_client import BoundedClientError
from alrust_conformance.client import Client, Response
from alrust_conformance import private_secret_writer as w
from alrust_conformance.private_secret_writer import SecretWriterError
from test_private_secret_writer import job, stage, records, completion
from test_private_raw_response_sink import sink, name


class Stream(httpx.SyncByteStream):
    def __init__(self, chunks, failure=None, close_failure=None):
        self.chunks, self.failure, self.close_failure = chunks, failure, close_failure
        self.read = self.closed = False

    def __iter__(self):
        self.read = True
        yield from self.chunks
        if self.failure is not None: raise self.failure

    def close(self):
        self.closed = True
        if self.close_failure is not None: raise self.close_failure


@contextmanager
def live(job, bodies, *, headers=None, limit=30, total=90, count=10,
         failure=None, close_failure=None, handler=None):
    root, core = job
    calls, streams = [], []
    with stage(root) as journal:
        with w.PrivateSecretWriter(str(root), core, journal) as writer:
            s, budget = sink(root, core, journal, writer, total=total, count=count, response=limit)
            def respond(request):
                calls.append(request)
                assert name(root, len(calls)).is_file()
                assert str(name(root, len(calls))) in [r["payload"]["path"] for r in records(root)
                                                      if r["kind"] == "owned_resource"]
                if handler is not None: return handler(request)
                stream = Stream(bodies[len(calls) - 1], failure, close_failure)
                streams.append(stream)
                return httpx.Response(200, headers=headers, stream=stream)
            client = CustodiedCaptureClient("http://reference.invalid", "SYNTHETIC-test",
                        sink=s, deadline_ns=time.monotonic_ns() + 10**10,
                        transport=httpx.MockTransport(respond))
            with client:
                yield client, s, budget, writer, calls, streams
            writer.finish()


@pytest.mark.parametrize("headers,body,expected,text_only", [
    ({"Content-Type": "application/json"}, b'{"x":1}', {"x": 1}, False),
    ({"Content-Type": "application/problem+json; charset=utf-8"}, b'{"x":2}', {"x": 2}, False),
    ({"Content-Type": "text/plain; charset=latin-1"}, b'caf\xe9', "café", False),
    ({}, b'OK', "OK", False),
    ({"Content-Type": "application/json"}, b'{bad', "{bad", True),
])
def test_same_decode_persist_before_parse_and_charge_once(job, monkeypatch, headers, body, expected, text_only):
    root, _ = job
    with live(job, [[body]], headers=headers) as (client, s, budget, writer, calls, streams):
        real_json = httpx.Response.json
        def parse(response, **kwargs):
            assert s._fd is None and name(root).read_bytes() == body
            return real_json(response, **kwargs)
        monkeypatch.setattr(httpx.Response, "json", parse)
        call = client.request_text if text_only else client.request
        assert call("GET", "/key") == Response(200, expected)
        assert name(root).read_bytes() == body and streams[0].closed
        assert budget.used_bytes == len(body) and budget.responses == 1
        assert calls[0].headers["Authorization"] == "Key SYNTHETIC-test"
        assert calls[0].headers["Accept-Encoding"] == "identity"
    assert len(completion(root)) == 1


@pytest.mark.parametrize("coding", ["gzip", "deflate", "br", "zstd", "identity, gzip", ""])
def test_coding_refused_without_body_iteration_or_completion(job, coding):
    root, _ = job
    with pytest.raises(BoundedClientError, match="response_coding_refused"):
        with live(job, [[b'private']], headers={"Content-Encoding": coding}) as (client, s, budget, writer, calls, streams):
            client.request("GET", "/key")
    assert not streams[0].read and streams[0].closed
    assert name(root).read_bytes() == b"" and not completion(root) and budget._failed


@pytest.mark.parametrize("length", [None, "1", "999"])
def test_stream_actual_limit_content_length_untrusted(job, length):
    headers = {} if length is None else {"Content-Length": length}
    with live(job, [[b'ab', b'cde']], headers=headers, limit=5, total=5) as (c, s, b, w, calls, streams):
        assert c.request("GET", "/key").body == "abcde"
        assert b.used_bytes == 5 and b.responses == 1
    assert len(completion(job[0])) == 1


@pytest.mark.parametrize("chunks", [[b'abcdef'], [b'ab', b'cdef'], [b'x' * 100000]])
def test_overflow_only_bounded_prefix_retained_no_snapshot(job, chunks):
    root, _ = job
    with pytest.raises(BoundedClientError, match="response_body_too_large"):
        with live(job, [chunks], limit=5) as (c, s, b, w, calls, streams):
            c.request("GET", "/key")
    assert len(name(root).read_bytes()) == b.used_bytes == 5
    assert b._failed and streams[0].closed and completion(root) == []


@pytest.mark.parametrize("cause", ["total", "count"])
def test_job_limits_exhaust_before_second_http_and_do_not_refund(job, cause):
    root, _ = job
    with pytest.raises(BoundedClientError):
        with live(job, [[b'abc'], [b'abc']], total=3 if cause == "total" else 20,
                  count=1 if cause == "count" else 3) as (c, s, b, w, calls, streams):
            c.request("GET", "/key")
            c.request("GET", "/key")
    assert len(calls) == 1 and b.used_bytes == 3 and b.responses == 1
    assert not name(root, 2).exists() and not completion(root)


def test_malformed_json_kept_durably_then_writer_poisoned(job):
    root, _ = job
    with pytest.raises(BoundedClientError, match="response_capture_failed"):
        with live(job, [[b'{SYNTHETIC']], headers={"Content-Type": "application/json"}) as (c, s, b, w, calls, streams):
            c.request("GET", "/key")
    assert name(root).read_bytes() == b'{SYNTHETIC' and s._fd is None
    assert w._failed and b._failed and not completion(root)


@pytest.mark.parametrize("primary", [KeyboardInterrupt("SYNTHETIC"), SystemExit(7), RuntimeError("SYNTHETIC")])
@pytest.mark.parametrize("secondary", [None, OSError("SYNTHETIC-close"), KeyboardInterrupt("SYNTHETIC-close")])
def test_primary_survives_stream_close_failure(job, primary, secondary):
    expected = type(primary) if not isinstance(primary, Exception) else BoundedClientError
    with pytest.raises(expected) as caught:
        with live(job, [[b'part']], failure=primary, close_failure=secondary) as (c, s, b, w, calls, streams):
            c.request("GET", "/key")
    if not isinstance(primary, Exception): assert caught.value is primary
    assert streams[0].closed and b._failed and w._failed and not completion(job[0])


def test_stream_close_without_primary_poisoned_before_oracle_return(job):
    with pytest.raises(BoundedClientError):
        with live(job, [[b'OK']], close_failure=OSError("SYNTHETIC")) as (c, s, b, w, calls, streams):
            c.request("GET", "/key")
    assert not completion(job[0])


@pytest.mark.parametrize("bad", ["deadline", "path", "poison", "closed_sink"])
def test_prerequisite_failure_no_http_or_file_allocation(job, bad):
    root, _ = job
    with pytest.raises(BoundedClientError):
        with live(job, [[b'OK']]) as (c, s, b, w, calls, streams):
            if bad == "deadline": c._deadline = 1
            elif bad == "poison":
                with pytest.raises(SecretWriterError): w.abort()
            elif bad == "closed_sink": s.close()
            c.request("GET", "//other.invalid/" if bad == "path" else "/key")
    assert not calls and not name(root).exists() and not completion(root)


def test_one_actual_client_per_sink_and_no_constructor_http(job):
    with pytest.raises(BoundedClientError):
        with live(job, [[b'OK']]) as (c, s, b, w, calls, streams):
            CustodiedCaptureClient(c.base_url, sink=s, deadline_ns=time.monotonic_ns() + 10**10)
    assert not calls and b._failed and not completion(job[0])


def test_persistent_cookies_and_redirect_policy_match_ordinary_client(job):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Set-Cookie": "synthetic_cookie=value; Path=/",
                                           "Location": "http://other.invalid"}, stream=Stream([b'OK']))
    ordinary = Client("http://reference.invalid", transport=httpx.MockTransport(handler))
    try:
        with live(job, [], handler=handler) as (c, s, b, w, calls, streams):
            assert c.request("GET", "/") == ordinary.request("GET", "/")
            assert list(c.http.cookies.items()) == list(ordinary.http.cookies.items())
            assert len(calls) == 1 and len(seen) == 2  # neither followed redirect
            c.request("GET", "/")
            assert "synthetic_cookie=value" in calls[-1].headers["Cookie"]
    finally: ordinary.close()
