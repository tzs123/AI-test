import re

from django.db import migrations


MESSAGE = (
    '已生成测试方案和数据，但没有真实执行结果；'
    '请提供可访问目标或绑定可执行 UI、APP、API 用例后继续执行'
)


def _has_planned_tests(test_plan):
    return isinstance(test_plan, dict) and any(
        isinstance(items, list) and items
        for items in test_plan.values()
    )


def reclassify_planning_only_tasks(apps, schema_editor):
    AgentStep = apps.get_model('agent', 'AgentStep')
    AgentTask = apps.get_model('agent', 'AgentTask')
    TestExecutionResult = apps.get_model('agent', 'TestExecutionResult')

    for task in AgentTask.objects.filter(status__in=['COMPLETED', 'FAILED']).iterator():
        if not _has_planned_tests(task.test_plan):
            continue
        results = TestExecutionResult.objects.filter(task_id=task.id)
        if task.started_at:
            results = results.filter(created_at__gte=task.started_at)
        has_real_execution = False
        for result in results.iterator():
            payload = result.response_payload if isinstance(result.response_payload, dict) else {}
            if result.module != 'browser' and result.status in {'PASSED', 'FAILED'} and payload.get('real_execution') is not False:
                has_real_execution = True
                break
        if has_real_execution:
            continue

        AgentStep.objects.filter(
            task_id=task.id,
            tool_name='create_report',
            status='SUCCESS',
        ).update(
            status='NOT_APPLICABLE',
            output_payload={
                'status': 'not_applicable',
                'message': '未发生真实测试执行，因此不生成质量报告。',
                'report_id': None,
                'report_assets': [],
            },
            error_message='',
        )
        context = task.context if isinstance(task.context, dict) else {}
        requirement = str(task.user_requirement or '')
        has_web_target = bool(re.search(r'(?<![\w@])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?/', requirement))
        if has_web_target and not context.get('app_project') and not context.get('app_case_ids'):
            for key in ('device', 'device_id', 'app_package', 'app_package_id'):
                context[key] = ''
        task.context = context
        task.status = 'NEEDS_INPUT'
        task.current_step = MESSAGE
        task.save(update_fields=['context', 'status', 'current_step', 'updated_at'])


class Migration(migrations.Migration):

    dependencies = [
        ('agent', '0010_agenttask_stopped_status'),
    ]

    operations = [
        migrations.RunPython(reclassify_planning_only_tasks, migrations.RunPython.noop),
    ]
