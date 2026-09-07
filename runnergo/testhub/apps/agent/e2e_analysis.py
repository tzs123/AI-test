from __future__ import annotations

from django.db import transaction

from backend.agent.e2e.analyzer_agent import AnalyzerAgent

from .models import E2ETask


@transaction.atomic
def analyze_e2e_task(task_id: int) -> E2ETask:
    task = E2ETask.objects.select_for_update().get(pk=task_id)
    previous_status = task.status
    task.status = E2ETask.STATUS_ANALYZING
    task.save(update_fields=['status', 'updated_time'])
    logs = list(task.execution_logs.filter(attempt=task.execution_attempt).order_by('step', 'id'))
    failed = next((item for item in logs if item.status == 'FAILED'), logs[-1] if logs else None)
    result = task.result if isinstance(task.result, dict) else {}
    report = AnalyzerAgent().analyze(
        task_name=task.task_name,
        scenario=task.scenario,
        steps=[{
            'name': item.step_name,
            'action': item.action,
            'status': item.status,
            'error': item.error,
        } for item in logs] or task.steps,
        screenshot=(failed.screenshot if failed else '') or next(iter(result.get('screenshots') or []), ''),
        console_logs=(failed.console_log if failed else result.get('console_logs')) or [],
        network_logs=(failed.network_log if failed else result.get('network_logs')) or [],
        device_log=(failed.device_log if failed else result.get('device_log')) or '',
        stack_trace=str(result.get('stack_trace') or ''),
        error=(failed.error if failed else '') or task.error_message or result.get('error') or '',
    )
    task.analysis = report
    task.status = E2ETask.STATUS_FAILED if previous_status != E2ETask.STATUS_PASSED else E2ETask.STATUS_PASSED
    task.save(update_fields=['analysis', 'status', 'updated_time'])
    return task
