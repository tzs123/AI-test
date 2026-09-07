from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone

from apps.agent.e2e_tasks import (
    _execution_steps,
    _resolve_data_value,
    _start_execution,
    execute_e2e_task,
    recover_orphaned_queued_e2e_tasks,
)
from apps.agent.e2e_service import e2e_plan_requires_refresh
from apps.agent.models import E2EExecutionLog, E2ETask


pytestmark = pytest.mark.django_db


def ready_task():
    user = get_user_model().objects.create_user(username='e2e-worker-user', password='x')
    return E2ETask.objects.create(
        created_by=user,
        task_name='Worker E2E',
        description='测试登录流程',
        scenario={'objective': '测试登录'},
        steps=[{'id': '1', 'name': '打开页面', 'action': 'navigate', 'url': 'https://example.test'}],
        expected_result='登录流程符合预期',
        executor_type='web',
        target_url='https://example.test',
        status=E2ETask.STATUS_READY,
        trace_id='worker-e2e-trace',
    )


def test_mobile_execution_backfills_target_app_for_legacy_plan():
    task = ready_task()
    task.executor_type = E2ETask.EXECUTOR_IOS
    task.target_url = ''
    task.mobile_config = {
        'server_url': 'http://127.0.0.1:4723',
        'udid': 'ios-device-1',
        'bundle_id': 'com.apple.mobilesafari',
    }
    task.steps = [{
        'id': '1',
        'name': '启动 App',
        'action': 'launch_app',
        'value': None,
    }]

    steps = _execution_steps(task)

    assert steps[0]['value'] == 'com.apple.mobilesafari'
    assert task.steps[0]['value'] is None


def test_mobile_business_task_refreshes_legacy_launch_and_screenshot_plan():
    task = ready_task()
    task.executor_type = E2ETask.EXECUTOR_IOS
    task.description = '1、测试【登录】 2、测试【首页】'
    task.steps = [
        {'id': '1', 'name': '启动 App', 'action': 'launch_app'},
        {'id': '2', 'name': '采集启动页', 'action': 'screenshot'},
    ]

    assert e2e_plan_requires_refresh(task) is True


def test_execution_steps_resolve_generated_test_data_without_mutating_plan():
    task = ready_task()
    task.test_data = {'normal': [{'username': 'resolved-user'}]}
    task.steps = [{
        'id': '1',
        'name': '输入用户',
        'action': 'fill',
        'target': {'label': 'Username'},
        'value': '${test_data.normal.0.username}',
    }]

    steps = _execution_steps(task)

    assert steps[0]['value'] == 'resolved-user'
    assert task.steps[0]['value'] == '${test_data.normal.0.username}'


def test_execution_data_reference_fails_loudly_when_missing():
    with pytest.raises(RuntimeError, match='test_data.normal.0.code'):
        _resolve_data_value('${test_data.normal.0.code}', {'normal': [{}]})


def test_worker_lost_redelivery_restarts_same_celery_task_id():
    task = ready_task()
    task.status = E2ETask.STATUS_RUNNING
    task.execution_attempt = 1
    task.celery_task_id = 'celery-redelivery-1'
    task.save(update_fields=['status', 'execution_attempt', 'celery_task_id'])

    attempt = _start_execution(task.id, celery_task_id='celery-redelivery-1')

    task.refresh_from_db()
    assert attempt == 2
    assert task.status == E2ETask.STATUS_RUNNING
    assert task.execution_attempt == 2


@patch('apps.agent.e2e_tasks.current_app.control.revoke')
@patch('apps.agent.e2e_tasks.execute_e2e_task.delay')
def test_worker_ready_requeues_stale_orphaned_task(delay_mock, revoke_mock):
    delay_mock.return_value = SimpleNamespace(id='celery-recovered-1')
    task = ready_task()
    task.status = E2ETask.STATUS_QUEUED
    task.celery_task_id = 'celery-lost-1'
    task.save(update_fields=['status', 'celery_task_id'])
    E2ETask.objects.filter(pk=task.pk).update(
        updated_time=timezone.now() - timezone.timedelta(minutes=5),
    )

    recovered = recover_orphaned_queued_e2e_tasks(stale_after_seconds=30)

    task.refresh_from_db()
    assert recovered == 1
    assert task.status == E2ETask.STATUS_QUEUED
    assert task.celery_task_id == 'celery-recovered-1'
    revoke_mock.assert_called_once_with('celery-lost-1', terminate=False)
    delay_mock.assert_called_once_with(task.id, headless=True, record_video=True)


@patch('apps.agent.e2e_tasks.BrowserAgent')
def test_successful_worker_run_persists_step_and_report(browser_agent, tmp_path):
    task = ready_task()
    task.analysis = {'title': '上一轮失败'}
    task.result = {'status': 'failed', 'plan_source': 'llm', 'planner': {'configuration_name': 'test-model'}}
    task.report_url = '/api/e2e/report/stale'
    task.save(update_fields=['analysis', 'result', 'report_url'])

    def execute(**kwargs):
        kwargs['on_step']({
            'sequence': 1,
            'name': '打开页面',
            'action': 'navigate',
            'status': 'passed',
            'actual': {'status_code': 200},
            'expected': None,
            'url': 'https://example.test',
            'console_logs': [],
            'network_logs': [],
            'page_errors': [],
            'duration_ms': 40,
        })
        return {
            'status': 'passed',
            'summary': {'total': 1, 'executed': 1, 'passed': 1, 'failed': 0, 'skipped': 0},
            'duration_ms': 40,
        }

    browser_agent.return_value.execute.side_effect = execute
    with override_settings(MEDIA_ROOT=tmp_path):
        result = execute_e2e_task.run(task.id, headless=True, record_video=False)

    task.refresh_from_db()
    assert result['status'] == 'passed'
    assert task.status == E2ETask.STATUS_PASSED
    assert task.result['plan_source'] == 'llm'
    assert task.result['planner']['configuration_name'] == 'test-model'
    assert task.analysis == {}
    assert task.report_url == f'/api/e2e/report/{task.id}'
    assert (tmp_path / 'e2e-reports' / f'e2e-task-{task.id}.html').is_file()
    assert E2EExecutionLog.objects.get(task=task, step=1).status == 'PASSED'


@pytest.mark.django_db(transaction=True)
@patch('apps.agent.e2e_tasks.BrowserAgent')
def test_worker_persists_playwright_callback_from_async_context(browser_agent, tmp_path):
    task = ready_task()

    def execute(**kwargs):
        async def emit_step():
            kwargs['on_step']({
                'sequence': 1,
                'name': '打开页面',
                'action': 'navigate',
                'status': 'passed',
                'actual': {'status_code': 200},
                'url': 'https://example.test',
                'duration_ms': 40,
            })

        asyncio.run(emit_step())
        return {
            'status': 'passed',
            'summary': {'total': 1, 'executed': 1, 'passed': 1, 'failed': 0, 'skipped': 0},
            'duration_ms': 40,
        }

    browser_agent.return_value.execute.side_effect = execute
    with override_settings(MEDIA_ROOT=tmp_path):
        result = execute_e2e_task.run(task.id, headless=True, record_video=False)

    task.refresh_from_db()
    assert result['status'] == 'passed'
    assert task.status == E2ETask.STATUS_PASSED
    assert E2EExecutionLog.objects.get(task=task, step=1).status == 'PASSED'


@patch('apps.agent.e2e_analysis.analyze_e2e_task')
@patch('apps.agent.e2e_tasks.BrowserAgent')
def test_worker_exception_still_generates_failure_report(browser_agent, analyze_task, tmp_path):
    task = ready_task()
    browser_agent.return_value.execute.side_effect = RuntimeError('browser unavailable')
    analyze_task.side_effect = lambda task_id: (E2ETask.objects.get(pk=task_id), None)

    with override_settings(MEDIA_ROOT=tmp_path):
        with pytest.raises(RuntimeError, match='browser unavailable'):
            execute_e2e_task.run(task.id, headless=True, record_video=False)

    task.refresh_from_db()
    assert task.status == E2ETask.STATUS_FAILED
    assert task.error_message == 'browser unavailable'
    assert task.report_url == f'/api/e2e/report/{task.id}'
    assert (tmp_path / 'e2e-reports' / f'e2e-task-{task.id}.html').is_file()
    analyze_task.assert_called_once_with(task.id)
