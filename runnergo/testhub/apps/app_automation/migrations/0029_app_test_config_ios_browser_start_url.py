from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0028_execution_runtime_orchestration'),
    ]

    operations = [
        migrations.AddField(
            model_name='apptestconfig',
            name='ios_browser_start_url',
            field=models.CharField(
                blank=True,
                default='',
                help_text='iOS Safari/H5 用例无应用包名时，执行前自动打开的起始页面 URL，可选',
                max_length=500,
                verbose_name='iOS 浏览器起始URL',
            ),
        ),
    ]
