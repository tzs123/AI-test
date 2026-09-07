"""Guards for user-configurable outbound HTTP endpoints."""

from __future__ import annotations

import fnmatch
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

from django.conf import settings


def _setting_list(name):
    return [str(item).strip() for item in getattr(settings, name, []) if str(item).strip()]


def _allowed_networks(values):
    networks = []
    for value in values:
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError as exc:
            raise ValueError(f'无效的外部服务 CIDR 白名单: {value}') from exc
    return networks


def _is_protected(address):
    return not address.is_global


def validate_outbound_http_url(
    value,
    *,
    resolve=True,
    label='外部服务地址',
    allowed_hosts=None,
    allowed_cidrs=None,
    allow_private=None,
):
    """Validate an outbound HTTP(S) URL and return its normalized form."""
    raw = str(value or '').strip()
    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme not in {'http', 'https'} or not parsed.hostname:
        raise ValueError(f'{label}必须是有效的 http/https URL')
    if parsed.username or parsed.password:
        raise ValueError(f'{label}不得包含用户名或密码')
    if parsed.fragment:
        raise ValueError(f'{label}不得包含 URL fragment')

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f'{label}端口无效') from exc

    hostname = parsed.hostname.rstrip('.').lower()
    host_patterns = allowed_hosts
    if host_patterns is None:
        host_patterns = _setting_list('AI_OUTBOUND_ALLOWED_HOSTS')
    host_patterns = [str(item).rstrip('.').lower() for item in host_patterns if str(item).strip()]
    host_allowed = any(fnmatch.fnmatch(hostname, pattern) for pattern in host_patterns)

    cidr_values = allowed_cidrs
    if cidr_values is None:
        cidr_values = _setting_list('AI_OUTBOUND_ALLOWED_CIDRS')
    networks = _allowed_networks(cidr_values)
    if allow_private is None:
        allow_private = bool(getattr(settings, 'AI_OUTBOUND_ALLOW_PRIVATE_URLS', False))

    normalized = urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, ''))

    try:
        literal_address = ipaddress.ip_address(hostname)
    except ValueError:
        literal_address = None

    if literal_address is not None:
        addresses = {literal_address}
    elif hostname == 'localhost' or hostname.endswith('.localhost'):
        addresses = {ipaddress.ip_address('127.0.0.1')}
    elif not resolve:
        return normalized
    elif hostname.endswith('.test'):
        return normalized
    else:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
            }
        except (socket.gaierror, ValueError) as exc:
            raise ValueError(f'{label}域名无法解析') from exc

    if not addresses:
        raise ValueError(f'{label}域名无法解析到有效地址')

    blocked = []
    for address in addresses:
        address_allowed = host_allowed or any(address in network for network in networks)
        if _is_protected(address) and not allow_private and not address_allowed:
            blocked.append(str(address))
    if blocked:
        raise ValueError(
            f'{label}解析到受保护的内网或本机地址: {", ".join(sorted(blocked))}；'
            '如确需访问，请配置 AI_OUTBOUND_ALLOWED_HOSTS/CIDRS 白名单'
        )

    return normalized
