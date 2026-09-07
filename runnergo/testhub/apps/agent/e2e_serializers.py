from __future__ import annotations

from rest_framework import serializers

from .models import E2EExecutionLog, E2ETask


class E2EExecutionLogSerializer(serializers.ModelSerializer):
    screenshot = serializers.SerializerMethodField()
    video = serializers.SerializerMethodField()

    def get_screenshot(self, obj):
        if not obj.screenshot:
            return ''
        return f'/api/e2e/evidence/{obj.task_id}/{obj.id}/screenshot'

    def get_video(self, obj):
        if not obj.video:
            return ''
        return f'/api/e2e/evidence/{obj.task_id}/{obj.id}/video'

    class Meta:
        model = E2EExecutionLog
        fields = [
            'id', 'attempt', 'step', 'step_name', 'action', 'status',
            'screenshot', 'video', 'console_log', 'network_log', 'device_log',
            'input_payload', 'output_payload', 'error', 'duration_ms',
            'created_time', 'updated_time',
        ]


class E2ETaskSerializer(serializers.ModelSerializer):
    execution_logs = E2EExecutionLogSerializer(many=True, read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    requires_replan = serializers.SerializerMethodField()

    def get_requires_replan(self, obj):
        from .e2e_service import e2e_plan_requires_refresh

        return e2e_plan_requires_refresh(obj)

    class Meta:
        model = E2ETask
        fields = [
            'id', 'project', 'project_name', 'agent_task', 'task_name',
            'description', 'scenario', 'steps', 'test_data', 'expected_result',
            'executor_type', 'target_url', 'mobile_config', 'status', 'result',
            'analysis', 'report_url', 'error_message', 'trace_id',
            'execution_attempt', 'started_at', 'finished_at', 'created_time',
            'updated_time', 'created_by_name', 'execution_logs', 'requires_replan',
        ]


class E2ETaskListSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    requires_replan = serializers.SerializerMethodField()

    def get_requires_replan(self, obj):
        from .e2e_service import e2e_plan_requires_refresh

        return e2e_plan_requires_refresh(obj)

    class Meta:
        model = E2ETask
        fields = [
            'id', 'project', 'project_name', 'task_name', 'description',
            'executor_type', 'target_url', 'status', 'trace_id',
            'execution_attempt', 'report_url', 'created_time', 'updated_time',
            'requires_replan',
        ]


class E2ECreateSerializer(serializers.Serializer):
    description = serializers.CharField(max_length=10000, trim_whitespace=True)
    task_name = serializers.CharField(max_length=200, required=False, allow_blank=True, trim_whitespace=True)
    project_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    executor_type = serializers.ChoiceField(choices=['web', 'android', 'ios'], default='web')
    target_url = serializers.URLField(max_length=2048, required=False, allow_blank=True)
    mobile_config = serializers.JSONField(required=False, default=dict)


class E2EReplanSerializer(serializers.Serializer):
    description = serializers.CharField(max_length=20000, required=False, trim_whitespace=True)
    target_url = serializers.URLField(max_length=2048, required=False, allow_blank=True)
    mobile_config = serializers.JSONField(required=False)
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

    def validate_description(self, value):
        if not value.strip():
            raise serializers.ValidationError('description 不能为空')
        return value.strip()

    def validate_mobile_config(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('mobile_config 必须是对象')
        return value


class E2ERunSerializer(serializers.Serializer):
    force = serializers.BooleanField(default=False, required=False)
    headless = serializers.BooleanField(default=True, required=False)
    record_video = serializers.BooleanField(default=True, required=False)


class E2EAnalyzeSerializer(serializers.Serializer):
    force = serializers.BooleanField(default=True, required=False)
