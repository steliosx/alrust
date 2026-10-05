"""Explicit offline-reviewed session hook candidate; no fixture activation.

Only the executor may construct this with its actual client, already-entered
writer capture and independently pinned job/core. Failed session HTTP cleanup
is refused; executor-owned teardown remains a separate qualification gate.
"""
from functools import wraps

from .api_key_capture import ApiKeyCapture, CaptureError
from .api_key_session import ApiKeySession, require


def _oracle(operation):
    @wraps(operation)
    def checked(self, *args, **kwargs):
        with self._capture.oracle_scope():
            self._binding()
            return operation(self, *args, **kwargs)
    return checked


class CapturedApiKeySession(ApiKeySession):
    """Same ordinary oracle, with explicit durable capture and poison scope.

    The private material destination is exactly the pinned job root; no new
    directory is allocated. An executing profile must require this class,
    not silently fall back to the legacy uncaptured fixture.
    """

    def __init__(self, client, private_directory, *, capture, core_sha256, now=None):
        if type(capture) is not ApiKeyCapture:
            raise CaptureError("session_capture_required") from None
        self._capture = capture
        self.client = client
        self._bound_client = client
        self._job_root = private_directory
        self._core_sha256 = core_sha256
        with capture.oracle_scope():
            self._binding()
            super().__init__(client, private_directory, now=now)

    def __repr__(self):
        return "<CapturedApiKeySession private>"

    def _binding(self):
        self._capture.check_session_binding(self.client, self._job_root, self._core_sha256)

    def _persist_material(self, private_directory):
        self._binding()
        self._capture.generated(self._secret)
        # Capture precedes both the private file and any HTTP use.
        super()._persist_material(private_directory)

    def _send_request(self, method, path, payload, expected):
        self._binding()
        # Select the declared shape before the client runs, never from its
        # observed response. The base oracle retains its owned-ID whitelist.
        shapes = {("POST", (201,)): "create", ("GET", (200,)): "entity",
                  ("PUT", (200,)): "entity", ("DELETE", (200,)): "deleted",
                  ("GET", (404,)): "absent", ("DELETE", (200, 404)): "cleanup"}
        require(type(expected) is tuple and all(type(s) is int for s in expected),
                "ordinary key declared status invalid")
        shape = shapes.get((method, expected))
        require(shape is not None, "ordinary key declared operation invalid")
        return self._capture.request(method, path, payload, shape=shape, expected=expected)

    create = _oracle(ApiKeySession.create)
    read = _oracle(ApiKeySession.read)
    update = _oracle(ApiKeySession.update)
    delete = _oracle(ApiKeySession.delete)
    safe_summary = _oracle(ApiKeySession.safe_summary)

    def close(self):
        # Even a failed binding/poison must close the actual injected client,
        # without using a replacement self.client or reopening HTTP teardown.
        try:
            self._binding()
        except BaseException:
            self._capture.abort()
            try:
                self._bound_client.close()
            except BaseException:
                pass  # preserve the primary refusal/interrupt
            raise
        # The base close visits every owned ID and closes the actual client
        # in finally. Any later refused request has no recovery/bypass channel.
        try:
            super().close()
        except BaseException:
            self._capture.abort()
            raise
