from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0027_app_test_execution_runtime_data'),
    ]

    operations = [
        migrations.AddField(
            model_name='apptestexecution',
            name='cleanup_config',
            field=models.JSONField(blank=True, default=dict, help_text='执行完成后的运行态数据清理配置', verbose_name='数据清理配置'),
        ),
        migrations.AddField(
            model_name='apptestexecution',
            name='runtime_context',
            field=models.JSONField(blank=True, default=dict, help_text='模板、数据集和步骤输出注入的运行态上下文', verbose_name='运行上下文'),
        ),
        migrations.AddField(
            model_name='apptestexecution',
            name='runtime_dataset_id',
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name='运行态数据集ID'),
        ),
        migrations.AddField(
            model_name='apptestexecution',
            name='runtime_dataset_row_index',
            field=models.IntegerField(blank=True, null=True, verbose_name='运行态数据集行号'),
        ),
        migrations.AddField(
            model_name='apptestexecution',
            name='runtime_template_id',
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name='运行态数据模板ID'),
        ),
    ]
