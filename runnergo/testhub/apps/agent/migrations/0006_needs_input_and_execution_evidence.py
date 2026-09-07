from django.db import migrations, models
from django.utils import timezone


UI_EVIDENCE_MESSAGE = (
    '历史结果已纠正：页面截图和元素识别不等于业务用例执行。'
    '该记录没有 Auto-test 执行任务或 Allure 步骤证据，请绑定 UI 自动化 YAML 用例后重新执行。'
)
SECURITY_NOT_APPLICABLE_MESSAGE = (
    '历史结果已纠正：当前只有前端页面 URL，没有 API 地址、OpenAPI 文档或 API 测试对象，'
    '安全接口扫描不适用。'
)


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _has_successful_actions(payload):
    actions = _list(payload.get('actions'))
    if any(_dict(item).get('status') == 'success' for item in actions):
        return True
    for row in _list(payload.get('results')):
        row = _dict(row)
        if any(_dict(item).get('status') == 'success' for item in _list(row.get('actions'))):
            return True
        if int(_dict(row.get('visual_summary')).get('action_count') or 0) > 0:
            return True
    return int(_dict(payload.get('visual_summary')).get('action_count') or 0) > 0


def _has_api_target(context):
    return bool(
        context.get('api_url')
        or context.get('api_doc')
        or context.get('swagger_url')
        or context.get('api_test_objects')
        or context.get('test_object_refs')
        or context.get('test_object_ref')
    )


def correct_historical_execution_evidence(apps, schema_editor):
    AgentStep = apps.get_model('agent', 'AgentStep')
    AgentTask = apps.get_model('agent', 'AgentTask')
    TestAsset = apps.get_model('agent', 'TestAsset')
    TestExecutionResult = apps.get_model('agent', 'TestExecutionResult')
    TestReport = apps.get_model('reports', 'TestReport')

    corrected_task_ids = set()
    for execution in TestExecutionResult.objects.filter(module='ui', status='PASSED').iterator():
        payload = _dict(execution.response_payload)
        has_auto_test_task = bool(payload.get('auto_test_task_ids'))
        has_allure_report = '/report/tasks/' in str(execution.report_url or '')
        if has_auto_test_task or has_allure_report or _has_successful_actions(payload):
            continue
        payload.update({
            'status': 'skipped',
            'real_execution': False,
            'evidence_type': 'page_exploration',
            'message': UI_EVIDENCE_MESSAGE,
        })
        execution.status = 'SKIPPED'
        execution.response_payload = payload
        execution.logs = UI_EVIDENCE_MESSAGE
        execution.report_url = ''
        execution.save(update_fields=['status', 'response_payload', 'logs', 'report_url'])
        if execution.step_id:
            step = AgentStep.objects.filter(id=execution.step_id).first()
            if step:
                output = _dict(step.output_payload)
                output.update({
                    'status': 'skipped',
                    'real_execution': False,
                    'message': UI_EVIDENCE_MESSAGE,
                })
                step.status = 'SKIPPED'
                step.output_payload = output
                step.error_message = ''
                step.save(update_fields=['status', 'output_payload', 'error_message'])
        corrected_task_ids.add(execution.task_id)

    for task in AgentTask.objects.filter(id__in=corrected_task_ids).iterator():
        context = _dict(task.context)
        if not _has_api_target(context):
            security_steps = AgentStep.objects.filter(task_id=task.id, tool_name='run_security_test')
            for execution in TestExecutionResult.objects.filter(
                task_id=task.id,
                module='security',
            ).iterator():
                payload = _dict(execution.response_payload)
                payload.update({
                    'status': 'not_applicable',
                    'real_execution': False,
                    'message': SECURITY_NOT_APPLICABLE_MESSAGE,
                })
                execution.status = 'NOT_APPLICABLE'
                execution.response_payload = payload
                execution.logs = SECURITY_NOT_APPLICABLE_MESSAGE
                execution.report_url = ''
                execution.save(update_fields=['status', 'response_payload', 'logs', 'report_url'])
            security_steps.update(
                status='NOT_APPLICABLE',
                output_payload={
                    'status': 'not_applicable',
                    'real_execution': False,
                    'message': SECURITY_NOT_APPLICABLE_MESSAGE,
                },
                error_message='',
            )

        for execution in TestExecutionResult.objects.filter(task_id=task.id, module='browser').iterator():
            payload = _dict(execution.response_payload)
            payload.update({'real_execution': False, 'evidence_type': 'page_exploration'})
            execution.response_payload = payload
            execution.report_url = ''
            execution.save(update_fields=['response_payload', 'report_url'])

        for step in AgentStep.objects.filter(
            task_id=task.id,
            status__in=['PENDING', 'RUNNING'],
            tool_name__in=['analyze_failure', 'self_heal_test', 'rerun_fixed_test'],
        ):
            step.status = 'NOT_APPLICABLE'
            step.output_payload = {
                'status': 'not_applicable',
                'message': 'UI 业务用例未实际执行，没有可分析、修复或复跑的失败步骤。',
            }
            step.error_message = ''
            step.finished_at = step.finished_at or timezone.now()
            step.save(update_fields=['status', 'output_payload', 'error_message', 'finished_at'])

        for step in AgentStep.objects.filter(
            task_id=task.id,
            status__in=['PENDING', 'RUNNING'],
            tool_name='create_report',
        ):
            step.status = 'SUCCESS'
            step.output_payload = {
                'status': 'success',
                'message': '当前没有真实模块执行报告，不生成无执行证据的报告资产。',
                'report_id': None,
                'report_assets': [],
            }
            step.error_message = ''
            step.finished_at = step.finished_at or timezone.now()
            step.save(update_fields=['status', 'output_payload', 'error_message', 'finished_at'])

        for asset in TestAsset.objects.filter(task_id=task.id, asset_type='report'):
            if _dict(asset.metadata).get('report_format') != 'allure':
                asset.delete()
        TestReport.objects.filter(summary__agent_task_id=task.id).delete()

        has_real_failure = TestExecutionResult.objects.filter(
            task_id=task.id,
            status='FAILED',
        ).exists()
        task.status = 'FAILED' if has_real_failure else 'NEEDS_INPUT'
        task.progress = 100
        task.current_step = (
            '执行存在失败，已保留真实失败证据'
            if has_real_failure else
            'UI 页面已探索，但业务用例未执行；请绑定 UI 自动化 YAML 用例后继续执行'
        )
        task.finished_at = task.finished_at or timezone.now()
        task.save(update_fields=['status', 'progress', 'current_step', 'finished_at'])


class Migration(migrations.Migration):

    dependencies = [
        ('agent', '0005_sync_not_applicable_results'),
        ('reports', '0002_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='agenttask',
            name='status',
            field=models.CharField(
                choices=[
                    ('CREATED', '已创建'),
                    ('ANALYZING', '需求分析中'),
                    ('GENERATING_DATA', '生成测试数据'),
                    ('GENERATING_CASE', '生成测试用例'),
                    ('RUNNING', '执行测试中'),
                    ('ANALYZING_RESULT', '结果分析中'),
                    ('NEEDS_INPUT', '待补充执行条件'),
                    ('COMPLETED', '已完成'),
                    ('FAILED', '失败'),
                ],
                db_index=True,
                default='CREATED',
                max_length=32,
                verbose_name='状态',
            ),
        ),
        migrations.RunPython(correct_historical_execution_evidence, migrations.RunPython.noop),
    ]
