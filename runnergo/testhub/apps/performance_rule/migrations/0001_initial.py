# Generated during RunnerGo integration on 2026-07-31

from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('projects', '0005_align_project_owner_and_member_constraints'),
    ]

    operations = [
        migrations.CreateModel(
            name='PerformanceRule',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('max_p95', models.FloatField(blank=True, null=True, verbose_name='P95最大响应时间(ms)')),
                ('max_error_rate', models.FloatField(blank=True, null=True, verbose_name='最大错误率(%)')),
                ('min_tps', models.FloatField(blank=True, null=True, verbose_name='最小TPS')),
                ('max_cpu', models.FloatField(blank=True, null=True, verbose_name='最大CPU使用率(%)')),
                ('create_time', models.DateTimeField(default=django.utils.timezone.now, verbose_name='创建时间')),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='performance_rules', to='projects.project', verbose_name='项目')),
            ],
            options={
                'verbose_name': 'SLA性能规则',
                'verbose_name_plural': 'SLA性能规则',
                'db_table': 'performance_rule',
                'ordering': ['-create_time'],
            },
        ),
        migrations.CreateModel(
            name='PerformanceResult',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('report_id', models.CharField(blank=True, db_index=True, max_length=100, verbose_name='报告ID')),
                ('tps', models.FloatField(blank=True, null=True, verbose_name='TPS')),
                ('p95', models.FloatField(blank=True, null=True, verbose_name='P95(ms)')),
                ('error_rate', models.FloatField(blank=True, null=True, verbose_name='错误率(%)')),
                ('cpu', models.FloatField(blank=True, null=True, verbose_name='CPU使用率(%)')),
                ('overall_status', models.CharField(choices=[('PASS', '通过'), ('FAIL', '失败')], default='PASS', max_length=10, verbose_name='整体结果')),
                ('details', models.JSONField(default=list, verbose_name='逐项结果')),
                ('create_time', models.DateTimeField(default=django.utils.timezone.now, verbose_name='创建时间')),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='performance_results', to='projects.project', verbose_name='项目')),
                ('rule', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='results', to='performance_rule.performancerule', verbose_name='命中规则')),
            ],
            options={
                'verbose_name': 'SLA性能结果',
                'verbose_name_plural': 'SLA性能结果',
                'db_table': 'performance_result',
                'ordering': ['-create_time'],
            },
        ),
        migrations.AddIndex(
            model_name='performancerule',
            index=models.Index(fields=['project', '-create_time'], name='perf_rule_project_time_idx'),
        ),
        migrations.AddIndex(
            model_name='performanceresult',
            index=models.Index(fields=['project', '-create_time'], name='perf_result_project_time_idx'),
        ),
        migrations.AddIndex(
            model_name='performanceresult',
            index=models.Index(fields=['report_id'], name='perf_result_report_idx'),
        ),
    ]
