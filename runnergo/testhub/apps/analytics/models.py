from django.conf import settings
from django.db import models

from apps.users.models import User


class AnalyticsEvent(models.Model):
    event_name = models.CharField(max_length=100, verbose_name='事件名称')
    event_type = models.CharField(max_length=32, default='custom', verbose_name='事件类型')
    module = models.CharField(max_length=64, blank=True, default='', verbose_name='所属模块')
    page_path = models.CharField(max_length=255, blank=True, default='', verbose_name='页面路径')
    route_name = models.CharField(max_length=100, blank=True, default='', verbose_name='路由名称')
    referrer_path = models.CharField(max_length=255, blank=True, default='', verbose_name='来源页面')
    target_path = models.CharField(max_length=255, blank=True, default='', verbose_name='目标页面')
    success = models.BooleanField(null=True, blank=True, verbose_name='是否成功')
    duration_ms = models.PositiveIntegerField(null=True, blank=True, verbose_name='耗时毫秒')
    session_id = models.CharField(max_length=64, blank=True, default='', verbose_name='会话ID')
    metadata = models.JSONField(default=dict, blank=True, verbose_name='扩展信息')
    ip_address = models.GenericIPAddressField(null=True, blank=True, verbose_name='客户端IP')
    user_agent = models.CharField(max_length=512, blank=True, default='', verbose_name='用户代理')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='analytics_events',
        verbose_name='关联用户',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'analytics_events'
        verbose_name = '埋点事件'
        verbose_name_plural = '埋点事件'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['event_name', '-created_at']),
            models.Index(fields=['module', 'event_name', '-created_at']),
            models.Index(fields=['session_id', '-created_at']),
            models.Index(fields=['user', '-created_at']),
        ]

    def __str__(self):
        return f'{self.event_name} ({self.module or "unknown"})'


class RegistrationStats(User):
    """用于后台展示注册统计，不新增数据表"""

    class Meta:
        proxy = True
        verbose_name = '注册统计'
        verbose_name_plural = '注册统计'
