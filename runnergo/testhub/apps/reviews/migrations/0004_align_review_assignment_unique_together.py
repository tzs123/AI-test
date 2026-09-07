# Generated during RunnerGo integration on 2026-07-14

from django.conf import settings
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("reviews", "0003_alter_reviewassignment_unique_together_and_more"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="reviewassignment",
            name="reviews_reviewassignment_review_reviewer_uniq",
        ),
        migrations.AlterUniqueTogether(
            name="reviewassignment",
            unique_together={("review", "reviewer")},
        ),
    ]
