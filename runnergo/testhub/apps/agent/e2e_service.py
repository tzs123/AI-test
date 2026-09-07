from __future__ import annotations

import re
import uuid

from django.db import transaction
from django.db.models import Q

from apps.projects.models import Project
from backend.agent.e2e.planner_agent import PlannerAgent
from backend.agent.url_utils import infer_http_url

from .models import AgentMemory, AgentTask, E2ETask


_INTERACTIVE_WEB_ACTIONS = {'fill', 'click', 'submit', 'select', 'check', 'uncheck', 'press', 'hover'}
_INTERACTIVE_MOBILE_ACTIONS = {'tap', 'input', 'swipe', 'back'}
SUPPLEMENT_MARKER = '--- 用户补充的执行条件 ---'


def accessible_e2e_tasks(user):
    queryset = E2ETask.objects.select_related('project', 'created_by', 'agent_task')
    if user.is_staff or user.is_superuser:
        return queryset
    return queryset.filter(
        Q(created_by=user) | Q(project__owner=user) | Q(project__members=user)
    ).distinct()


def accessible_project(user, project_id):
    if not project_id:
        return None
    queryset = Project.objects.all()
    if not (user.is_staff or user.is_superuser):
        queryset = queryset.filter(Q(owner=user) | Q(members=user)).distinct()
    return queryset.filter(pk=project_id).first()


def apply_e2e_plan(*, task: E2ETask, agent_task: AgentTask | None, plan: dict) -> list[str]:
    """Persist a planner result to both the E2E task and its Agent projection."""
    missing = list(dict.fromkeys(plan.get('needs_input') or []))
    task.scenario = plan['scenario']
    task.steps = plan['steps']
    task.test_data = plan['test_data']
    task.expected_result = plan['expected_result']
    task.status = E2ETask.STATUS_NEEDS_INPUT if missing else E2ETask.STATUS_READY
    task.result = {
        'plan_source': plan.get('source'),
        'planner': plan.get('planner') or {},
        'needs_input': missing,
    }
    task.analysis = {}
    task.report_url = ''
    task.error_message = ''
    task.started_at = None
    task.finished_at = None
    task.save(update_fields=[
        'scenario', 'steps', 'test_data', 'expected_result', 'status', 'result',
        'analysis', 'report_url', 'error_message', 'started_at', 'finished_at', 'updated_time',
    ])

    if agent_task is not None:
        agent_task.status = AgentTask.STATUS_NEEDS_INPUT if missing else AgentTask.STATUS_CREATED
        agent_task.progress = 20
        agent_task.current_step = (
            f"测试方案已生成，待补充：{', '.join(missing)}"
            if missing else 'E2E 测试方案已生成，等待执行'
        )
        agent_task.plan = plan['steps']
        agent_task.test_plan = {
            'scenario': plan['scenario'],
            'test_data': plan['test_data'],
            'expected_result': plan['expected_result'],
        }
        agent_task.save(update_fields=[
            'status', 'progress', 'current_step', 'plan', 'test_plan', 'updated_at',
        ])
        AgentMemory.objects.create(
            task=agent_task,
            project=task.project,
            memory_type='plan',
            role='planner_agent',
            content=f'E2E Planner 已生成 {len(plan["steps"])} 个测试步骤',
            payload=plan,
        )
    return missing


def refresh_e2e_plan(task: E2ETask) -> list[str]:
    """Re-plan an existing task whose rules plan lacked executable flow steps."""
    plan = PlannerAgent().plan(
        task.description,
        executor_type=task.executor_type,
        target_url=task.target_url,
        mobile_config=task.mobile_config or {},
    )
    return apply_e2e_plan(task=task, agent_task=task.agent_task, plan=plan)


def supplemented_description(
    description: str,
    *,
    business_steps: str = '',
    acceptance_criteria: str = '',
) -> str:
    base = str(description or '').split(SUPPLEMENT_MARKER, 1)[0].rstrip()
    supplement = []
    if str(business_steps or '').strip():
        supplement.append(f'可执行业务步骤：\n{str(business_steps).strip()}')
    if str(acceptance_criteria or '').strip():
        supplement.append(f'验收标准：\n{str(acceptance_criteria).strip()}')
    if not supplement:
        return base
    return f'{base}\n\n{SUPPLEMENT_MARKER}\n' + '\n'.join(supplement)


@transaction.atomic
def promote_agent_task_to_web_e2e(
    *,
    task: AgentTask,
    target_url: str = '',
    business_steps: str = '',
    acceptance_criteria: str = '',
) -> E2ETask:
    """Upgrade a planning-only UI Agent task into an executable Web E2E task."""
    task = AgentTask.objects.select_for_update().select_related('project', 'created_by').get(pk=task.pk)
    existing = E2ETask.objects.filter(agent_task=task).first()
    if existing is not None:
        return existing

    description = supplemented_description(
        task.user_requirement,
        business_steps=business_steps,
        acceptance_criteria=acceptance_criteria,
    )
    context = dict(task.context or {})
    resolved_target = str(target_url or '').strip() or next((
        infer_http_url(context.get(key))
        for key in ('target_url', 'frontend_url', 'url', 'target')
        if infer_http_url(context.get(key))
    ), '') or infer_http_url(description)
    trace_id = uuid.uuid4().hex

    context.update({
        'source': 'ai_e2e_testing_agent',
        'trace_id': trace_id,
        'executor_type': E2ETask.EXECUTOR_WEB,
        'target_url': resolved_target,
        'frontend_url': resolved_target,
    })
    task.user_requirement = description
    task.context = context
    task.status = AgentTask.STATUS_ANALYZING
    task.progress = 10
    task.current_step = 'E2E Planner 正在根据补充条件生成可执行测试方案'
    task.error_message = ''
    task.finished_at = None
    task.save(update_fields=[
        'user_requirement', 'context', 'status', 'progress', 'current_step',
        'error_message', 'finished_at', 'updated_at',
    ])

    e2e_task = E2ETask.objects.create(
        project=task.project,
        created_by=task.created_by,
        agent_task=task,
        task_name=task.task_name,
        description=description,
        executor_type=E2ETask.EXECUTOR_WEB,
        target_url=resolved_target,
        status=E2ETask.STATUS_PLANNING,
        trace_id=trace_id,
    )
    plan = PlannerAgent().plan(
        description,
        executor_type=E2ETask.EXECUTOR_WEB,
        target_url=resolved_target,
        mobile_config={},
    )
    apply_e2e_plan(task=e2e_task, agent_task=task, plan=plan)
    e2e_task.refresh_from_db()
    return e2e_task


def e2e_plan_requires_refresh(task: E2ETask) -> bool:
    """Identify legacy smoke-only plans before they can be reported as full E2E."""
    steps = task.steps if isinstance(task.steps, list) else []
    actions = {str(step.get('action') or '').lower() for step in steps if isinstance(step, dict)}
    interactive_actions = (
        _INTERACTIVE_WEB_ACTIONS
        if task.executor_type == E2ETask.EXECUTOR_WEB
        else _INTERACTIVE_MOBILE_ACTIONS
    )
    if actions & interactive_actions:
        return False
    description = str(task.description or '')
    has_structured_points = len(re.findall(r'【([^】]+)】', description)) >= 2
    business_keywords = ('流程', '链路', '申请', '提交', '交易', '表单', '登录', '注册', '下单', '支付')
    return has_structured_points or any(keyword in description for keyword in business_keywords)


@transaction.atomic
def create_e2e_task(*, user, validated_data: dict) -> E2ETask:
    project_id = validated_data.get('project_id')
    project = accessible_project(user, project_id)
    if project_id and project is None:
        raise PermissionError('项目不存在或无权访问')
    description = validated_data['description']
    task_name = validated_data.get('task_name') or _task_name(description)
    executor_type = validated_data.get('executor_type', 'web')
    target_url = validated_data.get('target_url', '')
    mobile_config = validated_data.get('mobile_config') or {}
    trace_id = uuid.uuid4().hex

    agent_task = AgentTask.objects.create(
        task_name=task_name,
        user_requirement=description,
        status=AgentTask.STATUS_ANALYZING,
        progress=10,
        current_step='E2E Planner 正在生成测试方案',
        project=project,
        created_by=user,
        context={
            'source': 'ai_e2e_testing_agent',
            'trace_id': trace_id,
            'executor_type': executor_type,
            'target_url': target_url,
        },
    )
    task = E2ETask.objects.create(
        project=project,
        created_by=user,
        agent_task=agent_task,
        task_name=task_name,
        description=description,
        executor_type=executor_type,
        target_url=target_url,
        mobile_config=mobile_config,
        status=E2ETask.STATUS_PLANNING,
        trace_id=trace_id,
    )

    try:
        plan = PlannerAgent().plan(
            description,
            executor_type=executor_type,
            target_url=target_url,
            mobile_config=mobile_config,
        )
    except Exception as exc:
        task.status = E2ETask.STATUS_FAILED
        task.error_message = str(exc)[:10000]
        task.save(update_fields=['status', 'error_message', 'updated_time'])
        agent_task.status = AgentTask.STATUS_FAILED
        agent_task.progress = 100
        agent_task.current_step = 'E2E 测试规划失败'
        agent_task.error_message = str(exc)[:10000]
        agent_task.save(update_fields=['status', 'progress', 'current_step', 'error_message', 'updated_at'])
        raise

    apply_e2e_plan(task=task, agent_task=agent_task, plan=plan)
    return task


def _task_name(description: str) -> str:
    compact = ' '.join(str(description).split())
    return compact[:197] + ('...' if len(compact) > 197 else '')
