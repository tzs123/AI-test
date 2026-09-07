"""SSRF protection for URLs opened by browser automation."""

from __future__ import annotations

import fnmatch
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

from backend import settings


# Clash and similar local DNS proxies commonly return synthetic addresses from
# RFC 2544's benchmarking range for public hostnames. A literal URL using this
# range is still blocked; only a hostname resolved by the local DNS proxy gets
# this compatibility treatment.
_SYNTHETIC_DNS_NETWORKS = (ipaddress.ip_network("198.18.0.0/15"),)
_NON_ROUTABLE_TEST_HOSTS = {"example.test"}


def _host_allowed(hostname: str) -> bool:
    hostname = hostname.rstrip(".").lower()
    for pattern in settings.ALLOWED_URL_HOSTS:
        normalized = pattern.strip().rstrip(".").lower()
        if normalized and fnmatch.fnmatch(hostname, normalized):
            return True
    return False


def _allowed_networks() -> list[ipaddress._BaseNetwork]:
    networks = []
    for value in settings.ALLOWED_URL_CIDRS:
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError as exc:
            raise ValueError(f"AUTO_TEST_ALLOWED_URL_CIDRS 配置无效: {value}") from exc
    return networks


def _address_allowed(address: ipaddress._BaseAddress) -> bool:
    if any(address in network for network in _allowed_networks()):
        return True
    if settings.ALLOW_PRIVATE_URLS:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def validate_outbound_url(value: str) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("录制地址必须是有效的 http/https URL")
    if parsed.username or parsed.password:
        raise ValueError("录制地址不得包含用户名或密码")

    hostname = parsed.hostname.rstrip(".").lower()
    if _host_allowed(hostname) or hostname in _NON_ROUTABLE_TEST_HOSTS:
        return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "", parsed.query, parsed.fragment))

    hostname_is_literal = True
    try:
        literal = ipaddress.ip_address(hostname)
        addresses = {literal}
    except ValueError:
        hostname_is_literal = False
        try:
            port = parsed.port
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
            }
        except (socket.gaierror, ValueError) as exc:
            raise ValueError("录制地址域名无法解析") from exc

    blocked = [
        str(address)
        for address in addresses
        if not _address_allowed(address)
        and not (
            not hostname_is_literal
            and any(address in network for network in _SYNTHETIC_DNS_NETWORKS)
        )
    ]
    if blocked:
        raise ValueError(f"录制地址解析到受保护的内网或本机地址: {', '.join(sorted(blocked))}")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "", parsed.query, parsed.fragment))


def validate_outbound_endpoint(hostname: str, port: int | None = None) -> tuple[str, int | None]:
    """SSRF 校验任意协议的 host:port（tcp/ws/ftp/smtp/ldap 等），复用 http 路径的内网规则。"""
    hostname = str(hostname or "").rstrip(".").lower()
    if not hostname:
        raise ValueError("协议目标主机不能为空")
    if port is not None:
        port = int(port)
        if not (1 <= port <= 65535):
            raise ValueError("协议目标端口必须在 1-65535 之间")
    if _host_allowed(hostname) or hostname in _NON_ROUTABLE_TEST_HOSTS:
        return hostname, port

    hostname_is_literal = True
    try:
        literal = ipaddress.ip_address(hostname)
        addresses = {literal}
    except ValueError:
        hostname_is_literal = False
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
            }
        except (socket.gaierror, ValueError) as exc:
            raise ValueError("协议目标域名无法解析") from exc

    blocked = [
        str(address)
        for address in addresses
        if not _address_allowed(address)
        and not (
            not hostname_is_literal
            and any(address in network for network in _SYNTHETIC_DNS_NETWORKS)
        )
    ]
    if blocked:
        raise ValueError(f"协议目标解析到受保护的内网或本机地址: {', '.join(sorted(blocked))}")
    return hostname, port


def validate_outbound_hostport_url(raw_url: str) -> tuple[str, str, int | None, str, str]:
    """解析任意 scheme 的 URL，校验 host:port，返回 (scheme, host, port, path, query)。"""
    parsed = urlsplit(str(raw_url or "").strip())
    scheme = parsed.scheme.lower()
    if not scheme or not parsed.hostname:
        raise ValueError("协议目标 URL 无效：缺少 scheme 或 host")
    if parsed.username or parsed.password:
        raise ValueError("协议目标 URL 不得包含用户名密码（请用步骤凭据字段）")
    host, port = validate_outbound_endpoint(parsed.hostname, parsed.port)
    return scheme, host, port, parsed.path or "/", parsed.query


def guard_playwright_route(route) -> None:
    """Validate every browser request, including redirects and subresources."""
    parsed = urlsplit(route.request.url)
    if parsed.scheme in {"http", "https"}:
        try:
            validate_outbound_url(route.request.url)
        except ValueError:
            route.abort("blockedbyclient")
            return
    route.continue_()
