import re

from django.db import migrations, models


def backfill_review_state(apps, schema_editor):
    task_model = apps.get_model('requirement_analysis', 'TestCaseGenerationTask')
    pattern = re.compile(
        r'(?:AI评分|综合评分|总评分|评审分数|质量评分|评分)[^\d]{0,16}(\d{1,3})(?:\s*/\s*100)?',
        flags=re.IGNORECASE,
    )
    for task in task_model.objects.exclude(review_feedback='').iterator():
        matches = pattern.findall(task.review_feedback or '')
        if not matches:
            continue
        score = max(0, min(100, int(matches[-1])))
        task.review_score = score
        task.review_round = 1
        task.review_history = [{
            'round': 1,
            'score': score,
            'source': 'migration',
            'feedback': task.review_feedback,
            'reviewed_at': task.updated_at.isoformat() if task.updated_at else None,
        }]
        task.save(update_fields=['review_score', 'review_round', 'review_history'])


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0014_business_rule_knowledge'),
    ]

    operations = [
        migrations.AddField(
            model_name='testcasegenerationtask',
            name='review_history',
            field=models.JSONField(blank=True, default=list, verbose_name='评审历史'),
        ),
        migrations.AddField(
            model_name='testcasegenerationtask',
            name='review_pending',
            field=models.BooleanField(default=False, verbose_name='优化后待复评'),
        ),
        migrations.AddField(
            model_name='testcasegenerationtask',
            name='review_round',
            field=models.PositiveIntegerField(default=0, verbose_name='评审轮次'),
        ),
        migrations.AddField(
            model_name='testcasegenerationtask',
            name='review_score',
            field=models.PositiveSmallIntegerField(blank=True, null=True, verbose_name='最新AI评审分数'),
        ),
        migrations.RunPython(backfill_review_state, migrations.RunPython.noop),
    ]
