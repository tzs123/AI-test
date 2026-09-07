"""Celery and process-lifecycle helpers for AI test-case generation."""

from __future__ import annotations

import logging
import os

from celery import shared_task
from celery.signals import worker_ready
from django.db import transaction
from django.utils import timezone

from .models import AIModelService, TestCaseGenerationTask


logger = logging.getLogger(__name__)

GENERATION_TASK_STALE_SECONDS = max(
    # Review calls may legitimately wait up to 3,600 seconds. Leave a full
    # additional window before treating an unchanged row as abandoned.
    int(os.environ.get('GENERATION_TASK_STALE_SECONDS', '7200')),
    60,
)
GENERATION_TASK_INTERRUPTED_MESSAGE = (
    '生成任务执行进程已中断，服务重启后未能恢复。请重新提交任务。'
)
GENERATION_TASK_ACTIVE_STATUSES = ('pending', 'generating', 'reviewing', 'revising')


def recover_orphaned_generation_tasks(
    *,
    stale_after_seconds=GENERATION_TASK_STALE_SECONDS,
    updated_before=None,
) -> int:
    """Close generation tasks left active after a backend/worker process exit.

    Generation currently performs long-running model calls in the web process.
    A process exit cannot execute its exception handler, so these rows otherwise
    remain active forever.  Use a row lock and a conditional update to make the
    cleanup safe when a still-running process updates the same task concurrently.
    """
    cutoff = updated_before or (
        timezone.now() - timezone.timedelta(
            seconds=max(int(stale_after_seconds), 0),
        )
    )
    task_ids = list(
        TestCaseGenerationTask.objects.filter(
            status__in=GENERATION_TASK_ACTIVE_STATUSES,
            updated_at__lt=cutoff,
        ).values_list('id', flat=True)
    )

    recovered = 0
    for task_id in task_ids:
        with transaction.atomic():
            task = (
                TestCaseGenerationTask.objects.select_for_update()
                .filter(
                    id=task_id,
                    status__in=GENERATION_TASK_ACTIVE_STATUSES,
                    updated_at__lt=cutoff,
                )
                .first()
            )
            if task is None:
                continue

            previous_status = task.status
            finished_at = timezone.now()
            task.status = 'failed'
            task.error_message = GENERATION_TASK_INTERRUPTED_MESSAGE
            task.completed_at = finished_at
            task.save(update_fields=['status', 'error_message', 'completed_at', 'updated_at'])

            try:
                AIModelService.record_generation_event(
                    task,
                    'task',
                    'failed',
                    '生成任务已中止',
                    GENERATION_TASK_INTERRUPTED_MESSAGE,
                )
            except Exception:
                # Recovery must still succeed if the optional timeline write
                # fails (for example while the database is under pressure).
                logger.warning(
                    '记录中断生成任务事件失败: task=%s',
                    task.task_id,
                    exc_info=True,
                )
            recovered += 1
            logger.warning(
                '已收口中断的生成任务: task_id=%s, previous_status=%s',
                task.task_id,
                previous_status,
            )

    return recovered


@shared_task(name='requirement_analysis.recover_orphaned_generation_tasks')
def recover_orphaned_generation_tasks_task() -> int:
    """Expose lifecycle cleanup for scheduled/operational invocations."""
    return recover_orphaned_generation_tasks()


@worker_ready.connect
def recover_generation_tasks_on_worker_ready(sender=None, **kwargs):
    """Clean up rows left by a previous web or Celery process."""
    try:
        recovered = recover_orphaned_generation_tasks()
        if recovered:
            logger.warning('生成任务 Worker 启动收口中断任务: %s 条', recovered)
    except Exception:
        logger.exception('生成任务 Worker 启动收口失败')
