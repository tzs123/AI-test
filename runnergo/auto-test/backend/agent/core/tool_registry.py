"""统一工具接口与工具注册表（Tool Registry）。

所有 Agent 工具统一实现：
    name         工具名
    description  工具说明
    parameters   参数列表
    execute()    执行入口

已注册工具：
    generate_test_data  测试数据生成（复用测试数据中心）
    generate_test_case  AI 用例生成
    run_api_test        API 测试
    run_web_test        UI 自动化
    run_app_test        APP 自动化
    run_security_test   安全测试
    run_browser_agent   浏览器视觉定位测试
    analyze_failure     AI 失败分析
    generate_from_api   接口数据生成（Swagger/OpenAPI）
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from backend.agent import tools as legacy_tools


class Tool:
    """统一工具接口。"""

    def __init__(
        self,
        name: str,
        description: str,
        handler: Callable[..., dict],
        parameters: Optional[list[str]] = None,
        category: str = "test",
    ) -> None:
        self.name = name
        self.description = description
        self.handler = handler
        self.parameters = list(parameters or [])
        self.category = category

    def execute(self, **kwargs: Any) -> dict:
        """调用工具，任何异常都结构化为 failed 结果。"""
        try:
            result = self.handler(**kwargs)
            if isinstance(result, dict):
                if "status" not in result:
                    result = dict(result)
                    result["status"] = "success"
                return result
            return {"status": "success", "result": result}
        except Exception as exc:  # noqa: BLE001 - 工具必须结构化返回错误
            return {"status": "failed", "error": f"{self.name} 执行异常: {exc}"}

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": list(self.parameters),
            "category": self.category,
        }


def _legacy_handler(name: str) -> Callable[..., dict]:
    def handler(**kwargs: Any) -> dict:
        return legacy_tools.TOOLS[name]["handler"](**kwargs)
    return handler


def _security_handler(**kwargs: Any) -> dict:
    from backend.security_agent import scan
    return scan(**kwargs)


def _analyze_failure_handler(**kwargs: Any) -> dict:
    from backend.agent.analyzer import analyze_failure
    return analyze_failure(**kwargs)


def _browser_handler(**kwargs: Any) -> dict:
    from backend.browser_agent import run_browser_agent
    return run_browser_agent(**kwargs)


def _generate_from_api_handler(**kwargs: Any) -> dict:
    from backend.data_agent import generate_from_api
    return generate_from_api(**kwargs)


def _build_registry() -> dict[str, Tool]:
    registry: dict[str, Tool] = {}
    specs = [
        Tool(
            "generate_test_data",
            "测试数据生成：按类型与字段生成用户/订单/商品等测试数据，返回 asset_id",
            legacy_tools.generate_test_data,
            ["asset_type", "count", "fields", "project_id"],
            "data",
        ),
        Tool(
            "generate_test_case",
            "AI 用例生成：输入业务需求，输出功能/异常/边界/安全/性能测试用例",
            legacy_tools.create_case,
            ["requirement", "dimension_plan", "project_id"],
            "case",
        ),
        Tool(
            "run_api_test",
            "执行 API 测试：method/url/base_url/headers/body/timeout，带 SSRF 校验",
            legacy_tools.run_api_test,
            ["method", "url", "base_url", "headers", "body", "timeout"],
            "test",
        ),
        Tool(
            "run_web_test",
            "执行 UI 自动化：复用现有项目用例与分布式执行器",
            _legacy_handler("run_ui_test"),
            ["project_id", "case_files", "env", "base_url", "data_bindings"],
            "test",
        ),
        Tool(
            "run_app_test",
            "执行 APP 自动化：由 testhub 平台承载",
            _legacy_handler("run_app_test"),
            ["project_id", "case_ids", "device_id"],
            "test",
        ),
        Tool(
            "run_security_test",
            "安全测试：接口/参数识别、Payload 生成与漏洞验证（SQL注入/XSS/越权/JWT/文件上传/SSRF）",
            _security_handler,
            ["api_doc", "target", "parameters"],
            "security",
        ),
        Tool(
            "run_browser_agent",
            "浏览器视觉定位：打开页面->截图->识别元素->执行点击/输入，不依赖固定 XPath",
            _browser_handler,
            ["url", "goal", "project_id"],
            "test",
        ),
        Tool(
            "analyze_failure",
            "AI 失败分析：输入日志/截图/接口响应，输出 error_type/reason/solution",
            _analyze_failure_handler,
            ["logs", "screenshots", "api_response", "context"],
            "analysis",
        ),
        Tool(
            "generate_from_api",
            "接口测试数据生成：解析 Swagger/OpenAPI 自动生成 normal/boundary/security 数据",
            _generate_from_api_handler,
            ["swagger_url", "swagger_doc", "api", "mode"],
            "data",
        ),
    ]
    for tool in specs:
        registry[tool.name] = tool
    if "create_report" in legacy_tools.TOOLS and "create_report" not in registry:
        registry["create_report"] = Tool(
            "create_report",
            "生成测试报告：汇总执行结果并输出 Markdown/JSON 报告",
            _legacy_handler("create_report"),
            ["agent_task_id", "title", "results"],
            "report",
        )
    return registry


_REGISTRY = _build_registry()


def register_tool(tool: Tool) -> None:
    """动态注册工具（覆盖同名工具）。"""
    _REGISTRY[tool.name] = tool


def get_tool(name: str) -> Optional[Tool]:
    return _REGISTRY.get(name)


def list_tools() -> list[dict]:
    """返回工具注册表（不含 handler，供 API/前端展示）。"""
    return [tool.to_dict() for tool in _REGISTRY.values()]


def execute_tool(
    name: str,
    *,
    agent_task_id: str = "",
    step_id: int = 0,
    logs: str = "",
    **kwargs: Any,
) -> dict:
    """统一执行入口：所有 ReAct 工具用新名称执行并记录结果。"""
    tool = _REGISTRY.get(name)
    if not tool:
        return {"status": "failed", "error": f"未知工具: {name}"}
    target = str(kwargs.get("target") or kwargs.get("url") or kwargs.get("requirement") or kwargs.get("task_id") or name)
    result = tool.execute(**kwargs)
    if agent_task_id:
        try:
            legacy_tools.record_tool_result(
                agent_task_id=agent_task_id,
                step_id=step_id,
                tool=name,
                target=target,
                result=result,
                logs=logs,
            )
            from backend.agent.memory import remember
            remember(agent_task_id, f"last_tool_result:{name}", result)
        except Exception:  # noqa: BLE001 - 记录失败不影响工具结果
            pass
    return result
