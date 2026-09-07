import re
import unicodedata

from django.db import migrations, models


def normalize_title(title):
    normalized = unicodedata.normalize('NFKC', str(title or ''))
    return re.sub(r'\s+', ' ', normalized).strip()


def populate_title_keys_and_remove_duplicates(apps, schema_editor):
    TestCase = apps.get_model('testcases', 'TestCase')
    seen = set()

    # Keep the newest row for each project/title pair.  Sorting explicitly
    # makes the cleanup deterministic on databases with equal timestamps.
    rows = TestCase.objects.all().order_by('project_id', '-created_at', '-id')
    for testcase in rows:
        title = normalize_title(testcase.title)
        key = (testcase.project_id, title.casefold())
        if key in seen:
            testcase.delete()
            continue
        seen.add(key)
        TestCase.objects.filter(pk=testcase.pk).update(
            title=title,
            title_key=title.casefold(),
        )


class Migration(migrations.Migration):
    dependencies = [
        ('testcases', '0005_align_testcase_author_and_step_constraints'),
    ]

    operations = [
        migrations.AddField(
            model_name='testcase',
            name='title_key',
            field=models.CharField(
                default='',
                editable=False,
                max_length=500,
                verbose_name='用例标题唯一键',
            ),
        ),
        migrations.RunPython(
            populate_title_keys_and_remove_duplicates,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name='testcase',
            constraint=models.UniqueConstraint(
                fields=('project', 'title_key'),
                name='testcases_project_title_key_uniq',
            ),
        ),
    ]
