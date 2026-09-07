from django.db import models
from django.utils import timezone

from apps.projects.models import Project
from apps.reports.models import TestReport
from apps.requirement_analysis.models import BusinessRequirement, GeneratedTestCase
from apps.testcases.models import TestCase
from apps.users.models import User


class TestAssetRequirement(models.Model):
    """需求资产，用于承载需求到测试资产的追踪关系。"""

    PRIORITY_CHOICES = [
        ('low', '低'),
        ('medium', '中'),
        ('high', '高'),
        ('critical', '紧急'),
    ]

    STATUS_CHOICES = [
        ('draft', '草稿'),
        ('reviewing', '评审中'),
        ('approved', '已确认'),
        ('changed', '已变更'),
        ('closed', '已关闭'),
    ]

    SOURCE_CHOICES = [
        ('manual', '手工录入'),
        ('ai', 'AI分析'),
        ('imported', '外部导入'),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='asset_requirements', verbose_name='所属项目')
    business_requirement = models.ForeignKey(
        BusinessRequirement,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='asset_requirements',
        verbose_name='关联AI需求',
    )
    requirement_key = models.CharField(max_length=80, verbose_name='需求编号')
    title = models.CharField(max_length=300, verbose_name='需求标题')
    module = models.CharField(max_length=120, blank=True, default='', verbose_name='所属模块')
    description = models.TextField(blank=True, default='', verbose_name='需求描述')
    acceptance_criteria = models.TextField(blank=True, default='', verbose_name='验收标准')
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default='medium', verbose_name='优先级')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft', verbose_name='状态')
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='manual', verbose_name='来源')
    external_url = models.URLField(blank=True, default='', verbose_name='外部链接')
    owner = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='owned_asset_requirements',
        verbose_name='负责人',
    )
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_asset_requirements', verbose_name='创建者')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'test_asset_requirements'
        verbose_name = '测试资产需求'
        verbose_name_plural = '测试资产需求'
        ordering = ['-updated_at']
        unique_together = ['project', 'requirement_key']
        indexes = [
            models.Index(fields=['project', 'requirement_key']),
            models.Index(fields=['project', 'status']),
            models.Index(fields=['project', 'priority']),
        ]

    def __str__(self):
        return f'{self.requirement_key} - {self.title}'


class ApiAsset(models.Model):
    METHOD_CHOICES = [
        ('GET', 'GET'),
        ('POST', 'POST'),
        ('PUT', 'PUT'),
        ('PATCH', 'PATCH'),
        ('DELETE', 'DELETE'),
        ('HEAD', 'HEAD'),
        ('OPTIONS', 'OPTIONS'),
    ]

    STATUS_CHOICES = [
        ('designing', '设计中'),
        ('active', '可用'),
        ('deprecated', '已废弃'),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='api_assets', verbose_name='所属项目')
    name = models.CharField(max_length=200, verbose_name='接口名称')
    method = models.CharField(max_length=10, choices=METHOD_CHOICES, default='GET', verbose_name='请求方法')
    path = models.CharField(max_length=500, verbose_name='接口路径')
    service = models.CharField(max_length=120, blank=True, default='', verbose_name='所属服务')
    description = models.TextField(blank=True, default='', verbose_name='说明')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active', verbose_name='状态')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_api_assets', verbose_name='创建者')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'test_asset_apis'
        verbose_name = '接口资产'
        verbose_name_plural = '接口资产'
        ordering = ['method', 'path']
        unique_together = ['project', 'method', 'path']
        indexes = [
            models.Index(fields=['project', 'method', 'path']),
        ]

    def __str__(self):
        return f'{self.method} {self.path}'


class PageAsset(models.Model):
    PLATFORM_CHOICES = [
        ('web', 'Web'),
        ('h5', 'H5'),
        ('android', 'Android'),
        ('ios', 'iOS'),
    ]

    STATUS_CHOICES = [
        ('designing', '设计中'),
        ('active', '可用'),
        ('deprecated', '已废弃'),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='page_assets', verbose_name='所属项目')
    name = models.CharField(max_length=200, verbose_name='页面/元素名称')
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES, default='web', verbose_name='平台')
    route = models.CharField(max_length=500, blank=True, default='', verbose_name='页面路径')
    element_name = models.CharField(max_length=200, blank=True, default='', verbose_name='元素名称')
    selector = models.CharField(max_length=500, blank=True, default='', verbose_name='定位信息')
    description = models.TextField(blank=True, default='', verbose_name='说明')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active', verbose_name='状态')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_page_assets', verbose_name='创建者')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'test_asset_pages'
        verbose_name = '页面资产'
        verbose_name_plural = '页面资产'
        ordering = ['platform', 'name']
        indexes = [
            models.Index(fields=['project', 'platform']),
        ]

    def __str__(self):
        return self.name


class AutomationScriptAsset(models.Model):
    SCRIPT_TYPE_CHOICES = [
        ('robot', 'Robot Framework'),
        ('pytest', 'Pytest'),
        ('playwright', 'Playwright'),
        ('appium', 'Appium'),
        ('postman', 'Postman'),
        ('other', '其他'),
    ]

    STATUS_CHOICES = [
        ('draft', '草稿'),
        ('active', '可用'),
        ('deprecated', '已废弃'),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='automation_script_assets', verbose_name='所属项目')
    name = models.CharField(max_length=200, verbose_name='脚本名称')
    script_type = models.CharField(max_length=20, choices=SCRIPT_TYPE_CHOICES, default='pytest', verbose_name='脚本类型')
    path = models.CharField(max_length=500, verbose_name='脚本路径')
    repository = models.CharField(max_length=300, blank=True, default='', verbose_name='仓库地址')
    entrypoint = models.CharField(max_length=300, blank=True, default='', verbose_name='执行入口')
    description = models.TextField(blank=True, default='', verbose_name='说明')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active', verbose_name='状态')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_automation_script_assets', verbose_name='创建者')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'test_asset_automation_scripts'
        verbose_name = '自动化脚本资产'
        verbose_name_plural = '自动化脚本资产'
        ordering = ['name']
        unique_together = ['project', 'path']
        indexes = [
            models.Index(fields=['project', 'script_type']),
        ]

    def __str__(self):
        return self.name


class DefectAsset(models.Model):
    SEVERITY_CHOICES = [
        ('low', '低'),
        ('medium', '中'),
        ('high', '高'),
        ('critical', '严重'),
    ]

    STATUS_CHOICES = [
        ('open', '待处理'),
        ('fixing', '修复中'),
        ('fixed', '已修复'),
        ('verified', '已验证'),
        ('closed', '已关闭'),
        ('reopened', '重新打开'),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='defect_assets', verbose_name='所属项目')
    defect_key = models.CharField(max_length=80, verbose_name='缺陷编号')
    title = models.CharField(max_length=300, verbose_name='缺陷标题')
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default='medium', verbose_name='严重程度')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open', verbose_name='状态')
    external_url = models.URLField(blank=True, default='', verbose_name='外部链接')
    description = models.TextField(blank=True, default='', verbose_name='说明')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_defect_assets', verbose_name='创建者')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'test_asset_defects'
        verbose_name = '缺陷资产'
        verbose_name_plural = '缺陷资产'
        ordering = ['-updated_at']
        unique_together = ['project', 'defect_key']
        indexes = [
            models.Index(fields=['project', 'status']),
            models.Index(fields=['project', 'severity']),
        ]

    def __str__(self):
        return f'{self.defect_key} - {self.title}'


class TestAssetLink(models.Model):
    ASSET_TYPE_CHOICES = [
        ('api', '接口'),
        ('page', '页面'),
        ('testcase', '测试用例'),
        ('automation_script', '自动化脚本'),
        ('defect', '缺陷'),
        ('report', '测试报告'),
        ('generated_case', 'AI生成用例'),
        ('business_requirement', 'AI业务需求'),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='test_asset_links', verbose_name='所属项目')
    requirement = models.ForeignKey(TestAssetRequirement, on_delete=models.CASCADE, related_name='links', verbose_name='关联需求')
    asset_type = models.CharField(max_length=30, choices=ASSET_TYPE_CHOICES, verbose_name='资产类型')
    api = models.ForeignKey(ApiAsset, on_delete=models.CASCADE, null=True, blank=True, related_name='requirement_links', verbose_name='接口')
    page = models.ForeignKey(PageAsset, on_delete=models.CASCADE, null=True, blank=True, related_name='requirement_links', verbose_name='页面')
    testcase = models.ForeignKey(TestCase, on_delete=models.CASCADE, null=True, blank=True, related_name='asset_links', verbose_name='测试用例')
    automation_script = models.ForeignKey(
        AutomationScriptAsset,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='requirement_links',
        verbose_name='自动化脚本',
    )
    defect = models.ForeignKey(DefectAsset, on_delete=models.CASCADE, null=True, blank=True, related_name='requirement_links', verbose_name='缺陷')
    report = models.ForeignKey(TestReport, on_delete=models.CASCADE, null=True, blank=True, related_name='asset_links', verbose_name='测试报告')
    generated_case = models.ForeignKey(
        GeneratedTestCase,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='asset_links',
        verbose_name='AI生成用例',
    )
    business_requirement = models.ForeignKey(
        BusinessRequirement,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='trace_links',
        verbose_name='AI业务需求',
    )
    note = models.CharField(max_length=300, blank=True, default='', verbose_name='关系说明')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_test_asset_links', verbose_name='创建者')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')

    class Meta:
        db_table = 'test_asset_links'
        verbose_name = '测试资产关系'
        verbose_name_plural = '测试资产关系'
        ordering = ['asset_type', '-created_at']
        indexes = [
            models.Index(fields=['project', 'requirement', 'asset_type']),
        ]

    def __str__(self):
        return f'{self.requirement.requirement_key} -> {self.asset_type}'
