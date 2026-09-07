from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('projects', '0005_align_project_owner_and_member_constraints'),
        ('requirement_analysis', '0021_merge_duplicate_domain_nodes'),
    ]

    operations = [
        migrations.CreateModel(
            name='BusinessWorkflow',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200, verbose_name='工作流名称')),
                ('identifier', models.CharField(blank=True, max_length=120, verbose_name='工作流标识')),
                ('category', models.CharField(blank=True, max_length=100, verbose_name='业务分类')),
                ('version', models.PositiveIntegerField(default=1, verbose_name='版本')),
                ('description', models.TextField(blank=True, verbose_name='工作流说明')),
                ('definition', models.JSONField(blank=True, default=dict, verbose_name='工作流定义')),
                ('source_file', models.FileField(blank=True, null=True, upload_to='business_workflows/%Y/%m/', verbose_name='导入源文件')),
                ('source_text', models.TextField(blank=True, verbose_name='导入源文本')),
                ('status', models.CharField(choices=[('draft', '草稿'), ('active', '启用'), ('archived', '已归档')], default='active', max_length=20, verbose_name='状态')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
                ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='created_business_workflows', to=settings.AUTH_USER_MODEL, verbose_name='创建人')),
                ('project', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='business_workflows', to='projects.project', verbose_name='所属项目')),
            ],
            options={
                'verbose_name': '业务工作流',
                'verbose_name_plural': '业务工作流',
                'db_table': 'business_workflow',
                'ordering': ['-updated_at'],
            },
        ),
        migrations.AddIndex(
            model_name='businessworkflow',
            index=models.Index(fields=['status', 'project'], name='bwf_status_project_idx'),
        ),
        migrations.AddIndex(
            model_name='businessworkflow',
            index=models.Index(fields=['category'], name='bwf_category_idx'),
        ),
        migrations.AddIndex(
            model_name='businessworkflow',
            index=models.Index(fields=['identifier'], name='bwf_identifier_idx'),
        ),
    ]
