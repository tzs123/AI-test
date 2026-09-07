from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('core', '0004_testdataasset_testdataassetrequirement_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='TestDataDecisionLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('target_type', models.CharField(db_index=True, max_length=30, verbose_name='目标类型')),
                ('target_case_id', models.PositiveIntegerField(db_index=True, verbose_name='目标用例ID')),
                ('target_case_name', models.CharField(blank=True, default='', max_length=500, verbose_name='目标用例名称')),
                ('execution_type', models.CharField(blank=True, default='', max_length=50, verbose_name='执行类型')),
                ('execution_id', models.CharField(blank=True, db_index=True, default='', max_length=100, verbose_name='执行ID')),
                ('field_key', models.CharField(blank=True, default='', max_length=200, verbose_name='字段Key')),
                ('field_label', models.CharField(blank=True, default='', max_length=500, verbose_name='字段名称')),
                ('field_type', models.CharField(blank=True, default='', max_length=50, verbose_name='识别类型')),
                ('alias', models.CharField(blank=True, default='', max_length=100, verbose_name='上下文别名')),
                ('source', models.CharField(choices=[('asset_pool', '资产池'), ('rule_generation', '规则生成'), ('skipped', '跳过')], db_index=True, max_length=30, verbose_name='数据来源')),
                ('strategy', models.CharField(blank=True, default='', max_length=50, verbose_name='决策策略')),
                ('runtime_context_path', models.CharField(blank=True, default='', max_length=200, verbose_name='Runtime Context路径')),
                ('decision', models.JSONField(blank=True, default=dict, verbose_name='决策详情')),
                ('value_snapshot', models.JSONField(blank=True, default=dict, verbose_name='值快照')),
                ('status', models.CharField(choices=[('prepared', '已准备'), ('released', '已释放'), ('used', '已使用'), ('expired', '已过期'), ('repaired', '已修复')], db_index=True, default='prepared', max_length=20, verbose_name='状态')),
                ('failure_info', models.JSONField(blank=True, default=dict, verbose_name='失败信息')),
                ('repair_result', models.JSONField(blank=True, default=dict, verbose_name='修复结果')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
                ('asset', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='decision_logs', to='core.testdataasset')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='test_data_decision_logs', to=settings.AUTH_USER_MODEL)),
                ('lease', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='decision_logs', to='core.testdataassetlease')),
            ],
            options={
                'verbose_name': '测试数据决策日志',
                'verbose_name_plural': '测试数据决策日志',
                'db_table': 'test_data_decision_logs',
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['execution_type', 'execution_id'], name='test_data_d_executi_788054_idx'),
                    models.Index(fields=['target_type', 'target_case_id'], name='test_data_d_target__7abc67_idx'),
                    models.Index(fields=['source', 'status'], name='test_data_d_source_b27c23_idx'),
                ],
            },
        ),
    ]
