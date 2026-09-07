from django.db import migrations, models


def backfill_monotonic_review_baseline(apps, schema_editor):
    task_model = apps.get_model('requirement_analysis', 'TestCaseGenerationTask')

    for task in task_model.objects.all().iterator():
        history = list(task.review_history or [])
        scored_entries = [
            entry for entry in history
            if isinstance(entry.get('score'), int)
        ]
        scores = [entry['score'] for entry in scored_entries]
        if isinstance(task.review_score, int):
            scores.append(task.review_score)
        if not scores:
            continue

        best_score = max(scores)
        running_best = -1
        best_feedback = ''
        normalized_history = []
        for entry in history:
            normalized = dict(entry)
            score = normalized.get('score')
            if isinstance(score, int):
                accepted = score >= running_best
                if accepted:
                    running_best = score
                    if score == best_score and normalized.get('feedback'):
                        best_feedback = normalized['feedback']
                else:
                    normalized['source'] = 'rejected_regression'
                    normalized['baseline_score'] = running_best
                normalized['accepted'] = accepted
            normalized_history.append(normalized)

        current_is_best = task.review_score == best_score and not task.review_pending
        if current_is_best:
            best_cases = task.final_test_cases or task.generated_test_cases
            best_feedback = task.review_feedback or best_feedback
        else:
            # 旧版未保存每轮用例快照；首轮评审的 generated_test_cases 是可回滚的最稳定基线。
            best_cases = task.generated_test_cases or task.final_test_cases

        task.best_review_score = best_score
        task.best_test_cases = best_cases
        task.final_test_cases = best_cases
        task.review_score = best_score
        task.review_feedback = best_feedback or task.review_feedback
        task.review_pending = False
        task.pending_base_test_cases = ''
        task.review_history = normalized_history
        task.save(update_fields=[
            'best_review_score',
            'best_test_cases',
            'final_test_cases',
            'review_score',
            'review_feedback',
            'review_pending',
            'pending_base_test_cases',
            'review_history',
        ])


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0015_testcasegenerationtask_review_loop'),
    ]

    operations = [
        migrations.AddField(
            model_name='testcasegenerationtask',
            name='best_review_score',
            field=models.PositiveSmallIntegerField(blank=True, null=True, verbose_name='历史最高AI评审分数'),
        ),
        migrations.AddField(
            model_name='testcasegenerationtask',
            name='best_test_cases',
            field=models.TextField(blank=True, verbose_name='历史最佳用例快照'),
        ),
        migrations.AddField(
            model_name='testcasegenerationtask',
            name='pending_base_test_cases',
            field=models.TextField(blank=True, verbose_name='待复评优化的基线快照'),
        ),
        migrations.RunPython(backfill_monotonic_review_baseline, migrations.RunPython.noop),
    ]
