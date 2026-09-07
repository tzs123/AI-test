from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('core', '0002_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='RuntimeDataTemplate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200, verbose_name='模板名称')),
                ('description', models.TextField(blank=True, default='', verbose_name='模板描述')),
                ('target_type', models.CharField(choices=[('ui_automation', 'UI自动化'), ('app_automation', 'APP自动化'), ('api_automation', '接口自动化')], db_index=True, max_length=30, verbose_name='目标类型')),
                ('target_case_id', models.PositiveIntegerField(db_index=True, verbose_name='目标用例ID')),
                ('target_case_name', models.CharField(blank=True, default='', max_length=500, verbose_name='目标用例名称')),
                ('fields', models.JSONField(blank=True, default=list, verbose_name='字段快照')),
                ('runtime_override', models.JSONField(blank=True, default=list, verbose_name='执行数据覆盖')),
                ('runtime_context', models.JSONField(blank=True, default=dict, verbose_name='运行上下文默认值')),
                ('cleanup_config', models.JSONField(blank=True, default=dict, verbose_name='执行后清理配置')),
                ('is_active', models.BooleanField(default=True, verbose_name='是否启用')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='runtime_data_templates', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': '运行态数据模板',
                'verbose_name_plural': '运行态数据模板',
                'db_table': 'runtime_data_templates',
                'ordering': ['-updated_at'],
                'indexes': [models.Index(fields=['target_type', 'target_case_id'], name='runtime_dat_target__a2cf6a_idx'), models.Index(fields=['created_by'], name='runtime_dat_created_cfe179_idx')],
            },
        ),
        migrations.CreateModel(
            name='RuntimeDataSet',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200, verbose_name='数据集名称')),
                ('description', models.TextField(blank=True, default='', verbose_name='数据集描述')),
                ('target_type', models.CharField(choices=[('ui_automation', 'UI自动化'), ('app_automation', 'APP自动化'), ('api_automation', '接口自动化')], db_index=True, max_length=30, verbose_name='目标类型')),
                ('target_case_id', models.PositiveIntegerField(db_index=True, verbose_name='目标用例ID')),
                ('target_case_name', models.CharField(blank=True, default='', max_length=500, verbose_name='目标用例名称')),
                ('columns', models.JSONField(blank=True, default=list, verbose_name='数据列')),
                ('rows', models.JSONField(blank=True, default=list, verbose_name='数据行')),
                ('row_count', models.PositiveIntegerField(default=0, verbose_name='数据行数')),
                ('source_filename', models.CharField(blank=True, default='', max_length=255, verbose_name='来源文件名')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='runtime_data_sets', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': '运行态数据集',
                'verbose_name_plural': '运行态数据集',
                'db_table': 'runtime_data_sets',
                'ordering': ['-updated_at'],
                'indexes': [models.Index(fields=['target_type', 'target_case_id'], name='runtime_dat_target__f268d0_idx'), models.Index(fields=['created_by'], name='runtime_dat_created_d22912_idx')],
            },
        ),
        migrations.CreateModel(
            name='RuntimeExecutionSnapshot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('target_type', models.CharField(choices=[('ui_automation', 'UI自动化'), ('app_automation', 'APP自动化'), ('api_automation', '接口自动化')], db_index=True, max_length=30, verbose_name='目标类型')),
                ('target_case_id', models.PositiveIntegerField(db_index=True, verbose_name='目标用例ID')),
                ('target_case_name', models.CharField(blank=True, default='', max_length=500, verbose_name='目标用例名称')),
                ('execution_type', models.CharField(blank=True, default='', max_length=50, verbose_name='执行类型')),
                ('execution_id', models.CharField(blank=True, db_index=True, default='', max_length=100, verbose_name='执行ID')),
                ('dataset_row_index', models.IntegerField(blank=True, null=True, verbose_name='数据集行号')),
                ('runtime_override', models.JSONField(blank=True, default=list, verbose_name='运行覆盖')),
                ('runtime_data', models.JSONField(blank=True, default=list, verbose_name='实际执行数据')),
                ('runtime_context', models.JSONField(blank=True, default=dict, verbose_name='运行上下文')),
                ('cleanup_config', models.JSONField(blank=True, default=dict, verbose_name='清理配置')),
                ('cleanup_result', models.JSONField(blank=True, default=dict, verbose_name='清理结果')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='runtime_execution_snapshots', to=settings.AUTH_USER_MODEL)),
                ('dataset', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='snapshots', to='core.runtimedataset')),
                ('template', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='snapshots', to='core.runtimedatatemplate')),
            ],
            options={
                'verbose_name': '运行态执行数据快照',
                'verbose_name_plural': '运行态执行数据快照',
                'db_table': 'runtime_execution_snapshots',
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['target_type', 'target_case_id'], name='runtime_exe_target__da7d87_idx'), models.Index(fields=['execution_type', 'execution_id'], name='runtime_exe_executi_584944_idx')],
            },
        ),
    ]
