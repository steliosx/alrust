"""Offline streaming stubs only: no socket, concrete sink, SQL or child process."""
import time

import httpx
import pytest

from alrust_conformance.bounded_capture_client import (
    BoundedCaptureClient, BoundedClientError, PrivateResponseSink, RawBudget)
from alrust_conformance.client import Client, Response


class Sink(PrivateResponseSink):
    def __init__(self):
        self.bodies, self.current, self.events = [], None, []
        self.failed = False

    def begin(self):
        self.events.append("begin")
        self.current = bytearray()

    def append(self, data):
        self.events.append("append")
        self.current.extend(data)

    def finish(self):
        self.events.append("finish")
        self.bodies.append(bytes(self.current))

    def abort(self):
        self.events.append("abort")
        self.failed = True


class Stream(httpx.SyncByteStream):
    def __init__(self, chunks, failure=None):
        self.chunks, self.failure = chunks, failure
        self.read = self.closed = False

    def __iter__(self):
        self.read = True
        yield from self.chunks
        if self.failure:
            raise self.failure

    def close(self):
        self.closed = True


def build(chunks, *, limit=30, total=90, count=10, headers=None, failure=None):
    stream = Stream(chunks, failure)
    sink = Sink(); calls = []
    def handler(request):
        calls.append(request)
        assert sink.events and sink.events[-1] == "begin"
        return httpx.Response(200, headers=headers, stream=stream)
    budget = RawBudget(max_bytes=total, max_responses=count)
    client = BoundedCaptureClient("http://reference.invalid", "SYNTHETIC-test",
        sink=sink, budget=budget, max_response_bytes=limit,
        deadline_ns=time.monotonic_ns() + 10**10, transport=httpx.MockTransport(handler))
    return client, sink, stream, budget, calls


@pytest.mark.parametrize("headers,body,expected", [
    ({"Content-Type":"application/json"}, b'{"x":1}', {"x":1}),
    ({"Content-Type":"application/problem+json; charset=utf-8"}, b'{"x":2}', {"x":2}),
    ({"Content-Type":"text/plain; charset=latin-1"}, b'caf\xe9', "café"),
    ({}, b'OK', "OK"),
])
def test_original_decode_and_durable_order(headers, body, expected):
    client,sink,stream,budget,calls=build([body],headers=headers)
    try:
        assert client.request("GET","/healthcheck") == Response(200,expected)
        assert sink.bodies == [body] and sink.events[-1] == "finish"
        assert budget.used_bytes == len(body) and stream.closed
        assert calls[0].headers["Accept-Encoding"] == "identity"
        assert calls[0].headers["Authorization"] == "Key SYNTHETIC-test"
    finally: client.close()


@pytest.mark.parametrize("coding", ["gzip","deflate","br","zstd","identity, gzip",""])
def test_nonidentity_refused_before_read(coding):
    client,sink,stream,budget,calls=build([b'private'],headers={"Content-Encoding":coding})
    try:
        with pytest.raises(BoundedClientError,match="response_coding_refused"):
            client.request("GET","/key")
        assert not stream.read and stream.closed and sink.failed and not sink.bodies
        with pytest.raises(BoundedClientError,match="bounded_client_failed"):
            client.request("GET","/key")
        assert len(calls)==1
    finally: client.close()


@pytest.mark.parametrize("content_length", [None,"1","999"])
def test_actual_exact_limit_not_content_length(content_length):
    h={} if content_length is None else {"Content-Length":content_length}
    client,sink,stream,budget,calls=build([b'ab',b'cde'],limit=5,headers=h)
    try: assert client.request("GET","/key").body=="abcde" and sink.bodies==[b'abcde']
    finally: client.close()


@pytest.mark.parametrize("chunks", [[b'abcdef'],[b'ab',b'cdef'],[b'x'*100000]])
def test_oversize_retains_only_cap_and_poison(chunks):
    client,sink,stream,budget,calls=build(chunks,limit=5)
    try:
        with pytest.raises(BoundedClientError,match="response_body_too_large"):
            client.request("GET","/key")
        assert len(sink.current)==5 and budget.used_bytes==5
        assert sink.failed and stream.closed and not sink.bodies
    finally: client.close()


def test_per_job_budget_not_refunded_and_refuses_before_next_request():
    client,sink,stream,budget,calls=build([b'abcde'],limit=5,total=5)
    try:
        client.request("GET","/key")
        with pytest.raises(BoundedClientError,match="raw_budget_unavailable"):
            client.request("GET","/key")
        assert len(calls)==1 and budget.used_bytes==5
    finally: client.close()


def test_malformed_json_is_retained_before_parse_and_poisoned():
    client,sink,stream,budget,calls=build([b'{private'],headers={"Content-Type":"application/json"})
    try:
        with pytest.raises(BoundedClientError,match="response_capture_failed"):
            client.request("GET","/key")
        assert sink.bodies==[b'{private'] and sink.events[-2:]==["finish","abort"]
    finally: client.close()


def test_text_only_does_not_parse_declared_json():
    client,sink,stream,budget,calls=build([b'{bad'],headers={"Content-Type":"application/json"})
    try: assert client.request_text("GET","/key").body=="{bad"
    finally: client.close()


@pytest.mark.parametrize("failure", [RuntimeError("private"),KeyboardInterrupt(),SystemExit(7)])
def test_partial_read_failure_closes_and_preserves_interrupt(failure):
    client,sink,stream,budget,calls=build([b'partial'],failure=failure)
    try:
        expected=type(failure) if isinstance(failure,(KeyboardInterrupt,SystemExit)) else BoundedClientError
        with pytest.raises(expected) as exc: client.request("GET","/key")
        if expected is not BoundedClientError: assert exc.value is failure
        assert sink.failed and stream.closed and not sink.bodies
    finally: client.close()


def test_expired_deadline_no_http():
    client,sink,stream,budget,calls=build([b'OK']);client._deadline=1
    try:
        with pytest.raises(BoundedClientError,match="response_deadline_expired"):
            client.request("GET","/key")
        assert not calls and sink.failed
    finally: client.close()


def test_cookie_behavior_equal_ordinary_client():
    def handler(request):
        return httpx.Response(200,headers={"Set-Cookie":"synthetic_cookie=value; Path=/"},stream=Stream([b'OK']))
    sink=Sink();transport=httpx.MockTransport(handler)
    client=BoundedCaptureClient("http://reference.invalid",sink=sink,
        budget=RawBudget(max_bytes=20,max_responses=3),max_response_bytes=5,
        deadline_ns=time.monotonic_ns()+10**10,transport=transport)
    ordinary=Client("http://reference.invalid",transport=httpx.MockTransport(handler))
    try:
        assert client.request("GET","/")==ordinary.request("GET","/")
        assert list(client.http.cookies.items())==list(ordinary.http.cookies.items())
    finally: client.close();ordinary.close()


@pytest.mark.parametrize("value", [0,-1,True,1.5,None])
def test_invalid_budget(value):
    with pytest.raises(BoundedClientError,match="raw_budget_invalid"):
        RawBudget(max_bytes=value,max_responses=3)


@pytest.mark.parametrize("operation", ["begin","append","finish"])
def test_sink_failure_never_parse_or_returns_success(operation, monkeypatch):
    client,sink,stream,budget,calls=build([b'{"x":1}'],headers={"Content-Type":"application/json"})
    parsed=[]
    monkeypatch.setattr(httpx.Response,"json",lambda self: parsed.append(True))
    def fail(*args): raise OSError("private diagnostic")
    monkeypatch.setattr(sink,operation,fail)
    try:
        with pytest.raises(BoundedClientError,match="response_capture_failed"):
            client.request("GET","/key")
        assert not parsed and sink.failed and budget._failed
        assert not calls if operation=="begin" else stream.closed
    finally: client.close()


def test_per_job_remaining_cap_on_second_response():
    client,sink,stream,budget,calls=build([b'abc'],limit=5,total=5)
    try:
        client.request("GET","/key")
        with pytest.raises(BoundedClientError,match="response_body_too_large"):
            client.request("GET","/key")
        assert sink.bodies==[b'abc'] and bytes(sink.current)==b'ab'
        assert budget.used_bytes==5 and budget._failed and len(calls)==2
    finally: client.close()


def test_empty_response_count_budget():
    client,sink,stream,budget,calls=build([],count=1)
    try:
        client.request("GET","/key")
        with pytest.raises(BoundedClientError,match="raw_budget_unavailable"):
            client.request("GET","/key")
        assert len(calls)==1 and sink.bodies==[b'']
    finally: client.close()


def test_failed_budget_cannot_be_reused_by_another_client():
    client,sink,stream,budget,calls=build([b'oversize'],limit=1)
    other=BoundedCaptureClient("http://reference.invalid",sink=Sink(),budget=budget,
        max_response_bytes=2,deadline_ns=time.monotonic_ns()+10**10,
        transport=httpx.MockTransport(lambda request: pytest.fail("HTTP after poison")))
    try:
        with pytest.raises(BoundedClientError): client.request("GET","/key")
        with pytest.raises(BoundedClientError,match="raw_budget_unavailable"):
            other.request("GET","/key")
    finally: client.close();other.close()


def test_identity_encoding_and_request_json_preserved():
    client,sink,stream,budget,calls=build([b'OK'],headers={"Content-Encoding":" identity "})
    try:
        client.request("POST","/key",{"synthetic":"value"})
        assert calls[0].content==b'{"synthetic":"value"}'
        assert not sink.failed
    finally: client.close()
