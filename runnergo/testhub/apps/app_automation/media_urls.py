# -*- coding: utf-8 -*-
"""Long-term signed media evidence URLs for APP automation runtime artifacts."""

from __future__ import annotations

import copy
import os
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.core import signing


PUBLIC_EVIDENCE_SALT = 'app-automation-public-evidence'
ALLOWED_EVIDENCE_PREFIXES = (
    'app-automation/agent-runtime/',
    'app-automation/screenshots/',
    'app-automation/recordings/',
    'app-automation/action-recordings/',
)


def normalize_evidence_media_path(value: Any) -> str:
    raw = str(value or '').strip()
    if not raw:
        return ''

    media_prefix = settings.MEDIA_URL.rstrip('/') + '/'
    parsed = urlsplit(raw)
    if parsed.scheme and parsed.netloc:
        raw = parsed.path

    raw = raw.lstrip('/')
    if raw.startswith(media_prefix.lstrip('/')):
        raw = raw[len(media_prefix.lstrip('/')):]

    normalized = os.path.normpath(raw).replace(os.sep, '/')
    if normalized in {'.', ''} or normalized.startswith('../') or normalized.startswith('/'):
        return ''
    if not any(normalized.startswith(prefix) for prefix in ALLOWED_EVIDENCE_PREFIXES):
        return ''
    return normalized


def build_public_evidence_path(path: Any) -> str:
    relative_path = normalize_evidence_media_path(path)
    if not relative_path:
        return ''
    token = signing.dumps(relative_path, salt=PUBLIC_EVIDENCE_SALT)
    return f'/api/app-automation/evidence/{token}/'


def enrich_evidence_media_urls(value: Any) -> Any:
    """Recursively convert stored /media evidence URLs to signed, openable URLs."""
    if isinstance(value, list):
        return [enrich_evidence_media_urls(item) for item in value]
    if not isinstance(value, dict):
        return value

    result = copy.deepcopy(value)
    evidence_path = result.get('path') or result.get('url')
    public_url = build_public_evidence_path(evidence_path)
    if public_url:
        result['path'] = normalize_evidence_media_path(evidence_path)
        result['url'] = public_url

    for key, item in list(result.items()):
        if isinstance(item, (dict, list)):
            result[key] = enrich_evidence_media_urls(item)
    return result
