from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0031_add_press_enter_component'),
    ]

    operations = [
        migrations.AddField(
            model_name='apptestexecution',
            name='skipped_steps',
            field=models.IntegerField(default=0, verbose_name='跳过步骤数'),
        ),
    ]
