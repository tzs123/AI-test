# -*- coding: utf-8 -*-
from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone

from .constants import DeviceStatus, ExecutionStatus, ExecutionResult, ElementType

User = get_user_model()


class AppProject(models.Model):
    """APP自动化测试项目"""
    STATUS_CHOICES = [
        ('NOT_STARTED', '未开始'),
        ('IN_PROGRESS', '进行中'),
        ('COMPLETED', '已结束'),
    ]

    name = models.CharField(max_length=200, verbose_name='项目名称')
    description = models.TextField(blank=True, default='', verbose_name='项目描述')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='IN_PROGRESS', verbose_name='项目状态')
    start_date = models.DateField(null=True, blank=True, verbose_name='开始日期')
    end_date = models.DateField(null=True, blank=True, verbose_name='结束日期')
    owner = models.ForeignKey(
        User, on_delete=models.CASCADE,
        related_name='owned_app_projects', verbose_name='负责人'
    )
    members = models.ManyToManyField(
        User, blank=True,
        related_name='app_projects', verbose_name='团队成员'
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'app_projects'
        verbose_name = 'APP自动化项目'
        verbose_name_plural = 'APP自动化项目'
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class AppTestConfig(models.Model):
    """APP自动化测试配置"""
    adb_path = models.CharField(
        max_length=500, 
        default='adb', 
        verbose_name='ADB路径',
        help_text='Android Debug Bridge 工具路径，默认为 adb（系统PATH）'
    )
    node_path = models.CharField(
        max_length=500,
        default='node',
        verbose_name='Node.js路径',
        help_text='Node.js 命令路径，默认为 node（系统PATH）'
    )
    appium_path = models.CharField(
        max_length=500,
        default='appium',
        verbose_name='Appium路径',
        help_text='Appium CLI 命令路径，默认为 appium（系统PATH）'
    )
    appium_server_url = models.CharField(
        max_length=500,
        default='http://127.0.0.1:4723',
        verbose_name='Appium服务地址',
        help_text='Appium Server 地址'
    )
    appium_driver = models.CharField(
        max_length=100,
        default='uiautomator2',
        verbose_name='Appium驱动',
        help_text='Android Appium 驱动名称'
    )
    appium_automation_name = models.CharField(
        max_length=100,
        default='UiAutomator2',
        verbose_name='AutomationName',
        help_text='Android 自动化引擎名称'
    )
    ios_wda_url = models.CharField(
        max_length=500,
        default='http://host.docker.internal:8100',
        verbose_name='iOS WDA地址',
        help_text='Mac 主机上 WebDriverAgent 的访问地址'
    )
    ios_wda_bundle_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name='WDA Bundle ID',
        help_text='自定义签名后的 WebDriverAgentRunner Bundle ID，可选'
    )
    ios_browser_start_url = models.CharField(
        max_length=500,
        blank=True,
        default='',
        verbose_name='iOS 浏览器起始URL',
        help_text='iOS Safari/H5 用例无应用包名时，执行前自动打开的起始页面 URL，可选'
    )
    device_bridge_url = models.CharField(
        max_length=500,
        default='http://host.docker.internal:8765',
        verbose_name='设备桥接服务地址',
        help_text='运行在 Mac 宿主机上的 RunnerGo 设备扫描服务'
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    
    class Meta:
        db_table = 'app_test_config'
        verbose_name = 'APP测试配置'
        verbose_name_plural = 'APP测试配置'
    
    def __str__(self):
        return f"APP测试配置 (ADB: {self.adb_path}, Appium: {self.appium_path})"


class AppDevice(models.Model):
    """Android/iOS 设备模型。"""
    PLATFORM_CHOICES = [
        ('android', 'Android'),
        ('ios', 'iOS'),
    ]
    STATUS_CHOICES = [
        (DeviceStatus.AVAILABLE, '可用'),
        (DeviceStatus.LOCKED, '已锁定'),
        (DeviceStatus.ONLINE, '在线'),
        (DeviceStatus.OFFLINE, '离线'),
    ]
    
    CONNECTION_TYPE_CHOICES = [
        ('emulator', '本地模拟器'),
        ('remote_emulator', '远程模拟器'),
        ('usb', 'USB连接'),
        ('remote', '远程设备'),
        ('ios_remote', 'iOS远程WDA'),
        ('real_device', '真实设备'),
    ]
    
    device_id = models.CharField(max_length=255, unique=True, verbose_name='设备序列号')
    name = models.CharField(max_length=255, blank=True, default='', verbose_name='设备名称')
    platform = models.CharField(
        max_length=20,
        choices=PLATFORM_CHOICES,
        default='android',
        db_index=True,
        verbose_name='设备平台'
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=DeviceStatus.OFFLINE, verbose_name='状态')
    android_version = models.CharField(max_length=50, blank=True, default='', verbose_name='Android版本')
    ios_version = models.CharField(max_length=50, blank=True, default='', verbose_name='iOS版本')
    wda_url = models.CharField(
        max_length=500,
        blank=True,
        default='',
        verbose_name='WDA地址',
        help_text='容器可访问的 WebDriverAgent 地址，例如 http://host.docker.internal:8100'
    )
    wda_bundle_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name='WDA Bundle ID'
    )
    default_bundle_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name='默认应用标识',
        help_text='Android 包名或 iOS Bundle ID'
    )
    connection_type = models.CharField(max_length=20, choices=CONNECTION_TYPE_CHOICES, default='emulator', verbose_name='连接类型')
    ip_address = models.CharField(max_length=50, blank=True, default='', verbose_name='IP地址')
    port = models.IntegerField(default=5555, verbose_name='端口')
    
    # 设备锁定相关字段
    locked_by = models.ForeignKey(
        User, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='locked_app_devices', 
        verbose_name='锁定用户'
    )
    locked_at = models.DateTimeField(null=True, blank=True, verbose_name='锁定时间')
    max_allocation_time = models.IntegerField(default=28800, verbose_name='最大分配时间(秒)', help_text='默认8小时')
    
    # 设备规格信息
    device_specs = models.JSONField(default=dict, verbose_name='设备规格', help_text='RAM, CPU, 分辨率等信息')
    description = models.TextField(blank=True, default='', verbose_name='设备描述')
    location = models.CharField(max_length=200, blank=True, default='', verbose_name='设备位置')
    
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    
    class Meta:
        db_table = 'app_devices'
        verbose_name = 'APP测试设备'
        verbose_name_plural = 'APP测试设备'
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['device_id']),
        ]
    
    def __str__(self):
        return f"{self.name or self.device_id} ({self.get_status_display()})"
    
    def lock(self, user):
        """锁定设备"""
        self.locked_by = user
        self.locked_at = timezone.now()
        self.status = DeviceStatus.LOCKED
        self.save()
    
    def unlock(self):
        """释放设备"""
        self.locked_by = None
        self.locked_at = None
        self.status = DeviceStatus.AVAILABLE
        self.save()
    
    def is_lock_expired(self):
        """检查锁定是否过期"""
        if not self.locked_at:
            return False
        elapsed = (timezone.now() - self.locked_at).total_seconds()
        return elapsed > self.max_allocation_time


class AppElementFolder(models.Model):
    """APP 元素文件夹，用于按设备、平台或业务场景归类元素。"""

    name = models.CharField(max_length=100, unique=True, verbose_name='文件夹名称')
    description = models.CharField(max_length=255, blank=True, default='', verbose_name='说明')
    sort_order = models.IntegerField(default=0, verbose_name='排序')
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_app_element_folders',
        verbose_name='创建人',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'app_element_folders'
        verbose_name = 'APP 元素文件夹'
        verbose_name_plural = 'APP 元素文件夹'
        ordering = ['sort_order', 'name', 'id']

    def __str__(self):
        return self.name


class AppElement(models.Model):
    """APP UI 元素管理 - 统一管理语义、OCR、图片、坐标和区域元素。"""
    
    ELEMENT_TYPE_CHOICES = [
        (ElementType.APPIUM, '智能元素'),
        (ElementType.OCR, 'OCR元素'),
        (ElementType.IMAGE, '图片元素'),
        (ElementType.POS, '坐标元素'),
        (ElementType.REGION, '区域元素'),
    ]

    project = models.ForeignKey(
        AppProject, on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='elements', verbose_name='所属项目'
    )

    folder = models.ForeignKey(
        AppElementFolder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='elements',
        verbose_name='所属文件夹',
    )
    
    # 基础信息
    name = models.CharField(
        max_length=200,
        verbose_name='元素名称',
        help_text='元素在所属文件夹内的唯一标识名称'
    )
    
    element_type = models.CharField(
        max_length=10,
        choices=ELEMENT_TYPE_CHOICES,
        verbose_name='元素类型'
    )
    
    # 标签
    tags = models.JSONField(
        default=list,
        verbose_name='标签',
        help_text='标签列表，如：["登录", "大厅", "支付"]'
    )
    
    # 元素配置（根据类型不同，内容不同）
    config = models.JSONField(
        default=dict,
        verbose_name='元素配置',
        help_text="""
        appium类型: {
            "platform": "android",
            "resource_id": "com.demo:id/login",
            "accessibility_id": "登录按钮",
            "text": "登录",
            "xpath": "//*[@resource-id='com.demo:id/login']",
            "bounds": {"x1": 100, "y1": 200, "x2": 300, "y2": 260},
            "locator_strategies": [
                {"type": "resource_id", "enabled": true},
                {"type": "accessibility", "enabled": true},
                {"type": "text", "enabled": true},
                {"type": "ocr", "enabled": true},
                {"type": "image", "enabled": true},
                {"type": "position", "enabled": true}
            ]
        }
        ocr类型: {"ocr_text": "登录", "ocr_languages": ["ch_sim", "en"]}
        image类型: {
            "image_category": "common",
            "image_path": "common/login.png", 
            "file_hash": "abc123...",
            "image_threshold": 0.85, 
            "rgb": false
        }
        pos类型: {"x": 100, "y": 200}
        region类型: {"x1": 100, "y1": 200, "x2": 300, "y2": 400}
        """
    )
    
    # 多分辨率配置（可选）
    resolution_configs = models.JSONField(
        default=dict,
        blank=True,
        verbose_name='分辨率配置',
        help_text='不同分辨率下的配置，如：{"1920x1080": {...}, "1280x720": {...}}'
    )
    
    # 使用统计
    usage_count = models.IntegerField(
        default=0,
        verbose_name='使用次数',
        help_text='该元素被用例引用的次数'
    )
    
    last_used_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='最后使用时间'
    )
    
    # 元数据
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_app_elements',
        verbose_name='创建人'
    )
    
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name='创建时间'
    )
    
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name='更新时间'
    )
    
    is_active = models.BooleanField(
        default=True,
        verbose_name='是否启用',
        help_text='软删除标记'
    )
    
    class Meta:
        db_table = 'app_elements'
        verbose_name = 'APP UI元素'
        verbose_name_plural = 'APP UI元素'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['element_type']),
            models.Index(fields=['name']),
            models.Index(fields=['is_active']),
            models.Index(fields=['folder'], name='app_element_folder__51ec29_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['folder', 'name'],
                name='app_element_folder_name_uniq',
            ),
        ]
    
    def __str__(self):
        return f"[{self.get_element_type_display()}] {self.name}"
    
    def increment_usage(self):
        """增加使用次数"""
        self.usage_count = models.F('usage_count') + 1
        self.last_used_at = timezone.now()
        self.save(update_fields=['usage_count', 'last_used_at'])


class AppComponent(models.Model):
    """APP UI组件定义, 用于UI场景编排与校验"""
    name = models.CharField(max_length=100, verbose_name='组件名称')
    type = models.CharField(max_length=50, unique=True, verbose_name='组件类型')
    category = models.CharField(max_length=50, blank=True, default='', verbose_name='类别')
    description = models.TextField(blank=True, default='', verbose_name='描述')
    schema = models.JSONField(default=dict, verbose_name='配置Schema')
    default_config = models.JSONField(default=dict, verbose_name='默认配置')
    enabled = models.BooleanField(default=True, verbose_name='是否启用')
    sort_order = models.IntegerField(default=0, verbose_name='排序')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'app_components'
        verbose_name = 'APP组件定义'
        verbose_name_plural = 'APP组件定义'
        ordering = ['sort_order', '-updated_at']

    def __str__(self):
        return f"{self.name} ({self.type})"


class AppCustomComponent(models.Model):
    """APP UI自定义组件定义, 由基础组件组合而成"""
    name = models.CharField(max_length=100, verbose_name='组件名称')
    type = models.CharField(max_length=50, unique=True, verbose_name='组件类型')
    description = models.TextField(blank=True, default='', verbose_name='描述')
    schema = models.JSONField(default=dict, verbose_name='参数Schema')
    default_config = models.JSONField(default=dict, verbose_name='默认参数')
    steps = models.JSONField(default=list, verbose_name='组合步骤')
    enabled = models.BooleanField(default=True, verbose_name='是否启用')
    sort_order = models.IntegerField(default=0, verbose_name='排序')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'app_custom_components'
        verbose_name = 'APP自定义组件'
        verbose_name_plural = 'APP自定义组件'
        ordering = ['sort_order', '-updated_at']

    def __str__(self):
        return f"{self.name} ({self.type})"


class AppComponentPackage(models.Model):
    """APP UI组件包(用于导入/安装组件定义)"""
    SOURCE_CHOICES = [
        ('upload', '上传'),
        ('market', '市场'),
        ('local', '本地'),
    ]

    name = models.CharField(max_length=100, verbose_name='包名称')
    version = models.CharField(max_length=50, blank=True, default='', verbose_name='版本')
    description = models.TextField(blank=True, default='', verbose_name='描述')
    author = models.CharField(max_length=100, blank=True, default='', verbose_name='作者')
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='upload', verbose_name='来源')
    manifest = models.JSONField(default=dict, verbose_name='包清单')
    created_by = models.ForeignKey(
        User, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='created_app_packages',
        verbose_name='创建人'
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'app_component_packages'
        verbose_name = 'APP组件包'
        verbose_name_plural = 'APP组件包'
        ordering = ['-updated_at']

    def __str__(self):
        return f"{self.name} ({self.version})"


class AppPackage(models.Model):
    """Android 包名 / iOS Bundle ID 管理。"""
    
    name = models.CharField(
        max_length=100,
        verbose_name='应用名称',
        help_text='友好的应用名称，如：系统设置'
    )
    
    package_name = models.CharField(
        max_length=255,
        unique=True,
        verbose_name='应用标识',
        help_text='Android 包名或 iOS Bundle ID，如：com.example.app'
    )
    
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_app_package_names',
        verbose_name='创建人'
    )
    
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name='创建时间'
    )
    
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name='更新时间'
    )
    
    class Meta:
        db_table = 'app_packages'
        verbose_name = 'APP应用标识'
        verbose_name_plural = 'APP应用标识管理'
        ordering = ['name']
        indexes = [
            models.Index(fields=['package_name']),
            models.Index(fields=['name']),
        ]
    
    def __str__(self):
        return f"{self.name} ({self.package_name})"


class AppTestSuite(models.Model):
    """APP测试套件"""
    EXECUTION_STATUS_CHOICES = [
        ('not_run', '未执行'),
        ('running', '执行中'),
        ('completed', '已完成'),
        ('error', '执行异常'),
        ('stopped', '已停止'),
    ]
    EXECUTION_RESULT_CHOICES = [
        ('passed', '通过'),
        ('failed', '失败'),
        ('skipped', '跳过'),
    ]

    project = models.ForeignKey(
        AppProject, on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='test_suites', verbose_name='所属项目'
    )
    name = models.CharField(max_length=200, verbose_name='套件名称')
    description = models.TextField(blank=True, default='', verbose_name='套件描述')
    test_cases = models.ManyToManyField(
        'AppTestCase',
        through='AppTestSuiteCase',
        verbose_name='测试用例',
        blank=True
    )

    # 执行统计
    execution_status = models.CharField(
        max_length=20,
        choices=EXECUTION_STATUS_CHOICES,
        default='not_run',
        verbose_name='执行状态'
    )
    execution_result = models.CharField(
        max_length=20,
        choices=EXECUTION_RESULT_CHOICES,
        null=True,
        blank=True,
        default=None,
        verbose_name='测试结果'
    )
    passed_count = models.IntegerField(default=0, verbose_name='通过用例数')
    failed_count = models.IntegerField(default=0, verbose_name='失败用例数')
    last_run_at = models.DateTimeField(null=True, blank=True, verbose_name='最后执行时间')

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_app_test_suites',
        verbose_name='创建人'
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'app_test_suites'
        verbose_name = 'APP测试套件'
        verbose_name_plural = 'APP测试套件'
        ordering = ['-updated_at']

    def __str__(self):
        return self.name

    @property
    def test_case_count(self):
        return self.suite_cases.count()


class AppTestSuiteCase(models.Model):
    """APP测试套件与用例的关联模型"""
    test_suite = models.ForeignKey(
        AppTestSuite,
        on_delete=models.CASCADE,
        related_name='suite_cases',
        verbose_name='测试套件'
    )
    test_case = models.ForeignKey(
        'AppTestCase',
        on_delete=models.CASCADE,
        related_name='suite_memberships',
        verbose_name='测试用例'
    )
    order = models.IntegerField(default=0, verbose_name='执行顺序')

    class Meta:
        db_table = 'app_test_suite_cases'
        verbose_name = 'APP套件用例关联'
        verbose_name_plural = 'APP套件用例关联'
        ordering = ['order']
        unique_together = ['test_suite', 'test_case']

    def __str__(self):
        return f'{self.test_suite.name} - {self.test_case.name}'


class AppTestCase(models.Model):
    """APP测试用例"""
    project = models.ForeignKey(
        AppProject, on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='test_cases', verbose_name='所属项目'
    )
    name = models.CharField(max_length=200, verbose_name='用例名称')
    description = models.TextField(blank=True, default='', verbose_name='用例描述')
    app_package = models.ForeignKey(
        AppPackage, 
        on_delete=models.CASCADE, 
        related_name='test_cases',
        null=True,
        blank=True,
        verbose_name='应用包名'
    )
    ui_flow = models.JSONField(default=dict, verbose_name='UI流程定义', help_text='UI Flow JSON配置')
    variables = models.JSONField(default=list, verbose_name='变量定义', help_text='测试变量列表')
    
    # 用例配置
    timeout = models.IntegerField(default=300, verbose_name='超时时间(秒)', help_text='默认5分钟')
    retry_count = models.IntegerField(default=0, verbose_name='失败重试次数')
    
    # 元数据
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_app_test_cases',
        verbose_name='创建人'
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    
    class Meta:
        db_table = 'app_test_cases'
        verbose_name = 'APP测试用例'
        verbose_name_plural = 'APP测试用例'
        ordering = ['-updated_at']
    
    def __str__(self):
        return self.name


class AppTestExecution(models.Model):
    """APP测试执行记录"""
    STATUS_CHOICES = [
        (ExecutionStatus.PENDING, '等待中'),
        (ExecutionStatus.RUNNING, '执行中'),
        (ExecutionStatus.COMPLETED, '已完成'),
        (ExecutionStatus.ERROR, '执行异常'),
        (ExecutionStatus.STOPPED, '已停止'),
    ]

    RESULT_CHOICES = [
        (ExecutionResult.PASSED, '通过'),
        (ExecutionResult.FAILED, '失败'),
        (ExecutionResult.SKIPPED, '跳过'),
    ]
    
    test_case = models.ForeignKey(
        AppTestCase, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='executions', 
        verbose_name='测试用例'
    )
    test_suite = models.ForeignKey(
        AppTestSuite,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='executions',
        verbose_name='所属套件'
    )
    device = models.ForeignKey(
        AppDevice, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='executions', 
        verbose_name='执行设备'
    )
    user = models.ForeignKey(
        User, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='app_test_executions', 
        verbose_name='执行用户'
    )
    status = models.CharField(
        max_length=20, 
        choices=STATUS_CHOICES, 
        default=ExecutionStatus.PENDING, 
        verbose_name='执行状态'
    )
    result = models.CharField(
        max_length=20,
        choices=RESULT_CHOICES,
        null=True,
        blank=True,
        default=None,
        verbose_name='测试结果'
    )
    task_id = models.CharField(
        max_length=255, 
        blank=True, 
        default='', 
        verbose_name='Celery任务ID', 
        help_text='用于停止任务'
    )
    progress = models.IntegerField(default=0, verbose_name='执行进度(0-100)')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='结束时间')
    duration = models.FloatField(default=0, verbose_name='执行时长(秒)')
    report_path = models.CharField(max_length=500, blank=True, default='', verbose_name='Allure报告路径')
    error_message = models.TextField(blank=True, default='', verbose_name='错误信息')
    runtime_override = models.JSONField(
        default=list,
        blank=True,
        verbose_name='执行前数据覆盖',
        help_text='仅用于本次执行的 runtimeOverride，不修改原始用例',
    )
    runtime_data = models.JSONField(
        default=list,
        blank=True,
        verbose_name='实际执行数据',
        help_text='RuntimeCase 合并并解析动态变量后的最终输入数据',
    )
    runtime_context = models.JSONField(
        default=dict,
        blank=True,
        verbose_name='运行上下文',
        help_text='模板、数据集和步骤输出注入的运行态上下文',
    )
    cleanup_config = models.JSONField(
        default=dict,
        blank=True,
        verbose_name='数据清理配置',
        help_text='执行完成后的运行态数据清理配置',
    )
    runtime_template_id = models.PositiveIntegerField(null=True, blank=True, verbose_name='运行态数据模板ID')
    runtime_dataset_id = models.PositiveIntegerField(null=True, blank=True, verbose_name='运行态数据集ID')
    runtime_dataset_row_index = models.IntegerField(null=True, blank=True, verbose_name='运行态数据集行号')
    
    # 执行结果统计
    total_steps = models.IntegerField(default=0, verbose_name='总步骤数')
    passed_steps = models.IntegerField(default=0, verbose_name='通过步骤数')
    failed_steps = models.IntegerField(default=0, verbose_name='失败步骤数')
    skipped_steps = models.IntegerField(default=0, verbose_name='跳过步骤数')
    
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    
    class Meta:
        db_table = 'app_test_executions'
        verbose_name = 'APP测试执行记录'
        verbose_name_plural = 'APP测试执行记录'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['-created_at']),
        ]
    
    def __str__(self):
        return f"{self.test_case.name} - {self.get_status_display()}"
    
    @property
    def case_name(self):
        """用例名称"""
        return self.test_case.name if self.test_case else ''
    
    @property
    def device_name(self):
        """设备名称"""
        return self.device.device_id if self.device else ''
    
    @property
    def user_name(self):
        """用户名"""
        return self.user.username if self.user else ''
    
    @property
    def pass_rate(self):
        """通过率"""
        if self.total_steps == 0:
            return 0
        return round((self.passed_steps / self.total_steps) * 100, 2)


class AppAgentTask(models.Model):
    """由 AI 规划并驱动现有 APP 自动化资产执行的任务。"""

    STATUS_CHOICES = [
        ('pending', '等待中'),
        ('planning', '规划中'),
        ('needs_input', '待补充执行条件'),
        ('review_required', '待评审草稿'),
        ('ready', '待执行'),
        ('executing', '执行中'),
        ('runtime_executing', '自治执行中'),
        ('awaiting_approval', '等待敏感操作审批'),
        ('matrix_executing', '兼容性矩阵执行中'),
        ('matrix_approval', '兼容性矩阵等待审批'),
        ('analyzing', '分析中'),
        ('completed', '已完成'),
        ('failed', '失败'),
        ('stopped', '已停止'),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='app_agent_tasks',
        verbose_name='创建用户',
    )
    project = models.ForeignKey(
        AppProject,
        on_delete=models.CASCADE,
        related_name='agent_tasks',
        verbose_name='所属项目',
    )
    device = models.ForeignKey(
        AppDevice,
        on_delete=models.PROTECT,
        related_name='agent_tasks',
        verbose_name='执行设备',
    )
    app_package = models.ForeignKey(
        AppPackage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='agent_tasks',
        verbose_name='应用包名',
    )
    parent_task = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='child_tasks',
        verbose_name='父 Agent 任务',
    )
    execution_mode = models.CharField(
        max_length=20,
        choices=[('standard', '标准任务'), ('matrix_child', '兼容性矩阵子任务')],
        default='standard',
        db_index=True,
        verbose_name='执行模式',
    )
    goal = models.TextField(verbose_name='测试目标')
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
        db_index=True,
        verbose_name='任务状态',
    )
    progress = models.PositiveSmallIntegerField(default=0, verbose_name='任务进度')
    auto_execute = models.BooleanField(default=False, verbose_name='规划完成后自动执行')
    plan = models.JSONField(default=dict, blank=True, verbose_name='测试计划')
    execution_ids = models.JSONField(default=list, blank=True, verbose_name='执行记录 ID')
    current_execution = models.ForeignKey(
        AppTestExecution,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='current_agent_tasks',
        verbose_name='当前执行记录',
    )
    result_summary = models.JSONField(default=dict, blank=True, verbose_name='结果汇总')
    failure_analysis = models.JSONField(default=dict, blank=True, verbose_name='失败分析')
    defect_draft = models.JSONField(default=list, blank=True, verbose_name='缺陷草稿')
    task_id = models.CharField(max_length=255, blank=True, default='', verbose_name='Celery 任务 ID')
    error_message = models.TextField(blank=True, default='', verbose_name='错误信息')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='完成时间')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'app_agent_tasks'
        verbose_name = 'APP AI Agent 任务'
        verbose_name_plural = 'APP AI Agent 任务'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', '-created_at'], name='app_agent_user_created_idx'),
            models.Index(fields=['project', 'status'], name='app_agent_project_status_idx'),
        ]

    def __str__(self):
        return f'{self.goal[:40]} - {self.get_status_display()}'


class AppAgentRuntimeSession(models.Model):
    """APP Agent 三期的有界观察-决策-执行会话。"""

    STATUS_CHOICES = [
        ('pending', '等待中'),
        ('running', '运行中'),
        ('awaiting_approval', '等待审批'),
        ('completed', '已完成'),
        ('failed', '失败'),
        ('budget_exhausted', '预算耗尽'),
        ('stopped', '已停止'),
    ]

    task = models.ForeignKey(
        AppAgentTask,
        on_delete=models.CASCADE,
        related_name='runtime_sessions',
        verbose_name='Agent 任务',
    )
    status = models.CharField(
        max_length=24,
        choices=STATUS_CHOICES,
        default='pending',
        db_index=True,
        verbose_name='会话状态',
    )
    max_steps = models.PositiveSmallIntegerField(default=20, verbose_name='最大动作步数')
    max_duration_seconds = models.PositiveIntegerField(default=600, verbose_name='最大运行秒数')
    max_model_tokens = models.PositiveIntegerField(default=6000, verbose_name='最大模型 Token')
    repeat_state_limit = models.PositiveSmallIntegerField(default=3, verbose_name='重复页面上限')
    used_steps = models.PositiveSmallIntegerField(default=0, verbose_name='已用动作步数')
    used_duration_seconds = models.FloatField(default=0, verbose_name='已用运行秒数')
    used_model_tokens = models.PositiveIntegerField(default=0, verbose_name='已用模型 Token')
    repeated_state_count = models.PositiveSmallIntegerField(default=0, verbose_name='连续重复页面次数')
    current_observation = models.JSONField(default=dict, blank=True, verbose_name='当前观察')
    current_decision = models.JSONField(default=dict, blank=True, verbose_name='当前决策')
    current_action = models.JSONField(default=dict, blank=True, verbose_name='当前动作')
    completed_candidate_ids = models.JSONField(default=list, blank=True, verbose_name='已处理候选动作')
    coverage = models.JSONField(default=dict, blank=True, verbose_name='探索路径与覆盖图')
    result_summary = models.JSONField(default=dict, blank=True, verbose_name='会话结果')
    telemetry_enabled = models.BooleanField(default=True, verbose_name='采集性能与崩溃遥测')
    telemetry_interval_steps = models.PositiveSmallIntegerField(default=2, verbose_name='遥测采样步骤间隔')
    telemetry_summary = models.JSONField(default=dict, blank=True, verbose_name='遥测汇总')
    stop_requested = models.BooleanField(default=False, verbose_name='请求停止')
    celery_task_id = models.CharField(max_length=255, blank=True, default='', verbose_name='Celery 任务 ID')
    error_message = models.TextField(blank=True, default='', verbose_name='错误信息')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='结束时间')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'app_agent_runtime_sessions'
        verbose_name = 'APP Agent 自治运行会话'
        verbose_name_plural = 'APP Agent 自治运行会话'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['task', '-created_at'], name='app_runtime_task_created_idx'),
            models.Index(fields=['status', '-updated_at'], name='app_runtime_status_time_idx'),
        ]

    def __str__(self):
        return f'Agent #{self.task_id} Runtime #{self.id} - {self.get_status_display()}'


class AppAgentMatrixRun(models.Model):
    """在多台已连接设备或模拟器上并行运行同一测试目标的兼容性矩阵。"""

    STATUS_CHOICES = [
        ('pending', '等待调度'),
        ('running', '执行中'),
        ('awaiting_approval', '等待审批'),
        ('completed', '已完成'),
        ('failed', '失败'),
        ('stopped', '已停止'),
    ]

    base_task = models.ForeignKey(
        AppAgentTask,
        on_delete=models.CASCADE,
        related_name='matrix_runs',
        verbose_name='基础 Agent 任务',
    )
    name = models.CharField(max_length=200, blank=True, default='', verbose_name='矩阵名称')
    status = models.CharField(
        max_length=24,
        choices=STATUS_CHOICES,
        default='pending',
        db_index=True,
        verbose_name='矩阵状态',
    )
    max_concurrency = models.PositiveSmallIntegerField(default=2, verbose_name='最大并发设备数')
    budgets = models.JSONField(default=dict, blank=True, verbose_name='单设备运行预算')
    device_ids = models.JSONField(default=list, blank=True, verbose_name='设备 ID 列表')
    summary = models.JSONField(default=dict, blank=True, verbose_name='兼容性汇总')
    error_message = models.TextField(blank=True, default='', verbose_name='错误信息')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='完成时间')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'app_agent_matrix_runs'
        verbose_name = 'APP Agent 兼容性矩阵'
        verbose_name_plural = 'APP Agent 兼容性矩阵'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['base_task', '-created_at'], name='app_matrix_task_time_idx'),
            models.Index(fields=['status', '-updated_at'], name='app_matrix_status_time_idx'),
        ]

    def __str__(self):
        return self.name or f'Compatibility Matrix #{self.id}'


class AppAgentMatrixCell(models.Model):
    """兼容性矩阵中的单设备自治执行单元。"""

    STATUS_CHOICES = [
        ('pending', '等待调度'),
        ('queued', '已入队'),
        ('running', '执行中'),
        ('awaiting_approval', '等待审批'),
        ('completed', '通过'),
        ('failed', '失败'),
        ('budget_exhausted', '预算耗尽'),
        ('stopped', '已停止'),
    ]

    matrix_run = models.ForeignKey(
        AppAgentMatrixRun,
        on_delete=models.CASCADE,
        related_name='cells',
        verbose_name='兼容性矩阵',
    )
    device = models.ForeignKey(
        AppDevice,
        on_delete=models.PROTECT,
        related_name='agent_matrix_cells',
        verbose_name='执行设备',
    )
    task = models.OneToOneField(
        AppAgentTask,
        on_delete=models.CASCADE,
        related_name='matrix_cell',
        verbose_name='设备子任务',
    )
    runtime_session = models.OneToOneField(
        AppAgentRuntimeSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='matrix_cell',
        verbose_name='自治会话',
    )
    status = models.CharField(
        max_length=24,
        choices=STATUS_CHOICES,
        default='pending',
        db_index=True,
        verbose_name='单元状态',
    )
    platform = models.CharField(max_length=20, blank=True, default='', verbose_name='平台快照')
    platform_version = models.CharField(max_length=50, blank=True, default='', verbose_name='系统版本快照')
    compatibility = models.CharField(max_length=30, blank=True, default='pending', verbose_name='兼容性结论')
    result_summary = models.JSONField(default=dict, blank=True, verbose_name='执行汇总')
    telemetry_summary = models.JSONField(default=dict, blank=True, verbose_name='遥测汇总')
    error_message = models.TextField(blank=True, default='', verbose_name='错误信息')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='完成时间')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'app_agent_matrix_cells'
        verbose_name = 'APP Agent 兼容性矩阵单元'
        verbose_name_plural = 'APP Agent 兼容性矩阵单元'
        ordering = ['id']
        constraints = [
            models.UniqueConstraint(
                fields=['matrix_run', 'device'],
                name='app_matrix_device_uniq',
            ),
        ]
        indexes = [models.Index(fields=['matrix_run', 'status'], name='app_matrix_cell_status_idx')]

    def __str__(self):
        return f'{self.matrix_run} / {self.device}'


class AppAgentRuntimeStep(models.Model):
    """自治会话中一次完整的观察、决策、动作和证据。"""

    STATUS_CHOICES = [
        ('decided', '已决策'),
        ('awaiting_approval', '等待审批'),
        ('running', '执行中'),
        ('passed', '通过'),
        ('failed', '失败'),
        ('rejected', '审批拒绝'),
        ('skipped', '跳过'),
    ]
    APPROVAL_CHOICES = [
        ('not_required', '无需审批'),
        ('pending', '待审批'),
        ('approved', '已批准'),
        ('rejected', '已拒绝'),
    ]

    session = models.ForeignKey(
        AppAgentRuntimeSession,
        on_delete=models.CASCADE,
        related_name='steps',
        verbose_name='运行会话',
    )
    sequence = models.PositiveSmallIntegerField(verbose_name='决策序号')
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default='decided', verbose_name='步骤状态')
    observation = models.JSONField(default=dict, blank=True, verbose_name='执行前观察')
    observation_hash = models.CharField(max_length=64, blank=True, default='', db_index=True, verbose_name='页面状态哈希')
    decision = models.JSONField(default=dict, blank=True, verbose_name='决策说明')
    action = models.JSONField(default=dict, blank=True, verbose_name='受控动作')
    element = models.ForeignKey(
        AppElement,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='agent_runtime_steps',
        verbose_name='引用元素',
    )
    approval_status = models.CharField(
        max_length=20,
        choices=APPROVAL_CHOICES,
        default='not_required',
        verbose_name='审批状态',
    )
    approval_category = models.CharField(max_length=40, blank=True, default='', verbose_name='审批风险类型')
    approval_note = models.CharField(max_length=500, blank=True, default='', verbose_name='审批备注')
    approved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_app_agent_runtime_steps',
        verbose_name='审批人',
    )
    approved_at = models.DateTimeField(null=True, blank=True, verbose_name='审批时间')
    selected_locator_strategy = models.CharField(max_length=40, blank=True, default='', verbose_name='命中定位策略')
    evidence = models.JSONField(default=dict, blank=True, verbose_name='执行证据')
    error_message = models.TextField(blank=True, default='', verbose_name='错误信息')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='完成时间')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'app_agent_runtime_steps'
        verbose_name = 'APP Agent 自治运行步骤'
        verbose_name_plural = 'APP Agent 自治运行步骤'
        ordering = ['sequence', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['session', 'sequence'],
                name='app_runtime_session_sequence_uniq',
            ),
        ]
        indexes = [models.Index(fields=['session', 'status'], name='app_runtime_step_status_idx')]

    def __str__(self):
        return f'Runtime #{self.session_id} Step {self.sequence} - {self.get_status_display()}'


class AppLocatorKnowledge(models.Model):
    """按项目和设备版本沉淀的定位策略成功历史，可审计并支持回滚。"""

    project = models.ForeignKey(
        AppProject,
        on_delete=models.CASCADE,
        related_name='locator_knowledge',
        verbose_name='所属项目',
    )
    element = models.ForeignKey(
        AppElement,
        on_delete=models.CASCADE,
        related_name='locator_knowledge',
        verbose_name='元素',
    )
    platform = models.CharField(max_length=20, blank=True, default='', verbose_name='平台')
    platform_version = models.CharField(max_length=50, blank=True, default='', verbose_name='系统版本')
    app_package = models.CharField(max_length=255, blank=True, default='', verbose_name='应用包名')
    strategy = models.CharField(max_length=40, verbose_name='定位策略')
    success_count = models.PositiveIntegerField(default=0, verbose_name='成功次数')
    failure_count = models.PositiveIntegerField(default=0, verbose_name='失败次数')
    confidence = models.FloatField(default=0.5, verbose_name='可信度')
    last_diagnostics = models.JSONField(default=dict, blank=True, verbose_name='最近诊断')
    rollback_point = models.JSONField(default=dict, blank=True, verbose_name='回滚点')
    last_success_at = models.DateTimeField(null=True, blank=True, verbose_name='最后成功时间')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'app_locator_knowledge'
        verbose_name = 'APP 定位知识'
        verbose_name_plural = 'APP 定位知识'
        ordering = ['-confidence', '-updated_at']
        constraints = [
            models.UniqueConstraint(
                fields=['project', 'element', 'platform', 'platform_version', 'app_package', 'strategy'],
                name='app_loc_scope_uniq',
            ),
        ]
        indexes = [
            models.Index(fields=['project', 'element'], name='app_loc_proj_elem_idx'),
        ]

    def __str__(self):
        return f'{self.project} / {self.element} / {self.strategy}'


class AppAgentEvent(models.Model):
    """APP Agent 的可审计执行事件流。"""

    LEVEL_CHOICES = [
        ('info', '信息'),
        ('success', '成功'),
        ('warning', '警告'),
        ('error', '错误'),
    ]

    task = models.ForeignKey(
        AppAgentTask,
        on_delete=models.CASCADE,
        related_name='events',
        verbose_name='Agent 任务',
    )
    phase = models.CharField(max_length=40, db_index=True, verbose_name='阶段')
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, default='info', verbose_name='级别')
    message = models.CharField(max_length=500, verbose_name='事件信息')
    payload = models.JSONField(default=dict, blank=True, verbose_name='事件数据')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'app_agent_events'
        verbose_name = 'APP AI Agent 事件'
        verbose_name_plural = 'APP AI Agent 事件'
        ordering = ['created_at', 'id']
        indexes = [models.Index(fields=['task', 'created_at'], name='app_agent_event_time_idx')]

    def __str__(self):
        return f'{self.task_id} - {self.phase} - {self.message[:40]}'


class AppScheduledTask(models.Model):
    """APP自动化定时任务"""
    TASK_TYPE_CHOICES = [
        ('TEST_SUITE', '测试套件执行'),
        ('TEST_CASE', '测试用例执行'),
    ]
    STATUS_CHOICES = [
        ('ACTIVE', '激活'),
        ('PAUSED', '暂停'),
        ('COMPLETED', '已完成'),
        ('FAILED', '失败'),
    ]
    TRIGGER_TYPE_CHOICES = [
        ('CRON', 'Cron表达式'),
        ('INTERVAL', '固定间隔'),
        ('ONCE', '单次执行'),
    ]
    NOTIFICATION_TYPE_CHOICES = [
        ('email', '邮箱通知'),
        ('webhook', 'Webhook机器人'),
        ('both', '两者都发送'),
    ]

    project = models.ForeignKey(
        'AppProject', on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='scheduled_tasks', verbose_name='所属项目'
    )
    name = models.CharField(max_length=200, verbose_name='任务名称')
    description = models.TextField(blank=True, default='', verbose_name='任务描述')
    task_type = models.CharField(max_length=20, choices=TASK_TYPE_CHOICES, verbose_name='任务类型')
    trigger_type = models.CharField(max_length=20, choices=TRIGGER_TYPE_CHOICES, verbose_name='触发器类型')

    # 调度配置
    cron_expression = models.CharField(max_length=100, blank=True, default='', verbose_name='Cron表达式')
    interval_seconds = models.IntegerField(null=True, blank=True, verbose_name='间隔秒数')
    execute_at = models.DateTimeField(null=True, blank=True, verbose_name='执行时间')

    # APP 特有配置
    device = models.ForeignKey(
        AppDevice, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='scheduled_tasks', verbose_name='执行设备'
    )
    app_package = models.ForeignKey(
        AppPackage, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='scheduled_tasks', verbose_name='应用包名'
    )
    test_suite = models.ForeignKey(
        AppTestSuite, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='scheduled_tasks', verbose_name='测试套件'
    )
    test_case = models.ForeignKey(
        AppTestCase, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='scheduled_tasks', verbose_name='测试用例'
    )

    # 通知配置
    notify_on_success = models.BooleanField(default=False, verbose_name='成功时通知')
    notify_on_failure = models.BooleanField(default=False, verbose_name='失败时通知')
    notification_type = models.CharField(
        max_length=20, blank=True, default='',
        choices=NOTIFICATION_TYPE_CHOICES, verbose_name='通知类型'
    )
    notify_emails = models.JSONField(default=list, blank=True, verbose_name='通知邮箱列表')

    # 状态与统计
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE', verbose_name='任务状态')
    last_run_time = models.DateTimeField(null=True, blank=True, verbose_name='最后运行时间')
    next_run_time = models.DateTimeField(null=True, blank=True, verbose_name='下次运行时间')
    total_runs = models.IntegerField(default=0, verbose_name='总运行次数')
    successful_runs = models.IntegerField(default=0, verbose_name='成功次数')
    failed_runs = models.IntegerField(default=0, verbose_name='失败次数')
    last_result = models.JSONField(default=dict, verbose_name='最后执行结果')
    error_message = models.TextField(blank=True, default='', verbose_name='错误信息')

    created_by = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name='创建者')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'app_scheduled_tasks'
        verbose_name = 'APP定时任务'
        verbose_name_plural = 'APP定时任务'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.get_task_type_display()})"

    def calculate_next_run(self):
        from datetime import timedelta
        from croniter import croniter
        now = timezone.now()
        if self.trigger_type == 'CRON' and self.cron_expression:
            try:
                cron = croniter(self.cron_expression, now)
                return cron.get_next(type(now))
            except Exception:
                return None
        elif self.trigger_type == 'INTERVAL' and self.interval_seconds:
            return now + timedelta(seconds=self.interval_seconds)
        elif self.trigger_type == 'ONCE' and self.execute_at:
            return self.execute_at if self.execute_at > now else None
        return None

    def should_run_now(self):
        if self.status != 'ACTIVE':
            return False
        if not self.next_run_time:
            return False
        return timezone.now() >= self.next_run_time


class AppNotificationLog(models.Model):
    """APP自动化通知日志"""
    NOTIFICATION_TYPES = [
        ('task_execution', '定时任务执行'),
        ('test_suite_execution', '测试套件执行'),
        ('system_alert', '系统警告'),
        ('manual', '手动通知'),
    ]
    STATUS_CHOICES = [
        ('pending', '待发送'),
        ('sending', '发送中'),
        ('success', '发送成功'),
        ('failed', '发送失败'),
        ('cancelled', '已取消'),
    ]

    task = models.ForeignKey(
        AppScheduledTask, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='notification_logs', verbose_name='关联任务'
    )
    task_name = models.CharField(max_length=200, verbose_name='任务名称')
    task_type = models.CharField(max_length=20, blank=True, default='', verbose_name='任务类型快照')
    notification_type = models.CharField(max_length=50, choices=NOTIFICATION_TYPES, verbose_name='通知类型')
    sender_name = models.CharField(max_length=100, verbose_name='发件人姓名')
    sender_email = models.EmailField(verbose_name='发件人邮箱')
    recipient_info = models.JSONField(default=list, verbose_name='收件人信息')
    webhook_bot_info = models.JSONField(default=dict, blank=True, verbose_name='Webhook机器人信息')
    notification_content = models.TextField(verbose_name='通知内容')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name='发送状态')
    error_message = models.TextField(blank=True, default='', verbose_name='错误信息')
    response_info = models.JSONField(default=dict, blank=True, verbose_name='响应信息')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    sent_at = models.DateTimeField(null=True, blank=True, verbose_name='发送时间')
    retry_count = models.IntegerField(default=0, verbose_name='重试次数')
    is_retried = models.BooleanField(default=False, verbose_name='是否已重试')

    class Meta:
        db_table = 'app_notification_logs'
        verbose_name = 'APP通知日志'
        verbose_name_plural = 'APP通知日志'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['notification_type']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"{self.task_name} - {self.get_notification_type_display()} - {self.status}"

    def get_recipient_names(self):
        if not self.recipient_info:
            return '未知收件人'
        if isinstance(self.recipient_info, list):
            names = []
            for rec in self.recipient_info:
                email = rec.get('email', '')
                name = rec.get('name', '')
                names.append(f"{name}({email})" if name and email else (email or name or '未知'))
            return ', '.join(names)
        return '未知收件人'

    def get_retry_status(self):
        return f"已重试 {self.retry_count} 次" if self.is_retried else "未重试"
