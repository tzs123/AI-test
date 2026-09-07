from django.db import migrations


SECURITY_TABLES = (
    'app_security_agent_events',
    'app_security_findings',
    'app_security_agent_tasks',
    'security_evidence_tokens',
    'security_evidence',
    'security_finding_events',
    'security_findings',
    'security_source_scan_jobs',
    'security_scan_jobs',
    'security_endpoints',
    'security_role_profiles',
    'security_credentials',
    'security_import_jobs',
    'security_scanner_nodes',
    'security_assets',
    'security_quality_gate_policies',
    'security_audit_logs',
)


def remove_security_testing_module(apps, schema_editor):
    quote_name = schema_editor.quote_name
    for table in SECURITY_TABLES:
        schema_editor.execute(f'DROP TABLE IF EXISTS {quote_name(table)}')

    ContentType = apps.get_model('contenttypes', 'ContentType')
    ContentType.objects.filter(app_label='security').delete()
    ContentType.objects.filter(
        app_label='app_automation',
        model__in=(
            'appsecurityagenttask',
            'appsecurityfinding',
            'appsecurityagentevent',
        ),
    ).delete()

    with schema_editor.connection.cursor() as cursor:
        # Keep applied app_automation migration history intact. Removing these
        # rows makes Django report historical migrations as unapplied.
        cursor.execute('DELETE FROM django_migrations WHERE app = %s', ['security'])


class Migration(migrations.Migration):
    dependencies = [
        ('app_automation', '0036_redact_notification_log_secrets'),
        ('contenttypes', '0002_remove_content_type_name'),
    ]

    operations = [
        migrations.RunPython(remove_security_testing_module, migrations.RunPython.noop),
    ]
