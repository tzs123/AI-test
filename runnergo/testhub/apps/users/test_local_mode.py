from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.performance_rule.models import PerformanceRule
from apps.projects.models import Project, ProjectMember
from apps.testcases.models import TestCase


@override_settings(LOCAL_TRUSTED_MODE=True)
class LocalTrustedModeTests(APITestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner_one = user_model.objects.create_user(username='owner-one')
        self.owner_two = user_model.objects.create_user(username='owner-two')
        self.project_one = Project.objects.create(name='APP 项目', owner=self.owner_one)
        self.project_two = Project.objects.create(name='性能项目', owner=self.owner_two)

    def test_api_does_not_require_login_and_local_principal_can_select_every_project(self):
        profile_response = self.client.get('/api/users/me/')
        projects_response = self.client.get('/api/projects/')

        self.assertEqual(profile_response.status_code, 200, profile_response.data)
        self.assertEqual(profile_response.data['username'], 'local-runnergo')
        self.assertTrue(profile_response.data['is_staff'])
        self.assertEqual(projects_response.status_code, 200, projects_response.data)
        project_ids = {item['id'] for item in projects_response.data['results']}
        self.assertEqual(project_ids, {self.project_one.id, self.project_two.id})

        local_user = get_user_model().objects.get(username='local-runnergo')
        memberships = ProjectMember.objects.filter(user=local_user)
        self.assertEqual(set(memberships.values_list('project_id', flat=True)), project_ids)
        self.assertEqual(set(memberships.values_list('role', flat=True)), {'admin'})

    def test_local_mode_preserves_project_ownership_and_project_ids(self):
        self.client.get('/api/users/me/')

        self.project_one.refresh_from_db()
        self.project_two.refresh_from_db()
        self.assertEqual(self.project_one.owner_id, self.owner_one.id)
        self.assertEqual(self.project_two.owner_id, self.owner_two.id)
        self.assertNotEqual(self.project_one.id, self.project_two.id)

    def test_project_and_test_module_filters_keep_data_domains_separate(self):
        api_case = TestCase.objects.create(
            project=self.project_one,
            title='项目一接口用例',
            expected_result='接口成功',
            test_type='api',
            author=self.owner_one,
        )
        TestCase.objects.create(
            project=self.project_one,
            title='项目一UI用例',
            expected_result='页面成功',
            test_type='ui',
            author=self.owner_one,
        )
        TestCase.objects.create(
            project=self.project_two,
            title='项目二接口用例',
            expected_result='接口成功',
            test_type='api',
            author=self.owner_two,
        )
        performance_rule = PerformanceRule.objects.create(
            project=self.project_one,
            max_p95=500,
        )
        PerformanceRule.objects.create(project=self.project_two, max_p95=800)

        api_response = self.client.get(
            f'/api/testcases/?project={self.project_one.id}&test_type=api&page_size=100'
        )
        performance_response = self.client.get(
            f'/api/performance-rule/rules/?project={self.project_one.id}&page_size=100'
        )

        self.assertEqual(api_response.status_code, 200, api_response.data)
        self.assertEqual(
            [item['id'] for item in api_response.data['results']],
            [api_case.id],
        )
        self.assertEqual(performance_response.status_code, 200, performance_response.data)
        self.assertEqual(
            [item['id'] for item in performance_response.data['results']],
            [performance_rule.id],
        )
