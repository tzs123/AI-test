from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


ALLOWED_BROWSER_ACTIONS = frozenset({
    'navigate',
    'fill',
    'click',
    'submit',
    'wait_for_url',
    'wait_for_text',
    'wait_for_element',
    'assert_text',
    'assert_visible',
    'assert_url',
    'assert_title',
    'assert_value',
    'select',
    'check',
    'uncheck',
    'press',
    'hover',
    'screenshot',
})


class E2EContractError(ValueError):
    """Raised when an AI-generated action violates the executor contract."""


@dataclass(frozen=True)
class BrowserRunRequest:
    base_url: str
    steps: list[dict[str, Any]]
    task_id: str = ''
    trace_id: str = ''
    headless: bool = True
    record_video: bool = True
    screenshot_each_step: bool = True
    viewport: dict[str, int] = field(default_factory=lambda: {'width': 1440, 'height': 900})

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> 'BrowserRunRequest':
        if not isinstance(payload, dict):
            raise E2EContractError('浏览器执行请求必须是 JSON 对象')
        base_url = str(payload.get('base_url') or payload.get('url') or '').strip()
        raw_steps = payload.get('steps')
        if not isinstance(raw_steps, list) or not raw_steps:
            raise E2EContractError('steps 必须是非空数组')
        max_steps = int(payload.get('max_steps') or 200)
        if max_steps < 1 or max_steps > 500:
            raise E2EContractError('max_steps 必须在 1 到 500 之间')
        if len(raw_steps) > max_steps:
            raise E2EContractError(f'步骤数超过限制 {max_steps}')
        steps = [normalize_browser_step(item, index + 1) for index, item in enumerate(raw_steps)]
        viewport = payload.get('viewport') or {'width': 1440, 'height': 900}
        if not isinstance(viewport, dict):
            raise E2EContractError('viewport 必须是对象')
        width = _bounded_int(viewport.get('width'), default=1440, minimum=320, maximum=3840)
        height = _bounded_int(viewport.get('height'), default=900, minimum=320, maximum=2160)
        return cls(
            base_url=base_url,
            steps=steps,
            task_id=str(payload.get('task_id') or '')[:128],
            trace_id=str(payload.get('trace_id') or '')[:128],
            headless=bool(payload.get('headless', True)),
            record_video=bool(payload.get('record_video', True)),
            screenshot_each_step=bool(payload.get('screenshot_each_step', True)),
            viewport={'width': width, 'height': height},
        )


def normalize_browser_step(raw_step: Any, sequence: int) -> dict[str, Any]:
    if not isinstance(raw_step, dict):
        raise E2EContractError(f'第 {sequence} 步必须是对象')
    action = str(raw_step.get('action') or '').strip().lower()
    if action not in ALLOWED_BROWSER_ACTIONS:
        raise E2EContractError(f'第 {sequence} 步包含不支持的动作: {action or "<empty>"}')
    target = raw_step.get('target') or {}
    if isinstance(target, str):
        target = {'text': target}
    if not isinstance(target, dict):
        raise E2EContractError(f'第 {sequence} 步 target 必须是对象或文本')
    action_value = raw_step.get('value')
    action_expected = raw_step.get('expected')
    action_url = str(raw_step.get('url') or '').strip()
    if action == 'navigate' and not (action_url or action_value):
        action_url = str(target.get('url') or '').strip()
    if action in {'wait_for_url', 'assert_url'} and action_expected is None and action_value is None:
        action_expected = target.get('url')
    if action == 'assert_title' and action_expected is None and action_value is None:
        action_expected = target.get('title') or target.get('text')
    if action in {'wait_for_text', 'assert_text'} and action_expected is None and action_value is None:
        action_expected = target.get('text')
        if action == 'wait_for_text':
            action_value = action_expected
    timeout_ms = _bounded_int(raw_step.get('timeout_ms'), default=15000, minimum=100, maximum=120000)
    raw_name = str(raw_step.get('name') or raw_step.get('description') or '').strip()
    step = {
        'id': str(raw_step.get('id') or sequence)[:64],
        'name': (raw_name or _browser_step_name(
            action=action,
            target=target,
            url=action_url,
            value=action_value,
            expected=action_expected,
            sequence=sequence,
        ))[:300],
        'action': action,
        'target': {str(key): value for key, value in target.items() if value not in (None, '')},
        'value': action_value,
        'expected': action_expected,
        'url': action_url,
        'timeout_ms': timeout_ms,
        'continue_on_failure': bool(raw_step.get('continue_on_failure', False)),
    }
    if action == 'navigate' and not (step['url'] or step['value']):
        raise E2EContractError(f'第 {sequence} 步 navigate 缺少 url')
    if action in {'fill', 'select', 'press'} and step['value'] is None:
        raise E2EContractError(f'第 {sequence} 步 {action} 缺少 value')
    if action.startswith('assert_') and action != 'assert_visible' and step['expected'] is None:
        step['expected'] = step['value']
    if action in {'wait_for_text', 'wait_for_url', 'assert_text', 'assert_url', 'assert_title'}:
        expected = step['expected'] if step['expected'] is not None else step['value']
        if not str(expected or '').strip():
            raise E2EContractError(f'第 {sequence} 步 {action} 缺少非空期望值')
    return step


def _browser_step_name(
    *,
    action: str,
    target: dict[str, Any],
    url: str,
    value: Any,
    expected: Any,
    sequence: int,
) -> str:
    target_name = next((
        str(target.get(key) or '').strip()
        for key in ('label', 'name', 'placeholder', 'text', 'test_id', 'title', 'url', 'css')
        if str(target.get(key) or '').strip()
    ), '')
    expected_name = str(expected if expected is not None else value or '').strip()
    action_labels = {
        'navigate': '打开页面',
        'fill': '填写',
        'click': '点击',
        'submit': '提交',
        'select': '选择',
        'check': '勾选',
        'uncheck': '取消勾选',
        'press': '按键',
        'hover': '悬停',
        'wait_for_url': '等待页面地址',
        'wait_for_text': '等待文本',
        'wait_for_element': '等待元素',
        'assert_url': '验证页面地址',
        'assert_title': '验证页面标题',
        'assert_text': '验证文本',
        'assert_visible': '验证元素可见',
        'assert_value': '验证输入值',
        'screenshot': '保存页面证据',
    }
    subject = url or target_name or expected_name
    label = action_labels.get(action, f'执行 {action}')
    return f'{label}：{subject}' if subject else f'{label}（步骤 {sequence}）'


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise E2EContractError('数值配置格式无效') from exc
    if parsed < minimum or parsed > maximum:
        raise E2EContractError(f'数值配置必须在 {minimum} 到 {maximum} 之间')
    return parsed
