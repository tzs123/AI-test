from django.db import migrations


def cleanup_non_allure_agent_reports(apps, schema_editor):
    AgentTask = apps.get_model('agent', 'AgentTask')
    TestAsset = apps.get_model('agent', 'TestAsset')
    TestReport = apps.get_model('reports', 'TestReport')

    task_ids = set(AgentTask.objects.filter(status='NEEDS_INPUT').values_list('id', flat=True))
    for asset in TestAsset.objects.filter(
        task_id__in=task_ids,
        asset_type='report',
    ).iterator():
        metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
        if metadata.get('report_format') != 'allure':
            asset.delete()

    for report in TestReport.objects.all().iterator():
        summary = report.summary if isinstance(report.summary, dict) else {}
        if summary.get('agent_task_id') in task_ids:
            report.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('agent', '0006_needs_input_and_execution_evidence'),
        ('reports', '0002_initial'),
    ]

    operations = [
        migrations.RunPython(cleanup_non_allure_agent_reports, migrations.RunPython.noop),
    ]
