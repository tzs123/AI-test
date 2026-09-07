from django.test import TestCase
from rest_framework.test import APIClient

from apps.executions.models import TestPlan, TestRun, TestRunCase
from apps.projects.models import Project
from apps.testcases.models import TestCase as ManagedTestCase
from apps.users.models import User


class ExecutionProjectScopeTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='execution-owner', password='testpass123')
        self.other = User.objects.create_user(username='execution-other', password='testpass123')
        self.project = Project.objects.create(name='可见项目', owner=self.owner)
        self.other_project = Project.objects.create(name='不可见项目', owner=self.other)
        self.testcase = ManagedTestCase.objects.create(
            project=self.project,
            title='可见用例',
            expected_result='通过',
            author=self.owner,
        )
        self.other_testcase = ManagedTestCase.objects.create(
            project=self.other_project,
            title='不可见用例',
            expected_result='通过',
            author=self.other,
        )
        self.plan = TestPlan.objects.create(name='可见计划', creator=self.owner)
        self.plan.projects.add(self.project)
        self.run = TestRun.objects.create(
            name='可见执行',
            test_plan=self.plan,
            project=self.project,
            assignee=self.owner,
            creator=self.owner,
        )
        self.run_case = TestRunCase.objects.create(
            test_run=self.run,
            testcase=self.testcase,
        )
        self.other_plan = TestPlan.objects.create(name='不可见计划', creator=self.other)
        self.other_plan.projects.add(self.other_project)
        self.other_run = TestRun.objects.create(
            name='不可见执行',
            test_plan=self.other_plan,
            project=self.other_project,
            assignee=self.other,
            creator=self.other,
        )
        TestRunCase.objects.create(test_run=self.other_run, testcase=self.other_testcase)
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def test_plan_run_case_and_history_lists_are_project_scoped(self):
        self.assertEqual(self.client.get('/api/executions/plans/').data['count'], 1)
        self.assertEqual(self.client.get('/api/executions/runs/').data['count'], 1)
        self.assertEqual(self.client.get('/api/executions/run_cases/').data['count'], 1)
        self.assertEqual(self.client.get('/api/executions/history/').data['count'], 0)

        self.assertEqual(
            self.client.get(f'/api/executions/runs/{self.other_run.id}/').status_code,
            404,
        )

    def test_partial_plan_update_keeps_existing_projects_and_runs(self):
        response = self.client.patch(
            f'/api/executions/plans/{self.plan.id}/',
            {'name': '重命名计划'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.name, '重命名计划')
        self.assertEqual(list(self.plan.projects.values_list('id', flat=True)), [self.project.id])
        self.assertEqual(self.plan.test_runs.count(), 1)

    def test_invalid_status_is_rejected(self):
        response = self.client.patch(
            f'/api/executions/run_cases/{self.run_case.id}/update_status/',
            {'status': 'not-a-status'},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
