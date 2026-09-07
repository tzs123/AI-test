from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0016_testcasegenerationtask_monotonic_review'),
    ]

    operations = [
        migrations.AddField(
            model_name='businessruleknowledge',
            name='attribute_name',
            field=models.CharField(blank=True, max_length=100, verbose_name='属性/维度'),
        ),
        migrations.AddField(
            model_name='businessruleknowledge',
            name='business_domain',
            field=models.CharField(blank=True, max_length=100, verbose_name='业务域'),
        ),
        migrations.AddField(
            model_name='businessruleknowledge',
            name='entity_name',
            field=models.CharField(blank=True, max_length=100, verbose_name='业务实体'),
        ),
        migrations.AddField(
            model_name='businessruleknowledge',
            name='relation_nodes',
            field=models.JSONField(blank=True, default=list, verbose_name='关联节点'),
        ),
        migrations.AddField(
            model_name='businessruleknowledge',
            name='risk_level',
            field=models.CharField(choices=[('low', '低风险'), ('medium', '中风险'), ('high', '高风险'), ('critical', '关键风险')], default='medium', max_length=20, verbose_name='业务风险等级'),
        ),
        migrations.AddField(
            model_name='businessruleknowledge',
            name='state_values',
            field=models.JSONField(blank=True, default=list, verbose_name='状态/枚举值'),
        ),
        migrations.AddField(
            model_name='businessruleknowledge',
            name='test_strategies',
            field=models.JSONField(blank=True, default=list, verbose_name='测试生成策略'),
        ),
        migrations.AddIndex(
            model_name='businessruleknowledge',
            index=models.Index(fields=['business_domain', 'entity_name'], name='brk_domain_entity_idx'),
        ),
    ]
