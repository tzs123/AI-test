from django.db import migrations, models


def encrypt_existing_api_keys(apps, schema_editor):
    from apps.core.crypto import encrypt_secret

    AIModelConfig = apps.get_model('requirement_analysis', 'AIModelConfig')
    for config in AIModelConfig.objects.exclude(api_key__isnull=True).exclude(api_key='').iterator():
        encrypted = encrypt_secret(config.api_key)
        if encrypted != config.api_key:
            AIModelConfig.objects.filter(pk=config.pk).update(api_key=encrypted)


class Migration(migrations.Migration):
    dependencies = [
        ('requirement_analysis', '0023_requirementdocument_content_hash'),
    ]

    operations = [
        migrations.AlterField(
            model_name='aimodelconfig',
            name='api_key',
            field=models.TextField(blank=True, null=True, verbose_name='API Key（加密存储）'),
        ),
        migrations.RunPython(encrypt_existing_api_keys, migrations.RunPython.noop),
    ]
