from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.executions.models import TestPlan, TestRunCase
from apps.projects.models import Project
from apps.test_assets.models import TestAssetRequirement
from apps.testcases.models import TestCase


class CopilotTaskApiTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='copilot_user', password='pass123456')
        self.project = Project.objects.create(name='支付项目', owner=self.user)
        self.client.force_authenticate(self.user)

    def test_copilot_creates_plan_assets_and_cases(self):
        response = self.client.post('/api/assistant/copilot/plan/', {
            'objective': '帮我测试微信支付功能',
            'project_id': self.project.id,
        }, format='json')

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], 'completed')
        self.assertIn('测试计划', response.data['summary'])

        requirement_id = response.data['created_assets']['requirement_id']
        testcase_ids = response.data['created_assets']['testcase_ids']
        test_plan_id = response.data['created_assets']['test_plan_id']
        test_run_id = response.data['created_assets']['test_run_id']

        requirement = TestAssetRequirement.objects.get(id=requirement_id)
        self.assertEqual(requirement.links.filter(asset_type='api').count(), 5)
        self.assertEqual(requirement.links.filter(asset_type='page').count(), 4)
        self.assertEqual(requirement.links.filter(asset_type='automation_script').count(), 4)
        self.assertEqual(requirement.links.filter(asset_type='testcase').count(), 8)

        self.assertEqual(TestCase.objects.filter(id__in=testcase_ids).count(), 8)
        self.assertTrue(TestPlan.objects.filter(id=test_plan_id).exists())
        self.assertEqual(TestRunCase.objects.filter(test_run_id=test_run_id).count(), 8)

        execute_response = self.client.post(f'/api/assistant/copilot/{response.data["id"]}/execute/', {}, format='json')
        self.assertEqual(execute_response.status_code, 200)
        execution_check = execute_response.data['created_assets']['execution_check']
        self.assertEqual(execution_check['passed'], 6)
        self.assertEqual(execution_check['blocked'], 2)
        self.assertEqual(TestRunCase.objects.filter(test_run_id=test_run_id, status='passed').count(), 6)
        self.assertEqual(TestRunCase.objects.filter(test_run_id=test_run_id, status='blocked').count(), 2)
