# Generated during RunnerGo integration on 2026-07-14

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("testcases", "0005_align_testcase_author_and_step_constraints"),
        ("testsuites", "0004_alter_testsuitecase_unique_together_and_more"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="testsuitecase",
            name="testsuites_testsuitecase_testsuite_testcase_uniq",
        ),
        migrations.AlterField(
            model_name="testsuitecase",
            name="testcase",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                to="testcases.testcase",
            ),
        ),
        migrations.AlterField(
            model_name="testsuitecase",
            name="testsuite",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                to="testsuites.testsuite",
            ),
        ),
        migrations.AlterUniqueTogether(
            name="testsuitecase",
            unique_together={("testsuite", "testcase")},
        ),
    ]
