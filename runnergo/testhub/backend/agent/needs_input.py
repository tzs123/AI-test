from __future__ import annotations

import os
from typing import Iterable

from backend.agent.url_utils import infer_http_url


EXECUTABLE_ASSET_KEYS = (
    'ui_case_ids',
    'ui_test_case_ids',
    'ui_case_files',
    'case_files',
    'api_test_objects',
    'test_object_refs',
    'app_case_ids',
    'app_test_case_ids',
)


def derive_task_needs_input(
    task,
    *,
    missing_real_execution: bool = False,
    refresh: bool = False,
) -> list[str]:
    """Return actionable missing inputs instead of asking for arbitrary prose."""
    context = dict(task.context or {})
    explicit = [] if refresh else _normalize_keys(context.get('needs_input'))
    has_asset = any(context.get(key) for key in EXECUTABLE_ASSET_KEYS)
    has_target = bool(
        next(
            (
                context.get(key)
                for key in ('target_url', 'frontend_url', 'url', 'api_url', 'base_url')
                if context.get(key)
            ),
            '',
        )
        or infer_http_url(task.user_requirement)
    )

    remaining = []
    for key in explicit:
        if key == 'executable_asset' and has_asset:
            continue
        if key == 'execution_service' and os.environ.get('AUTO_TEST_AGENT_API_URL', '').strip():
            continue
        if key == 'target_url' and has_target:
            continue
        if key == 'business_steps' and str(context.get('business_steps') or '').strip():
            continue
        if key == 'test_point_coverage' and str(context.get('acceptance_criteria') or '').strip():
            continue
        remaining.append(key)
    if explicit:
        return list(dict.fromkeys(remaining))

    skipped_steps = list(task.steps.filter(status='SKIPPED'))
    messages = ' '.join(
        str((step.output_payload or {}).get('message') or step.error_message or '')
        for step in skipped_steps
    )
    if 'Auto-test 执行服务未配置' in messages:
        return ['execution_service']

    return ['executable_asset']


def needs_input_message(keys: Iterable[str]) -> str:
    normalized = set(keys)
    if 'execution_service' in normalized:
        return '已选择 UI 自动化用例，但 Auto-test 执行服务未配置，请配置服务后重新执行'
    if 'executable_asset' in normalized:
        return '缺少可执行测试资产，请选择已有 UI 自动化 YAML 用例后执行'
    if 'target_url' in normalized:
        return '缺少目标网址，请补充后重新执行'
    return '执行条件尚未补全，请补充后重新执行'


def _normalize_keys(value) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
