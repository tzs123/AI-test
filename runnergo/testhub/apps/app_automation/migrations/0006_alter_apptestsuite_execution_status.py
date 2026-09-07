from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0005_app_test_config_runtime'),
    ]

    operations = [
        migrations.AlterField(
            model_name='apptestsuite',
            name='execution_status',
            field=models.CharField(
                choices=[
                    ('not_run', '未执行'),
                    ('running', '执行中'),
                    ('completed', '已完成'),
                    ('error', '执行异常'),
                    ('stopped', '已停止'),
                ],
                default='not_run',
                max_length=20,
                verbose_name='执行状态',
            ),
        ),
    ]
