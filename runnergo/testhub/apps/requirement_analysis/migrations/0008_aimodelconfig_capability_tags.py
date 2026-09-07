from django.db import migrations, models


GENERATION_CAPABILITY_TAGS = ['需求分析', '测试设计', '用例生成', '边界分析', '安全测试']
REVIEW_CAPABILITY_TAGS = ['用例审查', '覆盖率分析', '缺陷发现', '质量评分']


def backfill_capability_tags(apps, schema_editor):
    AIModelConfig = apps.get_model('requirement_analysis', 'AIModelConfig')
    for config in AIModelConfig.objects.all():
        if config.capability_tags:
            continue
        if config.model_usage == 'test_case_review' or config.role == 'reviewer':
            config.capability_tags = REVIEW_CAPABILITY_TAGS
        else:
            config.capability_tags = GENERATION_CAPABILITY_TAGS
        config.save(update_fields=['capability_tags'])


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0007_ai_model_config_usage_prompt_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='aimodelconfig',
            name='capability_tags',
            field=models.JSONField(blank=True, default=list, verbose_name='能力标签'),
        ),
        migrations.RunPython(backfill_capability_tags, migrations.RunPython.noop),
    ]
