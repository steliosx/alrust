from dataclasses import dataclass
import json
from typing import Any
from urllib.parse import urlsplit

import httpx


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


def _no_constant(_):
    raise ValueError('non-JSON number')


def parse_json(content: bytes):
    """Parse a response body strictly: a duplicate key or NaN/Infinity raises
    ValueError instead of silently keeping the last key or a float."""
    return json.loads(content, object_pairs_hook=_unique_object, parse_constant=_no_constant)


@dataclass(frozen=True)
class Response:
    status: int
    body: Any


def redact_secrets(value, secrets):
    """Redact credential echoes only when exporting evidence, after comparison."""
    if isinstance(value, dict):
        return {key: redact_secrets(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in sorted((secret for secret in secrets if secret), key=len, reverse=True):
            value = value.replace(secret, '<redacted-api-key>')
    return value


class Client:
    def __init__(self, base_url: str, api_key: str | None = None, *, transport=None, request_id=None):
        url = urlsplit(base_url)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username
                or url.password or url.query or url.fragment):
            raise ValueError('target must be an HTTP(S) base URL without credentials/query/fragment')
        # Only this fixed non-secret trace input is admitted; no arbitrary headers.
        if request_id is not None and (type(request_id) is not str
                or request_id != 'alrust-synthetic-unknown-01'):
            raise ValueError('request ID must be the fixed synthetic unknown-webhook trace')
        self.base_url = base_url.rstrip('/')
        headers = {'Authorization': f'Key {api_key}'} if api_key else {}
        if request_id is not None:
            headers['X-Request-ID'] = request_id
        self.http = httpx.Client(headers=headers, timeout=10, follow_redirects=False,
                                 trust_env=False, transport=transport)

    def request(self, method: str, path: str, payload=None) -> Response:
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('request path must be relative to the selected target')
        response = self.http.request(method, self.base_url + path, json=payload)
        media_type = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
        # Alerta's healthcheck returns plain "OK", not JSON. Malformed declared
        # JSON still fails; non-JSON bodies are preserved verbatim for comparison.
        body = parse_json(response.content) if media_type == 'application/json' or media_type.endswith('+json') else response.text
        return Response(response.status_code, body)

    def request_text(self, method: str, path: str, payload=None) -> Response:
        """Compare original text without JSON parsing or content-type assertions."""
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('request path must be relative to the selected target')
        response = self.http.request(method, self.base_url + path, json=payload)
        return Response(response.status_code, response.text)

    def close(self):
        self.http.close()
