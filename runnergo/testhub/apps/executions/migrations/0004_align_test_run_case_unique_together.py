# Generated during RunnerGo integration on 2026-07-14

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("testcases", "0005_align_testcase_author_and_step_constraints"),
        ("executions", "0003_alter_testruncase_unique_together_and_more"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="testruncase",
            name="executions_testruncase_test_run_testcase_uniq",
        ),
        migrations.AlterUniqueTogether(
            name="testruncase",
            unique_together={("test_run", "testcase")},
        ),
    ]
