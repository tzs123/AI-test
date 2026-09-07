from __future__ import annotations

import os
from copy import deepcopy
from typing import Any

from django.db.models import Q

from apps.agent.models import AgentTask, TestAsset


def save_agent_task_as_test_case(
    task: AgentTask,
    *,
    user: Any,
    name: str = '',
    description: str = '',
) -> dict[str, Any]:
    """Persist an Agent run only after the user explicitly confirms it."""
    mode = 'app' if (task.context or {}).get('agent_service') == 'app' else 'ui'
    saved = (task.context or {}).get('case_save') or {}
    ui_cases_materialized = mode == 'ui' and bool(saved.get('case_files'))
    if saved.get('status') == 'saved' and saved.get('case_id') and (mode == 'app' or ui_cases_materialized):
        existing = _existing_saved_case(mode, saved['case_id'], user)
        if existing:
            payload = _case_payload(mode, existing)
            if mode == 'ui':
                payload.update({
                    'filename': saved.get('filename') or '',
                    'case_file': saved.get('case_file') or '',
                    'case_files': saved.get('case_files') or [],
                    'ui_project_id': saved.get('ui_project_id') or '',
                })
            return {
                'created': False,
                'message': '本次 Agent 步骤已经保存为测试用例',
                'test_case': payload,
            }

    ui_cases: list[dict[str, Any]] = []
    if mode == 'app':
        case = _save_app_case(task, user=user, name=name, description=description)
    else:
        case = _save_ui_case(task, user=user, name=name, description=description)
        ui_cases = _save_ui_automation_cases(
            task,
            name=name or f'[Agent #{task.id}] {task.task_name}',
            description=description or task.user_requirement,
        )

    context = dict(task.context or {})
    case_save = {
        'status': 'saved',
        'case_id': case.id,
        'agent_service': mode,
    }
    if ui_cases:
        primary = ui_cases[0]
        case_save.update({
            'filename': primary.get('filename') or '',
            'case_file': primary.get('relative') or '',
            'case_files': [item.get('relative') for item in ui_cases if item.get('relative')],
            'ui_project_id': primary.get('project_id') or '',
            'count': len(ui_cases),
        })
    context['case_save'] = case_save
    task.context = context
    task.save(update_fields=['context', 'updated_at'])
    TestAsset.objects.update_or_create(
        task=task,
        asset_id=f'saved_{mode}_case_{case.id}',
        defaults={
            'asset_type': 'app' if mode == 'app' else 'case',
            'name': case.name if mode == 'app' else case.title,
            'project': task.project,
            'source_model': (
                'apps.app_automation.AppTestCase'
                if mode == 'app'
                else 'apps.testcases.TestCase'
            ),
            'source_id': str(case.id),
            'metadata': {'agent_service': mode, 'confirmed_by_user': True},
            'created_by': user,
        },
    )
    for index, saved_ui_case in enumerate(ui_cases, 1):
        relative = str(saved_ui_case.get('relative') or '')
        TestAsset.objects.update_or_create(
            asset_id=f'saved_ui_yaml_{task.id}_{index}',
            defaults={
                'task': task,
                'asset_type': 'ui',
                'name': str(saved_ui_case.get('name') or saved_ui_case.get('filename') or '')[:200],
                'project': task.project,
                'source_model': 'auto-test.cases.ui',
                'source_id': relative,
                'metadata': {
                    'agent_service': mode,
                    'confirmed_by_user': True,
                    'filename': saved_ui_case.get('filename') or '',
                    'case_file': relative,
                    'ui_project_id': saved_ui_case.get('project_id') or '',
                    'source_case_file': saved_ui_case.get('source_case_file') or '',
                    'module_url': '/auto-test/cases',
                },
                'created_by': user,
            },
        )
    payload = _case_payload(mode, case)
    if ui_cases:
        payload.update({
            'filename': ui_cases[0].get('filename') or '',
            'case_file': ui_cases[0].get('relative') or '',
            'case_files': [item.get('relative') for item in ui_cases if item.get('relative')],
            'ui_project_id': ui_cases[0].get('project_id') or '',
        })
    return {
        'created': True,
        'message': (
            '已保存到 UI 自动化用例管理'
            if mode == 'ui'
            else '已保存到 APP 自动化用例管理'
        ),
        'test_case': payload,
    }


def _save_ui_case(task: AgentTask, *, user: Any, name: str, description: str):
    from apps.testcases.models import TestCaseStep
    from apps.testcases.services import TestCaseDeduplicationService

    if not task.project_id:
        raise ValueError('当前任务未选择 Agent 资产项目，无法保存 UI 测试用例')
    title = (name or f'[Agent #{task.id}] {task.task_name}')[:500]
    steps = list(task.steps.order_by('step', 'id'))
    if not steps:
        raise ValueError('当前任务没有可保存的 Agent 步骤')
    testcase, created = TestCaseDeduplicationService.create(
        project=task.project,
        title=title,
        description=description or task.user_requirement,
        preconditions='由 AI Test Agent 执行完成后经用户确认保存。',
        steps='; '.join(item.action for item in steps)[:1000],
        expected_result='Agent 执行结果与本次保存的步骤证据一致。',
        priority='medium',
        status='draft',
        test_type='ui',
        tags=['AI Agent', '用户确认保存'],
        author=user,
    )
    if created:
        TestCaseStep.objects.bulk_create([
            TestCaseStep(
                testcase=testcase,
                step_number=index,
                action=item.action,
                expected=_step_expected(item),
            )
            for index, item in enumerate(steps, 1)
        ])
    return testcase


def _save_ui_automation_cases(
    task: AgentTask,
    *,
    name: str,
    description: str,
) -> list[dict[str, Any]]:
    """Materialize the executable YAML in the Auto-test UI case library."""
    import requests

    context = task.context or {}
    source_case_files = []
    for key in ('ui_case_files', 'case_files'):
        values = context.get(key) or []
        if not isinstance(values, (list, tuple)):
            values = [values]
        for value in values:
            normalized = str(value or '').strip()
            if normalized and normalized not in source_case_files:
                source_case_files.append(normalized)
    if not source_case_files:
        raise ValueError('本次 Agent 执行没有可保存到用例管理的 UI YAML 用例')

    auto_test_url = os.environ.get('AUTO_TEST_AGENT_API_URL', '').strip()
    if not auto_test_url:
        raise ValueError('Auto-test 服务未配置，无法保存到 UI 自动化用例管理')
    token = os.environ.get('TEST_DATA_CENTER_AGENT_TOKEN', 'runnergo-local-agent-token')
    try:
        response = requests.post(
            f"{auto_test_url.rstrip('/')}/api/agent/internal/save-ui-cases",
            headers={'X-Agent-Token': token},
            json={
                'project_id': str(context.get('ui_project_id') or 'default'),
                'source_case_files': source_case_files,
                'agent_task_id': str(task.id),
                'name': name,
                'description': description,
            },
            timeout=int(os.environ.get('AUTO_TEST_AGENT_REQUEST_TIMEOUT_SECONDS', '30')),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        detail = ''
        if getattr(exc, 'response', None) is not None:
            try:
                detail = str(exc.response.json().get('detail') or '').strip()
            except (AttributeError, TypeError, ValueError):
                detail = ''
        raise ValueError(f'保存到 UI 自动化用例管理失败: {detail or exc}') from exc
    except ValueError as exc:
        raise ValueError(f'Auto-test 保存用例响应无效: {exc}') from exc

    cases = payload.get('cases') if isinstance(payload, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ValueError('Auto-test 未返回已保存的 UI 自动化用例')
    return [item for item in cases if isinstance(item, dict)]


def _save_app_case(task: AgentTask, *, user: Any, name: str, description: str):
    from apps.app_automation.models import AppPackage, AppProject, AppTestCase

    context = task.context or {}
    project_id = context.get('app_project_id') or context.get('app_project')
    project = AppProject.objects.filter(
        Q(owner=user) | Q(members=user),
        id=project_id,
    ).distinct().first()
    if not project:
        raise ValueError('当前任务未选择可访问的 APP 项目，无法保存 APP 测试用例')

    case_ids = context.get('app_case_ids') or context.get('app_test_case_ids') or []
    source_cases = list(AppTestCase.objects.filter(
        Q(created_by=user) | Q(project__owner=user) | Q(project__members=user),
        project=project,
        id__in=case_ids,
    ).select_related('app_package').distinct())
    order = {int(case_id): index for index, case_id in enumerate(case_ids) if str(case_id).isdigit()}
    source_cases.sort(key=lambda item: order.get(item.id, len(order)))
    flow = []
    variables = []
    variable_names = set()
    for source_case in source_cases:
        raw_flow = source_case.ui_flow
        source_steps = raw_flow if isinstance(raw_flow, list) else (raw_flow or {}).get('steps', [])
        flow.extend(deepcopy(source_steps or []))
        for variable in source_case.variables or []:
            variable_name = str(variable.get('name') or '') if isinstance(variable, dict) else ''
            if variable_name and variable_name in variable_names:
                continue
            if variable_name:
                variable_names.add(variable_name)
            variables.append(deepcopy(variable))
    if not flow:
        raise ValueError('本次 Agent 执行没有可保存的 APP 用例步骤')

    package_id = context.get('app_package_id') or context.get('app_package')
    app_package = AppPackage.objects.filter(id=package_id).first() if package_id else None
    if not app_package:
        app_package = next((item.app_package for item in source_cases if item.app_package_id), None)
    return AppTestCase.objects.create(
        project=project,
        name=(name or f'[Agent #{task.id}] {task.task_name}')[:200],
        description=description or task.user_requirement,
        app_package=app_package,
        ui_flow=flow,
        variables=variables,
        created_by=user,
    )


def _step_expected(step) -> str:
    output = step.output_payload if isinstance(step.output_payload, dict) else {}
    message = str(output.get('message') or step.error_message or '').strip()
    if message:
        return message
    return f'步骤状态为 {step.status}'


def _existing_saved_case(mode: str, case_id: int, user: Any):
    if mode == 'app':
        from apps.app_automation.models import AppTestCase

        return AppTestCase.objects.filter(
            Q(created_by=user) | Q(project__owner=user) | Q(project__members=user),
            id=case_id,
        ).distinct().first()
    from apps.testcases.models import TestCase

    return TestCase.objects.filter(
        Q(author=user) | Q(project__owner=user) | Q(project__members=user),
        id=case_id,
    ).distinct().first()


def _case_payload(mode: str, case) -> dict[str, Any]:
    return {
        'id': case.id,
        'name': case.name if mode == 'app' else case.title,
        'project_id': case.project_id,
        'agent_service': mode,
    }
