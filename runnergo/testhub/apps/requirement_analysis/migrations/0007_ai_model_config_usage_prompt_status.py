from django.db import migrations, models
import django.db.models.deletion


def backfill_model_usage(apps, schema_editor):
    AIModelConfig = apps.get_model('requirement_analysis', 'AIModelConfig')
    AIModelConfig.objects.filter(role='reviewer').update(model_usage='test_case_review')
    AIModelConfig.objects.filter(role='writer').update(model_usage='test_case_generation')


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0006_alter_requirementdocument_document_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='aimodelconfig',
            name='model_usage',
            field=models.CharField(
                choices=[
                    ('test_case_generation', '测试用例生成模型'),
                    ('test_case_review', '测试用例评审模型'),
                    ('requirement_analysis', '测试需求分析模型'),
                    ('general', '通用模型'),
                ],
                default='test_case_generation',
                max_length=30,
                verbose_name='模型用途',
            ),
        ),
        migrations.AddField(
            model_name='aimodelconfig',
            name='bound_prompt',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='bound_model_configs',
                to='requirement_analysis.promptconfig',
                verbose_name='绑定Prompt',
            ),
        ),
        migrations.AddField(
            model_name='aimodelconfig',
            name='max_output_tokens',
            field=models.IntegerField(default=8192, verbose_name='Max Output Tokens'),
        ),
        migrations.AddField(
            model_name='aimodelconfig',
            name='retry_count',
            field=models.PositiveSmallIntegerField(default=3, verbose_name='请求失败重试次数'),
        ),
        migrations.AddField(
            model_name='aimodelconfig',
            name='model_status',
            field=models.CharField(
                choices=[
                    ('normal', '正常'),
                    ('abnormal', '异常'),
                    ('slow', '响应慢'),
                ],
                default='normal',
                max_length=20,
                verbose_name='模型状态',
            ),
        ),
        migrations.RunPython(backfill_model_usage, migrations.RunPython.noop),
    ]
