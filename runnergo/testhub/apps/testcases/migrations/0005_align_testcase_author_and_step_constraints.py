# Generated during RunnerGo integration on 2026-07-14

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_testcase_author(apps, schema_editor):
    TestCase = apps.get_model("testcases", "TestCase")
    if not TestCase.objects.filter(author__isnull=True).exists():
        return

    User = apps.get_model("users", "User")
    fallback_user = (
        User.objects.filter(is_superuser=True).order_by("id").first()
        or User.objects.order_by("id").first()
    )
    if fallback_user is None:
        fallback_user = User.objects.create(
            username="migration_author",
            email="migration-author@example.invalid",
            password="!",
            is_active=True,
        )

    TestCase.objects.filter(author__isnull=True).update(author=fallback_user)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("testcases", "0004_alter_testcasestep_unique_together_and_more"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="testcasestep",
            name="testcases_testcasestep_testcase_step_number_uniq",
        ),
        migrations.RunPython(backfill_testcase_author, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="testcase",
            name="author",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="authored_testcases",
                to=settings.AUTH_USER_MODEL,
                verbose_name="作者",
            ),
        ),
        migrations.AlterUniqueTogether(
            name="testcasestep",
            unique_together={("testcase", "step_number")},
        ),
    ]
