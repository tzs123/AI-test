from django.db import models
from django.utils import timezone
from apps.users.models import User
from apps.projects.models import Project
import json
import re
import httpx
import asyncio
from typing import Dict, Any, List, AsyncIterator, Optional
import logging
from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)


class AIModelRequestError(Exception):
    """AI provider error with an explicit retry decision."""

    def __init__(self, message: str, *, status_code: int = 0, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class RequirementDocument(models.Model):
    """需求文档模型"""
    DOCUMENT_TYPE_CHOICES = [
        ('pdf', 'PDF文档'),
        ('doc', 'Word 97-2003文档'),
        ('docx', 'Word文档'),
        ('excel', 'Excel文档'),
        ('txt', '文本文档'),
        ('md', 'Markdown文档'),
        ('image', '需求图片'),
        ('yaml', 'YAML文档'),
        ('json', 'JSON文档'),
        ('har', 'HAR文档'),
        ('xml', 'XML文档'),
        ('csv', 'CSV文档'),
        ('sql', 'SQL文档'),
    ]

    STATUS_CHOICES = [
        ('uploaded', '已上传'),
        ('analyzing', '分析中'),
        ('analyzed', '分析完成'),
        ('failed', '分析失败'),
    ]

    title = models.CharField(max_length=200, verbose_name='文档标题')
    file = models.FileField(upload_to='requirement_docs/%Y/%m/', verbose_name='文档文件')
    document_type = models.CharField(max_length=10, choices=DOCUMENT_TYPE_CHOICES, verbose_name='文档类型')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='uploaded', verbose_name='状态')
    uploaded_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='uploaded_documents',
                                    verbose_name='上传者')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='requirement_documents',
                                verbose_name='关联项目', null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    file_size = models.PositiveIntegerField(verbose_name='文件大小(bytes)', null=True, blank=True)
    content_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        default=None,
        db_index=True,
        verbose_name='文件内容哈希'
    )
    extracted_text = models.TextField(verbose_name='提取的文本内容', blank=True)

    class Meta:
        db_table = 'requirement_documents'
        verbose_name = '需求文档'
        verbose_name_plural = '需求文档'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} - {self.get_status_display()}"


class PrototypeUnderstandingAsset(models.Model):
    """AI 用例生成模块中的原型/图片/文档理解资产。"""

    ASSET_TYPE_CHOICES = [
        ('image', '图片/原型图'),
        ('pdf', 'PDF'),
        ('word', 'Word'),
        ('excel', 'Excel'),
        ('swagger', 'Swagger'),
        ('postman', 'Postman'),
        ('text', '文本'),
        ('other', '其他'),
    ]
    STATUS_CHOICES = [
        ('uploaded', '已上传'),
        ('analyzed', '已识别'),
        ('draft_generated', '已生成步骤草稿'),
        ('failed', '识别失败'),
    ]

    title = models.CharField(max_length=200, verbose_name='资产标题')
    file = models.FileField(upload_to='prototype_understanding/%Y/%m/', verbose_name='原型/文档文件')
    original_name = models.CharField(max_length=255, verbose_name='原始文件名')
    asset_type = models.CharField(
        max_length=20,
        choices=ASSET_TYPE_CHOICES,
        default='other',
        db_index=True,
        verbose_name='资产类型'
    )
    status = models.CharField(
        max_length=24,
        choices=STATUS_CHOICES,
        default='uploaded',
        db_index=True,
        verbose_name='处理状态'
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='prototype_understanding_assets',
        verbose_name='关联项目'
    )
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='prototype_understanding_assets',
        verbose_name='上传者'
    )
    file_size = models.PositiveIntegerField(null=True, blank=True, verbose_name='文件大小(bytes)')
    content_hash = models.CharField(max_length=64, blank=True, default='', db_index=True, verbose_name='文件哈希')
    extracted_text = models.TextField(blank=True, default='', verbose_name='提取文本')
    analysis_result = models.JSONField(default=dict, blank=True, verbose_name='页面/元素/接口识别结果')
    ui_flow_draft = models.JSONField(default=dict, blank=True, verbose_name='UI 自动化步骤草稿')
    error_message = models.TextField(blank=True, default='', verbose_name='错误信息')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'prototype_understanding_assets'
        verbose_name = '原型理解资产'
        verbose_name_plural = '原型理解资产'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['uploaded_by', '-created_at'], name='proto_asset_user_time_idx'),
            models.Index(fields=['project', 'status'], name='proto_asset_project_status_idx'),
        ]

    def __str__(self):
        return f"{self.title} - {self.get_status_display()}"


class RequirementAnalysis(models.Model):
    """需求分析记录"""
    document = models.OneToOneField(RequirementDocument, on_delete=models.CASCADE, related_name='analysis',
                                    verbose_name='关联文档')
    analysis_report = models.TextField(verbose_name='分析报告', blank=True)
    requirements_count = models.PositiveIntegerField(verbose_name='需求数量', default=0)
    analysis_time = models.FloatField(verbose_name='分析耗时(秒)', null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'requirement_analyses'
        verbose_name = '需求分析'
        verbose_name_plural = '需求分析'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.document.title} - 分析报告"


class BusinessRequirement(models.Model):
    """业务需求模型"""
    REQUIREMENT_TYPE_CHOICES = [
        ('functional', '功能需求'),
        ('performance', '性能需求'),
        ('security', '安全需求'),
        ('usability', '可用性需求'),
        ('interface', '接口需求'),
        ('other', '其他需求'),
    ]

    REQUIREMENT_LEVEL_CHOICES = [
        ('high', '高'),
        ('medium', '中'),
        ('low', '低'),
    ]

    analysis = models.ForeignKey(RequirementAnalysis, on_delete=models.CASCADE, related_name='requirements',
                                 verbose_name='关联分析')
    requirement_id = models.CharField(max_length=50, verbose_name='需求编号')
    requirement_name = models.CharField(max_length=200, verbose_name='需求名称')
    requirement_type = models.CharField(max_length=20, choices=REQUIREMENT_TYPE_CHOICES, verbose_name='需求类型')
    parent_requirement = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True,
                                           verbose_name='父级需求')
    module = models.CharField(max_length=100, verbose_name='所属模块')
    requirement_level = models.CharField(max_length=10, choices=REQUIREMENT_LEVEL_CHOICES, verbose_name='需求级别')
    reviewer = models.CharField(max_length=50, verbose_name='评审人', default='admin')
    estimated_hours = models.PositiveIntegerField(verbose_name='预计工时', default=8)
    description = models.TextField(verbose_name='需求描述')
    acceptance_criteria = models.TextField(verbose_name='验收标准')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'business_requirements'
        verbose_name = '业务需求'
        verbose_name_plural = '业务需求'
        ordering = ['-created_at']
        unique_together = ['analysis', 'requirement_id']

    def __str__(self):
        return f"{self.requirement_id} - {self.requirement_name}"


class GeneratedTestCase(models.Model):
    """生成的测试用例模型"""
    PRIORITY_CHOICES = [
        ('P0', '最高优先级'),
        ('P1', '高优先级'),
        ('P2', '中优先级'),
        ('P3', '低优先级'),
    ]

    STATUS_CHOICES = [
        ('generated', '已生成'),
        ('reviewing', '评审中'),
        ('reviewed', '已评审'),
        ('approved', '已批准'),
        ('rejected', '已拒绝'),
        ('adopted', '已采纳'),
        ('discarded', '已弃用'),
    ]

    requirement = models.ForeignKey(BusinessRequirement, on_delete=models.CASCADE, related_name='test_cases',
                                    verbose_name='关联需求')
    case_id = models.CharField(max_length=50, verbose_name='用例编号')
    title = models.CharField(max_length=300, verbose_name='用例标题')
    priority = models.CharField(max_length=5, choices=PRIORITY_CHOICES, verbose_name='优先级')
    precondition = models.TextField(verbose_name='前置条件')
    test_steps = models.TextField(verbose_name='测试步骤')
    expected_result = models.TextField(verbose_name='预期结果')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='generated', verbose_name='状态')
    generated_by_ai = models.CharField(max_length=50, verbose_name='生成AI模型', default='AI-A')
    reviewed_by_ai = models.CharField(max_length=50, verbose_name='评审AI模型', null=True, blank=True)
    review_comments = models.TextField(verbose_name='评审意见', blank=True)
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'generated_test_cases'
        verbose_name = '生成的测试用例'
        verbose_name_plural = '生成的测试用例'
        ordering = ['-created_at']
        unique_together = ['requirement', 'case_id']

    def __str__(self):
        return f"{self.case_id} - {self.title[:50]}"


class AnalysisTask(models.Model):
    """分析任务模型"""
    TASK_TYPE_CHOICES = [
        ('requirement_analysis', '需求分析'),
        ('testcase_generation', '测试用例生成'),
        ('testcase_review', '测试用例评审'),
    ]

    STATUS_CHOICES = [
        ('pending', '待处理'),
        ('running', '运行中'),
        ('completed', '已完成'),
        ('failed', '失败'),
    ]

    task_id = models.CharField(max_length=100, unique=True, verbose_name='任务ID')
    task_type = models.CharField(max_length=30, choices=TASK_TYPE_CHOICES, verbose_name='任务类型')
    document = models.ForeignKey(RequirementDocument, on_delete=models.CASCADE, related_name='tasks',
                                 verbose_name='关联文档')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name='状态')
    progress = models.PositiveIntegerField(default=0, verbose_name='进度百分比')
    result = models.JSONField(verbose_name='任务结果', null=True, blank=True)
    error_message = models.TextField(verbose_name='错误信息', blank=True)
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name='完成时间')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')

    class Meta:
        db_table = 'analysis_tasks'
        verbose_name = '分析任务'
        verbose_name_plural = '分析任务'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.task_id} - {self.get_task_type_display()}"


class AIModelConfig(models.Model):
    """AI模型配置模型"""
    MODEL_CHOICES = [
        ('deepseek', 'DeepSeek'),
        ('qwen', '通义千问'),
        ('siliconflow', '硅基流动'),
        ('zhipu', '智谱'),
        ('xiaomi', '小米'),
        ('xiaomi_coding_plan', '小米coding plan'),
        ('other', '其他'),
    ]

    ROLE_CHOICES = [
        ('writer', '测试用例编写专家'),
        ('reviewer', '测试评审专家'),
    ]

    MODEL_USAGE_CHOICES = [
        ('test_case_generation', '测试用例生成模型'),
        ('test_case_review', '测试用例评审模型'),
        ('requirement_analysis', '测试需求分析模型'),
        ('general', '通用模型'),
    ]

    MODEL_STATUS_CHOICES = [
        ('normal', '正常'),
        ('abnormal', '异常'),
        ('slow', '响应慢'),
    ]

    GENERATION_CAPABILITY_TAGS = ['需求分析', '测试设计', '用例生成', '边界分析', '安全测试']
    REVIEW_CAPABILITY_TAGS = ['用例审查', '覆盖率分析', '缺陷发现', '质量评分']

    name = models.CharField(max_length=100, verbose_name='配置名称')
    model_type = models.CharField(max_length=20, choices=MODEL_CHOICES, verbose_name='模型类型')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, verbose_name='角色')
    model_usage = models.CharField(
        max_length=30,
        choices=MODEL_USAGE_CHOICES,
        default='test_case_generation',
        verbose_name='模型用途'
    )
    bound_prompt = models.ForeignKey(
        'PromptConfig',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='bound_model_configs',
        verbose_name='绑定Prompt'
    )
    capability_tags = models.JSONField(default=list, blank=True, verbose_name='能力标签')
    api_key = models.TextField(verbose_name='API Key（加密存储）', blank=True, null=True)
    base_url = models.URLField(verbose_name='API Base URL')
    model_name = models.CharField(max_length=100, verbose_name='模型名称')
    model_version = models.CharField(max_length=100, default='', blank=True, verbose_name='模型版本')
    max_tokens = models.IntegerField(default=4096, verbose_name='最大Token数')
    max_output_tokens = models.IntegerField(default=8192, verbose_name='Max Output Tokens')
    temperature = models.FloatField(default=0.4, verbose_name='温度参数')
    top_p = models.FloatField(default=0.8, verbose_name='Top P参数')
    retry_count = models.PositiveSmallIntegerField(default=3, verbose_name='请求失败重试次数')
    model_status = models.CharField(
        max_length=20,
        choices=MODEL_STATUS_CHOICES,
        default='normal',
        verbose_name='模型状态'
    )
    is_active = models.BooleanField(default=True, verbose_name='是否启用')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name='创建者')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'ai_model_config'
        verbose_name = 'AI模型配置'
        verbose_name_plural = 'AI模型配置'
        # 移除 unique_together 约束，允许同一个 role 有多个配置
        # 在应用层面通过代码控制：每个 role 只能有一个 is_active=True 的配置

    def __str__(self):
        return f"{self.get_model_type_display()} - {self.get_role_display()}"

    def save(self, *args, **kwargs):
        from apps.core.crypto import encrypt_secret
        from apps.core.outbound import validate_outbound_http_url

        self.api_key = encrypt_secret(self.api_key)
        self.base_url = validate_outbound_http_url(
            self.base_url,
            resolve=False,
            label='AI API 地址',
        )
        return super().save(*args, **kwargs)

    def get_api_key(self):
        from apps.core.crypto import decrypt_secret

        return decrypt_secret(self.api_key)

    def get_effective_max_output_tokens(self):
        """Return the explicit output limit, falling back to the legacy max_tokens field."""
        return self.max_output_tokens or self.max_tokens

    @classmethod
    def get_active_config(cls, model_type: str, role: str):
        """获取活跃的配置"""
        return cls.objects.filter(
            model_type=model_type,
            role=role,
            is_active=True
        ).first()


class PromptConfig(models.Model):
    """提示词配置模型"""
    PROMPT_CHOICES = [
        ('writer', '用例编写提示词'),
        ('reviewer', '用例评审提示词'),
    ]

    name = models.CharField(max_length=100, verbose_name='配置名称')
    prompt_version = models.CharField(max_length=20, default='V1.0', verbose_name='Prompt版本')
    prompt_type = models.CharField(max_length=20, choices=PROMPT_CHOICES, verbose_name='提示词类型')
    content = models.TextField(verbose_name='提示词内容')
    is_active = models.BooleanField(default=True, verbose_name='是否启用')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name='创建者')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'prompt_config'
        verbose_name = '提示词配置'
        verbose_name_plural = '提示词配置'

    def __str__(self):
        return f"{self.get_prompt_type_display()} - {self.name}"

    @classmethod
    def get_active_config(cls, prompt_type: str):
        """获取活跃的提示词配置"""
        return cls.objects.filter(
            prompt_type=prompt_type,
            is_active=True
        ).order_by('-updated_at').first()


class TestSkill(models.Model):
    """Reusable test-design skill uploaded as a SKILL.md directory."""

    name = models.CharField(max_length=120, verbose_name='Skill名称')
    identifier = models.SlugField(max_length=120, db_index=True, verbose_name='Skill标识')
    version = models.CharField(max_length=40, default='1.0.0', verbose_name='版本')
    description = models.TextField(blank=True, default='', verbose_name='说明')
    tags = models.JSONField(default=list, blank=True, verbose_name='标签')
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='test_skills',
        verbose_name='关联项目',
    )
    storage_path = models.CharField(max_length=255, unique=True, verbose_name='存储目录')
    entrypoint = models.CharField(max_length=255, default='SKILL.md', verbose_name='入口文件')
    execution_config = models.JSONField(default=dict, blank=True, verbose_name='服务器执行配置')
    manifest = models.JSONField(default=list, blank=True, verbose_name='文件清单')
    content_hash = models.CharField(max_length=64, db_index=True, verbose_name='内容哈希')
    file_count = models.PositiveIntegerField(default=0, verbose_name='文件数')
    total_size = models.PositiveIntegerField(default=0, verbose_name='总大小(bytes)')
    is_active = models.BooleanField(default=True, db_index=True, verbose_name='是否启用')
    created_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='created_test_skills',
        verbose_name='创建者',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'test_skill'
        verbose_name = '测试Skill'
        verbose_name_plural = '测试Skills'
        ordering = ['-updated_at']
        constraints = [
            models.UniqueConstraint(
                fields=['project', 'identifier'],
                name='uniq_test_skill_project_identifier',
            ),
            models.UniqueConstraint(
                fields=['identifier'],
                condition=models.Q(project__isnull=True),
                name='uniq_global_test_skill_identifier',
            ),
        ]

    def __str__(self):
        return f'{self.name} ({self.version})'

    @property
    def command(self):
        """Return the slash command without the conventional package suffix."""
        command = str(self.identifier or '').strip().lower()
        if command.endswith('-skill'):
            command = command[:-6]
        return command or 'test-skill'


class GenerationConfig(models.Model):
    """生成行为配置模型"""
    OUTPUT_MODE_CHOICES = [
        ('stream', '实时流式输出'),
        ('complete', '完整输出'),
    ]

    name = models.CharField(max_length=100, verbose_name='配置名称', default='默认生成配置')
    default_output_mode = models.CharField(
        max_length=10,
        choices=OUTPUT_MODE_CHOICES,
        default='stream',
        verbose_name='默认输出模式',
        help_text='测试用例生成的默认输出方式'
    )

    # 扩展配置字段
    enable_auto_review = models.BooleanField(
        default=True,
        verbose_name='启用AI评审和改进',
        help_text='生成完成后自动进行AI评审，并根据评审意见改进测试用例'
    )
    review_timeout = models.IntegerField(
        default=60,
        verbose_name='评审和改进超时时间（秒）',
        help_text='AI评审和改进的最大等待时间（总时长）'
    )

    is_active = models.BooleanField(default=True, verbose_name='是否启用')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'generation_config'
        verbose_name = '生成行为配置'
        verbose_name_plural = '生成行为配置'

    def __str__(self):
        return self.name

    @classmethod
    def get_active_config(cls):
        """获取活跃的生成配置"""
        return cls.objects.filter(is_active=True).first()


class TestCaseGenerationTask(models.Model):
    """测试用例生成任务模型"""
    STATUS_CHOICES = [
        ('pending', '等待中'),
        ('generating', '生成中'),
        ('reviewing', '评审中'),
        ('revising', '改进中'),
        ('completed', '已完成'),
        ('failed', '失败'),
        ('cancelled', '已取消'),
    ]

    OUTPUT_MODE_CHOICES = [
        ('stream', '实时流式输出'),
        ('complete', '完整输出'),
    ]

    GENERATION_MODE_CHOICES = [
        ('quick', '快速生成'),
        ('deep', '深度测试设计'),
        ('security', '安全专项测试'),
    ]

    task_id = models.CharField(max_length=50, unique=True, verbose_name='任务ID')
    title = models.CharField(max_length=200, verbose_name='任务标题')
    requirement_text = models.TextField(verbose_name='需求描述')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name='状态')
    progress = models.IntegerField(default=0, verbose_name='进度百分比')

    # 流式输出配置
    output_mode = models.CharField(
        max_length=10,
        choices=OUTPUT_MODE_CHOICES,
        default='stream',
        verbose_name='输出模式'
    )
    generation_mode = models.CharField(
        max_length=20,
        choices=GENERATION_MODE_CHOICES,
        default='quick',
        verbose_name='测试生成模式'
    )
    case_type_rules = models.JSONField(
        default=dict,
        blank=True,
        verbose_name='用例类型配额规则'
    )
    knowledge_rule_context = models.JSONField(
        default=list,
        blank=True,
        verbose_name='本次生成采用的业务规则快照'
    )
    selected_skills = models.ManyToManyField(
        TestSkill,
        related_name='generation_tasks',
        blank=True,
        verbose_name='本次生成选择的Skills',
    )
    skill_context = models.JSONField(
        default=list,
        blank=True,
        verbose_name='本次生成采用的Skill快照',
    )
    skill_execution_results = models.JSONField(
        default=list,
        blank=True,
        verbose_name='Skill串行执行结果',
    )

    # 流式缓冲区和状态跟踪
    stream_buffer = models.TextField(blank=True, verbose_name='流式输出缓冲区')
    stream_position = models.IntegerField(default=0, verbose_name='流式输出位置')
    last_stream_update = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='最后流式更新时间'
    )

    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='generation_tasks',
        verbose_name='关联项目'
    )

    # 配置参数
    writer_model_config = models.ForeignKey(
        AIModelConfig, on_delete=models.SET_NULL, null=True,
        related_name='writer_tasks', verbose_name='编写模型配置'
    )
    reviewer_model_config = models.ForeignKey(
        AIModelConfig, on_delete=models.SET_NULL, null=True,
        related_name='reviewer_tasks', verbose_name='评审模型配置'
    )
    writer_prompt_config = models.ForeignKey(
        PromptConfig, on_delete=models.SET_NULL, null=True,
        related_name='writer_tasks', verbose_name='编写提示词配置'
    )
    reviewer_prompt_config = models.ForeignKey(
        PromptConfig, on_delete=models.SET_NULL, null=True,
        related_name='reviewer_tasks', verbose_name='评审提示词配置'
    )

    # 生成结果
    generated_test_cases = models.TextField(blank=True, verbose_name='生成的测试用例')
    review_feedback = models.TextField(blank=True, verbose_name='评审反馈')
    final_test_cases = models.TextField(blank=True, verbose_name='最终测试用例')
    review_score = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name='最新AI评审分数'
    )
    review_round = models.PositiveIntegerField(default=0, verbose_name='评审轮次')
    review_pending = models.BooleanField(default=False, verbose_name='优化后待复评')
    review_history = models.JSONField(default=list, blank=True, verbose_name='评审历史')
    best_review_score = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name='历史最高AI评审分数'
    )
    best_test_cases = models.TextField(blank=True, verbose_name='历史最佳用例快照')
    pending_base_test_cases = models.TextField(blank=True, verbose_name='待复评优化的基线快照')

    # 元数据
    generation_log = models.TextField(blank=True, verbose_name='生成日志')
    error_message = models.TextField(blank=True, verbose_name='错误信息')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name='创建者')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name='完成时间')
    is_saved_to_records = models.BooleanField(default=False, verbose_name='是否已保存到记录')
    saved_at = models.DateTimeField(null=True, blank=True, verbose_name='保存到记录时间')

    class Meta:
        db_table = 'testcase_generation_task'
        verbose_name = '测试用例生成任务'
        verbose_name_plural = '测试用例生成任务'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} - {self.get_status_display()}"


class BusinessRuleKnowledge(models.Model):
    """可审核、可复用的业务规则知识。"""

    RULE_TYPE_CHOICES = [
        ('business', '业务约束'),
        ('field_validation', '字段校验'),
        ('state_transition', '状态流转'),
        ('permission', '权限控制'),
        ('amount', '金额规则'),
        ('time', '时间规则'),
        ('interface', '接口规则'),
        ('exception', '异常处理'),
        ('security', '安全规则'),
        ('other', '其他'),
    ]
    STATUS_CHOICES = [
        ('draft', '草稿'),
        ('approved', '已审核'),
        ('deprecated', '已废弃'),
    ]
    RISK_LEVEL_CHOICES = [
        ('low', '低风险'),
        ('medium', '中风险'),
        ('high', '高风险'),
        ('critical', '关键风险'),
    ]

    title = models.CharField(max_length=200, verbose_name='规则名称')
    content = models.TextField(verbose_name='规则内容')
    keywords = models.JSONField(default=list, blank=True, verbose_name='关键词')
    business_domain = models.CharField(max_length=100, blank=True, verbose_name='业务域')
    entity_name = models.CharField(max_length=100, blank=True, verbose_name='业务实体')
    attribute_name = models.CharField(max_length=100, blank=True, verbose_name='属性/维度')
    state_values = models.JSONField(default=list, blank=True, verbose_name='状态/枚举值')
    relation_nodes = models.JSONField(default=list, blank=True, verbose_name='关联节点')
    test_strategies = models.JSONField(default=list, blank=True, verbose_name='测试生成策略')
    risk_level = models.CharField(
        max_length=20,
        choices=RISK_LEVEL_CHOICES,
        default='medium',
        verbose_name='业务风险等级'
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='business_rule_knowledge',
        verbose_name='所属项目'
    )
    module = models.CharField(max_length=100, blank=True, verbose_name='业务模块')
    rule_type = models.CharField(
        max_length=30,
        choices=RULE_TYPE_CHOICES,
        default='business',
        verbose_name='规则类型'
    )
    applicable_conditions = models.TextField(blank=True, verbose_name='适用条件')
    expected_behavior = models.TextField(blank=True, verbose_name='预期行为')
    exceptions = models.TextField(blank=True, verbose_name='例外情况')
    source_requirement = models.CharField(max_length=300, blank=True, verbose_name='来源需求')
    source_task = models.ForeignKey(
        TestCaseGenerationTask,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='derived_business_rules',
        verbose_name='来源生成任务'
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='approved',
        verbose_name='状态'
    )
    version = models.PositiveIntegerField(default=1, verbose_name='版本')
    usage_count = models.PositiveIntegerField(default=0, verbose_name='推荐次数')
    accepted_count = models.PositiveIntegerField(default=0, verbose_name='采纳次数')
    created_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='created_business_rules',
        verbose_name='创建人'
    )
    reviewed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_business_rules',
        verbose_name='审核人'
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'business_rule_knowledge'
        verbose_name = '业务规则知识'
        verbose_name_plural = '业务规则知识库'
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['status', 'project'], name='brk_status_project_idx'),
            models.Index(fields=['rule_type', 'module'], name='brk_type_module_idx'),
            models.Index(fields=['business_domain', 'entity_name'], name='brk_domain_entity_idx'),
        ]

    def __str__(self):
        return self.title


class BusinessRuleUsage(models.Model):
    """记录规则在AI生成任务中的推荐、采纳与快照。"""

    ACTION_CHOICES = [
        ('recommended', '已推荐'),
        ('selected', '已采纳'),
        ('rejected', '已忽略'),
    ]

    rule = models.ForeignKey(
        BusinessRuleKnowledge,
        on_delete=models.CASCADE,
        related_name='usage_records',
        verbose_name='业务规则'
    )
    task = models.ForeignKey(
        TestCaseGenerationTask,
        on_delete=models.CASCADE,
        related_name='knowledge_rule_usages',
        verbose_name='生成任务'
    )
    action = models.CharField(
        max_length=20,
        choices=ACTION_CHOICES,
        default='selected',
        verbose_name='使用动作'
    )
    similarity_score = models.FloatField(default=0, verbose_name='推荐相似度')
    rule_snapshot = models.JSONField(default=dict, blank=True, verbose_name='规则快照')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'business_rule_usage'
        verbose_name = '业务规则使用记录'
        verbose_name_plural = '业务规则使用记录'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['rule', 'task'], name='unique_rule_task_usage'),
        ]


class KnowledgeNode(models.Model):
    """业务知识拓扑中的可视化节点。"""

    NODE_TYPE_CHOICES = [
        ('rule', '业务规则'),
        ('domain', '业务域'),
        ('entity', '业务实体'),
        ('attribute', '属性/维度'),
        ('state', '状态'),
        ('strategy', '测试策略'),
        ('other', '其他'),
    ]

    name = models.CharField(max_length=200, verbose_name='节点名称')
    type = models.CharField(
        max_length=30,
        choices=NODE_TYPE_CHOICES,
        default='rule',
        verbose_name='节点类型'
    )
    content = models.TextField(blank=True, verbose_name='节点内容/业务规则')
    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='knowledge_nodes',
        verbose_name='所属项目'
    )
    rule = models.OneToOneField(
        BusinessRuleKnowledge,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='knowledge_node',
        verbose_name='关联业务规则'
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'knowledge_node'
        verbose_name = '知识节点'
        verbose_name_plural = '知识节点'
        ordering = ['name']
        indexes = [
            models.Index(fields=['project', 'type'], name='knode_project_type_idx'),
            models.Index(fields=['name'], name='knowledge_node_name_idx'),
        ]

    def __str__(self):
        return self.name


class KnowledgeRelation(models.Model):
    """业务知识拓扑中的节点关系。"""

    RELATION_TYPE_CHOICES = [
        ('upstream', '上游依赖'),
        ('downstream', '下游影响'),
        ('contains', '包含'),
        ('depends_on', '依赖'),
        ('constrains', '约束'),
        ('affects', '影响'),
        ('related', '关联'),
    ]

    source = models.ForeignKey(
        KnowledgeNode,
        on_delete=models.CASCADE,
        related_name='outgoing_relations',
        verbose_name='源节点'
    )
    target = models.ForeignKey(
        KnowledgeNode,
        on_delete=models.CASCADE,
        related_name='incoming_relations',
        verbose_name='目标节点'
    )
    relation_type = models.CharField(
        max_length=40,
        choices=RELATION_TYPE_CHOICES,
        default='related',
        verbose_name='关系类型'
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'knowledge_relation'
        verbose_name = '知识关系'
        verbose_name_plural = '知识关系'
        ordering = ['source_id', 'target_id']
        constraints = [
            models.CheckConstraint(
                check=~models.Q(source=models.F('target')),
                name='knowledge_relation_no_self_loop'
            ),
            models.UniqueConstraint(
                fields=['source', 'target', 'relation_type'],
                name='unique_knowledge_relation'
            ),
        ]

    def __str__(self):
        return f"{self.source_id} -> {self.target_id} ({self.relation_type})"


class BusinessWorkflow(models.Model):
    """AI 用例生成可引用的业务工作流文件。"""

    STATUS_CHOICES = [
        ('draft', '草稿'),
        ('active', '启用'),
        ('archived', '已归档'),
    ]

    name = models.CharField(max_length=200, verbose_name='工作流名称')
    identifier = models.CharField(max_length=120, blank=True, verbose_name='工作流标识')
    category = models.CharField(max_length=100, blank=True, verbose_name='业务分类')
    version = models.PositiveIntegerField(default=1, verbose_name='版本')
    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='business_workflows',
        verbose_name='所属项目'
    )
    description = models.TextField(blank=True, verbose_name='工作流说明')
    definition = models.JSONField(default=dict, blank=True, verbose_name='工作流定义')
    source_file = models.FileField(
        upload_to='business_workflows/%Y/%m/',
        null=True,
        blank=True,
        verbose_name='导入源文件'
    )
    source_text = models.TextField(blank=True, verbose_name='导入源文本')
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='active',
        verbose_name='状态'
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='created_business_workflows',
        verbose_name='创建人'
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'business_workflow'
        verbose_name = '业务工作流'
        verbose_name_plural = '业务工作流'
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['status', 'project'], name='bwf_status_project_idx'),
            models.Index(fields=['category'], name='bwf_category_idx'),
            models.Index(fields=['identifier'], name='bwf_identifier_idx'),
        ]

    def __str__(self):
        return self.name


class AIModelService:
    """AI模型服务类"""

    @staticmethod
    def format_api_error(provider_name: str, status_code: int, response_body: str = '') -> str:
        """Convert provider responses into short, actionable messages safe for the UI."""
        provider_name = str(provider_name or 'AI模型').strip()
        try:
            status_code = int(status_code)
        except (TypeError, ValueError):
            status_code = 0

        gateway_messages = {
            502: '上游模型服务返回了无效响应',
            503: '上游模型服务暂时不可用',
            504: '上游模型服务响应超时',
            524: '上游模型未在网关时限内响应',
        }
        if status_code in gateway_messages:
            return (
                f'{provider_name} API 网关错误（HTTP {status_code}）：'
                f'{gateway_messages[status_code]}。系统已自动重试，请稍后再试；'
                '若持续发生，请检查模型服务负载或更换可稳定流式响应的接口。'
            )

        body = str(response_body or '').strip()
        if status_code == 429:
            if 'DAILY_LIMIT_EXCEEDED' in body.upper() or 'DAILY USAGE LIMIT' in body.upper():
                return (
                    f'{provider_name} API 请求失败（HTTP 429）：当前模型账号今日额度已用完。'
                    '请补充额度、等待供应商日限额重置，或切换其他可用模型。'
                )
            return (
                f'{provider_name} API 请求失败（HTTP 429）：请求过于频繁或额度不足。'
                '请稍后再试或检查模型账号额度。'
            )

        content_type_is_html = bool(re.search(r'<!doctype\s+html|<html[\s>]', body, re.IGNORECASE))
        if content_type_is_html:
            detail = '上游服务返回了错误页面'
        else:
            detail = ''
            try:
                payload = json.loads(body)
                if isinstance(payload, dict):
                    error = payload.get('error')
                    if isinstance(error, dict):
                        detail = str(error.get('message') or error.get('detail') or '').strip()
                    elif error:
                        detail = str(error).strip()
                    detail = detail or str(payload.get('message') or payload.get('detail') or '').strip()
            except (TypeError, ValueError, json.JSONDecodeError):
                detail = re.sub(r'\s+', ' ', body)

        detail = detail[:500].strip()
        status_label = f' HTTP {status_code}' if status_code else ''
        suffix = f'：{detail}' if detail else ''
        return f'{provider_name} API 请求失败（{status_label.strip()}）{suffix}'

    @staticmethod
    def sanitize_error_message(error: Any) -> str:
        """Keep stored and serialized task errors readable and bounded."""
        message = str(error or '').strip()
        if not message:
            if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
                return 'AI模型响应超时，请稍后重试'
            return 'AI用例生成失败，请稍后重试'

        status_match = re.search(
            r'(?:HTTP(?:/\d(?:\.\d)?)?\s*|错误\s*|code\s*)([45]\d{2})',
            message,
            re.IGNORECASE,
        )
        contains_html = bool(re.search(r'<!doctype\s+html|<html[\s>]', message, re.IGNORECASE))
        if contains_html:
            provider_match = re.match(r'([^\s]+)\s+API', message)
            provider_name = provider_match.group(1) if provider_match else 'AI模型'
            status_code = int(status_match.group(1)) if status_match else 0
            return AIModelService.format_api_error(provider_name, status_code, message)

        # Skill runners may return a full traceback. Showing the first 1,000
        # characters usually cuts off the actionable exception and leaves the
        # user with an unhelpful ``line ...`` fragment. Prefer the final
        # exception line, while keeping a short tail when the runner output is
        # truncated before that line.
        if 'Traceback (most recent call last):' in message:
            traceback_lines = [line.strip() for line in message.splitlines() if line.strip()]
            if traceback_lines:
                for line in reversed(traceback_lines):
                    if re.search(
                            r'(?:Error|Exception|Timeout|failed|HTTP\s*[45]\d{2}|拒绝|失败|超时)',
                            line,
                            re.IGNORECASE,
                    ) and not line.startswith('Traceback'):
                        return line[:1000]
                return 'Skill脚本调用模型失败，但旧版错误日志被截断。请重试；若仍失败，请检查模型接口稳定性和 Skill 兼容性。'

        return message[:1000]

    @staticmethod
    def record_generation_event(
            task: TestCaseGenerationTask,
            stage: str,
            status: str,
            title: str,
            message: str = '',
            **extra: Any,
    ) -> Dict[str, Any]:
        """Append a safe, user-facing execution event to the task timeline.

        This is deliberately a stage summary, not model chain-of-thought. It
        lets the UI explain which integration is running without persisting
        hidden reasoning or credentials.
        """
        event = {
            'stage': str(stage or 'task'),
            'status': str(status or 'info'),
            'title': str(title or '').strip(),
            'message': AIModelService.sanitize_error_message(message) if status == 'failed' else str(message or '').strip(),
            'timestamp': timezone.now().isoformat(),
        }
        event.update({key: value for key, value in extra.items() if value is not None})
        try:
            refresh = getattr(task, 'refresh_from_db', None)
            if callable(refresh):
                refresh(fields=['generation_log'])
            raw_events = json.loads(getattr(task, 'generation_log', '') or '[]')
            events = raw_events if isinstance(raw_events, list) else []
        except Exception:
            events = []
        events.append(event)
        events = events[-200:]
        task.generation_log = json.dumps(events, ensure_ascii=False)
        save = getattr(task, 'save', None)
        if callable(save):
            try:
                save(update_fields=['generation_log', 'updated_at'])
            except Exception:
                logger.warning(
                    '保存生成过程事件失败: task=%s',
                    getattr(task, 'task_id', 'unknown'),
                    exc_info=True,
                )
        return event

    @staticmethod
    async def arecord_generation_event(
            task: TestCaseGenerationTask,
            stage: str,
            status: str,
            title: str,
            message: str = '',
            **extra: Any,
    ) -> Dict[str, Any]:
        """Persist a timeline event from an async model or Skill workflow."""
        return await sync_to_async(
            AIModelService.record_generation_event,
            thread_sensitive=True,
        )(task, stage, status, title, message, **extra)

    @staticmethod
    async def update_skill_pipeline_progress(
            task: TestCaseGenerationTask,
            stage_index: int,
            stage_count: int,
            *,
            completed: bool = False,
    ) -> int:
        """Persist visible progress while the Skill pipeline is executing."""
        if stage_count <= 0:
            return int(getattr(task, 'progress', 0) or 0)
        position = stage_index + (1 if completed else 0.5)
        target = min(50, 30 + int(20 * position / stage_count))
        current = int(getattr(task, 'progress', 0) or 0)
        target = max(current, target)
        task.progress = target
        task_pk = getattr(task, 'pk', None)
        if task_pk:
            await sync_to_async(
                TestCaseGenerationTask.objects.filter(
                    pk=task_pk,
                    status='generating',
                    progress__lt=target,
                ).update,
                thread_sensitive=True,
            )(progress=target, updated_at=timezone.now())
        return target

    @staticmethod
    def get_openai_compatible_headers(api_key: str) -> Dict[str, str]:
        """构建 OpenAI 兼容接口请求头，同时兼容部分厂商的 api-key 认证。"""
        return {
            'Authorization': f'Bearer {api_key}',
            'api-key': api_key,
            'Content-Type': 'application/json'
        }

    @staticmethod
    def resolve_api_key(config) -> str:
        getter = getattr(config, 'get_api_key', None)
        if callable(getter):
            return getter()
        return str(getattr(config, 'api_key', '') or '')

    @staticmethod
    def build_openai_compatible_url(base_url: str, endpoint: str) -> str:
        """根据用户输入的 base_url 构建 OpenAI 兼容接口地址。"""
        normalized_base_url = base_url.rstrip('/')

        if normalized_base_url.endswith(endpoint):
            return normalized_base_url

        for known_endpoint in ('/chat/completions', '/models'):
            if normalized_base_url.endswith(known_endpoint):
                normalized_base_url = normalized_base_url[:-len(known_endpoint)]
                break

        import re
        version_match = re.search(r'/v(\d+)/?$', normalized_base_url)
        if version_match:
            return f"{normalized_base_url}{endpoint}"

        return f"{normalized_base_url}/v1{endpoint}"

    @staticmethod
    async def call_openai_compatible_api(
            config: AIModelConfig,
            messages: List[Dict[str, str]],
            max_tokens: int = None,
            max_retries: int = None,
            read_timeout_seconds: float = None,
    ) -> Dict[str, Any]:
        """
        调用OpenAI兼容格式的API

        Args:
            config: AI模型配置
            messages: 消息列表
            max_tokens: 可选的最大token数，如果不指定则使用config.max_tokens

        Returns:
            API响应字典
        """
        headers = AIModelService.get_openai_compatible_headers(
            AIModelService.resolve_api_key(config)
        )

        actual_max_tokens = AIModelService.resolve_max_output_tokens(config, max_tokens)

        data = {
            'model': config.model_name,
            'messages': messages,
            'max_tokens': actual_max_tokens,
            'temperature': config.temperature,
            'top_p': config.top_p,
            'stream': False
        }
        url = AIModelService.build_openai_compatible_url(config.base_url, '/chat/completions')

        logger.info(f"=== API调用详情 ===")
        logger.info(f"原始base_url: {config.base_url}")
        logger.info(f"最终请求URL: {url}")
        logger.info(f"模型名称: {config.model_name}")
        logger.info(f"请求参数: max_tokens={actual_max_tokens}, temperature={config.temperature}, top_p={config.top_p}")

        provider_name = config.get_model_type_display()
        effective_max_retries = (
            AIModelService.resolve_retry_count(config)
            if max_retries is None
            else max(0, int(max_retries))
        )
        read_timeout = 900.0 if read_timeout_seconds is None else max(1.0, float(read_timeout_seconds))

        for attempt in range(effective_max_retries + 1):
            try:
                from apps.core.outbound import validate_outbound_http_url

                url = validate_outbound_http_url(url, label='AI API 地址')
                # 增加HTTP超时时间到900秒（15分钟），支持大文档生成
                # 禁用HTTP/2，使用HTTP/1.1以提高兼容性
                # 显式设置所有超时参数，避免默认的连接超时导致请求失败
                timeout_config = httpx.Timeout(
                    connect=60.0,  # 连接超时：60秒
                    read=read_timeout,
                    write=60.0,  # 写入超时：60秒
                    pool=60.0  # 连接池超时：60秒
                )
                async with httpx.AsyncClient(timeout=timeout_config, http2=False) as client:
                    logger.info(
                        f"发送POST请求到: {url} (尝试 {attempt + 1}/{effective_max_retries + 1})"
                    )
                    response = await client.post(
                        url,
                        headers=headers,
                        json=data
                    )

                    logger.info(f"收到响应: status_code={response.status_code}")

                    if response.status_code != 200:
                        logger.error(
                            AIModelService.format_api_error(
                                provider_name,
                                response.status_code,
                                response.text,
                            )
                        )

                    response.raise_for_status()
                    result = response.json()
                    logger.info(f"API调用成功，响应内容: {str(result)[:200]}...")
                    return result
            except httpx.HTTPStatusError as e:
                error_msg = AIModelService.format_api_error(
                    provider_name,
                    e.response.status_code,
                    e.response.text,
                )
                if 400 <= e.response.status_code < 500 or attempt >= effective_max_retries:
                    logger.error(error_msg)
                    raise Exception(error_msg)
                logger.warning(f"{error_msg}，准备重试")
            except httpx.TimeoutException as e:
                if attempt >= effective_max_retries:
                    logger.error(f"{provider_name} API请求超时: {repr(e)}")
                    raise Exception(f"{provider_name} API请求超时，请稍后再试或检查网络连接")
                logger.warning(f"{provider_name} API请求超时: {repr(e)}，准备重试")
            except httpx.RequestError as e:
                if attempt >= effective_max_retries:
                    logger.error(f"{provider_name} API请求失败: {repr(e)}")
                    raise Exception(f"{provider_name} API请求失败，请稍后再试或检查网络连接")
                logger.warning(f"{provider_name} API请求失败: {repr(e)}，准备重试")
            except Exception as e:
                if attempt >= effective_max_retries:
                    logger.error(f"{provider_name} API调用失败: {repr(e)}")
                    raise Exception(f"{provider_name} API调用失败: {str(e) or repr(e)}")
                logger.warning(f"{provider_name} API调用失败: {repr(e)}，准备重试")

            await asyncio.sleep(min(2 ** attempt, 5))

        raise Exception(f"{provider_name} API调用失败，请稍后再试")

    @staticmethod
    async def list_available_models(config: AIModelConfig) -> List[str]:
        """获取当前配置下可用的模型列表。"""
        headers = AIModelService.get_openai_compatible_headers(
            AIModelService.resolve_api_key(config)
        )
        url = AIModelService.build_openai_compatible_url(config.base_url, '/models')
        from apps.core.outbound import validate_outbound_http_url

        url = validate_outbound_http_url(url, label='AI API 地址')

        timeout_config = httpx.Timeout(
            connect=30.0,
            read=60.0,
            write=30.0,
            pool=30.0
        )

        try:
            async with httpx.AsyncClient(timeout=timeout_config, http2=False) as client:
                logger.info(f"获取模型列表，URL: {url}")
                response = await client.get(url, headers=headers)

                if response.status_code != 200:
                    error_detail = response.text
                    logger.error(f"获取模型列表失败: Status={response.status_code}, Body={error_detail}")

                response.raise_for_status()
                payload = response.json()
                raw_models = payload.get('data', []) if isinstance(payload, dict) else payload

                model_ids = []
                for item in raw_models or []:
                    if isinstance(item, dict):
                        model_id = item.get('id') or item.get('model') or item.get('name')
                        if model_id:
                            model_ids.append(str(model_id))
                    elif isinstance(item, str):
                        model_ids.append(item)

                # 去重并保留原始顺序
                unique_model_ids = list(dict.fromkeys(model_ids))
                logger.info(f"获取模型列表成功，共{len(unique_model_ids)}个模型")
                return unique_model_ids
        except httpx.HTTPStatusError as e:
            provider_name = config.get_model_type_display()
            raise Exception(f"{provider_name} 获取模型列表失败 {e.response.status_code}: {e.response.text}")
        except httpx.TimeoutException as e:
            provider_name = config.get_model_type_display()
            logger.error(f"{provider_name} 获取模型列表超时: {repr(e)}")
            raise Exception(f"{provider_name} 获取模型列表超时，请检查网络连接或API地址是否正确")
        except Exception as e:
            provider_name = config.get_model_type_display()
            logger.error(f"{provider_name} 获取模型列表失败: {repr(e)}")
            raise Exception(f"{provider_name} 获取模型列表失败: {str(e) or repr(e)}")

    @staticmethod
    def resolve_max_output_tokens(config: AIModelConfig, max_tokens: int = None) -> int:
        if max_tokens is not None:
            return max_tokens
        if hasattr(config, 'get_effective_max_output_tokens'):
            return config.get_effective_max_output_tokens()
        return getattr(config, 'max_output_tokens', None) or config.max_tokens

    @staticmethod
    def resolve_retry_count(config: AIModelConfig) -> int:
        try:
            return max(0, int(getattr(config, 'retry_count', 0) or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def get_effective_review_timeout(configured_timeout: int = None) -> int:
        """Honor the configured review timeout within the supported UI range."""
        try:
            configured = int(configured_timeout)
        except (TypeError, ValueError):
            configured = 60
        return max(10, min(configured, 3600))

    @staticmethod
    def trim_stream_recovery_overlap(existing_content: str, recovery_content: str) -> str:
        """移除断流恢复内容与已输出内容之间的重复前缀。"""
        existing_content = str(existing_content or '')
        recovery_content = str(recovery_content or '')
        if not existing_content or not recovery_content:
            return recovery_content

        max_overlap = min(len(existing_content), len(recovery_content), 4096)
        for overlap_length in range(max_overlap, 0, -1):
            if existing_content[-overlap_length:] == recovery_content[:overlap_length]:
                return recovery_content[overlap_length:]
        return recovery_content

    @staticmethod
    def build_stream_recovery_messages(
            messages: List[Dict[str, str]],
            partial_content: str
    ) -> List[Dict[str, str]]:
        """把已经成功输出的半截响应放回上下文，要求模型从断点继续。"""
        partial_content = str(partial_content or '')
        recovery_messages = list(messages)
        # Replace the previous network-recovery instruction instead of growing
        # a chain of assistant/user pairs after repeated disconnects. This keeps
        # the complete output in one assistant message and prevents the model
        # from treating an earlier partial response as a separate answer.
        if (
                recovery_messages
                and recovery_messages[-1].get('role') == 'user'
                and '因网络传输中断' in str(recovery_messages[-1].get('content') or '')
        ):
            recovery_messages.pop()
        if recovery_messages and recovery_messages[-1].get('role') == 'assistant':
            previous_content = str(recovery_messages[-1].get('content') or '')
            recovery_messages[-1] = {
                **recovery_messages[-1],
                'content': previous_content + partial_content,
            }
            partial_for_prompt = previous_content + partial_content
        else:
            recovery_messages.append({'role': 'assistant', 'content': partial_content})
            partial_for_prompt = partial_content
        recovery_messages.append({
            'role': 'user',
            'content': (
                '上一条回复因网络传输中断，只收到了部分内容。'
                '请从已输出内容的最后一个字符后继续，禁止重复任何已输出内容，'
                '保持原格式并完整输出剩余内容。\n'
                f'已输出内容末尾：\n{partial_for_prompt[-2000:]}'
            ),
        })
        return recovery_messages

    @staticmethod
    def get_platform_system_prompt(role: str) -> str:
        """Return the minimal platform contract; Skills own the testing method."""
        if role == 'reviewer':
            return (
                "执行用户消息首行激活的主 Skill，并将已选业务规则、需求资料和候选用例作为输入。"
                "这些输入是唯一业务事实来源；除平台要求的评审输出格式外，不得应用旧提示词、"
                "预设测试方法或输入中未提供的产品规则。"
            )
        return (
            "执行用户消息首行激活的主 Skill，并将已选业务规则和需求资料作为输入。"
            "这些输入是唯一业务事实来源；除平台要求的用例输出格式外，不得应用旧提示词、"
            "预设测试方法或输入中未提供的产品规则。"
        )

    @staticmethod
    def get_task_system_prompt(task: TestCaseGenerationTask, role: str) -> str:
        # PromptConfig remains only for historical database compatibility.
        return AIModelService.get_platform_system_prompt(role)

    @staticmethod
    def build_test_case_generation_messages(task: TestCaseGenerationTask) -> List[Dict[str, str]]:
        """Build writer messages from Skills, selected rules and requirement materials."""
        blocks = [AIModelService.get_required_skill_instruction(task)]
        handoff = AIModelService.get_skill_pipeline_handoff(task).strip()
        quota_instruction = AIModelService.get_case_type_quota_instruction(task)
        knowledge_rule_instruction = AIModelService.get_knowledge_rule_instruction(task)

        if handoff:
            blocks.append(handoff)
        if quota_instruction:
            blocks.append(quota_instruction)
        if knowledge_rule_instruction:
            blocks.append(knowledge_rule_instruction)
        blocks.extend([
            "【平台输出格式】\n" + AIModelService.get_test_case_output_contract(),
            "【需求资料】\n" + str(task.requirement_text or ''),
        ])
        return [
            {
                'role': 'system',
                'content': AIModelService.get_task_system_prompt(task, 'writer'),
            },
            {'role': 'user', 'content': '\n\n'.join(blocks)},
        ]

    @staticmethod
    def get_test_case_output_contract() -> str:
        return (
            "请严格按照以下字段生成 Markdown 表格，且字段顺序必须保持一致：\n"
            "| 用例编号 | 测试模块 | 测试类型 | 测试场景 | 测试数据 | 前置条件 | 操作步骤 | 预期结果 | 优先级 | 需求编号 |\n"
            "|---|---|---|---|---|---|---|---|---|---|\n\n"
            "字段规则：\n"
            "1. 测试模块：填写所属功能模块，例如页面展示、手机号输入、验证码功能、车牌输入、申请提交、安全测试、兼容性测试。\n"
            "2. 测试类型：只能填写功能测试、异常测试、边界值测试、等价类测试、安全测试、兼容测试。\n"
            "3. 测试数据：每条用例必须提供可直接执行的具体输入数据，例如手机号：13800138000；验证码：123456；车牌：京A12345。无输入时写“无”。\n"
            "4. 需求编号：必须填写可追踪编号，例如 REQ-LOGIN-001、REQ-CAR-APPLY-001；如原需求未提供，请基于模块和功能生成稳定编号。\n"
            "5. 优先级：只能填写 P0、P1、P2、P3。P0=核心流程阻断，P1=重要功能异常，P2=一般问题，P3=优化建议。\n\n"
            "5. 测试场景：必须描述具体业务动作、触发条件/状态和可验证的业务结果，并与工作流节点、知识库规则或上传材料中的事实建立关系；禁止只写‘验证功能正常’。\n"
            "6. 用例明细表必须一行对应一条独立测试用例；严禁把多条用例合并成编号区间（如 001-012），严禁输出只有编号、模块、类型而缺少场景、步骤或预期结果的汇总行。\n"
            "7. 可以在明细表前输出需求分析、配额统计或覆盖率分析，但最终必须输出完整的逐条用例明细表；统计表不能替代用例明细表。\n\n"
            "测试类型强制分类：\n"
            "- 页面展示、输入校验、流程提交必须标记为功能测试。\n"
            "- 接口异常、超时、断网、服务错误、返回异常必须标记为异常测试。\n"
            "- 最小/最大值、长度、金额、数量、时间、阈值临界点及其相邻值必须标记为边界值测试。\n"
            "- 对输入域、枚举、角色、状态、渠道和业务规则进行有效/无效分类验证时，必须标记为等价类测试。\n"
            "- SQL注入、XSS攻击、CSRF、越权、敏感数据泄露、验证码攻击、接口攻击、参数篡改必须标记为安全测试。\n"
            "- 兼容浏览器、多终端、分辨率适配以及 iOS/Android 设备特性必须标记为兼容测试。\n\n"
            "接口文档分析要求：若需求包含 Swagger JSON、OpenAPI JSON/YAML、Postman Collection、HAR 或接口描述，必须识别接口路径、HTTP方法、请求头、鉴权方式、Path/Query/Body 参数、响应码、错误结构、参数依赖，并生成接口测试、参数组合、异常接口和安全测试用例。\n"
            "原型图/页面资料分析要求：若需求包含原型图、截图或页面描述，必须识别页面布局、页面元素、控件类型、输入规则、按钮状态、页面跳转、异常状态，并转化为测试用例。"
        )

    @staticmethod
    def get_review_output_contract() -> str:
        return (
            "请输出结构化评审报告，必须包含：\n"
            "1. 第一行必须严格输出“AI评分：N/100”，N为0-100整数。\n"
            "2. 测试覆盖分析：功能覆盖率、业务覆盖率、异常覆盖率、边界覆盖率、等价类覆盖率、安全覆盖率、兼容覆盖率、移动端特性覆盖率。\n"
            "3. 用例质量评分：功能覆盖、业务覆盖、异常覆盖、边界覆盖、等价类覆盖、安全覆盖、兼容覆盖、移动端特性覆盖、综合评分。\n"
            "4. 缺失测试点：使用编号列表输出 AI 自动发现但当前用例未覆盖的测试点。\n"
            "5. 问题列表：精确到用例编号。\n"
            "6. 改进建议：说明需要补充或修正的业务模块、测试数据、测试类型、需求编号。\n"
            "7. 评分必须严格等于各维度得分之和；存在任一缺失测试点、虚构业务规则或不可执行用例时不得评100分。所有强制项全部满足时应评100分，不得人为压分。"
        )

    @staticmethod
    def get_business_test_design_instruction() -> str:
        """生成、评审和优化共用的业务驱动测试设计约束。"""
        return (
            "【业务驱动测试设计（强制）】\n"
            "- 先识别参与角色、业务目标、前置资格、核心规则、数据/状态流转、下游影响、失败回滚和终态，再设计用例。\n"
            "- 主流程、分支流程、逆向流程、跨角色协作、状态机和数据一致性必须可追溯到需求或业务规则。\n"
            "- 不得围绕页面控件堆砌用例；每条用例必须说明独立的业务风险、可执行数据和可验证的业务结果。\n"
            "【等价类（强制）】\n"
            "- 对每个输入域、枚举、角色、状态、渠道、资质与业务规则划分有效等价类和无效等价类，为每类选取代表数据。\n"
            "- 等价类用例要标明所属分类，不得仅替换数据生成重复用例。\n"
            "【边界值（强制）】\n"
            "- 对长度、金额、数量、比率、年龄、日期、有效期、次数和并发阈值覆盖 min-1/min/min+1 与 max-1/max/max+1。\n"
            "- 同时覆盖空值、空白、零值、负值、极大值、阈值前/当时/阈值后和跨日/跨月/跨时区。\n"
            "【移动端兼容性（有移动端场景时强制）】\n"
            "- 覆盖 iOS/Android 主流与最低支持版本、手机/平板/折叠屏、刘海屏/灵动岛、横竖屏、分屏、字体和显示缩放。\n"
            "- 覆盖触控/手势、虚拟键盘与中英文输入法、系统权限、前后台/锁屏/进程被回收/恢复、电话与通知中断。\n"
            "- 覆盖 Wi-Fi/4G/5G/弱网/断网/网络切换、WebView/系统浏览器、深链、深色模式、多语言、时区、低存储和低电量。\n"
            "- 需求未明确的终端范围或阈值必须标记“待业务确认”，禁止虚构产品规则。"
        )

    @staticmethod
    def get_source_traceability_instruction(task: TestCaseGenerationTask) -> str:
        """约束用例必须围绕已选业务来源形成可追溯的业务场景。"""
        knowledge_rules = getattr(task, 'knowledge_rule_context', None) or []
        workflow_rules = [
            item for item in knowledge_rules
            if item.get('workflow_id') or item.get('rule_type') == 'workflow'
        ]
        knowledge_items = [item for item in knowledge_rules if item not in workflow_rules]
        requirement_text = str(getattr(task, 'requirement_text', '') or '')
        has_uploaded_material = any(
            marker in requirement_text
            for marker in ('【文件：', '【需求文档内容】', '【上传文件', 'Swagger', 'OpenAPI', 'Postman', 'HAR')
        )

        source_lines = [
            "【来源追溯与业务场景链路（强制）】",
            "- 每条用例必须能回答：来自哪个业务来源、覆盖工作流哪一步/哪条路径、验证哪个业务规则、使用材料中的哪个字段/接口/页面事实。不能只写‘验证功能正常’或套用通用登录模板。",
            "- 测试场景必须写成‘业务动作 + 触发条件/状态 + 业务结果’，并与前置条件、测试数据、步骤、预期结果保持同一条业务链路；不要只描述页面控件动作。",
        ]
        if workflow_rules:
            source_lines.extend([
                "- 本次已选择业务工作流：必须沿工作流节点和连线生成主路径、分支条件、回退/重试、终止、异常和跨节点数据一致性场景；场景中要体现具体节点或路径名称。",
                "- 工作流中的每个关键节点、分支和终态都要有对应覆盖；不能只覆盖首尾页面而跳过中间业务状态。",
            ])
        if knowledge_items:
            source_lines.extend([
                "- 本次已选择知识库规则/知识图谱：必须将规则标题、业务实体、属性、状态、权限、上下游关系或例外条件落实到场景、数据和预期结果中；不能只在说明里提到规则而不验证。",
                "- 知识库规则与当前需求或上传材料冲突时，以当前需求/上传材料为准，并在相关用例中标记‘待业务确认’，禁止自行裁决或虚构规则。",
            ])
        if has_uploaded_material:
            source_lines.extend([
                "- 本次包含上传材料：优先使用文件中的真实页面元素、字段约束、枚举、接口路径、请求参数、响应码、错误结构、状态和示例数据生成场景；文件未说明的内容必须标记‘待业务确认’。",
                "- 上传材料中的每个关键接口/页面流程都要与具体用例建立关系，不能只把文件全文当背景描述。",
            ])
        source_lines.extend([
            "- 需求编号必须稳定且可追溯；测试模块、测试场景和需求编号应指向同一业务对象。无法确认来源的候选用例不得作为确定规则输出。",
            "- 生成前先建立‘来源 → 业务实体/状态 → 工作流节点或规则 → 测试场景 → 验证结果’覆盖矩阵，再输出逐条用例；覆盖矩阵不能替代用例明细。",
        ])
        return '\n'.join(source_lines)

    @staticmethod
    def get_review_scoring_rubric() -> str:
        """首次AI质量评审使用的百分制量化标准。"""
        return (
            "【百分制质量评审量表（必须逐项计分）】\n"
            "- 业务目标、规则、角色、状态流转与需求可追溯：30分。\n"
            "- 主流程、分支、逆向流程、数据一致性与失败回滚：20分。\n"
            "- 有效/无效等价类覆盖：10分。\n"
            "- 临界点及相邻值的边界覆盖：10分。\n"
            "- 异常、恢复、并发与重复提交：10分。\n"
            "- 移动端设备、系统、屏幕、权限、生命周期、中断和网络特性：10分（明确无移动端时按其它兼容性与环境风险评估）。\n"
            "- 安全、接口契约与敏感数据：5分。\n"
            "- 用例可执行性、预期结果可验证性、无重复无凑数：5分。\n"
            "合计100分。每个扣分项必须在缺失测试点或问题列表中给出可直接补充的依据。"
        )

    @staticmethod
    def extract_review_score(review_feedback: str) -> Optional[int]:
        """从AI评审报告中提取0-100整数分数。"""
        if not review_feedback:
            return None
        # 评审协议规定首行是正式总分。报告正文还可能包含维度分数、覆盖率
        # 或示例分数，不能取最后一个数字，否则会把“总分64、维度100”误记成100。
        first_line = str(review_feedback).strip().splitlines()[0] if str(review_feedback).strip() else ''
        first_line_match = re.search(
            r'^\s*AI评分\s*：\s*(\d{1,3})\s*/\s*100\s*$',
            first_line,
            flags=re.IGNORECASE,
        )
        if first_line_match:
            return max(0, min(100, int(first_line_match.group(1))))
        patterns = (
            r'(?:AI评分|综合评分|总评分|评审分数|质量评分|评分)[^\d]{0,16}(\d{1,3})(?:\s*/\s*100)?',
            r'(\d{1,3})\s*/\s*100',
        )
        for pattern in patterns:
            matches = re.findall(pattern, str(review_feedback), flags=re.IGNORECASE)
            if matches:
                return max(0, min(100, int(matches[0])))
        return None

    @staticmethod
    def get_task_review_score(task: TestCaseGenerationTask) -> Optional[int]:
        score = getattr(task, 'review_score', None)
        if score is not None:
            return max(0, min(100, int(score)))
        return AIModelService.extract_review_score(getattr(task, 'review_feedback', ''))

    @staticmethod
    def record_review_result(
            task: TestCaseGenerationTask,
            review_feedback: str,
            source: str = 'manual',
            reviewed_test_cases: str = None
    ) -> Optional[int]:
        """保存最新评审结果并追加轮次历史。"""
        score = AIModelService.extract_review_score(review_feedback)
        next_round = int(getattr(task, 'review_round', 0) or 0) + 1
        history = list(getattr(task, 'review_history', None) or [])
        history.append({
            'round': next_round,
            'score': score,
            'source': source,
            'accepted': True,
            'feedback': review_feedback,
            'reviewed_at': timezone.now().isoformat(),
        })
        best_score = getattr(task, 'best_review_score', None)
        reviewed_cases = (
            reviewed_test_cases
            or getattr(task, 'final_test_cases', '')
            or getattr(task, 'generated_test_cases', '')
        )
        task.review_feedback = review_feedback
        task.review_score = score
        task.review_round = next_round
        task.review_pending = False
        task.review_history = history
        task.pending_base_test_cases = ''
        update_fields = [
            'review_feedback',
            'review_score',
            'review_round',
            'review_pending',
            'review_history',
            'pending_base_test_cases',
            'updated_at',
        ]
        if score is not None and (best_score is None or score >= best_score):
            task.best_review_score = score
            task.best_test_cases = reviewed_cases
            update_fields.extend(['best_review_score', 'best_test_cases'])
        task.save(update_fields=update_fields)
        return score

    @staticmethod
    def get_case_type_quota_targets(task: TestCaseGenerationTask) -> Dict[str, int]:
        """返回启用后的六类测试用例精确配额。"""
        rules = getattr(task, 'case_type_rules', None) or {}
        if not rules.get('enabled'):
            return {}

        allowed_types = ('functional', 'exception', 'boundary', 'equivalence', 'security', 'compatibility')
        focused_types = {
            item for item in rules.get('focused_types', [])
            if item in allowed_types
        }
        if not focused_types:
            return {}

        try:
            focused_count = max(4, min(50, int(rules.get('focused_count', 12))))
        except (TypeError, ValueError):
            focused_count = 12

        return {
            case_type: focused_count if case_type in focused_types else 3
            for case_type in allowed_types
        }

    @staticmethod
    def get_case_type_quota_instruction(task: TestCaseGenerationTask) -> str:
        """构建贯穿生成、评审和修订阶段的类型配额指令。"""
        targets = AIModelService.get_case_type_quota_targets(task)
        if not targets:
            return ''

        labels = {
            'functional': '功能测试',
            'exception': '异常测试',
            'boundary': '边界值测试',
            'equivalence': '等价类测试',
            'security': '安全测试',
            'compatibility': '兼容测试',
        }
        focus_guidance = {
            'functional': '深入展开业务规则、状态流转、角色权限、数据一致性、主流程和组合路径。',
            'exception': '深入展开空值、非法值、边界值、接口超时、断网、服务错误、并发冲突和重复提交。',
            'boundary': '深入展开数值、长度、金额、日期、次数与并发阈值的临界点和相邻值。',
            'equivalence': '深入展开输入、枚举、角色、状态、渠道和业务规则的有效与无效等价类。',
            'security': '深入展开认证授权、越权、注入、篡改、重放、敏感数据、频率限制和审计。',
            'compatibility': '深入展开浏览器与 iOS/Android 版本、设备/折叠屏、横竖屏、权限、输入法、生命周期、中断和网络切换。',
        }
        rules = getattr(task, 'case_type_rules', None) or {}
        focused_types = set(rules.get('focused_types', []))
        focus_keywords = str(rules.get('focus_keywords') or '').strip()
        quota_lines = '\n'.join(
            f"- {labels[key]}：严格生成 {value} 条"
            for key, value in targets.items()
        )
        guidance_lines = '\n'.join(
            f"- {labels[key]}：{focus_guidance[key]}"
            for key in targets
            if key in focused_types
        )
        keyword_block = (
            f"【用户重点关键词 / 业务规则】\n{focus_keywords}\n"
            if focus_keywords
            else "【用户重点关键词 / 业务规则】\n未单独填写，请从需求描述和上传材料中自动提取核心业务词、字段、状态、接口与约束。\n"
        )

        return (
            "【用例类型精确配额（最高优先级）】\n"
            f"{quota_lines}\n"
            f"- 总用例数必须为 {sum(targets.values())} 条。\n"
            "- 每条用例只能归入一种测试类型，测试类型列必须使用上述六个标准名称之一。\n"
            "- 未选中的非重点类型固定为 3 条，不得擅自扩展；重点类型必须达到指定数量。\n"
            "- 先系统梳理覆盖点，再选择互不重复、可独立执行的场景满足配额。\n"
            "【材料驱动设计规则】\n"
            "- 已选择的测试类型只决定分析视角，具体测试点必须来自用户需求描述和上传材料。\n"
            "- 优先提取材料中的页面控件、字段规则、枚举值、状态流转、角色权限、接口参数、错误码、时序和依赖关系。\n"
            "- 将重点关键词与材料中的具体模块、字段、接口和业务规则建立关联后再展开用例。\n"
            "- 重点关键词是测试设计的种子概念，需要沿实体类型、生命周期、业务状态、时间有效性、权属/资质、关联一致性和接口返回状态进行同类语义延伸。\n"
            "- 对用户列举的示例，不仅逐项覆盖，还要从材料中寻找相邻风险并形成组合场景；无法从材料确认的扩展必须标记为“待业务确认”，不能当作既定规则。\n"
            "- 测试数据和预期结果应尽量引用材料中的真实示例、阈值、状态或错误码；材料未说明时必须明确合理假设，禁止虚构产品规则。\n"
            "- 不得用仅替换数据的通用模板凑数量，每条用例必须验证独立的业务风险或规则。\n"
            f"{keyword_block}"
            "【重点类型展开要求】\n"
            f"{guidance_lines}"
        )

    @staticmethod
    def get_requirement_grounding_context(task: TestCaseGenerationTask, max_chars: int = 20000) -> str:
        """为评审与修订保留需求材料首尾内容，避免大文档完全挤占上下文。"""
        requirement_text = str(getattr(task, 'requirement_text', '') or '').strip()
        if not requirement_text:
            return '未提供需求描述或材料内容。'
        if len(requirement_text) <= max_chars:
            return requirement_text

        head_length = max_chars * 3 // 5
        tail_length = max_chars - head_length
        return (
            requirement_text[:head_length]
            + '\n\n……中间材料因上下文长度已省略，评审时不得据此虚构规则……\n\n'
            + requirement_text[-tail_length:]
        )

    @staticmethod
    def get_knowledge_rule_instruction(task: TestCaseGenerationTask) -> str:
        """构建当前规则与已采纳历史规则的可追溯Prompt上下文。"""
        rules = getattr(task, 'case_type_rules', None) or {}
        focus_keywords = str(rules.get('focus_keywords') or '').strip()
        quota_enabled = bool(AIModelService.get_case_type_quota_targets(task))
        knowledge_rules = getattr(task, 'knowledge_rule_context', None) or []
        blocks = []

        if focus_keywords and not quota_enabled:
            blocks.append(f"【本次用户重点关键词 / 业务规则】\n{focus_keywords}")

        history_lines = []
        for index, rule in enumerate(knowledge_rules[:20], 1):
            title = str(rule.get('title') or f'历史规则{index}').strip()
            content = str(rule.get('content') or '').strip()
            if not content:
                continue
            metadata = [
                str(rule.get('project_name') or '').strip(),
                str(rule.get('module') or '').strip(),
            ]
            source = ' / '.join(item for item in metadata if item) or '全局规则库'
            detail_lines = [f"{index}. {title}（来源：{source}）", f"   规则：{content}"]
            graph_path = ' / '.join(
                item for item in [
                    str(rule.get('business_domain') or '').strip(),
                    str(rule.get('entity_name') or '').strip(),
                    str(rule.get('attribute_name') or '').strip(),
                ]
                if item
            )
            if graph_path:
                detail_lines.append(f"   图谱位置：{graph_path}")
            if rule.get('state_values'):
                detail_lines.append(f"   关键状态：{'、'.join(str(item) for item in rule['state_values'][:12])}")
            if rule.get('relation_nodes'):
                detail_lines.append(f"   关联节点：{'、'.join(str(item) for item in rule['relation_nodes'][:12])}")
            graph_neighbors = rule.get('knowledge_neighbors') or []
            if graph_neighbors:
                relation_lines = []
                seen_relation_lines = set()
                for neighbor in graph_neighbors[:12]:
                    for relation in (neighbor.get('relations') or [])[:6]:
                        line = (
                            f"{relation.get('source_name') or relation.get('source')} "
                            f"-[{relation.get('relation') or '关联'}]-> "
                            f"{relation.get('target_name') or relation.get('target')}"
                        )
                        if line not in seen_relation_lines:
                            seen_relation_lines.add(line)
                            relation_lines.append(line)
                        if len(relation_lines) >= 12:
                            break
                    if len(relation_lines) >= 12:
                        break
                if relation_lines:
                    detail_lines.append(f"   上下游关系：{'；'.join(relation_lines)}")
            if rule.get('test_strategies'):
                detail_lines.append(f"   测试策略：{'、'.join(str(item) for item in rule['test_strategies'][:12])}")
            if rule.get('risk_level'):
                detail_lines.append(f"   风险等级：{rule['risk_level']}")
            if rule.get('applicable_conditions'):
                detail_lines.append(f"   适用条件：{rule['applicable_conditions']}")
            if rule.get('expected_behavior'):
                detail_lines.append(f"   预期行为：{rule['expected_behavior']}")
            if rule.get('exceptions'):
                detail_lines.append(f"   例外：{rule['exceptions']}")
            history_lines.append('\n'.join(detail_lines))
        if history_lines:
            blocks.append("【用户已采纳的历史业务规则】\n" + '\n'.join(history_lines))

        if not blocks:
            return ''
        blocks.append(
            "【规则使用原则】\n"
            "- 当前需求和上传材料的明确说明优先级最高。\n"
            "- 历史规则仅在适用条件匹配时使用，不得脱离当前需求强行套用。\n"
            "- 当前需求与历史规则冲突时，以当前需求为准，并将冲突点标记为“待业务确认”。\n"
            "- 优先围绕图谱中的业务实体、属性、状态和关联节点生成正常、异常、边界、组合和冲突类用例。\n"
            "- 测试步骤和预期结果要明确体现被采纳规则对应的边界、状态、权限或异常行为。"
        )
        return '\n\n'.join(blocks)

    @staticmethod
    def get_skill_instruction(task: TestCaseGenerationTask) -> str:
        """Render a Trae-style slash invocation backed by immutable Skill snapshots."""
        snapshots = getattr(task, 'skill_context', None) or []
        if not snapshots:
            return ''

        primary = snapshots[0]
        command = str(primary.get('command') or primary.get('identifier') or '').strip().lower()
        command = re.sub(r'[^a-z0-9_-]+', '-', command).strip('-_') or 'test-skill'
        if command.endswith('-skill'):
            command = command[:-6]
        blocks = [
            f"/{command}",
            "【Skill 调用协议】",
            f"已通过斜杠命令激活主 Skill：{primary.get('name') or command}。",
            "- 必须以主 Skill 的 SKILL.md 逻辑、分析步骤、检查清单和测试方法作为本次任务的主导思维与执行工作流。",
            "- 主 Skill 后出现的需求资料、用例类型配额、业务知识和工作流均是交给该 Skill 分析的自然语言输入，不得把 Skill 降级为普通参考资料。",
            "- Skill 包中的脚本、依赖和工具会由服务器 Runner 按入口真实执行；文档型 Skill 则由模型按完整工作流执行。",
            "- Runner 已返回的执行结果是当前阶段的真实产物，必须继承，不得假装没有执行后重新从零开始。",
            "- 类型配额与结构化输出必须精确满足；业务事实以当前需求和上传材料为准；分析方法以主 Skill 为准；其余 Skills 作为协同能力。",
        ]
        for index, snapshot in enumerate(snapshots[:8], 1):
            role = '主 Skill' if index == 1 else '协同 Skill'
            blocks.append(
                f"\n### {role} {index}: {snapshot.get('name') or snapshot.get('identifier') or index} "
                f"(v{snapshot.get('version') or '1.0.0'})"
            )
            description = str(snapshot.get('description') or '').strip()
            if description:
                blocks.append(f"说明：{description}")
            for file_item in snapshot.get('files') or []:
                content = str(file_item.get('content') or '').strip()
                if content:
                    blocks.append(f"\n#### {file_item.get('path') or 'SKILL.md'}\n{content}")
        return '\n'.join(blocks)

    @staticmethod
    def get_skill_stage_instruction(task: TestCaseGenerationTask, stage_index: int) -> str:
        """Render one Skill only so each pipeline stage is an independent model call."""
        snapshots = getattr(task, 'skill_context', None) or []
        if not snapshots:
            raise ValueError('AI用例生成必须选择至少一个 Skill 作为主导工作流')
        if stage_index < 0 or stage_index >= len(snapshots):
            raise ValueError('Skill执行顺序超出范围')

        snapshot = snapshots[stage_index]
        command = str(snapshot.get('command') or snapshot.get('identifier') or '').strip().lower()
        command = re.sub(r'[^a-z0-9_-]+', '-', command).strip('-_') or 'test-skill'
        if command.endswith('-skill'):
            command = command[:-6]

        stage_number = stage_index + 1
        blocks = [
            f"/{command}",
            "【Skill 串行执行协议】",
            f"当前执行第 {stage_number}/{len(snapshots)} 个 Skill：{snapshot.get('name') or command}。",
            "- 只执行当前 Skill 的分析逻辑、步骤、检查清单和测试方法，不得混用后续 Skill。",
            "- 第一个 Skill 以原始需求、业务规则和工作流为输入；后续 Skill 必须以上一个 Skill 的完整输出为直接分析输入，并结合原始资料继续深化。",
            "- 当前 Skill 如声明服务器入口，入口脚本、依赖安装、网络请求和文件读写由 Runner 真实执行；模型不得伪造执行日志或产物。",
            "- 用例类型配额、事实来源和结构化输出协议仍需满足。",
            f"\n### 当前 Skill: {snapshot.get('name') or snapshot.get('identifier') or stage_number} "
            f"(v{snapshot.get('version') or '1.0.0'})",
        ]
        description = str(snapshot.get('description') or '').strip()
        if description:
            blocks.append(f"说明：{description}")
        for file_item in snapshot.get('files') or []:
            content = str(file_item.get('content') or '').strip()
            if content:
                blocks.append(f"\n#### {file_item.get('path') or 'SKILL.md'}\n{content}")
        return '\n'.join(blocks)

    @staticmethod
    def build_skill_stage_result(
            task: TestCaseGenerationTask,
            stage_index: int,
            output: str,
            status: str = 'completed',
            error: str = '',
            execution_detail: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        snapshots = getattr(task, 'skill_context', None) or []
        snapshot = snapshots[stage_index]
        previous = snapshots[stage_index - 1] if stage_index > 0 else None
        execution_detail = execution_detail or {}
        execution = snapshot.get('execution') or {}
        result = {
            'order': stage_index + 1,
            'skill_id': snapshot.get('id'),
            'name': snapshot.get('name') or snapshot.get('identifier') or f'Skill {stage_index + 1}',
            'identifier': snapshot.get('identifier') or '',
            'command': snapshot.get('command') or snapshot.get('identifier') or '',
            'input_from': (
                {
                    'order': stage_index,
                    'skill_id': previous.get('id'),
                    'name': previous.get('name') or previous.get('identifier') or f'Skill {stage_index}',
                }
                if previous
                else {'type': 'requirement'}
            ),
            'status': status,
            'output': str(output or ''),
            'error': str(error or ''),
            'execution_mode': execution_detail.get('execution_mode') or execution.get('mode') or 'model',
            'runtime': execution_detail.get('runtime') or execution.get('runtime') or 'model',
            'entrypoint': execution_detail.get('entrypoint') or execution.get('entrypoint') or 'SKILL.md',
        }
        if execution_detail:
            result.update({
                'command_line': execution_detail.get('command') or [],
                'stdout': str(execution_detail.get('stdout') or '')[-12000:],
                'stderr': str(execution_detail.get('stderr') or '')[-12000:],
                'artifacts': execution_detail.get('artifacts') or [],
                'duration_ms': execution_detail.get('duration_ms') or 0,
                'return_code': execution_detail.get('return_code'),
                'package_file_count': execution_detail.get('package_file_count')
                    or snapshot.get('package_file_count') or 0,
            })
        return result

    @staticmethod
    def get_required_skill_instruction(task: TestCaseGenerationTask) -> str:
        snapshots = getattr(task, 'skill_context', None) or []
        if getattr(task, '_skill_pipeline_final_stage_executed', False) and snapshots:
            snapshot = snapshots[-1]
            command = str(snapshot.get('command') or snapshot.get('identifier') or 'test-skill')
            return (
                f"/{command}\n"
                "【服务器 Skill 已实际执行】\n"
                f"{snapshot.get('name') or command} 的入口脚本、依赖和文件资源已经由 Runner 完整执行。"
                "下方执行结果是该 Skill 的真实主输出。请保留其中的有效事实和用例设计，仅将其整理为平台规定的"
                "Markdown 用例明细表，补齐必需字段和类型配额；不得把它当作未执行的普通提示词重新模拟。"
            )
        if getattr(task, '_skill_pipeline_prepared', False) and len(snapshots) > 1:
            instruction = AIModelService.get_skill_stage_instruction(task, len(snapshots) - 1)
        else:
            instruction = AIModelService.get_skill_instruction(task)
        if not instruction:
            raise ValueError('AI用例生成必须选择至少一个 Skill 作为主导工作流')
        return instruction

    @staticmethod
    def get_skill_pipeline_handoff(task: TestCaseGenerationTask) -> str:
        previous_output = str(getattr(task, '_skill_pipeline_previous_output', '') or '').strip()
        if not previous_output:
            return ''
        return (
            "【上一个 Skill 的完整执行结果】\n"
            f"{previous_output}\n\n"
            "【结果传递要求】\n"
            "必须分析并继承上一步中有效的事实、风险、覆盖点和待确认项；修正其遗漏或错误，"
            "禁止忽略该结果后重新从零开始。\n\n"
        )

    @staticmethod
    async def execute_skill_stage_with_platform_model(
            task: TestCaseGenerationTask,
            stage_index: int,
            writer_prompt: str,
            previous_output: str,
    ) -> str:
        """Execute one Skill's SKILL.md through the configured platform writer model."""
        skill_instruction = AIModelService.get_skill_stage_instruction(task, stage_index)
        previous_block = (
            "【上一个 Skill 的完整执行结果】\n"
            f"{previous_output}\n\n"
            "必须以上述结果为直接输入继续分析，继承有效结论并修正遗漏。\n\n"
            if previous_output
            else ''
        )
        user_message = (
            f"{skill_instruction}\n\n"
            f"{previous_block}"
            f"{AIModelService.get_knowledge_rule_instruction(task)}\n\n"
            "【需求资料】\n"
            f"{task.requirement_text}\n\n"
            "【本阶段输出要求】\n"
            "这是 Skill 流水线的中间阶段，结果不会直接作为页面最终输出。请完整执行当前 Skill 自身定义的"
            "职责并输出完整成果：若当前 Skill 要求生成用例，就生成完整用例；若要求分析、评审或排查，就输出"
            "完整报告。结果必须具体、完整、可追溯并可直接交给下一个 Skill，禁止提前执行后续 Skill 的职责。"
        )
        output_chunks = []
        async for chunk in AIModelService.call_openai_compatible_api_stream(
                task.writer_model_config,
                [
                    {'role': 'system', 'content': writer_prompt},
                    {'role': 'user', 'content': user_message},
                ],
        ):
            output_chunks.append(chunk)
        output = ''.join(output_chunks).strip()
        if not output:
            raise Exception('Skill 中间阶段未返回有效内容')
        return output

    @staticmethod
    async def prepare_skill_pipeline(task: TestCaseGenerationTask) -> None:
        """Execute intermediate Skills and any final Skill that declares a server entrypoint."""
        snapshots = getattr(task, 'skill_context', None) or []
        if not snapshots:
            raise ValueError('AI用例生成必须选择至少一个 Skill 作为执行工作流')
        from .skill_runtime import (
            execute_skill_snapshot,
            get_effective_skill_timeout,
            resolve_snapshot_execution,
        )

        final_execution = resolve_snapshot_execution(snapshots[-1])
        final_runs_on_server = final_execution.get('mode') == 'server'
        stage_count = len(snapshots) if final_runs_on_server else len(snapshots) - 1
        if stage_count <= 0:
            task._skill_pipeline_prepared = False
            task._skill_pipeline_previous_output = ''
            task._skill_pipeline_final_stage_executed = False
            task.skill_execution_results = []
            return

        writer_prompt = AIModelService.get_platform_system_prompt('writer')
        previous_output = ''
        results = []
        for stage_index in range(stage_count):
            snapshot = snapshots[stage_index]
            execution = resolve_snapshot_execution(snapshot)
            stage_name = snapshot.get('name') or snapshot.get('identifier') or f'Skill {stage_index + 1}'
            await AIModelService.update_skill_pipeline_progress(
                task, stage_index, len(snapshots)
            )
            is_final_stage = stage_index == len(snapshots) - 1
            stage_message = stage_name
            if execution.get('mode') == 'server':
                effective_timeout = get_effective_skill_timeout(execution)
                stage_message = (
                    f'{stage_name}（最多执行 {effective_timeout} 秒，'
                    '超时或失败将自动切换平台模型）'
                )
            await AIModelService.arecord_generation_event(
                task,
                'skill',
                'running',
                f'执行 Skill {stage_index + 1}/{len(snapshots)}',
                stage_message,
                order=stage_index + 1,
                skill_name=stage_name,
                execution_mode=execution.get('mode') or 'model',
                timeout_seconds=(
                    get_effective_skill_timeout(execution)
                    if execution.get('mode') == 'server'
                    else None
                ),
            )
            if not snapshot.get('execution'):
                snapshot['execution'] = execution
            if previous_output:
                stage_input = (
                    "【上一个 Skill 的完整执行结果】\n"
                    f"{previous_output}\n\n"
                    "【原始需求、业务规则与工作流】\n"
                    f"{task.requirement_text}"
                )
            else:
                stage_input = str(task.requirement_text or '')

            if execution.get('mode') == 'server':
                try:
                    runtime_result = await execute_skill_snapshot(
                        snapshot,
                        stage_input,
                        task.requirement_text,
                        task.writer_model_config,
                    )
                    previous_output = str(runtime_result.get('main_output') or '').strip()
                except Exception as exc:
                    safe_error = AIModelService.sanitize_error_message(exc)
                    await AIModelService.arecord_generation_event(
                        task,
                        'skill',
                        'failed',
                        f'Skill {stage_index + 1} 执行失败',
                        safe_error,
                        order=stage_index + 1,
                        skill_name=stage_name,
                    )
                    if is_final_stage:
                        # A final executable Skill is an optimization, not a
                        # reason to discard completed upstream stages. Execute
                        # the same SKILL.md through the configured platform
                        # model whenever its server entrypoint cannot finish.
                        final_runs_on_server = False
                        await AIModelService.arecord_generation_event(
                            task,
                            'skill',
                            'running',
                            '切换平台模型继续执行当前 Skill',
                            f'服务器 Skill 未完成（{safe_error}），'
                            '已自动使用平台模型和同一份 Skill 指令继续',
                            order=stage_index + 1,
                            skill_name=stage_name,
                            fallback='platform_model',
                        )
                        continue
                    await AIModelService.arecord_generation_event(
                        task,
                        'skill',
                        'running',
                        '切换平台模型继续执行当前 Skill',
                        f'服务器 Skill 未完成（{safe_error}），'
                        '已自动使用平台模型和同一份 Skill 指令继续',
                        order=stage_index + 1,
                        skill_name=stage_name,
                        fallback='platform_model',
                    )
                    try:
                        previous_output = await AIModelService.execute_skill_stage_with_platform_model(
                            task,
                            stage_index,
                            writer_prompt,
                            previous_output,
                        )
                    except Exception as fallback_exc:
                        fallback_error = AIModelService.sanitize_error_message(fallback_exc)
                        await AIModelService.arecord_generation_event(
                            task,
                            'skill',
                            'failed',
                            f'Skill {stage_index + 1} 平台模型回退失败',
                            fallback_error,
                            order=stage_index + 1,
                            skill_name=stage_name,
                            fallback='platform_model',
                        )
                        results.append(AIModelService.build_skill_stage_result(
                            task,
                            stage_index,
                            '',
                            status='failed',
                            error=fallback_error,
                            execution_detail={
                                'execution_mode': 'model',
                                'runtime': 'model',
                                'entrypoint': 'SKILL.md',
                            },
                        ))
                        task.skill_execution_results = results
                        raise Exception(fallback_error) from fallback_exc
                    results.append(AIModelService.build_skill_stage_result(
                        task,
                        stage_index,
                        previous_output,
                        execution_detail={
                            'execution_mode': 'model',
                            'runtime': 'model',
                            'entrypoint': 'SKILL.md',
                        },
                    ))
                    task.skill_execution_results = list(results)
                    await AIModelService.update_skill_pipeline_progress(
                        task, stage_index, len(snapshots), completed=True
                    )
                    await AIModelService.arecord_generation_event(
                        task,
                        'skill',
                        'completed',
                        f'Skill {stage_index + 1} 已完成',
                        f'{stage_name} 已通过平台模型完成，等待下一阶段',
                        order=stage_index + 1,
                        skill_name=stage_name,
                        fallback='platform_model',
                    )
                    continue
                results.append(AIModelService.build_skill_stage_result(
                    task,
                    stage_index,
                    previous_output,
                    execution_detail={
                        **runtime_result,
                        'execution_mode': 'server',
                    },
                ))
                task.skill_execution_results = list(results)
                await AIModelService.update_skill_pipeline_progress(
                    task, stage_index, len(snapshots), completed=True
                )
                await AIModelService.arecord_generation_event(
                    task,
                    'skill',
                    'completed',
                    f'Skill {stage_index + 1} 已完成',
                    f'{stage_name} 输出已准备，等待下一阶段',
                    order=stage_index + 1,
                    skill_name=stage_name,
                )
                continue

            try:
                previous_output = await AIModelService.execute_skill_stage_with_platform_model(
                    task,
                    stage_index,
                    writer_prompt,
                    previous_output,
                )
            except Exception as exc:
                safe_error = AIModelService.sanitize_error_message(exc)
                await AIModelService.arecord_generation_event(
                    task,
                    'skill',
                    'failed',
                    f'Skill {stage_index + 1} 调用模型失败',
                    safe_error,
                    order=stage_index + 1,
                    skill_name=stage_name,
                )
                results.append(AIModelService.build_skill_stage_result(
                    task, stage_index, '', status='failed', error=safe_error
                ))
                task.skill_execution_results = results
                raise Exception(safe_error) from exc
            results.append(AIModelService.build_skill_stage_result(
                task,
                stage_index,
                previous_output,
                execution_detail={
                    'execution_mode': 'model',
                    'runtime': 'model',
                    'entrypoint': 'SKILL.md',
                },
            ))
            task.skill_execution_results = list(results)
            await AIModelService.update_skill_pipeline_progress(
                task, stage_index, len(snapshots), completed=True
            )
            await AIModelService.arecord_generation_event(
                task,
                'skill',
                'completed',
                f'Skill {stage_index + 1} 已完成',
                f'{stage_name} 输出已准备，等待下一阶段',
                order=stage_index + 1,
                skill_name=stage_name,
            )

        task._skill_pipeline_prepared = True
        task._skill_pipeline_previous_output = previous_output
        task._skill_pipeline_final_stage_executed = final_runs_on_server

    @staticmethod
    async def complete_skill_pipeline(task: TestCaseGenerationTask, final_output: str) -> str:
        snapshots = getattr(task, 'skill_context', None) or []
        final_stage_already_recorded = bool(
            getattr(task, '_skill_pipeline_final_stage_executed', False)
        )
        if snapshots and not final_stage_already_recorded:
            results = list(getattr(task, 'skill_execution_results', None) or [])
            results.append(AIModelService.build_skill_stage_result(
                task,
                len(snapshots) - 1,
                final_output,
                execution_detail={
                    'execution_mode': 'model',
                    'runtime': 'model',
                    'entrypoint': 'SKILL.md',
                },
            ))
            task.skill_execution_results = results
            final_snapshot = snapshots[-1]
            final_name = final_snapshot.get('name') or final_snapshot.get('identifier') or f'Skill {len(snapshots)}'
            await AIModelService.update_skill_pipeline_progress(
                task, len(snapshots) - 1, len(snapshots), completed=True
            )
            await AIModelService.arecord_generation_event(
                task,
                'skill',
                'completed',
                f'Skill {len(snapshots)} 已完成',
                f'{final_name} 已通过平台模型完成输出',
                order=len(snapshots),
                skill_name=final_name,
            )
        task._skill_pipeline_prepared = False
        task._skill_pipeline_previous_output = ''
        task._skill_pipeline_final_stage_executed = False
        return final_output

    @staticmethod
    def apply_case_type_quotas(task: TestCaseGenerationTask, test_cases_content: str) -> str:
        """按类型配额裁剪 Markdown 表格中超出的用例，防止模型突破数量规则。"""
        targets = AIModelService.get_case_type_quota_targets(task)
        if not targets or not test_cases_content:
            return test_cases_content

        labels = {
            'functional': '功能测试',
            'exception': '异常测试',
            'boundary': '边界值测试',
            'equivalence': '等价类测试',
            'security': '安全测试',
            'compatibility': '兼容测试',
        }

        def parse_row(line: str) -> List[str]:
            normalized = line.strip().strip('*').strip()
            if not normalized.startswith('|') or not normalized.endswith('|'):
                return []
            return [cell.strip().strip('*').strip() for cell in normalized.strip('|').split('|')]

        def normalize_type(value: str) -> str:
            normalized = str(value or '').lower()
            if '兼容' in normalized or 'compatib' in normalized:
                return 'compatibility'
            if '安全' in normalized or 'security' in normalized:
                return 'security'
            if '等价类' in normalized or 'equivalence' in normalized or 'partition' in normalized:
                return 'equivalence'
            if '边界' in normalized or 'boundary' in normalized or 'limit' in normalized:
                return 'boundary'
            if '异常' in normalized or 'exception' in normalized or 'error' in normalized:
                return 'exception'
            if '功能' in normalized or 'functional' in normalized:
                return 'functional'
            return ''

        lines = test_cases_content.split('\n')
        header_index = -1
        type_column_index = -1
        column_count = 0
        for index, line in enumerate(lines):
            cells = parse_row(line)
            for cell_index, cell in enumerate(cells):
                if '测试类型' in cell or 'test type' in cell.lower() or 'testtype' in cell.lower():
                    header_index = index
                    type_column_index = cell_index
                    column_count = len(cells)
                    break
            if header_index >= 0:
                break

        if header_index < 0:
            logger.warning('类型配额未执行：未找到测试类型表头')
            return test_cases_content

        counts = {key: 0 for key in targets}
        result_lines = []
        for index, line in enumerate(lines):
            if index <= header_index:
                result_lines.append(line)
                continue

            cells = parse_row(line)
            if len(cells) != column_count or type_column_index >= len(cells):
                result_lines.append(line)
                continue

            case_type = normalize_type(cells[type_column_index])
            if not case_type:
                result_lines.append(line)
                continue

            if counts[case_type] >= targets[case_type]:
                continue

            counts[case_type] += 1
            result_lines.append(line)

        shortages = {
            labels[key]: targets[key] - counts[key]
            for key in targets
            if counts[key] < targets[key]
        }
        if shortages:
            logger.warning(f'模型生成用例未达到类型配额: {shortages}')
        else:
            logger.info(f'已严格应用用例类型配额: {counts}')

        return '\n'.join(result_lines)

    @staticmethod
    async def call_deepseek_api(config: AIModelConfig, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """调用DeepSeek API (兼容OpenAI格式)"""
        return await AIModelService.call_openai_compatible_api(config, messages)

    @staticmethod
    async def call_qwen_api(config: AIModelConfig, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """调用千问API (兼容OpenAI格式)"""
        return await AIModelService.call_openai_compatible_api(config, messages)

    @staticmethod
    async def call_openai_compatible_api_stream(
            config: AIModelConfig,
            messages: List[Dict[str, str]],
            callback=None,
            max_tokens: int = None,
            max_retries: int = None,
            read_timeout_seconds: float = None,
            max_continuations: int = None,
            allow_stream_recovery: bool = True,
    ) -> AsyncIterator[str]:
        """
        流式调用OpenAI兼容格式的API，支持自动续写
        """
        api_key = AIModelService.resolve_api_key(config)
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }

        actual_max_tokens = AIModelService.resolve_max_output_tokens(config, max_tokens)

        # 确保base_url不以/结尾
        base_url = config.base_url.rstrip('/')
        if not base_url.endswith('/chat/completions'):
            # 检查是否已经包含版本号（如v1, v4等）
            import re
            version_match = re.search(r'/v(\d+)/?$', base_url)
            if version_match:
                # 如果已经以版本号结尾（如/v1, /v4），直接添加/chat/completions
                url = f"{base_url}/chat/completions"
            else:
                # 默认假设是根路径，尝试添加 v1/chat/completions
                url = f"{base_url}/v1/chat/completions"
        else:
            url = base_url

        # 续写控制
        current_messages = list(messages)  # 浅拷贝
        continuation_count = 0
        effective_max_continuations = (
            5 if max_continuations is None else max(0, int(max_continuations))
        )
        effective_max_retries = (
            AIModelService.resolve_retry_count(config)
            if max_retries is None
            else max(0, int(max_retries))
        )
        read_timeout = 900.0 if read_timeout_seconds is None else max(1.0, float(read_timeout_seconds))

        def extract_response_content(payload):
            """Extract text from streaming or non-streaming OpenAI-compatible payloads."""
            if not isinstance(payload, dict):
                return '', None
            choices = payload.get('choices') or []
            if not choices or not isinstance(choices[0], dict):
                return '', None
            choice = choices[0]
            delta = choice.get('delta') or {}
            message = choice.get('message') or {}
            content = delta.get('content') or message.get('content') or choice.get('text') or ''
            if isinstance(content, list):
                content = ''.join(
                    str(item.get('text') or '') if isinstance(item, dict) else str(item or '')
                    for item in content
                )
            return str(content or ''), choice.get('finish_reason')

        while continuation_count <= effective_max_continuations:
            data = {
                'model': config.model_name,
                'messages': current_messages,
                'max_tokens': actual_max_tokens,
                'temperature': config.temperature,
                'top_p': config.top_p,
                'stream': True
            }

            logger.info(f"发起流式请求 (第{continuation_count + 1}次), messages数量: {len(current_messages)}")

            request_attempt = 0
            request_completed = False
            resume_after_disconnect = False
            chunk_content_buffer = ""  # 本次请求生成的完整内容缓存
            finish_reason = None

            while request_attempt <= effective_max_retries:
                chunk_content_buffer = ""
                finish_reason = None
                saw_done = False
                non_sse_lines = []

                try:
                    from apps.core.outbound import validate_outbound_http_url

                    request_url = validate_outbound_http_url(url, label='AI API 地址')
                    # 显式设置所有超时参数
                    timeout_config = httpx.Timeout(
                        connect=60.0,  # 连接超时：60秒
                        read=read_timeout,
                        write=60.0,  # 写入超时：60秒
                        pool=60.0  # 连接池超时：60秒
                    )
                    async with httpx.AsyncClient(timeout=timeout_config, http2=False) as client:
                        logger.info(
                            f"发送流式POST请求到: {request_url} "
                            f"(尝试 {request_attempt + 1}/{effective_max_retries + 1})"
                        )
                        async with client.stream('POST', request_url, headers=headers, json=data) as response:
                            if response.status_code != 200:
                                error_detail = await response.aread()
                                error_msg = AIModelService.format_api_error(
                                    config.get_model_type_display(),
                                    response.status_code,
                                    error_detail.decode('utf-8', errors='replace'),
                                )
                                logger.error(error_msg)
                                retryable = (
                                    response.status_code >= 500
                                    or response.status_code in {408, 409, 425}
                                )
                                raise AIModelRequestError(
                                    error_msg,
                                    status_code=response.status_code,
                                    retryable=retryable,
                                )

                            async for line in response.aiter_lines():
                                stripped_line = line.strip()
                                if not stripped_line:
                                    continue

                                if stripped_line.startswith('data:'):
                                    data_str = stripped_line[5:].lstrip()
                                    if data_str.strip() == '[DONE]':
                                        saw_done = True
                                        break

                                    try:
                                        chunk_data = json.loads(data_str)
                                        content, payload_finish_reason = extract_response_content(chunk_data)
                                        finish_reason = payload_finish_reason or finish_reason
                                        if content:
                                            chunk_content_buffer += content
                                            if callback:
                                                await callback(content)
                                            yield content
                                    except json.JSONDecodeError:
                                        continue
                                else:
                                    # Some OpenAI-compatible gateways ignore stream=true and
                                    # return one regular JSON response. Preserve the body and
                                    # parse it after the iterator finishes instead of silently
                                    # accepting an empty stream.
                                    non_sse_lines.append(stripped_line)

                            if not chunk_content_buffer and non_sse_lines:
                                raw_payload = '\n'.join(non_sse_lines)
                                try:
                                    regular_payload = json.loads(raw_payload)
                                except json.JSONDecodeError:
                                    regular_payload = None
                                content, payload_finish_reason = extract_response_content(regular_payload)
                                finish_reason = payload_finish_reason or finish_reason
                                if content:
                                    logger.warning(
                                        '模型网关返回普通 JSON 而非 SSE，已自动兼容解析 %s 个字符',
                                        len(content),
                                    )
                                    chunk_content_buffer += content
                                    if callback:
                                        await callback(content)
                                    yield content

                            if not chunk_content_buffer:
                                logger.warning('流式接口未返回内容，自动切换非流式请求')
                                fallback_result = await AIModelService.call_openai_compatible_api(
                                    config,
                                    current_messages,
                                    max_tokens=actual_max_tokens,
                                    max_retries=effective_max_retries,
                                    read_timeout_seconds=read_timeout,
                                )
                                content, payload_finish_reason = extract_response_content(fallback_result)
                                if not content:
                                    raise Exception('AI模型流式和非流式请求均未返回有效内容')
                                finish_reason = payload_finish_reason or finish_reason
                                chunk_content_buffer += content
                                if callback:
                                    await callback(content)
                                yield content

                    request_completed = True
                    break
                except Exception as e:
                    if isinstance(e, AIModelRequestError) and not e.retryable:
                        logger.error(f"流式请求不可重试: {e}")
                        raise

                    if saw_done or finish_reason:
                        logger.warning(
                            "流式响应已收到结束标记，但连接关闭不完整，按有效响应继续: %s",
                            e,
                        )
                        request_completed = True
                        break

                    if chunk_content_buffer and allow_stream_recovery:
                        if continuation_count >= effective_max_continuations:
                            logger.error(
                                "流式响应已达到最大续传次数 %s，最后一次中断于 %s 个字符: %s",
                                effective_max_continuations,
                                len(chunk_content_buffer),
                                e,
                            )
                            raise Exception(
                                'AI模型流式连接多次中断，已达到自动续传上限，请稍后重试'
                            ) from e
                        logger.warning(
                            "流式响应输出 %s 个字符后中断，准备发起流式断点续传: %s",
                            len(chunk_content_buffer),
                            e,
                        )
                        current_messages = AIModelService.build_stream_recovery_messages(
                            current_messages,
                            chunk_content_buffer,
                        )
                        continuation_count += 1
                        resume_after_disconnect = True
                        request_completed = True
                        logger.info(
                            "已保留本次 %s 个字符，开始第 %s/%s 次流式续传",
                            len(chunk_content_buffer),
                            continuation_count,
                            effective_max_continuations,
                        )
                        break

                    if request_attempt >= effective_max_retries:
                        if not chunk_content_buffer and allow_stream_recovery:
                            logger.warning(
                                '流式请求在收到内容前多次中断，自动切换非流式请求: %s',
                                e,
                            )
                            fallback_result = await AIModelService.call_openai_compatible_api(
                                config,
                                current_messages,
                                max_tokens=actual_max_tokens,
                                max_retries=effective_max_retries,
                                read_timeout_seconds=read_timeout,
                            )
                            content, payload_finish_reason = extract_response_content(fallback_result)
                            if not content:
                                raise Exception('AI模型流式和非流式请求均未返回有效内容')
                            finish_reason = payload_finish_reason or finish_reason
                            chunk_content_buffer += content
                            if callback:
                                await callback(content)
                            yield content
                            request_completed = True
                            break
                        logger.error(f"流式请求异常: {e}")
                        raise e

                    request_attempt += 1
                    logger.warning(f"流式请求异常且尚未输出内容: {e}，准备重试")
                    await asyncio.sleep(min(2 ** (request_attempt - 1), 5))

            if not request_completed:
                raise Exception("流式请求失败，请稍后再试")

            if resume_after_disconnect:
                continue

            # 本次请求结束，检查 finish_reason
            if finish_reason == 'length':
                logger.warning(
                    f"检测到生成被截断 (finish_reason='length')，准备自动续写。当前已续写 {continuation_count} 次。")
                continuation_count += 1

                # 将本次生成的内容作为 assistant 回复加入历史
                # 注意：如果之前已经有assistant消息，需要追加内容而不是新增消息
                if current_messages[-1]['role'] == 'assistant':
                    current_messages[-1]['content'] += chunk_content_buffer
                else:
                    current_messages.append({"role": "assistant", "content": chunk_content_buffer})

                # 只有当上一条不是user的续写指令时，才添加新的user指令
                # 防止多次续写时堆叠重复的 user 指令
                if current_messages[-1]['role'] != 'user':
                    current_messages.append(
                        {"role": "user", "content": "请继续输出剩余的内容，不要重复已输出的部分，紧接着上文继续。"})

                # 发送换行符以分隔续写内容（可选，视模型而定，通常不需要，但为了保险）
                # yield "\n"
                continue

            logger.info(f"流式生成正常结束 (finish_reason={finish_reason})")
            break

    @staticmethod
    async def generate_test_cases(task: TestCaseGenerationTask) -> str:
        """生成测试用例"""
        await AIModelService.prepare_skill_pipeline(task)
        messages = AIModelService.build_test_case_generation_messages(task)

        # 所有支持的模型都使用兼容OpenAI的接口
        # 使用配置的max_tokens，不硬编码限制
        response = await AIModelService.call_openai_compatible_api(
            task.writer_model_config,
            messages
            # 不再硬编码max_tokens，使用配置文件中的值（如32000）
        )

        generated_content = response['choices'][0]['message']['content']
        generated_content = AIModelService.apply_case_type_quotas(task, generated_content)
        return await AIModelService.complete_skill_pipeline(task, generated_content)

    @staticmethod
    async def review_test_cases(
            task: TestCaseGenerationTask,
            test_cases: str,
            timeout_seconds: float = 60,
    ) -> str:
        """评审测试用例"""
        try:
            reviewer_prompt = AIModelService.get_task_system_prompt(task, 'reviewer')
            quota_instruction = AIModelService.get_case_type_quota_instruction(task)
            quota_block = f'{quota_instruction}\n\n' if quota_instruction else ''
            knowledge_rule_instruction = AIModelService.get_knowledge_rule_instruction(task)
            knowledge_rule_block = f'{knowledge_rule_instruction}\n\n' if knowledge_rule_instruction else ''
            skill_instruction = AIModelService.get_required_skill_instruction(task)
            grounding_block = f"【需求描述与上传材料依据】\n{AIModelService.get_requirement_grounding_context(task)}\n\n"
            business_design_block = f'{AIModelService.get_business_test_design_instruction()}\n\n'
            traceability_block = f'{AIModelService.get_source_traceability_instruction(task)}\n\n'
            scoring_rubric_block = f'{AIModelService.get_review_scoring_rubric()}\n\n'
            best_score = getattr(task, 'best_review_score', None)
            if best_score is None:
                best_score = AIModelService.get_task_review_score(task)
            comparison_block = ''
            if getattr(task, 'review_round', 0) and best_score is not None:
                previous_feedback = str(getattr(task, 'review_feedback', '') or '')[:12000]
                comparison_block = (
                    "【上一轮已接受基线】\n"
                    f"历史最高分：{best_score}/100\n"
                    f"上轮评审意见：\n{previous_feedback}\n"
                    "请使用完全相同的量表对候选用例做增量对比。"
                    "若上轮有效覆盖未丢失且扣分项已改善，分数不得因评分尺度漂移而下降；"
                    "只有明确指出具体用例退化或覆盖丢失时才允许低于基线分。\n\n"
                )

            # 增强的评审指令
            user_message = (
                f"{skill_instruction}\n\n"
                f"【交给主 Skill 的自然语言输入】\n"
                f"请按照主 Skill 的方法，对以下规则、需求依据和测试用例进行严格的专家级评审。\n\n"
                f"{quota_block}"
                f"{knowledge_rule_block}"
                f"{business_design_block}"
                f"{traceability_block}"
                f"{scoring_rubric_block}"
                f"{comparison_block}"
                f"【评审重点】\n"
                f"1. **覆盖率漏洞**：请仔细比对用例集是否覆盖了常见的异常场景（如断网、超时、数据冲突）和边界条件。\n"
                f"2. **逻辑严密性**：检查预期结果是否具体、可验证（例如'提示错误'是不够的，需说明具体错误码或文案）。\n"
                f"3. **冗余检查**：指出是否有重复或无效的用例。\n"
                f"4. **配额检查**：如启用了类型配额，必须逐类统计数量，并指出缺少或超出的类型。\n"
                f"5. **材料一致性检查**：重点用例必须能对应需求描述、上传材料或用户关键词，指出脱离材料、重复套模板或虚构规则的用例。\n\n"
                f"{grounding_block}"
                f"【待评审用例】\n{test_cases}\n\n"
                f"【输出格式要求】\n"
                f"{AIModelService.get_review_output_contract()}\n"
                f"**重要**：输出格式要求紧凑，不要在段落之间添加多余的空行，每个问题点之间用单空行分隔即可，用例展示仍为markdown形式。"
            )

            messages = [
                {"role": "system", "content": reviewer_prompt},
                {"role": "user", "content": user_message}
            ]

            # 所有支持的模型都使用兼容OpenAI的接口
            response = await AIModelService.call_openai_compatible_api(
                task.reviewer_model_config,
                messages,
                max_retries=0,
                read_timeout_seconds=timeout_seconds,
            )

            return response['choices'][0]['message']['content']
        except Exception as e:
            logger.error(f"评审测试用例时出错: {e}")
            raise

    @staticmethod
    async def generate_test_cases_stream(
            task: TestCaseGenerationTask,
            callback=None
    ) -> str:
        """
        流式生成测试用例

        Args:
            task: 生成任务对象
            callback: 可选的回调函数，每收到一个chunk就调用，用于实时保存到数据库

        Returns:
            str: 完整的测试用例内容
        """
        await AIModelService.prepare_skill_pipeline(task)
        messages = AIModelService.build_test_case_generation_messages(task)

        # 流式调用API，确保正确关闭生成器
        # 使用配置的max_tokens，不硬编码限制
        generator = AIModelService.call_openai_compatible_api_stream(
            task.writer_model_config,
            messages,
            callback=callback
            # 不再硬编码max_tokens，使用配置文件中的值（如32000）
        )

        full_content = ""
        chunk_count = 0
        try:
            async for chunk in generator:
                full_content += chunk
                chunk_count += 1
        except Exception as e:
            logger.error(f"流式生成测试用例时出错: {e}")
            raise
        finally:
            # 确保生成器被正确关闭
            try:
                await generator.aclose()
            except Exception as close_error:
                logger.warning(f"关闭generator时出错: {close_error}")

        logger.info(f"流式生成完成: 总chunk数={chunk_count}, 总字符数={len(full_content)}")

        if not full_content.strip():
            raise Exception('AI模型未返回有效的测试用例内容')

        # 统计生成的用例数量
        case_count = full_content.count('TC-') + full_content.count('TEST-') + full_content.count('测试用例')
        logger.info(f"生成用例统计: 约检测到{case_count}个用例编号标记")

        full_content = AIModelService.apply_case_type_quotas(task, full_content)
        return await AIModelService.complete_skill_pipeline(task, full_content)

    @staticmethod
    async def review_test_cases_stream(
            task: TestCaseGenerationTask,
            test_cases: str,
            callback=None,
            timeout_seconds: float = 60,
    ) -> str:
        """
        流式评审测试用例

        Args:
            task: 生成任务对象
            test_cases: 待评审的测试用例
            callback: 可选的回调函数，每收到一个chunk就调用

        Returns:
            str: 完整的评审反馈
        """
        reviewer_prompt = AIModelService.get_task_system_prompt(task, 'reviewer')
        quota_instruction = AIModelService.get_case_type_quota_instruction(task)
        quota_block = f'{quota_instruction}\n\n' if quota_instruction else ''
        knowledge_rule_instruction = AIModelService.get_knowledge_rule_instruction(task)
        knowledge_rule_block = f'{knowledge_rule_instruction}\n\n' if knowledge_rule_instruction else ''
        skill_instruction = AIModelService.get_required_skill_instruction(task)
        grounding_block = f"【需求描述与上传材料依据】\n{AIModelService.get_requirement_grounding_context(task)}\n\n"
        business_design_block = f'{AIModelService.get_business_test_design_instruction()}\n\n'
        traceability_block = f'{AIModelService.get_source_traceability_instruction(task)}\n\n'
        scoring_rubric_block = f'{AIModelService.get_review_scoring_rubric()}\n\n'

        # 增强的评审指令
        user_message = (
            f"{skill_instruction}\n\n"
            f"【交给主 Skill 的自然语言输入】\n"
            f"请按照主 Skill 的方法，对以下规则、需求依据和测试用例进行严格的专家级评审。\n\n"
            f"{quota_block}"
            f"{knowledge_rule_block}"
            f"{business_design_block}"
            f"{traceability_block}"
            f"{scoring_rubric_block}"
            f"【评审重点】\n"
            f"1. **覆盖率漏洞**：请仔细比对用例集是否覆盖了常见的异常场景（如断网、超时、数据冲突）和边界条件。\n"
            f"2. **逻辑严密性**：检查预期结果是否具体、可验证（例如'提示错误'是不够的，需说明具体错误码或文案）。\n"
            f"3. **冗余检查**：指出是否有重复或无效的用例。\n"
            f"4. **配额检查**：如启用了类型配额，必须逐类统计数量，并指出缺少或超出的类型。\n"
            f"5. **材料一致性检查**：重点用例必须能对应需求描述、上传材料或用户关键词，指出脱离材料、重复套模板或虚构规则的用例。\n\n"
            f"{grounding_block}"
            f"【待评审用例】\n{test_cases}\n\n"
            f"【输出格式要求】\n"
            f"{AIModelService.get_review_output_contract()}\n"
            f"**重要**：输出格式要求紧凑，不要在段落之间添加多余的空行，每个问题点之间用单空行分隔即可，用例展示仍为markdown形式。"
        )

        messages = [
            {"role": "system", "content": reviewer_prompt},
            {"role": "user", "content": user_message}
        ]

        # 流式调用API，确保正确关闭生成器
        generator = AIModelService.call_openai_compatible_api_stream(
            task.reviewer_model_config,
            messages,
            callback=callback,
            max_retries=0,
            read_timeout_seconds=timeout_seconds,
            max_continuations=0,
            allow_stream_recovery=False,
        )

        full_content = ""
        chunk_count = 0
        try:
            async for chunk in generator:
                full_content += chunk
                chunk_count += 1
        except Exception as e:
            logger.error(f"流式评审测试用例时出错: {e}")
            raise
        finally:
            # 确保生成器被正确关闭
            try:
                await generator.aclose()
            except Exception as close_error:
                logger.warning(f"关闭generator时出错: {close_error}")

        logger.info(f"流式评审完成: 总chunk数={chunk_count}, 总字符数={len(full_content)}")
        if not full_content.strip():
            raise Exception('AI评审模型未返回有效内容')
        return full_content

    @staticmethod
    def sort_test_cases_by_id(test_cases_content: str) -> str:
        """
        按照测试用例编号排序测试用例内容

        Args:
            test_cases_content: 测试用例内容（字符串）

        Returns:
            str: 排序后的测试用例内容
        """
        if not test_cases_content:
            return test_cases_content

        lines = test_cases_content.split('\n')
        table = AIModelService.find_test_case_table(lines)
        if not table:
            logger.info('未检测到测试用例明细表，保持原顺序')
            return test_cases_content

        data_start, data_end = table['data_start'], table['data_end']
        data_rows = lines[data_start:data_end]

        def extract_case_id(row):
            cells = AIModelService.parse_markdown_table_row(row)
            first_cell = cells[0] if cells else ''
            match = re.search(r'(\d+)(?!.*\d)', first_cell)
            if match:
                return int(match.group(1))
            return float('inf')

        try:
            data_rows.sort(key=extract_case_id)
            logger.info(f"成功对{len(data_rows)}个测试用例按编号排序")
        except Exception as e:
            logger.warning(f"排序失败: {e}，保持原顺序")
            return test_cases_content

        return '\n'.join(lines[:data_start] + data_rows + lines[data_end:])

    @staticmethod
    def parse_markdown_table_row(line: str) -> List[str]:
        normalized = str(line or '').strip()
        if not normalized.startswith('|') or not normalized.endswith('|'):
            return []
        return [cell.strip() for cell in normalized.strip('|').split('|')]

    @staticmethod
    def find_test_case_table(lines: List[str]) -> Optional[Dict[str, int]]:
        """Locate the actual testcase detail table, ignoring analysis/summary tables."""
        required_headers = ('用例编号', '测试场景', '操作步骤', '预期结果')
        for header_index, line in enumerate(lines):
            headers = AIModelService.parse_markdown_table_row(line)
            normalized_headers = [header.lower().replace(' ', '') for header in headers]
            if not headers or not all(
                    any(required.lower().replace(' ', '') in header for header in normalized_headers)
                    for required in required_headers
            ):
                continue

            separator_index = header_index + 1
            if separator_index >= len(lines):
                continue
            separator_cells = AIModelService.parse_markdown_table_row(lines[separator_index])
            if len(separator_cells) != len(headers) or not all(
                    re.fullmatch(r':?-{3,}:?', cell.replace(' ', ''))
                    for cell in separator_cells
            ):
                continue

            data_start = separator_index + 1
            data_end = data_start
            while data_end < len(lines):
                row = AIModelService.parse_markdown_table_row(lines[data_end])
                if len(row) != len(headers):
                    break
                data_end += 1

            if data_end > data_start:
                return {
                    'header_index': header_index,
                    'separator_index': separator_index,
                    'data_start': data_start,
                    'data_end': data_end,
                    'column_count': len(headers),
                }
        return None

    @staticmethod
    def fix_incomplete_last_case(test_cases_content: str) -> str:
        """
        检测并修复不完整的最后一条测试用例

        Args:
            test_cases_content: 测试用例内容

        Returns:
            str: 修复后的测试用例内容
        """
        if not test_cases_content:
            return test_cases_content

        lines = test_cases_content.split('\n')

        # 检查最后几行，找到最后一个表格行
        table_lines = []
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i].strip()
            if line.startswith('|') and line.endswith('|'):
                table_lines.insert(0, (i, line))
                # 只检查最后10行
                if len(table_lines) >= 10:
                    break

        if not table_lines:
            return test_cases_content

        # 检查最后一个表格行是否完整（应该有7个|，即7列）
        last_line_index, last_line = table_lines[-1]
        column_count = last_line.count('|')

        # 正常的表格应该有7个|（开头+结尾+5个分隔符）
        if column_count < 7:
            logger.warning(f"检测到最后一条用例不完整: 只有{column_count}列，应该是7列")
            # 删除不完整的最后一条用例
            # 找到完整的上一条用例
            for i in range(len(table_lines) - 2, -1, -1):
                prev_index, prev_line = table_lines[i]
                if prev_line.count('|') >= 7:
                    # 截断到上一条完整用例的位置
                    fixed_content = '\n'.join(lines[:prev_index + 1])
                    logger.info(f"已删除不完整的最后一条用例，保留了{prev_index + 1}行")
                    return fixed_content

            # 如果找不到完整的上一条，直接删除最后5行
            fixed_content = '\n'.join(lines[:-5])
            logger.info(f"删除最后5行不完整的内容")
            return fixed_content

        return test_cases_content

    @staticmethod
    def renumber_test_cases(test_cases_content: str) -> str:
        """
        重新编号测试用例，使其编号连续

        Args:
            test_cases_content: 测试用例内容（字符串）

        Returns:
            str: 重新编号后的测试用例内容
        """
        if not test_cases_content:
            return test_cases_content

        lines = test_cases_content.split('\n')
        table = AIModelService.find_test_case_table(lines)
        if not table:
            logger.warning("未找到测试用例明细表，无法重新编号")
            return test_cases_content

        data_start, data_end = table['data_start'], table['data_end']
        first_cells = AIModelService.parse_markdown_table_row(lines[data_start])
        first_id = first_cells[0] if first_cells else ''
        id_match = re.match(r'^(.*?)(\d+)$', first_id)
        if not id_match:
            logger.warning(f"无法识别编号格式: {first_id}")
            return test_cases_content

        prefix = id_match.group(1)
        width = max(3, len(id_match.group(2)))
        result_lines = list(lines)
        for index, line_index in enumerate(range(data_start, data_end), 1):
            line = lines[line_index]
            parts = line.split('|')
            if len(parts) >= 2:
                parts[1] = f" {prefix}{index:0{width}d} "
                result_lines[line_index] = '|'.join(parts)

        renumbered_content = '\n'.join(result_lines)
        total_cases = data_end - data_start
        logger.info(
            f"重新编号完成: 共{total_cases}条测试用例，"
            f"编号范围: {prefix}{1:0{width}d}-{prefix}{total_cases:0{width}d}"
        )

        return renumbered_content
