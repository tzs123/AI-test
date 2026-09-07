from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0006_alter_apptestsuite_execution_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='apptestconfig',
            name='ios_wda_bundle_id',
            field=models.CharField(blank=True, default='', help_text='自定义签名后的 WebDriverAgentRunner Bundle ID，可选', max_length=255, verbose_name='WDA Bundle ID'),
        ),
        migrations.AddField(
            model_name='apptestconfig',
            name='ios_wda_url',
            field=models.CharField(default='http://host.docker.internal:8100', help_text='Mac 主机上 WebDriverAgent 的访问地址', max_length=500, verbose_name='iOS WDA地址'),
        ),
        migrations.AddField(
            model_name='appdevice',
            name='default_bundle_id',
            field=models.CharField(blank=True, default='', help_text='Android 包名或 iOS Bundle ID', max_length=255, verbose_name='默认应用标识'),
        ),
        migrations.AddField(
            model_name='appdevice',
            name='ios_version',
            field=models.CharField(blank=True, default='', max_length=50, verbose_name='iOS版本'),
        ),
        migrations.AddField(
            model_name='appdevice',
            name='platform',
            field=models.CharField(choices=[('android', 'Android'), ('ios', 'iOS')], db_index=True, default='android', max_length=20, verbose_name='设备平台'),
        ),
        migrations.AddField(
            model_name='appdevice',
            name='wda_bundle_id',
            field=models.CharField(blank=True, default='', max_length=255, verbose_name='WDA Bundle ID'),
        ),
        migrations.AddField(
            model_name='appdevice',
            name='wda_url',
            field=models.CharField(blank=True, default='', help_text='容器可访问的 WebDriverAgent 地址，例如 http://host.docker.internal:8100', max_length=500, verbose_name='WDA地址'),
        ),
        migrations.AlterField(
            model_name='appdevice',
            name='connection_type',
            field=models.CharField(choices=[('emulator', '本地模拟器'), ('remote_emulator', '远程模拟器'), ('usb', 'USB连接'), ('remote', '远程设备'), ('ios_remote', 'iOS远程WDA'), ('real_device', '真实设备')], default='emulator', max_length=20, verbose_name='连接类型'),
        ),
        migrations.AlterField(
            model_name='apppackage',
            name='name',
            field=models.CharField(help_text='友好的应用名称，如：系统设置', max_length=100, verbose_name='应用名称'),
        ),
        migrations.AlterField(
            model_name='apppackage',
            name='package_name',
            field=models.CharField(help_text='Android 包名或 iOS Bundle ID，如：com.example.app', max_length=255, unique=True, verbose_name='应用标识'),
        ),
    ]
