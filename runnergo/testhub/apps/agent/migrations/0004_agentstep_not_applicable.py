from django.db import migrations, models


CONDITIONAL_MESSAGES = (
    '未发现失败证据',
    '没有可应用的修复建议',
)


def has_any(context, keys):
    return any(context.get(key) for key in keys)


def is_not_applicable(step, context, message):
    if any(marker in message for marker in CONDITIONAL_MESSAGES):
        return True
    if step.tool_name == 'run_api_test' and '未绑定 API 测试模块' in message:
        return not has_any(context, (
            'api_url', 'api_doc', 'swagger_url', 'api_test_objects',
            'test_object_refs', 'test_object_ref',
        ))
    if step.tool_name == 'run_app_test' and '未绑定 APP 自动化' in message:
        return not has_any(context, (
            'app_case_ids', 'app_test_case_ids', 'app_test_object_refs',
            'app_project', 'app_project_id', 'device', 'device_id',
        ))
    if step.tool_name == 'run_performance_test' and '未绑定真实性能测试场景' in message:
        return not has_any(context, (
            'performance_plan_id', 'performance_plan_ids',
            'performance_scene_id', 'performance_scene_ids',
            'performance_test_ids',
        ))
    return False


def classify_historical_steps(apps, schema_editor):
    AgentStep = apps.get_model('agent', 'AgentStep')
    AgentTask = apps.get_model('agent', 'AgentTask')
    TestAsset = apps.get_model('agent', 'TestAsset')
    TestExecutionResult = apps.get_model('agent', 'TestExecutionResult')
    TestReport = apps.get_model('reports', 'TestReport')

    task_ids = set()
    steps = AgentStep.objects.filter(status='SKIPPED').select_related('task')
    for step in steps.iterator():
        payload = step.output_payload if isinstance(step.output_payload, dict) else {}
        message = str(payload.get('message') or '')
        context = step.task.context if isinstance(step.task.context, dict) else {}
        if is_not_applicable(step, context, message):
            step.status = 'NOT_APPLICABLE'
            step.save(update_fields=['status'])
            TestExecutionResult.objects.filter(
                task_id=step.task_id,
                step_id=step.id,
                status='SKIPPED',
            ).update(status='NOT_APPLICABLE')
            task_ids.add(step.task_id)

    for task in AgentTask.objects.filter(id__in=task_ids, status='COMPLETED'):
        has_unexecuted = AgentStep.objects.filter(
            task_id=task.id,
            status__in=['PENDING', 'RUNNING', 'SKIPPED', 'FAILED'],
        ).exists()
        if not has_unexecuted:
            task.current_step = '已生成测试报告'
            task.save(update_fields=['current_step'])

        success_steps = AgentStep.objects.filter(task_id=task.id, status='SUCCESS').count()
        skipped_steps = AgentStep.objects.filter(task_id=task.id, status='SKIPPED').count()
        not_applicable_steps = AgentStep.objects.filter(task_id=task.id, status='NOT_APPLICABLE').count()
        real_results = TestExecutionResult.objects.filter(task_id=task.id).exclude(
            status__in=['SKIPPED', 'NOT_APPLICABLE'],
        ).count()
        skipped_results = TestExecutionResult.objects.filter(task_id=task.id, status='SKIPPED').count()
        not_applicable_results = TestExecutionResult.objects.filter(
            task_id=task.id,
            status='NOT_APPLICABLE',
        ).count()
        summary_updates = {
            'success_steps': success_steps,
            'skipped_steps': skipped_steps,
            'not_applicable_steps': not_applicable_steps,
            'real_execution_results': real_results,
            'skipped_execution_results': skipped_results,
            'not_applicable_execution_results': not_applicable_results,
        }
        for report in TestReport.objects.filter(summary__agent_task_id=task.id):
            report.summary = {**(report.summary or {}), **summary_updates}
            content = report.content if isinstance(report.content, dict) else {}
            content['execution_results'] = list(
                TestExecutionResult.objects.filter(task_id=task.id).values(
                    'module', 'status', 'logs', 'response_payload',
                )
            )
            report.content = content
            report.save(update_fields=['summary', 'content'])
        for asset in TestAsset.objects.filter(task_id=task.id, asset_type='report'):
            asset.metadata = {**(asset.metadata or {}), **summary_updates}
            asset.save(update_fields=['metadata'])


class Migration(migrations.Migration):

    dependencies = [
        ('agent', '0003_agent_explorer_memory_rag'),
        ('reports', '0002_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='agentstep',
            name='status',
            field=models.CharField(
                choices=[
                    ('PENDING', '等待中'),
                    ('RUNNING', '执行中'),
                    ('SUCCESS', '成功'),
                    ('FAILED', '失败'),
                    ('SKIPPED', '跳过'),
                    ('NOT_APPLICABLE', '不适用'),
                ],
                db_index=True,
                default='PENDING',
                max_length=20,
                verbose_name='状态',
            ),
        ),
        migrations.AlterField(
            model_name='testexecutionresult',
            name='status',
            field=models.CharField(
                choices=[
                    ('PENDING', '等待中'),
                    ('RUNNING', '执行中'),
                    ('PASSED', '通过'),
                    ('FAILED', '失败'),
                    ('SKIPPED', '跳过'),
                    ('NOT_APPLICABLE', '不适用'),
                ],
                db_index=True,
                default='PENDING',
                max_length=20,
                verbose_name='状态',
            ),
        ),
        migrations.RunPython(classify_historical_steps, migrations.RunPython.noop),
    ]
