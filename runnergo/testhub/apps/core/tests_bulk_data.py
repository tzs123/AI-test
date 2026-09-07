import csv
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .bulk_data_generation import _iter_rows, normalize_field_definitions, read_generated_row, run_bulk_test_data_generation, validate_generation_request
from .data_assets import release_assets_for_execution, request_assets_for_case
from .models import BulkTestDataJob, BulkTestDataRow, TestDataAsset, TestDataAssetRequirement


class BulkTestDataGenerationTest(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='bulk-data-user', password='pass')

    def _fields(self):
        return [
            {'name': 'id', 'type': 'sequence', 'config': {'prefix': 'U', 'start': 1, 'width': 4}},
            {'name': 'name', 'type': 'fixed', 'config': {'value': '测试用户'}},
            {'name': 'score', 'type': 'integer', 'config': {'min': 1, 'max': 1}},
        ]

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_streams_csv_without_storing_rows_in_database(self):
        job = BulkTestDataJob.objects.create(
            name='小批量验证',
            asset_type='USER',
            field_definitions=self._fields(),
            total_count=25,
            batch_size=5,
            output_format='csv',
            seed=7,
            created_by=self.user,
        )

        result = run_bulk_test_data_generation(job.id, 'task-csv')

        self.assertEqual(result['status'], BulkTestDataJob.STATUS_COMPLETED)
        job.refresh_from_db()
        self.assertEqual(job.generated_count, 25)
        self.assertEqual(job.progress, 100)
        self.assertGreater(job.output_size, 0)
        self.assertEqual(len(job.field_definitions), 3)
        with job.output_file.open('r') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 25)
        self.assertEqual(rows[0]['id'], 'U0001')
        self.assertEqual(rows[-1]['id'], 'U0025')

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_cancel_removes_partial_file(self):
        job = BulkTestDataJob.objects.create(
            name='取消验证',
            field_definitions=self._fields(),
            total_count=25,
            batch_size=5,
            output_format='jsonl',
            cancel_requested=True,
            created_by=self.user,
        )

        result = run_bulk_test_data_generation(job.id, 'task-cancel')

        job.refresh_from_db()
        self.assertEqual(result['status'], BulkTestDataJob.STATUS_CANCELED)
        self.assertFalse(job.output_file)
        self.assertEqual(job.generated_count, 0)

    def test_invalid_field_definition_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_field_definitions([{'name': 'id', 'type': 'not-supported'}])

    def test_verification_code_generates_six_digits_by_default(self):
        rows = list(_iter_rows([
            {'name': 'code', 'type': 'verification_code', 'config': {}},
        ], 5, 7))

        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row['code'].isdigit() and len(row['code']) == 6 for row in rows))

    def test_generation_limit_supports_ten_million_but_rejects_more(self):
        payload = validate_generation_request({
            'name': '千万级任务',
            'total_count': 10_000_000,
            'field_definitions': self._fields(),
        })
        self.assertEqual(payload['total_count'], 10_000_000)
        with self.assertRaises(ValueError):
            validate_generation_request({
                'name': '超限任务',
                'total_count': 10_000_001,
                'field_definitions': self._fields(),
            })

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_streams_sql_for_database_import(self):
        job = BulkTestDataJob.objects.create(
            name='SQL导入验证',
            asset_type='USER',
            field_definitions=self._fields(),
            total_count=2,
            batch_size=1,
            output_format='sql',
            table_name='runnergo_users',
            sql_dialect='mysql',
            include_create_table=True,
            seed=7,
            created_by=self.user,
        )

        result = run_bulk_test_data_generation(job.id, 'task-sql')

        self.assertEqual(result['status'], BulkTestDataJob.STATUS_COMPLETED)
        job.refresh_from_db()
        with job.output_file.open('r') as stream:
            sql = stream.read()
        self.assertIn('CREATE TABLE IF NOT EXISTS `runnergo_users`', sql)
        self.assertIn('INSERT INTO `runnergo_users`', sql)
        self.assertIn("'测试用户'", sql)

    def test_sql_generation_request_validates_table_name(self):
        payload = validate_generation_request({
            'name': 'SQL任务',
            'total_count': 10,
            'output_format': 'sql',
            'table_name': 'orders_2026',
            'sql_dialect': 'postgresql',
            'field_definitions': self._fields(),
        })
        self.assertEqual(payload['table_name'], 'orders_2026')
        self.assertEqual(payload['sql_dialect'], 'postgresql')
        with self.assertRaises(ValueError):
            validate_generation_request({
                'name': '非法SQL任务',
                'total_count': 1,
                'output_format': 'sql',
                'table_name': 'orders;drop table users',
                'field_definitions': self._fields(),
            })

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_completed_bulk_job_can_publish_and_inject_rows_as_data_asset(self):
        job = BulkTestDataJob.objects.create(
            name='可复用用户数据',
            asset_type='USER',
            field_definitions=self._fields(),
            total_count=3,
            batch_size=2,
            output_format='csv',
            seed=7,
            asset_config={
                'save_to_asset': True,
                'asset_name': '批量用户资产',
                'tags': ['smoke'],
                'auto_create_requirement': True,
                'target_type': 'api_automation',
                'target_case_id': 3001,
                'target_case_name': '注册接口',
                'alias': 'registerData',
                'release_policy': 'release',
                'filters': {},
            },
            created_by=self.user,
        )

        run_bulk_test_data_generation(job.id, 'task-asset')

        job.refresh_from_db()
        asset = TestDataAsset.objects.get(pk=job.data_asset_id)
        self.assertEqual(asset.bulk_job_id, job.id)
        self.assertEqual(asset.payload['_runnergo_source'], 'bulk_test_data')
        self.assertNotIn('sample', asset.payload)
        self.assertEqual(BulkTestDataRow.objects.filter(job=job).count(), 3)
        self.assertTrue(TestDataAssetRequirement.objects.filter(alias='registerData', target_case_id=3001).exists())
        self.assertEqual(read_generated_row(job, 1)['id'], 'U0002')

        context, leases = request_assets_for_case(
            'api_automation', 3001, 'api_automation', 'bulk-run-1', user=self.user,
        )
        self.assertEqual(context['dataAssets']['registerData']['id'], 'U0001')
        self.assertEqual(len(leases), 1)

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_bulk_asset_reference_uses_each_row_in_order_and_cycles_after_total_count(self):
        job = BulkTestDataJob.objects.create(
            name='五组循环数据',
            asset_type='USER',
            field_definitions=self._fields(),
            total_count=5,
            batch_size=2,
            output_format='csv',
            seed=7,
            asset_config={
                'save_to_asset': True,
                'asset_name': '五组用户资产',
                'auto_create_requirement': True,
                'target_type': 'api_automation',
                'target_case_id': 3002,
                'alias': 'registerData',
                'release_policy': 'release',
            },
            created_by=self.user,
        )

        run_bulk_test_data_generation(job.id, 'task-asset-cycle')
        job.refresh_from_db()
        asset = TestDataAsset.objects.get(pk=job.data_asset_id)
        self.assertEqual(asset.payload['data_source'], 'database_rows')
        job.output_file.delete(save=False)
        job.output_file = ''
        job.save(update_fields=['output_file', 'updated_at'])

        values = []
        for execution_index in range(6):
            context, _ = request_assets_for_case(
                'api_automation',
                3002,
                'api_automation',
                f'bulk-cycle-{execution_index}',
                user=self.user,
            )
            values.append(context['dataAssets']['registerData']['id'])
            release_assets_for_execution('api_automation', f'bulk-cycle-{execution_index}')

        self.assertEqual(values, ['U0001', 'U0002', 'U0003', 'U0004', 'U0005', 'U0001'])
        asset.refresh_from_db()
        self.assertEqual(asset.bulk_row_cursor, 1)

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_deleting_generated_requirement_cascades_source_data(self):
        job = BulkTestDataJob.objects.create(
            name='删除级联验证',
            asset_type='USER',
            field_definitions=self._fields(),
            total_count=2,
            batch_size=2,
            output_format='jsonl',
            seed=7,
            asset_config={
                'save_to_asset': True,
                'asset_name': '待删除用户资产',
                'auto_create_requirement': True,
                'target_type': 'api_automation',
                'target_case_id': 3003,
                'alias': 'deleteData',
            },
            created_by=self.user,
        )
        run_bulk_test_data_generation(job.id, 'task-delete-cascade')

        job.refresh_from_db()
        requirement = TestDataAssetRequirement.objects.get(target_case_id=3003, alias='deleteData')
        asset_id = job.data_asset_id
        output_path = job.output_file.path

        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.delete(f'/api/core/test-data-asset-requirements/{requirement.id}/')

        self.assertEqual(response.status_code, 204)
        self.assertFalse(BulkTestDataJob.objects.filter(id=job.id).exists())
        self.assertFalse(BulkTestDataRow.objects.filter(job_id=job.id).exists())
        self.assertFalse(TestDataAsset.objects.filter(id=asset_id).exists())
        self.assertFalse(TestDataAssetRequirement.objects.filter(id=requirement.id).exists())
        self.assertFalse(__import__('pathlib').Path(output_path).exists())

    @patch('apps.core.tasks.generate_bulk_test_data.delay')
    def test_generate_api_creates_async_job(self, delay):
        delay.return_value.id = 'celery-bulk-1'
        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.post('/api/core/bulk-test-data-jobs/generate/', {
            'name': '接口生成任务',
            'total_count': 1_000_000,
            'batch_size': 5000,
            'output_format': 'csv',
            'field_definitions': self._fields(),
        }, format='json')

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data['status'], BulkTestDataJob.STATUS_PENDING)
        self.assertEqual(response.data['total_count'], 1_000_000)
        delay.assert_called_once()

    def test_bulk_delete_removes_terminal_jobs_and_protects_running_jobs(self):
        terminal_jobs = [
            BulkTestDataJob.objects.create(
                name=f'可删除任务-{index}',
                field_definitions=self._fields(),
                total_count=1,
                status=status_value,
                created_by=self.user,
            )
            for index, status_value in enumerate(
                [BulkTestDataJob.STATUS_COMPLETED, BulkTestDataJob.STATUS_FAILED],
                start=1,
            )
        ]
        running = BulkTestDataJob.objects.create(
            name='运行中任务',
            field_definitions=self._fields(),
            total_count=1,
            status=BulkTestDataJob.STATUS_RUNNING,
            created_by=self.user,
        )

        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.post(
            '/api/core/bulk-test-data-jobs/bulk-delete/',
            {'ids': [job.id for job in terminal_jobs]},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(BulkTestDataJob.objects.filter(id__in=[job.id for job in terminal_jobs]).exists())

        response = client.post(
            '/api/core/bulk-test-data-jobs/bulk-delete/',
            {'ids': [running.id]},
            format='json',
        )
        self.assertEqual(response.status_code, 409)
        self.assertTrue(BulkTestDataJob.objects.filter(id=running.id).exists())
