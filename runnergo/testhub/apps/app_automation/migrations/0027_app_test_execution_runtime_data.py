from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0026_add_popup_select_text_component'),
    ]

    operations = [
        migrations.AddField(
            model_name='apptestexecution',
            name='runtime_override',
            field=models.JSONField(
                blank=True,
                default=list,
                help_text='仅用于本次执行的 runtimeOverride，不修改原始用例',
                verbose_name='执行前数据覆盖',
            ),
        ),
        migrations.AddField(
            model_name='apptestexecution',
            name='runtime_data',
            field=models.JSONField(
                blank=True,
                default=list,
                help_text='RuntimeCase 合并并解析动态变量后的最终输入数据',
                verbose_name='实际执行数据',
            ),
        ),
    ]
