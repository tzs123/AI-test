# Generated during RunnerGo integration on 2026-07-14

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("requirement_analysis", "0003_alter_businessrequirement_unique_together_and_more"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="businessrequirement",
            name="requirement_analysis_businessrequirement_analysis_requiremen",
        ),
        migrations.RemoveConstraint(
            model_name="generatedtestcase",
            name="requirement_analysis_generatedtestcase_requirement_case_id_u",
        ),
        migrations.AlterUniqueTogether(
            name="businessrequirement",
            unique_together={("analysis", "requirement_id")},
        ),
        migrations.AlterUniqueTogether(
            name="generatedtestcase",
            unique_together={("requirement", "case_id")},
        ),
    ]
