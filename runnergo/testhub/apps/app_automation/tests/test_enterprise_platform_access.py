from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.app_automation.models import (
    AppDevice,
    AppNotificationLog,
    AppProject,
    AppTestCase,
    AppTestExecution,
)
from apps.app_automation.tasks import (
    _get_enabled_app_webhook_bots,
    _send_app_webhook_message,
    get_app_notification_targets,
    select_app_webhook_bots,
)


@override_settings(SECURITY_CREDENTIALS_KEY='app-platform-security-test-key')
class AppPlatformEnterpriseAccessTest(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            username='app-admin', password='pass', is_staff=True,
        )
        self.member = User.objects.create_user(
            username='app-member', password='pass',
        )
        self.client = APIClient()

    def test_global_runtime_config_and_notification_logs_require_login_only(self):
        self.client.force_authenticate(self.member)
        self.assertEqual(
            self.client.get('/api/app-automation/config/current/').status_code,
            200,
        )
        self.assertEqual(
            self.client.get('/api/app-automation/notification-logs/').status_code,
            200,
        )

        self.client.force_authenticate(self.admin)
        self.assertEqual(
            self.client.get('/api/app-automation/config/current/').status_code,
            200,
        )

    def test_device_inventory_mutation_requires_login_and_control_requires_lease(self):
        device = AppDevice.objects.create(
            device_id='enterprise-device-1',
            name='enterprise-device',
            platform='android',
            status='available',
        )
        self.client.force_authenticate(self.member)
        update_response = self.client.patch(
            f'/api/app-automation/devices/{device.id}/',
            {'name': 'member-updated-device'},
            format='json',
        )
        self.assertEqual(update_response.status_code, 200)
        response = self.client.post(
            f'/api/app-automation/devices/{device.id}/launch-app/',
            {'package_name': 'com.example.app'},
            format='json',
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn('请先锁定设备', str(response.data))

    @patch('requests.get')
    def test_notification_bots_come_from_runnergo_third_party_integrations(self, get_mock):
        response = Mock(status_code=200, content=b'{}')
        response.json.return_value = {
            'bots': [{
                'type': 'feishu',
                'name': 'global-quality-bot',
                'webhook_url': 'https://open.feishu.cn/open-apis/bot/v2/hook/global-token',
                'enabled': True,
            }],
        }
        response.raise_for_status.return_value = None
        get_mock.return_value = response

        bots = _get_enabled_app_webhook_bots()

        self.assertEqual(bots[0]['name'], 'global-quality-bot')
        self.assertTrue(
            get_mock.call_args.args[0].endswith(
                '/notice/internal/enabled_webhook_bots'
            )
        )
        self.assertEqual(
            get_mock.call_args.kwargs['headers']['X-Agent-Token'],
            'runnergo-local-agent-token',
        )
        self.assertFalse(get_mock.call_args.kwargs['allow_redirects'])

    def test_notification_targets_are_safe_and_selected_by_id(self):
        bots = [{
            'id': 'notice-a',
            'type': 'feishu',
            'name': '质量群',
            'webhook_url': 'https://example.test/private-webhook',
            'secret': 'private-secret',
            'enabled': True,
            'created_at': '2026-08-13 12:00:00',
        }, {
            'id': 'notice-b',
            'type': 'wechat',
            'name': '研发群',
            'webhook_url': 'https://example.test/private-webhook-b',
            'enabled': True,
        }]

        targets = get_app_notification_targets(bots)
        selected = select_app_webhook_bots(['notice-b'], bots)

        self.assertNotIn('webhook_url', targets[0])
        self.assertNotIn('secret', targets[0])
        self.assertEqual(targets[0]['id'], 'notice-a')
        self.assertEqual([item['id'] for item in selected], ['notice-b'])

    @patch('apps.app_automation.views.execution_views.send_manual_execution_completion_notification')
    @patch('apps.app_automation.views.execution_views.select_app_webhook_bots')
    def test_batch_report_notification_uses_selected_bots_and_access_scope(
        self,
        select_bots_mock,
        send_mock,
    ):
        project = AppProject.objects.create(name='member-project', owner=self.member)
        test_case = AppTestCase.objects.create(project=project, name='login')
        allowed_execution = AppTestExecution.objects.create(
            test_case=test_case,
            user=self.member,
            status='completed',
            result='passed',
            report_path='/tmp/member-report',
        )
        hidden_project = AppProject.objects.create(name='hidden-project', owner=self.admin)
        hidden_case = AppTestCase.objects.create(project=hidden_project, name='hidden')
        hidden_execution = AppTestExecution.objects.create(
            test_case=hidden_case,
            user=self.admin,
            status='completed',
            result='passed',
            report_path='/tmp/hidden-report',
        )
        selected_bot = {
            'id': 'notice-selected',
            'type': 'feishu',
            'name': '质量群',
            'webhook_url': 'https://example.test/selected',
        }
        select_bots_mock.return_value = [selected_bot]
        send_mock.return_value = [{
            'id': 'notice-selected',
            'type': 'feishu',
            'name': '质量群',
            'success': True,
        }]

        self.client.force_authenticate(self.member)
        response = self.client.post(
            '/api/app-automation/executions/batch-notify/',
            {
                'ids': [allowed_execution.id, hidden_execution.id],
                'notification_ids': ['notice-selected'],
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['sent'], 1)
        self.assertEqual(response.data['failed'], 1)
        self.assertEqual(send_mock.call_count, 1)
        self.assertEqual(send_mock.call_args.args[0].id, allowed_execution.id)
        self.assertEqual(send_mock.call_args.kwargs['bots'], [selected_bot])

    @patch('apps.core.outbound.validate_outbound_http_url')
    @patch('apps.app_automation.tasks._get_enabled_app_webhook_bots')
    @patch('requests.post')
    def test_webhook_request_disables_redirects_and_logs_no_credentials(
        self,
        post_mock,
        bots_mock,
        validate_mock,
    ):
        webhook_url = 'https://open.feishu.cn/open-apis/bot/v2/hook/plain-secret-token'
        validate_mock.return_value = webhook_url
        post_mock.return_value = Mock(status_code=200, text='provider-secret-response')
        bots_mock.return_value = [{
                'type': 'feishu',
                'name': 'app-quality-bot',
                'webhook_url': webhook_url,
                'enabled': True,
        }]

        _send_app_webhook_message(
            task_name='security regression',
            title='security regression',
            detail_content='completed',
            status_text='成功',
        )

        self.assertFalse(post_mock.call_args.kwargs['allow_redirects'])
        log = AppNotificationLog.objects.get()
        serialized = str({
            'recipient_info': log.recipient_info,
            'webhook_bot_info': log.webhook_bot_info,
            'response_info': log.response_info,
            'error_message': log.error_message,
        })
        self.assertNotIn('plain-secret-token', serialized)
        self.assertNotIn('provider-secret-response', serialized)
        self.assertEqual(log.response_info, {'status_code': 200})

    @patch('apps.core.outbound.validate_outbound_http_url')
    @patch('apps.app_automation.tasks._get_enabled_app_webhook_bots')
    @patch('requests.post')
    def test_feishu_report_button_uses_allure_report_label(
        self,
        post_mock,
        bots_mock,
        validate_mock,
    ):
        webhook_url = 'https://open.feishu.cn/open-apis/bot/v2/hook/plain-token'
        validate_mock.return_value = webhook_url
        post_mock.return_value = Mock(status_code=200, text='ok')
        bots_mock.return_value = [{
            'type': 'feishu',
            'name': 'app-quality-bot',
            'webhook_url': webhook_url,
            'enabled': True,
        }]

        _send_app_webhook_message(
            task_name='APP自动化报告',
            title='APP自动化执行失败',
            detail_content='用例名称: 录制脚本\n\nAllure 报告:\n- [录制脚本](https://runnergo.test/report/123)',
            status_text='失败',
            report_links=[{
                'name': '录制脚本',
                'url': 'https://runnergo.test/report/123',
            }],
        )

        message_data = post_mock.call_args.kwargs['json']
        actions = message_data['card']['elements'][1]['actions']
        self.assertEqual(actions[0]['text']['content'], '查看allure报告')
