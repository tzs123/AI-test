import re

from rest_framework import serializers

from .models import (
    AgentExecutionLog,
    AgentKnowledgeChunk,
    AgentKnowledgeDocument,
    AgentMemory,
    AgentStep,
    AgentTask,
    TestAsset,
    TestExecutionResult,
)
from backend.agent.needs_input import derive_task_needs_input, needs_input_message


class AgentStepSerializer(serializers.ModelSerializer):
    type = serializers.CharField(source='step_type', read_only=True)

    class Meta:
        model = AgentStep
        fields = [
            'id', 'step', 'type', 'step_type', 'action', 'tool_name', 'status',
            'input_payload', 'output_payload', 'error_message', 'started_at',
            'finished_at', 'created_at', 'updated_at',
        ]


class AgentMemorySerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentMemory
        fields = ['id', 'task', 'project', 'step', 'memory_type', 'role', 'content', 'payload', 'created_at']


class AgentMemoryCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentMemory
        fields = ['task', 'project', 'memory_type', 'role', 'content', 'payload']


class AgentKnowledgeChunkSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentKnowledgeChunk
        fields = ['id', 'document', 'chunk_index', 'content', 'tokens', 'metadata', 'created_at']


class AgentKnowledgeDocumentSerializer(serializers.ModelSerializer):
    chunks = AgentKnowledgeChunkSerializer(many=True, read_only=True)

    class Meta:
        model = AgentKnowledgeDocument
        fields = [
            'id', 'title', 'document_type', 'project', 'content', 'metadata',
            'embedding_model', 'status', 'chunks_count', 'uploaded_by',
            'created_at', 'updated_at', 'chunks',
        ]
        read_only_fields = ['uploaded_by', 'embedding_model', 'status', 'chunks_count', 'created_at', 'updated_at']


class AgentExecutionLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentExecutionLog
        fields = [
            'id', 'step', 'trace_type', 'action', 'tool', 'params', 'result',
            'status', 'created_at',
        ]


class TestAssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestAsset
        fields = [
            'id', 'asset_id', 'asset_type', 'name', 'task', 'project',
            'source_model', 'source_id', 'metadata', 'created_at', 'updated_at',
        ]


class TestExecutionResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestExecutionResult
        fields = [
            'id', 'task', 'step', 'module', 'status', 'asset', 'request_payload',
            'response_payload', 'logs', 'screenshot', 'report_url', 'duration_ms',
            'started_at', 'finished_at', 'created_at', 'updated_at',
        ]


class AgentTaskSerializer(serializers.ModelSerializer):
    current_step = serializers.SerializerMethodField()
    needs_input = serializers.SerializerMethodField()
    next_action = serializers.SerializerMethodField()
    business_steps = serializers.SerializerMethodField()
    acceptance_criteria = serializers.SerializerMethodField()
    steps = AgentStepSerializer(many=True, read_only=True)
    memories = AgentMemorySerializer(many=True, read_only=True)
    execution_logs = AgentExecutionLogSerializer(many=True, read_only=True)
    assets = TestAssetSerializer(many=True, read_only=True)
    execution_results = serializers.SerializerMethodField()
    project_name = serializers.CharField(source='project.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)

    def get_current_step(self, obj):
        if obj.status == AgentTask.STATUS_NEEDS_INPUT:
            return needs_input_message(derive_task_needs_input(obj))
        current_step = str(obj.current_step or '')
        legacy_match = re.fullmatch(
            r'已生成报告，(\d+) 个真实执行步骤待绑定资产',
            current_step,
        )
        if legacy_match:
            return f'已生成测试报告，{legacy_match.group(1)} 个步骤未执行，原因见执行明细'
        return current_step

    def get_execution_results(self, obj):
        results = obj.execution_results.all()
        if obj.started_at:
            results = results.filter(created_at__gte=obj.started_at)
        return TestExecutionResultSerializer(results, many=True).data

    def get_needs_input(self, obj):
        if obj.status != AgentTask.STATUS_NEEDS_INPUT:
            return []
        return derive_task_needs_input(obj)

    def get_next_action(self, obj):
        keys = self.get_needs_input(obj)
        return needs_input_message(keys) if keys else ''

    def get_business_steps(self, obj):
        return self._supplement_value(obj, 'business_steps', '可执行业务步骤')

    def get_acceptance_criteria(self, obj):
        return self._supplement_value(obj, 'acceptance_criteria', '验收标准')

    @staticmethod
    def _supplement_value(obj, context_key, label):
        context_value = str((obj.context or {}).get(context_key) or '').strip()
        if context_value:
            return context_value
        requirement = str(obj.user_requirement or '')
        marker = '--- 用户补充的执行条件 ---'
        if marker not in requirement:
            return ''
        supplemented = requirement.split(marker, 1)[1]
        match = re.search(
            rf'(?:^|\n){re.escape(label)}：\s*\n(.*?)(?=\n(?:目标网址|可执行业务步骤|验收标准)：|\Z)',
            supplemented,
            re.DOTALL,
        )
        return match.group(1).strip() if match else ''

    class Meta:
        model = AgentTask
        fields = [
            'id', 'task_name', 'user_requirement', 'status', 'progress',
            'current_step',
            'needs_input', 'next_action', 'business_steps', 'acceptance_criteria',
            'project', 'project_name', 'created_by', 'created_by_name', 'plan',
            'test_plan', 'context', 'failure_analysis', 'error_message',
            'started_at', 'finished_at', 'created_at', 'updated_at',
            'steps', 'memories', 'execution_logs', 'assets', 'execution_results',
        ]
        read_only_fields = [
            'id', 'created_by', 'status', 'progress', 'current_step', 'plan', 'test_plan',
            'context', 'failure_analysis', 'error_message', 'started_at',
            'finished_at', 'created_at', 'updated_at',
        ]


class AgentTaskCreateSerializer(serializers.ModelSerializer):
    auto_execute = serializers.BooleanField(default=True, write_only=True)

    class Meta:
        model = AgentTask
        fields = ['task_name', 'user_requirement', 'project', 'auto_execute']

    def validate_project(self, value):
        if not value:
            return value
        user = self.context['request'].user
        if value.owner_id == user.id or value.members.filter(id=user.id).exists():
            return value
        raise serializers.ValidationError('项目不存在或无权限访问')


class AgentFullCycleRequestSerializer(serializers.Serializer):
    agent_service = serializers.ChoiceField(
        choices=('ui',),
        required=False,
        default='ui',
    )
    task_name = serializers.CharField(required=False, allow_blank=True, max_length=200)
    requirement = serializers.CharField(required=True, allow_blank=False, max_length=4000)
    project = serializers.IntegerField(required=False, allow_null=True)
    frontend_url = serializers.CharField(required=False, allow_blank=True, default='')
    api_url = serializers.CharField(required=False, allow_blank=True, default='')
    api_doc = serializers.JSONField(required=False, default=dict)
    api_test_objects = serializers.JSONField(required=False, default=list)
    test_object_refs = serializers.JSONField(required=False, default=list)
    ui_case_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        default=list,
    )
    ui_test_case_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        default=list,
    )
    ui_case_names = serializers.ListField(
        child=serializers.CharField(allow_blank=False),
        required=False,
        default=list,
    )
    ui_test_case_names = serializers.ListField(
        child=serializers.CharField(allow_blank=False),
        required=False,
        default=list,
    )
    ui_case_files = serializers.ListField(
        child=serializers.CharField(allow_blank=False),
        required=False,
        default=list,
    )
    case_files = serializers.ListField(
        child=serializers.CharField(allow_blank=False),
        required=False,
        default=list,
    )
    ui_project_id = serializers.CharField(required=False, allow_blank=True, default='')
    app_project = serializers.IntegerField(required=False, allow_null=True)
    app_project_id = serializers.IntegerField(required=False, allow_null=True)
    device = serializers.CharField(required=False, allow_blank=True, default='')
    device_id = serializers.CharField(required=False, allow_blank=True, default='')
    app_package = serializers.IntegerField(required=False, allow_null=True)
    app_package_id = serializers.IntegerField(required=False, allow_null=True)
    app_test_object_refs = serializers.JSONField(required=False, default=list)
    source_entry = serializers.CharField(required=False, allow_blank=True, default='')
    reference_module = serializers.CharField(required=False, allow_blank=True, default='')
    app_case_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        default=list,
    )
    app_test_case_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        default=list,
    )
    test_data_count = serializers.IntegerField(required=False, default=3, min_value=1, max_value=20)
    auto_execute = serializers.BooleanField(required=False, default=True)


class AgentTaskSupplementSerializer(serializers.Serializer):
    target_url = serializers.URLField(max_length=2048, required=False, allow_blank=True)
    business_steps = serializers.CharField(
        max_length=10000,
        required=False,
        allow_blank=True,
        trim_whitespace=True,
    )
    acceptance_criteria = serializers.CharField(
        max_length=5000,
        required=False,
        allow_blank=True,
        trim_whitespace=True,
    )
    auto_execute = serializers.BooleanField(required=False, default=True)


class AgentTestCaseSaveSerializer(serializers.Serializer):
    name = serializers.CharField(required=False, allow_blank=True, max_length=200, default='')
    description = serializers.CharField(required=False, allow_blank=True, max_length=5000, default='')


class AgentPlanRequestSerializer(serializers.Serializer):
    requirement = serializers.CharField(required=True, allow_blank=False, max_length=4000)


class FailureAnalyzeRequestSerializer(serializers.Serializer):
    logs = serializers.CharField(required=False, allow_blank=True, default='')
    screenshot = serializers.CharField(required=False, allow_blank=True, default='')
    response = serializers.JSONField(required=False, default=dict)


class AgentExploreRequestSerializer(serializers.Serializer):
    url = serializers.URLField(required=True)
    goal = serializers.CharField(required=False, allow_blank=True, default='建立系统地图')
    inputs = serializers.JSONField(required=False, default=dict)
    project = serializers.IntegerField(required=False)
    save_memory = serializers.BooleanField(required=False, default=True)


class AgentKnowledgeUploadSerializer(serializers.Serializer):
    title = serializers.CharField(required=True, max_length=200)
    document_type = serializers.ChoiceField(
        choices=['prd', 'api', 'database', 'requirement', 'bug', 'other'],
        required=False,
        default='other',
    )
    project = serializers.IntegerField(required=False)
    content = serializers.CharField(required=True, allow_blank=False)
    metadata = serializers.JSONField(required=False, default=dict)


class AgentRagSearchSerializer(serializers.Serializer):
    query = serializers.CharField(required=True, allow_blank=False, max_length=1000)
    project = serializers.IntegerField(required=False)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=20, default=5)


class AgentAssetGenerateSerializer(serializers.Serializer):
    requirement = serializers.CharField(required=True, allow_blank=False, max_length=4000)
    project = serializers.IntegerField(required=False)
    base_url = serializers.CharField(required=False, allow_blank=True, default='')
    api_doc = serializers.JSONField(required=False, default=dict)
    schedule = serializers.CharField(required=False, allow_blank=True, default='daily')
    save_assets = serializers.BooleanField(required=False, default=True)


class AgentSelfHealSerializer(serializers.Serializer):
    logs = serializers.CharField(required=False, allow_blank=True, default='')
    screenshot = serializers.CharField(required=False, allow_blank=True, default='')
    response = serializers.JSONField(required=False, default=dict)
    old_locator = serializers.CharField(required=False, allow_blank=True, default='')
    new_dom = serializers.CharField(required=False, allow_blank=True, default='')
    apply = serializers.BooleanField(required=False, default=False)


class AgentQualityScoreSerializer(serializers.Serializer):
    project = serializers.IntegerField(required=False)
    project_name = serializers.CharField(required=False, allow_blank=True, default='')
