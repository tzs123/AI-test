from django.test import TestCase as DjangoTestCase
from rest_framework.test import APIClient

from apps.projects.models import Project
from apps.testcases.models import TestCase
from apps.users.models import User


class TestReportDashboardPermissionTests(DjangoTestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='report-dashboard-user',
            password='testpass123',
        )
        self.other_user = User.objects.create_user(
            username='report-dashboard-other',
            password='testpass123',
        )
        self.project = Project.objects.create(
            name='我的报告项目',
            owner=self.user,
        )
        self.other_project = Project.objects.create(
            name='他人报告项目',
            owner=self.other_user,
        )
        TestCase.objects.create(
            title='我的测试用例',
            project=self.project,
            steps='步骤',
            expected_result='结果',
            author=self.user,
        )
        TestCase.objects.create(
            title='他人的测试用例',
            project=self.other_project,
            steps='步骤',
            expected_result='结果',
            author=self.other_user,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_dashboard_only_counts_accessible_testcases(self):
        response = self.client.get('/api/reports/reports/dashboard/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['total_cases'], 1)

    def test_dashboard_inaccessible_project_filter_returns_zero(self):
        response = self.client.get(
            '/api/reports/reports/dashboard/',
            {'project': self.other_project.id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['total_cases'], 0)
