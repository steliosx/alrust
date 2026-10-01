"""Isolated transport for reviewed synthetic security cases; no execution CLI.

The future case runner must enforce its admission/evidence/cleanup prerequisites
before calling this function. Transport selection is not execution permission.
"""
from dataclasses import dataclass, field
from enum import Enum
import re
from urllib.parse import urlsplit

import httpx

from .client import Response


class Identity(Enum):
    ANONYMOUS = 'anonymous'


@dataclass(frozen=True)
class SyntheticKey:
    value: str = field(repr=False)

    def __post_init__(self):
        if type(self.value) is not str or not re.fullmatch(r'SYNTHETIC-[0-9a-f]{48}', self.value):
            raise ValueError('security cases require a labeled synthetic credential')


def security_request(base_url: str, *, identity: Identity | SyntheticKey,
                     method: str, path: str, payload=None, cron_headers=False,
                     transport=None) -> Response:
    """Send exactly one request with explicit identity and no persistent cookies.

    Anonymous is Identity.ANONYMOUS, never a missing/empty key. No caller headers,
    sessions, auth callbacks or environment credentials are accepted. The only
    header control enables the fixed SF-CRON synthetic pair. A new client per
    request prevents response cookies from authenticating later anonymous calls.
    """
    if identity is Identity.ANONYMOUS:
        headers = {}
    elif type(identity) is SyntheticKey:
        headers = {'Authorization': f'Key {identity.value}'}
    else:
        raise ValueError('select explicit anonymous identity or synthetic key')
    if type(cron_headers) is not bool:
        raise ValueError('cron header control must be a boolean')
    if cron_headers:
        headers.update({'X-Appengine-Cron': 'true', 'X-Forwarded-For': '0.1.0.1'})
    if type(base_url) is not str or any(ord(c) < 33 for c in base_url):
        raise ValueError('target must be a credential-free HTTP(S) base URL')
    url = urlsplit(base_url)
    if (url.scheme not in ('http', 'https') or not url.hostname
            or url.username is not None or url.password is not None
            or url.query or url.fragment or '\\' in base_url):
        raise ValueError('target must be a credential-free HTTP(S) base URL')
    # Accessing port also validates nonnumeric/out-of-range ports before I/O.
    _ = url.port
    if (type(path) is not str or not path.startswith('/') or path.startswith('//')
            or '\\' in path or '#' in path or any(ord(c) < 32 for c in path)):
        raise ValueError('path must stay relative to the selected target')
    if method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'):
        raise ValueError('unsupported security case method')
    with httpx.Client(headers=headers, timeout=10, follow_redirects=False,
                      trust_env=False, transport=transport) as client:
        response = client.request(method, base_url.rstrip('/') + path, json=payload)
        media_type = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
        body = (response.json() if media_type == 'application/json' or media_type.endswith('+json')
                else response.text)
        return Response(response.status_code, body)
