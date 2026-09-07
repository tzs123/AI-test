"""RunnerGo login-state validation for the Auto-test FastAPI service."""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests
from fastapi import Request

from backend import settings


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class AuthenticationUnavailable(RuntimeError):
    """The permission service could not validate a credential."""


@dataclass(frozen=True)
class Credential:
    token: str
    source: str


_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_cache_lock = threading.Lock()
_AUTH_RESULT_UNSET = object()


@dataclass
class _AuthInflight:
    """A single permission lookup shared by concurrent requests for one token."""

    event: threading.Event
    result: object = _AUTH_RESULT_UNSET
    error: BaseException | None = None


_inflight: dict[str, _AuthInflight] = {}


def extract_credential(request: Request) -> Credential | None:
    candidates = (
        (request.headers.get("authorization", ""), "authorization"),
        (request.headers.get("token", ""), "token"),
        (request.cookies.get("token", ""), "cookie"),
    )
    for raw, source in candidates:
        token = str(raw or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if token:
            return Credential(token=token, source=source)
    return None


def _cache_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def clear_auth_cache() -> None:
    with _cache_lock:
        _cache.clear()


def validate_credential(token: str) -> dict[str, Any] | None:
    key = _cache_key(token)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
        if cached:
            _cache.pop(key, None)

        pending = _inflight.get(key)
        if pending is None:
            pending = _AuthInflight(event=threading.Event())
            _inflight[key] = pending
            is_leader = True
        else:
            is_leader = False

    if not is_leader:
        # The HTML request and the initial dashboard API calls commonly arrive
        # together.  Wait for the first lookup instead of sending one request
        # to the permission service per browser request.
        if not pending.event.wait(timeout=5.5):
            raise AuthenticationUnavailable("RunnerGo 权限服务响应超时")
        if pending.error is not None:
            raise pending.error
        if pending.result is _AUTH_RESULT_UNSET:
            raise AuthenticationUnavailable("RunnerGo 权限校验未完成")
        return pending.result  # type: ignore[return-value]

    try:
        try:
            response = requests.get(
                settings.PERMISSION_USER_URL,
                headers={"authorization": token, "token": token},
                timeout=5,
            )
        except requests.RequestException as exc:
            raise AuthenticationUnavailable("RunnerGo 权限服务暂时不可用") from exc

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        user = None
        if response.ok and str(payload.get("code")) == "0":
            user = payload.get("data") or {}

        ttl = (
            settings.AUTH_CACHE_TTL_SECONDS
            if user is not None
            else settings.AUTH_NEGATIVE_CACHE_TTL_SECONDS
        )
        with _cache_lock:
            _cache[key] = (now + ttl, user)
            pending.result = user
        return user
    except BaseException as exc:
        with _cache_lock:
            pending.error = exc
        raise
    finally:
        with _cache_lock:
            if _inflight.get(key) is pending:
                _inflight.pop(key, None)
        pending.event.set()


def _normalized_origin(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return f"{parsed.scheme.lower()}://{parsed.hostname.lower()}:{port}"


def request_origin(request: Request) -> str:
    value = request.headers.get("origin", "")
    if not value:
        value = request.headers.get("referer", "")
    return _normalized_origin(value)


def expected_request_origin(request: Request) -> str:
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",", 1)[0].strip()
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "")).split(",", 1)[0].strip()
    return _normalized_origin(f"{scheme}://{host}")


def cookie_request_has_valid_origin(request: Request) -> bool:
    if request.method.upper() in SAFE_METHODS:
        return True
    origin = request_origin(request)
    return bool(origin and origin == expected_request_origin(request))
