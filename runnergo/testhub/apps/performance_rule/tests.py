from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml
from django.test import override_settings
from django.test import TestCase
from rest_framework.test import APIClient

from apps.executions.models import TestPlan, TestRun
from apps.performance_rule.models import PerformanceResult, PerformanceRule
from apps.projects.models import Project
from apps.users.models import User


class PerformanceRuleEvaluationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='perf-user', password='testpass123')
        self.other_user = User.objects.create_user(username='perf-other', password='testpass123')
        self.project = Project.objects.create(name='性能项目', owner=self.user)
        self.other_project = Project.objects.create(name='他人项目', owner=self.other_user)
        self.rule = PerformanceRule.objects.create(
            project=self.project,
            min_tps=5000,
            max_p95=500,
            max_error_rate=0.1,
            max_cpu=80,
            max_db_connection_usage=80,
            max_db_slow_queries_per_sec=1,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_evaluate_generates_pass_and_fail_details(self):
        response = self.client.post('/api/performance-rule/results/evaluate/', {
            'project_id': self.project.id,
            'report_id': 'RPT-001',
            'tps': 6000,
            'p95': 300,
            'error_rate': 0.05,
            'cpu': 90,
        }, format='json')

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['overall_status'], 'FAIL')
        self.assertEqual(response.data['report_id'], 'RPT-001')

        details = {item['metric']: item for item in response.data['details']}
        self.assertEqual(details['TPS']['status'], 'PASS')
        self.assertEqual(details['P95']['status'], 'PASS')
        self.assertEqual(details['Error']['status'], 'PASS')
        self.assertEqual(details['CPU']['status'], 'FAIL')

        result = PerformanceResult.objects.get(report_id='RPT-001')
        self.assertEqual(result.rule, self.rule)
        self.assertEqual(result.cpu, 90)

    def test_evaluate_accepts_nested_metric_aliases(self):
        response = self.client.post('/api/performance-rule/results/evaluate/', {
            'project_id': self.project.id,
            'report_id': 'RPT-002',
            'metrics': {
                'TPS': '6000',
                'ninety_five_request_time_line_value': '300ms',
                'Error': '0.05%',
                'cpu_usage': '70%',
                'DBConnectionUsage': '60%',
                'DBSlowQueriesQPS': '0.2',
            },
        }, format='json')

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['overall_status'], 'PASS')

    def test_repeating_report_evaluation_updates_one_snapshot(self):
        payload = {
            'project_id': self.project.id,
            'report_id': 'RPT-IDEMPOTENT',
            'metrics': {'tps': 6000, 'p95': 300, 'error_rate': 0.05, 'cpu': 70},
        }

        first = self.client.post('/api/performance-rule/results/evaluate/', payload, format='json')
        payload['metrics']['cpu'] = 90
        second = self.client.post('/api/performance-rule/results/evaluate/', payload, format='json')

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(
            PerformanceResult.objects.filter(
                project=self.project,
                report_id='RPT-IDEMPOTENT',
            ).count(),
            1,
        )
        result = PerformanceResult.objects.get(report_id='RPT-IDEMPOTENT')
        self.assertEqual(result.cpu, 90)
        self.assertEqual(result.overall_status, 'FAIL')

    def test_evaluate_rejects_inaccessible_project(self):
        response = self.client.post('/api/performance-rule/results/evaluate/', {
            'project_id': self.other_project.id,
            'metrics': {'tps': 6000, 'p95': 300, 'error_rate': 0.05, 'cpu': 70},
        }, format='json')

        self.assertEqual(response.status_code, 403)

    def test_project_member_can_create_and_update_a_rule(self):
        self.project.members.add(self.other_user)
        self.client.force_authenticate(user=self.other_user)

        create_response = self.client.post('/api/performance-rule/rules/', {
            'project_id': self.project.id,
            'min_tps': 100,
        }, format='json')
        self.assertEqual(create_response.status_code, 201)

        self.client.force_authenticate(user=self.user)
        rule_id = self.rule.id
        self.client.force_authenticate(user=self.other_user)
        update_response = self.client.patch(
            f'/api/performance-rule/rules/{rule_id}/',
            {'max_p95': 100},
            format='json',
        )
        self.assertEqual(update_response.status_code, 200)

    def test_complete_run_generates_performance_result(self):
        plan = TestPlan.objects.create(name='性能计划', creator=self.user)
        plan.projects.add(self.project)
        test_run = TestRun.objects.create(
            name='性能执行',
            test_plan=plan,
            project=self.project,
            creator=self.user,
            assignee=self.user,
        )

        response = self.client.post(f'/api/executions/runs/{test_run.id}/complete_with_performance/', {
            'report_id': 'RPT-RUN-001',
            'metrics': {
                'tps': 6000,
                'p95': 300,
                'error_rate': 0.05,
                'cpu': 90,
            },
        }, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['run']['status'], 'completed')
        self.assertEqual(response.data['performance_result']['overall_status'], 'FAIL')
        self.assertTrue(PerformanceResult.objects.filter(report_id='RPT-RUN-001').exists())

    def test_evaluate_includes_database_metric_sla(self):
        response = self.client.post('/api/performance-rule/results/evaluate/', {
            'project_id': self.project.id,
            'report_id': 'RPT-DB-001',
            'metrics': {
                'tps': 6000,
                'p95': 300,
                'error_rate': 0.05,
                'cpu': 70,
                'db_connection_usage': 92,
                'db_slow_queries_per_sec': 2,
                'db_qps': 1200,
                'db_row_lock_waits_per_sec': 0.5,
            },
        }, format='json')

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['overall_status'], 'FAIL')
        details = {item['metric']: item for item in response.data['details']}
        self.assertEqual(details['DB连接使用率']['status'], 'FAIL')
        self.assertEqual(details['DB慢查询/s']['status'], 'FAIL')

        result = PerformanceResult.objects.get(report_id='RPT-DB-001')
        self.assertEqual(result.db_connection_usage, 92)
        self.assertEqual(result.db_qps, 1200)


class ExporterTargetApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='exporter-user', password='testpass123', is_staff=True
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.settings = override_settings(
            PROMETHEUS_TARGET_DIR=self.temp_dir.name,
            PROMETHEUS_RELOAD_URL='',
        )
        self.settings.enable()
        self.addCleanup(self.settings.disable)

    def _read_yaml(self, filename):
        path = Path(self.temp_dir.name) / filename
        return yaml.safe_load(path.read_text(encoding='utf-8'))

    def test_list_exporter_targets_starts_empty(self):
        response = self.client.get('/api/performance-rule/exporters/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['process_targets'], [])
        self.assertEqual(response.data['jmx_targets'], [])
        self.assertEqual(response.data['mysql_targets'], [])

    def test_save_exporter_targets_writes_process_and_jmx_files(self):
        response = self.client.post('/api/performance-rule/exporters/', {
            'app': 'order-service',
            'app_url': 'http://172.16.0.88:9527/home?channelId=JDYFWH&productId=JDYPRD01',
            'process_target': '172.16.0.88:9256',
            'jmx_target': '172.16.0.88:9404',
            'mysql_target': 'mysql-exporter:9104',
            'database_name': 'jdy_apply_db',
        }, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['reload']['status'], 'skipped')
        self.assertEqual(response.data['process_targets'][0]['target'], '172.16.0.88:9256')
        self.assertEqual(response.data['jmx_targets'][0]['target'], '172.16.0.88:9404')
        self.assertEqual(response.data['mysql_targets'][0]['target'], 'mysql-exporter:9104')

        process_config = self._read_yaml('remote-process-exporters.yml')
        self.assertEqual(process_config[0]['labels']['role'], 'target')
        self.assertEqual(process_config[0]['labels']['app'], 'order-service')
        self.assertEqual(process_config[0]['targets'], ['172.16.0.88:9256'])

        jmx_config = self._read_yaml('remote-jmx-exporters.yml')
        self.assertEqual(jmx_config[0]['targets'], ['172.16.0.88:9404'])

        mysql_config = self._read_yaml('remote-mysql-exporters.yml')
        self.assertEqual(mysql_config[0]['targets'], ['mysql-exporter:9104'])
        self.assertEqual(mysql_config[0]['labels']['role'], 'database')
        self.assertEqual(mysql_config[0]['labels']['database'], 'jdy_apply_db')
        self.assertEqual(mysql_config[0]['labels']['db_access'], 'readonly_metrics')

    @patch.dict('os.environ', {
        'APPLY_DB_HOST': '10.0.0.82',
        'APPLY_DB_PORT': '3306',
        'APPLY_DB_USER': 'web',
        'APPLY_DB_NAME': 'jdy_apply_db',
    })
    def test_mysql_exporter_targets_include_tested_database_address(self):
        self.client.post('/api/performance-rule/exporters/', {
            'app': 'order-service',
            'app_url': 'http://172.16.0.88:9527/home',
            'mysql_target': 'mysql-exporter:9104',
            'database_name': 'jdy_apply_db',
        }, format='json')

        response = self.client.get('/api/performance-rule/exporters/')

        self.assertEqual(response.status_code, 200)
        labels = response.data['mysql_targets'][0]['labels']
        self.assertEqual(labels['db_host'], '10.0.0.82:3306')
        self.assertEqual(labels['tested_db_host'], '10.0.0.82')
        self.assertEqual(labels['tested_db_port'], '3306')

    def test_save_rejects_business_url_as_exporter_target(self):
        response = self.client.post('/api/performance-rule/exporters/', {
            'app': 'order-service',
            'app_url': 'http://172.16.0.88:9527/home',
            'process_target': 'http://172.16.0.88:9527/home',
        }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('process_target', response.data)

    def test_delete_exporter_target_removes_it_from_file(self):
        self.client.post('/api/performance-rule/exporters/', {
            'app': 'order-service',
            'app_url': 'http://172.16.0.88:9527/home',
            'process_target': '172.16.0.88:9256',
        }, format='json')

        response = self.client.delete('/api/performance-rule/exporters/', {
            'target_type': 'process',
            'target': '172.16.0.88:9256',
        }, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['removed'])
        self.assertEqual(response.data['process_targets'], [])
        self.assertEqual(self._read_yaml('remote-process-exporters.yml'), [])

    @patch('apps.performance_rule.exporter_targets.subprocess.run')
    def test_reload_recreates_mysql_exporter_and_prometheus(self, run_mock):
        compose_file = Path(self.temp_dir.name) / 'docker-compose.yml'
        compose_file.write_text('services: {}\n', encoding='utf-8')
        run_mock.return_value.returncode = 0
        run_mock.return_value.stdout = 'started'
        run_mock.return_value.stderr = ''

        with override_settings(
            DEPLOY_COMPOSE_FILE=str(compose_file),
            PERFORMANCE_EXPORTER_RESTART_ENABLED=True,
        ):
            response = self.client.post('/api/performance-rule/exporters/reload/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['reload']['status'], 'success')
        self.assertEqual(response.data['reload']['database_exporter_restart']['status'], 'success')
        command = run_mock.call_args.args[0]
        self.assertEqual(command[:4], ['docker', 'compose', '-f', str(compose_file)])
        self.assertIn('--force-recreate', command)
        self.assertIn('mysql-exporter', command)
        self.assertIn('prometheus', command)

    @patch.dict('os.environ', {
        'APPLY_DB_HOST': '10.0.0.82',
        'APPLY_DB_PORT': '3306',
        'APPLY_DB_USER': 'web',
        'APPLY_DB_PWD': 'top-password',
        'APPLY_DB_NAME': 'jdy_apply_db',
    })
    @patch('apps.performance_rule.exporter_targets.subprocess.run')
    def test_reload_falls_back_to_direct_mysql_exporter_recreate(self, run_mock):
        missing_compose = Path(self.temp_dir.name) / 'missing-compose.yml'
        run_mock.return_value.returncode = 0
        run_mock.return_value.stdout = 'ok'
        run_mock.return_value.stderr = ''

        with override_settings(
            DEPLOY_COMPOSE_FILE=str(missing_compose),
            DOCKER_SOCKET_PATH=str(Path(self.temp_dir.name) / 'missing-docker.sock'),
            PERFORMANCE_EXPORTER_RESTART_ENABLED=True,
        ):
            response = self.client.post('/api/performance-rule/exporters/reload/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['reload']['status'], 'success')
        commands = [call.args[0] for call in run_mock.call_args_list]
        self.assertEqual(commands[0], ['docker', 'rm', '-f', 'runnergo-mysql-exporter'])
        self.assertIn('docker', commands[1])
        self.assertIn('run', commands[1])
        self.assertIn('runnergo-mysql-exporter', commands[1])
        self.assertIn('--network-alias', commands[1])
        self.assertIn('mysql-exporter', commands[1])
        self.assertIn('--mysqld.address=10.0.0.82:3306', commands[1])
        self.assertNotIn('top-password', ' '.join(commands[1]))


class DatabaseDiagnosticsApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='db-diag-user', password='testpass123', is_staff=True
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch.dict('os.environ', {
        'APPLY_DB_HOST': '10.0.0.82',
        'APPLY_DB_PORT': '3306',
        'APPLY_DB_USER': 'web',
        'APPLY_DB_PWD': 'top-password',
        'APPLY_DB_NAME': 'jdy_apply_db',
    })
    @patch('apps.performance_rule.database_diagnostics.pymysql.connect')
    def test_database_diagnostics_uses_readonly_fixed_queries_and_redacts_password(self, connect_mock):
        connection = _FakeConnection()
        connect_mock.return_value = connection

        response = self.client.get('/api/performance-rule/database/diagnostics/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['access_mode'], 'readonly_select_only')
        self.assertEqual(response.data['config']['host'], '10.0.0.82')
        self.assertTrue(response.data['config']['password_configured'])
        self.assertNotIn('top-password', str(response.data))
        self.assertEqual(response.data['metrics']['threads_connected'], 12)
        self.assertEqual(response.data['metrics']['connection_usage_percent'], 6.0)

        executed_sql = '\n'.join(connection.cursor_obj.executed)
        self.assertIn('performance_schema.global_status', executed_sql)
        self.assertIn('performance_schema.global_variables', executed_sql)
        self.assertIn('performance_schema.events_statements_summary_by_digest', executed_sql)
        self.assertIn('SCHEMA_NAME = %s', executed_sql)
        self.assertIn('WHERE db = %s', executed_sql)
        self.assertEqual(connection.cursor_obj.executed_params.count(('jdy_apply_db',)), 2)
        self.assertNotRegex(executed_sql.lower(), r'\b(show|insert|update|delete|replace|alter|drop|create|truncate)\b')


class TestedDatabaseConfigApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='db-config-user', password='testpass123', is_staff=True
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env_file = Path(self.temp_dir.name) / 'runnergo.env'
        self.settings = override_settings(TESTED_DATABASE_ENV_FILE=str(self.env_file))
        self.settings.enable()
        self.addCleanup(self.settings.disable)

    @patch.dict('os.environ', {}, clear=True)
    def test_database_config_saves_local_env_without_returning_password(self):
        response = self.client.post('/api/performance-rule/database/config/', {
            'host': '10.0.0.82',
            'port': 3306,
            'user': 'web',
            'password': 'top-password',
            'database': 'jdy_apply_db',
        }, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['restart_required'])
        self.assertEqual(response.data['access_mode'], 'readonly_metrics')
        self.assertEqual(response.data['config']['host'], '10.0.0.82')
        self.assertNotIn('password', response.data['config'])
        self.assertTrue(response.data['config']['password_configured'])

        get_response = self.client.get('/api/performance-rule/database/config/')
        self.assertEqual(get_response.status_code, 200)
        self.assertNotIn('password', get_response.data['config'])

        content = self.env_file.read_text(encoding='utf-8')
        self.assertIn('APPLY_DB_HOST=10.0.0.82', content)
        self.assertIn('APPLY_DB_PORT=3306', content)
        self.assertIn('APPLY_DB_USER=web', content)
        self.assertIn('APPLY_DB_PWD=top-password', content)
        self.assertIn('APPLY_DB_NAME=jdy_apply_db', content)

    def test_regular_user_can_read_global_performance_configuration(self):
        regular = User.objects.create_user(username='db-config-regular', password='testpass123')
        self.client.force_authenticate(user=regular)
        self.assertEqual(
            self.client.get('/api/performance-rule/database/config/').status_code,
            200,
        )
        self.assertEqual(
            self.client.get('/api/performance-rule/exporters/').status_code,
            200,
        )

    @patch.dict('os.environ', {}, clear=True)
    def test_database_config_preserves_password_when_password_omitted(self):
        self.env_file.write_text(
            'APPLY_DB_HOST=old-db\nAPPLY_DB_PORT=3306\nAPPLY_DB_USER=web\nAPPLY_DB_PWD=secret\nAPPLY_DB_NAME=old_schema\n',
            encoding='utf-8',
        )

        response = self.client.post('/api/performance-rule/database/config/', {
            'host': 'new-db',
            'port': 3307,
            'user': 'readonly',
            'database': 'new_schema',
        }, format='json')

        self.assertEqual(response.status_code, 200)
        content = self.env_file.read_text(encoding='utf-8')
        self.assertIn('APPLY_DB_HOST=new-db', content)
        self.assertIn('APPLY_DB_PORT=3307', content)
        self.assertIn('APPLY_DB_USER=readonly', content)
        self.assertIn('APPLY_DB_PWD=secret', content)
        self.assertIn('APPLY_DB_NAME=new_schema', content)


class _FakeConnection:
    def __init__(self):
        self.cursor_obj = _FakeCursor()

    def cursor(self):
        return self.cursor_obj

    def close(self):
        pass


class _FakeCursor:
    def __init__(self):
        self.executed = []
        self.executed_params = []
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        self.executed.append(sql)
        self.executed_params.append(params)
        if 'performance_schema.global_status' in sql:
            self.rows = [
                {'Variable_name': 'Threads_connected', 'Value': '12'},
                {'Variable_name': 'Threads_running', 'Value': '3'},
                {'Variable_name': 'Max_used_connections', 'Value': '60'},
                {'Variable_name': 'Slow_queries', 'Value': '4'},
                {'Variable_name': 'Innodb_row_lock_waits', 'Value': '2'},
            ]
        elif 'performance_schema.global_variables' in sql:
            self.rows = [
                {'Variable_name': 'max_connections', 'Value': '200'},
                {'Variable_name': 'slow_query_log', 'Value': 'ON'},
                {'Variable_name': 'long_query_time', 'Value': '1.000000'},
                {'Variable_name': 'log_output', 'Value': 'TABLE'},
            ]
        elif 'events_statements_summary_by_digest' in sql:
            self.rows = [
                {
                    'schema_name': 'jdy_apply_db',
                    'digest_text': 'SELECT * FROM apply WHERE id = ?',
                    'count_star': 10,
                    'sum_errors': 0,
                    'avg_seconds': 0.2,
                    'max_seconds': 1.1,
                    'first_seen': None,
                    'last_seen': None,
                }
            ]
        elif 'mysql.slow_log' in sql:
            self.rows = []
        else:
            self.rows = []

    def fetchall(self):
        return self.rows
