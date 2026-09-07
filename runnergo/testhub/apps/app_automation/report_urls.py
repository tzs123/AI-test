# -*- coding: utf-8 -*-
"""Helpers for APP automation Allure report links."""
from urllib.parse import quote

from django.conf import settings
from django.core import signing


PUBLIC_REPORT_SALT = 'app-automation-public-report'


def build_internal_report_path(execution_id) -> str:
    return f"/api/app-automation/executions/{execution_id}/report/"


def build_public_report_path(execution_id) -> str:
    token = signing.TimestampSigner(salt=PUBLIC_REPORT_SALT).sign(str(execution_id))
    return (
        f"/api/app-automation/executions/{execution_id}/public-report/"
        f"{quote(token, safe='')}/"
    )


def build_public_report_url(execution_id) -> str:
    return f"{settings.APP_PUBLIC_BASE_URL.rstrip('/')}{build_public_report_path(execution_id)}"
