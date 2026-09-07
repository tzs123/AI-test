# Generated manually for local APP automation runtime settings.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0004_align_suite_case_unique_together'),
    ]

    operations = [
        migrations.AddField(
            model_name='apptestconfig',
            name='node_path',
            field=models.CharField(default='node', help_text='Node.js 命令路径，默认为 node（系统PATH）', max_length=500, verbose_name='Node.js路径'),
        ),
        migrations.AddField(
            model_name='apptestconfig',
            name='appium_path',
            field=models.CharField(default='appium', help_text='Appium CLI 命令路径，默认为 appium（系统PATH）', max_length=500, verbose_name='Appium路径'),
        ),
        migrations.AddField(
            model_name='apptestconfig',
            name='appium_server_url',
            field=models.CharField(default='http://127.0.0.1:4723', help_text='Appium Server 地址', max_length=500, verbose_name='Appium服务地址'),
        ),
        migrations.AddField(
            model_name='apptestconfig',
            name='appium_driver',
            field=models.CharField(default='uiautomator2', help_text='Android Appium 驱动名称', max_length=100, verbose_name='Appium驱动'),
        ),
        migrations.AddField(
            model_name='apptestconfig',
            name='appium_automation_name',
            field=models.CharField(default='UiAutomator2', help_text='Android 自动化引擎名称', max_length=100, verbose_name='AutomationName'),
        ),
    ]
