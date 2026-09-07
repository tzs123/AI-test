from django.db import migrations


EXPLORATION_MESSAGE = (
    '浏览器已打开页面并完成截图、元素识别；这是页面探索证据，不代表业务用例执行或通过。'
)
REPORT_MESSAGE = (
    '当前没有真实模块执行报告，不生成无执行证据的报告资产。'
)


def clarify_historical_exploration(apps, schema_editor):
    AgentStep = apps.get_model('agent', 'AgentStep')
    AgentTask = apps.get_model('agent', 'AgentTask')
    task_ids = AgentTask.objects.filter(status='NEEDS_INPUT').values_list('id', flat=True)

    for step in AgentStep.objects.filter(
        task_id__in=task_ids,
        tool_name='run_browser_agent',
        status='SUCCESS',
    ).iterator():
        output = step.output_payload if isinstance(step.output_payload, dict) else {}
        output.update({
            'message': EXPLORATION_MESSAGE,
            'real_execution': False,
            'evidence_type': 'page_exploration',
        })
        step.output_payload = output
        step.save(update_fields=['output_payload'])

    for step in AgentStep.objects.filter(
        task_id__in=task_ids,
        tool_name='create_report',
        status='SUCCESS',
    ).iterator():
        output = step.output_payload if isinstance(step.output_payload, dict) else {}
        output.update({
            'status': 'success',
            'message': REPORT_MESSAGE,
            'report_id': None,
            'report_assets': [],
        })
        step.output_payload = output
        step.save(update_fields=['output_payload'])


class Migration(migrations.Migration):

    dependencies = [
        ('agent', '0007_cleanup_non_allure_agent_reports'),
    ]

    operations = [
        migrations.RunPython(clarify_historical_exploration, migrations.RunPython.noop),
    ]
