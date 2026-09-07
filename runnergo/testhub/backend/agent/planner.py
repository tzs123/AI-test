from __future__ import annotations

import re
from typing import Any, Dict, List

from backend.llm.provider import LLMProvider
from backend.agent.url_utils import infer_http_url


FUNCTIONAL_CATEGORIES = [
    '功能测试',
    '异常测试',
    '边界测试',
    '安全测试',
    '性能测试',
]

REACT_ARCHITECTURE = [
    'Agent Planner',
    'Action Executor',
    'Observation',
    'Reflection',
    'Retry',
]

ALLOWED_TOOLS = {
    'analyze_requirement',
    'discover_test_points',
    'generate_test_data',
    'generate_test_case',
    'generate_from_api',
    'run_api_test',
    'run_web_test',
    'run_browser_agent',
    'run_app_test',
    'run_security_test',
    'run_performance_test',
    'analyze_failure',
    'self_heal_test',
    'rerun_fixed_test',
    'create_report',
}


class TestPlanner:
    """把自然语言需求拆成 ReAct Agent 测试计划和测试设计。"""

    def __init__(self, llm: LLMProvider | None = None):
        self.llm = llm or LLMProvider()

    def plan(self, requirement: str) -> Dict[str, Any]:
        llm_plan = self._plan_with_llm(requirement)
        if llm_plan:
            return llm_plan
        return self._fallback_plan(requirement)

    def _plan_with_llm(self, requirement: str) -> Dict[str, Any] | None:
        payload = self.llm.chat_json([
            {
                'role': 'system',
                'content': (
                    '你是资深测试平台架构师，请按 ReAct 架构输出 JSON，字段包含 summary, steps, test_plan。'
                    'steps 是数组，每项包含 step,type,action,tool,input。'
                    'type 只能是 data,case,api,ui,browser,app,security,performance,analysis,report。'
                    'tool 只能是 analyze_requirement,discover_test_points,generate_test_data,'
                    'generate_test_case,generate_from_api,run_api_test,'
                    'run_web_test,run_browser_agent,run_app_test,run_security_test,run_performance_test,'
                    'analyze_failure,self_heal_test,rerun_fixed_test,create_report。'
                    'test_plan 必须包含 functional, abnormal, boundary, security, performance 五类数组。'
                    '必须体现 Agent Planner -> Action Executor -> Observation -> Reflection -> Retry。'
                    '执行范围只来自用户需求和调用方传入的引用对象、本地测试资产；'
                    '不得自行添加生产环境、沙箱或隔离账号等前提。'
                ),
            },
            {'role': 'user', 'content': requirement},
        ])
        if not isinstance(payload, dict):
            return None
        steps = payload.get('steps')
        test_plan = payload.get('test_plan')
        if not isinstance(steps, list) or not isinstance(test_plan, dict):
            return None
        return {
            'summary': payload.get('summary') or requirement[:120],
            'steps': self._normalize_steps(steps),
            'test_plan': self._normalize_test_plan(test_plan, requirement),
            'source': 'react_agent_planner',
            'react_architecture': REACT_ARCHITECTURE,
        }

    def _fallback_plan(self, requirement: str) -> Dict[str, Any]:
        url = self._infer_url(requirement)
        fields = self._infer_data_fields(requirement)
        steps: List[Dict[str, Any]] = [
            {
                'step': 1,
                'type': 'analysis',
                'action': 'AI 分析需求：识别中文业务目标、前端 URL、接口 URL、APP 场景和测试范围',
                'tool': 'analyze_requirement',
                'input': {'requirement': requirement, 'url': url},
            },
            {
                'step': 2,
                'type': 'browser',
                'action': 'AI 理解系统：读取目标、页面、接口和已有知识，建立系统上下文',
                'tool': 'run_browser_agent',
                'input': {'url': url, 'goal': requirement, 'mode': 'understand_system'},
            },
            {
                'step': 3,
                'type': 'browser',
                'action': 'AI 探索系统：通过 Playwright/视觉能力识别页面、按钮、表单和业务流',
                'tool': 'run_browser_agent',
                'input': {'url': url, 'goal': requirement, 'mode': 'explore_system'},
            },
            {
                'step': 4,
                'type': 'case',
                'action': '生成测试方案：覆盖功能、异常、边界、安全、性能和回归策略',
                'tool': 'generate_test_case',
                'input': {'requirement': requirement},
            },
            {
                'step': 5,
                'type': 'case',
                'action': 'AI 发现测试点：抽取主流程、异常流、边界值、安全风险和质量门禁',
                'tool': 'discover_test_points',
                'input': {'requirement': requirement},
            },
            {
                'step': 6,
                'type': 'data',
                'action': 'AI 生成业务测试数据、边界数据和安全攻击数据',
                'tool': 'generate_test_data',
                'input': {'type': 'BUSINESS', 'count': self._infer_count(requirement), 'fields': fields},
            },
            {
                'step': 7,
                'type': 'data',
                'action': '基于 OpenAPI/接口 Schema 生成 normal/boundary/security 数据集',
                'tool': 'generate_from_api',
                'input': {'swagger_url': '', 'swagger_doc': {}, 'api': '', 'mode': ['normal', 'boundary', 'security']},
            },
            {
                'step': 8,
                'type': 'api',
                'action': '调用 API 测试能力执行接口链路',
                'tool': 'run_api_test',
                'input': {'requirement': requirement},
            },
            {
                'step': 9,
                'type': 'ui',
                'action': '调用 Playwright/Web UI 自动化执行页面链路',
                'tool': 'run_web_test',
                'input': {'requirement': requirement, 'url': url},
            },
            {
                'step': 10,
                'type': 'app',
                'action': '调用 Appium/APP 自动化能力执行移动端链路',
                'tool': 'run_app_test',
                'input': {'requirement': requirement},
            },
            {
                'step': 11,
                'type': 'analysis',
                'action': 'AI 自动执行：汇总 Playwright、Appium 和接口执行观察结果，判断下一步动作',
                'tool': 'analyze_requirement',
                'input': {'requirement': requirement, 'mode': 'execution_observation'},
            },
            {
                'step': 12,
                'type': 'analysis',
                'action': 'AI 发现 Bug 并定位根因，输出日志、截图、响应和定位器证据',
                'tool': 'analyze_failure',
                'input': {'logs': '', 'screenshot': '', 'response': {'context': requirement}},
            },
            {
                'step': 13,
                'type': 'analysis',
                'action': 'AI 修复测试：生成自愈定位、数据修复或步骤修复建议',
                'tool': 'self_heal_test',
                'input': {'requirement': requirement},
            },
            {
                'step': 14,
                'type': 'ui',
                'action': '再次执行修复后的测试链路，验证问题是否闭环',
                'tool': 'rerun_fixed_test',
                'input': {'requirement': requirement},
            },
            {
                'step': 15,
                'type': 'security',
                'action': '自动安全扫描：调用 Yakit 兼容 Security Agent 检测注入、越权、JWT 和敏感信息泄露',
                'tool': 'run_security_test',
                'input': {'target': url, 'api_doc': '', 'parameters': []},
            },
            {
                'step': 16,
                'type': 'performance',
                'action': '执行性能基线检查并纳入质量评分',
                'tool': 'run_performance_test',
                'input': {'requirement': requirement},
            },
            {
                'step': 17,
                'type': 'report',
                'action': '自动输出质量报告，汇总覆盖率、风险、缺陷和执行证据',
                'tool': 'create_report',
                'input': {'requirement': requirement},
            },
        ]
        return {
            'summary': f'围绕“{requirement[:80]}”生成跨模块测试工作流',
            'steps': self._normalize_steps(steps),
            'test_plan': self._fallback_test_plan(requirement),
            'source': 'react_agent_planner',
            'react_architecture': REACT_ARCHITECTURE,
        }

    def _normalize_steps(self, steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized = []
        for index, item in enumerate(steps, start=1):
            step_type = str(item.get('type') or 'case').lower()
            tool = str(item.get('tool') or self._tool_for_type(step_type))
            if tool not in ALLOWED_TOOLS:
                tool = self._tool_for_type(step_type)
            normalized.append({
                'step': int(item.get('step') or index),
                'type': step_type,
                'action': str(item.get('action') or '执行测试步骤'),
                'tool': tool,
                'input': item.get('input') if isinstance(item.get('input'), dict) else {},
            })
        return normalized

    def _normalize_test_plan(self, test_plan: Dict[str, Any], requirement: str) -> Dict[str, Any]:
        fallback = self._fallback_test_plan(requirement)
        key_map = {
            'functional': 'functional',
            '功能测试': 'functional',
            'abnormal': 'abnormal',
            '异常测试': 'abnormal',
            'boundary': 'boundary',
            '边界测试': 'boundary',
            'security': 'security',
            '安全测试': 'security',
            'performance': 'performance',
            '性能测试': 'performance',
        }
        normalized = fallback.copy()
        for key, value in test_plan.items():
            target = key_map.get(key)
            if target and isinstance(value, list) and value:
                normalized[target] = [str(item) for item in value]
        return normalized

    def _fallback_test_plan(self, requirement: str) -> Dict[str, List[str]]:
        subject = self._subject(requirement)
        return {
            'functional': [
                f'{subject}主流程可成功完成',
                f'{subject}关键状态与返回内容正确',
            ],
            'abnormal': [
                '必填字段为空时返回明确错误',
                '账号不存在、密码错误或验证码错误时拒绝操作',
                '依赖服务异常时有可观测错误信息',
            ],
            'boundary': [
                '输入最大长度、最小长度和空白字符处理正确',
                '特殊字符、中文、Emoji、超长参数不导致系统异常',
            ],
            'security': [
                'SQL 注入和脚本注入被拦截或转义',
                'Token 缺失、过期、篡改时访问被拒绝',
                '暴力破解或高频重试触发限制策略',
            ],
            'performance': [
                '核心接口响应时间满足基线',
                '并发访问下错误率和吞吐量满足预期',
            ],
        }

    def _infer_data_fields(self, requirement: str) -> List[str]:
        fields = ['name', 'phone', 'email', 'password']
        if '地址' in requirement or '订单' in requirement:
            fields.append('address')
        if '身份证' in requirement:
            fields.append('id_card')
        return fields

    def _infer_count(self, requirement: str) -> int:
        match = re.search(r'(\d+)\s*(个|条|名)?', requirement)
        if not match:
            return 100
        return min(max(int(match.group(1)), 1), 10000)

    def _infer_url(self, requirement: str) -> str:
        return infer_http_url(requirement)

    def _subject(self, requirement: str) -> str:
        compact = re.sub(r'\s+', '', requirement)
        return compact[:24] or '目标功能'

    def _tool_for_type(self, step_type: str) -> str:
        return {
            'data': 'generate_test_data',
            'case': 'generate_test_case',
            'api': 'run_api_test',
            'ui': 'run_web_test',
            'browser': 'run_browser_agent',
            'app': 'run_app_test',
            'security': 'run_security_test',
            'performance': 'run_performance_test',
            'report': 'create_report',
            'analysis': 'analyze_failure',
            'requirement': 'analyze_requirement',
        }.get(step_type, 'generate_test_case')
