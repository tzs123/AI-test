from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate
from types import SimpleNamespace

from apps.projects.models import Project, ProjectMember

from .models import (
    AIModelConfig,
    AnalysisTask,
    BusinessRequirement,
    GeneratedTestCase,
    RequirementAnalysis,
    RequirementDocument,
    TestCaseGenerationTask,
)
from .serializers import AIModelConfigSerializer, TestCaseGenerationRequestSerializer
from .views import (
    AIModelConfigViewSet,
    AnalysisTaskViewSet,
    BusinessRequirementViewSet,
    GeneratedTestCaseViewSet,
    RequirementAnalysisViewSet,
    RequirementDocumentViewSet,
    TestCaseGenerationTaskViewSet,
)


class GenerationTaskAccessTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner = user_model.objects.create_user(username='task-owner')
        self.viewer = user_model.objects.create_user(username='task-viewer')
        self.outsider = user_model.objects.create_user(username='task-outsider')
        self.project = Project.objects.create(name='AI project', owner=self.owner)
        ProjectMember.objects.create(project=self.project, user=self.viewer, role='viewer')
        self.task = TestCaseGenerationTask.objects.create(
            task_id='TASK_ACCESS_1',
            title='access test',
            requirement_text='requirement',
            project=self.project,
            created_by=self.owner,
        )
        self.factory = APIRequestFactory()

    def test_outsider_cannot_list_or_read_project_task(self):
        list_request = self.factory.get('/api/requirement-analysis/testcase-generation/')
        force_authenticate(list_request, user=self.outsider)
        list_response = TestCaseGenerationTaskViewSet.as_view({'get': 'list'})(list_request)
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.data['count'], 0)

        detail_request = self.factory.get('/progress/')
        force_authenticate(detail_request, user=self.outsider)
        detail_response = TestCaseGenerationTaskViewSet.as_view(
            {'get': 'progress'}
        )(detail_request, task_id=self.task.task_id)
        self.assertEqual(detail_response.status_code, 404)

    def test_project_member_can_read_and_cancel_project_task(self):
        token_request = self.factory.get('/stream-token/')
        force_authenticate(token_request, user=self.viewer)
        token_response = TestCaseGenerationTaskViewSet.as_view(
            {'get': 'stream_token'}
        )(token_request, task_id=self.task.task_id)
        self.assertEqual(token_response.status_code, 200)
        self.assertTrue(token_response.data['stream_token'])

        cancel_request = self.factory.post('/cancel/', {}, format='json')
        force_authenticate(cancel_request, user=self.viewer)
        cancel_response = TestCaseGenerationTaskViewSet.as_view(
            {'post': 'cancel'}
        )(cancel_request, task_id=self.task.task_id)
        self.assertEqual(cancel_response.status_code, 200)

    def test_generation_request_rejects_foreign_project(self):
        serializer = TestCaseGenerationRequestSerializer(
            data={
                'title': 'foreign project',
                'requirement_text': 'must not attach',
                'project': self.project.pk,
            },
            context={'request': SimpleNamespace(user=self.outsider)},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn('project', serializer.errors)


@override_settings(SECURITY_CREDENTIALS_KEY='ai-credential-test-key')
class AIModelCredentialTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.admin = user_model.objects.create_user(username='ai-admin', is_staff=True)
        self.user = user_model.objects.create_user(username='ai-user')
        self.config = AIModelConfig.objects.create(
            name='encrypted model',
            model_type='other',
            role='writer',
            api_key='sk-sensitive-value',
            base_url='https://ai.example.test',
            model_name='model-1',
            created_by=self.admin,
        )
        self.factory = APIRequestFactory()

    def test_api_key_is_encrypted_at_rest_and_masked_in_api(self):
        self.config.refresh_from_db()
        self.assertTrue(self.config.api_key.startswith('fernet:v1:'))
        self.assertNotIn('sk-sensitive-value', self.config.api_key)
        self.assertEqual(self.config.get_api_key(), 'sk-sensitive-value')

        data = AIModelConfigSerializer(self.config).data
        self.assertNotIn('api_key', data)
        self.assertEqual(data['api_key_masked'], 'sk-***********alue')

    def test_model_configuration_is_available_to_authenticated_users(self):
        regular_request = self.factory.get('/api/requirement-analysis/ai-models/')
        force_authenticate(regular_request, user=self.user)
        regular_response = AIModelConfigViewSet.as_view({'get': 'list'})(regular_request)
        self.assertEqual(regular_response.status_code, 200)

        admin_request = self.factory.get('/api/requirement-analysis/ai-models/')
        force_authenticate(admin_request, user=self.admin)
        admin_response = AIModelConfigViewSet.as_view({'get': 'list'})(admin_request)
        self.assertEqual(admin_response.status_code, 200)


class RequirementAssetIsolationTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner = user_model.objects.create_user(username='requirement-owner')
        self.viewer = user_model.objects.create_user(username='requirement-viewer')
        self.outsider = user_model.objects.create_user(username='requirement-outsider')
        self.project = Project.objects.create(name='Requirement project', owner=self.owner)
        ProjectMember.objects.create(project=self.project, user=self.viewer, role='viewer')
        self.document = RequirementDocument.objects.create(
            title='private requirement',
            file='requirement_docs/private.txt',
            document_type='txt',
            uploaded_by=self.owner,
            project=self.project,
        )
        self.analysis = RequirementAnalysis.objects.create(
            document=self.document,
            analysis_report='private analysis',
            requirements_count=1,
        )
        self.requirement = BusinessRequirement.objects.create(
            analysis=self.analysis,
            requirement_id='REQ-PRIVATE-1',
            requirement_name='private business requirement',
            requirement_type='functional',
            module='private module',
            requirement_level='high',
            description='private description',
            acceptance_criteria='private acceptance',
        )
        self.case = GeneratedTestCase.objects.create(
            requirement=self.requirement,
            case_id='TC-PRIVATE-1',
            title='private generated test case',
            priority='P1',
            precondition='private precondition',
            test_steps='private test steps long enough',
            expected_result='private result',
        )
        self.task = AnalysisTask.objects.create(
            task_id='ANALYSIS-PRIVATE-1',
            task_type='requirement_analysis',
            document=self.document,
        )
        self.factory = APIRequestFactory()

    def _list(self, viewset, user):
        request = self.factory.get('/')
        force_authenticate(request, user=user)
        return viewset.as_view({'get': 'list'})(request)

    def test_outsider_cannot_list_requirement_derived_assets(self):
        for viewset in (
            RequirementDocumentViewSet,
            RequirementAnalysisViewSet,
            BusinessRequirementViewSet,
            GeneratedTestCaseViewSet,
            AnalysisTaskViewSet,
        ):
            with self.subTest(viewset=viewset.__name__):
                response = self._list(viewset, self.outsider)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data['count'], 0)

    def test_project_member_can_read_and_modify_document(self):
        response = self._list(RequirementDocumentViewSet, self.viewer)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)

        patch_request = self.factory.patch('/', {'title': 'member change'}, format='json')
        force_authenticate(patch_request, user=self.viewer)
        patch_response = RequirementDocumentViewSet.as_view({'patch': 'partial_update'})(
            patch_request,
            pk=self.document.pk,
        )
        self.assertEqual(patch_response.status_code, 200)

    def test_document_update_rejects_foreign_project(self):
        foreign_project = Project.objects.create(name='Foreign project', owner=self.outsider)
        request = self.factory.patch('/', {'project': foreign_project.pk}, format='json')
        force_authenticate(request, user=self.owner)
        response = RequirementDocumentViewSet.as_view({'patch': 'partial_update'})(
            request,
            pk=self.document.pk,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('project', response.data)
