# -*- coding: utf-8 -*-
"""APP Agent 四期的多设备兼容性矩阵编排与汇总。"""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, List

from celery import current_app
from django.db import transaction
from django.utils import timezone

from .agent_service import record_agent_event, validate_agent_execution
from .models import (
    AppAgentMatrixCell,
    AppAgentMatrixRun,
    AppAgentRuntimeSession,
    AppAgentTask,
    AppDevice,
)
from .runtime_service import (
    approve_runtime_step,
    validate_runtime_candidate_set,
)

MATRIX_ACTIVE_CELL_STATUSES = {'pending', 'queued', 'running', 'awaiting_approval'}
MATRIX_TERMINAL_CELL_STATUSES = {'completed', 'failed', 'budget_exhausted', 'stopped'}


def normalize_matrix_concurrency(value: Any) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 2
    return max(1, min(value, 10))


def matrix_devices(base_task: AppAgentTask, device_ids: Iterable[Any]) -> List[AppDevice]:
    normalized = []
    for raw_id in device_ids or []:
        try:
            device_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if device_id not in normalized:
            normalized.append(device_id)
    if not normalized:
        raise RuntimeError('请至少选择一台兼容性测试设备')
    if len(normalized) > 20:
        raise RuntimeError('单次兼容性矩阵最多支持 20 台设备')
    device_map = {
        item.id: item for item in AppDevice.objects.filter(id__in=normalized)
    }
    missing = [item for item in normalized if item not in device_map]
    if missing:
        raise RuntimeError(f'设备不存在或已被删除：{missing}')
    devices = [device_map[item] for item in normalized]
    blocked = [
        device.name or device.device_id
        for device in devices
        if device.status == 'offline'
        or (device.status == 'locked' and device.locked_by_id != base_task.user_id)
    ]
    if blocked:
        raise RuntimeError(f'以下设备当前不可执行：{", ".join(blocked)}')
    return devices


def _session_coverage(candidate_count: int) -> Dict[str, Any]:
    return {
        'candidate_total': candidate_count,
        'candidate_completed': 0,
        'pages': [],
        'edges': [],
        'page_count': 0,
        'edge_count': 0,
    }


@transaction.atomic
def create_matrix_run(
    base_task: AppAgentTask,
    devices: List[AppDevice],
    *,
    budgets: Dict[str, Any],
    max_concurrency: int,
    name: str = '',
) -> AppAgentMatrixRun:
    """为每台设备创建隔离子任务和自治会话，失败设备也保留矩阵单元证据。"""
    matrix = AppAgentMatrixRun.objects.create(
        base_task=base_task,
        name=str(name or f'{base_task.goal[:80]} · 兼容性矩阵')[:200],
        status='pending',
        max_concurrency=normalize_matrix_concurrency(max_concurrency),
        budgets=copy.deepcopy(budgets),
        device_ids=[device.id for device in devices],
        started_at=timezone.now(),
    )
    for device in devices:
        version = device.ios_version if device.platform == 'ios' else device.android_version
        child_task = AppAgentTask.objects.create(
            user=base_task.user,
            project=base_task.project,
            device=device,
            app_package=base_task.app_package,
            parent_task=base_task,
            execution_mode='matrix_child',
            goal=base_task.goal,
            status='ready',
            progress=0,
            auto_execute=False,
            plan=copy.deepcopy(base_task.plan or {}),
        )
        cell = AppAgentMatrixCell.objects.create(
            matrix_run=matrix,
            device=device,
            task=child_task,
            platform=device.platform or '',
            platform_version=version or '',
        )
        try:
            validate_agent_execution(child_task)
            candidates = validate_runtime_candidate_set(child_task)
            session = AppAgentRuntimeSession.objects.create(
                task=child_task,
                max_steps=budgets['max_steps'],
                max_duration_seconds=budgets['max_duration_seconds'],
                max_model_tokens=budgets['max_model_tokens'],
                repeat_state_limit=budgets['repeat_state_limit'],
                telemetry_enabled=bool(budgets.get('telemetry_enabled', True)),
                telemetry_interval_steps=max(1, min(int(budgets.get('telemetry_interval_steps') or 2), 10)),
                coverage=_session_coverage(len(candidates)),
            )
            cell.runtime_session = session
            cell.save(update_fields=['runtime_session', 'updated_at'])
        except Exception as exc:
            message = str(exc)
            child_task.status = 'failed'
            child_task.error_message = message
            child_task.finished_at = timezone.now()
            child_task.save(update_fields=['status', 'error_message', 'finished_at', 'updated_at'])
            cell.status = 'failed'
            cell.compatibility = 'asset_gap'
            cell.error_message = message
            cell.finished_at = timezone.now()
            cell.save(update_fields=[
                'status', 'compatibility', 'error_message', 'finished_at', 'updated_at'
            ])

    base_task.status = 'matrix_executing'
    base_task.progress = 5
    base_task.error_message = ''
    base_task.finished_at = None
    base_task.save(update_fields=[
        'status', 'progress', 'error_message', 'finished_at', 'updated_at'
    ])
    record_agent_event(
        base_task,
        'matrix_created',
        f'兼容性矩阵 #{matrix.id} 已创建，共 {len(devices)} 台设备',
        payload={
            'matrix_id': matrix.id,
            'device_ids': matrix.device_ids,
            'max_concurrency': matrix.max_concurrency,
            'budgets': matrix.budgets,
        },
    )
    transaction.on_commit(lambda: dispatch_matrix_cells(matrix.id))
    return matrix


def dispatch_matrix_cells(matrix_id: int) -> None:
    """按矩阵并发上限提交等待中的设备单元。"""
    from .tasks import execute_app_agent_runtime_task

    matrix = AppAgentMatrixRun.objects.select_related('base_task').get(id=matrix_id)
    if matrix.status == 'stopped':
        return
    active_count = matrix.cells.filter(status__in=['queued', 'running']).count()
    slots = max(0, matrix.max_concurrency - active_count)
    if slots <= 0:
        return
    cells = list(
        matrix.cells.filter(status='pending', runtime_session__isnull=False)
        .select_related('runtime_session', 'task', 'device')
        .order_by('id')[:slots]
    )
    for cell in cells:
        session = cell.runtime_session
        try:
            celery_task = execute_app_agent_runtime_task.delay(session.id)
            session.celery_task_id = celery_task.id
            session.save(update_fields=['celery_task_id', 'updated_at'])
            cell.status = 'queued'
            cell.save(update_fields=['status', 'updated_at'])
            cell.task.status = 'runtime_executing'
            cell.task.task_id = celery_task.id
            cell.task.save(update_fields=['status', 'task_id', 'updated_at'])
            record_agent_event(
                matrix.base_task,
                'matrix_dispatch',
                f'设备 {cell.device.name or cell.device.device_id} 已进入自治执行队列',
                payload={'matrix_id': matrix.id, 'cell_id': cell.id, 'session_id': session.id},
            )
        except Exception as exc:
            cell.status = 'failed'
            cell.compatibility = 'dispatch_failed'
            cell.error_message = str(exc)
            cell.finished_at = timezone.now()
            cell.save(update_fields=[
                'status', 'compatibility', 'error_message', 'finished_at', 'updated_at'
            ])
    refresh_matrix_summary(matrix.id, dispatch=False)


def mark_matrix_cell_running(session: AppAgentRuntimeSession) -> None:
    try:
        cell = session.matrix_cell
    except AppAgentMatrixCell.DoesNotExist:
        return
    if cell.status in ('pending', 'queued'):
        cell.status = 'running'
        cell.started_at = cell.started_at or timezone.now()
        cell.save(update_fields=['status', 'started_at', 'updated_at'])
    refresh_matrix_summary(cell.matrix_run_id, dispatch=False)


def _compatibility_for_session(session: AppAgentRuntimeSession) -> str:
    risks = (session.telemetry_summary or {}).get('risks') or []
    if session.status == 'completed':
        return 'compatible_with_risk' if risks else 'compatible'
    if session.status == 'failed':
        return 'incompatible'
    if session.status == 'budget_exhausted':
        return 'inconclusive'
    if session.status == 'stopped':
        return 'not_run'
    return 'pending'


def sync_matrix_cell(session: AppAgentRuntimeSession) -> None:
    """把自治会话状态、遥测和缺陷同步回设备矩阵，并继续调度等待设备。"""
    try:
        cell = AppAgentMatrixCell.objects.select_related(
            'matrix_run', 'matrix_run__base_task', 'task', 'device'
        ).get(runtime_session=session)
    except AppAgentMatrixCell.DoesNotExist:
        return
    status_map = {
        'pending': 'pending',
        'running': 'running',
        'awaiting_approval': 'awaiting_approval',
        'completed': 'completed',
        'failed': 'failed',
        'budget_exhausted': 'budget_exhausted',
        'stopped': 'stopped',
    }
    cell.status = status_map.get(session.status, cell.status)
    cell.compatibility = _compatibility_for_session(session)
    cell.result_summary = copy.deepcopy(session.result_summary or {})
    cell.telemetry_summary = copy.deepcopy(session.telemetry_summary or {})
    cell.error_message = session.error_message or ''
    cell.started_at = session.started_at or cell.started_at
    if cell.status in MATRIX_TERMINAL_CELL_STATUSES:
        cell.finished_at = session.finished_at or timezone.now()
    cell.save(update_fields=[
        'status', 'compatibility', 'result_summary', 'telemetry_summary',
        'error_message', 'started_at', 'finished_at', 'updated_at',
    ])
    refresh_matrix_summary(cell.matrix_run_id, dispatch=True)


def _cell_item(cell: AppAgentMatrixCell) -> Dict[str, Any]:
    telemetry = cell.telemetry_summary or {}
    return {
        'cell_id': cell.id,
        'device_id': cell.device_id,
        'device': cell.device.name or cell.device.device_id,
        'platform': cell.platform,
        'version': cell.platform_version,
        'status': cell.status,
        'compatibility': cell.compatibility,
        'error': cell.error_message,
        'telemetry': {
            key: telemetry.get(key)
            for key in (
                'samples', 'max_cpu_percent', 'max_memory_mb', 'min_fps_estimate',
                'max_temperature_c', 'min_battery_percent', 'crash_count', 'risks',
            )
            if telemetry.get(key) is not None
        },
    }


def refresh_matrix_summary(matrix_id: int, *, dispatch: bool) -> AppAgentMatrixRun:
    matrix = AppAgentMatrixRun.objects.select_related('base_task').prefetch_related(
        'cells__device', 'cells__task'
    ).get(id=matrix_id)
    cells = list(matrix.cells.all())
    counts = {status: 0 for status in (
        'pending', 'queued', 'running', 'awaiting_approval', 'completed',
        'failed', 'budget_exhausted', 'stopped',
    )}
    compatibility_counts: Dict[str, int] = {}
    for cell in cells:
        counts[cell.status] = counts.get(cell.status, 0) + 1
        compatibility_counts[cell.compatibility] = compatibility_counts.get(cell.compatibility, 0) + 1
    compatible = compatibility_counts.get('compatible', 0) + compatibility_counts.get('compatible_with_risk', 0)
    total = len(cells)
    terminal = sum(counts.get(item, 0) for item in MATRIX_TERMINAL_CELL_STATUSES)
    matrix.summary = {
        'total': total,
        'terminal': terminal,
        'counts': counts,
        'compatibility_counts': compatibility_counts,
        'compatible': compatible,
        'incompatible': compatibility_counts.get('incompatible', 0) + compatibility_counts.get('asset_gap', 0),
        'compatibility_rate': round(compatible / total * 100, 2) if total else 0,
        'devices': [_cell_item(cell) for cell in cells],
    }
    if matrix.status == 'stopped':
        pass
    elif counts.get('awaiting_approval'):
        matrix.status = 'awaiting_approval'
    elif terminal == total and total:
        matrix.status = 'completed'
        matrix.finished_at = timezone.now()
    else:
        matrix.status = 'running'
    matrix.save(update_fields=['status', 'summary', 'finished_at', 'updated_at'])

    base_task = matrix.base_task
    base_task.progress = min(100, max(5, round(terminal / max(total, 1) * 100)))
    if matrix.status == 'awaiting_approval':
        base_task.status = 'matrix_approval'
    elif matrix.status == 'running':
        base_task.status = 'matrix_executing'
    elif matrix.status == 'stopped':
        base_task.status = 'stopped'
    else:
        base_task.status = 'completed'
        base_task.finished_at = matrix.finished_at or timezone.now()
    base_task.result_summary = {
        **(base_task.result_summary or {}),
        'matrix': matrix.summary,
    }
    update_fields = ['status', 'progress', 'result_summary', 'finished_at', 'updated_at']
    if matrix.status == 'completed':
        failures = [cell for cell in cells if cell.compatibility not in ('compatible', 'compatible_with_risk')]
        base_task.failure_analysis = {
            'summary': f'兼容 {compatible}/{total} 台设备',
            'items': [_cell_item(cell) for cell in failures],
        }
        defect_drafts = []
        for cell in failures:
            defect_drafts.extend(cell.task.defect_draft or [])
        base_task.defect_draft = defect_drafts
        update_fields.extend(['failure_analysis', 'defect_draft'])
    base_task.save(update_fields=update_fields)

    if matrix.status == 'completed':
        record_agent_event(
            base_task,
            'matrix_completed',
            f'兼容性矩阵完成：兼容 {compatible}/{total} 台设备',
            level='warning' if compatible < total else 'success',
            payload={'matrix_id': matrix.id, **matrix.summary},
        )
    elif matrix.status == 'awaiting_approval':
        record_agent_event(
            base_task,
            'matrix_approval',
            '兼容性矩阵中存在等待人工审批的敏感动作',
            level='warning',
            payload={'matrix_id': matrix.id, 'awaiting': counts.get('awaiting_approval')},
        )
    if dispatch and matrix.status in ('running', 'awaiting_approval'):
        dispatch_matrix_cells(matrix.id)
    return matrix


def approve_matrix_cell(
    matrix: AppAgentMatrixRun,
    cell_id: int,
    user,
    *,
    approved: bool,
    note: str,
) -> AppAgentMatrixCell:
    cell = matrix.cells.select_related('runtime_session', 'task').get(id=cell_id)
    session = cell.runtime_session
    if not session or session.status != 'awaiting_approval':
        raise RuntimeError('该设备当前没有等待审批的敏感动作')
    runtime_step = approve_runtime_step(
        session,
        user,
        approved=approved,
        note=note,
    )
    if not runtime_step:
        raise RuntimeError('审批动作已被处理')
    if approved:
        cell.status = 'pending'
        cell.save(update_fields=['status', 'updated_at'])
        dispatch_matrix_cells(matrix.id)
    else:
        sync_matrix_cell(session)
    return AppAgentMatrixCell.objects.select_related(
        'device', 'task', 'runtime_session'
    ).get(id=cell.id)


def stop_matrix_run(matrix: AppAgentMatrixRun) -> None:
    matrix.status = 'stopped'
    matrix.finished_at = timezone.now()
    matrix.save(update_fields=['status', 'finished_at', 'updated_at'])
    for cell in matrix.cells.select_related('runtime_session', 'task'):
        if cell.status in MATRIX_TERMINAL_CELL_STATUSES:
            continue
        session = cell.runtime_session
        if session:
            session.stop_requested = True
            session.status = 'stopped'
            session.finished_at = timezone.now()
            session.save(update_fields=[
                'stop_requested', 'status', 'finished_at', 'updated_at'
            ])
            if session.celery_task_id:
                current_app.control.revoke(session.celery_task_id, terminate=False)
        cell.status = 'stopped'
        cell.compatibility = 'not_run'
        cell.finished_at = timezone.now()
        cell.save(update_fields=['status', 'compatibility', 'finished_at', 'updated_at'])
        cell.task.status = 'stopped'
        cell.task.finished_at = timezone.now()
        cell.task.save(update_fields=['status', 'finished_at', 'updated_at'])
    refresh_matrix_summary(matrix.id, dispatch=False)
    record_agent_event(
        matrix.base_task,
        'matrix_stopped',
        f'兼容性矩阵 #{matrix.id} 已停止',
        level='warning',
        payload={'matrix_id': matrix.id},
    )
