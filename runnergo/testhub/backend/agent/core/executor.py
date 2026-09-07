from __future__ import annotations

from typing import Dict

from django.db import close_old_connections, transaction
from django.utils import timezone

from apps.agent.models import AgentExecutionLog, AgentStep, AgentTask, TestExecutionResult
from backend.agent.executor import STATUS_BY_TYPE

from .decision_engine import DecisionEngine
from .memory import ReActMemory
from ..needs_input import derive_task_needs_input, needs_input_message
from .observer import Observer
from .planner import ReActPlanner
from .reflection import ReflectionEngine
from .tool_registry import ToolRegistry, default_registry


class AgentStopped(Exception):
    """Raised when a user has requested the current Agent task to stop."""


class ReActAgentExecutor:
    """Thought -> Action -> Observation -> Reflection Agent executor."""

    def __init__(self, task: AgentTask, registry: ToolRegistry | None = None):
        self.task = task
        self.registry = registry or default_registry
        self.memory = ReActMemory()
        self.decisions = DecisionEngine()
        self.observer = Observer()
        self.reflection = ReflectionEngine()
        self.attempts: Dict[int, int] = {}

    def run(self) -> AgentTask:
        try:
            self._halt_if_stopped()
            self._start()
            self._halt_if_stopped()
            plan = ReActPlanner().plan(self.task.user_requirement, self.task.context or {})
            self._halt_if_stopped()
            self._persist_plan(plan)
            self._halt_if_stopped()
            self._react_loop()
            self._halt_if_stopped()
            self._finish()
        except AgentStopped:
            return self.task
        except Exception as exc:
            self.task.status = AgentTask.STATUS_FAILED
            self.task.current_step = 'Agent 执行失败'
            self.task.error_message = str(exc)
            self.task.finished_at = timezone.now()
            self.task.save(update_fields=['status', 'current_step', 'error_message', 'finished_at', 'updated_at'])
            self._log('reflection', f'Agent 执行失败：{exc}', status='failed')
            self.memory.remember(self.task, f'Agent 执行失败：{exc}', memory_type='event', payload={'error': str(exc)})
        return self.task

    def _halt_if_stopped(self) -> None:
        self.task.refresh_from_db()
        if self.task.status == AgentTask.STATUS_STOPPED:
            raise AgentStopped()

    def _start(self) -> None:
        context = dict(self.task.context or {})
        context.pop('self_heal', None)
        context.pop('needs_input', None)
        self.task.status = AgentTask.STATUS_ANALYZING
        self.task.progress = 5
        self.task.current_step = '正在分析需求'
        self.task.started_at = timezone.now()
        self.task.finished_at = None
        self.task.error_message = ''
        self.task.failure_analysis = {}
        self.task.context = context
        self.task.save(update_fields=[
            'status', 'progress', 'current_step', 'started_at',
            'finished_at', 'error_message', 'failure_analysis', 'context', 'updated_at',
        ])
        self.task.steps.all().delete()
        self.task.execution_logs.all().delete()
        self.memory.remember(self.task, 'Thought: 开始理解用户测试目标', memory_type='event')
        self._log('thought', f'用户只输入一句目标：{self.task.user_requirement}。需要自动拆解测试计划并调用工具验证。')

    def _persist_plan(self, planner_output: Dict) -> None:
        steps = planner_output['steps']
        with transaction.atomic():
            self.task.plan = steps
            self.task.test_plan = planner_output['test_plan']
            recalled = self.memory.recall_project(self.task)
            self.task.context = {
                **(self.task.context or {}),
                'summary': planner_output.get('summary', ''),
                'source': planner_output.get('source', 'react-rules'),
                'recalled_memory_count': len(recalled),
            }
            self.task.progress = 15
            self.task.current_step = f'已生成 {len(steps)} 个 Agent 动作'
            self.task.save(update_fields=['plan', 'test_plan', 'context', 'progress', 'current_step', 'updated_at'])
            for item in steps:
                AgentStep.objects.create(
                    task=self.task,
                    step=item['step'],
                    step_type=item['type'],
                    action=item['action'],
                    tool_name=item['tool'],
                    input_payload=item.get('input') or {},
                )
        self.memory.remember(self.task, 'Thought: 测试计划已生成', memory_type='plan', payload=planner_output)
        self._log('thought', '已生成测试计划、测试数据策略、执行动作和失败分析动作。', result=planner_output)

    def _react_loop(self) -> None:
        while True:
            self._halt_if_stopped()
            steps = list(self.task.steps.order_by('step', 'id'))
            step = self.decisions.next_step(steps)
            if not step:
                break
            self._run_step(step, len(steps))
            self._halt_if_stopped()
            if step.status == AgentStep.STATUS_FAILED:
                self._run_failure_closure(steps)
                break

    def _run_step(self, step: AgentStep, total: int) -> None:
        self._halt_if_stopped()
        self.attempts[step.id] = self.attempts.get(step.id, 0) + 1
        thought = self.decisions.thought_for(step)
        self.task.status = STATUS_BY_TYPE.get(step.step_type, AgentTask.STATUS_RUNNING)
        self.task.current_step = step.action
        self.task.save(update_fields=['status', 'current_step', 'updated_at'])
        self._log('thought', thought, step=step, tool=step.tool_name)
        self.memory.remember(self.task, f'Thought: {thought}', step=step, memory_type='event')

        step.status = AgentStep.STATUS_RUNNING
        step.started_at = timezone.now()
        step.error_message = ''
        step.save(update_fields=['status', 'started_at', 'error_message', 'updated_at'])
        self._log('action', step.action, step=step, tool=step.tool_name, params=step.input_payload)

        started_at = timezone.now()
        # Do not keep a worker database connection alive while a tool waits on
        # a remote executor. All state above is already committed.
        close_old_connections()
        try:
            output = self.registry.execute(step.tool_name, self.task, step, step.input_payload or {})
        except Exception as exc:
            output = {
                'status': 'failed',
                'message': f'{step.tool_name} 执行异常：{exc}',
                'error': str(exc),
            }
        finally:
            close_old_connections()
        self._halt_if_stopped()
        observation = self.observer.observe(output)
        self._log(
            'observation',
            observation['message'],
            step=step,
            tool=step.tool_name,
            result=observation,
            status=observation['status'],
        )
        self.memory.remember(
            self.task,
            f"Observation: {observation['message']}",
            step=step,
            memory_type='tool_result',
            payload=output,
        )

        reflection = self.reflection.reflect(observation, attempt=self.attempts[step.id])
        self._log('reflection', reflection['reason'], step=step, tool=step.tool_name, result=reflection)
        self.memory.remember(self.task, f"Reflection: {reflection['reason']}", step=step, memory_type='analysis', payload=reflection)

        if reflection['decision'] == 'retry':
            step.status = AgentStep.STATUS_PENDING
            step.output_payload = {**output, 'retryable': True}
            step.save(update_fields=['status', 'output_payload', 'updated_at'])
            return

        step.output_payload = output
        output_status = str(output.get('status') or '').lower()
        if output_status == 'not_applicable':
            step.status = AgentStep.STATUS_NOT_APPLICABLE
        elif output_status == 'skipped':
            step.status = AgentStep.STATUS_SKIPPED
        else:
            step.status = AgentStep.STATUS_SUCCESS if observation['success'] else AgentStep.STATUS_FAILED
        if step.status == AgentStep.STATUS_FAILED:
            step.error_message = observation['message']
            self._persist_failed_result(step, output, started_at)
        step.finished_at = timezone.now()
        step.save(update_fields=['status', 'output_payload', 'error_message', 'finished_at', 'updated_at'])
        complete = self.task.steps.exclude(status=AgentStep.STATUS_PENDING).count()
        self.task.progress = min(95, 15 + int(complete / max(total, 1) * 80))
        self.task.save(update_fields=['progress', 'updated_at'])

    def _run_failure_closure(self, existing_steps: list[AgentStep]) -> None:
        """失败后仍执行归因和报告，保证任务有可复盘的终态产物。"""
        total = max(len(existing_steps) + 2, 1)
        analysis = next(
            (item for item in existing_steps if item.tool_name == 'analyze_failure' and item.status == AgentStep.STATUS_PENDING),
            None,
        )
        if analysis is None:
            analysis = AgentStep.objects.create(
                task=self.task,
                step=self.task.steps.order_by('-step', '-id').first().step + 1 if self.task.steps.exists() else 1,
                step_type='analysis',
                action='AI 分析失败原因',
                tool_name='analyze_failure',
                input_payload={},
            )
        self._run_step(analysis, total)

        for tool_name in ('self_heal_test', 'rerun_fixed_test'):
            closure_step = next(
                (
                    item for item in existing_steps
                    if item.tool_name == tool_name and item.status == AgentStep.STATUS_PENDING
                ),
                None,
            )
            if closure_step is not None:
                self._run_step(closure_step, total)

        report = next(
            (item for item in existing_steps if item.tool_name == 'create_report' and item.status == AgentStep.STATUS_PENDING),
            None,
        )
        if report is None:
            report = AgentStep.objects.create(
                task=self.task,
                step=self.task.steps.order_by('-step', '-id').first().step + 1 if self.task.steps.exists() else 1,
                step_type='report',
                action='生成失败测试报告',
                tool_name='create_report',
                input_payload={},
            )
        self._run_step(report, total)

    def _persist_failed_result(self, step: AgentStep, output: dict, started_at) -> None:
        if output.get('execution_result_id'):
            return
        TestExecutionResult.objects.create(
            task=self.task,
            step=step,
            module=step.step_type or 'agent',
            status='FAILED',
            request_payload=step.input_payload or {},
            response_payload=output,
            logs=str(output.get('message') or output.get('error') or '工具执行失败'),
            screenshot=str(output.get('screenshot') or ''),
            started_at=started_at,
            finished_at=timezone.now(),
            duration_ms=max(int((timezone.now() - started_at).total_seconds() * 1000), 1),
        )

    def _finish(self) -> None:
        current_results = self.task.execution_results.all()
        if self.task.started_at:
            current_results = current_results.filter(created_at__gte=self.task.started_at)
        current_results = list(current_results)
        real_results = [
            item for item in current_results
            if item.module != 'browser'
            and item.status in {'PASSED', 'FAILED'}
            and (item.response_payload or {}).get('real_execution') is not False
        ]
        has_failed = (
            self.task.steps.filter(status=AgentStep.STATUS_FAILED).exists()
            or any(item.status == 'FAILED' for item in current_results)
        )
        skipped_count = self.task.steps.filter(status=AgentStep.STATUS_SKIPPED).count()
        test_plan = self.task.test_plan if isinstance(self.task.test_plan, dict) else {}
        has_planned_tests = any(
            isinstance(items, list) and items
            for items in test_plan.values()
        )
        missing_real_execution = has_planned_tests and not real_results
        if skipped_count or missing_real_execution:
            self.task.status = AgentTask.STATUS_NEEDS_INPUT
            context = dict(self.task.context or {})
            needs_input = derive_task_needs_input(
                self.task,
                missing_real_execution=missing_real_execution,
                refresh=True,
            )
            context['needs_input'] = needs_input
            self.task.context = context
        else:
            self.task.status = AgentTask.STATUS_COMPLETED
            context = dict(self.task.context or {})
            context.pop('needs_input', None)
            self.task.context = context
        self.task.progress = 100
        if skipped_count:
            self.task.current_step = needs_input_message(self.task.context.get('needs_input') or [])
        elif missing_real_execution:
            self.task.current_step = needs_input_message(self.task.context.get('needs_input') or [])
        elif has_failed:
            self.task.current_step = '执行存在失败，已完成失败分析'
        elif not real_results:
            self.task.current_step = '流程已结束，当前没有需要执行的测试'
        else:
            has_report = self.task.assets.filter(asset_type='report').exists()
            self.task.current_step = '已生成测试报告' if has_report else '真实执行已完成，当前执行器未返回可查看报告'
        self.task.finished_at = timezone.now()
        self.task.save(update_fields=['status', 'progress', 'current_step', 'context', 'finished_at', 'updated_at'])
        final_log_status = 'pending' if skipped_count or missing_real_execution else 'failed' if has_failed else 'success'
        self._log('reflection', 'Agent 工作流结束，已完成执行结果汇总。', status=final_log_status)
        self.memory.remember(self.task, 'Reflection: Agent 工作流执行完成', memory_type='event')

    def _log(
        self,
        trace_type: str,
        action: str,
        *,
        step: AgentStep | None = None,
        tool: str = '',
        params: Dict | None = None,
        result: Dict | None = None,
        status: str = 'success',
    ) -> AgentExecutionLog:
        return AgentExecutionLog.objects.create(
            task=self.task,
            step=step,
            trace_type=trace_type,
            action=action[:4000],
            tool=tool or '',
            params=params or {},
            result=result or {},
            status=status,
        )
