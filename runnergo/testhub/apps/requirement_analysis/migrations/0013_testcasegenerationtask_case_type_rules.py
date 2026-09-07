from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0012_promptconfig_prompt_version'),
    ]

    operations = [
        migrations.AddField(
            model_name='testcasegenerationtask',
            name='case_type_rules',
            field=models.JSONField(blank=True, default=dict, verbose_name='用例类型配额规则'),
        ),
    ]
