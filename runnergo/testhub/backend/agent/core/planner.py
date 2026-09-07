from __future__ import annotations

import re
from typing import Any, Dict

from backend.agent.planner import REACT_ARCHITECTURE, TestPlanner
from backend.agent.url_utils import infer_http_url


class ReActPlanner:
    """Builds Agent Planner -> Action -> Observation -> Reflection -> Retry plans."""

    def __init__(self, planner: TestPlanner | None = None):
        self.planner = planner or TestPlanner()

    def plan(self, goal: str, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
        context = context or {}
        base = self.planner.plan(goal)
        url = self._infer_url(goal, context)
        api_doc = context.get('api_doc') or {}
        api_url = context.get('api_url') or (url if self._looks_like_api(goal, url) else '')
        frontend_url = context.get('frontend_url') or context.get('base_url') or (url if not api_url else '')
        test_data_count = max(1, min(int(context.get('test_data_count') or 3), 20))
        has_api_target = bool(
            api_url
            or api_doc
            or context.get('swagger_url')
            or context.get('api_test_objects')
            or context.get('test_object_refs')
            or context.get('test_object_ref')
        )
        has_ui_target = bool(
            frontend_url
            or context.get('ui_case_ids')
            or context.get('ui_test_case_ids')
            or context.get('ui_case_files')
            or context.get('case_files')
        )
        has_browser_target = bool(frontend_url or url)
        has_app_target = bool(
            context.get('app_case_ids')
            or context.get('app_test_case_ids')
            or context.get('app_test_object_refs')
            or (
                (context.get('app_project') or context.get('app_project_id'))
                and (context.get('device') or context.get('device_id'))
            )
        )
        has_performance_target = any(context.get(key) for key in (
            'performance_plan_id',
            'performance_plan_ids',
            'performance_scene_id',
            'performance_scene_ids',
            'performance_test_ids',
        ))
        scoped_tools = {
            'generate_from_api': has_api_target,
            'run_api_test': has_api_target,
            'run_web_test': has_ui_target,
            'run_browser_agent': has_browser_target,
            'run_app_test': has_app_target,
            'run_performance_test': has_performance_target,
            'run_security_test': has_api_target,
        }
        steps = []
        for index, item in enumerate(base.get('steps') or [], start=1):
            tool = item.get('tool') or 'generate_test_case'
            if tool in scoped_tools and not scoped_tools[tool]:
                continue
            payload = item.get('input') if isinstance(item.get('input'), dict) else {}
            if tool in {'run_web_test', 'run_browser_agent'} and url:
                if not payload.get('url'):
                    payload['url'] = frontend_url or url
                if not payload.get('goal'):
                    payload['goal'] = goal
            if tool == 'run_security_test':
                if not payload.get('target'):
                    payload['target'] = api_url or url
                if not payload.get('api_doc'):
                    payload['api_doc'] = api_doc or ''
            if tool == 'generate_from_api':
                if not payload.get('swagger_url'):
                    payload['swagger_url'] = context.get('swagger_url', '')
                if not payload.get('swagger_doc'):
                    payload['swagger_doc'] = api_doc
                if not payload.get('api'):
                    payload['api'] = context.get('api') or api_url or ''
                if not payload.get('mode'):
                    payload['mode'] = ['normal', 'boundary', 'security']
            if tool == 'run_api_test':
                for key in ('api_test_objects', 'test_object_refs', 'test_object_ref', 'team_id', 'target_id'):
                    if context.get(key):
                        payload.setdefault(key, context[key])
                if api_url and not context.get('api_test_objects') and not context.get('test_object_refs'):
                    payload.setdefault('url', api_url)
                    payload.setdefault('allow_direct_api', True)
            if tool == 'generate_test_data':
                payload['count'] = test_data_count
            if tool == 'run_app_test':
                for key in ('app_project', 'app_project_id', 'device', 'device_id', 'app_package', 'app_package_id', 'app_case_ids', 'app_test_case_ids', 'test_data_count'):
                    if context.get(key):
                        payload.setdefault(key, context[key])
            if tool == 'run_web_test':
                for key in ('ui_case_ids', 'ui_test_case_ids', 'ui_case_names', 'ui_test_case_names'):
                    if context.get(key):
                        payload.setdefault(key, context[key])
            steps.append({
                'step': int(item.get('step') or index),
                'type': item.get('type') or 'case',
                'action': item.get('action') or '执行测试动作',
                'tool': tool,
                'input': payload,
            })
        singleton_tools = {'analyze_requirement', 'run_browser_agent'}
        seen_singletons = set()
        deduplicated_steps = []
        for item in steps:
            tool = item['tool']
            if tool in singleton_tools:
                if tool in seen_singletons:
                    continue
                seen_singletons.add(tool)
            deduplicated_steps.append(item)
        steps = deduplicated_steps
        tools = {step['tool'] for step in steps}
        if 'analyze_requirement' not in tools:
            steps.insert(0, {
                'step': 0,
                'type': 'analysis',
                'action': 'AI 分析需求：识别中文业务目标、前端 URL、接口 URL、APP 场景和测试范围',
                'tool': 'analyze_requirement',
                'input': {'requirement': goal, 'url': url, 'api_doc': api_doc},
            })
            tools.add('analyze_requirement')
        if 'generate_test_case' not in tools:
            steps.insert(0, {
                'step': 0,
                'type': 'case',
                'action': '生成测试方案并发现功能、异常、边界、安全和性能测试点',
                'tool': 'generate_test_case',
                'input': {'requirement': goal},
            })
            tools.add('generate_test_case')
        if 'discover_test_points' not in tools:
            insert_at = self._after_tool_index(steps, 'generate_test_case', default=1)
            steps.insert(insert_at, {
                'step': 0,
                'type': 'case',
                'action': 'AI 发现测试点：抽取主流程、异常流、边界值、安全风险和质量门禁',
                'tool': 'discover_test_points',
                'input': {'requirement': goal},
            })
            tools.add('discover_test_points')
        if has_api_target and not any(step['tool'] == 'run_security_test' for step in steps):
            insert_at = max(len(steps) - 2, 0)
            steps.insert(insert_at, {
                'step': 0,
                'type': 'security',
                'action': 'AI Security Agent 调用 Yakit 兼容扫描执行 SQL注入/越权/JWT/敏感信息探测',
                'tool': 'run_security_test',
                'input': {'target': api_url or url, 'api_doc': api_doc or ''},
            })
            tools.add('run_security_test')
        if url and 'run_browser_agent' not in tools:
            steps.insert(max(len(steps) - 2, 0), {
                'step': 0,
                'type': 'browser',
                'action': '通过 Playwright MCP 驱动 Chrome，截图并进行视觉模型分析',
                'tool': 'run_browser_agent',
                'input': {'url': frontend_url or url, 'goal': goal},
            })
            tools.add('run_browser_agent')
        if has_api_target and 'generate_from_api' not in tools:
            steps.insert(0, {
                'step': 0,
                'type': 'data',
                'action': 'AI Test Data Generator 基于接口 Schema 生成 normal/boundary/security 测试数据',
                'tool': 'generate_from_api',
                'input': {'swagger_url': context.get('swagger_url', ''), 'swagger_doc': api_doc, 'api': context.get('api') or api_url or '', 'mode': ['normal', 'boundary', 'security']},
            })
            tools.add('generate_from_api')
        if 'generate_test_data' not in tools:
            steps.insert(min(2, len(steps)), {
                'step': 0,
                'type': 'data',
                'action': 'AI 生成业务测试数据、边界数据和安全攻击数据',
                'tool': 'generate_test_data',
                'input': {'type': 'BUSINESS', 'count': test_data_count, 'fields': ['name', 'phone', 'email', 'password']},
            })
            tools.add('generate_test_data')
        if has_ui_target and 'run_web_test' not in tools:
            steps.append({
                'step': 0,
                'type': 'ui',
                'action': '调用 Playwright/Web UI 自动化执行页面链路',
                'tool': 'run_web_test',
                'input': {
                    'requirement': goal,
                    'url': frontend_url or url,
                    'ui_case_ids': context.get('ui_case_ids') or context.get('ui_test_case_ids') or [],
                    'ui_test_case_ids': context.get('ui_test_case_ids') or context.get('ui_case_ids') or [],
                    'ui_case_names': context.get('ui_case_names') or context.get('ui_test_case_names') or [],
                    'case_files': context.get('ui_case_files') or context.get('case_files') or [],
                },
            })
            tools.add('run_web_test')
        if has_app_target and 'run_app_test' not in tools:
            steps.append({
                'step': 0,
                'type': 'app',
                'action': '调用 Appium/APP 自动化能力执行移动端链路',
                'tool': 'run_app_test',
                'input': {
                    'requirement': goal,
                    'test_data_count': test_data_count,
                    **{key: context[key] for key in ('app_project', 'app_project_id', 'device', 'device_id', 'app_package', 'app_package_id', 'app_case_ids', 'app_test_case_ids') if context.get(key)},
                },
            })
            tools.add('run_app_test')
        if has_api_target and 'run_api_test' not in tools:
            steps.append({
                'step': 0,
                'type': 'api',
                'action': '调用 API 测试模块测试对象执行接口验证',
                'tool': 'run_api_test',
                'input': {
                    'api_test_objects': context.get('api_test_objects') or context.get('test_object_refs') or [],
                    'test_object_ref': context.get('test_object_ref') or {},
                    'url': api_url,
                    'allow_direct_api': bool(api_url and not context.get('api_test_objects') and not context.get('test_object_refs')),
                },
            })
            tools.add('run_api_test')
        if 'analyze_failure' not in tools:
            steps.append({
                'step': 0,
                'type': 'analysis',
                'action': 'AI 分析失败证据，输出根因、旧定位、新定位候选和修复建议',
                'tool': 'analyze_failure',
                'input': {'logs': '', 'screenshot': '', 'response': {'context': goal}},
            })
            tools.add('analyze_failure')
        if 'self_heal_test' not in tools:
            steps.append({
                'step': 0,
                'type': 'analysis',
                'action': 'AI 修复测试：生成自愈定位、数据修复或步骤修复建议',
                'tool': 'self_heal_test',
                'input': {'requirement': goal},
            })
            tools.add('self_heal_test')
        if 'rerun_fixed_test' not in tools:
            steps.append({
                'step': 0,
                'type': 'ui',
                'action': '再次执行修复后的测试链路，验证问题是否闭环',
                'tool': 'rerun_fixed_test',
                'input': {'requirement': goal, 'url': url},
            })
            tools.add('rerun_fixed_test')
        if has_performance_target and 'run_performance_test' not in tools:
            steps.append({
                'step': 0,
                'type': 'performance',
                'action': '执行性能基线检查并纳入质量评分',
                'tool': 'run_performance_test',
                'input': {'requirement': goal},
            })
            tools.add('run_performance_test')
        if 'create_report' not in tools:
            steps.append({
                'step': 0,
                'type': 'report',
                'action': '自动输出质量报告，汇总覆盖率、风险、缺陷和执行证据',
                'tool': 'create_report',
                'input': {'requirement': goal},
            })
        for index, item in enumerate(steps, start=1):
            item['step'] = index
        return {
            'summary': base.get('summary') or goal[:120],
            'steps': steps,
            'test_plan': base.get('test_plan') or {},
            'source': 'react_agent_planner',
            'react_architecture': REACT_ARCHITECTURE,
        }

    def _infer_url(self, goal: str, context: Dict[str, Any]) -> str:
        for key in ('url', 'frontend_url', 'api_url', 'target', 'base_url'):
            value = infer_http_url(context.get(key))
            if value:
                return value
        return infer_http_url(goal)

    def _looks_like_api(self, goal: str, url: str) -> bool:
        text = f'{goal} {url}'.lower()
        return any(marker in text for marker in ['/api/', '接口', 'openapi', 'swagger']) or bool(re.search(r'\b(get|post|put|patch|delete)\s+', text))

    def _after_tool_index(self, steps: list[Dict[str, Any]], tool_name: str, default: int = 0) -> int:
        for index, step in enumerate(steps):
            if step.get('tool') == tool_name:
                return index + 1
        return min(default, len(steps))
