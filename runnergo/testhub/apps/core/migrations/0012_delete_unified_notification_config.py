from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0011_encrypt_notification_webhooks'),
    ]

    operations = [
        migrations.DeleteModel(
            name='UnifiedNotificationConfig',
        ),
    ]
