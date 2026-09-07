from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.projects.models import Project


class AgentTask(models.Model):
    """跨模块 AI Test Agent 任务。"""

    STATUS_CREATED = 'CREATED'
    STATUS_ANALYZING = 'ANALYZING'
    STATUS_GENERATING_DATA = 'GENERATING_DATA'
    STATUS_GENERATING_CASE = 'GENERATING_CASE'
    STATUS_RUNNING = 'RUNNING'
    STATUS_ANALYZING_RESULT = 'ANALYZING_RESULT'
    STATUS_NEEDS_INPUT = 'NEEDS_INPUT'
    STATUS_COMPLETED = 'COMPLETED'
    STATUS_FAILED = 'FAILED'
    STATUS_STOPPED = 'STOPPED'

    STATUS_CHOICES = [
        (STATUS_CREATED, '已创建'),
        (STATUS_ANALYZING, '需求分析中'),
        (STATUS_GENERATING_DATA, '生成测试数据'),
        (STATUS_GENERATING_CASE, '生成测试用例'),
        (STATUS_RUNNING, '执行测试中'),
        (STATUS_ANALYZING_RESULT, '结果分析中'),
        (STATUS_NEEDS_INPUT, '待补充执行条件'),
        (STATUS_COMPLETED, '已完成'),
        (STATUS_FAILED, '失败'),
        (STATUS_STOPPED, '已停止'),
    ]

    task_name = models.CharField(max_length=200, verbose_name='任务名称')
    user_requirement = models.TextField(verbose_name='用户需求')
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_CREATED, db_index=True, verbose_name='状态')
    progress = models.PositiveSmallIntegerField(default=0, verbose_name='进度')
    current_step = models.CharField(max_length=300, blank=True, default='', verbose_name='当前步骤')
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, blank=True, related_name='agent_tasks', verbose_name='关联项目')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='agent_tasks', verbose_name='创建人')
    plan = models.JSONField(default=list, blank=True, verbose_name='执行计划')
    test_plan = models.JSONField(default=dict, blank=True, verbose_name='测试计划')
    context = models.JSONField(default=dict, blank=True, verbose_name='运行上下文')
    failure_analysis = models.JSONField(default=dict, blank=True, verbose_name='失败分析')
    error_message = models.TextField(blank=True, default='', verbose_name='错误信息')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='结束时间')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'agent_tasks'
        verbose_name = 'AI Agent任务'
        verbose_name_plural = 'AI Agent任务'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['created_by', '-created_at']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return self.task_name


class AgentStep(models.Model):
    """Agent 执行步骤。"""

    STATUS_PENDING = 'PENDING'
    STATUS_RUNNING = 'RUNNING'
    STATUS_SUCCESS = 'SUCCESS'
    STATUS_FAILED = 'FAILED'
    STATUS_SKIPPED = 'SKIPPED'
    STATUS_NOT_APPLICABLE = 'NOT_APPLICABLE'

    STATUS_CHOICES = [
        (STATUS_PENDING, '等待中'),
        (STATUS_RUNNING, '执行中'),
        (STATUS_SUCCESS, '成功'),
        (STATUS_FAILED, '失败'),
        (STATUS_SKIPPED, '跳过'),
        (STATUS_NOT_APPLICABLE, '不适用'),
    ]

    task = models.ForeignKey(AgentTask, on_delete=models.CASCADE, related_name='steps', verbose_name='任务')
    step = models.PositiveIntegerField(verbose_name='步骤序号')
    step_type = models.CharField(max_length=32, db_index=True, verbose_name='步骤类型')
    action = models.CharField(max_length=300, verbose_name='动作')
    tool_name = models.CharField(max_length=80, blank=True, default='', verbose_name='工具名称')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True, verbose_name='状态')
    input_payload = models.JSONField(default=dict, blank=True, verbose_name='输入')
    output_payload = models.JSONField(default=dict, blank=True, verbose_name='输出')
    error_message = models.TextField(blank=True, default='', verbose_name='错误信息')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='结束时间')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'agent_steps'
        verbose_name = 'AI Agent步骤'
        verbose_name_plural = 'AI Agent步骤'
        ordering = ['step', 'id']
        unique_together = ['task', 'step']
        indexes = [
            models.Index(fields=['task', 'status']),
            models.Index(fields=['step_type']),
        ]

    def __str__(self):
        return f'{self.task_id}#{self.step} {self.action}'


class AgentMemory(models.Model):
    """Agent 记忆与事件，用于审计、复盘和后续任务上下文。"""

    MEMORY_CHOICES = [
        ('event', '事件'),
        ('plan', '计划'),
        ('tool_result', '工具结果'),
        ('analysis', '分析'),
        ('system_knowledge', '系统知识'),
        ('business_flow', '业务流程'),
        ('api_knowledge', '接口知识'),
        ('database_knowledge', '数据库知识'),
        ('security_knowledge', '安全知识'),
    ]

    task = models.ForeignKey(AgentTask, on_delete=models.CASCADE, null=True, blank=True, related_name='memories', verbose_name='任务')
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, blank=True, related_name='agent_memories', verbose_name='关联项目')
    step = models.ForeignKey(AgentStep, on_delete=models.SET_NULL, null=True, blank=True, related_name='memories', verbose_name='步骤')
    memory_type = models.CharField(max_length=32, choices=MEMORY_CHOICES, default='event', verbose_name='类型')
    role = models.CharField(max_length=32, default='agent', verbose_name='角色')
    content = models.TextField(verbose_name='内容')
    payload = models.JSONField(default=dict, blank=True, verbose_name='结构化内容')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')

    class Meta:
        db_table = 'agent_memory'
        verbose_name = 'AI Agent记忆'
        verbose_name_plural = 'AI Agent记忆'
        ordering = ['created_at', 'id']
        indexes = [
            models.Index(fields=['project', 'memory_type']),
            models.Index(fields=['task', 'memory_type']),
            models.Index(fields=['created_at']),
        ]


class AgentKnowledgeDocument(models.Model):
    """测试知识库文档，面向 PRD、接口文档、数据库设计和历史 Bug。"""

    DOCUMENT_CHOICES = [
        ('prd', 'PRD'),
        ('api', '接口文档'),
        ('database', '数据库设计'),
        ('requirement', '需求说明'),
        ('bug', '历史Bug'),
        ('other', '其他'),
    ]

    title = models.CharField(max_length=200, verbose_name='标题')
    document_type = models.CharField(max_length=32, choices=DOCUMENT_CHOICES, default='other', db_index=True, verbose_name='文档类型')
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, blank=True, related_name='agent_knowledge_documents', verbose_name='关联项目')
    content = models.TextField(blank=True, default='', verbose_name='文档内容')
    metadata = models.JSONField(default=dict, blank=True, verbose_name='元数据')
    embedding_model = models.CharField(max_length=100, blank=True, default='local-hash-embedding', verbose_name='Embedding模型')
    status = models.CharField(max_length=32, default='indexed', db_index=True, verbose_name='状态')
    chunks_count = models.PositiveIntegerField(default=0, verbose_name='分片数')
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='agent_knowledge_documents', verbose_name='上传人')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'agent_knowledge_documents'
        verbose_name = 'Agent知识文档'
        verbose_name_plural = 'Agent知识文档'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['project', 'document_type']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return self.title


class AgentKnowledgeChunk(models.Model):
    """知识库分片。embedding 使用 JSON 存储，PostgreSQL 可后续迁移到 pgvector。"""

    document = models.ForeignKey(AgentKnowledgeDocument, on_delete=models.CASCADE, related_name='chunks', verbose_name='文档')
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, blank=True, related_name='agent_knowledge_chunks', verbose_name='关联项目')
    chunk_index = models.PositiveIntegerField(verbose_name='分片序号')
    content = models.TextField(verbose_name='内容')
    embedding = models.JSONField(default=list, blank=True, verbose_name='Embedding向量')
    tokens = models.PositiveIntegerField(default=0, verbose_name='估算Token数')
    metadata = models.JSONField(default=dict, blank=True, verbose_name='元数据')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')

    class Meta:
        db_table = 'agent_knowledge_chunks'
        verbose_name = 'Agent知识分片'
        verbose_name_plural = 'Agent知识分片'
        ordering = ['document_id', 'chunk_index']
        unique_together = ['document', 'chunk_index']
        indexes = [
            models.Index(fields=['project', 'created_at']),
        ]


class AgentExecutionLog(models.Model):
    """ReAct Agent 大脑轨迹。"""

    TRACE_CHOICES = [
        ('thought', 'Thought'),
        ('action', 'Action'),
        ('observation', 'Observation'),
        ('reflection', 'Reflection'),
    ]

    task = models.ForeignKey(AgentTask, on_delete=models.CASCADE, related_name='execution_logs', verbose_name='任务')
    step = models.ForeignKey(AgentStep, on_delete=models.SET_NULL, null=True, blank=True, related_name='execution_logs', verbose_name='步骤')
    trace_type = models.CharField(max_length=32, choices=TRACE_CHOICES, db_index=True, verbose_name='轨迹类型')
    action = models.TextField(blank=True, default='', verbose_name='动作/思考')
    tool = models.CharField(max_length=100, blank=True, default='', verbose_name='工具')
    params = models.JSONField(default=dict, blank=True, verbose_name='参数')
    result = models.JSONField(default=dict, blank=True, verbose_name='结果')
    status = models.CharField(max_length=32, blank=True, default='success', db_index=True, verbose_name='状态')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')

    class Meta:
        db_table = 'agent_execution_log'
        verbose_name = 'Agent执行轨迹'
        verbose_name_plural = 'Agent执行轨迹'
        ordering = ['created_at', 'id']
        indexes = [
            models.Index(fields=['task', 'trace_type']),
            models.Index(fields=['task', 'created_at']),
        ]


class TestAsset(models.Model):
    """Agent 生成或引用的统一测试资产索引。"""

    ASSET_CHOICES = [
        ('data', '测试数据'),
        ('case', '测试用例'),
        ('api', 'API测试'),
        ('ui', 'UI自动化'),
        ('app', 'APP自动化'),
        ('performance', '性能测试'),
        ('report', '测试报告'),
    ]

    asset_id = models.CharField(max_length=80, unique=True, verbose_name='资产编号')
    asset_type = models.CharField(max_length=32, choices=ASSET_CHOICES, db_index=True, verbose_name='资产类型')
    name = models.CharField(max_length=200, verbose_name='资产名称')
    task = models.ForeignKey(AgentTask, on_delete=models.SET_NULL, null=True, blank=True, related_name='assets', verbose_name='Agent任务')
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, blank=True, related_name='agent_assets', verbose_name='关联项目')
    source_model = models.CharField(max_length=100, blank=True, default='', verbose_name='来源模型')
    source_id = models.CharField(max_length=80, blank=True, default='', verbose_name='来源ID')
    metadata = models.JSONField(default=dict, blank=True, verbose_name='元数据')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='agent_test_assets', verbose_name='创建人')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'test_assets'
        verbose_name = 'Agent测试资产'
        verbose_name_plural = 'Agent测试资产'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['asset_type']),
            models.Index(fields=['task', 'asset_type']),
        ]


class TestExecutionResult(models.Model):
    """跨模块执行结果统一落库。"""

    STATUS_CHOICES = [
        ('PENDING', '等待中'),
        ('RUNNING', '执行中'),
        ('PASSED', '通过'),
        ('FAILED', '失败'),
        ('SKIPPED', '跳过'),
        ('NOT_APPLICABLE', '不适用'),
    ]

    task = models.ForeignKey(AgentTask, on_delete=models.CASCADE, related_name='execution_results', verbose_name='Agent任务')
    step = models.ForeignKey(AgentStep, on_delete=models.SET_NULL, null=True, blank=True, related_name='execution_results', verbose_name='步骤')
    module = models.CharField(max_length=32, db_index=True, verbose_name='执行模块')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING', db_index=True, verbose_name='状态')
    asset = models.ForeignKey(TestAsset, on_delete=models.SET_NULL, null=True, blank=True, related_name='execution_results', verbose_name='资产')
    request_payload = models.JSONField(default=dict, blank=True, verbose_name='请求/输入')
    response_payload = models.JSONField(default=dict, blank=True, verbose_name='响应/输出')
    logs = models.TextField(blank=True, default='', verbose_name='日志')
    screenshot = models.CharField(max_length=500, blank=True, default='', verbose_name='截图')
    report_url = models.CharField(max_length=500, blank=True, default='', verbose_name='报告地址')
    duration_ms = models.PositiveIntegerField(default=0, verbose_name='耗时毫秒')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='结束时间')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'test_execution_result'
        verbose_name = '测试执行结果'
        verbose_name_plural = '测试执行结果'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['task', 'module']),
            models.Index(fields=['status']),
        ]


class E2ETask(models.Model):
    """Natural-language end-to-end test task and its execution state."""

    EXECUTOR_WEB = 'web'
    EXECUTOR_ANDROID = 'android'
    EXECUTOR_IOS = 'ios'
    EXECUTOR_CHOICES = [
        (EXECUTOR_WEB, 'Web'),
        (EXECUTOR_ANDROID, 'Android'),
        (EXECUTOR_IOS, 'iOS'),
    ]

    STATUS_CREATED = 'CREATED'
    STATUS_PLANNING = 'PLANNING'
    STATUS_NEEDS_INPUT = 'NEEDS_INPUT'
    STATUS_READY = 'READY'
    STATUS_QUEUED = 'QUEUED'
    STATUS_RUNNING = 'RUNNING'
    STATUS_ANALYZING = 'ANALYZING'
    STATUS_REPORTING = 'REPORTING'
    STATUS_PASSED = 'PASSED'
    STATUS_FAILED = 'FAILED'
    STATUS_CANCELLED = 'CANCELLED'
    STATUS_CHOICES = [
        (STATUS_CREATED, '已创建'),
        (STATUS_PLANNING, '规划中'),
        (STATUS_NEEDS_INPUT, '待补充执行条件'),
        (STATUS_READY, '待执行'),
        (STATUS_QUEUED, '排队中'),
        (STATUS_RUNNING, '执行中'),
        (STATUS_ANALYZING, '分析中'),
        (STATUS_REPORTING, '生成报告中'),
        (STATUS_PASSED, '通过'),
        (STATUS_FAILED, '失败'),
        (STATUS_CANCELLED, '已取消'),
    ]

    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='e2e_tasks',
        verbose_name='关联项目',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='e2e_tasks',
        verbose_name='创建人',
    )
    agent_task = models.OneToOneField(
        AgentTask,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='e2e_task',
        verbose_name='通用 Agent 任务',
    )
    task_name = models.CharField(max_length=200, verbose_name='任务名称')
    description = models.TextField(verbose_name='自然语言需求')
    scenario = models.JSONField(default=dict, blank=True, verbose_name='测试场景')
    steps = models.JSONField(default=list, blank=True, verbose_name='测试步骤')
    test_data = models.JSONField(default=dict, blank=True, verbose_name='测试数据')
    expected_result = models.TextField(blank=True, default='', verbose_name='预期结果')
    executor_type = models.CharField(
        max_length=16,
        choices=EXECUTOR_CHOICES,
        default=EXECUTOR_WEB,
        db_index=True,
        verbose_name='执行器类型',
    )
    target_url = models.URLField(max_length=2048, blank=True, default='', verbose_name='Web 目标地址')
    mobile_config = models.JSONField(default=dict, blank=True, verbose_name='移动端配置')
    status = models.CharField(
        max_length=24,
        choices=STATUS_CHOICES,
        default=STATUS_CREATED,
        db_index=True,
        verbose_name='状态',
    )
    result = models.JSONField(default=dict, blank=True, verbose_name='执行结果')
    analysis = models.JSONField(default=dict, blank=True, verbose_name='AI 分析')
    report_url = models.CharField(max_length=500, blank=True, default='', verbose_name='报告地址')
    error_message = models.TextField(blank=True, default='', verbose_name='错误信息')
    trace_id = models.CharField(max_length=128, unique=True, db_index=True, verbose_name='链路 ID')
    celery_task_id = models.CharField(max_length=255, blank=True, default='', db_index=True, verbose_name='Celery 任务 ID')
    execution_attempt = models.PositiveIntegerField(default=0, verbose_name='执行次数')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='完成时间')
    created_time = models.DateTimeField(default=timezone.now, db_index=True, verbose_name='创建时间')
    updated_time = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'e2e_task'
        verbose_name = 'E2E 测试任务'
        verbose_name_plural = 'E2E 测试任务'
        ordering = ['-created_time', '-id']
        indexes = [
            models.Index(fields=['project', '-created_time']),
            models.Index(fields=['created_by', '-created_time']),
            models.Index(fields=['status', '-created_time']),
        ]

    def __str__(self):
        return f'{self.id}:{self.task_name}'


class E2EExecutionLog(models.Model):
    """Step-level E2E execution evidence."""

    STATUS_CHOICES = [
        ('PENDING', '等待中'),
        ('RUNNING', '执行中'),
        ('PASSED', '通过'),
        ('FAILED', '失败'),
        ('SKIPPED', '跳过'),
    ]

    task = models.ForeignKey(E2ETask, on_delete=models.CASCADE, related_name='execution_logs', verbose_name='E2E 任务')
    attempt = models.PositiveIntegerField(default=1, verbose_name='执行次数')
    step = models.PositiveIntegerField(verbose_name='步骤序号')
    step_name = models.CharField(max_length=300, blank=True, default='', verbose_name='步骤名称')
    action = models.CharField(max_length=64, verbose_name='动作')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='PENDING', db_index=True, verbose_name='状态')
    screenshot = models.CharField(max_length=500, blank=True, default='', verbose_name='截图')
    video = models.CharField(max_length=500, blank=True, default='', verbose_name='视频')
    console_log = models.JSONField(default=list, blank=True, verbose_name='Console 日志')
    network_log = models.JSONField(default=list, blank=True, verbose_name='网络日志')
    device_log = models.TextField(blank=True, default='', verbose_name='设备日志')
    input_payload = models.JSONField(default=dict, blank=True, verbose_name='输入')
    output_payload = models.JSONField(default=dict, blank=True, verbose_name='输出')
    error = models.TextField(blank=True, default='', verbose_name='错误')
    duration_ms = models.PositiveIntegerField(default=0, verbose_name='耗时毫秒')
    created_time = models.DateTimeField(default=timezone.now, db_index=True, verbose_name='创建时间')
    updated_time = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'e2e_execution_log'
        verbose_name = 'E2E 执行日志'
        verbose_name_plural = 'E2E 执行日志'
        ordering = ['attempt', 'step', 'id']
        constraints = [
            models.UniqueConstraint(fields=['task', 'attempt', 'step'], name='uniq_e2e_task_attempt_step'),
        ]
        indexes = [
            models.Index(fields=['task', 'attempt', 'status']),
            models.Index(fields=['status', '-created_time']),
        ]
