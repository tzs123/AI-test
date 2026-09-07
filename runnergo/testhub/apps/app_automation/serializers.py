# -*- coding: utf-8 -*-
from rest_framework import serializers

from django.db.models import Q
from django.utils import timezone
from .models import (
    AppProject,
    AppTestConfig,
    AppDevice,
    AppElement,
    AppElementFolder,
    AppComponent,
    AppCustomComponent,
    AppComponentPackage,
    AppPackage,
    AppTestCase,
    AppTestSuite,
    AppTestSuiteCase,
    AppTestExecution,
    AppAgentTask,
    AppAgentEvent,
    AppAgentRuntimeSession,
    AppAgentRuntimeStep,
    AppAgentMatrixRun,
    AppAgentMatrixCell,
    AppLocatorKnowledge,
    AppScheduledTask,
    AppNotificationLog,
)
from .media_urls import enrich_evidence_media_urls
from .runtime_service import (
    redact_runtime_approval_input,
    runtime_approval_input_spec,
    runtime_approval_requires_input,
)
from .access import can_access_app_project
from .utils.element_references import collect_element_reference_ids, missing_active_element_ids
from .utils.recorded_steps import repair_recorded_input_targets_in_flow
from apps.test_assets.models import ApiAsset

# ========== 项目管理序列化器 ==========

class AppProjectSerializer(serializers.ModelSerializer):
    """APP项目序列化器 - 列表/详情"""
    owner_name = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()
    test_case_count = serializers.SerializerMethodField()
    test_suite_count = serializers.SerializerMethodField()

    class Meta:
        model = AppProject
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')

    def get_owner_name(self, obj):
        return obj.owner.username if obj.owner else None

    def get_member_count(self, obj):
        return obj.members.count()

    def get_test_case_count(self, obj):
        return obj.test_cases.count()

    def get_test_suite_count(self, obj):
        return obj.test_suites.count()


class AppProjectCreateSerializer(serializers.ModelSerializer):
    """APP项目创建序列化器"""
    class Meta:
        model = AppProject
        fields = ('name', 'description', 'status', 'start_date', 'end_date', 'members')
        extra_kwargs = {
            'members': {'required': False},
        }


class AppProjectUpdateSerializer(serializers.ModelSerializer):
    """APP项目更新序列化器"""
    class Meta:
        model = AppProject
        fields = ('name', 'description', 'status', 'start_date', 'end_date', 'members')


# ========== 配置序列化器 ==========

class AppTestConfigSerializer(serializers.ModelSerializer):
    """APP测试配置序列化器"""
    
    class Meta:
        model = AppTestConfig
        fields = [
            'id',
            'adb_path',
            'node_path',
            'appium_path',
            'appium_server_url',
            'appium_driver',
            'appium_automation_name',
            'ios_wda_url',
            'ios_wda_bundle_id',
            'ios_browser_start_url',
            'device_bridge_url',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class AppDeviceSerializer(serializers.ModelSerializer):
    """APP设备序列化器"""
    locked_by_name = serializers.SerializerMethodField()
    platform_display = serializers.CharField(source='get_platform_display', read_only=True)
    os_version = serializers.SerializerMethodField()
    
    class Meta:
        model = AppDevice
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')
    
    def get_locked_by_name(self, obj):
        return obj.locked_by.username if obj.locked_by else None

    def get_os_version(self, obj):
        return obj.ios_version if obj.platform == 'ios' else obj.android_version


class AppElementFolderSerializer(serializers.ModelSerializer):
    """APP 元素文件夹序列化器。"""

    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    element_count = serializers.SerializerMethodField()

    class Meta:
        model = AppElementFolder
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at', 'created_by')

    def get_element_count(self, obj):
        annotated = getattr(obj, 'element_count', None)
        if annotated is not None:
            return annotated
        return obj.elements.filter(is_active=True).count()


class AppElementSerializer(serializers.ModelSerializer):
    """APP元素序列化器"""
    created_by_name = serializers.SerializerMethodField()
    element_type_display = serializers.CharField(source='get_element_type_display', read_only=True)
    preview_url = serializers.SerializerMethodField()
    folder_name = serializers.CharField(source='folder.name', read_only=True)
    
    class Meta:
        model = AppElement
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at', 'usage_count', 'last_used_at')
        validators = []
    
    def get_created_by_name(self, obj):
        return obj.created_by.username if obj.created_by else None
    
    def get_preview_url(self, obj):
        """获取图片预览 URL"""
        if obj.config:
            image_path = obj.config.get('image_path')
            if image_path:
                from .utils.image_helpers import get_element_image_url
                return get_element_image_url(image_path)
        return None

    def _requested_folder_id(self):
        """返回当前请求对应的文件夹 ID；未分组返回 None。"""
        if 'folder' in self.initial_data:
            raw_folder = self.initial_data.get('folder')
            if raw_folder in (None, '', 0, '0'):
                return None
            try:
                return int(raw_folder)
            except (TypeError, ValueError):
                return None
        return self.instance.folder_id if self.instance else None

    def validate(self, attrs):
        """元素名称只需在当前文件夹内唯一，不同文件夹允许同名。"""
        attrs = super().validate(attrs)
        request = self.context.get('request')
        project = attrs.get('project', getattr(self.instance, 'project', None))
        if project and request and not can_access_app_project(request.user, project):
            raise serializers.ValidationError({'project': '无权使用该 APP 项目'})
        name = str(attrs.get('name', getattr(self.instance, 'name', '')) or '').strip()
        folder = attrs.get('folder', getattr(self.instance, 'folder', None))
        attrs['name'] = name

        duplicate = AppElement.objects.filter(name=name, folder=folder)
        if self.instance:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            folder_name = folder.name if folder else '未分组'
            raise serializers.ValidationError({
                'name': f'文件夹“{folder_name}”内已存在同名元素，请更换名称或选择其他文件夹。'
            })
        return attrs
    
    def validate_config(self, value):
        """验证配置项"""
        element_type = self.initial_data.get('element_type') or getattr(self.instance, 'element_type', None)

        locator_strategies = value.get('locator_strategies')
        if locator_strategies is not None:
            if not isinstance(locator_strategies, list):
                raise serializers.ValidationError('locator_strategies 必须是数组')
            supported = {
                'css', 'webview_xpath',
                'resource_id', 'accessibility', 'text', 'xpath',
                'ocr', 'image', 'position', 'region',
            }
            seen = set()
            for index, item in enumerate(locator_strategies):
                if isinstance(item, str):
                    strategy_type = item
                elif isinstance(item, dict):
                    strategy_type = item.get('type')
                else:
                    raise serializers.ValidationError(f'第 {index + 1} 个定位策略格式无效')
                if strategy_type not in supported:
                    raise serializers.ValidationError(f'不支持的定位策略: {strategy_type}')
                if strategy_type in seen:
                    raise serializers.ValidationError(f'定位策略重复: {strategy_type}')
                seen.add(strategy_type)

        if element_type == 'appium':
            fingerprint = value.get('fingerprint') or {}
            identifiers = (
                value.get('resource_id'),
                value.get('accessibility_id'),
                value.get('text'),
                value.get('xpath'),
                value.get('webview_css'),
                value.get('webview_xpath'),
                fingerprint.get('resource_id'),
                fingerprint.get('content_desc'),
                fingerprint.get('text'),
            )
            if not any(str(item or '').strip() for item in identifiers):
                raise serializers.ValidationError(
                    '智能元素至少需要语义属性、WebView CSS 或 WebView XPath 中的一项'
                )

        elif element_type == 'ocr':
            if not str(value.get('ocr_text') or value.get('text') or '').strip():
                raise serializers.ValidationError('OCR元素必须包含 ocr_text 字段')
        
        elif element_type == 'image':
            if not value.get('image_path'):
                raise serializers.ValidationError('图片元素必须包含 image_path 字段')
            
            # 如果有 file_hash，可以进行重复检测（在视图层处理更合适）
            file_hash = value.get('file_hash')
            if file_hash:
                # 检查是否有其他元素使用相同哈希（排除自身）
                instance_id = self.instance.id if self.instance else None
                existing = AppElement.objects.filter(
                    config__file_hash=file_hash,
                    folder_id=self._requested_folder_id(),
                ).exclude(id=instance_id).first()
                
                if existing:
                    raise serializers.ValidationError(
                        f'当前文件夹内的相同图片已被元素 "{existing.name}" (ID: {existing.id}) 使用。'
                        f'建议使用现有元素、选择其他文件夹或上传不同的图片。'
                    )
        
        elif element_type == 'pos':
            # 坐标类型需要 x, y
            if 'x' not in value or 'y' not in value:
                raise serializers.ValidationError('坐标元素必须包含 x 和 y 字段')
        
        elif element_type == 'region':
            # 区域类型需要 x1, y1, x2, y2
            required_fields = ['x1', 'y1', 'x2', 'y2']
            missing_fields = [f for f in required_fields if f not in value]
            if missing_fields:
                raise serializers.ValidationError(
                    f'区域元素缺少必需字段: {", ".join(missing_fields)}'
                )
        
        return value
    
    def to_representation(self, instance):
        data = super().to_representation(instance)
        return data


class AppPackageSerializer(serializers.ModelSerializer):
    """APP应用包名序列化器"""
    created_by_name = serializers.SerializerMethodField()
    
    class Meta:
        model = AppPackage
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')
    
    def get_created_by_name(self, obj):
        return obj.created_by.username if obj.created_by else None


class AppTestCaseSerializer(serializers.ModelSerializer):
    """APP测试用例序列化器"""
    created_by_name = serializers.SerializerMethodField()
    app_package_name = serializers.CharField(source='app_package.name', read_only=True)
    
    class Meta:
        model = AppTestCase
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')
    
    def get_created_by_name(self, obj):
        return obj.created_by.username if obj.created_by else None

    def validate_ui_flow(self, value):
        missing_ids = missing_active_element_ids(value)
        if missing_ids:
            joined = ', '.join(str(element_id) for element_id in missing_ids)
            raise serializers.ValidationError(
                f'用例引用了不存在或已停用的元素: {joined}，请重新绑定后再保存。'
            )
        repaired_flow, repair_result = repair_recorded_input_targets_in_flow(
            value,
            reject_unfixed=True,
        )
        if repair_result.problems:
            messages = '；'.join(
                f"第 {item.get('index')} 步 {item.get('name')}: "
                f"{item.get('message') or item.get('reason')}"
                for item in repair_result.problems
            )
            raise serializers.ValidationError(
                '录制输入步骤缺少稳定定位，已阻止保存：' + messages
            )
        return repaired_flow

    def validate(self, attrs):
        attrs = super().validate(attrs)
        request = self.context.get('request')
        user = request.user if request else None
        project = attrs.get('project', getattr(self.instance, 'project', None))
        if project and user and not can_access_app_project(user, project):
            raise serializers.ValidationError({'project': '无权使用该 APP 项目'})

        ui_flow = attrs.get('ui_flow', getattr(self.instance, 'ui_flow', {}))
        reference_ids = collect_element_reference_ids(ui_flow)
        if reference_ids:
            elements = AppElement.objects.filter(id__in=reference_ids, is_active=True)
            if project:
                foreign_ids = list(
                    elements.exclude(Q(project=project) | Q(project__isnull=True))
                    .values_list('id', flat=True)
                )
            else:
                foreign_ids = list(
                    elements.exclude(project__isnull=True).values_list('id', flat=True)
                )
            if foreign_ids:
                raise serializers.ValidationError({
                    'ui_flow': f'流程引用了其他项目的元素: {sorted(foreign_ids)}'
                })
        return attrs


class AppTestExecutionSerializer(serializers.ModelSerializer):
    """APP测试执行记录序列化器"""
    case_name = serializers.CharField(read_only=True)
    device_name = serializers.CharField(read_only=True)
    user_name = serializers.CharField(read_only=True)
    pass_rate = serializers.FloatField(read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    result_display = serializers.CharField(source='get_result_display', read_only=True, default=None)
    report_url = serializers.SerializerMethodField()
    public_report_url = serializers.SerializerMethodField()
    assertions = serializers.SerializerMethodField()
    assertion_total = serializers.SerializerMethodField()
    assertion_failed = serializers.SerializerMethodField()
    
    class Meta:
        model = AppTestExecution
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at', 'started_at', 'finished_at', 'duration')

    def get_report_url(self, obj):
        if not obj.report_path:
            return ''
        from .report_urls import build_internal_report_path
        return build_internal_report_path(obj.id)

    def get_public_report_url(self, obj):
        if not obj.report_path:
            return ''
        from .report_urls import build_public_report_path
        return build_public_report_path(obj.id)

    @staticmethod
    def _assertion_results(obj):
        context = obj.runtime_context if isinstance(obj.runtime_context, dict) else {}
        results = context.get('assertions')
        return results if isinstance(results, list) else []

    def get_assertions(self, obj):
        return self._assertion_results(obj)

    def get_assertion_total(self, obj):
        return len(self._assertion_results(obj))

    def get_assertion_failed(self, obj):
        return sum(
            1 for item in self._assertion_results(obj)
            if isinstance(item, dict) and item.get('matched') is False
        )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        request = self.context.get('request')
        user = request.user if request else None
        test_case = attrs.get('test_case', getattr(self.instance, 'test_case', None))
        test_suite = attrs.get('test_suite', getattr(self.instance, 'test_suite', None))
        projects = [
            item.project for item in (test_case, test_suite)
            if item is not None and item.project_id
        ]
        if user and any(not can_access_app_project(user, project) for project in projects):
            raise serializers.ValidationError('无权使用所选 APP 测试资产')
        if len({project.id for project in projects}) > 1:
            raise serializers.ValidationError('测试用例与测试套件必须属于同一项目')
        return attrs


class AppAgentEventSerializer(serializers.ModelSerializer):
    """APP AI Agent 事件序列化器。"""

    class Meta:
        model = AppAgentEvent
        fields = '__all__'
        read_only_fields = ('task', 'phase', 'level', 'message', 'payload', 'created_at')


class AppAgentRuntimeStepSerializer(serializers.ModelSerializer):
    """自治 Agent 单步审计记录。"""

    element_name = serializers.CharField(source='element.name', read_only=True, default='')
    approved_by_name = serializers.CharField(source='approved_by.username', read_only=True, default='')
    approval_input = serializers.SerializerMethodField()

    class Meta:
        model = AppAgentRuntimeStep
        fields = '__all__'

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['manual_action_required'] = False
        if runtime_approval_requires_input(instance.action or {}, instance.approval_category):
            data['action'] = redact_runtime_approval_input(data.get('action') or {})
            data['input_provided'] = instance.approval_status == 'approved'
        data['observation'] = enrich_evidence_media_urls(data.get('observation') or {})
        data['evidence'] = enrich_evidence_media_urls(data.get('evidence') or {})
        return data

    def get_approval_input(self, obj):
        return runtime_approval_input_spec(obj.action or {}, obj.approval_category)


class AppAgentRuntimeSessionSerializer(serializers.ModelSerializer):
    """包含预算、当前状态、探索路径和步骤证据的自治会话详情。"""

    status_display = serializers.CharField(source='get_status_display', read_only=True)
    steps = AppAgentRuntimeStepSerializer(many=True, read_only=True)
    pending_approval = serializers.SerializerMethodField()

    class Meta:
        model = AppAgentRuntimeSession
        fields = '__all__'

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['current_observation'] = enrich_evidence_media_urls(data.get('current_observation') or {})
        return data

    def get_pending_approval(self, obj):
        runtime_step = next((
            item for item in reversed(list(obj.steps.all()))
            if item.status == 'awaiting_approval' and item.approval_status == 'pending'
        ), None)
        return AppAgentRuntimeStepSerializer(runtime_step).data if runtime_step else None


class AppLocatorKnowledgeSerializer(serializers.ModelSerializer):
    """项目级定位学习记录。"""

    element_name = serializers.CharField(source='element.name', read_only=True)

    class Meta:
        model = AppLocatorKnowledge
        fields = '__all__'


class AppAgentMatrixCellSerializer(serializers.ModelSerializer):
    """兼容性矩阵中的单设备结果。"""

    device_name = serializers.SerializerMethodField()
    device_identifier = serializers.CharField(source='device.device_id', read_only=True)
    runtime = AppAgentRuntimeSessionSerializer(source='runtime_session', read_only=True)
    pending_approval = serializers.SerializerMethodField()

    class Meta:
        model = AppAgentMatrixCell
        fields = '__all__'

    def get_device_name(self, obj):
        return obj.device.name or obj.device.device_id

    def get_pending_approval(self, obj):
        if not obj.runtime_session_id or obj.status != 'awaiting_approval':
            return None
        runtime_step = obj.runtime_session.steps.filter(
            status='awaiting_approval',
            approval_status='pending',
        ).order_by('-sequence').first()
        return AppAgentRuntimeStepSerializer(runtime_step).data if runtime_step else None


class AppAgentMatrixRunSerializer(serializers.ModelSerializer):
    """多设备兼容性矩阵详情。"""

    status_display = serializers.CharField(source='get_status_display', read_only=True)
    cells = AppAgentMatrixCellSerializer(many=True, read_only=True)

    class Meta:
        model = AppAgentMatrixRun
        fields = '__all__'


class AppAgentTaskSerializer(serializers.ModelSerializer):
    """APP AI Agent 任务详情序列化器。"""

    app_case_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, write_only=True, default=list,
    )
    app_test_case_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, write_only=True, default=list,
    )
    app_test_object_refs = serializers.JSONField(required=False, write_only=True, default=list)
    api_test_objects = serializers.JSONField(required=False, write_only=True, default=list)
    test_object_refs = serializers.JSONField(required=False, write_only=True, default=list)
    source_entry = serializers.ChoiceField(
        choices=('requirement', 'reference'), required=False, write_only=True, default='requirement',
    )
    reference_module = serializers.CharField(required=False, allow_blank=True, write_only=True, default='')
    test_data_count = serializers.IntegerField(required=False, write_only=True, default=3, min_value=1, max_value=20)
    project_name = serializers.CharField(source='project.name', read_only=True)
    device_name = serializers.SerializerMethodField()
    device_identifier = serializers.CharField(source='device.device_id', read_only=True)
    device_platform = serializers.CharField(source='device.platform', read_only=True)
    package_label = serializers.CharField(source='app_package.name', read_only=True, default='')
    package_name = serializers.CharField(source='app_package.package_name', read_only=True, default='')
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    current_execution_detail = AppTestExecutionSerializer(source='current_execution', read_only=True)
    event_count = serializers.IntegerField(source='events.count', read_only=True)
    latest_runtime = serializers.SerializerMethodField()
    latest_matrix = serializers.SerializerMethodField()
    execution_reports = serializers.SerializerMethodField()
    execution_results = serializers.SerializerMethodField()

    class Meta:
        model = AppAgentTask
        fields = '__all__'
        extra_kwargs = {
            'device': {'required': False},
        }
        read_only_fields = (
            'user', 'status', 'progress', 'plan', 'execution_ids', 'current_execution',
            'result_summary', 'failure_analysis', 'defect_draft', 'task_id', 'error_message',
            'started_at', 'finished_at', 'created_at', 'updated_at',
        )

    def get_device_name(self, obj):
        return obj.device.name or obj.device.device_id

    def get_latest_runtime(self, obj):
        session = obj.runtime_sessions.all().first()
        if not session:
            return None
        pending_approval = next((
            item for item in reversed(list(session.steps.all()))
            if item.status == 'awaiting_approval' and item.approval_status == 'pending'
        ), None)
        return {
            'id': session.id,
            'status': session.status,
            'status_display': session.get_status_display(),
            'used_steps': session.used_steps,
            'max_steps': session.max_steps,
            'used_duration_seconds': session.used_duration_seconds,
            'max_duration_seconds': session.max_duration_seconds,
            'used_model_tokens': session.used_model_tokens,
            'max_model_tokens': session.max_model_tokens,
            'repeated_state_count': session.repeated_state_count,
            'pending_approval': (
                AppAgentRuntimeStepSerializer(pending_approval, context=self.context).data
                if pending_approval else None
            ),
            'created_at': session.created_at,
            'updated_at': session.updated_at,
        }

    def get_latest_matrix(self, obj):
        matrix = obj.matrix_runs.all().first()
        if not matrix:
            return None
        return {
            'id': matrix.id,
            'name': matrix.name,
            'status': matrix.status,
            'status_display': matrix.get_status_display(),
            'max_concurrency': matrix.max_concurrency,
            'summary': matrix.summary,
            'created_at': matrix.created_at,
            'updated_at': matrix.updated_at,
        }

    def get_execution_reports(self, obj):
        execution_ids = obj.execution_ids or []
        if not execution_ids:
            return {}
        executions = AppTestExecution.objects.filter(
            id__in=execution_ids,
        ).only('id', 'report_path')
        from .report_urls import build_internal_report_path, build_public_report_path

        return {
            str(execution.id): {
                'report_url': build_internal_report_path(execution.id),
                'public_report_url': build_public_report_path(execution.id),
            }
            for execution in executions
            if execution.report_path
        }

    def get_execution_results(self, obj):
        execution_ids = obj.execution_ids or []
        executions = {
            execution.id: execution
            for execution in AppTestExecution.objects.filter(
                id__in=execution_ids,
            ).select_related('test_case', 'device')
        }
        results = []
        for execution_id in execution_ids:
            execution = executions.get(execution_id)
            if not execution:
                fallback = self._execution_result_from_failure_analysis(obj, execution_id)
                if fallback:
                    results.append(fallback)
                continue
            if execution.status in {'pending', 'running'}:
                status = 'RUNNING' if execution.status == 'running' else 'PENDING'
            elif execution.status == 'stopped':
                status = 'SKIPPED'
            elif execution.status == 'completed' and execution.result == 'passed':
                status = 'PASSED'
            elif execution.status == 'completed' and execution.result == 'skipped':
                status = 'SKIPPED'
            else:
                status = 'FAILED'
            case_name = execution.test_case.name if execution.test_case else '未知用例'
            message = execution.error_message or (
                f'{case_name}：{execution.passed_steps}/{execution.total_steps} 步通过'
                if execution.total_steps else f'{case_name} 执行完成'
            )
            results.append({
                'id': execution.id,
                'execution_id': execution.id,
                'module': 'app',
                'status': status,
                'logs': message,
                'duration_ms': int((execution.duration or 0) * 1000),
                'case_id': execution.test_case_id,
                'case_name': case_name,
                'device_name': execution.device.device_id if execution.device else '',
                'report_url': AppTestExecutionSerializer(
                    execution,
                    context=self.context,
                ).data.get('report_url', ''),
            })
        if results:
            return results
        return self._summary_execution_results(obj)

    def _execution_result_from_failure_analysis(self, obj, execution_id):
        analysis = obj.failure_analysis if isinstance(obj.failure_analysis, dict) else {}
        for item in analysis.get('items') or []:
            if not isinstance(item, dict) or item.get('execution_id') != execution_id:
                continue
            case_name = item.get('case_name') or '未知用例'
            actual = item.get('actual') or analysis.get('summary') or '执行记录已清理，保留失败分析摘要'
            return {
                'id': execution_id,
                'execution_id': execution_id,
                'module': 'app',
                'status': 'FAILED',
                'logs': f'{case_name}：{actual}',
                'duration_ms': 0,
                'case_id': item.get('case_id'),
                'case_name': case_name,
                'device_name': '',
                'report_url': item.get('report_url', ''),
                'source': 'failure_analysis',
            }
        return None

    def _summary_execution_results(self, obj):
        summary = obj.result_summary if isinstance(obj.result_summary, dict) else {}
        if not summary:
            return []
        total = int(summary.get('total') or 0)
        failed = int(summary.get('failed') or 0)
        passed = int(summary.get('passed') or 0)
        skipped = int(summary.get('skipped') or summary.get('stopped') or 0)
        if not any([total, failed, passed, skipped]):
            return []
        if failed:
            status = 'FAILED'
        elif skipped and not passed:
            status = 'SKIPPED'
        else:
            status = 'PASSED'
        return [{
            'id': f'agent-summary-{obj.id}',
            'execution_id': '',
            'module': 'app',
            'status': status,
            'logs': f'执行汇总：共 {total} 个用例，{passed} 个通过，{failed} 个失败，{skipped} 个跳过。',
            'duration_ms': 0,
            'case_id': None,
            'case_name': obj.goal[:80],
            'device_name': self.get_device_name(obj),
            'report_url': '',
            'source': 'result_summary',
        }]

    def validate_goal(self, value):
        value = str(value or '').strip()
        if len(value) < 2:
            raise serializers.ValidationError('请描述需要完成的 APP 测试任务')
        return value

    def validate(self, attrs):
        request = self.context.get('request')
        user = request.user if request else None
        project = attrs.get('project')
        device = attrs.get('device') or getattr(self.instance, 'device', None)
        app_package = attrs.get('app_package')

        if user and project and not can_access_app_project(user, project):
            raise serializers.ValidationError({'project': '无权使用该 APP 项目'})
        if not device:
            device_queryset = AppDevice.objects.filter(status__in=['available', 'online'])
            device = (
                device_queryset.filter(
                    connection_type__in=['emulator', 'remote_emulator']
                ).order_by('-updated_at').first()
                or device_queryset.order_by('-updated_at').first()
            )
            if not device:
                raise serializers.ValidationError({
                    'device': '没有可用的在线模拟器或设备，请先启动模拟器'
                })
            if self.instance is None:
                attrs['device'] = device
        if device:
            if device.status == 'offline':
                raise serializers.ValidationError({'device': '所选设备当前离线'})
            if device.status == 'locked' and user and device.locked_by_id != user.id:
                raise serializers.ValidationError({'device': '所选设备已被其他用户锁定'})
        source_entry = attrs.get('source_entry', 'requirement')
        reference_module = str(attrs.get('reference_module') or '')
        case_ids = list(dict.fromkeys(
            attrs.get('app_case_ids') or attrs.get('app_test_case_ids') or []
        ))
        if source_entry == 'reference' and not reference_module:
            if attrs.get('api_test_objects') or attrs.get('test_object_refs'):
                reference_module = 'api'
            elif case_ids or attrs.get('app_test_object_refs'):
                reference_module = 'app'
        raw_api_refs = attrs.get('api_test_objects') or (
            attrs.get('test_object_refs') if reference_module == 'api' else []
        ) or []
        if not isinstance(raw_api_refs, list):
            raise serializers.ValidationError({'api_test_objects': 'API 测试对象引用必须是数组'})
        api_refs = []
        api_object_ids = []
        runnergo_api_ids = []
        for item in raw_api_refs:
            if not isinstance(item, dict):
                continue
            raw_id = item.get('id') or item.get('target_id')
            source = str(item.get('source') or 'api_test_module')
            if source == 'runnergo_api_test_module':
                target_id = str(raw_id or '').strip()
                team_id = str(item.get('team_id') or '').strip()
                name = str(item.get('name') or '').strip()
                method = str(item.get('method') or '').strip().upper()
                if not target_id or not team_id or not name or not method:
                    continue
                if target_id in runnergo_api_ids:
                    continue
                api_refs.append({
                    **item,
                    'id': target_id,
                    'target_id': target_id,
                    'team_id': team_id,
                    'target_type': 'api',
                    'name': name,
                    'method': method,
                    'path': str(item.get('path') or ''),
                    'source': source,
                })
                runnergo_api_ids.append(target_id)
                continue
            try:
                api_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if api_id <= 0 or api_id in api_object_ids:
                continue
            normalized = dict(item)
            normalized.update({
                'id': api_id,
                'target_id': str(api_id),
                'target_type': str(item.get('target_type') or item.get('type') or 'api').lower(),
                'source': item.get('source') or 'api_test_module',
            })
            api_refs.append(normalized)
            api_object_ids.append(api_id)

        raw_object_refs = attrs.get('app_test_object_refs') or (
            attrs.get('test_object_refs') if reference_module == 'app' else []
        ) or []
        if not isinstance(raw_object_refs, list):
            raise serializers.ValidationError({'app_test_object_refs': 'APP 测试对象引用必须是数组'})
        object_ids = []
        for item in raw_object_refs:
            raw_id = item.get('id') if isinstance(item, dict) else item
            try:
                object_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if object_id > 0 and object_id not in object_ids:
                object_ids.append(object_id)

        if source_entry == 'reference':
            if reference_module not in {'api', 'app'}:
                raise serializers.ValidationError({
                    'reference_module': 'APP 自动化只能引用 API 测试对象、APP 测试对象或 APP 测试用例'
                })
            if not case_ids and not object_ids and not api_object_ids and not runnergo_api_ids:
                raise serializers.ValidationError('请选择 API 测试对象或 APP 测试用例')
        elif app_package and project and not project.test_cases.filter(app_package=app_package).exists():
            raise serializers.ValidationError({
                'app_package': '该项目下没有使用此包名的 APP 测试用例'
            })

        if project and case_ids:
            cases = AppTestCase.objects.filter(id__in=case_ids).filter(
                Q(project=project) | Q(project__isnull=True, created_by=user)
            )
            if app_package:
                cases = cases.filter(app_package=app_package)
            case_map = {case.id: case for case in cases}
            invalid_case_ids = [case_id for case_id in case_ids if case_id not in case_map]
            if invalid_case_ids:
                raise serializers.ValidationError({
                    'app_case_ids': f'包含无效或越权的 APP 测试用例: {invalid_case_ids}'
                })
            unusable_case_ids = []
            for case_id in case_ids:
                flow = case_map[case_id].ui_flow
                usable = bool(flow) if isinstance(flow, list) else bool((flow or {}).get('steps'))
                if not usable:
                    unusable_case_ids.append(case_id)
            if unusable_case_ids:
                raise serializers.ValidationError({
                    'app_case_ids': f'APP 测试用例没有可执行步骤: {unusable_case_ids}'
                })

        if project and object_ids:
            valid_object_ids = set(
                AppElement.objects.filter(id__in=object_ids, is_active=True).filter(
                    Q(project=project) | Q(project__isnull=True, created_by=user)
                ).values_list('id', flat=True)
            )
            invalid_object_ids = [object_id for object_id in object_ids if object_id not in valid_object_ids]
            if invalid_object_ids:
                raise serializers.ValidationError({
                    'app_test_object_refs': f'包含无效或越权的 APP 测试对象: {invalid_object_ids}'
                })

        if api_object_ids:
            runnergo_refs = [
                ref for ref in api_refs
                if ref.get('source') == 'runnergo_api_test_module'
            ]
            api_queryset = ApiAsset.objects.filter(id__in=api_object_ids)
            if user:
                api_queryset = api_queryset.filter(
                    Q(project__owner=user) | Q(project__members=user)
                )
            api_map = {api.id: api for api in api_queryset.select_related('project')}
            invalid_api_ids = [api_id for api_id in api_object_ids if api_id not in api_map]
            if invalid_api_ids:
                raise serializers.ValidationError({
                    'api_test_objects': f'包含无效或越权的 API 测试对象: {invalid_api_ids}'
                })
            api_refs = [
                {
                    **ref,
                    'team_id': str(ref.get('team_id') or api_map[ref['id']].project_id),
                    'project_id': api_map[ref['id']].project_id,
                    'name': ref.get('name') or api_map[ref['id']].name,
                    'method': ref.get('method') or api_map[ref['id']].method,
                    'path': ref.get('path') or api_map[ref['id']].path,
                    'service': ref.get('service') or api_map[ref['id']].service,
                }
                for ref in api_refs
                if ref.get('source') != 'runnergo_api_test_module'
            ]
            api_refs.extend(runnergo_refs)

        attrs['_reference_context'] = {
            'source_entry': source_entry,
            'reference_module': reference_module if source_entry == 'reference' else '',
            'app_case_ids': case_ids if source_entry == 'reference' else [],
            'app_object_ids': object_ids if source_entry == 'reference' else [],
            'api_test_objects': api_refs if source_entry == 'reference' else [],
            'test_object_refs': api_refs if source_entry == 'reference' else [],
        }
        return attrs

    def create(self, validated_data):
        reference_context = validated_data.pop('_reference_context', {})
        test_data_count = validated_data.pop('test_data_count', 3)
        for field in (
            'app_case_ids', 'app_test_case_ids', 'app_test_object_refs',
            'api_test_objects', 'test_object_refs', 'source_entry', 'reference_module',
        ):
            validated_data.pop(field, None)
        validated_data['plan'] = {
            'reference_context': reference_context,
            'test_data_count': test_data_count,
        }
        return super().create(validated_data)


class AppComponentSerializer(serializers.ModelSerializer):
    """UI组件定义序列化器"""
    
    class Meta:
        model = AppComponent
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')


class AppCustomComponentSerializer(serializers.ModelSerializer):
    """自定义组件定义序列化器"""
    
    class Meta:
        model = AppCustomComponent
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')


class AppComponentPackageSerializer(serializers.ModelSerializer):
    """组件包序列化器"""
    created_by_name = serializers.SerializerMethodField()
    
    class Meta:
        model = AppComponentPackage
        fields = '__all__'
        read_only_fields = ('created_at', 'updated_at')
    
    def get_created_by_name(self, obj):
        return obj.created_by.username if obj.created_by else None


# ========== 测试套件序列化器 ==========

class AppTestSuiteCaseSerializer(serializers.ModelSerializer):
    """套件-用例关联序列化器"""
    test_case = serializers.SerializerMethodField()
    test_case_id = serializers.IntegerField(write_only=True, required=False)

    class Meta:
        model = AppTestSuiteCase
        fields = ('id', 'test_case', 'test_case_id', 'order')

    def get_test_case(self, obj):
        tc = obj.test_case
        return {
            'id': tc.id,
            'name': tc.name,
            'description': tc.description,
            'app_package_name': tc.app_package.name if tc.app_package else '',
            'updated_at': tc.updated_at,
        }


class AppTestSuiteSerializer(serializers.ModelSerializer):
    """测试套件列表序列化器"""
    created_by_name = serializers.SerializerMethodField()
    test_case_count = serializers.SerializerMethodField()
    suite_cases = AppTestSuiteCaseSerializer(many=True, read_only=True)
    execution_status_display = serializers.CharField(
        source='get_execution_status_display', read_only=True
    )
    execution_result_display = serializers.CharField(
        source='get_execution_result_display', read_only=True, default=None
    )

    class Meta:
        model = AppTestSuite
        fields = (
            'id', 'name', 'description', 'project',
            'execution_status', 'execution_status_display',
            'execution_result', 'execution_result_display',
            'passed_count', 'failed_count', 'last_run_at',
            'test_case_count', 'suite_cases',
            'created_by', 'created_by_name',
            'created_at', 'updated_at',
        )
        read_only_fields = (
            'created_at', 'updated_at',
            'execution_status', 'execution_result',
            'passed_count', 'failed_count', 'last_run_at',
        )

    def get_created_by_name(self, obj):
        return obj.created_by.username if obj.created_by else None

    def get_test_case_count(self, obj):
        return obj.suite_cases.count()


class AppTestSuiteCreateSerializer(serializers.ModelSerializer):
    """测试套件创建序列化器"""
    test_case_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        write_only=True,
        default=[]
    )

    class Meta:
        model = AppTestSuite
        fields = ('id', 'name', 'description', 'project', 'test_case_ids')
        read_only_fields = ('id',)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        request = self.context.get('request')
        project = attrs.get('project')
        if project and request and not can_access_app_project(request.user, project):
            raise serializers.ValidationError({'project': '无权使用该 APP 项目'})
        test_case_ids = attrs.get('test_case_ids') or []
        if test_case_ids:
            cases = AppTestCase.objects.filter(id__in=test_case_ids)
            if cases.count() != len(set(test_case_ids)):
                raise serializers.ValidationError({'test_case_ids': '包含不存在的测试用例'})
            foreign = cases.exclude(project=project).values_list('id', flat=True)
            if foreign:
                raise serializers.ValidationError({
                    'test_case_ids': f'测试套件不能引用其他项目的用例: {sorted(foreign)}'
                })
        return attrs

    def create(self, validated_data):
        test_case_ids = validated_data.pop('test_case_ids', [])
        suite = AppTestSuite.objects.create(**validated_data)
        for idx, tc_id in enumerate(test_case_ids):
            AppTestSuiteCase.objects.create(
                test_suite=suite,
                test_case_id=tc_id,
                order=idx
            )
        return suite


class AppTestSuiteUpdateSerializer(serializers.ModelSerializer):
    """测试套件更新序列化器"""
    class Meta:
        model = AppTestSuite
        fields = ('name', 'description', 'project')

    def validate_project(self, project):
        request = self.context.get('request')
        if request and not can_access_app_project(request.user, project):
            raise serializers.ValidationError('无权使用该 APP 项目')
        if self.instance and self.instance.test_cases.exclude(project=project).exists():
            raise serializers.ValidationError('套件内存在其他项目的用例，不能直接变更项目')
        return project


# ========== 定时任务序列化器 ==========

class AppScheduledTaskSerializer(serializers.ModelSerializer):
    """APP定时任务序列化器"""
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    device_name = serializers.SerializerMethodField()
    app_package_name = serializers.CharField(source='app_package.name', read_only=True, default='')
    test_suite_name = serializers.CharField(source='test_suite.name', read_only=True, default='')
    test_case_name = serializers.CharField(source='test_case.name', read_only=True, default='')
    task_type_display = serializers.CharField(source='get_task_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    trigger_type_display = serializers.CharField(source='get_trigger_type_display', read_only=True)

    class Meta:
        model = AppScheduledTask
        fields = [
            'id', 'name', 'description', 'project',
            'task_type', 'task_type_display',
            'trigger_type', 'trigger_type_display',
            'cron_expression', 'interval_seconds', 'execute_at',
            'device', 'device_name',
            'app_package', 'app_package_name',
            'test_suite', 'test_suite_name',
            'test_case', 'test_case_name',
            'status', 'status_display',
            'last_run_time', 'next_run_time',
            'total_runs', 'successful_runs', 'failed_runs',
            'last_result', 'error_message',
            'created_by', 'created_by_name',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'created_by', 'last_run_time', 'next_run_time',
            'total_runs', 'successful_runs', 'failed_runs',
            'last_result', 'error_message',
            'created_at', 'updated_at',
        ]

    def get_device_name(self, obj):
        if obj.device:
            return obj.device.name or obj.device.device_id
        return ''

    def validate(self, attrs):
        attrs = super().validate(attrs)
        request = self.context.get('request')
        user = request.user if request else None
        project = attrs.get('project', getattr(self.instance, 'project', None))
        if project and user and not can_access_app_project(user, project):
            raise serializers.ValidationError({'project': '无权使用该 APP 项目'})

        trigger_type = attrs.get('trigger_type')
        if trigger_type == 'CRON' and not attrs.get('cron_expression'):
            raise serializers.ValidationError('Cron表达式不能为空')
        if trigger_type == 'INTERVAL':
            if not attrs.get('interval_seconds'):
                raise serializers.ValidationError('间隔秒数不能为空')
            if attrs['interval_seconds'] < 60:
                raise serializers.ValidationError('间隔秒数不能小于60秒')
        if trigger_type == 'ONCE':
            if not attrs.get('execute_at'):
                raise serializers.ValidationError('执行时间不能为空')
            if attrs['execute_at'] <= timezone.now():
                raise serializers.ValidationError('执行时间必须大于当前时间')

        task_type = attrs.get('task_type')
        if task_type == 'TEST_SUITE' and not attrs.get('test_suite'):
            raise serializers.ValidationError('请选择测试套件')
        if task_type == 'TEST_CASE' and not attrs.get('test_case'):
            raise serializers.ValidationError('请选择测试用例')

        test_suite = attrs.get('test_suite', getattr(self.instance, 'test_suite', None))
        test_case = attrs.get('test_case', getattr(self.instance, 'test_case', None))
        if test_suite and test_suite.project_id != getattr(project, 'id', None):
            raise serializers.ValidationError({'test_suite': '测试套件不属于所选项目'})
        if test_case and test_case.project_id != getattr(project, 'id', None):
            raise serializers.ValidationError({'test_case': '测试用例不属于所选项目'})

        return attrs

    def create(self, validated_data):
        validated_data['created_by'] = self.context['request'].user
        instance = super().create(validated_data)
        instance.next_run_time = instance.calculate_next_run()
        instance.save(update_fields=['next_run_time'])
        return instance

    def update(self, instance, validated_data):
        instance = super().update(instance, validated_data)
        instance.next_run_time = instance.calculate_next_run()
        instance.save(update_fields=['next_run_time'])
        return instance


class AppNotificationLogSerializer(serializers.ModelSerializer):
    """APP通知日志序列化器"""
    recipient_names = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    notification_type_display = serializers.CharField(source='get_notification_type_display', read_only=True)
    retry_status = serializers.SerializerMethodField()
    task_type_display = serializers.SerializerMethodField()
    actual_notification_type_display = serializers.SerializerMethodField()

    class Meta:
        model = AppNotificationLog
        fields = [
            'id', 'task', 'task_name',
            'notification_type', 'notification_type_display',
            'actual_notification_type_display', 'task_type_display',
            'sender_name', 'sender_email',
            'recipient_names', 'webhook_bot_info', 'notification_content',
            'status', 'status_display', 'error_message', 'response_info',
            'created_at', 'sent_at', 'retry_count', 'retry_status',
        ]
        read_only_fields = ['created_at', 'sent_at']

    def get_recipient_names(self, obj):
        return obj.get_recipient_names()

    def get_retry_status(self, obj):
        return obj.get_retry_status()

    def get_task_type_display(self, obj):
        if obj.task_type:
            choices = dict(AppScheduledTask.TASK_TYPE_CHOICES)
            return choices.get(obj.task_type, obj.task_type)
        return '未记录'

    def get_actual_notification_type_display(self, obj):
        if obj.webhook_bot_info:
            bot_type = obj.webhook_bot_info.get('type', '') or obj.webhook_bot_info.get('bot_type', '')
            type_map = {'wechat': '企微机器人', 'feishu': '飞书机器人', 'dingtalk': '钉钉机器人'}
            return type_map.get(bot_type, 'Webhook机器人')
        if obj.recipient_info and isinstance(obj.recipient_info, list) and len(obj.recipient_info) > 0:
            return '邮箱通知'
        return '-'
