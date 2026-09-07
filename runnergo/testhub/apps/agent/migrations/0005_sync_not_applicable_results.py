from django.db import migrations


def sync_not_applicable_results(apps, schema_editor):
    AgentStep = apps.get_model('agent', 'AgentStep')
    AgentTask = apps.get_model('agent', 'AgentTask')
    TestAsset = apps.get_model('agent', 'TestAsset')
    TestExecutionResult = apps.get_model('agent', 'TestExecutionResult')
    TestReport = apps.get_model('reports', 'TestReport')

    not_applicable_steps = AgentStep.objects.filter(status='NOT_APPLICABLE')
    task_ids = set(not_applicable_steps.values_list('task_id', flat=True))
    TestExecutionResult.objects.filter(
        step_id__in=not_applicable_steps.values_list('id', flat=True),
        status='SKIPPED',
    ).update(status='NOT_APPLICABLE')

    for task in AgentTask.objects.filter(id__in=task_ids, status='COMPLETED'):
        if not AgentStep.objects.filter(
            task_id=task.id,
            status__in=['PENDING', 'RUNNING', 'SKIPPED', 'FAILED'],
        ).exists():
            task.current_step = '已生成测试报告'
            task.save(update_fields=['current_step'])

        summary_updates = {
            'success_steps': AgentStep.objects.filter(task_id=task.id, status='SUCCESS').count(),
            'skipped_steps': AgentStep.objects.filter(task_id=task.id, status='SKIPPED').count(),
            'not_applicable_steps': AgentStep.objects.filter(
                task_id=task.id,
                status='NOT_APPLICABLE',
            ).count(),
            'real_execution_results': TestExecutionResult.objects.filter(task_id=task.id).exclude(
                status__in=['SKIPPED', 'NOT_APPLICABLE'],
            ).count(),
            'skipped_execution_results': TestExecutionResult.objects.filter(
                task_id=task.id,
                status='SKIPPED',
            ).count(),
            'not_applicable_execution_results': TestExecutionResult.objects.filter(
                task_id=task.id,
                status='NOT_APPLICABLE',
            ).count(),
        }
        execution_results = list(
            TestExecutionResult.objects.filter(task_id=task.id).values(
                'module', 'status', 'logs', 'response_payload',
            )
        )

        for report in TestReport.objects.filter(summary__agent_task_id=task.id):
            report.summary = {**(report.summary or {}), **summary_updates}
            content = report.content if isinstance(report.content, dict) else {}
            content['execution_results'] = execution_results
            report.content = content
            report.save(update_fields=['summary', 'content'])

        for asset in TestAsset.objects.filter(task_id=task.id, asset_type='report'):
            asset.metadata = {**(asset.metadata or {}), **summary_updates}
            asset.save(update_fields=['metadata'])

        for step in AgentStep.objects.filter(task_id=task.id, tool_name='create_report'):
            output = step.output_payload if isinstance(step.output_payload, dict) else {}
            summary = output.get('summary') if isinstance(output.get('summary'), dict) else {}
            step.output_payload = {
                **output,
                'summary': {**summary, **summary_updates},
            }
            step.save(update_fields=['output_payload'])


class Migration(migrations.Migration):

    dependencies = [
        ('agent', '0004_agentstep_not_applicable'),
        ('reports', '0002_initial'),
    ]

    operations = [
        migrations.RunPython(sync_not_applicable_results, migrations.RunPython.noop),
    ]
