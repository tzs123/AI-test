from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict
from urllib.parse import urlsplit
from django.utils import timezone

from apps.agent.models import AgentStep, AgentTask
from backend.agent import tools as legacy_tools


ToolHandler = Callable[[AgentTask, AgentStep, Dict[str, Any]], Dict[str, Any]]


@dataclass
class Tool:
    """Unified Agent tool contract."""

    name: str
    description: str
    handler: ToolHandler
    category: str = 'test'

    def execute(self, task: AgentTask, step: AgentStep, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
        result = self.handler(task, step, payload or {})
        if not isinstance(result, dict):
            return {'status': 'success', 'result': result}
        if 'status' not in result:
            result = {**result, 'status': 'success'}
        return result

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'description': self.description,
            'category': self.category,
        }


class ToolRegistry:
    """Registry for all AI Software Testing Agent tools."""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list(self) -> list[Dict[str, Any]]:
        seen = set()
        catalog = []
        for tool in self._tools.values():
            if tool.name in seen:
                continue
            seen.add(tool.name)
            catalog.append(tool.to_dict())
        return catalog

    def execute(self, name: str, task: AgentTask, step: AgentStep, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
        tool = self.get(name)
        if not tool:
            raise RuntimeError(f'未注册工具：{name}')
        return tool.execute(task, step, payload or {})


def _run_security_test(task: AgentTask, step: AgentStep, payload: Dict[str, Any]) -> Dict[str, Any]:
    context = task.context if isinstance(task.context, dict) else {}
    target = str(payload.get('target') or context.get('api_url') or '').strip()
    api_doc = payload.get('api_doc') or context.get('api_doc') or context.get('swagger_url')
    api_refs = (
        context.get('api_test_objects')
        or context.get('test_object_refs')
        or context.get('test_object_ref')
    )
    target_path = urlsplit(target).path.lower() if target else ''
    looks_like_api = bool(
        api_doc
        or api_refs
        or context.get('api_url')
        or '/api/' in target_path
        or target_path.rstrip('/').endswith(('/api', '/openapi', '/swagger'))
    )
    if not looks_like_api:
        return {
            'status': 'not_applicable',
            'message': '当前只有前端页面 URL，没有 API 地址、OpenAPI 文档或 API 测试对象，安全接口扫描不适用。',
            'real_execution': False,
        }

    from backend.security_agent.scanner import SecurityAgent

    started = timezone.now()
    result = SecurityAgent().scan(**payload)
    status = result.get('status') or 'success'
    high_risk = result.get('risk') == 'HIGH'
    result = {**result, 'status': 'failed' if high_risk else status}
    if status != 'skipped':
        execution_status = 'FAILED' if high_risk or result.get('status') == 'failed' else 'PASSED'
        execution = legacy_tools._persist_execution_result(
            task,
            step,
            'security',
            payload,
            execution_status,
            result.get('message') or 'AI Security Agent 扫描完成',
            result,
            started,
        )
        result['execution_result_id'] = execution.id
        result['real_execution'] = True
    return result


def _run_web_test(task: AgentTask, step: AgentStep, payload: Dict[str, Any]) -> Dict[str, Any]:
    return legacy_tools.run_ui_test(task, step, payload)


def _run_browser_agent(task: AgentTask, step: AgentStep, payload: Dict[str, Any]) -> Dict[str, Any]:
    if payload.get('url'):
        from backend.browser_agent.browser_controller import WebVisionAgent

        started = timezone.now()
        analyze_only = str(payload.get('mode') or '').lower() in {'understand_system', 'analyze_only'}
        result = WebVisionAgent().run(
            url=payload.get('url'),
            goal=payload.get('goal') or task.user_requirement,
            analyze_only=analyze_only,
        )
        execution_status = 'PASSED' if result.get('status') == 'success' else 'FAILED'
        evidence_result = {
            **result,
            'real_execution': False,
            'evidence_type': 'page_exploration',
        }
        execution = legacy_tools._persist_execution_result(
            task,
            step,
            'browser',
            payload,
            execution_status,
            result.get('message') or 'Browser Vision Agent 执行完成',
            evidence_result,
            started,
            screenshot=result.get('screenshot') or '',
        )
        return {
            **evidence_result,
            'execution_result_id': execution.id,
        }
    return {
        'status': 'not_applicable',
        'message': '当前任务已绑定 UI YAML 用例，但未提供独立页面 URL，额外的浏览器探索步骤不适用。',
        'architecture': 'AI -> Playwright MCP -> Chrome -> 页面 -> 截图 -> 视觉模型分析',
    }


def _generate_from_api(task: AgentTask, step: AgentStep, payload: Dict[str, Any]) -> Dict[str, Any]:
    from backend.data_agent.data_generator import DataAgent

    result = DataAgent().generate_from_api(**payload)
    return {
        'status': 'success',
        **result,
    }


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Tool(
        'analyze_requirement',
        'AI 分析中文业务需求、前端 URL、接口 URL、APP 上下文并作出执行决策',
        legacy_tools.analyze_requirement,
        category='analysis',
    ))
    registry.register(Tool(
        'discover_test_points',
        'AI 发现功能、异常、边界、安全、性能测试点',
        legacy_tools.discover_test_points,
        category='case',
    ))
    registry.register(Tool(
        'generate_test_data',
        'AI Test Data Generator 生成正常/异常/边界/安全测试数据资产',
        legacy_tools.generate_test_data,
        category='data',
    ))
    registry.register(Tool(
        'generate_test_case',
        '调用 AI 用例生成能力生成平台测试用例',
        legacy_tools.create_case,
        category='case',
    ))
    registry.register(Tool(
        'run_api_test',
        '调用 API 测试执行器或登记 API 测试结果',
        legacy_tools.run_api_test,
        category='api',
    ))
    registry.register(Tool(
        'run_web_test',
        '调用 UI 自动化执行器执行 Web 用例',
        _run_web_test,
        category='web',
    ))
    registry.register(Tool(
        'run_browser_agent',
        '通过 Playwright MCP 驱动 Chrome，截图并进行视觉模型分析',
        _run_browser_agent,
        category='browser',
    ))
    registry.register(Tool(
        'run_app_test',
        '调用 APP 自动化执行器',
        legacy_tools.run_app_test,
        category='app',
    ))
    registry.register(Tool(
        'run_performance_test',
        '调用性能测试执行器或登记性能测试结果',
        legacy_tools.run_performance_test,
        category='performance',
    ))
    registry.register(Tool(
        'run_security_test',
        '调用 AI Security Agent/Yakit 兼容流程进行 SQL 注入、越权、JWT、敏感信息泄露扫描',
        _run_security_test,
        category='security',
    ))
    registry.register(Tool(
        'analyze_failure',
        '调用 Failure Analyzer 分析日志、截图、接口响应和异常',
        legacy_tools.analyze_failure,
        category='analysis',
    ))
    registry.register(Tool(
        'self_heal_test',
        'AI 修复测试：生成定位器、数据和步骤自愈建议',
        legacy_tools.self_heal_test,
        category='analysis',
    ))
    registry.register(Tool(
        'rerun_fixed_test',
        '再次执行修复后的测试链路并登记复跑证据',
        legacy_tools.rerun_fixed_test,
        category='execution',
    ))
    registry.register(Tool(
        'create_report',
        '生成 AI Test Agent 测试报告',
        legacy_tools.create_report,
        category='report',
    ))
    registry.register(Tool(
        'generate_from_api',
        'AI Test Data Generator 解析 Swagger/OpenAPI 并生成 normal/abnormal/boundary/security 测试数据',
        _generate_from_api,
        category='data',
    ))
    return registry


default_registry = build_default_registry()
