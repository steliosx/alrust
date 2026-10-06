"""Explicit standalone construction; no fixture, executor or live activation."""
import time

from .api_key_capture import ApiKeyCapture
from .bounded_capture_client import RawBudget, _positive
from .captured_api_key_session import CapturedApiKeySession
from .custodied_capture_client import CustodiedCaptureClient
from .private_raw_response_sink import PrivateRawResponseSink
from .private_secret_writer import PrivateSecretWriter


class FlowError(ValueError):
    """Fixed diagnostic; credential and dependency details remain private."""


def make_captured_api_key_session(base_url, api_key=None, *, writer, stage,
                                  job_root, core_sha256, max_bytes, max_responses,
                                  max_response_bytes, deadline_ns, transport=None,
                                  request_id=None, now=None):
    """Construct one actual writer/sink/client/capture/session generation.

    The already-entered writer and stage belong to the caller. A supplied
    authentication key is remembered BEFORE constructing its HTTP client.
    It is not declared a session-generated key. All other components are
    fresh, explicitly bound to the same job/core; none comes from the env.
    Session close closes the client/sink, never finishes the writer or stage.
    Completion still requires the independently reviewed executor's gates.
    """
    if type(writer) is not PrivateSecretWriter:
        raise FlowError("flow_writer_required") from None
    sink = client = None
    try:
        writer.check_stage_binding(stage, job_root, core_sha256)
        if not _positive(deadline_ns) or time.monotonic_ns() >= deadline_ns:
            raise FlowError("flow_deadline_invalid") from None
        budget = RawBudget(max_bytes=max_bytes, max_responses=max_responses)
        sink = PrivateRawResponseSink(writer=writer, stage=stage, job_root=job_root,
                                      core_sha256=core_sha256, budget=budget,
                                      max_response_bytes=max_response_bytes)
        if api_key is not None:
            writer.remember(api_key)
        # Keep the object reachable even if initialization fails after an
        # HTTP handle was allocated, so our failure cleanup can close it.
        client = CustodiedCaptureClient.__new__(CustodiedCaptureClient)
        client.__init__(base_url, api_key, sink=sink, deadline_ns=deadline_ns,
                        transport=transport, request_id=request_id)
        capture = ApiKeyCapture(writer, client)
        return CapturedApiKeySession(client, job_root, capture=capture,
                                     core_sha256=core_sha256, now=now)
    except BaseException as error:
        # Latch failure before cleanup. Secondary close/record failures cannot
        # replace the primary interrupt. No HTTP request or unlink is cleanup.
        try:
            writer.abort()
        except BaseException:
            pass
        if client is not None:
            try:
                client.close()
            except BaseException:
                pass
        if sink is not None:
            try:
                sink.abort()
            except BaseException:
                pass
        if not isinstance(error, Exception) or isinstance(error, FlowError):
            raise
        raise FlowError("flow_construction_failed") from None
