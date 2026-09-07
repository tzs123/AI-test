from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0026_test_skills'),
    ]

    operations = [
        migrations.AddField(
            model_name='testcasegenerationtask',
            name='skill_execution_results',
            field=models.JSONField(blank=True, default=list, verbose_name='Skill串行执行结果'),
        ),
    ]
