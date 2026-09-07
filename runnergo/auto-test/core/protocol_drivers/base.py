"""商业协议库驱动框架。

每个驱动声明一组 action 名，实现 ``execute(step, context)`` 返回与
``execute_api_step`` 同形状的结果 dict，以便复用压测的 ``record_iteration``、
业务事务、Analysis 导出。可选连接对象挂在 ``context.protocol_sessions[driver]``
上，按 VU 复用（对齐 ``http_session`` 的 ``connection_reuse`` 语义）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.url_security import validate_outbound_url

_DRIVERS: List["ProtocolDriver"] = []
_REGISTRY: Dict[str, "ProtocolDriver"] = {}


class ProtocolDriver:
    """协议驱动基类。子类设置 ``name`` / ``actions``，实现 ``execute``。"""

    name: str = ""
    actions: set[str] = set()
    optional_dependency: str = ""

    def execute(self, step: Dict[str, Any], context: Any) -> Dict[str, Any]:
        raise NotImplementedError

    def acquire(self, context: Any) -> Any:
        return None

    def release(self, context: Any) -> None:
        return None


def register_driver(driver: ProtocolDriver) -> ProtocolDriver:
    if not driver.name or not driver.actions:
        raise ValueError("协议驱动必须提供 name 与 actions")
    _DRIVERS.append(driver)
    for action in driver.actions:
        _REGISTRY[action] = driver
    return driver


def driver_for(action: str) -> Optional[ProtocolDriver]:
    return _REGISTRY.get(action)


def all_protocol_actions() -> set[str]:
    return set(_REGISTRY.keys())


def protocol_capabilities() -> List[Dict[str, Any]]:
    return [
        {
            "name": driver.name,
            "actions": sorted(driver.actions),
            "optional_dependency": driver.optional_dependency,
        }
        for driver in _DRIVERS
    ]


def protocol_sessions(context: Any) -> Dict[str, Any]:
    pool = getattr(context, "protocol_sessions", None)
    if not isinstance(pool, dict):
        pool = {}
        try:
            context.protocol_sessions = pool
        except Exception:
            pass
    return pool


def resolve(context: Any, value: Any) -> Any:
    resolver = getattr(context, "resolve", None)
    if callable(resolver):
        try:
            return resolver(value)
        except Exception:
            return value
    return value


def safe_url(context: Any, raw_url: str) -> str:
    resolved = resolve(context, raw_url)
    return validate_outbound_url(str(resolved))


def require_dependency(dep: str, install_hint: str = "") -> None:
    hint = install_hint or f"pip install {dep}"
    raise RuntimeError(f"协议驱动 {dep!r} 未安装，请安装可选依赖后重试：{hint}")


def base_result(step: Dict[str, Any], *, action: str) -> Dict[str, Any]:
    """对齐 execute_api_step 的结果形状，便于压测结果上报与导出。"""
    return {
        "action": action,
        "name": str(step.get("name") or action),
        "status": "ok",
        "url": "",
        "body": None,
        "text": "",
        "extracted": {},
        "scripts": {},
        "duration_ms": 0.0,
    }


def attach_runtime_result(exc: BaseException, result: Dict[str, Any]) -> BaseException:
    """把结果挂到异常上，供 execute_step 的失败分支提取 runtime_result。"""
    setattr(exc, "runtime_result", result)
    return exc
