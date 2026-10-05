"""Standalone private API-key capture adapter; no fixture or workflow wiring.

Only an explicitly injected client is called. The caller must durably register
generated values before persistence/use and run its unchanged oracle inside
oracle_scope(). Provisioning, profile completeness, subprocess lifetimes and
writer.finish() belong to the separately reviewed executor, not this adapter.
"""
from contextlib import contextmanager
import re
from urllib.parse import urlsplit

from .client import Response
from .private_secret_writer import PrivateSecretWriter
from .security_assertions import _ERROR_FIELDS


class CaptureError(ValueError):
    """Fixed diagnostic; never a response, credential or client exception."""


_ENTITY = frozenset({"id", "key", "status", "href", "user", "scopes", "type",
                     "text", "expireTime", "count", "lastUsedTime", "customer"})
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


class ApiKeyCapture:
    """Single-writer capture with no implicit client/writer construction.

    Each request declares one supported response shape and expected status.
    Known returned keys are captured before shape/status/oracle assertions.
    Unsupported or malformed responses permanently poison completion. Raw
    responses/exceptions and any test traceback must stay private.
    """

    def __init__(self, writer, client):
        if type(writer) is not PrivateSecretWriter:
            raise CaptureError("capture_writer_required") from None
        self._writer = writer
        self._client = client
        self._failed = False
        self._generated = set()
        self._captured = set()
        try:
            base = client.base_url
            url = urlsplit(base)
            valid = (type(base) is str and base == base.rstrip("/")
                     and url.scheme in ("http", "https") and bool(url.hostname)
                     and not url.username and not url.password
                     and not url.query and not url.fragment)
        except Exception:
            valid = False
        if not valid:
            self._refuse("capture_client_invalid")
        self._base = base
        self._guard()

    def __repr__(self):
        return "<ApiKeyCapture private>"

    def _abort(self):
        self._failed = True
        try:
            self._writer.abort()
        except BaseException:
            # Actual writer.abort latches failure before recording it. A
            # secondary recording failure or interrupt must neither expose
            # private diagnostics nor replace the caller's primary failure.
            # The executor still must propagate its failed operation.
            pass

    def _refuse(self, code):
        self._abort()
        raise CaptureError(code) from None

    def _guard(self):
        if self._failed:
            raise CaptureError("capture_failed") from None
        try:
            self._writer.check_collecting()
        except Exception:
            self._failed = True
            self._abort()
            raise CaptureError("capture_writer_unavailable") from None
        except BaseException:
            self._abort()
            raise

    def generated(self, secret):
        """Durably retain a generated value BEFORE persistence or any use."""
        self._remember(secret)
        self._generated.add(secret)

    def _remember(self, secret):
        self._guard()
        try:
            self._writer.remember(secret)
        except Exception:
            self._failed = True
            self._abort()
            raise CaptureError("capture_persistence_failed") from None
        except BaseException:
            self._abort()
            raise
        self._captured.add(secret)

    @contextmanager
    def oracle_scope(self):
        """Failures in the unchanged oracle poison even a caught exception.

        Preserve the exact primary exception (including interrupts). A later
        writer context exit still refuses if a caller catches this failure.
        """
        self._guard()
        try:
            yield
        except BaseException:
            try:
                self._abort()
            except BaseException:
                pass  # retain the primary; executor must not suppress failures
            raise

    def _entity_capture(self, entity):
        # Capture the known key before rejecting another field of this shape.
        if type(entity) is not dict or type(entity.get("key")) is not str:
            self._refuse("capture_entity_invalid")
        self._remember(entity["key"])
        if set(entity) != _ENTITY:
            self._refuse("capture_entity_invalid")
        if (type(entity["href"]) is not str
                or entity["href"] != self._base + "/key/" + entity["key"]):
            # No claim to recover an unknown divergent href secret. Refusal
            # prevents COMPLETE/export before any ordinary entity assertion.
            self._refuse("capture_href_invalid")

    def _absence(self, body):
        # Reuse the pinned reference error field set. This is the ApiError
        # 'not found' path, not the HTTP routing-error handler. Raw diagnostic
        # values remain private; the unchanged ordinary oracle still runs.
        if (set(body) != _ERROR_FIELDS or body.get("status") != "error"
                or type(body.get("code")) is not int or body["code"] != 404
                or type(body.get("message")) is not str
                or body.get("errors") is not None
                or type(body.get("requestId")) is not str or not body["requestId"]):
            self._refuse("capture_response_invalid")

    def request(self, method, path, payload=None, *, shape, expected):
        """Call the injected client, then capture before returning to oracle.

        Shapes: create (/key POST), entity (UUID GET/PUT), deleted (UUID
        DELETE), absent (UUID GET/DELETE), cleanup (UUID DELETE, 200/404).
        The existing oracle still decides
        field values, customer/scopes/expiry and ownership; no secret path
        or extra endpoint is admitted here. Client parse/transport errors fail
        closed; body-size enforcement remains a future transport prerequisite.
        """
        self._guard()
        if type(method) is not str or type(path) is not str or type(shape) is not str:
            self._refuse("capture_request_invalid")
        uuid_path = (path.startswith("/key/")
                     and _UUID.fullmatch(path[5:]) is not None)
        permitted = ((shape == "create" and method == "POST" and path == "/key")
                     or (shape == "entity" and method in ("GET", "PUT") and uuid_path)
                     or (shape == "deleted" and method == "DELETE" and uuid_path)
                     or (shape == "absent" and method in ("GET", "DELETE") and uuid_path)
                     or (shape == "cleanup" and method == "DELETE" and uuid_path))
        if (not permitted or type(expected) is not tuple or not expected
                or any(type(s) is not int or not 100 <= s <= 599 for s in expected)):
            self._refuse("capture_request_invalid")
        if uuid_path and path[5:] in self._captured:
            self._refuse("capture_secret_path")
        if (shape == "create" and (type(payload) is not dict
                or type(payload.get("key")) is not str
                or payload["key"] not in self._generated)):
            self._refuse("capture_generated_key_missing")
        try:
            response = self._client.request(method, path, payload)
        except Exception:
            self._refuse("capture_request_failed")
        except BaseException:
            try:
                self._abort()
            except BaseException:
                pass
            raise
        if type(response) is not Response or type(response.body) is not dict:
            self._refuse("capture_response_invalid")
        body = response.body
        if shape == "create":
            if type(body.get("key")) is str:
                self._remember(body["key"])
            if type(body.get("data")) is dict:
                self._entity_capture(body["data"])
            else:
                self._refuse("capture_response_invalid")
            if set(body) != {"status", "key", "data"} or type(body["key"]) is not str:
                self._refuse("capture_response_invalid")
        elif shape == "entity":
            self._entity_capture(body.get("key"))
            allowed = {"status", "key", "total"} if method == "GET" else {"status", "key"}
            if set(body) != allowed:
                self._refuse("capture_response_invalid")
        elif shape == "deleted":
            if body != {"status": "ok"}:
                self._refuse("capture_response_invalid")
        elif shape == "cleanup":
            if type(response.status) is not int:
                self._refuse("capture_status_invalid")
            if response.status == 404:
                self._absence(body)
            elif response.status != 200 or body != {"status": "ok"}:
                self._refuse("capture_response_invalid")
        else:
            if type(response.status) is not int or response.status != 404:
                self._refuse("capture_status_invalid")
            self._absence(body)
        if type(response.status) is not int or response.status not in expected:
            self._refuse("capture_status_invalid")
        return response
