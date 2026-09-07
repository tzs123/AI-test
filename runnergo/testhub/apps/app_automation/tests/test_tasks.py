# -*- coding: utf-8 -*-
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.app_automation.models import (
    AppDevice,
    AppProject,
    AppScheduledTask,
    AppTestCase,
    AppTestExecution,
)
from apps.app_automation.tasks import (
    APP_EXECUTION_INTERRUPTED_MESSAGE,
    recover_interrupted_app_executions,
    send_scheduled_task_notification,
)


User = get_user_model()


class AppAutomationTaskRecoveryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='task_recovery_user', password='secret')
        self.project = AppProject.objects.create(name='APP 自动化', owner=self.user)
        self.test_case = AppTestCase.objects.create(
            project=self.project,
            name='中断恢复用例',
            ui_flow={'steps': []},
            created_by=self.user,
        )

    @patch('apps.app_automation.tasks.send_execution_update')
    @patch('apps.core.data_assets.release_assets_for_execution')
    def test_recover_interrupted_running_execution_releases_device(
        self,
        release_assets_mock,
        send_update_mock,
    ):
        device = AppDevice.objects.create(
            device_id='ios-recovery-1',
            name='iPhone Recovery',
            platform='ios',
            status='available',
        )
        device.lock(self.user)
        execution = AppTestExecution.objects.create(
            test_case=self.test_case,
            device=device,
            user=self.user,
            status='running',
            task_id='lost-celery-task',
            progress=41,
            started_at=timezone.now() - timezone.timedelta(minutes=10),
        )

        recovered = recover_interrupted_app_executions(active_task_ids=set())

        self.assertEqual(recovered, 1)
        execution.refresh_from_db()
        device.refresh_from_db()
        self.assertEqual(execution.status, 'error')
        self.assertIsNone(execution.result)
        self.assertEqual(execution.error_message, APP_EXECUTION_INTERRUPTED_MESSAGE)
        self.assertIsNotNone(execution.finished_at)
        self.assertEqual(device.status, 'available')
        self.assertIsNone(device.locked_by)
        release_assets_mock.assert_called_once_with('app_automation', execution.id)
        send_update_mock.assert_called_once()

    def test_recover_interrupted_running_execution_keeps_known_active_task(self):
        device = AppDevice.objects.create(
            device_id='ios-recovery-2',
            name='iPhone Active',
            platform='ios',
            status='available',
        )
        device.lock(self.user)
        execution = AppTestExecution.objects.create(
            test_case=self.test_case,
            device=device,
            user=self.user,
            status='running',
            task_id='active-celery-task',
            progress=20,
            started_at=timezone.now(),
        )

        recovered = recover_interrupted_app_executions(active_task_ids={'active-celery-task'})

        self.assertEqual(recovered, 0)
        execution.refresh_from_db()
        device.refresh_from_db()
        self.assertEqual(execution.status, 'running')
        self.assertEqual(device.status, 'locked')


class AppAutomationAutomaticNotificationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='automatic_notification_user',
            password='secret',
        )

    @patch('apps.app_automation.tasks._send_app_webhook_notification')
    @patch('apps.app_automation.tasks._get_report_links')
    def test_scheduled_execution_uses_unified_integrations_without_legacy_switches(
        self,
        report_links_mock,
        send_webhook_mock,
    ):
        task = AppScheduledTask.objects.create(
            name='自动通知任务',
            task_type='TEST_CASE',
            trigger_type='CRON',
            cron_expression='0 0 * * *',
            created_by=self.user,
            notify_on_success=False,
            notify_on_failure=False,
            notification_type='',
            notify_emails=[],
            last_run_time=timezone.now(),
            last_result={
                'message': '登录用例 - passed',
                'execution_id': 123,
            },
        )
        report_links_mock.return_value = [{
            'name': '登录用例',
            'url': 'https://runnergo.test/report/123',
        }]
        send_webhook_mock.return_value = [{'success': True}]

        result = send_scheduled_task_notification(task.id, success=True)

        self.assertEqual(result, [{'success': True}])
        send_webhook_mock.assert_called_once()
        self.assertEqual(send_webhook_mock.call_args.args[0].id, task.id)
        self.assertEqual(send_webhook_mock.call_args.args[2], '成功')
