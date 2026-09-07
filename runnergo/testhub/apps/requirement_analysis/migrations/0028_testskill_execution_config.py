from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0027_skill_execution_results'),
    ]

    operations = [
        migrations.AddField(
            model_name='testskill',
            name='execution_config',
            field=models.JSONField(blank=True, default=dict, verbose_name='服务器执行配置'),
        ),
    ]
