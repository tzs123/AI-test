# Generated during RunnerGo integration on 2026-07-14

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("app_automation", "0003_alter_apptestsuitecase_unique_together_and_more"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="apptestsuitecase",
            name="app_automation_apptestsuitecase_test_suite_test_case_uniq",
        ),
        migrations.AlterUniqueTogether(
            name="apptestsuitecase",
            unique_together={("test_suite", "test_case")},
        ),
    ]
