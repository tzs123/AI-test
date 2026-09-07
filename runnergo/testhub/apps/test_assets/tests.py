from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.projects.models import Project
from apps.testcases.models import TestCase


class TestAssetCenterApiTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='asset_tester', password='pass123456')
        self.project = Project.objects.create(name='支付项目', owner=self.user)
        self.client.force_authenticate(self.user)

    def test_requirement_traceability_flow(self):
        requirement_response = self.client.post('/api/test-assets/requirements/', {
            'project_id': self.project.id,
            'requirement_key': 'REQ-PAY-001',
            'title': '支付订单功能',
            'module': '订单支付',
            'priority': 'high',
            'status': 'approved',
            'source': 'manual',
        }, format='json')
        self.assertEqual(requirement_response.status_code, 201)
        requirement_id = requirement_response.data['id']

        api_response = self.client.post('/api/test-assets/apis/', {
            'project_id': self.project.id,
            'name': '支付订单接口',
            'method': 'POST',
            'path': '/order/pay',
            'status': 'active',
        }, format='json')
        self.assertEqual(api_response.status_code, 201)

        testcase = TestCase.objects.create(
            project=self.project,
            title='TC_PAY_001 支付订单成功',
            expected_result='订单支付成功',
            author=self.user,
            priority='high',
            test_type='api',
            status='active',
        )

        script_response = self.client.post('/api/test-assets/automation-scripts/', {
            'project_id': self.project.id,
            'name': 'PayFlow.robot',
            'script_type': 'robot',
            'path': 'tests/PayFlow.robot',
            'status': 'active',
        }, format='json')
        self.assertEqual(script_response.status_code, 201)

        link_payloads = [
            {'asset_type': 'api', 'api': api_response.data['id']},
            {'asset_type': 'testcase', 'testcase': testcase.id},
            {'asset_type': 'automation_script', 'automation_script': script_response.data['id']},
        ]
        for payload in link_payloads:
            response = self.client.post('/api/test-assets/links/', {
                'requirement': requirement_id,
                **payload,
            }, format='json')
            self.assertEqual(response.status_code, 201)

        detail_response = self.client.get(f'/api/test-assets/requirements/{requirement_id}/')
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.data['coverage_status'], 'tested')
        self.assertEqual(detail_response.data['asset_counts']['api'], 1)
        self.assertEqual(detail_response.data['asset_counts']['testcase'], 1)
        self.assertEqual(detail_response.data['asset_counts']['automation_script'], 1)

        dashboard_response = self.client.get('/api/test-assets/dashboard/')
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertEqual(dashboard_response.data['total_requirements'], 1)
        self.assertEqual(dashboard_response.data['linked_requirements'], 1)
        self.assertEqual(dashboard_response.data['tested_requirements'], 1)
