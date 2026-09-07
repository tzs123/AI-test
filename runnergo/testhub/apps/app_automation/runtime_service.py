# -*- coding: utf-8 -*-
"""APP Agent 三期：有界、可审批、可审计的观察-决策-执行闭环。"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from asgiref.sync import async_to_sync
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .agent_service import (
    _active_writer_config,
    _extract_json_object,
    collect_app_element_assets,
    record_agent_event,
)
from .database import retry_database_write
from .models import (
    AppAgentRuntimeSession,
    AppAgentRuntimeStep,
    AppElement,
    AppLocatorKnowledge,
    AppTestCase,
)
from .media_urls import build_public_evidence_path
from .runtime_telemetry import collect_runtime_telemetry, merge_telemetry_summary

logger = logging.getLogger(__name__)

ACTIVE_SESSION_STATUSES = {'pending', 'running', 'awaiting_approval'}
EXPLORATORY_SESSION_MODE = 'exploratory'
RUNTIME_ELEMENT_ACTIONS = {
    'click': 'self_heal_click',
    'touch': 'self_heal_click',
    'smart_click': 'self_heal_click',
    'self_heal_click': 'self_heal_click',
    'input': 'self_heal_input',
    'input_text': 'self_heal_input',
    'smart_input': 'self_heal_input',
    'self_heal_input': 'self_heal_input',
    'assert_exists': 'assert_exists',
}
RUNTIME_COMPONENT_ACTIONS = {
    'license_plate_input',
    'upload_image',
    'identity_verify',
}
RUNTIME_DIRECT_ACTIONS = {'checkbox_toggle'}
RUNTIME_PASSIVE_ACTIONS = {'wait', 'screenshot', 'page_detect', 'back'}
STATE_CHANGING_ACTIONS = {
    'self_heal_click', 'self_heal_input', 'click', 'input', 'smart_input',
    'license_plate_input', 'upload_image', 'identity_verify', 'swipe', 'back',
}
EXPLORATORY_INPUT_KEYS = (
    ('password', ('密码', 'password', 'passwd', 'pwd', 'pin'), True),
    ('phone', ('手机号', '手机号码', 'mobile', 'phone', 'tel'), True),
    ('verify_code', ('验证码', '校验码', 'otp', 'captcha', 'verify', 'verification'), True),
    ('license_plate', ('车牌号', '车牌号码', 'license plate', 'license_plate'), True),
    ('keyword', ('搜索', 'search', '关键词', 'keyword', 'query'), False),
    ('default_text', ('',), False),
)
DEFAULT_EXPLORATION_INPUT_VALUES = {
    'keyword': 'RunnerGo',
    'default_text': 'runnergo-test',
    'phone': '13800138000',
    'password': 'RunnerGo@123',
    'license_plate': '京A12345',
}


def _save_runtime_state(instance, update_fields: Sequence[str]) -> None:
    """Persist runtime progress without losing a completed device action to a brief lock."""
    retry_database_write(
        lambda: instance.save(update_fields=list(update_fields))
    )

# Only one-time verification input pauses execution.
IOS_KEYBOARD_CONTROL_LABELS = {
    '上一个', '下一个', '完成', '听写', 'dictation', 'shift', '删除',
    'return', 'space', '空格', '换行', '收起键盘',
}
BROWSER_CHROME_LABELS = {
    '返回', 'backbutton', '页面菜单', 'pageformatmenubutton', '刷新',
    'reloadbutton', '更多', 'moremenubutton', '地址', 'tabbaritemtitle',
}
CRASH_PATTERNS = (
    '已停止运行', '无响应', '应用崩溃', 'keeps stopping', "isn't responding", 'fatal exception', 'anr',
)


def _clamp(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def normalized_runtime_budgets(data: Dict[str, Any]) -> Dict[str, int]:
    """对外部提交的运行预算做硬限制。"""
    return {
        'max_steps': _clamp(data.get('max_steps'), 1, 50, 20),
        'max_duration_seconds': _clamp(data.get('max_duration_seconds'), 30, 3600, 600),
        'max_model_tokens': _clamp(data.get('max_model_tokens'), 1000, 50000, 6000),
        'repeat_state_limit': _clamp(data.get('repeat_state_limit'), 2, 8, 3),
    }


def is_exploratory_session(session: AppAgentRuntimeSession) -> bool:
    """判断会话是否为无脚本自动探索模式。"""
    return (session.coverage or {}).get('mode') == EXPLORATORY_SESSION_MODE


def normalized_exploration_settings(
    data: Dict[str, Any],
    *,
    goal: str = '',
) -> Dict[str, Any]:
    """整理自动探索运行参数，避免前端或 API 传入无限制动作空间。"""
    raw_inputs = data.get('exploration_inputs') or {}
    if not isinstance(raw_inputs, dict):
        raw_inputs = {}
    input_values = {}
    for key in ('keyword', 'default_text', 'phone', 'password', 'license_plate'):
        value = str(raw_inputs.get(key) or '').strip()
        if value:
            input_values[key] = value[:120]
    goal_text = str(goal or '')
    goal_input_patterns = {
        'phone': r'(?:手机号|手机号码|phone|mobile)\s*[:：=为是]?\s*(1[3-9]\d{9})',
        'password': r'(?:密码|password|passwd|pwd)\s*[:：=为是]\s*([^\s，,。；;【】]{1,120})',
        'keyword': r'(?:搜索词|搜索内容|关键词|keyword)\s*[:：=为是]\s*([^\n，,。；;【】]{1,120})',
        'default_text': r'(?:输入内容|默认文本|default text)\s*[:：=为是]\s*([^\n，,。；;【】]{1,120})',
    }
    for key, pattern in goal_input_patterns.items():
        if key in input_values:
            continue
        matched = re.search(pattern, goal_text, re.I)
        if matched:
            input_values[key] = matched.group(1).strip()[:120]
    if 'license_plate' not in input_values:
        plate_match = re.search(
            r'[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼]'
            r'[A-HJ-NP-Z][A-HJ-NP-Z0-9]{5,6}',
            str(goal or '').upper(),
        )
        if plate_match:
            input_values['license_plate'] = plate_match.group(0)
    for key, value in DEFAULT_EXPLORATION_INPUT_VALUES.items():
        input_values.setdefault(key, value)
    return {
        'max_candidates_per_page': _clamp(data.get('max_candidates_per_page'), 3, 20, 12),
        'input_values': input_values,
        'allow_generic_text_input': bool(data.get('allow_generic_text_input', True)),
    }


def validate_exploratory_runtime_start(task) -> None:
    """自动探索不依赖正式用例，但仍然必须独占一台可执行设备。"""
    device = task.device
    if device.status == 'offline':
        raise RuntimeError(f'设备 {device.device_id} 当前离线')
    if device.status == 'locked' and device.locked_by_id != task.user_id:
        raise RuntimeError(f'设备 {device.device_id} 已被其他用户锁定')


def create_exploratory_runtime_session(
    task,
    data: Optional[Dict[str, Any]] = None,
) -> AppAgentRuntimeSession:
    """为已有任务创建可直接执行的无脚本探索会话。"""
    data = data or {}
    validate_exploratory_runtime_start(task)
    budgets = normalized_runtime_budgets(data)
    try:
        telemetry_interval = int(data.get('telemetry_interval_steps') or 2)
    except (TypeError, ValueError):
        telemetry_interval = 2
    budgets.update({
        'telemetry_enabled': bool(data.get('telemetry_enabled', True)),
        'telemetry_interval_steps': max(1, min(telemetry_interval, 10)),
    })
    exploration = normalized_exploration_settings(data, goal=task.goal)
    target_url = str(data.get('target_url') or '').strip()
    session = retry_database_write(
        lambda: AppAgentRuntimeSession.objects.create(
            task=task,
            coverage={
                'mode': EXPLORATORY_SESSION_MODE,
                'target_url': target_url,
                'candidate_total': 0,
                'candidate_completed': 0,
                'pages': [],
                'edges': [],
                'page_count': 0,
                'edge_count': 0,
                'exploration': exploration,
            },
            **budgets,
        )
    )
    task.status = 'runtime_executing'
    task.progress = max(task.progress, 40)
    task.error_message = ''
    task.finished_at = None
    _save_runtime_state(task, [
        'status', 'progress', 'error_message', 'finished_at', 'updated_at'
    ])
    record_agent_event(
        task,
        'explore_queued',
        f'自动探索测试会话 #{session.id} 已进入队列',
        payload={
            'session_id': session.id,
            'mode': EXPLORATORY_SESSION_MODE,
            'target_url': target_url,
            **budgets,
            'exploration': exploration,
        },
    )
    return session


def queue_next_planned_exploration(
    task,
    *,
    reset: bool = False,
) -> Optional[AppAgentRuntimeSession]:
    """Queue the next explicit page target from a mixed APP Agent plan."""
    task.refresh_from_db()
    plan = copy.deepcopy(task.plan or {})
    targets = list(plan.get('exploration_targets') or [])
    if not targets:
        return None
    if task.runtime_sessions.filter(status__in=ACTIVE_SESSION_STATUSES).exists():
        raise RuntimeError('当前已有运行中的页面探索会话')

    state = {} if reset else copy.deepcopy(plan.get('exploration_run') or {})
    if reset:
        state = {
            'next_index': 0,
            'session_ids': [],
            'results': [],
            'status': 'running',
        }
    target_index = int(state.get('next_index') or 0)
    if target_index >= len(targets):
        return None
    target = targets[target_index]
    target_url = str(target.get('url') or '').strip()
    if not target_url:
        raise RuntimeError(f'第 {target_index + 1} 个页面探索目标缺少 URL')

    session = create_exploratory_runtime_session(task, {
        'target_url': target_url,
    })
    coverage = copy.deepcopy(session.coverage or {})
    coverage.update({
        'planned_exploration': True,
        'target_index': target_index,
        'target_label': target.get('label') or target_url,
    })
    session.coverage = coverage
    _save_runtime_state(session, ['coverage', 'updated_at'])

    state['next_index'] = target_index + 1
    state['session_ids'] = [*(state.get('session_ids') or []), session.id]
    state['status'] = 'running'
    plan['exploration_run'] = state
    task.plan = plan
    _save_runtime_state(task, ['plan', 'updated_at'])
    record_agent_event(
        task,
        'explore_target_queued',
        f'页面探索 {target_index + 1}/{len(targets)} 已进入队列：{target_url}',
        payload={
            'status': 'pending',
            'session_id': session.id,
            'target_index': target_index,
            'target_count': len(targets),
            'target_url': target_url,
        },
    )
    try:
        from .tasks import execute_app_agent_runtime_task

        celery_task = execute_app_agent_runtime_task.delay(session.id)
    except Exception as exc:
        session.status = 'failed'
        session.error_message = f'页面探索会话提交失败: {exc}'
        session.finished_at = timezone.now()
        _save_runtime_state(session, [
            'status', 'error_message', 'finished_at', 'updated_at'
        ])
        raise RuntimeError(session.error_message) from exc
    session.celery_task_id = celery_task.id
    _save_runtime_state(session, ['celery_task_id', 'updated_at'])
    task.task_id = celery_task.id
    _save_runtime_state(task, ['task_id', 'updated_at'])
    return session


def _flow_steps(test_case: AppTestCase) -> List[Dict[str, Any]]:
    if isinstance(test_case.ui_flow, list):
        return test_case.ui_flow
    if isinstance(test_case.ui_flow, dict):
        steps = test_case.ui_flow.get('steps')
        return steps if isinstance(steps, list) else []
    return []


def collect_runtime_candidates(task) -> List[Dict[str, Any]]:
    """只从任务已确认用例和授权元素中创建可执行候选动作。"""
    raw_ids = task.plan.get('selected_case_ids') or []
    selected_ids = []
    for raw_id in raw_ids:
        try:
            case_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if case_id not in selected_ids:
            selected_ids.append(case_id)

    case_queryset = AppTestCase.objects.filter(id__in=selected_ids).filter(
        Q(project=task.project) | Q(project__isnull=True, created_by=task.user)
    ).select_related('app_package')
    case_map = {case.id: case for case in case_queryset}
    element_assets = collect_app_element_assets(task)
    element_ids = {item['id'] for item in element_assets}
    elements = {
        element.id: element
        for element in AppElement.objects.filter(id__in=element_ids, is_active=True)
    }

    candidates = []
    for case_id in selected_ids:
        test_case = case_map.get(case_id)
        if not test_case:
            continue
        for step_index, raw_step in enumerate(_flow_steps(test_case), 1):
            if not isinstance(raw_step, dict):
                continue
            source_type = str(raw_step.get('type') or raw_step.get('action') or '').strip().lower()
            action_type = RUNTIME_ELEMENT_ACTIONS.get(source_type)
            config = raw_step.get('config') if isinstance(raw_step.get('config'), dict) else {}
            element_id = config.get('element_id') or raw_step.get('element_id')
            element = None
            if action_type:
                try:
                    element_id = int(element_id)
                except (TypeError, ValueError):
                    continue
                element = elements.get(element_id)
                if not element:
                    continue
            elif source_type in RUNTIME_COMPONENT_ACTIONS | RUNTIME_DIRECT_ACTIONS | RUNTIME_PASSIVE_ACTIONS:
                action_type = source_type
                element_id = None
            else:
                continue

            name = str(raw_step.get('name') or raw_step.get('label') or action_type)[:200]
            metadata = raw_step.get('ai_metadata') if isinstance(raw_step.get('ai_metadata'), dict) else {}
            try:
                confidence = float(metadata.get('confidence') or 0.75)
            except (TypeError, ValueError):
                confidence = 0.75
            candidate = {
                'candidate_id': f'case:{test_case.id}:step:{step_index}',
                'case_id': test_case.id,
                'case_name': test_case.name,
                'step_index': step_index,
                'name': name,
                'action_type': action_type,
                'element_id': element.id if element else None,
                'element_name': element.name if element else '',
                'confidence': max(0.0, min(confidence, 1.0)),
                '_source_step': copy.deepcopy(raw_step),
                '_variables': copy.deepcopy(test_case.variables or []),
            }
            candidate['approval_category'] = runtime_approval_category(candidate)
            candidates.append(candidate)
    return candidates


def public_runtime_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    action = {
        key: value
        for key, value in candidate.items()
        if not str(key).startswith('_')
    }
    if not action.get('step_config') and candidate.get('_source_step'):
        action['step_config'] = copy.deepcopy(candidate['_source_step'])
    if runtime_approval_requires_input(action):
        action = redact_runtime_approval_input(action)
    return action


def runtime_approval_requires_input(action: Dict[str, Any], category: str = '') -> bool:
    """验证码输入必须由审批人提供，不能使用探索阶段的占位值。"""
    approval_category = str(category or action.get('approval_category') or '')
    action_type = str(action.get('action_type') or '').strip().lower()
    step_config = action.get('step_config') if isinstance(action.get('step_config'), dict) else {}
    input_kind = str(
        action.get('input_kind')
        or step_config.get('input_kind')
        or (step_config.get('config') or {}).get('input_kind')
        or ''
    ).strip().lower()
    action_text = json.dumps({
        'name': action.get('name'),
        'element_name': action.get('element_name'),
        'step_config': step_config,
    }, ensure_ascii=False, default=str).casefold()
    verification_input = input_kind in ('verify_code', 'otp', 'captcha') or any(
        keyword in action_text for keyword in ('验证码', '动态码', 'otp', 'captcha')
    )
    return (
        approval_category == 'security_challenge'
        and action_type in ('input', 'smart_input', 'self_heal_input')
        and verification_input
    )


def redact_runtime_approval_input(action: Dict[str, Any]) -> Dict[str, Any]:
    """移除序列化或审计数据中的一次性验证码。"""
    redacted = copy.deepcopy(action or {})
    redacted.pop('value', None)
    step_config = redacted.get('step_config')
    if isinstance(step_config, dict):
        step_config.pop('value', None)
        config = step_config.get('config')
        if isinstance(config, dict):
            config.pop('value', None)
    return redacted


def runtime_approval_input_spec(action: Dict[str, Any], category: str = '') -> Optional[Dict[str, Any]]:
    if not runtime_approval_requires_input(action, category):
        return None
    return {
        'name': 'input_value',
        'label': '验证码',
        'placeholder': '请输入当前收到的验证码',
        'required': True,
        'input_type': 'text',
        'max_length': 120,
        'autocomplete': 'one-time-code',
    }


def apply_runtime_approval_input(action: Dict[str, Any], value: str) -> Dict[str, Any]:
    updated = copy.deepcopy(action or {})
    step_config = updated.get('step_config')
    if not isinstance(step_config, dict):
        raise ValueError('待审批动作缺少可执行步骤配置，请刷新任务后重试')
    step_config['value'] = value
    config = step_config.get('config')
    if isinstance(config, dict):
        config['value'] = value
    return updated


def runtime_approval_category(candidate: Dict[str, Any]) -> str:
    """只对需要用户提供一次性验证码的输入动作暂停执行。"""
    source_step = candidate.get('_source_step') or candidate.get('step_config') or {}
    rule_text = ' '.join((
        str(candidate.get('name') or ''),
        str(candidate.get('element_name') or ''),
        str(candidate.get('case_name') or ''),
        str(candidate.get('action_type') or ''),
    )).casefold()
    text = ' '.join((
        rule_text,
        json.dumps(source_step, ensure_ascii=False, default=str),
    )).casefold()
    action_type = str(candidate.get('action_type') or '').strip().lower()
    input_kind = str(
        candidate.get('input_kind')
        or (source_step.get('input_kind') if isinstance(source_step, dict) else '')
        or ''
    ).strip().lower()
    verification_input = input_kind in ('verify_code', 'otp', 'captcha') or any(
        keyword in text for keyword in ('验证码', '动态码', 'otp', 'captcha')
    )
    if action_type in ('input', 'smart_input', 'self_heal_input') and verification_input:
        return 'security_challenge'
    return ''


def validate_runtime_candidate_set(task) -> List[Dict[str, Any]]:
    candidates = collect_runtime_candidates(task)
    if not candidates:
        raise RuntimeError(
            '当前计划没有可供自治 Agent 单步执行的受控动作。'
            '请先为用例步骤绑定项目元素，并使用点击、输入、断言、等待、截图或页面识别组件。'
        )
    return candidates


def _node_bounds(node: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    bounds = node.get('bounds') or {}
    try:
        x1 = float(bounds.get('x1'))
        y1 = float(bounds.get('y1'))
        x2 = float(bounds.get('x2'))
        y2 = float(bounds.get('y2'))
    except (AttributeError, TypeError, ValueError):
        return None
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _node_center(node: Dict[str, Any]) -> Optional[List[int]]:
    bounds = _node_bounds(node)
    if not bounds:
        return None
    x1, y1, x2, y2 = bounds
    return [int((x1 + x2) / 2), int((y1 + y2) / 2)]


def _node_label(node: Dict[str, Any]) -> str:
    class_name = str(node.get('class_name') or '').casefold()
    keys = (
        ('content_desc', 'resource_id', 'text', 'placeholder', 'value')
        if any(token in class_name for token in ('switch', 'checkbox', 'toggle'))
        else ('text', 'content_desc', 'placeholder', 'value', 'resource_id')
    )
    for key in keys:
        value = str(node.get(key) or '').strip()
        if value:
            return value.rsplit('/', 1)[-1][:80]
    return str(node.get('class_name') or '未命名控件')[:80]


def _input_node_label(node: Dict[str, Any]) -> str:
    """输入框优先使用稳定语义，避免已填 value 被误当成字段名称。"""
    for key in ('content_desc', 'resource_id', 'placeholder', 'text', 'value'):
        value = str(node.get(key) or '').strip()
        if value:
            return value.rsplit('/', 1)[-1][:80]
    return str(node.get('class_name') or '未命名输入框')[:80]


def _node_text(node: Dict[str, Any]) -> str:
    return ' '.join(
        str(node.get(key) or '').strip()
        for key in ('text', 'value', 'content_desc', 'placeholder')
        if node.get(key)
    ).strip()


def _is_excluded_exploratory_click(
    node: Dict[str, Any],
    *,
    platform: str,
    screen_size: Optional[Tuple[float, float]],
    keyboard_visible: bool,
) -> bool:
    """过滤浏览器外壳、系统键盘和备案页脚等非业务探索目标。"""
    label = re.sub(r'\s+', '', _node_label(node)).casefold()
    text = re.sub(r'\s+', '', _node_text(node)).casefold()
    if label in BROWSER_CHROME_LABELS:
        return True
    if re.search(r'(?:icp备|icp证|icp\d|公安备案|copyright|版权所有)', text, re.I):
        return True
    if platform != 'ios' or not keyboard_visible or not screen_size:
        return False
    bounds = _node_bounds(node)
    if not bounds:
        return False
    _screen_width, screen_height = screen_size
    _x1, y1, _x2, y2 = bounds
    if max(y1, y2) < screen_height * 0.52:
        return False
    class_name = str(node.get('class_name') or '').casefold()
    keyboard_label = label in IOS_KEYBOARD_CONTROL_LABELS or bool(
        re.fullmatch(r'[0-9a-z.+*#]{1,3}', label, re.I)
    )
    return keyboard_label or any(token in class_name for token in ('keyboard', 'keycap'))


def _node_signature(state_hash: str, node: Dict[str, Any], action_type: str) -> str:
    payload = {
        'state': state_hash,
        'action_type': action_type,
        'label': _node_label(node),
        'bounds': node.get('bounds') or {},
        'resource_id': node.get('resource_id') or '',
        'class_name': node.get('class_name') or '',
    }
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
    ).hexdigest()[:16]


def _is_input_node(node: Dict[str, Any]) -> bool:
    class_name = str(node.get('class_name') or '')
    return any(token in class_name for token in (
        'EditText',
        'TextField',
        'SecureTextField',
        'SearchField',
        'XCUIElementTypeTextView',
    ))


def _is_tappable_node(node: Dict[str, Any]) -> bool:
    if node.get('visible') is False or node.get('enabled') is False:
        return False
    bounds = _node_bounds(node)
    if not bounds:
        return False
    x1, y1, x2, y2 = bounds
    width, height = x2 - x1, y2 - y1
    if width < 8 or height < 8:
        return False
    class_name = str(node.get('class_name') or '')
    if class_name in ('XCUIElementTypeApplication', 'XCUIElementTypeWindow'):
        # 应用/窗口根节点不是可点击控件（如 iOS Safari 的整屏 "Safari浏览器" 根节点）。
        return False
    if node.get('clickable') is True:
        return True
    return any(token in class_name for token in (
        'Button',
        'Cell',
        'Link',
        'Switch',
        'CheckBox',
        'RadioButton',
        'ToggleButton',
        'MenuItem',
    ))


def _is_identity_upload_node(node: Dict[str, Any]) -> bool:
    text = _node_text(node).casefold()
    identity_marker = any(keyword in text for keyword in (
        '身份证', '身份照片', '证件照', '人像面', '国徽面', 'identity card', 'id card',
    ))
    upload_marker = any(keyword in text for keyword in (
        '上传', '选择照片', '选择图片', '选取照片', '选取图片',
        '正面', '反面', 'photo', 'image', 'upload',
    ))
    return identity_marker and upload_marker and _node_bounds(node) is not None


def _screen_size(runner) -> Optional[Tuple[float, float]]:
    resolver = getattr(runner, '_get_current_resolution', None)
    if not callable(resolver):
        return None
    try:
        value = resolver()
        if isinstance(value, (tuple, list)) and len(value) >= 2:
            return float(value[0]), float(value[1])
        if isinstance(value, str):
            match = re.search(r'(\d+)\s*[xX×]\s*(\d+)', value)
            if match:
                return float(match.group(1)), float(match.group(2))
    except Exception:
        return None
    return None


def _exploration_input_value(
    session: AppAgentRuntimeSession,
    label: str,
) -> Tuple[str, str, bool]:
    settings_data = (session.coverage or {}).get('exploration') or {}
    values = settings_data.get('input_values') or {}
    normalized = str(label or '').casefold()
    for key, keywords, sensitive in EXPLORATORY_INPUT_KEYS:
        if key == 'default_text':
            continue
        if any(str(keyword).casefold() in normalized for keyword in keywords):
            if key == 'verify_code':
                return '', key, sensitive
            return str(values.get(key) or DEFAULT_EXPLORATION_INPUT_VALUES.get(key) or ''), key, sensitive
    if settings_data.get('allow_generic_text_input', True):
        return str(
            values.get('default_text')
            or DEFAULT_EXPLORATION_INPUT_VALUES['default_text']
        ), 'default_text', False
    return '', '', False


def _exploratory_candidate(
    *,
    session: AppAgentRuntimeSession,
    state_hash: str,
    action_type: str,
    name: str,
    source_step: Dict[str, Any],
    confidence: float,
    node: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    signature = _node_signature(state_hash, node or {'resource_id': name}, action_type)
    candidate = {
        'candidate_id': f'explore:{state_hash[:12]}:{signature}',
        'case_id': None,
        'case_name': '自动探索测试',
        'step_index': session.used_steps + 1,
        'name': name[:200],
        'action_type': action_type,
        'element_id': None,
        'element_name': '',
        'confidence': max(0.0, min(confidence, 1.0)),
        'exploratory': True,
        'step_config': copy.deepcopy(source_step),
        '_source_step': copy.deepcopy(source_step),
        '_variables': [],
    }
    candidate['step_config'].setdefault('exploratory', True)
    candidate['_source_step'].setdefault('exploratory', True)
    if node:
        candidate['observed_node'] = {
            key: node.get(key)
            for key in ('text', 'content_desc', 'resource_id', 'class_name', 'bounds')
            if node.get(key) not in (None, '', {})
        }
    candidate['approval_category'] = runtime_approval_category(candidate)
    return candidate


def _exploratory_candidate_priority(candidate: Dict[str, Any]) -> int:
    """按表单依赖排列探索动作，避免先索取尚未发送的验证码。"""
    step = candidate.get('step_config') or {}
    input_kind = str(step.get('input_kind') or '')
    if input_kind and input_kind != 'verify_code':
        return 10
    label = str(candidate.get('name') or '').casefold()
    if any(keyword in label for keyword in (
        '点击发送', '发送验证码', '获取验证码', '获取动态码',
        'send code', 'get code', 'send otp',
    )):
        return 20
    if input_kind == 'verify_code':
        return 30
    return 60


def collect_exploratory_candidates(
    session: AppAgentRuntimeSession,
    observation: Dict[str, Any],
    runner,
) -> List[Dict[str, Any]]:
    """从本次真实观察到的 UI 树生成有界 Monkey/Agent 候选动作。"""
    state_hash = observation.get('state_hash') or 'unknown'
    nodes = list(getattr(runner, '_last_page_nodes', None) or [])
    limit = int(((session.coverage or {}).get('exploration') or {}).get('max_candidates_per_page') or 12)
    candidates: List[Dict[str, Any]] = []
    seen_targets = set()
    is_browser_address_input = getattr(runner, '_is_ios_browser_address_input', None)
    exploration = (session.coverage or {}).get('exploration') or {}
    attempted_input_targets = {
        tuple(item)
        for item in (exploration.get('input_targets') or [])
        if isinstance(item, (list, tuple)) and len(item) == 2
    }
    attempted_input_keys = {
        str(item)
        for item in (exploration.get('input_target_keys') or [])
        if str(item).strip()
    }
    attempted_click_targets = {
        tuple(item)
        for item in (exploration.get('click_targets') or [])
        if isinstance(item, (list, tuple)) and len(item) == 2
    }
    screen_size = _screen_size(runner)
    keyboard_visible = False
    keyboard_checker = getattr(runner, '_ios_keyboard_overlay_visible', None)
    if callable(keyboard_checker):
        try:
            keyboard_visible = bool(keyboard_checker(nodes))
        except Exception:
            keyboard_visible = False

    for node in nodes:
        if not _is_input_node(node) or node.get('visible') is False or node.get('enabled') is False:
            continue
        if (
            getattr(runner, 'platform', None) == 'ios'
            and callable(is_browser_address_input)
            and is_browser_address_input(node)
        ):
            # Safari 地址栏/浏览器输入框不属于被测页面，不能作为探索输入候选。
            continue
        center = _node_center(node)
        if not center:
            continue
        if tuple(center) in attempted_input_targets:
            # 同一坐标已经尝试过输入，避免失败后无限重试同一个字段。
            continue
        label = _input_node_label(node)
        value, input_kind, sensitive = _exploration_input_value(session, _node_text(node))
        if not value and input_kind not in ('verify_code', 'license_plate'):
            continue
        input_target_key = f'{input_kind}:{label.casefold()}'
        if input_target_key in attempted_input_keys:
            continue
        node_text = _node_text(node)
        if value and node_text and (value in node_text or node_text in value):
            # 字段当前值已经包含待输入文本（如刚输入过），不再重复输入，避免自喂循环。
            continue
        key = ('input', tuple(center))
        if key in seen_targets:
            continue
        seen_targets.add(key)
        step = {
            'type': 'input',
            'name': f'探索输入：{label}',
            'selector_type': 'pos',
            'selector': center,
            'resource_id': node.get('resource_id') or '',
            'content_desc': node.get('content_desc') or '',
            'placeholder': node.get('placeholder') or '',
            'class_name': node.get('class_name') or '',
            'fingerprint': {
                key: node.get(key)
                for key in (
                    'resource_id', 'content_desc', 'placeholder', 'text', 'class_name', 'bounds'
                )
                if node.get(key) not in (None, '', {})
            },
            'input_kind': input_kind,
            'sensitive': sensitive,
            'exploration_target_key': input_target_key,
            'clear_first': True,
            'send_enter': input_kind == 'keyword',
            'wait_after': 0.8,
            'block_coordinate_fallback': False,
            'allow_coordinate_fallback': True,
        }
        if value:
            step['value'] = value
        candidates.append(_exploratory_candidate(
            session=session,
            state_hash=state_hash,
            action_type='input',
            name=step['name'],
            source_step=step,
            confidence=0.76,
            node=node,
        ))

    plate_target_resolver = getattr(runner, '_license_plate_input_container_target', None)
    plate_target_key = 'license_plate:车牌号'
    if (
        plate_target_key not in attempted_input_keys
        and callable(plate_target_resolver)
    ):
        try:
            plate_target, plate_detail = plate_target_resolver(nodes)
        except Exception as exc:
            logger.debug('识别探索车牌输入控件失败: %s', exc)
            plate_target, plate_detail = None, {}
        if (
            isinstance(plate_target, (list, tuple))
            and len(plate_target) >= 2
            and tuple(plate_target[:2]) not in attempted_input_targets
        ):
            center = [int(plate_target[0]), int(plate_target[1])]
            key = ('input', tuple(center))
            if key not in seen_targets:
                seen_targets.add(key)
                node = {
                    'text': '车牌号',
                    'resource_id': '车牌号',
                    'class_name': 'RunnerGoLicensePlateControl',
                    'bounds': {
                        'x1': center[0] - 1,
                        'y1': center[1] - 1,
                        'x2': center[0] + 1,
                        'y2': center[1] + 1,
                    },
                }
                plate_value = str((exploration.get('input_values') or {}).get('license_plate') or '')
                step = {
                    'type': 'license_plate_input',
                    'name': '自动输入车牌号',
                    'selector_type': 'pos',
                    'selector': center,
                    'ocr_text': '车牌号',
                    'plate': plate_value,
                    'input_kind': 'license_plate',
                    'sensitive': True,
                    'exploration_target_key': plate_target_key,
                    'wait_after': 0.8,
                    'block_coordinate_fallback': False,
                    'allow_coordinate_fallback': True,
                    'container_diagnostics': _json_safe(plate_detail),
                }
                candidates.append(_exploratory_candidate(
                    session=session,
                    state_hash=state_hash,
                    action_type='license_plate_input',
                    name=step['name'],
                    source_step=step,
                    confidence=0.78,
                    node=node,
                ))

    for node in nodes:
        if _is_input_node(node) or (
            not _is_tappable_node(node) and not _is_identity_upload_node(node)
        ):
            continue
        if screen_size:
            bounds = _node_bounds(node)
            if bounds:
                x1, y1, x2, y2 = bounds
                screen_w, screen_h = screen_size
                if (
                    screen_w > 0
                    and screen_h > 0
                    and (x2 - x1) * (y2 - y1) >= 0.8 * screen_w * screen_h
                    and not node.get('clickable')
                ):
                    # 全屏非交互容器（如 WebView 容器）点击中心没有意义。
                    continue
        if _is_excluded_exploratory_click(
            node,
            platform=str(getattr(runner, 'platform', '') or '').casefold(),
            screen_size=screen_size,
            keyboard_visible=keyboard_visible,
        ):
            continue
        center = _node_center(node)
        if not center:
            continue
        label = _node_label(node)
        if tuple(center) in attempted_click_targets:
            # 同一坐标已经点击过，避免重复点同一个按钮。
            continue
        key = ('click', tuple(center))
        if key in seen_targets:
            continue
        seen_targets.add(key)
        step = {
            'type': 'click',
            'name': f'探索点击：{label}',
            'selector_type': 'pos',
            'selector': center,
            'wait_after': 0.8,
            'block_coordinate_fallback': False,
            'allow_coordinate_fallback': True,
            'verify_postcondition': True,
            'resource_id': node.get('resource_id') or '',
            'content_desc': node.get('content_desc') or '',
            'text': node.get('text') or '',
            'class_name': node.get('class_name') or '',
            'fingerprint': {
                key: node.get(key)
                for key in ('resource_id', 'content_desc', 'text', 'class_name', 'bounds')
                if node.get(key) not in (None, '', {})
            },
        }
        candidates.append(_exploratory_candidate(
            session=session,
            state_hash=state_hash,
            action_type='click',
            name=step['name'],
            source_step=step,
            confidence=0.7 if node.get('clickable') else 0.58,
            node=node,
        ))

    candidates.sort(key=_exploratory_candidate_priority)
    utility_steps = [
        ('swipe', '探索上滑页面', {'type': 'swipe', 'name': '探索上滑页面', 'direction': 'up', 'distance': 0.55, 'duration': 0.45, 'wait_after': 0.8}),
        ('back', '探索返回上一页', {'type': 'back', 'name': '探索返回上一页', 'wait_after': 0.6}),
        ('wait', '探索等待页面稳定', {'type': 'wait', 'name': '探索等待页面稳定', 'duration': 1}),
    ]
    for action_type, name, step in utility_steps:
        candidates.append(_exploratory_candidate(
            session=session,
            state_hash=state_hash,
            action_type=action_type,
            name=name,
            source_step=step,
            confidence=0.52,
            node={'resource_id': name, 'bounds': {'x1': 0, 'y1': 0, 'x2': 1, 'y2': 1}},
        ))
        if len(candidates) >= limit:
            break
    return candidates[:limit]


def _redact_visible_text(value: Any) -> str:
    text = str(value or '').strip()
    text = re.sub(r'(?<!\d)1\d{10}(?!\d)', '1**********', text)
    text = re.sub(r'(?<!\d)\d{6,}(?!\d)', lambda match: f'{match.group(0)[:2]}***{match.group(0)[-2:]}', text)
    return text[:160]


def _media_evidence(path: str) -> Dict[str, str]:
    if not path:
        return {'path': '', 'url': ''}
    absolute = os.path.abspath(path)
    try:
        relative = os.path.relpath(absolute, settings.MEDIA_ROOT)
        if not relative.startswith('..'):
            return {
                'path': relative,
                'url': build_public_evidence_path(relative),
            }
    except (TypeError, ValueError):
        pass
    return {'path': absolute, 'url': ''}


def _image_signals(path: str) -> Dict[str, Any]:
    if not path or not os.path.isfile(path):
        return {'suspected_white_screen': False}
    try:
        from PIL import Image, ImageStat

        with Image.open(path) as image:
            grayscale = image.convert('L').resize((64, 64))
            stat = ImageStat.Stat(grayscale)
            mean = float(stat.mean[0])
            variance = float(stat.var[0])
        return {
            'suspected_white_screen': mean >= 245 and variance <= 18,
            'brightness': round(mean, 2),
            'variance': round(variance, 2),
        }
    except Exception as exc:
        logger.debug('分析运行时截图失败: %s', exc)
        return {'suspected_white_screen': False}


def observe_runtime(session: AppAgentRuntimeSession, runner) -> Dict[str, Any]:
    """读取截图、页面上下文和经过脱敏的 UI 树摘要。"""
    state = runner._current_page_state()
    state.update(runner._detect_semantic_page({}, state))
    nodes = list(runner._last_page_nodes or [])
    visible_texts = []
    for node in nodes:
        for key in ('text', 'content_desc', 'resource_id'):
            value = _redact_visible_text(node.get(key))
            if value and value not in visible_texts:
                visible_texts.append(value)
        if len(visible_texts) >= 80:
            break
    screenshot_path = runner._capture_screenshot(f'runtime_{session.id}_observe_{session.used_steps + 1}')
    screenshot = _media_evidence(screenshot_path or '')
    image_signals = _image_signals(screenshot_path or '')
    telemetry = {}
    telemetry_interval = max(1, int(session.telemetry_interval_steps or 1))
    if session.telemetry_enabled and (
        session.used_steps == 0 or session.used_steps % telemetry_interval == 0
    ):
        telemetry = collect_runtime_telemetry(session, runner, screenshot_path or '')
        telemetry['sampled'] = True
        session.telemetry_summary = merge_telemetry_summary(
            session.telemetry_summary,
            telemetry,
        )
        _save_runtime_state(session, ['telemetry_summary', 'updated_at'])
    elif session.telemetry_enabled:
        telemetry = copy.deepcopy((session.telemetry_summary or {}).get('latest') or {})
        telemetry['sampled'] = False
    combined_text = ' '.join(visible_texts).casefold()
    issues = []
    if any(pattern.casefold() in combined_text for pattern in CRASH_PATTERNS):
        issues.append('crash_or_anr')
    if image_signals.get('suspected_white_screen'):
        issues.append('white_screen')
    if (telemetry.get('crash') or {}).get('detected'):
        issues.append('crash_or_anr')
    issues.extend(
        risk for risk in (telemetry.get('risks') or [])
        if risk not in issues
    )

    expected_package = session.task.app_package.package_name if session.task.app_package else ''
    actual_package = str(state.get('package') or '')
    if expected_package and actual_package and actual_package != expected_package:
        issues.append('unexpected_package')
    fingerprint_payload = {
        'type': state.get('type'),
        'package': actual_package,
        'activity': state.get('activity'),
        'page_name': state.get('page_name'),
        'visible_texts': visible_texts,
    }
    state_hash = hashlib.sha256(
        json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
    ).hexdigest()
    return {
        'captured_at': timezone.now().isoformat(),
        'state_hash': state_hash,
        'page': {
            'type': state.get('type') or 'unknown',
            'page_name': state.get('page_name') or '',
            'page_label': state.get('page_label') or '',
            'package': actual_package,
            'activity': state.get('activity') or '',
            'confidence': state.get('page_confidence') or state.get('confidence') or 0,
            'evidence': state.get('page_evidence') or state.get('evidence') or [],
        },
        'visible_texts': visible_texts,
        'node_count': len(nodes),
        'screenshot': screenshot,
        'telemetry': telemetry,
        'signals': {**image_signals, 'issues': issues},
    }


def _rule_decision(remaining: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    candidate = remaining[0]
    return {
        'source': 'rules',
        'candidate_id': candidate['candidate_id'],
        'reason': '按已评审用例顺序选择下一个尚未执行的受控动作',
        'confidence': 0.82,
        'finish': False,
    }


def _ai_runtime_decision(
    session: AppAgentRuntimeSession,
    observation: Dict[str, Any],
    remaining: Sequence[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], int]:
    config = _active_writer_config(session.task.user)
    token_remaining = session.max_model_tokens - session.used_model_tokens
    if not config or token_remaining < 512:
        return None, 0

    from apps.requirement_analysis.models import AIModelService

    candidates = [
        {
            'candidate_id': item.get('candidate_id'),
            'name': item.get('name'),
            'action_type': item.get('action_type'),
            'approval_category': item.get('approval_category') or '',
            'confidence': item.get('confidence'),
            'input_kind': (item.get('step_config') or {}).get('input_kind') or '',
        }
        for item in remaining[:40]
    ]
    page = observation.get('page') or {}
    observation_for_ai = {
        'state_hash': observation.get('state_hash') or '',
        'page': {
            key: page.get(key)
            for key in ('type', 'page_name', 'page_label', 'package', 'activity')
            if page.get(key) not in (None, '')
        },
        'visible_texts': list(observation.get('visible_texts') or [])[:40],
        'node_count': int(observation.get('node_count') or 0),
        'issues': list((observation.get('signals') or {}).get('issues') or [])[:10],
    }
    request_payload = {
        'goal': session.task.goal[:500],
        'observation': observation_for_ai,
        'completed_candidate_ids': list(session.completed_candidate_ids or [])[-50:],
        'candidates': candidates,
    }
    request_json = json.dumps(request_payload, ensure_ascii=False, separators=(',', ':'))
    estimated_input_tokens = max(1, len(request_json) // 3)
    max_completion_tokens = min(320, token_remaining)
    if estimated_input_tokens + max_completion_tokens > token_remaining:
        return None, 0
    messages = [
        {
            'role': 'system',
            'content': (
                '你是APP测试运行时决策器。只能从candidates中选择一个真实candidate_id，'
                '不能创造元素、动作或坐标。输出JSON：'
                '{"candidate_id":"case:1:step:1","reason":"原因","confidence":0.9}。'
                '只有验证码输入由平台暂停并等待用户填写，你不能绕过。不要输出Markdown。'
            ),
        },
        {
            'role': 'user',
            'content': request_json,
        },
    ]
    try:
        response = async_to_sync(AIModelService.call_openai_compatible_api)(
            config,
            messages,
            max_tokens=max_completion_tokens,
        )
        usage = response.get('usage') or {}
        used_tokens = int(usage.get('total_tokens') or usage.get('completion_tokens') or 256)
        parsed = _extract_json_object(response['choices'][0]['message']['content'])
        valid_ids = {item['candidate_id'] for item in remaining}
        candidate_id = str(parsed.get('candidate_id') or '')
        if candidate_id not in valid_ids:
            raise ValueError('模型选择了不存在或无权执行的 candidate_id')
        return ({
            'source': 'ai',
            'candidate_id': candidate_id,
            'reason': str(parsed.get('reason') or 'AI 根据当前页面选择下一动作')[:500],
            'confidence': max(0.0, min(float(parsed.get('confidence') or 0.7), 1.0)),
            'finish': False,
        }, used_tokens)
    except Exception as exc:
        logger.warning('APP Runtime Agent 决策失败，降级为规则选择: %s', exc)
        return None, 0


def decide_runtime_action(
    session: AppAgentRuntimeSession,
    observation: Dict[str, Any],
    candidates: Sequence[Dict[str, Any]],
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], int]:
    completed = set(session.completed_candidate_ids or [])
    remaining = [item for item in candidates if item['candidate_id'] not in completed]
    if not remaining:
        return ({
            'source': 'rules',
            'candidate_id': '',
            'reason': '所有授权候选动作均已完成',
            'confidence': 1.0,
            'finish': True,
        }, None, 0)

    if any(item.get('exploratory') for item in remaining):
        current_priority = min(_exploratory_candidate_priority(item) for item in remaining)
        remaining = [
            item for item in remaining
            if _exploratory_candidate_priority(item) == current_priority
        ]

    decision, used_tokens = _ai_runtime_decision(session, observation, remaining)
    if not decision:
        decision = _rule_decision(remaining)
    candidate_map = {item['candidate_id']: item for item in remaining}
    candidate = candidate_map.get(decision.get('candidate_id'))
    if not candidate:
        decision = _rule_decision(remaining)
        decision['reason'] = 'AI 决策未通过资产校验，已降级到下一个授权动作'
        candidate = remaining[0]
    return decision, candidate, used_tokens


def _json_safe(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return str(value)[:500]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item, depth + 1) for key, item in list(value.items())[:100]}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth + 1) for item in list(value)[:100]]
    return str(value)[:1000]


def _runtime_step_config(candidate: Dict[str, Any]) -> Dict[str, Any]:
    step = copy.deepcopy(candidate.get('_source_step') or candidate.get('step_config') or {})
    if not step:
        raise RuntimeError('候选动作缺少可执行步骤配置')
    step['type'] = candidate['action_type']
    config = step.setdefault('config', {})
    if not isinstance(config, dict):
        config = {}
        step['config'] = config
    if candidate.get('element_id'):
        config['element_id'] = candidate['element_id']
    if candidate['action_type'] in ('self_heal_click', 'self_heal_input'):
        config['self_heal_mode'] = 'report'
        config['allow_coordinate_persist'] = False
        config['guard_security_challenge'] = True
    if candidate['action_type'] == 'wait':
        duration = config.get('duration', step.get('duration', 1))
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            duration = 1
        config['duration'] = max(0, min(duration, 5))
    return step


def _locator_strategy(runner) -> str:
    diagnostics = runner._last_webview_diagnostics or runner._last_locator_diagnostics or {}
    return str(
        diagnostics.get('selected_strategy')
        or (diagnostics.get('self_heal') or {}).get('selected_strategy')
        or ''
    )[:40]


def _update_locator_knowledge(
    session: AppAgentRuntimeSession,
    candidate: Dict[str, Any],
    strategy: str,
    diagnostics: Dict[str, Any],
    *,
    success: bool,
) -> None:
    element_id = candidate.get('element_id')
    if not element_id or not strategy:
        return
    device = session.task.device
    version = device.ios_version if device.platform == 'ios' else device.android_version
    package_name = session.task.app_package.package_name if session.task.app_package else ''
    knowledge, _ = AppLocatorKnowledge.objects.get_or_create(
        project=session.task.project,
        element_id=element_id,
        platform=device.platform or '',
        platform_version=version or '',
        app_package=package_name,
        strategy=strategy,
    )
    knowledge.rollback_point = {
        'confidence': knowledge.confidence,
        'success_count': knowledge.success_count,
        'failure_count': knowledge.failure_count,
        'captured_at': timezone.now().isoformat(),
    }
    if success:
        knowledge.success_count += 1
        knowledge.last_success_at = timezone.now()
    else:
        knowledge.failure_count += 1
    knowledge.confidence = round(
        (knowledge.success_count + 1) /
        (knowledge.success_count + knowledge.failure_count + 2),
        4,
    )
    knowledge.last_diagnostics = _json_safe(diagnostics)
    knowledge.save()


def _update_coverage(
    session: AppAgentRuntimeSession,
    observation: Dict[str, Any],
    previous_step: Optional[AppAgentRuntimeStep],
) -> None:
    coverage = copy.deepcopy(session.coverage or {})
    pages = coverage.setdefault('pages', [])
    edges = coverage.setdefault('edges', [])
    state_hash = observation.get('state_hash') or ''
    page = next((item for item in pages if item.get('id') == state_hash), None)
    if page:
        page['visits'] = int(page.get('visits') or 0) + 1
        page['last_seen_at'] = observation.get('captured_at')
    else:
        page_detail = observation.get('page') or {}
        pages.append({
            'id': state_hash,
            'name': page_detail.get('page_label') or page_detail.get('page_name') or page_detail.get('activity') or '未知页面',
            'package': page_detail.get('package') or '',
            'visits': 1,
            'last_seen_at': observation.get('captured_at'),
        })
    if previous_step and previous_step.observation_hash and previous_step.action:
        edge = {
            'from': previous_step.observation_hash,
            'to': state_hash,
            'action': previous_step.action.get('name') or previous_step.action.get('action_type'),
            'candidate_id': previous_step.action.get('candidate_id'),
        }
        if edge not in edges:
            edges.append(edge)
    coverage['page_count'] = len(pages)
    coverage['edge_count'] = len(edges)
    session.coverage = coverage


class RuntimeDeviceContext:
    """在一个 worker 执行窗口内独占设备并复用同一个 Airtest 会话。"""

    def __init__(self, session: AppAgentRuntimeSession):
        self.session = session
        self.airtest = None
        self.runner = None
        self.locked_here = False

    def __enter__(self):
        from .runners.ui_flow_runner import UiFlowRunner
        from .utils.airtest_base import AirtestBase

        task = self.session.task
        device = task.device
        device.refresh_from_db()
        if device.status == 'offline':
            raise RuntimeError('执行设备当前离线')
        if device.status == 'locked' and device.locked_by_id != task.user_id:
            raise RuntimeError('执行设备已被其他用户锁定')
        try:
            if device.status != 'locked':
                device.lock(task.user)
                self.locked_here = True

            runtime_dir = os.path.join(
                settings.MEDIA_ROOT,
                'app-automation',
                'agent-runtime',
                f'session_{self.session.id}',
            )
            os.makedirs(runtime_dir, exist_ok=True)
            self.airtest = AirtestBase(
                device_id=device.device_id,
                screenshots_dir=runtime_dir,
                username=task.user.username,
                platform=device.platform,
                wda_url=device.wda_url,
                wda_bundle_id=device.wda_bundle_id,
            )
            if not self.airtest.setup_airtest():
                raise RuntimeError('Airtest 环境设置失败')

            package_name = task.app_package.package_name if task.app_package else device.default_bundle_id
            target_url = str((self.session.coverage or {}).get('target_url') or '').strip()
            resuming_approved_step = self.session.steps.filter(
                status='awaiting_approval',
                approval_status='approved',
            ).exists()
            if self.session.used_steps == 0 and not resuming_approved_step and target_url:
                if not self.airtest.open_url(target_url):
                    raise RuntimeError(f'模拟器打开目标 URL 失败: {target_url}')
            elif self.session.used_steps == 0 and not resuming_approved_step and package_name:
                if not self.airtest.open_app(package_name):
                    raise RuntimeError(f'应用启动失败: {package_name}')
            self.runner = UiFlowRunner(
                username=task.user.username,
                device_id=device.device_id,
                package_name='' if target_url else (package_name or ''),
                platform=device.platform,
            )
            self.runner.screenshots_dir = runtime_dir
            return self.runner
        except Exception:
            if self.airtest:
                self.airtest.teardown_airtest()
            device.refresh_from_db()
            if device.locked_by_id == task.user_id:
                device.unlock()
            raise

    def __exit__(self, exc_type, exc, traceback):
        try:
            if self.runner:
                self.runner.close()
        finally:
            if self.airtest:
                self.airtest.teardown_airtest()
            device = self.session.task.device
            device.refresh_from_db()
            if device.locked_by_id == self.session.task.user_id:
                device.unlock()


def _execute_runtime_step(
    session: AppAgentRuntimeSession,
    runtime_step: AppAgentRuntimeStep,
    candidate: Dict[str, Any],
    runner,
) -> None:
    runtime_step.status = 'running'
    runtime_step.started_at = timezone.now()
    _save_runtime_state(runtime_step, ['status', 'started_at'])
    step_config = _runtime_step_config(candidate)
    try:
        result = runner.run(
            [step_config],
            variables=candidate.get('_variables') or [],
            runtime={
                'stop_on_error': True,
                'retry_times': 1,
                'locator_timeout': 5,
            },
        )
        if result.get('failed'):
            raise RuntimeError(f'受控动作执行失败: {candidate["name"]}')
        selected_strategy = _locator_strategy(runner)
        after_path = runner._capture_screenshot(
            f'runtime_{session.id}_step_{runtime_step.sequence}_after'
        )
        diagnostics = runner._last_webview_diagnostics or runner._last_locator_diagnostics or {}
        runtime_step.status = 'passed'
        runtime_step.selected_locator_strategy = selected_strategy
        runtime_step.evidence = {
            'result': _json_safe(result),
            'screenshot_after': _media_evidence(after_path or ''),
            'locator_diagnostics': _json_safe(diagnostics),
        }
        runtime_step.finished_at = timezone.now()
        if runtime_approval_requires_input(runtime_step.action or {}, runtime_step.approval_category):
            runtime_step.action = redact_runtime_approval_input(runtime_step.action or {})
        _save_runtime_state(runtime_step, [
            'status', 'action', 'selected_locator_strategy', 'evidence', 'finished_at'
        ])
        completed = list(session.completed_candidate_ids or [])
        if candidate['candidate_id'] not in completed:
            completed.append(candidate['candidate_id'])
        session.completed_candidate_ids = completed
        session.used_steps += 1
        session.current_action = public_runtime_candidate(candidate)
        coverage = copy.deepcopy(session.coverage or {})
        coverage['candidate_completed'] = len(completed)
        coverage['candidate_total'] = max(
            int(coverage.get('candidate_total') or 0),
            len(completed),
        )
        session.coverage = coverage
        _save_runtime_state(session, [
            'completed_candidate_ids', 'used_steps', 'current_action', 'coverage', 'updated_at'
        ])
        _update_locator_knowledge(
            session,
            candidate,
            selected_strategy,
            diagnostics,
            success=True,
        )
        record_agent_event(
            session.task,
            'runtime_action',
            f'自治步骤 {runtime_step.sequence} 执行通过：{candidate["name"]}',
            level='success',
            payload={
                'session_id': session.id,
                'runtime_step_id': runtime_step.id,
                'candidate_id': candidate['candidate_id'],
                'locator_strategy': selected_strategy,
            },
        )
        _mark_explored_target(session, candidate)
    except Exception as exc:
        diagnostics = runner._last_webview_diagnostics or runner._last_locator_diagnostics or {}
        selected_strategy = _locator_strategy(runner)
        runtime_step.status = 'failed'
        runtime_step.error_message = str(exc)
        runtime_step.selected_locator_strategy = selected_strategy
        runtime_step.evidence = {'locator_diagnostics': _json_safe(diagnostics)}
        runtime_step.finished_at = timezone.now()
        if runtime_approval_requires_input(runtime_step.action or {}, runtime_step.approval_category):
            runtime_step.action = redact_runtime_approval_input(runtime_step.action or {})
        _save_runtime_state(runtime_step, [
            'status', 'action', 'error_message', 'selected_locator_strategy', 'evidence', 'finished_at'
        ])
        session.used_steps += 1
        update_fields = ['used_steps', 'updated_at']
        if candidate.get('exploratory'):
            completed = list(session.completed_candidate_ids or [])
            if candidate['candidate_id'] not in completed:
                completed.append(candidate['candidate_id'])
            session.completed_candidate_ids = completed
            coverage = copy.deepcopy(session.coverage or {})
            coverage['candidate_completed'] = len(completed)
            coverage['candidate_total'] = max(
                int(coverage.get('candidate_total') or 0),
                len(completed),
            )
            session.coverage = coverage
            update_fields.extend(['completed_candidate_ids', 'coverage'])
        _save_runtime_state(session, update_fields)
        _update_locator_knowledge(
            session,
            candidate,
            selected_strategy,
            diagnostics,
            success=False,
        )
        _mark_explored_target(session, candidate)
        raise


def _mark_explored_target(
    session: AppAgentRuntimeSession,
    candidate: Dict[str, Any],
) -> None:
    """记录探索会话已尝试交互的控件坐标，避免同一字段/按钮被反复选择。"""
    if not candidate.get('exploratory'):
        return
    action_type = candidate.get('action_type')
    if action_type not in ('input', 'smart_input', 'license_plate_input', 'click'):
        return
    node = candidate.get('observed_node') or {}
    center = _node_center(node)
    if not center:
        return
    coverage = copy.deepcopy(session.coverage or {})
    exploration = coverage.setdefault('exploration', {})
    key = tuple(center)
    targets = exploration.setdefault(
        'input_targets' if action_type in ('input', 'smart_input', 'license_plate_input') else 'click_targets',
        [],
    )
    known = {
        tuple(item)
        for item in targets
        if isinstance(item, (list, tuple)) and len(item) == 2
    }
    if key in known:
        return
    targets.append(list(key))
    target_key = str(
        (candidate.get('step_config') or {}).get('exploration_target_key') or ''
    ).strip()
    if target_key:
        target_keys = exploration.setdefault('input_target_keys', [])
        if target_key not in target_keys:
            target_keys.append(target_key)
    session.coverage = coverage
    _save_runtime_state(session, ['coverage', 'updated_at'])


def _mark_ineffective_exploration_step(
    session: AppAgentRuntimeSession,
    runtime_step: AppAgentRuntimeStep,
    observation: Dict[str, Any],
) -> None:
    """把已执行但未改变页面的探索动作记为无效尝试，而不是成功动作。"""
    message = '探索动作执行后页面未变化，已标记为无效动作并继续探索'
    evidence = copy.deepcopy(runtime_step.evidence or {})
    evidence['postcondition'] = {
        'effective': False,
        'reason': 'state_unchanged',
        'before_state_hash': runtime_step.observation_hash,
        'after_state_hash': observation.get('state_hash') or '',
        'observation': {
            'captured_at': observation.get('captured_at'),
            'screenshot': observation.get('screenshot') or {},
        },
    }
    runtime_step.status = 'skipped'
    runtime_step.error_message = message
    runtime_step.evidence = evidence
    _save_runtime_state(runtime_step, ['status', 'error_message', 'evidence'])

    coverage = copy.deepcopy(session.coverage or {})
    exploration = coverage.setdefault('exploration', {})
    ineffective = exploration.setdefault('ineffective_actions', [])
    candidate_id = str((runtime_step.action or {}).get('candidate_id') or '')
    if not any(item.get('runtime_step_id') == runtime_step.id for item in ineffective):
        ineffective.append({
            'runtime_step_id': runtime_step.id,
            'candidate_id': candidate_id,
            'name': (runtime_step.action or {}).get('name') or '',
            'state_hash': observation.get('state_hash') or '',
        })
    for edge in coverage.get('edges') or []:
        if edge.get('candidate_id') == candidate_id and edge.get('to') == observation.get('state_hash'):
            edge['effective'] = False
    session.coverage = coverage
    _save_runtime_state(session, ['coverage', 'updated_at'])
    record_agent_event(
        session.task,
        'runtime_action',
        f'探索步骤 {runtime_step.sequence} 未改变页面，已跳过：'
        f'{(runtime_step.action or {}).get("name") or "探索动作"}',
        level='warning',
        payload={
            'session_id': session.id,
            'runtime_step_id': runtime_step.id,
            'candidate_id': candidate_id,
            'reason': 'state_unchanged',
        },
    )


def _duration_used(session: AppAgentRuntimeSession, base: float, started: float) -> float:
    return round(base + max(0.0, time.monotonic() - started), 3)


def _runtime_failure_artifacts(
    session: AppAgentRuntimeSession,
    message: str,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    lowered = str(message or '').casefold()
    if '崩溃' in lowered or '无响应' in lowered or 'crash' in lowered or 'anr' in lowered:
        category, label, severity = 'app_crash', '应用崩溃或无响应', 'critical'
    elif '白屏' in lowered or 'white' in lowered:
        category, label, severity = 'white_screen', '应用白屏', 'critical'
    elif '死循环' in lowered or '页面未变化' in lowered:
        category, label, severity = 'dead_loop', '自治路径陷入重复页面', 'high'
    elif '定位' in lowered or 'element' in lowered or 'locator' in lowered:
        category, label, severity = 'locator', '元素定位失败', 'high'
    else:
        category, label, severity = 'runtime', '自治执行异常', 'high'
    steps = list(session.steps.select_related('element').order_by('sequence'))
    reproduction = [
        f'{item.sequence}. {item.action.get("name") or item.action.get("action_type") or "执行动作"}'
        for item in steps
    ]
    observation = session.current_observation or (steps[-1].observation if steps else {})
    device = session.task.device
    version = device.ios_version if device.platform == 'ios' else device.android_version
    package_name = session.task.app_package.package_name if session.task.app_package else ''
    latest_step = steps[-1] if steps else None
    analysis = {
        'summary': label,
        'items': [{
            'runtime_session_id': session.id,
            'runtime_step_id': latest_step.id if latest_step else None,
            'category': category,
            'category_label': label,
            'actual': message,
            'page': (observation.get('page') or {}).get('page_label') or (observation.get('page') or {}).get('page_name') or '',
            'state_hash': observation.get('state_hash') or '',
        }],
    }
    defect = {
        'title': f'[{device.name or device.device_id}] {label}',
        'severity': severity,
        'category': category,
        'environment': {
            'device': device.name or device.device_id,
            'platform': device.platform,
            'version': version,
            'package': package_name,
        },
        'steps': reproduction or ['1. 启动 APP Agent 自治运行会话'],
        'expected': f'Agent 在预算内完成测试目标：{session.task.goal}',
        'actual': message,
        'evidence': {
            'runtime_session_id': session.id,
            'runtime_step_id': latest_step.id if latest_step else None,
            'screenshot': observation.get('screenshot') or {},
            'action_evidence': latest_step.evidence if latest_step else {},
            'state_hash': observation.get('state_hash') or '',
            'telemetry': session.telemetry_summary or {},
        },
    }
    return analysis, [defect]


def _finish_session(
    session: AppAgentRuntimeSession,
    *,
    status: str,
    message: str,
    task_status: str,
) -> None:
    coverage = copy.deepcopy(session.coverage or {})
    planned_exploration = bool(coverage.get('planned_exploration'))
    target_index = int(coverage.get('target_index') or 0)
    target_url = str(coverage.get('target_url') or '')
    session.status = status
    session.error_message = '' if status == 'completed' else message
    session.finished_at = timezone.now()
    session.result_summary = {
        'status': status,
        'message': message,
        'used_steps': session.used_steps,
        'max_steps': session.max_steps,
        'used_duration_seconds': session.used_duration_seconds,
        'max_duration_seconds': session.max_duration_seconds,
        'used_model_tokens': session.used_model_tokens,
        'max_model_tokens': session.max_model_tokens,
        'completed_candidate_ids': session.completed_candidate_ids,
        'page_count': (session.coverage or {}).get('page_count', 0),
        'edge_count': (session.coverage or {}).get('edge_count', 0),
        'telemetry': session.telemetry_summary or {},
        'target_index': target_index if planned_exploration else None,
        'target_url': target_url,
    }
    _save_runtime_state(session, [
        'status', 'error_message', 'finished_at', 'result_summary', 'updated_at'
    ])
    task = session.task
    task.refresh_from_db()
    plan = copy.deepcopy(task.plan or {})
    exploration_targets = list(plan.get('exploration_targets') or [])
    exploration_run = copy.deepcopy(plan.get('exploration_run') or {})
    continue_planned_exploration = planned_exploration and status != 'stopped'
    if planned_exploration:
        results = [
            item for item in (exploration_run.get('results') or [])
            if int(item.get('target_index', -1)) != target_index
        ]
        results.append({
            'session_id': session.id,
            'target_index': target_index,
            'target_url': target_url,
            'status': status,
            'message': message,
            'used_steps': session.used_steps,
            'page_count': (session.coverage or {}).get('page_count', 0),
            'edge_count': (session.coverage or {}).get('edge_count', 0),
        })
        results.sort(key=lambda item: int(item.get('target_index') or 0))
        exploration_run['results'] = results
        if status == 'stopped':
            exploration_run['status'] = 'stopped'
        elif len(results) < len(exploration_targets):
            exploration_run['status'] = 'running'
        else:
            exploration_run['status'] = 'completed'
        plan['exploration_run'] = exploration_run
        task.plan = plan

    task.status = (
        'runtime_executing'
        if continue_planned_exploration
        else task_status
    )
    if continue_planned_exploration:
        attempted_count = len(exploration_run.get('results') or [])
        target_count = max(1, len(exploration_targets))
        task.progress = max(task.progress, 85 + int(attempted_count / target_count * 10))
    elif status == 'completed':
        task.progress = 100
    task.error_message = '' if status == 'completed' or continue_planned_exploration else message
    task.finished_at = (
        None
        if continue_planned_exploration
        else timezone.now()
    )
    task.result_summary = {
        **(task.result_summary or {}),
        'runtime': session.result_summary,
    }
    update_fields = [
        'status', 'progress', 'error_message', 'finished_at', 'result_summary', 'updated_at'
    ]
    if planned_exploration:
        update_fields.append('plan')
    if status == 'failed':
        analysis, defect_drafts = _runtime_failure_artifacts(session, message)
        task.failure_analysis = analysis
        task.defect_draft = defect_drafts
        update_fields.extend(['failure_analysis', 'defect_draft'])
    _save_runtime_state(task, update_fields)
    record_agent_event(
        task,
        'runtime_completed' if status == 'completed' else 'runtime_error',
        message,
        level='success' if status == 'completed' else 'error',
        payload={'session_id': session.id, **session.result_summary},
    )
    if planned_exploration:
        record_agent_event(
            task,
            'explore_target_completed' if status == 'completed' else 'explore_target_failed',
            (
                f'页面探索 {target_index + 1}/{len(exploration_targets)} 完成：{target_url}'
                if status == 'completed' else
                f'页面探索 {target_index + 1}/{len(exploration_targets)} 失败：{target_url}'
            ),
            level='success' if status == 'completed' else 'error',
            payload={
                'status': 'success' if status == 'completed' else 'failed',
                'session_id': session.id,
                'target_index': target_index,
                'target_count': len(exploration_targets),
                'target_url': target_url,
            },
        )
    try:
        from .matrix_service import sync_matrix_cell

        sync_matrix_cell(session)
    except Exception as exc:
        logger.warning('同步兼容性矩阵结果失败: session=%s error=%s', session.id, exc)
    if continue_planned_exploration:
        try:
            if int(exploration_run.get('next_index') or 0) < len(exploration_targets):
                queue_next_planned_exploration(task)
            else:
                from .tasks import finalize_app_agent_task

                finalize_app_agent_task(task.id)
        except Exception as exc:
            logger.error('继续页面探索或汇总混合任务失败: %s', exc, exc_info=True)
            task.refresh_from_db()
            task.status = 'failed'
            task.error_message = str(exc)
            task.finished_at = timezone.now()
            _save_runtime_state(task, [
                'status', 'error_message', 'finished_at', 'updated_at'
            ])
            record_agent_event(task, 'error', str(exc), level='error')
    elif status != 'stopped':
        # URL-only exploration must enter the same analysis and report closure
        # as recorded-case and planned-exploration runs.
        try:
            from .tasks import finalize_app_agent_task

            finalize_app_agent_task(task.id)
        except Exception as exc:
            logger.error('汇总 APP Runtime Agent 任务失败: %s', exc, exc_info=True)
            task.refresh_from_db()
            task.status = 'failed'
            task.error_message = str(exc)
            task.finished_at = timezone.now()
            _save_runtime_state(task, [
                'status', 'error_message', 'finished_at', 'updated_at'
            ])
            record_agent_event(task, 'error', str(exc), level='error')


def _budget_failure(session: AppAgentRuntimeSession, reason: str) -> None:
    _finish_session(
        session,
        status='budget_exhausted',
        message=reason,
        task_status='failed',
    )


def run_runtime_session(session_id: int) -> None:
    """运行或在审批后继续运行一个自治会话。每次只执行经验证的单个动作。"""
    session = AppAgentRuntimeSession.objects.select_related(
        'task', 'task__user', 'task__project', 'task__device', 'task__app_package'
    ).get(id=session_id)
    claimed = AppAgentRuntimeSession.objects.filter(
        id=session.id,
        status='pending',
        stop_requested=False,
    ).update(
        status='running',
        started_at=session.started_at or timezone.now(),
        finished_at=None,
        error_message='',
    )
    if not claimed:
        logger.info('APP Runtime Agent 会话不允许启动: session=%s status=%s', session.id, session.status)
        return
    session.refresh_from_db()
    task = session.task
    task.status = 'runtime_executing'
    task.progress = max(task.progress, 40)
    task.started_at = task.started_at or timezone.now()
    task.finished_at = None
    task.error_message = ''
    _save_runtime_state(task, [
        'status', 'progress', 'started_at', 'finished_at', 'error_message', 'updated_at'
    ])
    try:
        from .matrix_service import mark_matrix_cell_running

        mark_matrix_cell_running(session)
    except Exception as exc:
        logger.warning('标记兼容性矩阵设备开始失败: session=%s error=%s', session.id, exc)

    exploratory = is_exploratory_session(session)
    candidates = [] if exploratory else validate_runtime_candidate_set(task)
    candidate_map = {item['candidate_id']: item for item in candidates}
    invocation_started = time.monotonic()
    base_duration = float(session.used_duration_seconds or 0)

    record_agent_event(
        task,
        'runtime_observe',
        f'自治会话 #{session.id} 开始观察设备并动态决策',
        payload={
            'session_id': session.id,
            'candidate_count': 'dynamic' if exploratory else len(candidates),
            'mode': EXPLORATORY_SESSION_MODE if exploratory else 'controlled_case',
            'budgets': {
                'steps': session.max_steps,
                'seconds': session.max_duration_seconds,
                'model_tokens': session.max_model_tokens,
            },
        },
    )

    try:
        with RuntimeDeviceContext(session) as runner:
            approved_step = session.steps.filter(
                status='awaiting_approval',
                approval_status='approved',
            ).order_by('sequence').first()
            if approved_step:
                candidate = candidate_map.get(approved_step.action.get('candidate_id'))
                if not candidate and exploratory:
                    action = approved_step.action or {}
                    candidate = {
                        **action,
                        '_source_step': copy.deepcopy(action.get('step_config') or {}),
                        '_variables': [],
                    }
                elif candidate:
                    approved_config = (approved_step.action or {}).get('step_config')
                    if isinstance(approved_config, dict):
                        candidate = {
                            **candidate,
                            '_source_step': copy.deepcopy(approved_config),
                        }
                if not candidate:
                    raise RuntimeError('审批动作对应的用例或元素已变更，无法继续执行')
                _execute_runtime_step(session, approved_step, candidate, runner)

            while True:
                session.refresh_from_db()
                if session.stop_requested or session.status == 'stopped':
                    session.status = 'stopped'
                    session.finished_at = timezone.now()
                    _save_runtime_state(session, ['status', 'finished_at', 'updated_at'])
                    try:
                        from .matrix_service import sync_matrix_cell

                        sync_matrix_cell(session)
                    except Exception as exc:
                        logger.warning('同步已停止矩阵设备失败: %s', exc)
                    return
                session.used_duration_seconds = _duration_used(session, base_duration, invocation_started)
                _save_runtime_state(session, ['used_duration_seconds', 'updated_at'])
                if session.used_steps >= session.max_steps:
                    _budget_failure(session, '自治 Agent 已达到最大动作步数，已安全停止')
                    return
                if session.used_duration_seconds >= session.max_duration_seconds:
                    _budget_failure(session, '自治 Agent 已达到最大运行时长，已安全停止')
                    return

                observation = observe_runtime(session, runner)
                latest_step = session.steps.order_by('-sequence').first()
                previous_step = latest_step if latest_step and latest_step.status == 'passed' else None
                _update_coverage(session, observation, previous_step)
                unchanged_state_action = bool(
                    previous_step
                    and previous_step.action.get('action_type') in STATE_CHANGING_ACTIONS
                    and previous_step.observation_hash == observation.get('state_hash')
                )
                if exploratory and unchanged_state_action:
                    _mark_ineffective_exploration_step(session, previous_step, observation)
                    session.repeated_state_count = 0
                elif unchanged_state_action:
                    session.repeated_state_count += 1
                else:
                    session.repeated_state_count = 0
                session.current_observation = observation
                _save_runtime_state(session, [
                    'coverage', 'repeated_state_count', 'current_observation', 'updated_at'
                ])

                issues = (observation.get('signals') or {}).get('issues') or []
                if 'crash_or_anr' in issues:
                    raise RuntimeError('观察到应用崩溃或无响应信号，自治执行已停止')
                if 'white_screen' in issues:
                    raise RuntimeError('观察到疑似白屏，自治执行已停止')
                if session.repeated_state_count >= session.repeat_state_limit:
                    if exploratory:
                        _finish_session(
                            session,
                            status='completed',
                            message=(
                                f'连续 {session.repeated_state_count} 次探索动作未改变页面，'
                                '已安全结束并保留探索证据'
                            ),
                            task_status='completed',
                        )
                        return
                    raise RuntimeError(
                        f'连续 {session.repeated_state_count} 次状态变更动作后页面未变化，判定为死循环'
                    )

                if exploratory:
                    candidates = collect_exploratory_candidates(session, observation, runner)
                    coverage = copy.deepcopy(session.coverage or {})
                    coverage['candidate_total'] = max(
                        int(coverage.get('candidate_total') or 0),
                        len(set(session.completed_candidate_ids or [])) + len(candidates),
                    )
                    session.coverage = coverage
                    _save_runtime_state(session, ['coverage', 'updated_at'])

                remaining = [
                    item for item in candidates
                    if item['candidate_id'] not in set(session.completed_candidate_ids or [])
                ]
                if not remaining:
                    _finish_session(
                        session,
                        status='completed',
                        message=(
                            '自动探索测试已无新的可执行页面动作，已留存探索证据'
                            if exploratory else
                            '自治 Agent 已完成全部授权动作并留存运行证据'
                        ),
                        task_status='completed',
                    )
                    return

                decision, candidate, used_tokens = decide_runtime_action(
                    session, observation, candidates
                )
                session.used_model_tokens = min(
                    session.max_model_tokens,
                    session.used_model_tokens + max(0, used_tokens),
                )
                session.current_decision = decision
                if not candidate:
                    _finish_session(
                        session,
                        status='completed',
                        message=decision.get('reason') or '自治 Agent 判断目标已完成',
                        task_status='completed',
                    )
                    return
                action = public_runtime_candidate(candidate)
                session.current_action = action
                sequence = (session.steps.order_by('-sequence').values_list('sequence', flat=True).first() or 0) + 1
                approval_category = candidate.get('approval_category') or ''
                runtime_step = retry_database_write(
                    lambda: AppAgentRuntimeStep.objects.create(
                        session=session,
                        sequence=sequence,
                        status='awaiting_approval' if approval_category else 'decided',
                        observation=observation,
                        observation_hash=observation.get('state_hash') or '',
                        decision=decision,
                        action=action,
                        element_id=candidate.get('element_id'),
                        approval_status='pending' if approval_category else 'not_required',
                        approval_category=approval_category,
                    )
                )
                _save_runtime_state(session, [
                    'used_model_tokens', 'current_decision', 'current_action', 'updated_at'
                ])
                record_agent_event(
                    task,
                    'runtime_decision',
                    f'自治步骤 {sequence} 决策：{candidate["name"]}',
                    level='warning' if approval_category else 'info',
                    payload={
                        'session_id': session.id,
                        'runtime_step_id': runtime_step.id,
                        'decision': decision,
                        'action': action,
                    },
                )
                if approval_category:
                    session.status = 'awaiting_approval'
                    session.used_duration_seconds = _duration_used(session, base_duration, invocation_started)
                    _save_runtime_state(session, ['status', 'used_duration_seconds', 'updated_at'])
                    task.status = 'awaiting_approval'
                    _save_runtime_state(task, ['status', 'updated_at'])
                    record_agent_event(
                        task,
                        'runtime_approval',
                        f'步骤“{candidate["name"]}”等待输入验证码',
                        level='warning',
                        payload={
                            'session_id': session.id,
                            'runtime_step_id': runtime_step.id,
                            'category': approval_category,
                        },
                    )
                    try:
                        from .matrix_service import sync_matrix_cell

                        sync_matrix_cell(session)
                    except Exception as exc:
                        logger.warning('同步矩阵审批状态失败: %s', exc)
                    return
                try:
                    _execute_runtime_step(session, runtime_step, candidate, runner)
                except Exception as exc:
                    if not exploratory:
                        raise
                    logger.warning('探索动作失败已跳过，继续探索: %s', exc)
                    record_agent_event(
                        task,
                        'runtime_error',
                        f'探索步骤 {sequence} 失败已跳过：{candidate["name"]}（{exc}）',
                        level='error',
                        payload={
                            'session_id': session.id,
                            'runtime_step_id': runtime_step.id,
                            'candidate_id': candidate['candidate_id'],
                        },
                    )
    except Exception as exc:
        logger.error('APP Runtime Agent 会话失败: %s', exc, exc_info=True)
        session.refresh_from_db()
        if session.status in ('awaiting_approval', 'stopped'):
            return
        session.used_duration_seconds = _duration_used(session, base_duration, invocation_started)
        _save_runtime_state(session, ['used_duration_seconds', 'updated_at'])
        _finish_session(
            session,
            status='failed',
            message=str(exc),
            task_status='failed',
        )


@transaction.atomic
def approve_runtime_step(
    session: AppAgentRuntimeSession,
    user,
    *,
    approved: bool,
    note: str = '',
    input_value: str = '',
) -> Optional[AppAgentRuntimeStep]:
    """确认当前验证码输入；拒绝后立即终止会话。"""
    locked = AppAgentRuntimeSession.objects.select_for_update().select_related('task').get(id=session.id)
    runtime_step = locked.steps.select_for_update().filter(
        status='awaiting_approval',
        approval_status='pending',
    ).order_by('-sequence').first()
    if not runtime_step:
        return None
    approval_input = str(input_value or '').strip()
    if approved and runtime_approval_requires_input(
        runtime_step.action or {}, runtime_step.approval_category
    ):
        if not approval_input:
            raise ValueError('请输入验证码后再批准执行')
        if len(approval_input) > 120:
            raise ValueError('验证码长度不能超过 120 个字符')
        runtime_step.action = apply_runtime_approval_input(
            runtime_step.action or {}, approval_input
        )
    runtime_step.approval_status = 'approved' if approved else 'rejected'
    runtime_step.approved_by = user
    runtime_step.approved_at = timezone.now()
    runtime_step.approval_note = str(note or '')[:500]
    if not approved:
        runtime_step.status = 'rejected'
        runtime_step.finished_at = timezone.now()
    runtime_step.save(update_fields=[
        'approval_status', 'approved_by', 'approved_at', 'approval_note',
        'action', 'status', 'finished_at',
    ])
    if approved:
        locked.status = 'pending'
        locked.error_message = ''
        locked.save(update_fields=['status', 'error_message', 'updated_at'])
        locked.task.status = 'runtime_executing'
        locked.task.save(update_fields=['status', 'updated_at'])
    else:
        locked.status = 'stopped'
        locked.stop_requested = True
        locked.error_message = '验证码输入被拒绝'
        locked.finished_at = timezone.now()
        locked.save(update_fields=[
            'status', 'stop_requested', 'error_message', 'finished_at', 'updated_at'
        ])
        locked.task.status = 'stopped'
        locked.task.error_message = '验证码输入被拒绝，自治会话已停止'
        locked.task.finished_at = timezone.now()
        locked.task.save(update_fields=[
            'status', 'error_message', 'finished_at', 'updated_at'
        ])
    return runtime_step
