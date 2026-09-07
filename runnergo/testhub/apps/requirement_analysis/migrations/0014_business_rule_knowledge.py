from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0013_testcasegenerationtask_case_type_rules'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('projects', '0002_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='testcasegenerationtask',
            name='knowledge_rule_context',
            field=models.JSONField(blank=True, default=list, verbose_name='本次生成采用的业务规则快照'),
        ),
        migrations.CreateModel(
            name='BusinessRuleKnowledge',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=200, verbose_name='规则名称')),
                ('content', models.TextField(verbose_name='规则内容')),
                ('keywords', models.JSONField(blank=True, default=list, verbose_name='关键词')),
                ('module', models.CharField(blank=True, max_length=100, verbose_name='业务模块')),
                ('rule_type', models.CharField(choices=[('business', '业务约束'), ('field_validation', '字段校验'), ('state_transition', '状态流转'), ('permission', '权限控制'), ('amount', '金额规则'), ('time', '时间规则'), ('interface', '接口规则'), ('exception', '异常处理'), ('security', '安全规则'), ('other', '其他')], default='business', max_length=30, verbose_name='规则类型')),
                ('applicable_conditions', models.TextField(blank=True, verbose_name='适用条件')),
                ('expected_behavior', models.TextField(blank=True, verbose_name='预期行为')),
                ('exceptions', models.TextField(blank=True, verbose_name='例外情况')),
                ('source_requirement', models.CharField(blank=True, max_length=300, verbose_name='来源需求')),
                ('status', models.CharField(choices=[('draft', '草稿'), ('approved', '已审核'), ('deprecated', '已废弃')], default='approved', max_length=20, verbose_name='状态')),
                ('version', models.PositiveIntegerField(default=1, verbose_name='版本')),
                ('usage_count', models.PositiveIntegerField(default=0, verbose_name='推荐次数')),
                ('accepted_count', models.PositiveIntegerField(default=0, verbose_name='采纳次数')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
                ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='created_business_rules', to=settings.AUTH_USER_MODEL, verbose_name='创建人')),
                ('project', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='business_rule_knowledge', to='projects.project', verbose_name='所属项目')),
                ('reviewed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='reviewed_business_rules', to=settings.AUTH_USER_MODEL, verbose_name='审核人')),
                ('source_task', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='derived_business_rules', to='requirement_analysis.testcasegenerationtask', verbose_name='来源生成任务')),
            ],
            options={
                'verbose_name': '业务规则知识',
                'verbose_name_plural': '业务规则知识库',
                'db_table': 'business_rule_knowledge',
                'ordering': ['-updated_at'],
            },
        ),
        migrations.CreateModel(
            name='BusinessRuleUsage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('action', models.CharField(choices=[('recommended', '已推荐'), ('selected', '已采纳'), ('rejected', '已忽略')], default='selected', max_length=20, verbose_name='使用动作')),
                ('similarity_score', models.FloatField(default=0, verbose_name='推荐相似度')),
                ('rule_snapshot', models.JSONField(blank=True, default=dict, verbose_name='规则快照')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('rule', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='usage_records', to='requirement_analysis.businessruleknowledge', verbose_name='业务规则')),
                ('task', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='knowledge_rule_usages', to='requirement_analysis.testcasegenerationtask', verbose_name='生成任务')),
            ],
            options={
                'verbose_name': '业务规则使用记录',
                'verbose_name_plural': '业务规则使用记录',
                'db_table': 'business_rule_usage',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='businessruleknowledge',
            index=models.Index(fields=['status', 'project'], name='brk_status_project_idx'),
        ),
        migrations.AddIndex(
            model_name='businessruleknowledge',
            index=models.Index(fields=['rule_type', 'module'], name='brk_type_module_idx'),
        ),
        migrations.AddConstraint(
            model_name='businessruleusage',
            constraint=models.UniqueConstraint(fields=('rule', 'task'), name='unique_rule_task_usage'),
        ),
    ]
