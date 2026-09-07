from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlsplit

from django.conf import settings


_ARTIFACT_DIRECTORIES = ('e2e', 'e2e-mobile')


def resolve_evidence_path(task_id: int, stored_value: str) -> Path | None:
    """Resolve an executor artifact without allowing access outside its task directory."""
    value = str(stored_value or '').strip()
    if not value:
        return None

    media_root = Path(settings.MEDIA_ROOT).resolve()
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return None

    media_url = str(getattr(settings, 'MEDIA_URL', '/media/') or '/media/')
    media_prefix = '/' + media_url.strip('/') + '/'
    decoded_path = unquote(parsed.path)
    if decoded_path.startswith(media_prefix):
        candidate = (media_root / decoded_path[len(media_prefix):]).resolve()
    else:
        raw_path = Path(value)
        if not raw_path.is_absolute():
            return None
        candidate = raw_path.resolve()

    allowed_roots = [
        (media_root / directory / str(int(task_id))).resolve()
        for directory in _ARTIFACT_DIRECTORIES
    ]
    if not any(candidate.is_relative_to(root) for root in allowed_roots):
        return None
    if not candidate.is_file():
        return None
    return candidate


def evidence_content_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or 'application/octet-stream'


def image_data_uri(task_id: int, stored_value: str, *, max_bytes: int = 10 * 1024 * 1024) -> str:
    path = resolve_evidence_path(task_id, stored_value)
    if path is None or path.stat().st_size > max_bytes:
        return ''
    content_type = evidence_content_type(path)
    if not content_type.startswith('image/'):
        return ''
    encoded = base64.b64encode(path.read_bytes()).decode('ascii')
    return f'data:{content_type};base64,{encoded}'
