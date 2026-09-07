from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from django.conf import settings

from .browser_controller import WebVisionAgent


def run_browser_agent(
    *,
    url: str = '',
    goal: str = '',
    case_file: str = '',
    project_id: str = '',
    headless: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """统一 Browser Agent 入口；case_file 存在时以 UI YAML 为执行来源。"""
    if case_file:
        case = _load_ui_yaml_case(case_file)
        case_url = url or str(case.get('base_url') or '').strip()
        case_goal = goal or str(case.get('name') or case.get('title') or '').strip()
        result = WebVisionAgent().run(
            url=case_url,
            goal=case_goal,
            inputs=kwargs.get('inputs') or case.get('variables') or None,
            headless=headless,
        )
        return {
            **result,
            'case_file': case_file,
            'project_id': project_id,
            'yaml_source': True,
            'yaml_step_count': len(case.get('steps') or []),
        }
    return WebVisionAgent().run(url=url, goal=goal, headless=headless, **kwargs)


def _load_ui_yaml_case(case_file: str) -> dict[str, Any]:
    raw = str(case_file or '').strip()
    if not raw:
        raise ValueError('需要 UI 自动化 YAML 用例文件')
    candidates = []
    if os.path.isabs(raw):
        candidates.append(Path(raw))
    else:
        workspace_root = Path(getattr(settings, 'BASE_DIR', os.getcwd())).resolve().parent
        candidates.extend([
            workspace_root / 'auto-test' / raw,
            workspace_root / 'auto-test' / 'cases' / 'ui' / raw,
            workspace_root / 'auto-test' / 'cases' / raw,
            Path(os.getcwd()) / raw,
        ])
        if not raw.endswith(('.yaml', '.yml')):
            candidates.append(workspace_root / 'auto-test' / 'cases' / 'ui' / f'{raw}.yaml')
    for path in candidates:
        if path.exists():
            with path.open('r', encoding='utf-8') as handle:
                return yaml.safe_load(handle) or {}
    raise ValueError(f'UI YAML 用例文件不存在: {case_file}')


__all__ = ['WebVisionAgent', 'run_browser_agent']
