from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0037_remove_security_testing_module'),
    ]

    operations = [
        migrations.AlterField(
            model_name='appagenttask',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', '等待中'),
                    ('planning', '规划中'),
                    ('needs_input', '待补充执行条件'),
                    ('review_required', '待评审草稿'),
                    ('ready', '待执行'),
                    ('executing', '执行中'),
                    ('runtime_executing', '自治执行中'),
                    ('awaiting_approval', '等待敏感操作审批'),
                    ('matrix_executing', '兼容性矩阵执行中'),
                    ('matrix_approval', '兼容性矩阵等待审批'),
                    ('analyzing', '分析中'),
                    ('completed', '已完成'),
                    ('failed', '失败'),
                    ('stopped', '已停止'),
                ],
                db_index=True,
                default='pending',
                max_length=20,
                verbose_name='任务状态',
            ),
        ),
    ]
