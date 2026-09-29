from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx


@dataclass(frozen=True)
class Response:
    status: int
    body: Any


class Client:
    def __init__(self, base_url: str, api_key: str | None = None, *, transport=None):
        url = urlsplit(base_url)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username
                or url.password or url.query or url.fragment):
            raise ValueError('target must be an HTTP(S) base URL without credentials/query/fragment')
        self.base_url = base_url.rstrip('/')
        headers = {'Authorization': f'Key {api_key}'} if api_key else {}
        self.http = httpx.Client(headers=headers, timeout=10, follow_redirects=False,
                                 trust_env=False, transport=transport)

    def request(self, method: str, path: str, payload=None) -> Response:
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('request path must be relative to the selected target')
        response = self.http.request(method, self.base_url + path, json=payload)
        media_type = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
        # Alerta's healthcheck returns plain "OK", not JSON. Malformed declared
        # JSON still fails; non-JSON bodies are preserved verbatim for comparison.
        body = response.json() if media_type == 'application/json' or media_type.endswith('+json') else response.text
        return Response(response.status_code, body)

    def close(self):
        self.http.close()
