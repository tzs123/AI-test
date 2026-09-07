from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0008_aimodelconfig_capability_tags'),
    ]

    operations = [
        migrations.AddField(
            model_name='aimodelconfig',
            name='model_version',
            field=models.CharField(blank=True, default='', max_length=100, verbose_name='模型版本'),
        ),
    ]
