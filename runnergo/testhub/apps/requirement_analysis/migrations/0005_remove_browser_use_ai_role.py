from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0004_align_unique_together_constraints'),
    ]

    operations = [
        migrations.AlterField(
            model_name='aimodelconfig',
            name='role',
            field=models.CharField(
                choices=[
                    ('writer', '测试用例编写专家'),
                    ('reviewer', '测试评审专家'),
                ],
                max_length=20,
                verbose_name='角色',
            ),
        ),
    ]
