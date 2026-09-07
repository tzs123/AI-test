from __future__ import annotations

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from celery import current_app, shared_task
from celery.signals import worker_ready
from django.db import close_old_connections, transaction
from django.utils import timezone

from backend.agent.e2e.browser_agent import BrowserAgent
from backend.agent.e2e.mobile_agent import MobileAgent

from .models import E2EExecutionLog, E2ETask

logger = logging.getLogger(__name__)
E2E_QUEUE_STALE_SECONDS = 30
_DATA_REFERENCE = re.compile(r'\$\{(test_data(?:\.[A-Za-z0-9_-]+)+)\}')
_PLAN_RESULT_KEYS = {'plan_source', 'planner', 'needs_input'}


@shared_task(
    bind=True,
    name='agent.execute_e2e_task',
    acks_late=True,
    reject_on_worker_lost=True,
)
def execute_e2e_task(self, task_id: int, *, headless: bool = True, record_video: bool = True):
    task = E2ETask.objects.get(pk=task_id)
    raw_steps = []
    attempt = None
    step_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f'e2e-step-{task_id}')

    def persist_step(step_result):
        if attempt is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            _persist_step_result(task_id, attempt, raw_steps, step_result)
            return
        step_writer.submit(_persist_step_result_in_thread, task_id, attempt, raw_steps, step_result).result()

    try:
        raw_steps = _execution_steps(task)
        attempt = _start_execution(task_id, celery_task_id=str(self.request.id or ''))
        _seed_pending_steps(task_id, attempt, raw_steps)
        if task.executor_type == E2ETask.EXECUTOR_WEB:
            result = BrowserAgent().execute(
                url=task.target_url,
                steps=raw_steps,
                task_id=str(task.id),
                trace_id=task.trace_id,
                headless=headless,
                record_video=record_video,
                on_step=persist_step,
            )
        else:
            result = MobileAgent().execute(
                user=task.created_by,
                platform=task.executor_type,
                mobile_config=task.mobile_config,
                steps=raw_steps,
                task_id=str(task.id),
                trace_id=task.trace_id,
                record_video=record_video,
                on_step=persist_step,
            )
        video = str(result.get('video') or '')[:500]
        log_updates = {}
        if video:
            log_updates['video'] = video
        if result.get('device_log'):
            log_updates['device_log'] = str(result['device_log'])[:100000]
        if log_updates:
            E2EExecutionLog.objects.filter(task_id=task_id, attempt=attempt).update(**log_updates)
        final_status = E2ETask.STATUS_PASSED if result.get('status') == 'passed' else E2ETask.STATUS_FAILED
        _finish_execution(
            task_id,
            status=final_status,
            result=result,
            error=str(result.get('error') or '')[:20000],
        )
        if final_status == E2ETask.STATUS_FAILED:
            from .e2e_analysis import analyze_e2e_task
            analyze_e2e_task(task_id)
        from backend.agent.e2e.report_agent import ReportAgent
        report_agent = ReportAgent()
        report_task = E2ETask.objects.get(pk=task_id)
        report_path = report_agent.generate(report_task)
        report_task.report_url = f'/api/e2e/report/{report_task.id}'
        report_task.save(update_fields=['report_url', 'updated_time'])
        return result
    except Exception as exc:
        logger.exception('E2E task %s failed', task_id)
        error = str(exc)[:20000]
        _finish_execution(task_id, status=E2ETask.STATUS_FAILED, result={'status': 'failed', 'error': error}, error=error)
        try:
            from .e2e_analysis import analyze_e2e_task
            analyze_e2e_task(task_id)
        except Exception:
            logger.exception('Unable to analyze E2E execution exception')
        try:
            from backend.agent.e2e.report_agent import ReportAgent
            failed_task = E2ETask.objects.get(pk=task_id)
            ReportAgent().generate(failed_task)
            failed_task.report_url = f'/api/e2e/report/{failed_task.id}'
            failed_task.save(update_fields=['report_url', 'updated_time'])
        except Exception:
            logger.exception('Unable to generate report for failed E2E execution')
        raise
    finally:
        step_writer.shutdown(wait=True)


def _persist_step_result_in_thread(task_id: int, attempt: int, raw_steps: list, step_result: dict) -> None:
    close_old_connections()
    try:
        _persist_step_result(task_id, attempt, raw_steps, step_result)
    finally:
        close_old_connections()


def _execution_steps(task: E2ETask) -> list:
    steps = [dict(step) for step in task.steps] if isinstance(task.steps, list) else []
    steps = [_resolve_step_data(step, task.test_data if isinstance(task.test_data, dict) else {}) for step in steps]
    if task.executor_type == E2ETask.EXECUTOR_WEB:
        return steps
    mobile_config = task.mobile_config if isinstance(task.mobile_config, dict) else {}
    app_identifier = str(
        mobile_config.get('bundle_id') or mobile_config.get('app_package') or ''
    ).strip()
    if not app_identifier:
        return steps
    for step in steps:
        if step.get('action') == 'launch_app':
            if not step.get('value'):
                step['value'] = app_identifier
            break
    return steps


def _resolve_step_data(step: dict, test_data: dict) -> dict:
    return {key: _resolve_data_value(value, test_data) for key, value in step.items()}


def _resolve_data_value(value, test_data: dict):
    if isinstance(value, dict):
        return {key: _resolve_data_value(item, test_data) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_data_value(item, test_data) for item in value]
    if not isinstance(value, str) or '${test_data.' not in value:
        return value

    matches = list(_DATA_REFERENCE.finditer(value))
    if not matches:
        return value

    def lookup(reference: str):
        current = test_data
        for part in reference.split('.')[1:]:
            try:
                current = current[int(part)] if isinstance(current, list) else current[part]
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise RuntimeError(f'测试步骤引用了不存在的数据: ${{{reference}}}') from exc
        return current

    if len(matches) == 1 and matches[0].span() == (0, len(value)):
        return lookup(matches[0].group(1))

    return _DATA_REFERENCE.sub(lambda match: str(lookup(match.group(1))), value)


def _persist_step_result(task_id: int, attempt: int, raw_steps: list, step_result: dict) -> None:
    sequence = int(step_result.get('sequence') or 0)
    input_payload = raw_steps[sequence - 1] if 0 < sequence <= len(raw_steps) else {}
    status = _normalize_step_status(step_result.get('status'))
    E2EExecutionLog.objects.update_or_create(
        task_id=task_id,
        attempt=attempt,
        step=sequence,
        defaults={
            'step_name': str(step_result.get('name') or '')[:300],
            'action': str(step_result.get('action') or '')[:64],
            'status': status,
            'screenshot': str(step_result.get('screenshot') or '')[:500],
            'console_log': step_result.get('console_logs') or [],
            'network_log': step_result.get('network_logs') or [],
            'input_payload': input_payload,
            'output_payload': {
                'actual': step_result.get('actual'),
                'expected': step_result.get('expected'),
                'url': step_result.get('url'),
                'page_errors': step_result.get('page_errors') or [],
            },
            'error': str(step_result.get('error') or '')[:20000],
            'duration_ms': int(step_result.get('duration_ms') or 0),
        },
    )


def _normalize_step_status(status: object) -> str:
    normalized = str(status or '').upper()
    if normalized in {'PASSED', 'PASS', 'SUCCESS', 'SUCCEEDED'}:
        return 'PASSED'
    if normalized in {'RUNNING', 'IN_PROGRESS'}:
        return 'RUNNING'
    if normalized in {'PENDING', 'QUEUED'}:
        return 'PENDING'
    if normalized in {'SKIPPED', 'SKIP'}:
        return 'SKIPPED'
    return 'FAILED'


def _seed_pending_steps(task_id: int, attempt: int, raw_steps: list) -> None:
    for index, step in enumerate(raw_steps, 1):
        E2EExecutionLog.objects.update_or_create(
            task_id=task_id,
            attempt=attempt,
            step=index,
            defaults={
                'step_name': str(step.get('name') or f'步骤 {index}')[:300],
                'action': str(step.get('action') or '')[:64],
                'status': 'PENDING',
                'input_payload': step,
                'output_payload': {'message': '等待执行'},
                'error': '',
                'duration_ms': 0,
            },
        )


@transaction.atomic
def _start_execution(task_id: int, *, celery_task_id: str) -> int:
    task = E2ETask.objects.select_for_update().get(pk=task_id)
    redelivered_after_worker_loss = (
        task.status == E2ETask.STATUS_RUNNING
        and celery_task_id
        and celery_task_id == task.celery_task_id
    )
    if task.status not in {E2ETask.STATUS_QUEUED, E2ETask.STATUS_READY} and not redelivered_after_worker_loss:
        raise RuntimeError(f'E2E 任务状态 {task.status} 不允许开始执行')
    task.execution_attempt += 1
    task.status = E2ETask.STATUS_RUNNING
    task.started_at = timezone.now()
    task.finished_at = None
    previous_result = task.result if isinstance(task.result, dict) else {}
    task.result = {key: previous_result[key] for key in _PLAN_RESULT_KEYS if key in previous_result}
    task.analysis = {}
    task.report_url = ''
    task.error_message = ''
    if celery_task_id:
        task.celery_task_id = celery_task_id
    task.save(update_fields=[
        'execution_attempt', 'status', 'started_at', 'finished_at',
        'result', 'analysis', 'report_url', 'error_message', 'celery_task_id', 'updated_time',
    ])
    return task.execution_attempt


@transaction.atomic
def _finish_execution(task_id: int, *, status: str, result: dict, error: str = '') -> None:
    task = E2ETask.objects.select_for_update().get(pk=task_id)
    if task.status == E2ETask.STATUS_CANCELLED:
        return
    task.status = status
    previous_result = task.result if isinstance(task.result, dict) else {}
    task.result = {
        **{key: previous_result[key] for key in _PLAN_RESULT_KEYS if key in previous_result},
        **result,
    }
    task.error_message = error
    task.finished_at = timezone.now()
    task.save(update_fields=['status', 'result', 'error_message', 'finished_at', 'updated_time'])


def recover_orphaned_queued_e2e_tasks(*, stale_after_seconds=E2E_QUEUE_STALE_SECONDS) -> int:
    cutoff = timezone.now() - timezone.timedelta(seconds=max(int(stale_after_seconds), 0))
    task_ids = list(E2ETask.objects.filter(
        status=E2ETask.STATUS_QUEUED,
        updated_time__lt=cutoff,
    ).values_list('id', flat=True))
    recovered = 0
    for task_id in task_ids:
        with transaction.atomic():
            task = E2ETask.objects.select_for_update().get(pk=task_id)
            if task.status != E2ETask.STATUS_QUEUED or task.updated_time >= cutoff:
                continue
            previous_celery_task_id = task.celery_task_id
            task.status = E2ETask.STATUS_READY
            task.celery_task_id = ''
            task.error_message = ''
            task.finished_at = None
            task.save(update_fields=[
                'status', 'celery_task_id', 'error_message', 'finished_at', 'updated_time',
            ])

        if previous_celery_task_id:
            try:
                current_app.control.revoke(previous_celery_task_id, terminate=False)
            except Exception:
                logger.warning(
                    '撤销孤儿 E2E Celery 任务失败: %s',
                    previous_celery_task_id,
                    exc_info=True,
                )
        try:
            celery_result = execute_e2e_task.delay(task_id, headless=True, record_video=True)
        except Exception as exc:
            E2ETask.objects.filter(pk=task_id, status=E2ETask.STATUS_READY).update(
                status=E2ETask.STATUS_FAILED,
                error_message=f'E2E 孤儿任务重新入队失败: {exc}'[:20000],
                finished_at=timezone.now(),
                updated_time=timezone.now(),
            )
            logger.exception('E2E 孤儿任务重新入队失败: task_id=%s', task_id)
            continue
        E2ETask.objects.filter(pk=task_id, status=E2ETask.STATUS_READY).update(
            status=E2ETask.STATUS_QUEUED,
            celery_task_id=celery_result.id,
            updated_time=timezone.now(),
        )
        recovered += 1
        logger.warning(
            '已重新入队孤儿 E2E 任务: task_id=%s, old_celery_task_id=%s, new_celery_task_id=%s',
            task_id,
            previous_celery_task_id,
            celery_result.id,
        )
    return recovered


@worker_ready.connect
def recover_e2e_tasks_on_worker_ready(sender=None, **kwargs):
    try:
        recovered = recover_orphaned_queued_e2e_tasks()
        if recovered:
            logger.warning('E2E Worker 启动恢复孤儿排队任务: %s 条', recovered)
    except Exception:
        logger.exception('E2E Worker 启动恢复孤儿排队任务失败')
