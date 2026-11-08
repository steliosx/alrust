"""Standalone bounded captured-flow client; no fixture or executor activation.

A separately reviewed concrete sink and supervised hard deadline are required
before execution. The interface here is not proof of private file custody.
"""
from abc import ABC, abstractmethod
import os
import sys
import threading
import time

import httpx

from .client import Client, Response, parse_json


class BoundedClientError(ValueError):
    """Fixed diagnostics; raw responses/exceptions remain private."""


def _positive(value):
    return type(value) is int and 0 < value <= sys.maxsize


class PrivateResponseSink(ABC):
    """Explicit sink contract, NOT an implemented private-file sink.

    begin must validate live same-job/core/owner binding and durably record an
    exclusive owned_resource BEFORE append writes any data. finish durably
    closes bytes. abort permanently poisons entrypoint completion. No raw file
    may be an export candidate. All methods may refuse; no fallback is allowed.
    """
    @abstractmethod
    def begin(self): ...

    @abstractmethod
    def append(self, data): ...

    @abstractmethod
    def finish(self): ...

    @abstractmethod
    def abort(self): ...


class RawBudget:
    """One explicitly shared per-job budget; executor must prove its identity.

    Bytes charged before sink writes are never refunded, including failed or
    partial writes. Request count also bounds empty-file allocation. Not an
    environment/global budget or a hostile same-user security boundary.
    """
    def __init__(self, *, max_bytes, max_responses):
        if not _positive(max_bytes) or not _positive(max_responses):
            raise BoundedClientError("raw_budget_invalid") from None
        self.max_bytes, self.max_responses = max_bytes, max_responses
        self.used_bytes = self.responses = 0
        self._failed = False
        self._pid, self._thread = os.getpid(), threading.get_ident()

    def check(self):
        if (self._failed or os.getpid() != self._pid or threading.get_ident() != self._thread
                or self.used_bytes >= self.max_bytes
                or self.responses >= self.max_responses):
            raise BoundedClientError("raw_budget_unavailable") from None

    def begin(self):
        self.check()
        self.responses += 1

    def abort(self):
        self._failed = True

    def charge(self, size):
        if (self._failed or type(size) is not int or size < 0
                or os.getpid() != self._pid or threading.get_ident() != self._thread
                or self.used_bytes + size > self.max_bytes):
            raise BoundedClientError("raw_budget_exceeded") from None
        self.used_bytes += size


class BoundedCaptureClient(Client):
    """Ordinary persistent identity/cookies with explicitly bounded raw input.

    Advertises identity and refuses any nonidentity Content-Encoding BEFORE
    body iteration; captured flows do not qualify compression behavior.
    Monotonic checks are cooperative, not a substitute for process supervision.
    """
    def __init__(self, base_url, api_key=None, *, sink, budget, max_response_bytes,
                 deadline_ns, transport=None, request_id=None):
        if (not isinstance(sink, PrivateResponseSink) or type(budget) is not RawBudget
                or not _positive(max_response_bytes) or not _positive(deadline_ns)):
            raise BoundedClientError("bounded_client_binding_invalid") from None
        self._sink, self._budget = sink, budget
        self._limit, self._deadline = max_response_bytes, deadline_ns
        self._failed = False
        super().__init__(base_url, api_key, transport=transport, request_id=request_id)
        self.http.headers["Accept-Encoding"] = "identity"

    def __repr__(self):
        return "<BoundedCaptureClient private>"

    def _deadline_check(self):
        if time.monotonic_ns() >= self._deadline:
            raise BoundedClientError("response_deadline_expired") from None

    def _fail(self):
        self._failed = True
        self._budget.abort()
        try:
            self._sink.abort()
        except BaseException:
            pass  # preserve primary failure/interrupt; executor must propagate

    def _response(self, method, path, payload, *, text_only):
        if self._failed:
            raise BoundedClientError("bounded_client_failed") from None
        try:
            if type(path) is not str or not path.startswith("/") or path.startswith("//"):
                raise BoundedClientError("response_path_invalid") from None
            self._deadline_check()
            self._budget.begin()  # refuse exhausted budgets BEFORE any HTTP
            self._sink.begin()  # custody before incoming bytes, no implicit sink
            pieces, size = [], 0
            with self.http.stream(method, self.base_url + path, json=payload) as response:
                self._deadline_check()
                coding = response.headers.get("content-encoding")
                if coding is not None and coding.strip().lower() != "identity":
                    raise BoundedClientError("response_coding_refused") from None
                for chunk in response.iter_raw(chunk_size=min(self._limit + 1, 65536)):
                    self._deadline_check()
                    remaining = min(self._limit - size,
                                    self._budget.max_bytes - self._budget.used_bytes)
                    kept = chunk[:remaining]
                    self._budget.charge(len(kept))
                    self._sink.append(kept)
                    pieces.append(kept)
                    size += len(kept)
                    if len(chunk) > remaining:
                        raise BoundedClientError("response_body_too_large") from None
                self._deadline_check()
                self._sink.finish()  # retain bounded raw bytes BEFORE parsing
                buffered = httpx.Response(response.status_code, headers=response.headers,
                                          content=b"".join(pieces))
                media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                body = (parse_json(buffered.content) if not text_only and
                        (media == "application/json" or media.endswith("+json"))
                        else buffered.text)
                self._deadline_check()
                return Response(response.status_code, body)
        except BaseException as error:
            self._fail()
            if not isinstance(error, Exception):
                raise
            if isinstance(error, BoundedClientError):
                raise
            raise BoundedClientError("response_capture_failed") from None

    def request(self, method, path, payload=None):
        return self._response(method, path, payload, text_only=False)

    def request_text(self, method, path, payload=None):
        return self._response(method, path, payload, text_only=True)
