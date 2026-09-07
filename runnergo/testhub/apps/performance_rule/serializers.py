from rest_framework import serializers

from apps.projects.models import Project

from .models import PerformanceResult, PerformanceRule
from .services import generate_performance_result


class PerformanceRuleSerializer(serializers.ModelSerializer):
    project_id = serializers.PrimaryKeyRelatedField(
        queryset=Project.objects.all(),
        source='project',
        write_only=True
    )
    project_name = serializers.CharField(source='project.name', read_only=True)

    class Meta:
        model = PerformanceRule
        fields = [
            'id', 'project', 'project_id', 'project_name',
            'max_p95', 'max_error_rate', 'min_tps', 'max_cpu',
            'max_db_connection_usage', 'max_db_slow_queries_per_sec',
            'create_time',
        ]
        read_only_fields = ['id', 'project', 'project_name', 'create_time']


class PerformanceMetricInputSerializer(serializers.Serializer):
    project_id = serializers.IntegerField()
    report_id = serializers.CharField(required=False, allow_blank=True, default='')
    tps = serializers.FloatField(required=False, allow_null=True)
    p95 = serializers.FloatField(required=False, allow_null=True)
    error_rate = serializers.FloatField(required=False, allow_null=True)
    cpu = serializers.FloatField(required=False, allow_null=True)
    db_connection_usage = serializers.FloatField(required=False, allow_null=True)
    db_qps = serializers.FloatField(required=False, allow_null=True)
    db_slow_queries_per_sec = serializers.FloatField(required=False, allow_null=True)
    db_row_lock_waits_per_sec = serializers.FloatField(required=False, allow_null=True)
    metrics = serializers.DictField(required=False)

    def validate(self, attrs):
        project_id = attrs['project_id']
        if not Project.objects.filter(id=project_id).exists():
            raise serializers.ValidationError({'project_id': '项目不存在'})

        inline_metrics = {
            key: attrs.get(key)
            for key in (
                'tps', 'p95', 'error_rate', 'cpu',
                'db_connection_usage', 'db_qps',
                'db_slow_queries_per_sec', 'db_row_lock_waits_per_sec',
            )
            if key in attrs
        }
        metrics = attrs.get('metrics') or inline_metrics
        if not metrics:
            raise serializers.ValidationError('请提供性能指标')

        attrs['metrics'] = {**metrics, **inline_metrics}
        return attrs

    def create(self, validated_data):
        return generate_performance_result(
            project_id=validated_data['project_id'],
            report_id=validated_data.get('report_id', ''),
            metrics=validated_data['metrics'],
        )


class PerformanceResultSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    rule_id = serializers.IntegerField(source='rule.id', read_only=True, allow_null=True)

    class Meta:
        model = PerformanceResult
        fields = [
            'id', 'project', 'project_name', 'rule_id', 'report_id',
            'tps', 'p95', 'error_rate', 'cpu',
            'db_connection_usage', 'db_qps',
            'db_slow_queries_per_sec', 'db_row_lock_waits_per_sec',
            'overall_status', 'details', 'create_time',
        ]
        read_only_fields = fields


class ExporterTargetInputSerializer(serializers.Serializer):
    app = serializers.CharField(max_length=100)
    app_url = serializers.URLField(max_length=500)
    database_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    process_target = serializers.RegexField(
        regex=r'^[A-Za-z0-9._-]+:[0-9]+$',
        required=False,
        allow_blank=True,
        error_messages={'invalid': 'process_target 必须是 host:port 格式'},
    )
    jmx_target = serializers.RegexField(
        regex=r'^[A-Za-z0-9._-]+:[0-9]+$',
        required=False,
        allow_blank=True,
        error_messages={'invalid': 'jmx_target 必须是 host:port 格式'},
    )
    mysql_target = serializers.RegexField(
        regex=r'^[A-Za-z0-9._-]+:[0-9]+$',
        required=False,
        allow_blank=True,
        error_messages={'invalid': 'mysql_target 必须是 MySQL Exporter 的 host:port 格式'},
    )
    reload = serializers.BooleanField(required=False, default=True)

    def validate(self, attrs):
        if not attrs.get('process_target') and not attrs.get('jmx_target') and not attrs.get('mysql_target'):
            raise serializers.ValidationError('process_target、jmx_target 和 mysql_target 至少填写一个')
        return attrs


class ExporterTargetDeleteSerializer(serializers.Serializer):
    target_type = serializers.ChoiceField(choices=['process', 'jmx', 'mysql'])
    target = serializers.RegexField(
        regex=r'^[A-Za-z0-9._-]+:[0-9]+$',
        error_messages={'invalid': 'target 必须是 host:port 格式'},
    )


class TestedDatabaseConfigSerializer(serializers.Serializer):
    host = serializers.RegexField(
        regex=r'^[A-Za-z0-9._-]+$',
        error_messages={'invalid': 'host 必须是 IP、域名或容器名'},
    )
    port = serializers.IntegerField(min_value=1, max_value=65535, default=3306)
    user = serializers.CharField(max_length=100)
    password = serializers.CharField(
        max_length=200,
        required=False,
        allow_blank=True,
        trim_whitespace=False,
    )
    database = serializers.RegexField(
        regex=r'^[A-Za-z0-9_$.-]+$',
        error_messages={'invalid': 'database 只能包含字母、数字、下划线、点、横线或 $'},
    )
