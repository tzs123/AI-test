# Generated during RunnerGo integration on 2026-07-14

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_project_owner(apps, schema_editor):
    Project = apps.get_model("projects", "Project")
    if not Project.objects.filter(owner__isnull=True).exists():
        return

    User = apps.get_model("users", "User")
    fallback_user = (
        User.objects.filter(is_superuser=True).order_by("id").first()
        or User.objects.order_by("id").first()
    )
    if fallback_user is None:
        fallback_user = User.objects.create(
            username="migration_owner",
            email="migration-owner@example.invalid",
            password="!",
            is_active=True,
        )

    Project.objects.filter(owner__isnull=True).update(owner=fallback_user)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("projects", "0004_alter_projectmember_unique_together_and_more"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="projectmember",
            name="projects_projectmember_project_user_uniq",
        ),
        migrations.RunPython(backfill_project_owner, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="project",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="owned_projects",
                to=settings.AUTH_USER_MODEL,
                verbose_name="负责人",
            ),
        ),
        migrations.AlterUniqueTogether(
            name="projectmember",
            unique_together={("project", "user")},
        ),
    ]
