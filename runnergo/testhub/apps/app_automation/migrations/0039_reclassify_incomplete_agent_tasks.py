from django.db import migrations


STRUCTURED_INPUT_NEXT_ACTION = (
    '页面解析已完成，但尚未生成或执行测试用例。'
    '请引用可执行的 APP 测试用例后重新创建任务。'
)


def reclassify_incomplete_tasks(apps, schema_editor):
    AppAgentTask = apps.get_model('app_automation', 'AppAgentTask')
    for task in AppAgentTask.objects.filter(status='completed').iterator():
        if task.execution_ids:
            continue
        summary = task.result_summary if isinstance(task.result_summary, dict) else {}
        structured_input = bool(summary.get('structured_input_analysis'))
        coverage_gap = bool(summary.get('coverage_gap'))
        if not structured_input and not coverage_gap:
            continue

        plan = task.plan if isinstance(task.plan, dict) else {}
        next_action = (
            STRUCTURED_INPUT_NEXT_ACTION
            if structured_input else
            plan.get('coverage_message') or '请先补充可执行测试用例'
        )
        summary.update({
            'total': 0,
            'passed': 0,
            'pass_rate': 0,
            'execution_completed': False,
            'needs_input': True,
            'next_action': next_action,
        })
        task.status = 'needs_input'
        task.progress = 35 if structured_input else 30
        task.result_summary = summary
        task.save(update_fields=['status', 'progress', 'result_summary', 'updated_at'])


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0038_app_agent_needs_input_status'),
    ]

    operations = [
        migrations.RunPython(reclassify_incomplete_tasks, migrations.RunPython.noop),
    ]
