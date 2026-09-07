from django.db import migrations, models


def encrypt_existing_webhooks(apps, schema_editor):
    from apps.core.crypto import encrypt_json

    NotificationConfig = apps.get_model('core', 'UnifiedNotificationConfig')
    for config in NotificationConfig.objects.all().iterator():
        value = config.webhook_bots or {}
        if not value:
            continue
        NotificationConfig.objects.filter(pk=config.pk).update(
            webhook_bots={},
            webhook_bots_secret=encrypt_json(value),
        )


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0010_bulk_test_data_rows'),
    ]

    operations = [
        migrations.AddField(
            model_name='unifiednotificationconfig',
            name='webhook_bots_secret',
            field=models.TextField(
                blank=True,
                default='',
                editable=False,
                verbose_name='加密 Webhook 机器人配置',
            ),
        ),
        migrations.RunPython(encrypt_existing_webhooks, migrations.RunPython.noop),
    ]
