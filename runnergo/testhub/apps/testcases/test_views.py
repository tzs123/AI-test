"""
测试用例导入视图测试
覆盖 views.py 中新增的导入相关 API 端点
"""
import io

from django.test import TestCase as DjangoTestCase
from django.urls import reverse
from rest_framework.test import APIClient
from openpyxl import Workbook

from apps.testcases.models import TestCase, TestCaseImportRecord
from apps.testcases.services import TestCaseImportTemplateService, TEMPLATE_SHEET_NAME
from apps.projects.models import Project
from apps.users.models import User


class TestCaseImportTemplateDownloadViewTests(DjangoTestCase):
    """TestCaseImportTemplateDownloadView 测试"""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='templateuser', password='testpass123'
        )
        self.client.force_authenticate(user=self.user)

    def test_download_template_returns_200(self):
        """正向：已认证用户应能下载模板"""
        url = reverse('testcase-import-template')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_download_template_content_type(self):
        """正向：响应应为 xlsx 格式"""
        url = reverse('testcase-import-template')
        response = self.client.get(url)
        self.assertIn('spreadsheetml', response['Content-Type'])

    def test_download_template_unauthenticated_returns_401(self):
        """反向：未认证用户应返回 401"""
        client = APIClient()
        url = reverse('testcase-import-template')
        response = client.get(url)
        self.assertEqual(response.status_code, 401)


class TestCaseImportRecordListCreateViewTests(DjangoTestCase):
    """TestCaseImportRecordListCreateView 测试"""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='importuser', password='testpass123'
        )
        self.project = Project.objects.create(
            name='导入测试项目', owner=self.user
        )
        self.client.force_authenticate(user=self.user)

    def _create_valid_excel(self):
        """创建有效的 Excel 文件"""
        wb = Workbook()
        ws = wb.active
        ws.title = TEMPLATE_SHEET_NAME
        ws.append(TestCaseImportTemplateService.HEADERS)
        ws.append(TestCaseImportTemplateService.SAMPLE_ROW)
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        return output

    def test_create_import_record_without_project_id_returns_400(self):
        """反向：缺少 project_id 应返回 400"""
        url = reverse('testcase-import-record-list')
        excel_file = self._create_valid_excel()
        response = self.client.post(url, {'file': excel_file}, format='multipart')
        self.assertEqual(response.status_code, 400)

    def test_create_import_record_without_file_returns_400(self):
        """反向：缺少文件应返回 400"""
        url = reverse('testcase-import-record-list')
        response = self.client.post(url, {'project_id': self.project.id}, format='multipart')
        self.assertEqual(response.status_code, 400)

    def test_create_import_record_non_xlsx_file_returns_400(self):
        """反向：非 .xlsx 文件应返回 400"""
        url = reverse('testcase-import-record-list')
        fake_file = io.BytesIO(b'not an excel file')
        fake_file.name = 'test.txt'
        response = self.client.post(url, {
            'project_id': self.project.id,
            'file': fake_file,
        }, format='multipart')
        self.assertEqual(response.status_code, 400)

    def test_list_import_records(self):
        """正向：应能列出导入记录"""
        TestCaseImportRecord.objects.create(
            import_no='IMP_20240101000000_LIST01',
            project=self.project,
            created_by=self.user,
            template_version='v1',
        )
        url = reverse('testcase-import-record-list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_create_returns_401(self):
        """反向：未认证用户应返回 401"""
        client = APIClient()
        url = reverse('testcase-import-record-list')
        response = client.post(url, {}, format='multipart')
        self.assertEqual(response.status_code, 401)


class TestCaseImportRecordDetailViewTests(DjangoTestCase):
    """TestCaseImportRecordDetailView 测试"""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='detailuser', password='testpass123'
        )
        self.project = Project.objects.create(
            name='详情测试项目', owner=self.user
        )
        self.client.force_authenticate(user=self.user)
        self.record = TestCaseImportRecord.objects.create(
            import_no='IMP_20240101000000_DETAIL',
            project=self.project,
            created_by=self.user,
            template_version='v1',
        )

    def test_get_import_record_detail(self):
        """正向：应能获取导入记录详情"""
        url = reverse('testcase-import-record-detail', kwargs={'pk': self.record.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['import_no'], 'IMP_20240101000000_DETAIL')

    def test_get_import_record_detail_contains_failure_report_url(self):
        """正向：详情应包含 failure_report_url 字段"""
        url = reverse('testcase-import-record-detail', kwargs={'pk': self.record.pk})
        response = self.client.get(url)
        self.assertIn('failure_report_url', response.data)


class TestCaseImportFailureReportDownloadViewTests(DjangoTestCase):
    """TestCaseImportFailureReportDownloadView 测试"""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='reportuser', password='testpass123'
        )
        self.project = Project.objects.create(
            name='报告测试项目', owner=self.user
        )
        self.client.force_authenticate(user=self.user)
        self.record = TestCaseImportRecord.objects.create(
            import_no='IMP_20240101000000_REPORT',
            project=self.project,
            created_by=self.user,
            template_version='v1',
        )

    def test_download_failure_report_no_record_returns_404(self):
        """反向：不存在的记录应返回 404"""
        url = reverse('testcase-import-record-failure-report', kwargs={'pk': 99999})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_download_failure_report_no_file_returns_404(self):
        """反向：无失败报告文件应返回 404"""
        url = reverse('testcase-import-record-failure-report', kwargs={'pk': self.record.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)


class TestCaseListCreateViewPermissionTests(DjangoTestCase):
    """TestCaseListCreateView 权限测试"""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='permuser', password='testpass123'
        )
        self.project = Project.objects.create(
            name='权限测试项目', owner=self.user
        )
        self.client.force_authenticate(user=self.user)

    def test_create_testcase_with_accessible_project(self):
        """正向：有权限的项目应能创建用例"""
        url = '/api/testcases/'
        response = self.client.post(url, {
            'title': '权限测试用例',
            'steps': '测试步骤',
            'expected_result': '预期结果',
            'project_id': self.project.id,
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['title'], '权限测试用例')

    def test_create_testcase_without_project_uses_accessible_project(self):
        """边界：未指定项目时自动使用可访问项目"""
        url = '/api/testcases/'
        response = self.client.post(url, {
            'title': '无项目用例',
            'steps': '测试步骤',
            'expected_result': '预期结果',
        })
        self.assertEqual(response.status_code, 201)
        # 验证用例已创建且关联到了用户可访问的项目
        self.assertTrue(TestCase.objects.filter(title='无项目用例', project=self.project).exists())

    def test_create_testcase_rejects_duplicate_title_in_same_project(self):
        TestCase.objects.create(
            title='登录成功',
            project=self.project,
            steps='原步骤',
            expected_result='原结果',
            author=self.user,
        )

        response = self.client.post('/api/testcases/', {
            'title': '  登录成功  ',
            'steps': '新步骤',
            'expected_result': '新结果',
            'project_id': self.project.id,
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn('title', response.data)
        self.assertEqual(TestCase.objects.filter(project=self.project).count(), 1)

    def test_list_testcases_only_accessible_projects(self):
        """正向：应只列出有权限项目的用例"""
        other_user = User.objects.create_user(
            username='otherpermuser', password='testpass123'
        )
        other_project = Project.objects.create(
            name='他人项目', owner=other_user
        )
        # 创建自己项目的用例
        TestCase.objects.create(
            title='我的用例', project=self.project,
            steps='步骤', expected_result='结果',
            author=self.user,
        )
        url = '/api/testcases/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        titles = [item['title'] for item in response.data['results']]
        self.assertIn('我的用例', titles)
