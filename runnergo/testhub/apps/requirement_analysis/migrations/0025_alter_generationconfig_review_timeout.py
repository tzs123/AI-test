from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('requirement_analysis', '0024_encrypt_ai_model_api_keys'),
    ]

    operations = [
        migrations.AlterField(
            model_name='generationconfig',
            name='review_timeout',
            field=models.IntegerField(
                default=60,
                help_text='AI评审和改进的最大等待时间（总时长）',
                verbose_name='评审和改进超时时间（秒）',
            ),
        ),
    ]
