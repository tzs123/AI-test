from django.db import migrations


def redact_notification_logs(apps, schema_editor):
    NotificationLog = apps.get_model('app_automation', 'AppNotificationLog')
    for log in NotificationLog.objects.all().iterator():
        recipients = []
        for item in log.recipient_info or []:
            if not isinstance(item, dict):
                continue
            recipients.append({
                key: item.get(key)
                for key in ('name', 'email')
                if item.get(key)
            })

        raw_bot = log.webhook_bot_info or {}
        safe_bot = {
            key: raw_bot.get(key)
            for key in ('type', 'bot_type', 'name', 'enabled')
            if raw_bot.get(key) not in (None, '')
        } if isinstance(raw_bot, dict) else {}

        raw_response = log.response_info or {}
        safe_response = {}
        if isinstance(raw_response, dict) and raw_response.get('status_code') is not None:
            safe_response['status_code'] = raw_response.get('status_code')

        NotificationLog.objects.filter(pk=log.pk).update(
            recipient_info=recipients,
            webhook_bot_info=safe_bot,
            response_info=safe_response,
            error_message=(
                '历史通知失败信息已脱敏'
                if log.error_message else ''
            ),
        )


class Migration(migrations.Migration):
    dependencies = [
        ('app_automation', '0032_add_skipped_steps'),
    ]

    operations = [
        migrations.RunPython(redact_notification_logs, migrations.RunPython.noop),
    ]
