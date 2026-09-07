"""
测试用例 Celery 异步导入任务测试
覆盖 tasks.py 中 import_testcases_from_excel 的状态管理逻辑
"""
from unittest.mock import patch, MagicMock

from django.core.files.base import ContentFile
from django.test import TestCase as DjangoTestCase

from apps.testcases.models import TestCaseImportRecord
from apps.testcases.services import ImportSummary
from apps.testcases import tasks as testcase_tasks
from apps.projects.models import Project
from apps.users.models import User


class ImportTestcasesFromExcelTaskTests(DjangoTestCase):
    """import_testcases_from_excel Celery 任务测试"""

    def setUp(self):
        self.user = User.objects.create_user(
            username='taskuser', password='testpass123'
        )
        self.project = Project.objects.create(
            name='任务测试项目', owner=self.user
        )
        self.record = TestCaseImportRecord.objects.create(
            import_no='IMP_20240101000000_TEST01',
            project=self.project,
            template_version='v1',
            created_by=self.user,
            status='pending',
        )

    @patch('apps.testcases.tasks.TestCaseExcelImportService.import_record')
    @patch('apps.testcases.tasks.TestCaseExcelImportService.build_failure_report', return_value=None)
    def test_task_completed_when_all_success(self, mock_build_report, mock_import):
        """正向：全部成功时状态应为 completed"""
        mock_import.return_value = ImportSummary(
            total_rows=5, success_count=5, failed_count=0,
            skip_count=0, failure_details=[]
        )
        result = testcase_tasks.import_testcases_from_excel.run(self.record.id)

        self.record.refresh_from_db()
        self.assertEqual(self.record.status, 'completed')
        self.assertEqual(self.record.progress, 100)
        self.assertEqual(self.record.success_count, 5)
        self.assertEqual(self.record.failed_count, 0)
        self.assertIsNotNone(self.record.completed_at)

    @patch('apps.testcases.tasks.TestCaseExcelImportService.import_record')
    @patch('apps.testcases.tasks.TestCaseExcelImportService.build_failure_report')
    def test_task_partial_success_when_some_failed(self, mock_build_report, mock_import):
        """边界：部分成功时状态应为 partial_success"""
        mock_import.return_value = ImportSummary(
            total_rows=5, success_count=3, failed_count=2,
            skip_count=0, failure_details=[
                {'row_number': 2, 'title': '失败用例', 'errors': ['标题不能为空']}
            ]
        )
        mock_report = ContentFile(b'fake xlsx content', name='IMP_20240101000000_TEST01_failed_rows.xlsx')
        mock_build_report.return_value = mock_report

        result = testcase_tasks.import_testcases_from_excel.run(self.record.id)

        self.record.refresh_from_db()
        self.assertEqual(self.record.status, 'partial_success')
        self.assertEqual(self.record.success_count, 3)
        self.assertEqual(self.record.failed_count, 2)

    @patch('apps.testcases.tasks.TestCaseExcelImportService.import_record')
    @patch('apps.testcases.tasks.TestCaseExcelImportService.build_failure_report', return_value=None)
    def test_task_failed_when_all_failed(self, mock_build_report, mock_import):
        """反向：全部失败时状态应为 failed"""
        mock_import.return_value = ImportSummary(
            total_rows=3, success_count=0, failed_count=3,
            skip_count=0, failure_details=[
                {'row_number': 2, 'title': '', 'errors': ['标题不能为空']},
                {'row_number': 3, 'title': '', 'errors': ['标题不能为空']},
            ]
        )
        result = testcase_tasks.import_testcases_from_excel.run(self.record.id)

        self.record.refresh_from_db()
        self.assertEqual(self.record.status, 'failed')

    @patch('apps.testcases.tasks.TestCaseExcelImportService.import_record')
    def test_task_failed_on_exception(self, mock_import):
        """反向：导入过程抛出异常时状态应为 failed"""
        mock_import.side_effect = Exception('文件读取失败')
        with self.assertRaises(Exception):
            testcase_tasks.import_testcases_from_excel.run(self.record.id)

        self.record.refresh_from_db()
        self.assertEqual(self.record.status, 'failed')
        self.assertEqual(self.record.progress, 100)
        self.assertIn('文件读取失败', self.record.error_message)
        self.assertIsNotNone(self.record.completed_at)

    @patch('apps.testcases.tasks.TestCaseExcelImportService.import_record')
    @patch('apps.testcases.tasks.TestCaseExcelImportService.build_failure_report', return_value=None)
    def test_task_returns_result_dict(self, mock_build_report, mock_import):
        """正向：任务应返回结果字典"""
        mock_import.return_value = ImportSummary(
            total_rows=2, success_count=2, failed_count=0,
            skip_count=0, failure_details=[]
        )
        result = testcase_tasks.import_testcases_from_excel.run(self.record.id)

        self.assertIn('record_id', result)
        self.assertIn('status', result)
        self.assertIn('success_count', result)
        self.assertIn('failed_count', result)
        self.assertEqual(result['status'], 'completed')

    @patch('apps.testcases.tasks.TestCaseExcelImportService.build_failure_report')
    @patch('apps.testcases.tasks.TestCaseExcelImportService.import_record')
    def test_task_builds_failure_report_on_failures(self, mock_import, mock_build_report):
        """正向：有失败记录时应生成失败报告"""
        failure_details = [
            {'row_number': 2, 'title': '失败用例', 'errors': ['标题不能为空']}
        ]
        mock_import.return_value = ImportSummary(
            total_rows=2, success_count=1, failed_count=1,
            skip_count=0, failure_details=failure_details
        )
        mock_report = ContentFile(b'fake xlsx content', name='IMP_20240101000000_TEST01_failed_rows.xlsx')
        mock_build_report.return_value = mock_report

        testcase_tasks.import_testcases_from_excel.run(self.record.id)
        mock_build_report.assert_called_once()

    def test_task_nonexistent_record_raises(self):
        """反向：不存在的记录 ID 应抛出异常"""
        with self.assertRaises(TestCaseImportRecord.DoesNotExist):
            testcase_tasks.import_testcases_from_excel.run(99999)
