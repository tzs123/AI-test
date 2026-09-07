from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0019_app_element_folders'),
    ]

    operations = [
        migrations.AlterField(
            model_name='appelement',
            name='name',
            field=models.CharField(
                help_text='元素在所属文件夹内的唯一标识名称',
                max_length=200,
                verbose_name='元素名称',
            ),
        ),
        migrations.AddConstraint(
            model_name='appelement',
            constraint=models.UniqueConstraint(
                fields=('folder', 'name'),
                name='app_element_folder_name_uniq',
            ),
        ),
    ]
