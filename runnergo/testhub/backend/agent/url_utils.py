from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit


_SCHEME_URL = re.compile(r'https?://[^\s，,。；;】》」』）)\]]+', re.I)
_BARE_URL = re.compile(
    r'(?<![\w@])('
    r'(?:localhost|(?:\d{1,3}\.){3}\d{1,3}|(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})'
    r'(?::\d{1,5})?'
    r'(?:/[^\s，,。；;】》」』）)\]]*)?'
    r')',
    re.I,
)


def infer_http_url(value: object) -> str:
    """Extract an HTTP(S) URL, accepting common bare host/port input."""
    text = str(value or '').strip()
    if not text:
        return ''

    match = _SCHEME_URL.search(text)
    candidate = match.group(0) if match else ''
    if not candidate:
        match = _BARE_URL.search(text)
        candidate = f'http://{match.group(1)}' if match else ''
    if not candidate:
        return ''

    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname or ''
        port = parsed.port
    except ValueError:
        return ''
    if not hostname or parsed.scheme.lower() not in {'http', 'https'}:
        return ''
    if hostname != 'localhost':
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            if '.' not in hostname:
                return ''
    if port is not None and not 1 <= port <= 65535:
        return ''
    return candidate
