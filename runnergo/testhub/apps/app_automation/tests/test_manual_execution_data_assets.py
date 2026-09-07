from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.app_automation.models import AppDevice, AppProject, AppTestCase, AppTestExecution
from apps.core.models import TestDataAsset, TestDataAssetLease, TestDataAssetRequirement


class ManualExecutionDataAssetTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='manual-execution-user',
            password='secret',
        )
        self.client.force_authenticate(self.user)
        self.project = AppProject.objects.create(name='APP execution', owner=self.user)
        self.device = AppDevice.objects.create(
            device_id='manual-execution-device',
            name='Manual execution device',
            status='available',
        )
        self.test_case = AppTestCase.objects.create(
            project=self.project,
            name='Manual execution case',
            ui_flow={'steps': []},
            created_by=self.user,
        )

    @patch('apps.app_automation.tasks.execute_app_test_task.delay')
    def test_manual_execution_skips_stale_agent_binding_but_keeps_regular_binding(self, delay):
        delay.return_value = SimpleNamespace(id='manual-execution-task')
        stale_alias = f'agent_app_64_{self.test_case.id}'
        stale_asset = TestDataAsset.objects.create(
            asset_type='CUSTOM',
            name='Consumed agent data',
            status=TestDataAsset.STATUS_USED,
            payload={'username': 'agent-user'},
            created_by=self.user,
        )
        TestDataAssetRequirement.objects.create(
            target_type='app_automation',
            target_case_id=self.test_case.id,
            alias=stale_alias,
            asset_type='CUSTOM',
            source_asset=stale_asset,
            release_policy='mark_used',
            created_by=self.user,
        )
        regular_asset = TestDataAsset.objects.create(
            asset_type='USER',
            name='Reusable manual data',
            payload={'phone': '13800138000'},
            created_by=self.user,
        )
        TestDataAssetRequirement.objects.create(
            target_type='app_automation',
            target_case_id=self.test_case.id,
            alias='loginData',
            asset_type='USER',
            source_asset=regular_asset,
            release_policy='release',
            created_by=self.user,
        )

        response = self.client.post(
            f'/api/app-automation/test-cases/{self.test_case.id}/execute/',
            {'device_id': self.device.device_id},
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.data)
        execution = AppTestExecution.objects.get(id=response.data['execution']['id'])
        self.assertEqual(
            execution.runtime_context['dataAssets']['loginData']['phone'],
            '13800138000',
        )
        self.assertNotIn(stale_alias, execution.runtime_context['dataAssets'])
        self.assertEqual(
            list(
                TestDataAssetLease.objects.filter(execution_id=str(execution.id))
                .values_list('alias', flat=True)
            ),
            ['loginData'],
        )
