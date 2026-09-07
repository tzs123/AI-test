# -*- coding: utf-8 -*-
"""APP AI Test Agent 的资产规划、结果分析与缺陷草稿服务。"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from asgiref.sync import async_to_sync
from django.db.models import Q

from .models import (
    AppAgentEvent,
    AppAgentTask,
    AppElement,
    AppTestCase,
    AppTestConfig,
    AppTestExecution,
)
from .utils.ios_flow import strip_ios_browser_bootstrap_steps

logger = logging.getLogger(__name__)


GENERIC_GOAL_WORDS = (
    '请帮我', '帮我', '进行', '执行', '自动化', '自动', '测试', '验证', '检查',
    '功能', '流程', '场景', 'app', 'application', 'mobile', '客户端', '移动端',
)
INPUT_ELEMENT_KEYWORDS = (
    '手机号', '手机号码', '账号', '用户名', '密码', '验证码', '搜索', '关键词',
    '金额', '银行卡', '身份证', '地址', '评论', '消息', '内容', '固件编号',
)
CLICK_ELEMENT_KEYWORDS = (
    '登录', '注册', '搜索', '确认', '提交', '下一步', '完成', '同意', '勾选',
    '升级', '进入', '保存', '返回', '支付', '付款', '发送验证码', '获取验证码', '点击发送',
)
ASSERT_ELEMENT_KEYWORDS = (
    '成功', '首页', '结果', '个人中心', '我的', '完成', '通过',
)

GENERATED_FLOW_ACTIONS = {
    'click': 'self_heal_click',
    'tap': 'self_heal_click',
    'self_heal_click': 'self_heal_click',
    'input': 'self_heal_input',
    'fill': 'self_heal_input',
    'type': 'self_heal_input',
    'self_heal_input': 'self_heal_input',
    'assert': 'assert_exists',
    'assert_exists': 'assert_exists',
    'wait': 'wait',
    'screenshot': 'screenshot',
    'page_detect': 'page_detect',
}

DOMAIN_ELEMENT_KEYWORDS = (
    (('登录', 'login', 'sign in'), ('账号', '用户名', '手机号', '手机', '密码', '验证码', '发送验证码', '获取验证码', '点击发送', '登录', '同意', '确认', '首页')),
    (('注册', 'register', 'sign up'), ('手机号', '验证码', '发送验证码', '获取验证码', '点击发送', '密码', '注册', '同意', '确认')),
    (('搜索', 'search'), ('搜索', '关键词', '输入框', '确认', '结果')),
    (('购物', '下单', '订单', 'checkout', 'cart'), ('商品', '购物车', '结算', '地址', '订单', '提交', '确认')),
    (('支付', '付款', 'pay'), ('金额', '银行卡', '支付', '付款', '密码', '确认')),
    (('转账', 'transfer'), ('收款人', '账号', '金额', '转账', '验证码', '发送验证码', '获取验证码', '点击发送', '确认')),
    (('个人中心', '我的', 'profile'), ('我的', '个人中心', '头像', '设置')),
)

INPUT_VARIABLE_RULES = (
    (('手机号', '手机号码', 'phone', 'mobile'), 'PHONE', '手机号', False),
    (('用户名', '用户名称', '账号', 'username', 'account'), 'USERNAME', '用户名/账号', False),
    (('密码', 'password'), 'PASSWORD', '密码', True),
    (('验证码', 'verification code', 'verify code'), 'VERIFY_CODE', '验证码', True),
    (('搜索', '关键词', 'keyword'), 'KEYWORD', '搜索关键词', False),
    (('金额', 'money', 'amount'), 'AMOUNT', '金额', False),
    (('银行卡', 'bank card'), 'BANK_CARD', '银行卡号', True),
    (('身份证', 'identity'), 'ID_CARD', '身份证号', True),
    (('车牌', 'license plate'), 'LICENSE_PLATE', '车牌号', False),
    (('地址', 'address'), 'ADDRESS', '地址', False),
    (('评论', '消息', '内容', 'comment', 'message'), 'CONTENT', '文本内容', False),
)

VERIFICATION_CODE_TRIGGER_KEYWORDS = (
    '点击发送', '发送验证码', '获取验证码', '重新发送', '重发验证码',
    '发送动态码', '获取动态码', '发送校验码', '获取校验码',
    'sendcode', 'getcode', 'resendcode',
)

REACT_STAGES = (
    'Agent Planner',
    'Action Executor',
    'Observation',
    'Reflection',
    'Retry',
)

SECURITY_AGENT_CHECKS = (
    'SQL注入测试',
    '越权测试',
    'JWT测试',
    '敏感信息泄露',
)

HTTP_URL_PATTERN = re.compile(r'https?://[^\s，,。；;]+', re.I)
EXPLORATION_URL_PATTERN = re.compile(
    r'(?<![\w@])(?:https?://)?(?:localhost|(?:\d{1,3}\.){3}\d{1,3}|'
    r'(?:[a-z0-9-]+\.)+[a-z]{2,})(?::\d{1,5})?(?:/[^\s，,。；;、【】]*)?',
    re.I,
)
FLOW_SEMANTIC_MIN_SCORE = 0.35


def record_agent_event(
    task: AppAgentTask,
    phase: str,
    message: str,
    *,
    level: str = 'info',
    payload: Optional[Dict[str, Any]] = None,
) -> AppAgentEvent:
    """写入一条持久化 Agent 事件。"""
    from .database import retry_database_write

    return retry_database_write(
        lambda: AppAgentEvent.objects.create(
            task=task,
            phase=phase,
            level=level,
            message=message[:500],
            payload=payload or {},
        )
    )


def _app_react_context(task: AppAgentTask, *, mode: str, source: str) -> Dict[str, Any]:
    """构造 APP Agent 的 ReAct 能力说明，直接进入 plan 返回。"""
    return {
        'architecture': list(REACT_STAGES),
        'mode': mode,
        'source': 'react_agent_planner',
        'planner_source': source,
        'browser_mcp': {
            'enabled': True,
            'driver': 'Playwright MCP',
            'browser': 'Chrome',
            'flow': ['AI', 'Browser MCP', 'Chrome', '页面', '截图', '视觉模型分析'],
            'usage': 'H5/WebView 或页面视觉定位失败时使用截图和语义候选辅助定位分析。',
        },
        'security_agent': {
            'enabled': True,
            'engine': 'Yakit-compatible',
            'flow': ['输入测试目标', 'AI分析接口', '调用Yakit', '生成安全报告'],
            'checks': list(SECURITY_AGENT_CHECKS),
        },
        'test_data_generator': {
            'enabled': True,
            'normal': [
                {'field': 'amount', 'value': 100},
                {'field': 'user', 'value': '用户A'},
                {'field': 'product', 'value': '商品B'},
            ],
            'abnormal': ['金额-1', '超长字符', 'SQL字符', 'Unicode字符', '空值'],
        },
        'result_analyzer': {
            'enabled': True,
            'outputs': ['失败原因', '旧定位', '新定位候选', '修复建议', '证据报告'],
        },
        'task': {
            'goal': task.goal,
            'device': task.device.device_id if task.device else '',
            'platform': task.device.platform if task.device else '',
        },
    }


def _app_react_trace(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把计划转换成前端/审计可展示的 ReAct trace。"""
    trace = [
        {
            'trace_type': 'thought',
            'phase': 'Agent Planner',
            'action': plan.get('summary') or '分析测试目标并拆解可执行动作',
            'status': 'success',
        }
    ]
    for scenario in plan.get('scenarios') or []:
        trace.extend([
            {
                'trace_type': 'action',
                'phase': 'Action Executor',
                'tool': 'run_app_test',
                'action': f"执行 APP 用例：{scenario.get('name')}",
                'params': {'case_id': scenario.get('case_id')},
                'status': 'pending',
            },
            {
                'trace_type': 'observation',
                'phase': 'Observation',
                'tool': 'run_app_test',
                'action': '等待设备执行、Allure 报告和截图证据',
                'status': 'pending',
            },
        ])
    if plan.get('generated_draft'):
        trace.append({
            'trace_type': 'action',
            'phase': 'Action Executor',
            'tool': 'review_generated_flow',
            'action': '生成 APP Flow 草稿并等待人工评审',
            'params': {'review_required': True},
            'status': 'pending',
        })
    trace.extend([
        {
            'trace_type': 'reflection',
            'phase': 'Reflection',
            'tool': 'analyze_failure',
            'action': '失败时分析日志、截图和定位链，生成新定位候选',
            'status': 'pending',
        },
        {
            'trace_type': 'reflection',
            'phase': 'Retry',
            'tool': 'retry',
            'action': '按修复建议重试失败场景，需要一次性验证码时等待用户输入',
            'status': 'pending',
        },
    ])
    return trace


def _attach_react_metadata(task: AppAgentTask, plan: Dict[str, Any]) -> Dict[str, Any]:
    plan['react'] = _app_react_context(
        task,
        mode=str(plan.get('mode') or ''),
        source=str(plan.get('source') or ''),
    )
    plan['source'] = 'react_agent_planner'
    plan['agent_trace'] = _app_react_trace(plan)
    plan['capabilities'] = {
        'planner': 'ReAct Agent Planner',
        'executor': 'APP Action Executor',
        'observation': '设备日志 + 截图 + Allure',
        'reflection': 'AI 失败归因与定位修复建议',
        'retry': '失败场景可基于修复建议重试',
        'browser_mcp': 'Playwright MCP + Chrome + 视觉模型分析',
        'security_agent': 'Yakit-compatible AI Security Agent',
        'test_data_generator': 'AI Test Data Generator',
        'result_analyzer': 'AI Failure Analyzer',
    }
    return plan


def _usable_flow(ui_flow: Any) -> bool:
    if isinstance(ui_flow, list):
        return bool(ui_flow)
    if isinstance(ui_flow, dict):
        return bool(ui_flow.get('steps'))
    return False


def _flow_search_text(flow: Sequence[Dict[str, Any]]) -> str:
    """Extract non-secret business semantics from recorded APP steps."""
    values = []
    seen = set()

    def append(value: Any) -> None:
        text = re.sub(r'\s+', ' ', str(value or '')).strip()
        if not text or text in seen:
            return
        seen.add(text)
        values.append(text[:160])

    for step in flow[:200]:
        if not isinstance(step, dict):
            continue
        append(step.get('name'))
        config = step.get('config') if isinstance(step.get('config'), dict) else {}
        for field in ('url', 'target_url', 'path', 'expected', 'option_label'):
            append(config.get(field))
        fingerprint = config.get('fingerprint')
        if not isinstance(fingerprint, dict):
            continue
        for field in ('resource_id', 'text', 'content_desc'):
            append(fingerprint.get(field))
        parent = fingerprint.get('parent')
        if isinstance(parent, dict):
            for field in ('resource_id', 'text', 'content_desc'):
                append(parent.get(field))
    return ' '.join(values)[:6000]


def _explicit_goal_test_points(goal: str) -> List[str]:
    text = str(goal or '')
    bracketed = [item.strip() for item in re.findall(r'【([^】]+)】', text) if item.strip()]
    if bracketed:
        return list(dict.fromkeys(bracketed))[:50]
    numbered = [
        item.strip(' ，。；;、')
        for item in re.split(r'(?:^|\s)\d+[、.．)]\s*', text)
        if item.strip(' ，。；;、')
    ]
    return list(dict.fromkeys(numbered))[:50] if len(numbered) >= 2 else []


def _coverage_text(value: Any) -> str:
    text = str(value or '').lower().strip()
    text = re.sub(r'^https?://', '', text)
    text = re.sub(r'^测试\s*', '', text)
    return re.sub(r'[\s，。；;、【】/?=&:_-]+', '', text)


def _uncovered_app_test_points(
    goal: str,
    assets: Sequence[Dict[str, Any]],
    selected_ids: Sequence[int],
) -> List[str]:
    points = _explicit_goal_test_points(goal)
    if not points:
        return []
    selected = {int(item) for item in selected_ids}
    haystacks = [
        _coverage_text(' '.join([
            str(asset.get('name') or ''),
            str(asset.get('description') or ''),
            str(asset.get('search_text') or ''),
        ]))
        for asset in assets
        if int(asset.get('id') or 0) in selected
    ]
    uncovered = []
    for point in points:
        normalized = _coverage_text(point)
        if not normalized or not any(normalized in haystack for haystack in haystacks):
            uncovered.append(point)
    return uncovered


def _normalize_exploration_url(value: Any) -> str:
    """Normalize an explicit page target, including host:port/path input."""
    text = str(value or '').strip()
    match = EXPLORATION_URL_PATTERN.search(text)
    if not match:
        return ''
    candidate = match.group(0).rstrip(').]}》>，,。；;、')
    if not re.match(r'^https?://', candidate, re.I):
        candidate = f'http://{candidate}'
    try:
        parsed = urlsplit(candidate)
        _ = parsed.port
    except ValueError:
        return ''
    if (
        parsed.scheme.lower() not in {'http', 'https'}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return ''
    return candidate


def _goal_exploration_targets(goal: str) -> List[Dict[str, str]]:
    """Return explicit frontend page targets that should be explored on-device."""
    points = _explicit_goal_test_points(goal)
    targets = []
    seen = set()
    for point in points:
        lowered = str(point or '').lower()
        if any(marker in lowered for marker in ('接口', 'openapi', 'swagger')):
            continue
        url = _normalize_exploration_url(point)
        if not url or '/api/' in urlsplit(url).path.lower() or url in seen:
            continue
        seen.add(url)
        targets.append({
            'url': url,
            'label': str(point).strip(),
            'source': 'business_requirement',
        })
    return targets[:20]


def parse_agent_structured_input(goal: str) -> Dict[str, Any]:
    """识别业务需求文本中需要先解析的结构化目标。"""
    match = HTTP_URL_PATTERN.search(str(goal or ''))
    if not match:
        return {}
    url = match.group(0).rstrip(').]}》>，,。；;')
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        return {}
    text = str(goal or '').lower()
    input_type = 'api_url' if (
        any(marker in text for marker in ('接口', 'api', 'openapi', 'swagger'))
        or '/api/' in parsed.path.lower()
    ) else 'frontend_url'
    return {
        'type': input_type,
        'url': url,
        'scheme': parsed.scheme,
        'host': parsed.netloc,
        'path': parsed.path or '/',
        'query': parsed.query,
        'source': 'business_requirement',
    }


def analyze_agent_structured_input(task: AppAgentTask, structured_input: Dict[str, Any]) -> Dict[str, Any]:
    """使用现有解析能力处理 URL；前端 URL 只分析，不执行页面操作。"""
    target_type = structured_input.get('type')
    target_url = str(structured_input.get('url') or '')
    if target_type == 'frontend_url':
        from backend.browser_agent.browser_controller import WebVisionAgent

        return WebVisionAgent().run(
            url=target_url,
            goal=task.goal,
            analyze_only=True,
        )
    return {
        'status': 'success',
        'message': '已解析接口 URL，等待 API 测试模块按接口目标继续处理',
        'target': {
            'url': target_url,
            'scheme': structured_input.get('scheme'),
            'host': structured_input.get('host'),
            'path': structured_input.get('path'),
            'query': structured_input.get('query'),
        },
    }


def collect_app_case_assets(task: AppAgentTask) -> List[Dict[str, Any]]:
    """收集任务项目内可真实执行的 APP 用例资产。"""
    queryset = AppTestCase.objects.filter(
        Q(project=task.project) |
        Q(project__isnull=True, created_by=task.user)
    ).select_related('app_package')
    if task.app_package_id:
        queryset = queryset.filter(app_package_id=task.app_package_id)

    assets = []
    for case in queryset.order_by('-updated_at'):
        if not _usable_flow(case.ui_flow):
            continue
        flow = case.ui_flow if isinstance(case.ui_flow, list) else case.ui_flow.get('steps', [])
        flow_sha256 = hashlib.sha256(
            json.dumps(case.ui_flow, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')
        ).hexdigest()
        assets.append({
            'id': case.id,
            'name': case.name,
            'description': case.description or '',
            'search_text': _flow_search_text(flow),
            'package_name': case.app_package.package_name if case.app_package else '',
            'package_label': case.app_package.name if case.app_package else '',
            'step_count': len(flow),
            'variables': case.variables or [],
            'updated_at': case.updated_at.isoformat(),
            'flow_sha256': flow_sha256,
        })
    return assets


def _selected_app_asset_snapshot(
    assets: Sequence[Dict[str, Any]],
    selected_ids: Sequence[int],
) -> List[Dict[str, Any]]:
    selected = set(selected_ids)
    return [
        {
            'case_id': asset['id'],
            'updated_at': asset.get('updated_at'),
            'flow_sha256': asset.get('flow_sha256'),
            'step_count': asset.get('step_count', 0),
        }
        for asset in assets
        if asset['id'] in selected
    ]


def _element_strategy_summary(element: AppElement) -> List[Dict[str, Any]]:
    config = element.config or {}
    strategies = []
    configured = config.get('locator_strategies')
    if isinstance(configured, list):
        for item in configured:
            if not isinstance(item, dict) or item.get('enabled') is False:
                continue
            strategy_type = str(item.get('type') or '').strip().lower()
            value = item.get('value')
            if strategy_type and value not in (None, '', {}):
                strategies.append({
                    'type': strategy_type,
                    'value': value,
                    'priority': int(item.get('priority') or len(strategies) + 1),
                })
    if strategies:
        return sorted(strategies, key=lambda item: item['priority'])

    fallback_fields = (
        ('resource_id', 'resource_id'),
        ('accessibility', 'accessibility_id'),
        ('text', 'text'),
        ('xpath', 'xpath'),
        ('ocr', 'ocr_text'),
        ('image', 'image_path'),
        ('position', 'fallback_position'),
    )
    for strategy_type, field in fallback_fields:
        value = config.get(field)
        if value not in (None, '', {}):
            strategies.append({
                'type': strategy_type,
                'value': value,
                'priority': len(strategies) + 1,
            })
    if not strategies and element.element_type == 'pos' and config.get('x') is not None:
        strategies.append({
            'type': 'position',
            'value': {'x': config.get('x'), 'y': config.get('y')},
            'priority': 1,
        })
    return strategies


def _element_confidence(element: AppElement) -> float:
    strategy_types = {item['type'] for item in _element_strategy_summary(element)}
    confidence = 0.42
    if 'resource_id' in strategy_types:
        confidence = 0.94
    elif 'accessibility' in strategy_types:
        confidence = 0.9
    elif 'text' in strategy_types:
        confidence = 0.82
    elif 'xpath' in strategy_types:
        confidence = 0.68
    elif 'ocr' in strategy_types:
        confidence = 0.62
    elif 'image' in strategy_types:
        confidence = 0.58
    if len(strategy_types) >= 3:
        confidence += 0.04
    if strategy_types == {'position'} or element.element_type == 'pos':
        confidence = min(confidence, 0.48)
    return round(min(confidence, 0.98), 2)


def _preferred_selector(element: AppElement) -> Tuple[str, Any]:
    strategies = _element_strategy_summary(element)
    selector_type_map = {
        'resource_id': 'id',
        'accessibility': 'accessibility',
        'accessibility_id': 'accessibility',
        'position': 'pos',
    }
    if strategies:
        selected = strategies[0]
        return selector_type_map.get(selected['type'], selected['type']), selected['value']
    return element.element_type, ''


def collect_app_element_assets(task: AppAgentTask) -> List[Dict[str, Any]]:
    """收集当前任务可引用的元素资产，隔离其他用户的未归档元素。"""
    queryset = AppElement.objects.filter(is_active=True).filter(
        Q(project=task.project) |
        Q(project__isnull=True, created_by=task.user)
    ).select_related('folder')
    reference_context = (task.plan or {}).get('reference_context') or {}
    referenced_object_ids = reference_context.get('app_object_ids') or []
    if reference_context.get('source_entry') == 'reference' and referenced_object_ids:
        queryset = queryset.filter(id__in=referenced_object_ids)
    platform = str(task.device.platform or '').lower()
    selected_package = task.app_package.package_name if task.app_package else ''
    assets = []
    for element in queryset.order_by('id'):
        config = element.config or {}
        element_platform = str(config.get('platform') or '').lower()
        if element_platform and platform and element_platform != platform:
            continue
        fingerprint = config.get('fingerprint') if isinstance(config.get('fingerprint'), dict) else {}
        element_package = str(config.get('package') or fingerprint.get('package') or '')
        if selected_package and element_package and element_package != selected_package:
            continue
        selector_type, selector = _preferred_selector(element)
        strategies = _element_strategy_summary(element)
        assets.append({
            'id': element.id,
            'name': element.name,
            'type': element.element_type,
            'tags': element.tags or [],
            'folder': element.folder.name if element.folder else '',
            'selector_type': selector_type,
            'selector': selector,
            'locator_strategies': strategies,
            'confidence': _element_confidence(element),
            'package': element_package,
        })
    return assets


def _goal_terms(goal: str) -> List[str]:
    text = re.sub(r'[，。；;,.!?！？、/\\|]+', ' ', str(goal or ''))
    text = re.sub(r'(验证|测试|检查|进入|点击|输入|填写|选择|流程|场景|核心|自动化|APP|app)', ' ', text)
    terms = []
    for item in re.split(r'\s+|和|及|与|并|后|再|然后|到|为', text):
        item = item.strip(' ：:（）()[]【】"\'')
        if 2 <= len(item) <= 20 and item not in GENERIC_GOAL_WORDS:
            terms.append(item)
    return terms


def _suggestion_action(term: str) -> str:
    lowered = str(term or '').lower()
    if any(keyword.lower() in lowered for keyword in INPUT_ELEMENT_KEYWORDS):
        return 'input'
    if any(keyword.lower() in lowered for keyword in ASSERT_ELEMENT_KEYWORDS):
        return 'assert_exists'
    return 'click'


def _suggestion_name(term: str, action: str) -> str:
    if action == 'input':
        return f'输入{term}' if not str(term).startswith('输入') else term
    if action == 'assert_exists':
        return f'{term}提示' if '成功' in str(term) else term
    return f'点击{term}' if not str(term).startswith(('点击', '选择', '进入')) else term


def build_agent_element_suggestions(task: AppAgentTask, limit: int = 12) -> List[Dict[str, Any]]:
    """从目标中生成可人工确认的一键落库元素候选。"""
    existing_names = {
        item['name'].strip().lower()
        for item in collect_app_element_assets(task)
        if item.get('name')
    }
    ordered_terms = []
    goal_lower = str(task.goal or '').lower()
    for keyword in [*INPUT_ELEMENT_KEYWORDS, *CLICK_ELEMENT_KEYWORDS, *ASSERT_ELEMENT_KEYWORDS]:
        if keyword.lower() in goal_lower and keyword not in ordered_terms:
            ordered_terms.append(keyword)
    for term in _goal_terms(task.goal):
        if term not in ordered_terms:
            ordered_terms.append(term)

    suggestions = []
    for term in ordered_terms:
        action = _suggestion_action(term)
        name = _suggestion_name(term, action)
        if name.strip().lower() in existing_names:
            continue
        config = {
            'platform': task.device.platform,
            'text': term,
            'ocr_text': term,
            'package': task.app_package.package_name if task.app_package else '',
            'locator_strategies': [
                {'type': 'text', 'enabled': True, 'priority': 1, 'value': term},
                {'type': 'ocr', 'enabled': True, 'priority': 2, 'value': term},
            ],
            'ai_metadata': {
                'source': 'app_agent_element_suggestion',
                'task_id': task.id,
                'goal': task.goal[:500],
                'action_hint': action,
            },
        }
        suggestions.append({
            'key': hashlib.sha1(f'{task.id}:{name}:{term}:{action}'.encode('utf-8')).hexdigest()[:12],
            'name': name[:200],
            'element_type': 'appium',
            'tags': list(dict.fromkeys(['AI Agent', '待验证', term, action])),
            'config': config,
            'action_hint': action,
            'locator_text': term,
            'confidence': 0.58 if action == 'input' else 0.54,
            'reason': '从测试目标中提取，保存后会作为文本/OCR 定位候选，需要在编排器或元素管理中复核。',
        })
        if len(suggestions) >= limit:
            break
    return suggestions


def _unique_element_name(base_name: str, *, project_id: int, user_id: int) -> str:
    candidate = str(base_name or 'AI元素').strip()[:180] or 'AI元素'
    accessible = AppElement.objects.filter(
        is_active=True,
        name=candidate,
    ).filter(Q(project_id=project_id) | Q(project__isnull=True, created_by_id=user_id))
    if not accessible.exists():
        return candidate
    suffix = 2
    while suffix <= 99:
        next_name = f'{candidate}-{suffix}'[:200]
        if not AppElement.objects.filter(name=next_name, folder__isnull=True, is_active=True).exists():
            return next_name
        suffix += 1
    return f'{candidate}-{hashlib.sha1(candidate.encode("utf-8")).hexdigest()[:6]}'[:200]


def materialize_agent_element_suggestions(
    task: AppAgentTask,
    *,
    suggestion_keys: Optional[Sequence[str]] = None,
) -> List[AppElement]:
    """把 Agent 元素候选保存为真实 AppElement 资产。"""
    suggestions = build_agent_element_suggestions(task)
    if suggestion_keys:
        allowed = {str(item) for item in suggestion_keys}
        suggestions = [item for item in suggestions if item['key'] in allowed]
    created = []
    for suggestion in suggestions:
        name = _unique_element_name(
            suggestion['name'],
            project_id=task.project_id,
            user_id=task.user_id,
        )
        element = AppElement.objects.create(
            project=task.project,
            name=name,
            element_type=suggestion['element_type'],
            tags=suggestion['tags'],
            config=suggestion['config'],
            created_by=task.user,
        )
        created.append(element)
    return created


def _input_variable(element_name: str) -> Optional[Dict[str, Any]]:
    normalized = str(element_name or '').lower()
    if _is_verification_code_trigger_text(normalized):
        return None
    for keywords, name, label, sensitive in INPUT_VARIABLE_RULES:
        if any(keyword in normalized for keyword in keywords):
            return {
                'name': name,
                'label': label,
                'sensitive': sensitive,
                'reason': f'步骤“{element_name}”需要输入测试数据',
            }
    if normalized.startswith('输入') or any(word in normalized for word in ('填写', '录入')):
        return {
            'name': 'INPUT_VALUE',
            'label': '输入内容',
            'sensitive': False,
            'reason': f'步骤“{element_name}”需要输入测试数据',
        }
    return None


def _is_verification_code_trigger_text(value: Any) -> bool:
    text = re.sub(r'\s+', '', str(value or '').lower())
    if not text:
        return False
    if any(keyword in text for keyword in VERIFICATION_CODE_TRIGGER_KEYWORDS):
        return True
    return (
        any(word in text for word in ('发送', '获取', '重发', 'send', 'get', 'resend')) and
        any(word in text for word in ('验证码', '动态码', '校验码', 'sms', 'otp', 'code'))
    )


def _asset_semantic_text(asset: Dict[str, Any]) -> str:
    parts = [
        asset.get('name', ''),
        asset.get('folder', ''),
        asset.get('selector', ''),
        ' '.join(map(str, asset.get('tags') or [])),
    ]
    for strategy in asset.get('locator_strategies') or []:
        if isinstance(strategy, dict):
            parts.append(strategy.get('value', ''))
    return ' '.join(json.dumps(part, ensure_ascii=False) if isinstance(part, (dict, list)) else str(part) for part in parts)


def _is_verification_code_trigger_asset(asset: Dict[str, Any]) -> bool:
    return _is_verification_code_trigger_text(_asset_semantic_text(asset))


def _is_verification_code_input_step(step: Dict[str, Any], asset_map: Dict[int, Dict[str, Any]]) -> bool:
    if step.get('type') != 'self_heal_input':
        return False
    config = step.get('config') if isinstance(step.get('config'), dict) else {}
    text = f"{step.get('name', '')} {config.get('selector', '')} {config.get('value', '')}"
    try:
        asset = asset_map.get(int(config.get('element_id')))
    except (TypeError, ValueError):
        asset = None
    if asset:
        text = f'{text} {_asset_semantic_text(asset)}'
    return any(word in str(text).lower() for word in ('验证码', 'verify_code', 'sms_code', 'verification', 'otp', 'captcha'))


def _is_phone_input_step(step: Dict[str, Any], asset_map: Dict[int, Dict[str, Any]]) -> bool:
    if step.get('type') != 'self_heal_input':
        return False
    config = step.get('config') if isinstance(step.get('config'), dict) else {}
    text = f"{step.get('name', '')} {config.get('selector', '')} {config.get('value', '')}"
    try:
        asset = asset_map.get(int(config.get('element_id')))
    except (TypeError, ValueError):
        asset = None
    if asset:
        text = f'{text} {_asset_semantic_text(asset)}'
    return any(word in str(text).lower() for word in ('手机号', '手机号码', 'phone', 'mobile', '${phone}'))


def _ensure_verification_code_prerequisite_order(
    steps: Sequence[Dict[str, Any]],
    element_assets: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """验证码输入前必须先触发获取/发送验证码按钮。"""
    normalized_steps = list(steps or [])
    if not normalized_steps:
        return normalized_steps
    asset_map = {item['id']: item for item in element_assets}
    code_input_index = next(
        (index for index, step in enumerate(normalized_steps) if _is_verification_code_input_step(step, asset_map)),
        None,
    )
    if code_input_index is None:
        return normalized_steps
    phone_input_index = next(
        (index for index, step in enumerate(normalized_steps) if _is_phone_input_step(step, asset_map)),
        None,
    )
    if phone_input_index is None or phone_input_index > code_input_index:
        return normalized_steps

    trigger_step_index = None
    for index, step in enumerate(normalized_steps):
        config = step.get('config') if isinstance(step.get('config'), dict) else {}
        try:
            asset = asset_map.get(int(config.get('element_id')))
        except (TypeError, ValueError):
            asset = None
        if step.get('type') == 'self_heal_click' and (
            _is_verification_code_trigger_text(step.get('name')) or
            (asset and _is_verification_code_trigger_asset(asset))
        ):
            trigger_step_index = index
            break

    if trigger_step_index is None:
        trigger_asset = next(
            (asset for asset in element_assets if _is_verification_code_trigger_asset(asset)),
            None,
        )
        if not trigger_asset:
            return normalized_steps
        trigger_step = _draft_step_from_element(
            trigger_asset,
            'click',
            len(normalized_steps) + 1,
            reason='验证码输入前需要先点击获取/发送验证码',
            confidence=trigger_asset.get('confidence'),
        )
        insert_at = code_input_index
        normalized_steps.insert(insert_at, trigger_step)
        return normalized_steps

    trigger_step = normalized_steps.pop(trigger_step_index)
    if trigger_step_index < code_input_index:
        code_input_index -= 1
    insert_at = max(phone_input_index + 1, min(code_input_index, len(normalized_steps)))
    normalized_steps.insert(insert_at, trigger_step)
    return normalized_steps


def _element_order(asset: Dict[str, Any]) -> Tuple[float, int]:
    name = str(asset['name']).lower()
    if _is_verification_code_trigger_asset(asset):
        return 12.5, asset['id']
    variable = _input_variable(name)
    if variable:
        variable_order = {
            'PHONE': 10,
            'USERNAME': 11,
            'PASSWORD': 12,
            'VERIFY_CODE': 13,
        }
        return variable_order.get(variable['name'], 15), asset['id']
    if any(word in name for word in ('同意', '勾选', '协议')):
        return 20, asset['id']
    if any(word in name for word in ('登录', '提交', '确认', '支付', '付款', '下一步', '完成')):
        return 30, asset['id']
    if any(word in name for word in ('成功', '首页', '结果', '个人中心', '我的')):
        return 40, asset['id']
    return 25, asset['id']


def _draft_step_from_element(
    asset: Dict[str, Any],
    action: str,
    index: int,
    *,
    value: str = '',
    reason: str = '',
    confidence: Optional[float] = None,
) -> Dict[str, Any]:
    action = GENERATED_FLOW_ACTIONS.get(str(action or '').lower(), '')
    if not action:
        raise ValueError('不支持的草稿动作')
    config = {
        'element_id': asset['id'],
        'selector_type': asset['selector_type'],
        'selector': asset['selector'],
        'locator_strategies': copy.deepcopy(asset['locator_strategies']),
        'timeout': 5,
        'semantic_min_score': 36,
        'ocr_min_confidence': 0.5,
        'self_heal_mode': 'report',
        'allow_coordinate_persist': False,
        'guard_security_challenge': True,
    }
    if action == 'self_heal_input':
        config.update({
            'value': value,
            'clear_first': True,
            'send_enter': False,
            'save_as': f'ai_input_{index}_result',
        })
    elif action == 'self_heal_click':
        config['save_as'] = f'ai_click_{index}_result'
    elif action == 'assert_exists':
        config['expected_exists'] = True
    return {
        'id': f'ai_step_{index}_{asset["id"]}',
        'type': action,
        'name': (
            f'输入 {asset["name"]}' if action == 'self_heal_input'
            else f'校验 {asset["name"]}' if action == 'assert_exists'
            else f'点击 {asset["name"]}'
        ),
        'kind': 'atomic',
        'config': config,
        'ai_metadata': {
            'generated': True,
            'element_id': asset['id'],
            'confidence': round(float(confidence if confidence is not None else asset['confidence']), 2),
            'reason': str(reason or '根据测试目标与元素语义生成')[:300],
            'locator_summary': [item['type'] for item in asset['locator_strategies']],
            'reviewed': False,
        },
    }


def _variable_names_in_value(value: Any) -> set:
    if isinstance(value, str):
        return {
            (match.group(1) or match.group(2)).strip()
            for match in re.finditer(r'\$\{\s*([^}]+)\s*\}|\{\{\s*([^}]+)\s*\}\}', value)
        }
    if isinstance(value, dict):
        names = set()
        for item in value.values():
            names.update(_variable_names_in_value(item))
        return names
    if isinstance(value, list):
        names = set()
        for item in value:
            names.update(_variable_names_in_value(item))
        return names
    return set()


def _normalize_draft_steps(
    raw_steps: Sequence[Dict[str, Any]],
    element_assets: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    asset_map = {item['id']: item for item in element_assets}
    steps = []
    variable_map: Dict[str, Dict[str, Any]] = {}
    warnings = []
    for raw in list(raw_steps or [])[:20]:
        if not isinstance(raw, dict):
            continue
        action = GENERATED_FLOW_ACTIONS.get(str(raw.get('action') or raw.get('type') or '').lower())
        if not action:
            continue
        if action in ('wait', 'screenshot', 'page_detect'):
            config = {}
            if action == 'wait':
                config['timeout'] = min(max(float(raw.get('timeout') or 1), 0.1), 30)
            elif action == 'screenshot':
                config['note'] = str(raw.get('note') or 'AI 流程执行证据')[:200]
            else:
                config.update({
                    'expected_page': str(raw.get('expected_page') or '')[:100],
                    'save_as': 'ai_page_detect_result',
                    'scope': 'local',
                    'page_rules': [],
                })
            steps.append({
                'id': f'ai_step_{len(steps) + 1}_{action}',
                'type': action,
                'name': str(raw.get('name') or {'wait': '等待页面稳定', 'screenshot': '保存执行截图', 'page_detect': '识别当前页面'}[action])[:100],
                'kind': 'atomic',
                'config': config,
                'ai_metadata': {
                    'generated': True,
                    'confidence': round(min(max(float(raw.get('confidence') or 0.7), 0), 1), 2),
                    'reason': str(raw.get('reason') or 'AI 流程辅助步骤')[:300],
                    'reviewed': False,
                },
            })
            continue
        try:
            element_id = int(raw.get('element_id'))
        except (TypeError, ValueError):
            continue
        asset = asset_map.get(element_id)
        if not asset:
            continue
        value = str(raw.get('value') or '')
        if action == 'self_heal_input':
            variable = _input_variable(asset['name'])
            if not _variable_names_in_value(value):
                variable = variable or {
                    'name': 'INPUT_VALUE',
                    'label': '输入内容',
                    'sensitive': False,
                    'reason': f'步骤“{asset["name"]}”需要输入测试数据',
                }
                value = f'${{{variable["name"]}}}'
            if variable:
                variable_map[variable['name']] = variable
        step = _draft_step_from_element(
            asset,
            action,
            len(steps) + 1,
            value=value,
            reason=str(raw.get('reason') or ''),
            confidence=raw.get('confidence'),
        )
        steps.append(step)
        if asset['confidence'] < 0.6:
            warnings.append(f'元素“{asset["name"]}”主要依赖坐标或弱定位策略，建议在编排器中补充语义定位。')

    steps = _ensure_verification_code_prerequisite_order(steps, element_assets)
    for variable_name in _variable_names_in_value(steps):
        variable_map.setdefault(variable_name, {
            'name': variable_name,
            'label': variable_name,
            'sensitive': any(word in variable_name for word in ('PASSWORD', 'TOKEN', 'CARD', 'ID_CARD', 'CODE')),
            'reason': 'AI Flow 使用的运行变量',
        })
    if steps and not any(step['type'] in ('assert_exists', 'page_detect') for step in steps):
        warnings.append('草稿暂未包含明确业务断言，请在编排器中补充成功状态校验。')
    if steps and not any(step['type'] == 'screenshot' for step in steps):
        steps.append({
            'id': f'ai_step_{len(steps) + 1}_screenshot',
            'type': 'screenshot',
            'name': '保存流程结果截图',
            'kind': 'atomic',
            'config': {'note': 'AI 生成流程结果证据'},
            'ai_metadata': {
                'generated': True,
                'confidence': 0.98,
                'reason': '保留人工评审与失败分析证据',
                'reviewed': False,
            },
        })
    return steps, list(variable_map.values()), list(dict.fromkeys(warnings))


def _compact_text(value: str) -> str:
    text = str(value or '').lower()
    for word in GENERIC_GOAL_WORDS:
        text = text.replace(word, ' ')
    return re.sub(r'[^0-9a-z\u4e00-\u9fff]+', '', text)


def _ngrams(value: str, size: int = 2) -> set:
    value = _compact_text(value)
    if not value:
        return set()
    if len(value) <= size:
        return {value}
    return {value[index:index + size] for index in range(len(value) - size + 1)}


def _asset_score(goal: str, asset: Dict[str, Any]) -> float:
    raw_goal = str(goal or '')
    goal_parts = [raw_goal]
    goal_parts.extend(re.split(r'(?:^|\s)\d+\s*[、.)）]|[\n；;]+', raw_goal))
    goal_parts.extend(re.findall(r'[【\[]([^】\]]+)[】\]]', raw_goal))
    goal_texts = list(dict.fromkeys(
        compact for part in goal_parts
        if len(compact := _compact_text(part)) >= 2
    ))
    if not goal_texts:
        return 0.0

    def score(candidate: str) -> float:
        candidate_text = _compact_text(candidate)
        if not candidate_text:
            return 0.0
        candidate_grams = _ngrams(candidate_text)
        scores = []
        for goal_text in goal_texts:
            goal_grams = _ngrams(goal_text)
            overlap = len(goal_grams & candidate_grams) / max(len(goal_grams), 1)
            similarity = SequenceMatcher(
                None,
                goal_text,
                candidate_text[: max(len(goal_text) * 3, 30)],
            ).ratio()
            containment = 0.65 if (
                goal_text in candidate_text or candidate_text in goal_text
            ) else 0.0
            scores.append(overlap * 0.8 + similarity * 0.35 + containment)
        return max(scores)

    title_score = score(f"{asset['name']} {asset['description']}")
    flow_score = score(asset.get('search_text', ''))
    if flow_score < FLOW_SEMANTIC_MIN_SCORE:
        flow_score = 0.0
    return round(max(title_score, flow_score), 4)


def _rule_select(goal: str, assets: Sequence[Dict[str, Any]]) -> Tuple[List[int], Dict[int, str]]:
    scored = [(asset, _asset_score(goal, asset)) for asset in assets]
    scored.sort(key=lambda item: item[1], reverse=True)
    if not scored or scored[0][1] < 0.22:
        return [], {}

    cutoff = max(0.22, scored[0][1] * 0.55)
    selected = [(asset, score) for asset, score in scored if score >= cutoff][:5]
    return (
        [asset['id'] for asset, _ in selected],
        {
            asset['id']: f'规则匹配度 {round(min(score, 1) * 100)}%'
            for asset, score in selected
        },
    )


def _extract_json_object(content: str) -> Dict[str, Any]:
    content = str(content or '').strip()
    if content.startswith('```'):
        content = re.sub(r'^```(?:json)?\s*', '', content, flags=re.I)
        content = re.sub(r'\s*```$', '', content)
    start = content.find('{')
    end = content.rfind('}')
    if start < 0 or end <= start:
        raise ValueError('模型响应中没有 JSON 对象')
    result = json.loads(content[start:end + 1])
    if not isinstance(result, dict):
        raise ValueError('模型响应不是 JSON 对象')
    return result


def _active_writer_config(user):
    try:
        from apps.requirement_analysis.models import AIModelConfig

        own = AIModelConfig.objects.filter(
            role='writer', is_active=True, created_by=user
        ).order_by('-updated_at').first()
        return own or AIModelConfig.objects.filter(
            role='writer', is_active=True
        ).order_by('-updated_at').first()
    except Exception as exc:
        logger.warning('读取 APP Agent AI 模型配置失败: %s', exc)
        return None


def _ai_select(
    task: AppAgentTask,
    assets: Sequence[Dict[str, Any]],
) -> Tuple[Optional[List[int]], Dict[int, str], str, str]:
    """调用现有 OpenAI-compatible writer 模型；失败时返回 None 触发规则降级。"""
    config = _active_writer_config(task.user)
    if not config:
        return None, {}, '', ''

    from apps.requirement_analysis.models import AIModelService

    compact_assets = [
        {
            'id': asset['id'],
            'name': asset['name'][:80],
            'desc': asset['description'][:120],
            'flow_semantics': asset.get('search_text', '')[:800],
            'steps': asset['step_count'],
            'package': asset['package_name'],
        }
        for asset in assets[:60]
    ]
    messages = [
        {
            'role': 'system',
            'content': (
                '你是APP测试任务规划器。只能选择输入中真实存在的case id，不能创建步骤。'
                '只输出JSON：{"case_ids":[1],"summary":"计划摘要",'
                '"reasons":{"1":"选择原因"}}。没有匹配用例时case_ids为空。'
                '执行范围只来自goal和cases，不得推断生产环境、沙箱或隔离账号。'
            ),
        },
        {
            'role': 'user',
            'content': json.dumps(
                {'goal': task.goal[:500], 'cases': compact_assets},
                ensure_ascii=False,
                separators=(',', ':'),
            ),
        },
    ]

    try:
        response = async_to_sync(AIModelService.call_openai_compatible_api)(
            config, messages, max_tokens=512
        )
        content = response['choices'][0]['message']['content']
        parsed = _extract_json_object(content)
        raw_ids = parsed.get('case_ids', [])
        if not isinstance(raw_ids, list):
            raise ValueError('case_ids 必须是数组')
        valid_ids = {asset['id'] for asset in assets}
        selected_ids = []
        for raw_id in raw_ids:
            try:
                case_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if case_id in valid_ids and case_id not in selected_ids:
                selected_ids.append(case_id)

        reasons_raw = parsed.get('reasons') or {}
        reasons = {
            case_id: str(reasons_raw.get(str(case_id)) or reasons_raw.get(case_id) or 'AI 识别为相关场景')[:200]
            for case_id in selected_ids
        }
        return (
            selected_ids,
            reasons,
            str(parsed.get('summary') or '')[:500],
            '',
        )
    except Exception as exc:
        logger.warning('APP Agent AI 规划失败，降级到规则规划: %s', exc)
        return None, {}, '', ''


def _rule_generate_draft(
    goal: str,
    element_assets: Sequence[Dict[str, Any]],
    *,
    force_all: bool = False,
) -> Tuple[List[Dict[str, Any]], str]:
    goal_text = str(goal or '').lower()
    domain_keywords = []
    for triggers, keywords in DOMAIN_ELEMENT_KEYWORDS:
        if any(trigger in goal_text for trigger in triggers):
            domain_keywords.extend(keywords)

    selected = []
    for asset in element_assets:
        searchable = f"{asset['name']} {' '.join(map(str, asset['tags']))} {asset['folder']}".lower()
        direct_score = _asset_score(goal, {
            'name': asset['name'],
            'description': f"{' '.join(map(str, asset['tags']))} {asset['folder']}",
        })
        domain_match = bool(domain_keywords and any(keyword.lower() in searchable for keyword in domain_keywords))
        if force_all or domain_match or direct_score >= 0.2:
            selected.append((asset, direct_score + (0.6 if domain_match else 0)))
    selected_ids = {asset['id'] for asset, _ in selected}
    selected_variables = {
        variable['name']
        for asset, _ in selected
        for variable in [_input_variable(asset['name'])]
        if variable
    }
    if {'PHONE', 'VERIFY_CODE'}.issubset(selected_variables):
        has_trigger = any(_is_verification_code_trigger_asset(asset) for asset, _ in selected)
        if not has_trigger:
            trigger_asset = next(
                (
                    asset for asset in element_assets
                    if asset['id'] not in selected_ids and _is_verification_code_trigger_asset(asset)
                ),
                None,
            )
            if trigger_asset:
                selected.append((trigger_asset, 0.85))
    selected.sort(key=lambda item: (_element_order(item[0]), -item[1]))
    selected = selected[:10]
    if not selected:
        return [], ''

    raw_steps = []
    for asset, score in selected:
        variable = _input_variable(asset['name'])
        lowered_name = str(asset['name']).lower()
        if variable:
            action = 'input'
            value = f'${{{variable["name"]}}}'
        elif any(word in lowered_name for word in ('成功', '首页', '结果', '个人中心', '我的')):
            action = 'assert_exists'
            value = ''
        else:
            action = 'click'
            value = ''
        raw_steps.append({
            'action': action,
            'element_id': asset['id'],
            'value': value,
            'confidence': round(min(asset['confidence'], max(score, 0.55)), 2),
            'reason': '元素名称、标签或目录与测试目标匹配',
        })
    return raw_steps, '基于元素语义和业务关键词生成草稿'


def _ai_generate_draft(
    task: AppAgentTask,
    element_assets: Sequence[Dict[str, Any]],
) -> Tuple[Optional[List[Dict[str, Any]]], str, str]:
    config = _active_writer_config(task.user)
    if not config:
        return None, '', ''
    from apps.requirement_analysis.models import AIModelService

    compact_elements = [
        {
            'id': item['id'],
            'name': item['name'][:80],
            'type': item['type'],
            'tags': item['tags'][:8],
            'folder': item['folder'][:50],
            'strategies': [strategy['type'] for strategy in item['locator_strategies']],
            'confidence': item['confidence'],
        }
        for item in element_assets[:80]
    ]
    messages = [
        {
            'role': 'system',
            'content': (
                '你是APP自动化Flow草稿规划器。只能引用输入中真实存在的element_id。'
                '允许action仅为click,input,assert_exists,wait,screenshot,page_detect。'
                'input的value必须使用${变量名}，禁止输出支付凭据或真实隐私数据。'
                '执行范围只来自goal、package和elements，不得添加生产环境、沙箱或隔离账号前提。'
                '若流程包含手机号输入、获取/发送验证码按钮、验证码输入，顺序必须是先输入手机号，'
                '再click获取/发送验证码，最后input验证码；获取/发送验证码按钮禁止用input。'
                '输出JSON：{"name":"用例名","summary":"摘要","steps":['
                '{"action":"input","element_id":1,"value":"${PHONE}","confidence":0.9,"reason":"原因"}],'
                '"warnings":["评审提示"]}。不要输出Markdown。'
            ),
        },
        {
            'role': 'user',
            'content': json.dumps(
                {
                    'goal': task.goal[:500],
                    'platform': task.device.platform,
                    'package': task.app_package.package_name if task.app_package else '',
                    'elements': compact_elements,
                },
                ensure_ascii=False,
                separators=(',', ':'),
            ),
        },
    ]
    try:
        response = async_to_sync(AIModelService.call_openai_compatible_api)(
            config, messages, max_tokens=1400
        )
        parsed = _extract_json_object(response['choices'][0]['message']['content'])
        raw_steps = parsed.get('steps')
        if not isinstance(raw_steps, list):
            raise ValueError('steps 必须是数组')
        warning_text = '；'.join(
            str(item)[:200] for item in (parsed.get('warnings') or []) if item
        )
        return raw_steps, str(parsed.get('name') or '')[:200], warning_text[:1000]
    except Exception as exc:
        logger.warning('APP Agent AI Flow 草稿生成失败，降级到规则生成: %s', exc)
        return None, '', ''


def build_generated_app_flow_draft(task: AppAgentTask) -> Optional[Dict[str, Any]]:
    """从授权元素资产生成必须人工评审的 APP Flow 草稿。"""
    element_assets = collect_app_element_assets(task)
    if not element_assets:
        return None

    reference_context = (task.plan or {}).get('reference_context') or {}
    referenced_objects = (
        reference_context.get('source_entry') == 'reference'
        and bool(reference_context.get('app_object_ids'))
    )
    if referenced_objects:
        raw_steps, summary = _rule_generate_draft(task.goal, element_assets, force_all=True)
        draft_name = ''
        ai_warning = ''
        source = 'reference'
        summary = 'Agent 根据引用的 APP 测试对象生成 Flow 草稿'
    else:
        raw_steps, draft_name, ai_warning = _ai_generate_draft(task, element_assets)
        source = 'ai'
        summary = 'AI 根据元素库生成 APP Flow 草稿'
        if raw_steps is None:
            raw_steps, summary = _rule_generate_draft(task.goal, element_assets)
            draft_name = ''
            source = 'rules'
    steps, required_variables, warnings = _normalize_draft_steps(raw_steps, element_assets)
    original_step_count = len(steps)
    steps = _strip_generated_ios_browser_bootstrap(task, steps)
    if len(steps) < original_step_count:
        warnings.append('已移除 iOS 浏览器启动和地址栏输入前置步骤，执行器会按配置自动打开起始 URL。')
        required_variables = [
            item for item in required_variables
            if item.get('name') in _variable_names_in_value(steps)
        ]
    if not steps or all(step['type'] == 'screenshot' for step in steps):
        return None

    confidence_values = [
        float(step.get('ai_metadata', {}).get('confidence') or 0)
        for step in steps if step['type'] not in ('screenshot', 'wait')
    ]
    overall_confidence = (
        round(sum(confidence_values) / len(confidence_values), 2)
        if confidence_values else 0
    )
    return {
        'mode': 'generated_flow',
        'source': source,
        'name': draft_name or f'AI草稿-{task.goal[:50]}',
        'description': f'由 APP AI Test Agent 根据目标生成：{task.goal}',
        'summary': summary,
        'flow': steps,
        'required_variables': required_variables,
        'warnings': list(dict.fromkeys(warnings)),
        'element_count': len(element_assets),
        'used_element_ids': sorted({
            step.get('config', {}).get('element_id')
            for step in steps if step.get('config', {}).get('element_id')
        }),
        'overall_confidence': overall_confidence,
        'review_required': True,
        'saved_case_id': None,
    }


def validate_generated_app_flow(
    task: AppAgentTask,
    flow: Sequence[Dict[str, Any]],
) -> List[str]:
    """保存草稿前验证动作、元素权限和变量引用。"""
    if not isinstance(flow, list) or not flow:
        raise ValueError('APP Flow 草稿不能为空')
    if len(flow) > 30:
        raise ValueError('APP Flow 草稿最多允许 30 个步骤')

    allowed_types = set(GENERATED_FLOW_ACTIONS.values())
    accessible_element_ids = {
        item['id'] for item in collect_app_element_assets(task)
    }
    variable_names = set()
    for index, step in enumerate(flow, 1):
        if not isinstance(step, dict):
            raise ValueError(f'第 {index} 个步骤格式无效')
        step_type = str(step.get('type') or '')
        if step_type not in allowed_types:
            raise ValueError(f'第 {index} 个步骤类型不允许：{step_type}')
        config = step.get('config') if isinstance(step.get('config'), dict) else {}
        if step_type in ('self_heal_click', 'self_heal_input', 'assert_exists'):
            try:
                element_id = int(config.get('element_id'))
            except (TypeError, ValueError):
                raise ValueError(f'第 {index} 个步骤缺少有效 element_id')
            if element_id not in accessible_element_ids:
                raise ValueError(f'第 {index} 个步骤引用了无权访问或不存在的元素')
        if step_type == 'self_heal_input' and not str(config.get('value') or ''):
            raise ValueError(f'第 {index} 个输入步骤缺少 value')
        variable_names.update(_variable_names_in_value(config))
    return sorted(variable_names)


def _configured_ios_browser_start_url() -> str:
    config = AppTestConfig.objects.order_by('id').first()
    return str(getattr(config, 'ios_browser_start_url', '') or '').strip()


def _strip_generated_ios_browser_bootstrap(
    task: AppAgentTask,
    flow: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    package_name = task.app_package.package_name if task.app_package else ''
    platform = task.device.platform if task.device else ''
    return strip_ios_browser_bootstrap_steps(
        flow,
        platform=platform,
        package_name=package_name,
        browser_start_url=_configured_ios_browser_start_url(),
    )


def prepare_generated_flow_for_save(
    task: AppAgentTask,
    *,
    self_heal_mode: str = 'report',
) -> Tuple[List[Dict[str, Any]], List[str]]:
    draft = task.plan.get('generated_draft') or {}
    flow = copy.deepcopy(draft.get('flow') or [])
    flow = _strip_generated_ios_browser_bootstrap(task, flow)
    if not flow:
        raise ValueError('生成结果只包含 iOS 浏览器启动/地址栏输入步骤，请重新生成或补充业务操作步骤')
    variable_names = validate_generated_app_flow(task, flow)
    if self_heal_mode not in ('report', 'persist'):
        raise ValueError('self_heal_mode 只能是 report 或 persist')
    for step in flow:
        if step.get('type') in ('self_heal_click', 'self_heal_input'):
            step.setdefault('config', {})['self_heal_mode'] = self_heal_mode
            # 坐标作为最后降级手段可以执行，但不允许自动持久化为主定位器。
            step['config']['allow_coordinate_persist'] = False
        metadata = step.setdefault('ai_metadata', {})
        metadata['reviewed'] = True
        metadata['reviewed_self_heal_mode'] = self_heal_mode
    return flow, variable_names


def build_app_agent_plan(task: AppAgentTask) -> Dict[str, Any]:
    """ReAct APP Agent：规划、执行、观察、反思和重试。"""
    assets = collect_app_case_assets(task)
    selected_ids = []
    reasons = {}
    ai_summary = ''
    ai_risk = ''
    source = 'rules'
    reference_context = (task.plan or {}).get('reference_context') or {}
    requested_case_ids = reference_context.get('app_case_ids') or []
    requested_object_ids = reference_context.get('app_object_ids') or []
    requested_api_refs = reference_context.get('api_test_objects') or reference_context.get('test_object_refs') or []
    structured_input = parse_agent_structured_input(task.goal)
    requirement_input = reference_context.get('source_entry') == 'requirement'
    exploration_targets = _goal_exploration_targets(task.goal) if requirement_input else []
    risk_notes = []
    if ai_risk:
        risk_notes.append(ai_risk)
    if task.device.status == 'locked' and task.device.locked_by_id != task.user_id:
        risk_notes.append('设备当前被其他用户锁定，执行前需要等待设备释放。')
    if task.device.status == 'offline':
        risk_notes.append('设备当前离线，暂时无法执行。')
    if requirement_input and structured_input and (
        structured_input['type'] == 'api_url' or not exploration_targets
    ):
        input_label = '前端 URL' if structured_input['type'] == 'frontend_url' else '接口 URL'
        return _attach_react_metadata(task, {
            'goal': task.goal,
            'mode': 'structured_input_analysis',
            'source': 'react_agent_planner',
            'planner_source': 'structured_input_parser',
            'summary': f'识别到{input_label}，将先解析目标内容，不按普通业务关键词匹配 APP 测试用例。',
            'selected_case_ids': [],
            'scenarios': [],
            'structured_input': structured_input,
            'input_parse_required': True,
            'coverage_gap': False,
            'coverage_message': '',
            'review_required': False,
            'risk_level': 'low',
            'risk_notes': risk_notes,
            'asset_count': len(assets),
            'asset_snapshot': [],
            'reference_context': reference_context,
        })
    if reference_context.get('source_entry') == 'reference' and requested_case_ids:
        available_ids = {asset['id'] for asset in assets}
        selected_ids = [case_id for case_id in requested_case_ids if case_id in available_ids]
        reasons = {case_id: '用户引用的 APP 测试用例' for case_id in selected_ids}
        ai_summary = f'已引用 {len(selected_ids)} 个 APP 测试用例，交由 Agent 分析并执行。'
        source = 'reference'
    elif reference_context.get('source_entry') == 'reference' and requested_api_refs:
        return _attach_react_metadata(task, {
            'goal': task.goal,
            'mode': 'api_reference',
            'source': 'react_agent_planner',
            'planner_source': 'reference',
            'summary': f'已引用 {len(requested_api_refs)} 个 API 测试对象，交由 Agent 分析接口来源与移动端链路关系。',
            'selected_case_ids': [],
            'scenarios': [],
            'api_test_objects': requested_api_refs,
            'test_object_refs': requested_api_refs,
            'coverage_gap': True,
            'coverage_message': '已保存 API 测试模块引用；APP 设备执行需要同时选择 APP 测试用例或保存生成的 APP Flow。',
            'review_required': False,
            'risk_level': 'medium',
            'risk_notes': list(dict.fromkeys([
                *risk_notes,
                '当前 APP 执行器不会直接发送 API 测试对象，请在统一 Agent 或 API 测试模块执行接口调试。',
            ])),
            'asset_count': len(assets),
            'asset_snapshot': [],
            'reference_context': reference_context,
            'device': {
                'id': task.device_id,
                'device_id': task.device.device_id,
                'name': task.device.name or task.device.device_id,
                'platform': task.device.platform,
                'version': task.device.ios_version if task.device.platform == 'ios' else task.device.android_version,
                'status': task.device.status,
            },
            'package': {
                'id': task.app_package_id,
                'name': task.app_package.name if task.app_package else '',
                'package_name': task.app_package.package_name if task.app_package else '',
            },
        })
    elif assets and not (
        reference_context.get('source_entry') == 'reference' and requested_object_ids
    ):
        selected_ids, reasons, ai_summary, ai_risk = _ai_select(task, assets)
        source = 'ai'
        if selected_ids is None:
            selected_ids, reasons = _rule_select(task.goal, assets)
            source = 'rules'
            if exploration_targets and not selected_ids:
                selected_assets = list(assets[:5])
                selected_ids = [asset['id'] for asset in selected_assets]
                reasons = {
                    asset['id']: '页面目标缺少业务关键词，规则规划保留项目内现有可执行用例'
                    for asset in selected_assets
                }
                ai_summary = (
                    f'模型规划不可用且页面目标无法按关键词匹配，'
                    f'已保留项目内 {len(selected_ids)} 个现有 APP 用例。'
                )

    asset_map = {asset['id']: asset for asset in assets}
    selected_ids = [case_id for case_id in selected_ids if case_id in asset_map]
    if ai_risk:
        risk_notes.append(ai_risk)

    scenarios = []
    for index, case_id in enumerate(selected_ids, 1):
        asset = asset_map[case_id]
        scenarios.append({
            'order': index,
            'case_id': case_id,
            'name': asset['name'],
            'description': asset['description'],
            'package_name': asset['package_name'],
            'step_count': asset['step_count'],
            'updated_at': asset.get('updated_at'),
            'flow_sha256': asset.get('flow_sha256'),
            'reason': reasons.get(case_id, '与测试目标匹配'),
        })

    uncovered_points = _uncovered_app_test_points(task.goal, assets, selected_ids)
    uncovered_non_exploration = [
        point for point in uncovered_points if not _normalize_exploration_url(point)
    ]
    if selected_ids and uncovered_non_exploration:
        coverage_message = (
            '现有 APP 测试用例未覆盖以下测试点：'
            f'{"、".join(uncovered_non_exploration)}。请补充或更新正式用例后重新规划执行。'
        )
        return _attach_react_metadata(task, {
            'goal': task.goal,
            'mode': 'coverage_gap',
            'source': 'react_agent_planner',
            'planner_source': source,
            'summary': f'已匹配 {len(selected_ids)} 个 APP 用例，但仅覆盖部分业务测试点。',
            'selected_case_ids': selected_ids,
            'scenarios': scenarios,
            'coverage_gap': True,
            'uncovered_test_points': uncovered_non_exploration,
            'coverage_message': coverage_message,
            'review_required': False,
            'risk_level': 'high',
            'risk_notes': list(dict.fromkeys([
                *risk_notes,
                '部分覆盖的 APP 用例不得作为完整业务需求执行通过。',
            ])),
            'asset_count': len(assets),
            'asset_snapshot': _selected_app_asset_snapshot(assets, selected_ids),
            'reference_context': reference_context,
            'device': {
                'id': task.device_id,
                'device_id': task.device.device_id,
                'name': task.device.name or task.device.device_id,
                'platform': task.device.platform,
                'version': task.device.ios_version if task.device.platform == 'ios' else task.device.android_version,
                'status': task.device.status,
            },
            'package': {
                'id': task.app_package_id,
                'name': task.app_package.name if task.app_package else '',
                'package_name': task.app_package.package_name if task.app_package else '',
            },
        })

    if exploration_targets:
        exploration_count = len(exploration_targets)
        case_count = len(selected_ids)
        mode = 'hybrid_cases_and_exploration' if selected_ids else 'exploration_targets'
        summary = (
            f'已匹配 {case_count} 个正式 APP 用例，并识别 {exploration_count} 个页面探索目标；'
            '将先执行正式用例，再逐个执行页面探索。'
            if selected_ids else
            f'已识别 {exploration_count} 个页面探索目标，将按输入顺序逐个执行自动探索。'
        )
        return _attach_react_metadata(task, {
            'goal': task.goal,
            'mode': mode,
            'source': 'react_agent_planner',
            'planner_source': source,
            'summary': summary,
            'selected_case_ids': selected_ids,
            'scenarios': scenarios,
            'exploration_targets': exploration_targets,
            'coverage_gap': False,
            'coverage_message': '',
            'review_required': False,
            'risk_level': 'medium',
            'risk_notes': list(dict.fromkeys([
                *risk_notes,
                '页面探索会保留动态操作证据，但不会把探索动作自动保存为正式测试用例。',
            ])),
            'asset_count': len(assets),
            'asset_snapshot': _selected_app_asset_snapshot(assets, selected_ids),
            'reference_context': reference_context,
            'device': {
                'id': task.device_id,
                'device_id': task.device.device_id,
                'name': task.device.name or task.device.device_id,
                'platform': task.device.platform,
                'version': task.device.ios_version if task.device.platform == 'ios' else task.device.android_version,
                'status': task.device.status,
            },
            'package': {
                'id': task.app_package_id,
                'name': task.app_package.name if task.app_package else '',
                'package_name': task.app_package.package_name if task.app_package else '',
            },
        })

    if not selected_ids:
        referenced_objects = (
            reference_context.get('source_entry') == 'reference'
            and bool(requested_object_ids)
        )
        allow_generated_draft = referenced_objects or not requirement_input
        draft = build_generated_app_flow_draft(task) if allow_generated_draft else None
        element_suggestions = build_agent_element_suggestions(task) if allow_generated_draft else []
        if draft:
            risk_notes.extend(draft.get('warnings') or [])
            return _attach_react_metadata(task, {
                'goal': task.goal,
                'mode': 'generated_flow',
                'source': 'react_agent_planner',
                'planner_source': draft['source'],
                'summary': '没有匹配的正式用例，已生成等待人工评审的 APP Flow 草稿。',
                'selected_case_ids': [],
                'scenarios': [],
                'coverage_gap': True,
                'coverage_message': '请检查每个步骤、定位置信度和测试变量，确认保存后才能执行。',
                'review_required': True,
                'generated_draft': draft,
                'risk_level': 'medium' if risk_notes else 'low',
                'risk_notes': list(dict.fromkeys(risk_notes)),
                'asset_count': len(assets),
                'asset_snapshot': [],
                'element_asset_count': draft['element_count'],
                'element_suggestions': element_suggestions,
                'reference_context': reference_context,
                'device': {
                    'id': task.device_id,
                    'device_id': task.device.device_id,
                    'name': task.device.name or task.device.device_id,
                    'platform': task.device.platform,
                    'version': task.device.ios_version if task.device.platform == 'ios' else task.device.android_version,
                    'status': task.device.status,
                },
                'package': {
                    'id': task.app_package_id,
                    'name': task.app_package.name if task.app_package else '',
                    'package_name': task.app_package.package_name if task.app_package else '',
                },
            })

        coverage_summary = (
            '已检索“测试用例”模块，但没有找到与当前业务需求匹配的可执行正式用例。'
            if requirement_input else
            '没有找到匹配的已有用例，元素库也不足以生成可评审的 APP Flow。'
        )
        coverage_message = (
            '已检索“测试用例”模块，未找到与当前业务需求匹配的正式 APP 测试用例。请先在“测试用例”模块创建或完善用例后重试。'
            if requirement_input else
            '请先补充相关 APP 元素，或在“用例编排”中创建正式测试用例。'
        )
        coverage_risk = (
            '业务需求输入只执行“测试用例”模块中的正式用例，不会自动生成未评审的元素或坐标步骤。'
            if requirement_input else
            '缺少可复用用例和相关元素资产，Agent 不会臆造不可审计的坐标步骤。'
        )
        return _attach_react_metadata(task, {
            'goal': task.goal,
            'mode': 'coverage_gap',
            'source': 'react_agent_planner',
            'planner_source': source,
            'summary': coverage_summary,
            'selected_case_ids': [],
            'scenarios': [],
            'coverage_gap': True,
            'coverage_message': coverage_message,
            'review_required': False,
            'risk_level': 'high',
            'risk_notes': list(dict.fromkeys([
                *risk_notes,
                coverage_risk,
            ])),
            'asset_count': len(assets),
            'asset_snapshot': [],
            'element_asset_count': 0,
            'element_suggestions': element_suggestions,
            'reference_context': reference_context,
        })

    summary = ai_summary or f'已从 {len(assets)} 个可执行资产中选择 {len(selected_ids)} 个用例。'

    return _attach_react_metadata(task, {
        'goal': task.goal,
        'mode': 'existing_cases',
        'source': 'react_agent_planner',
        'planner_source': source,
        'summary': summary,
        'selected_case_ids': selected_ids,
        'scenarios': scenarios,
        'coverage_gap': False,
        'coverage_message': '',
        'review_required': False,
        'risk_level': 'medium' if risk_notes else 'low',
        'risk_notes': risk_notes,
        'asset_count': len(assets),
        'asset_snapshot': _selected_app_asset_snapshot(assets, selected_ids),
        'reference_context': reference_context,
        'device': {
            'id': task.device_id,
            'device_id': task.device.device_id,
            'name': task.device.name or task.device.device_id,
            'platform': task.device.platform,
            'version': task.device.ios_version if task.device.platform == 'ios' else task.device.android_version,
            'status': task.device.status,
        },
        'package': {
            'id': task.app_package_id,
            'name': task.app_package.name if task.app_package else '',
            'package_name': task.app_package.package_name if task.app_package else '',
        },
    })


def validate_agent_execution(task: AppAgentTask) -> None:
    """在真正操作设备前重新验证可执行边界。"""
    if (task.plan or {}).get('coverage_gap'):
        raise RuntimeError((task.plan or {}).get('coverage_message') or 'APP 测试计划未完整覆盖业务测试点')
    task.device.refresh_from_db()
    if task.device.status == 'offline':
        raise RuntimeError(f'设备 {task.device.device_id} 当前离线')
    if task.device.status == 'locked' and task.device.locked_by_id != task.user_id:
        raise RuntimeError(f'设备 {task.device.device_id} 已被其他用户锁定')

    selected_ids = task.plan.get('selected_case_ids') or []
    if not selected_ids:
        raise RuntimeError('测试计划中没有可执行用例')
    actual_ids = set(
        AppTestCase.objects.filter(id__in=selected_ids).filter(
            Q(project=task.project) |
            Q(project__isnull=True, created_by=task.user)
        ).values_list('id', flat=True)
    )
    if task.app_package_id:
        actual_ids &= set(
            AppTestCase.objects.filter(
                id__in=selected_ids, app_package_id=task.app_package_id
            ).values_list('id', flat=True)
        )
    invalid_ids = [case_id for case_id in selected_ids if case_id not in actual_ids]
    if invalid_ids:
        raise RuntimeError(f'计划包含无效或越权用例 ID: {invalid_ids}')

    cases = AppTestCase.objects.filter(id__in=selected_ids)
    missing_variables = set()
    for test_case in cases:
        required = _variable_names_in_value(test_case.ui_flow)
        provided = {}
        if isinstance(test_case.variables, list):
            provided = {
                str(item.get('name')): item.get('value')
                for item in test_case.variables
                if isinstance(item, dict) and item.get('name')
            }
        elif isinstance(test_case.variables, dict):
            provided = test_case.variables
        missing_variables.update({
            name for name in required if provided.get(name) in (None, '')
        })
    if missing_variables:
        raise RuntimeError(
            f'测试用例缺少运行变量：{", ".join(sorted(missing_variables))}。'
            '请先在用例编排器中补充测试数据。'
        )


def _flow_step_names(test_case: AppTestCase) -> List[str]:
    flow = test_case.ui_flow if isinstance(test_case.ui_flow, list) else test_case.ui_flow.get('steps', [])
    names = []
    for index, step in enumerate(flow[:12], 1):
        if not isinstance(step, dict):
            names.append(f'{index}. 执行步骤')
            continue
        name = step.get('name') or step.get('label') or step.get('type') or step.get('action') or '执行步骤'
        names.append(f'{index}. {name}')
    return names


def _failure_category(execution: AppTestExecution) -> Tuple[str, str]:
    message = (execution.error_message or '').lower()
    if re.search(r'crash|崩溃|fatal exception|anr', message):
        return 'app_crash', '应用崩溃或无响应'
    if re.search(r'adb|appium|wda|device|设备|connection|连接|environment', message):
        return 'environment', '设备或执行环境异常'
    if re.search(r'element|locator|xpath|selector|not found|找不到|定位', message):
        return 'locator', '元素定位失败'
    if re.search(r'timeout|timed out|超时', message):
        return 'timeout', '执行超时'
    if execution.status == 'completed' and execution.result == 'failed':
        return 'assertion', '业务步骤或断言失败'
    return 'runtime', '执行器异常'


def _locator_repair_suggestions(
    test_case: Optional[AppTestCase],
    error_message: str,
) -> List[Dict[str, Any]]:
    """基于失败用例引用的元素，生成不自动落库的定位修复建议。"""
    if not test_case:
        return []
    flow = test_case.ui_flow if isinstance(test_case.ui_flow, list) else test_case.ui_flow.get('steps', [])
    referenced = []
    for index, step in enumerate(flow, 1):
        if not isinstance(step, dict):
            continue
        config = step.get('config') if isinstance(step.get('config'), dict) else {}
        element_id = config.get('element_id') or step.get('element_id')
        try:
            element_id = int(element_id)
        except (TypeError, ValueError):
            continue
        referenced.append((index, step, element_id))
    if not referenced:
        return []

    element_map = {
        item.id: item
        for item in AppElement.objects.filter(id__in=[item[2] for item in referenced], is_active=True)
    }
    error_lower = str(error_message or '').lower()
    suggestions = []
    stable_order = ['resource_id', 'accessibility', 'text', 'xpath', 'ocr', 'image', 'position']
    for step_index, step, element_id in referenced:
        element = element_map.get(element_id)
        if not element:
            continue
        strategies = _element_strategy_summary(element)
        current_order = [item['type'] for item in strategies]
        proposed_order = [item for item in stable_order if item in current_order]
        matched_error = element.name.lower() in error_lower or str(element.id) in error_lower
        coordinate_only = not current_order or set(current_order).issubset({'position'})
        if coordinate_only:
            recommendation = '补充 resource-id、accessibility 或稳定文本定位；坐标仅保留为最后降级策略。'
        elif current_order != proposed_order:
            recommendation = f'建议按 {" → ".join(proposed_order)} 的稳定性顺序重排定位策略。'
        elif len(current_order) < 2:
            recommendation = '当前只有单一定位策略，建议补充 OCR、图像或归一化坐标作为降级链路。'
        else:
            recommendation = '定位链已具备多策略降级；建议针对当前目标用 report 模式验证命中结果后再持久化。'
        selected_strategy = next((item for item in proposed_order if item != 'position'), proposed_order[0] if proposed_order else '')
        suggestions.append({
            'step_index': step_index,
            'step_name': step.get('name') or step.get('type') or f'步骤 {step_index}',
            'element_id': element.id,
            'element_name': element.name,
            'current_strategy_order': current_order,
            'proposed_strategy_order': proposed_order,
            'recommended_primary': selected_strategy,
            'old_locator': {
                'selector_type': current_order[0] if current_order else element.element_type,
                'selector': strategies[0]['value'] if strategies else '',
            },
            'new_locator': {
                'selector_type': selected_strategy,
                'selector': next(
                    (item['value'] for item in strategies if item['type'] == selected_strategy),
                    '',
                ),
            },
            'recommendation': recommendation,
            'confidence': 0.9 if matched_error else (0.72 if len(referenced) == 1 else 0.52),
            'safe_to_auto_persist': bool(selected_strategy and selected_strategy != 'position'),
            'apply_mode': 'persist_after_review',
            'evidence': {
                'error_mentions_element': matched_error,
                'coordinate_only': coordinate_only,
                'self_heal_history': (element.config or {}).get('self_heal') or {},
            },
        })
    suggestions.sort(key=lambda item: item['confidence'], reverse=True)
    return suggestions[:5]


def analyze_app_agent_results(executions: Iterable[AppTestExecution]) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
    """聚合执行结果，并由 AI 化分析生成根因、定位修复和缺陷草稿。"""
    executions = list(executions)
    passed = [item for item in executions if item.status == 'completed' and item.result == 'passed']
    skipped = [item for item in executions if item.status == 'completed' and item.result == 'skipped']
    stopped = [item for item in executions if item.status == 'stopped']
    failed = [
        item for item in executions
        if item.status == 'error' or (item.status == 'completed' and item.result == 'failed')
    ]

    summary = {
        'total': len(executions),
        'passed': len(passed),
        'failed': len(failed),
        'skipped': len(skipped),
        'stopped': len(stopped),
        'pass_rate': round(len(passed) / len(executions) * 100, 2) if executions else 0,
        'execution_ids': [item.id for item in executions],
        'has_failures': bool(failed),
    }

    categories: Dict[str, int] = {}
    failure_items = []
    defect_drafts = []
    all_repair_suggestions = []
    for execution in failed:
        category, category_label = _failure_category(execution)
        categories[category] = categories.get(category, 0) + 1
        device = execution.device
        test_case = execution.test_case
        platform = device.platform if device else 'unknown'
        version = ''
        if device:
            version = device.ios_version if platform == 'ios' else device.android_version
        actual = execution.error_message or f'共 {execution.failed_steps} 个步骤失败'
        if execution.report_path:
            from .report_urls import build_public_report_path
            report_url = build_public_report_path(execution.id)
        else:
            report_url = ''
        repair_suggestions = (
            _locator_repair_suggestions(test_case, actual)
            if category == 'locator' else []
        )
        for suggestion in repair_suggestions:
            all_repair_suggestions.append({
                'execution_id': execution.id,
                'case_id': test_case.id if test_case else None,
                **suggestion,
            })
        failure_items.append({
            'execution_id': execution.id,
            'case_id': test_case.id if test_case else None,
            'case_name': test_case.name if test_case else '未知用例',
            'category': category,
            'category_label': category_label,
            'actual': actual,
            'failed_steps': execution.failed_steps,
            'report_url': report_url,
            'repair_suggestions': repair_suggestions,
            'ai_analysis': {
                'failure_reason': (
                    '登录按钮DOM发生变化或定位链未命中' if category == 'locator'
                    else category_label
                ),
                'old_locator': repair_suggestions[0]['old_locator'] if repair_suggestions else {},
                'new_locator': repair_suggestions[0]['new_locator'] if repair_suggestions else {},
                'suggestion': repair_suggestions[0]['recommendation'] if repair_suggestions else '结合日志、截图和报告复核失败原因。',
            },
        })
        defect_drafts.append({
            'title': f'[{platform} {version}] {test_case.name if test_case else "APP用例"}执行失败'.replace('  ', ' '),
            'severity': 'critical' if category == 'app_crash' else ('high' if execution.status == 'error' else 'medium'),
            'category': category,
            'environment': {
                'device': device.name or device.device_id if device else '',
                'device_id': device.device_id if device else '',
                'platform': platform,
                'version': version,
                'package': test_case.app_package.package_name if test_case and test_case.app_package else '',
            },
            'preconditions': '按用户输入和引用的本地测试用例前置条件执行。',
            'steps': _flow_step_names(test_case) if test_case else [],
            'expected': '用例中的所有操作和断言均执行通过。',
            'actual': actual,
            'evidence': {
                'execution_id': execution.id,
                'allure_report': report_url,
                'report_path': execution.report_path,
                'total_steps': execution.total_steps,
                'failed_steps': execution.failed_steps,
                'repair_suggestions': repair_suggestions,
            },
        })

    analysis = {
        'agent': 'AI Failure Analyzer',
        'react_stage': 'Reflection',
        'summary': (
            f'共执行 {len(executions)} 个用例，{len(passed)} 个通过，{len(failed)} 个失败。'
            if executions else '没有产生执行记录。'
        ),
        'categories': categories,
        'items': failure_items,
        'repair_suggestions': all_repair_suggestions,
        'locator_change_examples': [
            {
                'failure_reason': item.get('ai_analysis', {}).get('failure_reason'),
                'old_locator': item.get('ai_analysis', {}).get('old_locator'),
                'new_locator': item.get('ai_analysis', {}).get('new_locator'),
                'suggestion': item.get('ai_analysis', {}).get('suggestion'),
            }
            for item in failure_items
            if item.get('ai_analysis')
        ],
        'recommendations': (
            [
                '优先处理应用崩溃与环境异常，再复核定位或业务断言失败。',
                '定位修复建议默认只生成草稿；启用 persist 前必须人工确认，坐标策略不会被自动持久化。',
            ] if failed else ['本次执行未发现失败。']
        ),
    }
    return summary, analysis, defect_drafts
