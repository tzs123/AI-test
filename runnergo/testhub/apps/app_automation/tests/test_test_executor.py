# -*- coding: utf-8 -*-
import os
import json
import tempfile

from django.conf import settings
from django.test import SimpleTestCase

from apps.app_automation.executors.test_executor import AppTestExecutor


class AppTestExecutorCommandTests(SimpleTestCase):
    def test_pytest_command_only_collects_runtime_ui_flow_entrypoint(self):
        executor = AppTestExecutor(base_path=str(settings.BASE_DIR))

        args = executor._build_pytest_args('/tmp/allure-results')

        self.assertEqual(args[3], AppTestExecutor.RUNTIME_TEST_TARGET)
        self.assertNotIn('apps/app_automation/tests/', args)

    def test_prepare_allure_results_dir_removes_stale_results(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            results_dir = os.path.join(temp_dir, 'execution_51')
            os.makedirs(results_dir)
            stale_result = os.path.join(results_dir, 'stale-result.json')
            with open(stale_result, 'w', encoding='utf-8') as handle:
                handle.write('{}')

            AppTestExecutor._prepare_allure_results_dir(results_dir)

            self.assertTrue(os.path.isdir(results_dir))
            self.assertEqual(os.listdir(results_dir), [])

    def test_parse_allure_counts_ui_flow_children_and_missing_tail_as_skipped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ui_flow_steps = [
                {'name': f'步骤{i}', 'status': 'failed' if i == 68 else 'passed'}
                for i in range(1, 69)
            ]
            result_file = os.path.join(temp_dir, 'case-result.json')
            with open(result_file, 'w', encoding='utf-8') as handle:
                json.dump({
                    'name': '用例名称: 录制脚本',
                    'status': 'failed',
                    'steps': [{
                        'name': '执行 UI Flow',
                        'status': 'failed',
                        'steps': ui_flow_steps,
                    }],
                }, handle)

            result = AppTestExecutor._parse_allure_results(
                AppTestExecutor(base_path=str(settings.BASE_DIR)),
                temp_dir,
                expected_step_count=69,
            )

            self.assertEqual(result['total'], 69)
            self.assertEqual(result['passed'], 67)
            self.assertEqual(result['failed'], 1)
            self.assertEqual(result['skipped'], 1)
            self.assertTrue(result['step_level'])
