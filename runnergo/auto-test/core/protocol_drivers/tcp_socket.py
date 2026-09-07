"""TCP/Socket 与 WebSocket 协议驱动。

YAML 步骤示例::

    - id: send
      action: tcp
      host: 127.0.0.1
      port: 6379
      data: "PING\\r\\n"
      encoding: text          # text / hex / base64
      receive_bytes: 64
      timeout_ms: 5000
    - id: ws
      action: websocket
      url: ws://127.0.0.1:8080/echo
      message: hello
      op: send                # send / receive / close
      timeout_ms: 5000
"""
from __future__ import annotations

import base64
import json
import socket
import time
from typing import Any, Dict

from backend.url_security import validate_outbound_endpoint, validate_outbound_hostport_url
from .base import (
    attach_runtime_result,
    base_result,
    protocol_sessions,
    register_driver,
    require_dependency,
    resolve,
)
from .base import ProtocolDriver


def _payload_bytes(step: Dict[str, Any], context: Any) -> bytes:
    data = resolve(context, step.get("data") or step.get("payload") or step.get("message") or "")
    if isinstance(data, (dict, list)):
        data = json.dumps(data, ensure_ascii=False)
    encoding = str(resolve(context, step.get("encoding") or "text")).lower()
    text = str(data) if data is not None else ""
    if encoding == "hex":
        return bytes.fromhex(text)
    if encoding == "base64":
        return base64.b64decode(text)
    return text.encode("utf-8")


class TcpSocketDriver(ProtocolDriver):
    name = "tcp_socket"
    actions = {"tcp", "socket"}

    def execute(self, step: Dict[str, Any], context: Any) -> Dict[str, Any]:
        host = str(resolve(context, step.get("host") or ""))
        port = int(resolve(context, step.get("port") or 0) or 0)
        host, port = validate_outbound_endpoint(host, port)
        timeout_ms = int(resolve(context, step.get("timeout") or step.get("timeout_ms") or 10000) or 10000)
        payload = _payload_bytes(step, context)
        receive_bytes = int(resolve(context, step.get("receive_bytes") or 4096) or 4096)
        result = base_result(step, action="tcp")
        result["url"] = f"tcp://{host}:{port}"
        started = time.monotonic()
        sock = None
        try:
            pool = protocol_sessions(context)
            key = ("tcp", host, port)
            sock = pool.get(key)
            if sock is None:
                sock = socket.create_connection((host, port), timeout=max(0.1, timeout_ms / 1000))
                pool[key] = sock
            if payload:
                sock.sendall(payload)
            data = b""
            if step.get("receive", True) not in (False, 0, "0", "false"):
                sock.settimeout(max(0.1, timeout_ms / 1000))
                data = sock.recv(receive_bytes)
            result["body"] = data.hex()
            result["text"] = data.decode("utf-8", "replace")
            result["status"] = "ok"
        except Exception as exc:
            result["status"] = "error"
            result["body"] = str(exc)
            attach_runtime_result(exc, result)
            raise
        finally:
            result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
        return result


class WebSocketDriver(ProtocolDriver):
    name = "websocket"
    actions = {"websocket", "ws"}
    optional_dependency = "websocket-client (pip install websocket-client)"

    def execute(self, step: Dict[str, Any], context: Any) -> Dict[str, Any]:
        try:
            import websocket  # websocket-client
        except ImportError:
            require_dependency("websocket-client", "pip install websocket-client")
        raw_url = str(resolve(context, step.get("url") or ""))
        scheme, host, port, path, _query = validate_outbound_hostport_url(raw_url)
        timeout_ms = int(resolve(context, step.get("timeout") or step.get("timeout_ms") or 10000) or 10000)
        op = str(resolve(context, step.get("op") or "send")).lower()
        result = base_result(step, action="websocket")
        result["url"] = raw_url
        started = time.monotonic()
        try:
            pool = protocol_sessions(context)
            key = ("ws", raw_url)
            conn = pool.get(key)
            if conn is None and op != "close":
                conn = websocket.create_connection(raw_url, timeout=max(0.1, timeout_ms / 1000))
                pool[key] = conn
            if op == "close":
                if conn is not None:
                    conn.close()
                    pool.pop(key, None)
                result["status"] = "ok"
            elif op == "receive":
                data = conn.recv() if conn is not None else ""
                result["text"] = data if isinstance(data, str) else str(data)
                result["status"] = "ok"
            else:
                message = resolve(context, step.get("message") or step.get("data") or "")
                if isinstance(message, (dict, list)):
                    message = json.dumps(message, ensure_ascii=False)
                if conn is not None:
                    conn.send(str(message))
                result["status"] = "ok"
        except Exception as exc:
            result["status"] = "error"
            result["body"] = str(exc)
            attach_runtime_result(exc, result)
            raise
        finally:
            result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
        return result


register_driver(TcpSocketDriver())
register_driver(WebSocketDriver())
