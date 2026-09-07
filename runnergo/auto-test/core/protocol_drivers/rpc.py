"""RPC 协议驱动：gRPC（裸调用）与 Dubbo（经 HTTP 网关）。

gRPC 裸调用通过 ``channel.unary_unary(method)`` 按 fully-qualified 方法名发起
HTTP/2 调用，请求体由调用方提供序列化字节（hex/base64/text），因此可压测任意
proto 服务而无需预编译 stub。Dubbo 无成熟 Python 客户端，走 HTTP/Triple 网关。

YAML 步骤示例::

    - id: call
      action: grpc
      target: 127.0.0.1:50051
      method: /helloworld.Greeter/SayHello
      request: "0a0568656c6c6f"   # hex 序列化字节
      encoding: hex
      timeout_ms: 5000
    - id: dubbo
      action: dubbo
      url: http://127.0.0.1:8000/dubbo
      service: org.demo.UserService
      method: getUser
      params: '[{"id":1}]'
      timeout_ms: 5000
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any, Dict

import requests
from backend.url_security import validate_outbound_endpoint, validate_outbound_url
from .base import (
    attach_runtime_result,
    base_result,
    protocol_sessions,
    register_driver,
    require_dependency,
    resolve,
)
from .base import ProtocolDriver


def _request_bytes(step: Dict[str, Any], context: Any) -> bytes:
    raw = resolve(context, step.get("request") or step.get("data") or "")
    encoding = str(resolve(context, step.get("encoding") or "text")).lower()
    text = str(raw) if raw is not None else ""
    if encoding == "hex":
        return bytes.fromhex(text)
    if encoding == "base64":
        return base64.b64decode(text)
    return text.encode("utf-8")


class GrpcDriver(ProtocolDriver):
    name = "grpc"
    actions = {"grpc"}
    optional_dependency = "grpcio (pip install grpcio)"

    def execute(self, step: Dict[str, Any], context: Any) -> Dict[str, Any]:
        try:
            import grpc
        except ImportError:
            require_dependency("grpcio", "pip install grpcio")
        host = str(resolve(context, step.get("host") or step.get("target") or "")).split(":")
        target = str(resolve(context, step.get("target") or ""))
        if not target:
            raw_host = str(resolve(context, step.get("host") or ""))
            port = int(resolve(context, step.get("port") or 0) or 0)
            host_name, port_num = validate_outbound_endpoint(raw_host, port)
            target = f"{host_name}:{port_num}"
        method = str(resolve(context, step.get("method") or ""))
        timeout_ms = int(resolve(context, step.get("timeout") or step.get("timeout_ms") or 10000) or 10000)
        request_bytes = _request_bytes(step, context)
        result = base_result(step, action="grpc")
        result["url"] = f"grpc://{target}{method}"
        started = time.monotonic()
        try:
            pool = protocol_sessions(context)
            key = ("grpc", target)
            channel = pool.get(key)
            if channel is None:
                channel = grpc.insecure_channel(target)
                pool[key] = channel
            response = channel.unary_unary(method)(
                request_bytes,
                timeout=max(0.1, timeout_ms / 1000),
            )
            result["body"] = response.hex()
            result["text"] = response.decode("utf-8", "replace")
            result["status"] = "ok"
        except Exception as exc:
            result["status"] = "error"
            result["body"] = str(exc)
            attach_runtime_result(exc, result)
            raise
        finally:
            result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
        return result


class DubboDriver(ProtocolDriver):
    """Dubbo 经 HTTP/Triple 网关调用（无成熟 Python 原生客户端）。"""

    name = "dubbo"
    actions = {"dubbo"}

    def execute(self, step: Dict[str, Any], context: Any) -> Dict[str, Any]:
        raw_url = str(resolve(context, step.get("url") or ""))
        url = validate_outbound_url(raw_url)
        service = str(resolve(context, step.get("service") or ""))
        method = str(resolve(context, step.get("method") or ""))
        params = resolve(context, step.get("params") or "[]")
        timeout_ms = int(resolve(context, step.get("timeout") or step.get("timeout_ms") or 10000) or 10000)
        result = base_result(step, action="dubbo")
        result["url"] = url
        started = time.monotonic()
        try:
            transport = getattr(context, "http_session", None) or requests
            payload = {"service": service, "method": method, "paramsTypes": [], "params": params}
            response = transport.post(
                url,
                json=payload,
                timeout=max(0.1, timeout_ms / 1000),
            )
            result["status"] = response.status_code
            try:
                result["body"] = response.json()
            except ValueError:
                result["body"] = response.text
            result["text"] = response.text
            result["url"] = response.url
            if response.status_code >= 400:
                raise AssertionError(f"Dubbo 网关返回 {response.status_code}")
        except Exception as exc:
            result.setdefault("status", "error")
            attach_runtime_result(exc, result)
            raise
        finally:
            result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
        return result


register_driver(GrpcDriver())
register_driver(DubboDriver())
