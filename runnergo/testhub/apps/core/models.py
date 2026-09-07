"""
Core 应用模型
"""
from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()


class RuntimeDataTemplate(models.Model):
    """测试数据模板：保存某个自动化用例的执行前数据配置。"""

    TARGET_TYPE_CHOICES = [
        ('ui_automation', 'UI自动化'),
        ('app_automation', 'APP自动化'),
        ('api_automation', '接口自动化'),
    ]

    name = models.CharField(max_length=200, verbose_name='模板名称')
    description = models.TextField(blank=True, default='', verbose_name='模板描述')
    target_type = models.CharField(max_length=30, choices=TARGET_TYPE_CHOICES, db_index=True, verbose_name='目标类型')
    target_case_id = models.PositiveIntegerField(db_index=True, verbose_name='目标用例ID')
    target_case_name = models.CharField(max_length=500, blank=True, default='', verbose_name='目标用例名称')
    fields = models.JSONField(default=list, blank=True, verbose_name='字段快照')
    runtime_override = models.JSONField(default=list, blank=True, verbose_name='执行数据覆盖')
    runtime_context = models.JSONField(default=dict, blank=True, verbose_name='运行上下文默认值')
    cleanup_config = models.JSONField(default=dict, blank=True, verbose_name='执行后清理配置')
    is_active = models.BooleanField(default=True, verbose_name='是否启用')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='runtime_data_templates')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'runtime_data_templates'
        verbose_name = '运行态数据模板'
        verbose_name_plural = '运行态数据模板'
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['target_type', 'target_case_id']),
            models.Index(fields=['created_by']),
        ]

    def __str__(self):
        return self.name


class RuntimeDataSet(models.Model):
    """批量执行数据集，支持 CSV/Excel 导入多组数据。"""

    TARGET_TYPE_CHOICES = RuntimeDataTemplate.TARGET_TYPE_CHOICES

    name = models.CharField(max_length=200, verbose_name='数据集名称')
    description = models.TextField(blank=True, default='', verbose_name='数据集描述')
    target_type = models.CharField(max_length=30, choices=TARGET_TYPE_CHOICES, db_index=True, verbose_name='目标类型')
    target_case_id = models.PositiveIntegerField(db_index=True, verbose_name='目标用例ID')
    target_case_name = models.CharField(max_length=500, blank=True, default='', verbose_name='目标用例名称')
    columns = models.JSONField(default=list, blank=True, verbose_name='数据列')
    rows = models.JSONField(default=list, blank=True, verbose_name='数据行')
    row_count = models.PositiveIntegerField(default=0, verbose_name='数据行数')
    source_filename = models.CharField(max_length=255, blank=True, default='', verbose_name='来源文件名')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='runtime_data_sets')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'runtime_data_sets'
        verbose_name = '运行态数据集'
        verbose_name_plural = '运行态数据集'
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['target_type', 'target_case_id']),
            models.Index(fields=['created_by']),
        ]

    def __str__(self):
        return self.name


class RuntimeExecutionSnapshot(models.Model):
    """每次执行实际使用数据的快照。"""

    TARGET_TYPE_CHOICES = RuntimeDataTemplate.TARGET_TYPE_CHOICES

    target_type = models.CharField(max_length=30, choices=TARGET_TYPE_CHOICES, db_index=True, verbose_name='目标类型')
    target_case_id = models.PositiveIntegerField(db_index=True, verbose_name='目标用例ID')
    target_case_name = models.CharField(max_length=500, blank=True, default='', verbose_name='目标用例名称')
    execution_type = models.CharField(max_length=50, blank=True, default='', verbose_name='执行类型')
    execution_id = models.CharField(max_length=100, blank=True, default='', db_index=True, verbose_name='执行ID')
    template = models.ForeignKey(RuntimeDataTemplate, on_delete=models.SET_NULL, null=True, blank=True, related_name='snapshots')
    dataset = models.ForeignKey(RuntimeDataSet, on_delete=models.SET_NULL, null=True, blank=True, related_name='snapshots')
    dataset_row_index = models.IntegerField(null=True, blank=True, verbose_name='数据集行号')
    runtime_override = models.JSONField(default=list, blank=True, verbose_name='运行覆盖')
    runtime_data = models.JSONField(default=list, blank=True, verbose_name='实际执行数据')
    runtime_context = models.JSONField(default=dict, blank=True, verbose_name='运行上下文')
    cleanup_config = models.JSONField(default=dict, blank=True, verbose_name='清理配置')
    cleanup_result = models.JSONField(default=dict, blank=True, verbose_name='清理结果')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='runtime_execution_snapshots')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'runtime_execution_snapshots'
        verbose_name = '运行态执行数据快照'
        verbose_name_plural = '运行态执行数据快照'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['target_type', 'target_case_id']),
            models.Index(fields=['execution_type', 'execution_id']),
        ]


class TestDataAsset(models.Model):
    """测试数据资产池：可复用、可申请、可释放的业务测试数据。"""

    STATUS_AVAILABLE = 'AVAILABLE'
    STATUS_LOCKED = 'LOCKED'
    STATUS_USED = 'USED'
    STATUS_EXPIRED = 'EXPIRED'

    STATUS_CHOICES = [
        (STATUS_AVAILABLE, '可用'),
        (STATUS_LOCKED, '已锁定'),
        (STATUS_USED, '已使用'),
        (STATUS_EXPIRED, '已过期'),
    ]

    ASSET_TYPE_CHOICES = [
        ('USER', '用户'),
        ('PHONE', '手机号'),
        ('PRODUCT', '商品'),
        ('ORDER', '订单'),
        ('CUSTOM', '自定义'),
    ]

    asset_type = models.CharField(max_length=50, db_index=True, verbose_name='资产类型')
    name = models.CharField(max_length=200, verbose_name='资产名称')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_AVAILABLE, db_index=True, verbose_name='资产状态')
    payload = models.JSONField(default=dict, blank=True, verbose_name='资产数据')
    # 大批量生成资产只保存数据源引用，执行时按游标读取一行，避免把百万级数据写入 JSONField。
    bulk_job = models.ForeignKey(
        'BulkTestDataJob',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='source_assets',
        verbose_name='大批量生成任务',
    )
    bulk_row_cursor = models.PositiveBigIntegerField(default=0, verbose_name='大批量数据游标')
    tags = models.JSONField(default=list, blank=True, verbose_name='标签')
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name='过期时间')
    locked_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='locked_test_data_assets')
    locked_at = models.DateTimeField(null=True, blank=True, verbose_name='锁定时间')
    lock_ttl_seconds = models.PositiveIntegerField(default=3600, verbose_name='锁定TTL秒数')
    used_at = models.DateTimeField(null=True, blank=True, verbose_name='使用时间')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='test_data_assets')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'test_data_assets'
        verbose_name = '测试数据资产'
        verbose_name_plural = '测试数据资产'
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['asset_type', 'status']),
            models.Index(fields=['locked_by', 'status']),
            models.Index(fields=['created_by']),
        ]

    def __str__(self):
        return f'{self.asset_type}:{self.name}'


class TestDataAssetRequirement(models.Model):
    """用例声明的数据需求，不修改原始用例，通过 Runtime Context 注入。"""

    TARGET_TYPE_CHOICES = RuntimeDataTemplate.TARGET_TYPE_CHOICES
    RELEASE_POLICY_CHOICES = [
        ('release', '执行后释放为可用'),
        ('mark_used', '执行后标记已使用'),
        ('expire', '执行后标记过期'),
        ('keep_locked', '执行后保持锁定'),
    ]

    target_type = models.CharField(max_length=30, choices=TARGET_TYPE_CHOICES, db_index=True, verbose_name='目标类型')
    target_case_id = models.PositiveIntegerField(db_index=True, verbose_name='目标用例ID')
    target_case_name = models.CharField(max_length=500, blank=True, default='', verbose_name='目标用例名称')
    alias = models.CharField(max_length=100, verbose_name='上下文别名')
    asset_type = models.CharField(max_length=50, db_index=True, verbose_name='资产类型')
    source_asset = models.ForeignKey(
        'TestDataAsset',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='requirements',
        verbose_name='绑定的数据资产',
    )
    quantity = models.PositiveIntegerField(default=1, verbose_name='申请数量')
    filters = models.JSONField(default=dict, blank=True, verbose_name='资产过滤条件')
    tags = models.JSONField(default=list, blank=True, verbose_name='标签过滤')
    release_policy = models.CharField(max_length=20, choices=RELEASE_POLICY_CHOICES, default='release', verbose_name='释放策略')
    inject_path = models.CharField(max_length=200, blank=True, default='', verbose_name='注入路径')
    lock_ttl_seconds = models.PositiveIntegerField(default=3600, verbose_name='锁定TTL秒数')
    is_active = models.BooleanField(default=True, verbose_name='是否启用')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='test_data_asset_requirements')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'test_data_asset_requirements'
        verbose_name = '测试数据需求'
        verbose_name_plural = '测试数据需求'
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['target_type', 'target_case_id', 'is_active']),
            models.Index(fields=['asset_type', 'is_active']),
        ]

    def __str__(self):
        return f'{self.target_type}:{self.target_case_id}:{self.alias}'


class TestDataAssetLease(models.Model):
    """执行前数据申请生成的租约记录。"""

    STATUS_LOCKED = 'LOCKED'
    STATUS_RELEASED = 'RELEASED'
    STATUS_USED = 'USED'
    STATUS_EXPIRED = 'EXPIRED'
    STATUS_CLEANED = 'CLEANED'

    STATUS_CHOICES = [
        (STATUS_LOCKED, '已锁定'),
        (STATUS_RELEASED, '已释放'),
        (STATUS_USED, '已使用'),
        (STATUS_EXPIRED, '已过期'),
        (STATUS_CLEANED, '已清理'),
    ]

    asset = models.ForeignKey(TestDataAsset, on_delete=models.CASCADE, related_name='leases')
    requirement = models.ForeignKey(TestDataAssetRequirement, on_delete=models.SET_NULL, null=True, blank=True, related_name='leases')
    target_type = models.CharField(max_length=30, db_index=True, verbose_name='目标类型')
    target_case_id = models.PositiveIntegerField(db_index=True, verbose_name='目标用例ID')
    target_case_name = models.CharField(max_length=500, blank=True, default='', verbose_name='目标用例名称')
    execution_type = models.CharField(max_length=50, blank=True, default='', verbose_name='执行类型')
    execution_id = models.CharField(max_length=100, blank=True, default='', db_index=True, verbose_name='执行ID')
    alias = models.CharField(max_length=100, verbose_name='上下文别名')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_LOCKED, db_index=True, verbose_name='租约状态')
    release_policy = models.CharField(max_length=20, default='release', verbose_name='释放策略')
    runtime_context_path = models.CharField(max_length=200, blank=True, default='', verbose_name='Runtime Context路径')
    payload_snapshot = models.JSONField(default=dict, blank=True, verbose_name='申请数据快照')
    requested_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='test_data_asset_leases')
    locked_at = models.DateTimeField(auto_now_add=True, verbose_name='锁定时间')
    released_at = models.DateTimeField(null=True, blank=True, verbose_name='释放时间')
    cleanup_result = models.JSONField(default=dict, blank=True, verbose_name='清理结果')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'test_data_asset_leases'
        verbose_name = '测试数据租约'
        verbose_name_plural = '测试数据租约'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['execution_type', 'execution_id', 'status']),
            models.Index(fields=['target_type', 'target_case_id']),
            models.Index(fields=['asset', 'status']),
        ]


class TestDataDecisionLog(models.Model):
    """V6 智能测试数据编排决策日志。"""

    SOURCE_ASSET_POOL = 'asset_pool'
    SOURCE_RULE_GENERATION = 'rule_generation'
    SOURCE_SKIPPED = 'skipped'

    STATUS_PREPARED = 'prepared'
    STATUS_RELEASED = 'released'
    STATUS_USED = 'used'
    STATUS_EXPIRED = 'expired'
    STATUS_REPAIRED = 'repaired'

    SOURCE_CHOICES = [
        (SOURCE_ASSET_POOL, '资产池'),
        (SOURCE_RULE_GENERATION, '规则生成'),
        (SOURCE_SKIPPED, '跳过'),
    ]
    STATUS_CHOICES = [
        (STATUS_PREPARED, '已准备'),
        (STATUS_RELEASED, '已释放'),
        (STATUS_USED, '已使用'),
        (STATUS_EXPIRED, '已过期'),
        (STATUS_REPAIRED, '已修复'),
    ]

    target_type = models.CharField(max_length=30, db_index=True, verbose_name='目标类型')
    target_case_id = models.PositiveIntegerField(db_index=True, verbose_name='目标用例ID')
    target_case_name = models.CharField(max_length=500, blank=True, default='', verbose_name='目标用例名称')
    execution_type = models.CharField(max_length=50, blank=True, default='', verbose_name='执行类型')
    execution_id = models.CharField(max_length=100, blank=True, default='', db_index=True, verbose_name='执行ID')
    field_key = models.CharField(max_length=200, blank=True, default='', verbose_name='字段Key')
    field_label = models.CharField(max_length=500, blank=True, default='', verbose_name='字段名称')
    field_type = models.CharField(max_length=50, blank=True, default='', verbose_name='识别类型')
    alias = models.CharField(max_length=100, blank=True, default='', verbose_name='上下文别名')
    source = models.CharField(max_length=30, choices=SOURCE_CHOICES, db_index=True, verbose_name='数据来源')
    strategy = models.CharField(max_length=50, blank=True, default='', verbose_name='决策策略')
    runtime_context_path = models.CharField(max_length=200, blank=True, default='', verbose_name='Runtime Context路径')
    decision = models.JSONField(default=dict, blank=True, verbose_name='决策详情')
    value_snapshot = models.JSONField(default=dict, blank=True, verbose_name='值快照')
    asset = models.ForeignKey(TestDataAsset, on_delete=models.SET_NULL, null=True, blank=True, related_name='decision_logs')
    lease = models.ForeignKey(TestDataAssetLease, on_delete=models.SET_NULL, null=True, blank=True, related_name='decision_logs')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PREPARED, db_index=True, verbose_name='状态')
    failure_info = models.JSONField(default=dict, blank=True, verbose_name='失败信息')
    repair_result = models.JSONField(default=dict, blank=True, verbose_name='修复结果')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='test_data_decision_logs')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'test_data_decision_logs'
        verbose_name = '测试数据决策日志'
        verbose_name_plural = '测试数据决策日志'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['execution_type', 'execution_id']),
            models.Index(fields=['target_type', 'target_case_id']),
            models.Index(fields=['source', 'status']),
        ]


class BulkTestDataJob(models.Model):
    """大批量测试数据生成任务，数据文件化保存，不把明细放入 JSONField。"""

    STATUS_PENDING = 'PENDING'
    STATUS_RUNNING = 'RUNNING'
    STATUS_COMPLETED = 'COMPLETED'
    STATUS_FAILED = 'FAILED'
    STATUS_CANCELED = 'CANCELED'

    STATUS_CHOICES = [
        (STATUS_PENDING, '排队中'),
        (STATUS_RUNNING, '生成中'),
        (STATUS_COMPLETED, '已完成'),
        (STATUS_FAILED, '失败'),
        (STATUS_CANCELED, '已取消'),
    ]

    FORMAT_CSV = 'csv'
    FORMAT_JSONL = 'jsonl'
    FORMAT_SQL = 'sql'
    FORMAT_CHOICES = [
        (FORMAT_CSV, 'CSV'),
        (FORMAT_JSONL, 'JSONL'),
        (FORMAT_SQL, 'SQL'),
    ]

    name = models.CharField(max_length=200, verbose_name='任务名称')
    asset_type = models.CharField(max_length=50, default='CUSTOM', verbose_name='资产类型')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True, verbose_name='任务状态')
    field_definitions = models.JSONField(default=list, verbose_name='字段定义')
    total_count = models.PositiveBigIntegerField(verbose_name='目标条数')
    generated_count = models.PositiveBigIntegerField(default=0, verbose_name='已生成条数')
    progress = models.PositiveSmallIntegerField(default=0, verbose_name='进度百分比')
    batch_size = models.PositiveIntegerField(default=5000, verbose_name='进度更新批次')
    output_format = models.CharField(max_length=10, choices=FORMAT_CHOICES, default=FORMAT_CSV, verbose_name='输出格式')
    table_name = models.CharField(max_length=128, default='test_data', verbose_name='SQL目标表')
    sql_dialect = models.CharField(max_length=20, default='mysql', verbose_name='SQL方言')
    include_create_table = models.BooleanField(default=True, verbose_name='包含建表语句')
    asset_config = models.JSONField(default=dict, blank=True, verbose_name='数据资产配置')
    output_file = models.FileField(upload_to='test-data-bulk/%Y/%m/%d/', null=True, blank=True, verbose_name='生成文件')
    output_size = models.PositiveBigIntegerField(default=0, verbose_name='文件大小')
    checksum_sha256 = models.CharField(max_length=64, blank=True, default='', verbose_name='SHA256')
    seed = models.PositiveBigIntegerField(null=True, blank=True, verbose_name='随机种子')
    task_id = models.CharField(max_length=100, blank=True, default='', db_index=True, verbose_name='Celery任务ID')
    cancel_requested = models.BooleanField(default=False, verbose_name='请求取消')
    error_message = models.TextField(blank=True, default='', verbose_name='错误信息')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name='完成时间')
    data_asset = models.ForeignKey(
        TestDataAsset,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='bulk_source_jobs',
        verbose_name='生成的数据资产',
    )
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='bulk_test_data_jobs')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'bulk_test_data_jobs'
        verbose_name = '大批量测试数据任务'
        verbose_name_plural = '大批量测试数据任务'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['created_by', 'status']),
            models.Index(fields=['status', 'created_at']),
        ]

    def __str__(self):
        return f'{self.name} ({self.total_count})'


class BulkTestDataRow(models.Model):
    """大批量数据的真实行数据，按任务和行号索引，运行时无需下载生成文件。"""

    job = models.ForeignKey(BulkTestDataJob, on_delete=models.CASCADE, related_name='rows')
    row_index = models.PositiveBigIntegerField(verbose_name='行号')
    payload = models.JSONField(default=dict, verbose_name='行数据')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'bulk_test_data_rows'
        verbose_name = '大批量测试数据行'
        verbose_name_plural = '大批量测试数据行'
        constraints = [
            models.UniqueConstraint(fields=['job', 'row_index'], name='uniq_bulk_test_data_job_row'),
        ]
        indexes = [
            models.Index(fields=['job', 'row_index'], name='bulk_test_d_job_id_8ae1f3_idx'),
        ]

    def __str__(self):
        return f'{self.job_id}:{self.row_index}'
