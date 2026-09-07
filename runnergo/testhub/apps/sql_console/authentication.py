"""SQL 控制台（DBeaver 风格多条 SQL 执行）认证。

注入脚本运行在 legacy web-ui 页面里，该页面只持有 RunnerGo manage 的会话
Cookie，不一定持有 TestHub 的 Django JWT。这里提供第二条认证路径：把请求的
Cookie 原样转发给 manage 的一个轻量鉴权接口，manage 认为已登录则放行。
"""
import logging

import requests
from django.conf import settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from apps.users.authentication import get_local_principal

logger = logging.getLogger(__name__)

_MANAGE_PROBE_TIMEOUT = 8


def _manage_probe_ok(request):
    """Forward the browser cookies to manage and treat a non-401 response as
    an authenticated session. Uses get_sql_database_list because it is cheap
    and only reachable with a valid manage session."""
    url = settings.RUNNERGO_MANAGEMENT_API_URL.rstrip('/') + '/target/get_sql_database_list'
    cookie_header = request.META.get('HTTP_COOKIE', '')
    if not cookie_header:
        return False
    try:
        resp = requests.post(
            url,
            json={'team_id': request.headers.get('X-RG-Team-Id', '0'), 'env_id': 0},
            headers={'Cookie': cookie_header},
            timeout=_MANAGE_PROBE_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning('sql-console manage auth probe failed: %s', exc)
        return False
    if resp.status_code == 401:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    # manage 约定：业务码 401 表示未登录；其余（包括参数错误）均说明会话有效。
    return body.get('code') != 401


class ManageCookieAuthentication(BaseAuthentication):
    """Second-path auth for the SQL console: browser carries the manage session."""

    def authenticate(self, request):
        if request.headers.get('X-RG-Manage-Auth') != '1':
            return None
        if not _manage_probe_ok(request):
            raise AuthenticationFailed('manage 会话校验失败，请重新登录 RunnerGo')
        return (get_local_principal(), 'manage-cookie')
