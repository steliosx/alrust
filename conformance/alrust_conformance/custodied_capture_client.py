"""Standalone bounded client + concrete private custody; no fixture activation.

The sink owns response-count and charge/write transactions. Deadline checks
are cooperative; the reviewed executor still needs a positive hard deadline
and whole credential-holder generation proof before any real qualification.
"""
import sys
import time

import httpx

from .bounded_capture_client import BoundedClientError, _positive
from .client import Client, Response
from .private_raw_response_sink import PrivateRawResponseSink


class CustodiedCaptureClient(Client):
    """One concrete sink generation and ordinary persistent HTTP identity.

    Explicit close closes both the ordinary HTTP client and its idle sink;
    writer.finish remains the caller's explicit separately checked action.
    """
    def __init__(self, base_url, api_key=None, *, sink, deadline_ns,
                 transport=None, request_id=None):
        if type(sink) is not PrivateRawResponseSink or not _positive(deadline_ns):
            raise BoundedClientError("custody_client_binding_invalid") from None
        self._sink, self._deadline = sink, deadline_ns
        self._failed = self._closed = False
        try:
            sink.register_client(self)
            self._deadline_check()
            super().__init__(base_url, api_key, transport=transport, request_id=request_id)
            self.http.headers["Accept-Encoding"] = "identity"
        except BaseException as error:
            self._raise_failure(error)

    def __repr__(self):
        return "<CustodiedCaptureClient private>"

    def _deadline_check(self):
        if time.monotonic_ns() >= self._deadline:
            raise BoundedClientError("response_deadline_expired") from None

    def _fail(self):
        self._failed = True
        try:
            self._sink.abort()
        except BaseException:
            pass  # preserve the exact primary failure or interrupt

    def _raise_failure(self, error):
        self._fail()
        if not isinstance(error, Exception) or isinstance(error, BoundedClientError):
            raise error
        raise BoundedClientError("response_capture_failed") from None

    def _consume(self, response, *, text_only):
        self._deadline_check()
        coding = response.headers.get("content-encoding")
        if coding is not None and coding.strip().lower() != "identity":
            raise BoundedClientError("response_coding_refused") from None
        pieces = []
        # This capacity belongs to the concrete sink. Content-Length is never
        # trusted, and the client does not inspect/change any budget counters.
        chunk_size = min(self._sink.remaining() + 1, 65536)
        for chunk in response.iter_raw(chunk_size=chunk_size):
            self._deadline_check()
            remaining = self._sink.remaining()
            kept = chunk[:remaining]
            self._sink.reserve_and_append(kept)
            pieces.append(kept)
            if len(chunk) > remaining:
                raise BoundedClientError("response_body_too_large") from None
        self._deadline_check()
        self._sink.finish()  # durable file close BEFORE parsing/oracle return
        buffered = httpx.Response(response.status_code, headers=response.headers,
                                  content=b"".join(pieces))
        media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        body = (buffered.json() if not text_only and
                (media == "application/json" or media.endswith("+json"))
                else buffered.text)
        self._deadline_check()
        return Response(response.status_code, body)

    def _response(self, method, path, payload, *, text_only):
        if self._failed or self._closed:
            raise BoundedClientError("custody_client_unavailable") from None
        try:
            self._sink.check_client_binding(self)
            if type(path) is not str or not path.startswith("/") or path.startswith("//"):
                raise BoundedClientError("response_path_invalid") from None
            self._deadline_check()
            self._sink.begin()  # owns count/custody; exhausted budget => no HTTP
            context = self.http.stream(method, self.base_url + path, json=payload)
            response = context.__enter__()
            try:
                result = self._consume(response, text_only=text_only)
            except BaseException:
                # A stream-close error cannot replace the original body or
                # parsing failure/interrupt. No completion follows either.
                try:
                    context.__exit__(*sys.exc_info())
                except BaseException:
                    pass
                raise
            else:
                context.__exit__(None, None, None)
                return result
        except BaseException as error:
            self._raise_failure(error)

    def request(self, method, path, payload=None):
        return self._response(method, path, payload, text_only=False)

    def request_text(self, method, path, payload=None):
        return self._response(method, path, payload, text_only=True)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.http.close()
            if not self._failed:
                self._sink.close()
        except BaseException as error:
            self._raise_failure(error)

    def __enter__(self):
        self._sink.check_client_binding(self)
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is not None:
            self._fail()
            try:
                self.close()
            except BaseException:
                pass
            return False
        self.close()
        return False
