# -*- coding: utf-8 -*-
"""带时效签名的 APP 元素模板图片访问。"""

import mimetypes
from pathlib import Path

from django.conf import settings
from django.core import signing
from django.http import FileResponse, Http404
from django.views.decorators.http import require_safe

from ..utils.image_helpers import PUBLIC_TEMPLATE_SALT, normalize_element_image_path


@require_safe
def serve_public_template_file(request, token):
    try:
        relative_path = signing.loads(
            token,
            salt=PUBLIC_TEMPLATE_SALT,
            max_age=max(
                int(getattr(settings, 'LONG_TERM_VALIDITY_SECONDS', 3650 * 24 * 60 * 60)),
                int(getattr(settings, 'PUBLIC_TEMPLATE_MAX_AGE_SECONDS', 3650 * 24 * 60 * 60)),
            ),
        )
    except signing.BadSignature as exc:
        raise Http404('Template not found') from exc

    relative_path = normalize_element_image_path(relative_path)
    if not relative_path:
        raise Http404('Template not found')

    template_root = (
        Path(settings.BASE_DIR) / 'apps' / 'app_automation' / 'Template'
    ).resolve()
    full_path = (template_root / relative_path).resolve()
    if template_root not in full_path.parents or not full_path.is_file():
        raise Http404('Template not found')

    content_type = mimetypes.guess_type(full_path.name)[0] or 'application/octet-stream'
    response = FileResponse(full_path.open('rb'), content_type=content_type)
    response['X-Robots-Tag'] = 'noindex, nofollow'
    response['Cache-Control'] = 'private, max-age=300'
    return response
