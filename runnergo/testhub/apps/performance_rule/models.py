from django.db import models
from django.utils import timezone

from apps.projects.models import Project


class PerformanceRule(models.Model):
    """项目级 SLA 性能规则。"""

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='performance_rules',
        verbose_name='项目'
    )
    max_p95 = models.FloatField(null=True, blank=True, verbose_name='P95最大响应时间(ms)')
    max_error_rate = models.FloatField(null=True, blank=True, verbose_name='最大错误率(%)')
    min_tps = models.FloatField(null=True, blank=True, verbose_name='最小TPS')
    max_cpu = models.FloatField(null=True, blank=True, verbose_name='最大CPU使用率(%)')
    max_db_connection_usage = models.FloatField(null=True, blank=True, verbose_name='最大数据库连接使用率(%)')
    max_db_slow_queries_per_sec = models.FloatField(null=True, blank=True, verbose_name='最大慢查询速率(/s)')
    create_time = models.DateTimeField(default=timezone.now, verbose_name='创建时间')

    class Meta:
        db_table = 'performance_rule'
        verbose_name = 'SLA性能规则'
        verbose_name_plural = 'SLA性能规则'
        ordering = ['-create_time']
        indexes = [
            models.Index(fields=['project', '-create_time'], name='perf_rule_project_time_idx'),
        ]

    def __str__(self):
        return f'{self.project_id} performance rule'


class PerformanceResult(models.Model):
    """测试完成后的 SLA 评价结果。"""

    STATUS_PASS = 'PASS'
    STATUS_FAIL = 'FAIL'
    STATUS_CHOICES = [
        (STATUS_PASS, '通过'),
        (STATUS_FAIL, '失败'),
    ]

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='performance_results',
        verbose_name='项目'
    )
    rule = models.ForeignKey(
        PerformanceRule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='results',
        verbose_name='命中规则'
    )
    report_id = models.CharField(max_length=100, blank=True, db_index=True, verbose_name='报告ID')
    tps = models.FloatField(null=True, blank=True, verbose_name='TPS')
    p95 = models.FloatField(null=True, blank=True, verbose_name='P95(ms)')
    error_rate = models.FloatField(null=True, blank=True, verbose_name='错误率(%)')
    cpu = models.FloatField(null=True, blank=True, verbose_name='CPU使用率(%)')
    db_connection_usage = models.FloatField(null=True, blank=True, verbose_name='数据库连接使用率(%)')
    db_qps = models.FloatField(null=True, blank=True, verbose_name='数据库QPS')
    db_slow_queries_per_sec = models.FloatField(null=True, blank=True, verbose_name='慢查询速率(/s)')
    db_row_lock_waits_per_sec = models.FloatField(null=True, blank=True, verbose_name='行锁等待速率(/s)')
    overall_status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default=STATUS_PASS,
        verbose_name='整体结果'
    )
    details = models.JSONField(default=list, verbose_name='逐项结果')
    create_time = models.DateTimeField(default=timezone.now, verbose_name='创建时间')

    class Meta:
        db_table = 'performance_result'
        verbose_name = 'SLA性能结果'
        verbose_name_plural = 'SLA性能结果'
        ordering = ['-create_time']
        indexes = [
            models.Index(fields=['project', '-create_time'], name='perf_result_project_time_idx'),
            models.Index(fields=['report_id'], name='perf_result_report_idx'),
        ]

    def __str__(self):
        return f'{self.report_id or self.id} {self.overall_status}'
