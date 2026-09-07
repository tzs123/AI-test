from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.app_automation.models import (
    AppElement,
    AppProject,
    AppTestCase,
    AppTestExecution,
    AppTestSuite,
)


class AppAutomationProjectIsolationTest(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user_a = User.objects.create_user(username='app-a', password='pass')
        self.user_b = User.objects.create_user(username='app-b', password='pass')
        self.project_a = AppProject.objects.create(name='project-a', owner=self.user_a)
        self.project_b = AppProject.objects.create(name='project-b', owner=self.user_b)
        self.case_a = AppTestCase.objects.create(
            project=self.project_a,
            name='case-a',
            ui_flow={'steps': []},
            created_by=self.user_a,
        )
        self.case_b = AppTestCase.objects.create(
            project=self.project_b,
            name='case-b',
            ui_flow={'steps': []},
            created_by=self.user_b,
        )
        self.element_b = AppElement.objects.create(
            project=self.project_b,
            name='element-b',
            element_type='pos',
            config={'x': 1, 'y': 1},
            created_by=self.user_b,
        )
        self.suite_a = AppTestSuite.objects.create(
            project=self.project_a,
            name='suite-a',
            created_by=self.user_a,
        )
        self.execution_b = AppTestExecution.objects.create(
            test_case=self.case_b,
            user=self.user_b,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user_a)

    def test_foreign_project_assets_are_hidden_from_list_and_detail(self):
        response = self.client.get('/api/app-automation/test-cases/?page_size=100')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['id'] for item in response.data['results']], [self.case_a.id])

        self.assertEqual(
            self.client.get(f'/api/app-automation/test-cases/{self.case_b.id}/').status_code,
            404,
        )
        self.assertEqual(
            self.client.get(f'/api/app-automation/elements/{self.element_b.id}/').status_code,
            404,
        )
        self.assertEqual(
            self.client.get(f'/api/app-automation/executions/{self.execution_b.id}/').status_code,
            404,
        )

    def test_case_cannot_bind_foreign_project_or_foreign_element(self):
        response = self.client.post('/api/app-automation/test-cases/', {
            'project': self.project_b.id,
            'name': 'forbidden-project-case',
            'ui_flow': {'steps': []},
            'variables': [],
        }, format='json')
        self.assertEqual(response.status_code, 400)

        response = self.client.post('/api/app-automation/test-cases/', {
            'project': self.project_a.id,
            'name': 'foreign-element-case',
            'ui_flow': {
                'steps': [{'id': 'step-1', 'element_id': self.element_b.id}],
            },
            'variables': [],
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('ui_flow', response.data)

    def test_case_save_rejects_unrepairable_recorded_input_step(self):
        response = self.client.post('/api/app-automation/test-cases/', {
            'project': self.project_a.id,
            'name': 'bad-recorded-input',
            'ui_flow': {
                'steps': [{
                    'id': 'step-1',
                    'type': 'input',
                    'name': '单位地址',
                    'config': {
                        'selector_type': 'image',
                        'selector': '',
                        'value': '杭州体育中心',
                    },
                }],
            },
            'variables': [],
        }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('ui_flow', response.data)
        self.assertIn('缺少稳定定位', str(response.data['ui_flow']))

    def test_case_save_repairs_recorded_input_step_from_previous_text_field(self):
        response = self.client.post('/api/app-automation/test-cases/', {
            'project': self.project_a.id,
            'name': 'repaired-recorded-input',
            'ui_flow': {
                'steps': [
                    {
                        'id': 'step-1',
                        'type': 'click',
                        'name': 'iOS 录制点击 2',
                        'config': {
                            'selector_type': 'pos',
                            'selector': '244,382',
                            'fingerprint': {
                                'resource_id': '单位地址',
                                'text': '请填写包含门牌号的地址',
                                'content_desc': '单位地址',
                                'class_name': 'XCUIElementTypeTextField',
                            },
                        },
                    },
                    {
                        'id': 'step-2',
                        'type': 'input',
                        'name': '单位地址',
                        'config': {
                            'selector_type': 'image',
                            'selector': '',
                            'value': '杭州体育中心',
                        },
                    },
                ],
            },
            'variables': [],
        }, format='json')

        self.assertEqual(response.status_code, 201)
        test_case = AppTestCase.objects.get(id=response.data['id'])
        repaired_config = test_case.ui_flow['steps'][1]['config']
        self.assertEqual(repaired_config['selector_type'], 'pos')
        self.assertEqual(repaired_config['selector'], '244,382')
        self.assertTrue(repaired_config['_recording_input_target_repaired'])

    def test_suite_rejects_foreign_project_case(self):
        response = self.client.post(
            f'/api/app-automation/test-suites/{self.suite_a.id}/add_test_case/',
            {'test_case_id': self.case_b.id},
            format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_internal_report_endpoint_uses_app_project_not_unrelated_project_table(self):
        response = self.client.get(
            f'/api/app-automation/executions/{self.execution_b.id}/report/'
        )
        self.assertEqual(response.status_code, 404)


@override_settings(LOCAL_TRUSTED_MODE=True)
class AppAutomationLocalModeIsolationTest(TestCase):
    def setUp(self):
        User = get_user_model()
        owner = User.objects.create_user(username='app-local-owner', password='pass')
        self.project_a = AppProject.objects.create(name='local-project-a', owner=owner)
        self.project_b = AppProject.objects.create(name='local-project-b', owner=owner)
        self.case_a = AppTestCase.objects.create(
            project=self.project_a,
            name='local-case-a',
            ui_flow={'steps': []},
            created_by=owner,
        )
        self.case_b = AppTestCase.objects.create(
            project=self.project_b,
            name='local-case-b',
            ui_flow={'steps': []},
            created_by=owner,
        )
        self.client = APIClient()

    def test_local_mode_can_open_all_app_projects_without_merging_project_data(self):
        projects_response = self.client.get('/api/app-automation/projects/?page_size=100')
        self.assertEqual(projects_response.status_code, 200, projects_response.data)
        self.assertEqual(
            {item['id'] for item in projects_response.data['results']},
            {self.project_a.id, self.project_b.id},
        )

        project_a_response = self.client.get(
            f'/api/app-automation/test-cases/?project={self.project_a.id}&page_size=100'
        )
        project_b_response = self.client.get(
            f'/api/app-automation/test-cases/?project={self.project_b.id}&page_size=100'
        )
        self.assertEqual(project_a_response.status_code, 200, project_a_response.data)
        self.assertEqual(project_b_response.status_code, 200, project_b_response.data)
        self.assertEqual(
            [item['id'] for item in project_a_response.data['results']],
            [self.case_a.id],
        )
        self.assertEqual(
            [item['id'] for item in project_b_response.data['results']],
            [self.case_b.id],
        )
