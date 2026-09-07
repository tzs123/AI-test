from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0010_generation_mode_document_types'),
    ]

    operations = [
        migrations.AlterField(
            model_name='aimodelconfig',
            name='temperature',
            field=models.FloatField(default=0.4, verbose_name='温度参数'),
        ),
        migrations.AlterField(
            model_name='aimodelconfig',
            name='top_p',
            field=models.FloatField(default=0.8, verbose_name='Top P参数'),
        ),
    ]
