from django.db import models
from django.utils import timezone
from apps.executions.models import TestPlan
from apps.projects.models import Project
from apps.users.models import User


class DifyConfig(models.Model):
    """Dify API配置"""
    api_url = models.URLField(max_length=500, verbose_name='API URL', help_text='Dify API endpoint URL')
    api_key = models.TextField(verbose_name='API Key（加密存储）', help_text='Dify API密钥')
    is_active = models.BooleanField(default=True, verbose_name='是否启用')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    
    class Meta:
        db_table = 'dify_configs'
        verbose_name = 'Dify配置'
        verbose_name_plural = 'Dify配置'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"Dify Config - {'Active' if self.is_active else 'Inactive'}"

    def save(self, *args, **kwargs):
        from apps.core.crypto import encrypt_secret
        from apps.core.outbound import validate_outbound_http_url

        self.api_key = encrypt_secret(self.api_key)
        self.api_url = validate_outbound_http_url(
            self.api_url,
            resolve=False,
            label='Dify API 地址',
        )
        return super().save(*args, **kwargs)

    def get_api_key(self):
        from apps.core.crypto import decrypt_secret

        return decrypt_secret(self.api_key)
    
    @classmethod
    def get_active_config(cls):
        """获取当前激活的配置"""
        return cls.objects.filter(is_active=True).first()


class AssistantSession(models.Model):
    """智能助手会话记录"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='assistant_sessions', verbose_name='用户')
    session_id = models.CharField(max_length=200, verbose_name='会话ID')
    conversation_id = models.CharField(max_length=200, blank=True, null=True, verbose_name='Dify对话ID')
    title = models.CharField(max_length=500, blank=True, verbose_name='会话标题')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    
    class Meta:
        db_table = 'assistant_sessions'
        verbose_name = '智能助手会话'
        verbose_name_plural = '智能助手会话'
        ordering = ['-updated_at']
    
    def __str__(self):
        return f"{self.user.username} - {self.title or self.session_id}"


class ChatMessage(models.Model):
    """聊天消息记录"""
    ROLE_CHOICES = [
        ('user', '用户'),
        ('assistant', '助手'),
    ]
    
    session = models.ForeignKey(AssistantSession, on_delete=models.CASCADE, related_name='chat_messages', verbose_name='会话')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, verbose_name='角色')
    content = models.TextField(verbose_name='消息内容')
    conversation_id = models.CharField(max_length=200, blank=True, null=True, verbose_name='Dify对话ID')
    message_id = models.CharField(max_length=200, blank=True, null=True, verbose_name='Dify消息ID')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    
    class Meta:
        db_table = 'chat_messages'
        verbose_name = '聊天消息'
        verbose_name_plural = '聊天消息'
        ordering = ['created_at']
    
    def __str__(self):
        return f"{self.get_role_display()}: {self.content[:50]}"


class AssistantMessage(models.Model):
    """智能助手消息记录（保留用于向后兼容）"""
    MESSAGE_TYPE_CHOICES = [
        ('user', '用户消息'),
        ('assistant', '助手回复'),
    ]
    
    session = models.ForeignKey(AssistantSession, on_delete=models.CASCADE, related_name='messages', verbose_name='会话')
    message_type = models.CharField(max_length=20, choices=MESSAGE_TYPE_CHOICES, verbose_name='消息类型')
    content = models.TextField(verbose_name='消息内容')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    
    class Meta:
        db_table = 'assistant_messages'
        verbose_name = '智能助手消息'
        verbose_name_plural = '智能助手消息'
        ordering = ['created_at']
    
    def __str__(self):
        return f"{self.get_message_type_display()}: {self.content[:50]}"


class CopilotTask(models.Model):
    """AI 测试 Copilot 编排任务。"""

    STATUS_CHOICES = [
        ('pending', '等待中'),
        ('completed', '已完成'),
        ('failed', '失败'),
    ]

    task_id = models.CharField(max_length=80, unique=True, verbose_name='任务ID')
    objective = models.CharField(max_length=500, verbose_name='测试目标')
    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='copilot_tasks',
        verbose_name='关联项目',
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name='状态')
    analysis = models.JSONField(default=dict, blank=True, verbose_name='业务分析')
    test_strategy = models.JSONField(default=dict, blank=True, verbose_name='测试策略')
    created_assets = models.JSONField(default=dict, blank=True, verbose_name='创建的资产')
    test_plan = models.ForeignKey(
        TestPlan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='copilot_tasks',
        verbose_name='测试计划',
    )
    summary = models.TextField(blank=True, default='', verbose_name='摘要')
    error_message = models.TextField(blank=True, default='', verbose_name='错误信息')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='copilot_tasks', verbose_name='创建者')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name='完成时间')

    class Meta:
        db_table = 'copilot_tasks'
        verbose_name = 'AI测试Copilot任务'
        verbose_name_plural = 'AI测试Copilot任务'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.task_id} - {self.objective[:40]}'
