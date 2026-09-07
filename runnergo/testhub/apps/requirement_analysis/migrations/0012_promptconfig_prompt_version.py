from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0011_update_ai_model_sampling_defaults'),
    ]

    operations = [
        migrations.AddField(
            model_name='promptconfig',
            name='prompt_version',
            field=models.CharField(default='V1.0', max_length=20, verbose_name='Prompt版本'),
        ),
    ]
