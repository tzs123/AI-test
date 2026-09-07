from django.db import migrations, models


def encrypt_existing_api_keys(apps, schema_editor):
    from apps.core.crypto import encrypt_secret

    DifyConfig = apps.get_model('assistant', 'DifyConfig')
    for config in DifyConfig.objects.exclude(api_key='').iterator():
        encrypted = encrypt_secret(config.api_key)
        if encrypted != config.api_key:
            DifyConfig.objects.filter(pk=config.pk).update(api_key=encrypted)


class Migration(migrations.Migration):
    dependencies = [
        ('assistant', '0003_copilottask'),
    ]

    operations = [
        migrations.AlterField(
            model_name='difyconfig',
            name='api_key',
            field=models.TextField(help_text='Dify API密钥', verbose_name='API Key（加密存储）'),
        ),
        migrations.RunPython(encrypt_existing_api_keys, migrations.RunPython.noop),
    ]
