from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('performance_rule', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='performancerule',
            name='max_db_connection_usage',
            field=models.FloatField(blank=True, null=True, verbose_name='最大数据库连接使用率(%)'),
        ),
        migrations.AddField(
            model_name='performancerule',
            name='max_db_slow_queries_per_sec',
            field=models.FloatField(blank=True, null=True, verbose_name='最大慢查询速率(/s)'),
        ),
        migrations.AddField(
            model_name='performanceresult',
            name='db_connection_usage',
            field=models.FloatField(blank=True, null=True, verbose_name='数据库连接使用率(%)'),
        ),
        migrations.AddField(
            model_name='performanceresult',
            name='db_qps',
            field=models.FloatField(blank=True, null=True, verbose_name='数据库QPS'),
        ),
        migrations.AddField(
            model_name='performanceresult',
            name='db_slow_queries_per_sec',
            field=models.FloatField(blank=True, null=True, verbose_name='慢查询速率(/s)'),
        ),
        migrations.AddField(
            model_name='performanceresult',
            name='db_row_lock_waits_per_sec',
            field=models.FloatField(blank=True, null=True, verbose_name='行锁等待速率(/s)'),
        ),
    ]
