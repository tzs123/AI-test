# -*- coding: utf-8 -*-
"""Public signed evidence file access for APP automation."""

from __future__ import annotations

import mimetypes
import os

from django.conf import settings
from django.core import signing
from django.http import FileResponse, Http404
from django.views.decorators.http import require_safe

from ..media_urls import PUBLIC_EVIDENCE_SALT, normalize_evidence_media_path


@require_safe
def serve_public_evidence_file(request, token):
    try:
        relative_path = signing.loads(token, salt=PUBLIC_EVIDENCE_SALT)
    except signing.BadSignature as exc:
        raise Http404('Evidence not found') from exc

    relative_path = normalize_evidence_media_path(relative_path)
    if not relative_path:
        raise Http404('Evidence not found')

    media_root = os.path.abspath(settings.MEDIA_ROOT)
    full_path = os.path.abspath(os.path.join(media_root, relative_path))
    if not full_path.startswith(media_root + os.sep) or not os.path.isfile(full_path):
        raise Http404('Evidence not found')

    content_type = mimetypes.guess_type(full_path)[0] or 'application/octet-stream'
    return FileResponse(open(full_path, 'rb'), content_type=content_type)
