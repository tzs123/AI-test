from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0007_ios_device_support'),
    ]

    operations = [
        migrations.AddField(
            model_name='apptestconfig',
            name='device_bridge_url',
            field=models.CharField(default='http://host.docker.internal:8765', help_text='运行在 Mac 宿主机上的 RunnerGo 设备扫描服务', max_length=500, verbose_name='设备桥接服务地址'),
        ),
    ]
