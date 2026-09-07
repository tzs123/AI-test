from __future__ import annotations

from typing import Dict

from django.db import transaction
from django.utils import timezone

from apps.agent.models import AgentStep, AgentTask

from .memory import AgentMemoryStore
from .planner import TestPlanner
from .tools import TOOLS


STATUS_BY_TYPE: Dict[str, str] = {
    'data': AgentTask.STATUS_GENERATING_DATA,
    'case': AgentTask.STATUS_GENERATING_CASE,
    'api': AgentTask.STATUS_RUNNING,
    'ui': AgentTask.STATUS_RUNNING,
    'app': AgentTask.STATUS_RUNNING,
    'security': AgentTask.STATUS_RUNNING,
    'performance': AgentTask.STATUS_RUNNING,
    'analysis': AgentTask.STATUS_ANALYZING_RESULT,
    'report': AgentTask.STATUS_ANALYZING_RESULT,
}


class AgentExecutor:
    """顺序执行 Agent 计划，并在每一步更新状态。"""

    def __init__(self, task: AgentTask):
        self.task = task
        self.memory = AgentMemoryStore()

    def run(self) -> AgentTask:
        try:
            self._start()
            planner_output = TestPlanner().plan(self.task.user_requirement)
            self._persist_plan(planner_output)
            self._execute_steps()
            self._finish()
        except Exception as exc:
            self.task.status = AgentTask.STATUS_FAILED
            self.task.error_message = str(exc)
            self.task.finished_at = timezone.now()
            self.task.save(update_fields=['status', 'error_message', 'finished_at', 'updated_at'])
            self.memory.remember(self.task, f'Agent 执行失败：{exc}', memory_type='event', payload={'error': str(exc)})
        return self.task

    def _start(self) -> None:
        self.task.status = AgentTask.STATUS_ANALYZING
        self.task.progress = 5
        self.task.started_at = timezone.now()
        self.task.finished_at = None
        self.task.error_message = ''
        self.task.save(update_fields=['status', 'progress', 'started_at', 'finished_at', 'error_message', 'updated_at'])
        self.task.steps.all().delete()
        self.memory.remember(self.task, '开始分析用户需求', memory_type='event')

    def _persist_plan(self, planner_output) -> None:
        steps = planner_output['steps']
        with transaction.atomic():
            self.task.plan = steps
            self.task.test_plan = planner_output['test_plan']
            self.task.context = {'summary': planner_output.get('summary', ''), 'source': planner_output.get('source', 'rules')}
            self.task.progress = 15
            self.task.save(update_fields=['plan', 'test_plan', 'context', 'progress', 'updated_at'])
            for item in steps:
                AgentStep.objects.create(
                    task=self.task,
                    step=item['step'],
                    step_type=item['type'],
                    action=item['action'],
                    tool_name=item['tool'],
                    input_payload=item.get('input') or {},
                )
        self.memory.remember(self.task, '测试计划已生成并保存', memory_type='plan', payload=planner_output)

    def _execute_steps(self) -> None:
        steps = list(self.task.steps.order_by('step'))
        total = max(len(steps), 1)
        for index, step in enumerate(steps, start=1):
            self._run_step(step)
            self.task.progress = min(95, 15 + int(index / total * 80))
            self.task.save(update_fields=['progress', 'updated_at'])

    def _run_step(self, step: AgentStep) -> None:
        self.task.status = STATUS_BY_TYPE.get(step.step_type, AgentTask.STATUS_RUNNING)
        self.task.save(update_fields=['status', 'updated_at'])
        step.status = AgentStep.STATUS_RUNNING
        step.started_at = timezone.now()
        step.save(update_fields=['status', 'started_at', 'updated_at'])
        self.memory.remember(self.task, f'开始：{step.action}', step=step, memory_type='event')

        tool = TOOLS.get(step.tool_name)
        if not tool:
            raise RuntimeError(f'未注册工具：{step.tool_name}')
        try:
            output = tool(self.task, step, step.input_payload or {})
            step.output_payload = output or {}
            output_status = str(output.get('status') or '').lower()
            if output_status == 'not_applicable':
                step.status = AgentStep.STATUS_NOT_APPLICABLE
            elif output_status == 'skipped':
                step.status = AgentStep.STATUS_SKIPPED
            else:
                step.status = AgentStep.STATUS_SUCCESS
            step.finished_at = timezone.now()
            step.save(update_fields=['status', 'output_payload', 'finished_at', 'updated_at'])
            self.memory.remember(self.task, f'完成：{step.action}', step=step, memory_type='tool_result', payload=step.output_payload)
        except Exception as exc:
            step.status = AgentStep.STATUS_FAILED
            step.error_message = str(exc)
            step.finished_at = timezone.now()
            step.save(update_fields=['status', 'error_message', 'finished_at', 'updated_at'])
            self.memory.remember(self.task, f'失败：{step.action}', step=step, memory_type='event', payload={'error': str(exc)})
            raise

    def _finish(self) -> None:
        has_failed = self.task.steps.filter(status=AgentStep.STATUS_FAILED).exists()
        self.task.status = AgentTask.STATUS_FAILED if has_failed else AgentTask.STATUS_COMPLETED
        self.task.progress = 100
        self.task.finished_at = timezone.now()
        self.task.save(update_fields=['status', 'progress', 'finished_at', 'updated_at'])
        self.memory.remember(self.task, 'Agent 工作流执行完成', memory_type='event')
