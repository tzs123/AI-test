"""商业协议库与 JVM/DB 深度监控的单元测试。

不依赖真实 broker / 邮件 / 目录服务：用 monkeypatch 替换 socket / http 会话 /
可选依赖，验证驱动注册、结果形状、SSRF 拒绝、连接复用、可选依赖缺失提示，
以及监控字段扩展后 _resource_summary 的聚合。
"""
from __future__ import annotations

import socket
import sys
import types
from typing import Any, Dict

import pytest

from backend import load_test as load_test_module
from backend.url_security import validate_outbound_endpoint
from core import protocol_drivers
from core.protocol_drivers import base as driver_base
from core.protocol_drivers.tcp_socket import TcpSocketDriver


# ---------------------------------------------------------------------------
# 注册与查找
# ---------------------------------------------------------------------------
EXPECTED_ACTIONS = {
    "tcp", "socket", "websocket", "ws",
    "kafka", "rabbitmq", "amqp",
    "grpc", "dubbo",
    "ftp", "sftp", "smtp", "imap", "pop3", "ldap",
}


def test_protocol_actions_registered():
    assert EXPECTED_ACTIONS <= protocol_drivers.PROTOCOL_ACTIONS


def test_driver_for_returns_driver_and_none_for_unknown():
    for action in EXPECTED_ACTIONS:
        driver = protocol_drivers.driver_for(action)
        assert driver is not None
        assert action in driver.actions
    assert protocol_drivers.driver_for("nope-not-real") is None


def test_protocol_capabilities_advertises_all_drivers():
    names = {cap["name"] for cap in protocol_drivers.PROTOCOL_CAPABILITIES}
    assert {"kafka", "rabbitmq", "grpc", "dubbo", "tcp_socket",
            "websocket", "ftp", "sftp", "smtp", "imap", "pop3", "ldap"} <= names
    for cap in protocol_drivers.PROTOCOL_CAPABILITIES:
        assert cap["actions"] and cap["optional_dependency"] is not None or cap["name"] in {
            "tcp_socket", "dubbo", "ftp", "smtp", "imap", "pop3"
        }


# ---------------------------------------------------------------------------
# 结果形状
# ---------------------------------------------------------------------------
def test_base_result_shape_matches_api_contract():
    result = driver_base.base_result({"name": "x"}, action="tcp")
    for key in ("action", "name", "status", "url", "body", "text",
                "extracted", "scripts", "duration_ms"):
        assert key in result
    assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# TCP 驱动：mock socket + 连接复用 + SSRF 拒绝
# ---------------------------------------------------------------------------
class _FakeSocket:
    def __init__(self) -> None:
        self.sent = b""
        self.recv_data = b"PONG"

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def settimeout(self, value: Any) -> None:
        pass

    def recv(self, size: int) -> bytes:
        return self.recv_data


class _FakeContext:
    def __init__(self) -> None:
        self.http_session = None
        self.protocol_sessions: Dict[str, Any] = {}

    def resolve(self, value: Any) -> Any:
        return value


def test_tcp_driver_sends_payload_returns_response_and_reuses_connection(monkeypatch):
    fake = _FakeSocket()
    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout=None: fake)
    driver = TcpSocketDriver()
    ctx = _FakeContext()
    step = {
        "action": "tcp",
        "host": "example.test",
        "port": 1234,
        "data": "PING",
        "encoding": "text",
        "receive_bytes": 64,
    }
    result = driver.execute(step, ctx)
    assert result["status"] == "ok"
    assert result["url"] == "tcp://example.test:1234"
    assert result["text"] == "PONG"
    assert result["body"] == b"PONG".hex()
    assert result["duration_ms"] >= 0
    # 第二次执行复用同一 socket（连接池命中）
    driver.execute(step, ctx)
    assert ctx.protocol_sessions.get(("tcp", "example.test", 1234)) is fake


def test_tcp_driver_rejects_empty_host():
    driver = TcpSocketDriver()
    ctx = _FakeContext()
    with pytest.raises(ValueError, match="主机不能为空"):
        driver.execute({"action": "tcp", "host": "", "port": 1234, "data": "x"}, ctx)


def test_validate_outbound_endpoint_rejects_bad_port():
    with pytest.raises(ValueError, match="端口"):
        validate_outbound_endpoint("example.test", 0)
    with pytest.raises(ValueError, match="端口"):
        validate_outbound_endpoint("example.test", 70000)


# ---------------------------------------------------------------------------
# Dubbo 驱动：mock http 会话
# ---------------------------------------------------------------------------
class _FakeHttpResponse:
    def __init__(self, status_code: int, payload: Any, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or str(payload)
        self.url = "http://example.test/dubbo"

    def json(self) -> Any:
        return self._payload


class _FakeHttpSession:
    def __init__(self, response: _FakeHttpResponse) -> None:
        self.response = response
        self.last_kwargs: Dict[str, Any] = {}

    def post(self, url, **kwargs) -> _FakeHttpResponse:
        self.last_kwargs = kwargs
        return self.response


def test_dubbo_driver_posts_to_gateway_and_returns_body():
    from core.protocol_drivers.rpc import DubboDriver

    ctx = _FakeContext()
    ctx.http_session = _FakeHttpSession(_FakeHttpResponse(200, {"code": 0, "data": {"id": 1}}))
    result = DubboDriver().execute({
        "action": "dubbo",
        "url": "http://example.test/dubbo",
        "service": "org.demo.UserService",
        "method": "getUser",
        "params": [{"id": 1}],
    }, ctx)
    assert result["status"] == 200
    assert result["body"] == {"code": 0, "data": {"id": 1}}
    assert ctx.http_session.last_kwargs["json"]["method"] == "getUser"


def test_dubbo_driver_raises_on_gateway_error():
    from core.protocol_drivers.rpc import DubboDriver

    ctx = _FakeContext()
    ctx.http_session = _FakeHttpSession(_FakeHttpResponse(500, {"err": "boom"}))
    with pytest.raises(AssertionError, match="500"):
        DubboDriver().execute({
            "action": "dubbo",
            "url": "http://example.test/dubbo",
            "service": "s", "method": "m",
        }, ctx)


# ---------------------------------------------------------------------------
# 可选依赖缺失提示
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("module_name", ["kafka", "pika", "grpc", "paramiko", "ldap3"])
def test_optional_drivers_raise_install_hint_when_missing(module_name, monkeypatch):
    monkeypatch.setitem(sys.modules, module_name, None)
    ctx = _FakeContext()
    action_map = {
        "kafka": ("kafka", {"action": "kafka", "brokers": "example.test:9092", "topic": "t"}),
        "pika": ("rabbitmq", {"action": "rabbitmq", "url": "amqp://example.test:5672", "queue": "q"}),
        "grpc": ("grpc", {"action": "grpc", "target": "example.test:50051",
                          "method": "/s/m", "request": "", "encoding": "text"}),
        "paramiko": ("sftp", {"action": "sftp", "host": "example.test", "port": 22, "op": "list"}),
        "ldap3": ("ldap", {"action": "ldap", "url": "ldap://example.test:389",
                          "base": "dc=x", "filter": "(objectClass=*)"}),
    }
    action, step = action_map[module_name]
    driver = protocol_drivers.driver_for(action)
    with pytest.raises(RuntimeError, match="pip install"):
        driver.execute(step, ctx)


# ---------------------------------------------------------------------------
# JVM/DB 深度监控字段扩展
# ---------------------------------------------------------------------------
DEEP_JVM_FIELDS = {
    "jvm_heap_eden_mb", "jvm_heap_survivor_mb", "jvm_heap_old_mb",
    "jvm_gc_marksweep_count", "jvm_gc_marksweep_time_ms",
    "jvm_gc_scavenge_count", "jvm_gc_scavenge_time_ms",
    "jvm_threads_blocked", "jvm_threads_waiting", "jvm_classes_loaded",
}
DEEP_DB_FIELDS = {
    "db_active_connections", "db_pool_max", "db_pool_wait_ms",
    "db_slow_query_samples", "db_replication_lag_seconds",
    "db_table_locks", "db_innodb_row_ops_per_sec",
}


def test_deep_monitoring_fields_present():
    fields = set(load_test_module.EXTERNAL_MONITOR_FIELDS.keys())
    assert DEEP_JVM_FIELDS <= fields
    assert DEEP_DB_FIELDS <= fields


def test_normalize_monitoring_config_accepts_deep_fields():
    normalized = load_test_module._normalize_monitoring_config({
        "prometheus_url": "http://example.test:9090",
        "queries": {
            "jvm_heap_old_mb": "jvm_memory_bytes_used",
            "db_replication_lag_seconds": "mysql_slave_status_seconds_behind_master",
        },
    })
    assert normalized["enabled"] is True
    assert "jvm_heap_old_mb" in normalized["queries"]
    assert "db_replication_lag_seconds" in normalized["queries"]


def test_resource_summary_aggregates_deep_monitoring_fields():
    samples = [
        {"availability": {"jvm": True, "database": True, "cpu": True, "memory": True, "network": True},
         "jvm_heap_old_mb": 100.0, "db_replication_lag_seconds": 2.0},
        {"availability": {"jvm": True, "database": True, "cpu": True, "memory": True, "network": True},
         "jvm_heap_old_mb": 200.0, "db_replication_lag_seconds": 4.0},
    ]
    summary = load_test_module._resource_summary(samples, include_agents=False)
    assert summary["availability"]["jvm"] is True
    assert summary["availability"]["database"] is True
    assert summary["metrics"]["jvm_heap_old_mb"]["avg"] == 150
    assert summary["metrics"]["jvm_heap_old_mb"]["peak"] == 200
    assert summary["metrics"]["db_replication_lag_seconds"]["avg"] == 3


def test_capabilities_advertises_protocols_and_monitoring_fields():
    # PROTOCOL_CAPABILITIES 由协议库导出，监控字段由 load_test 导出，二者都在 /capabilities 返回
    proto_names = {cap["name"] for cap in protocol_drivers.PROTOCOL_CAPABILITIES}
    assert {"kafka", "grpc", "ftp", "smtp", "ldap"} <= proto_names
    assert "jvm_heap_old_mb" in load_test_module.EXTERNAL_MONITOR_FIELDS
    assert "db_replication_lag_seconds" in load_test_module.EXTERNAL_MONITOR_FIELDS
