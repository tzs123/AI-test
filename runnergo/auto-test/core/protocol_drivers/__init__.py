"""商业协议库：可扩展协议驱动框架。

导入各驱动模块即完成注册。``PROTOCOL_ACTIONS`` 合并到
``core.web_flow_runner.NON_BROWSER_ACTIONS``，``execute_step`` 通过
``driver_for(action)`` 分发到对应驱动。第三方依赖缺失时驱动抛清晰提示，
不影响平台启动与其余协议。
"""
from __future__ import annotations

from .base import (
    all_protocol_actions,
    driver_for,
    protocol_capabilities,
    register_driver,
)

# 导入各驱动模块 —— 模块末尾的 register_driver(...) 完成注册。
from . import messaging as _messaging  # noqa: F401
from . import rpc as _rpc  # noqa: F401
from . import tcp_socket as _tcp_socket  # noqa: F401
from . import transfer as _transfer  # noqa: F401

PROTOCOL_ACTIONS: set[str] = all_protocol_actions()
PROTOCOL_CAPABILITIES = protocol_capabilities()

__all__ = [
    "PROTOCOL_ACTIONS",
    "PROTOCOL_CAPABILITIES",
    "driver_for",
    "register_driver",
]
