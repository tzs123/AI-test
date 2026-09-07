from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0013_expand_universal_component_library'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='apppackage',
            options={
                'ordering': ['name'],
                'verbose_name': 'APP应用标识',
                'verbose_name_plural': 'APP应用标识管理',
            },
        ),
    ]
