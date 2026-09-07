"""
Core 应用序列化器
"""
from rest_framework import serializers
from .models import (
    RuntimeDataSet,
    RuntimeDataTemplate,
    RuntimeExecutionSnapshot,
    BulkTestDataJob,
    TestDataAsset,
    TestDataDecisionLog,
    TestDataAssetLease,
    TestDataAssetRequirement,
)


class RuntimeDataTemplateSerializer(serializers.ModelSerializer):
    """运行态数据模板序列化器。"""

    created_by_name = serializers.CharField(source='created_by.username', read_only=True, default='')

    class Meta:
        model = RuntimeDataTemplate
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at', 'created_by', 'created_by_name']


class RuntimeDataSetSerializer(serializers.ModelSerializer):
    """运行态批量数据集序列化器。"""

    created_by_name = serializers.CharField(source='created_by.username', read_only=True, default='')

    class Meta:
        model = RuntimeDataSet
        fields = '__all__'
        read_only_fields = [
            'columns', 'rows', 'row_count', 'source_filename',
            'created_at', 'updated_at', 'created_by', 'created_by_name',
        ]


class RuntimeExecutionSnapshotSerializer(serializers.ModelSerializer):
    """运行态执行数据快照序列化器。"""

    created_by_name = serializers.CharField(source='created_by.username', read_only=True, default='')
    template_name = serializers.CharField(source='template.name', read_only=True, default='')
    dataset_name = serializers.CharField(source='dataset.name', read_only=True, default='')

    class Meta:
        model = RuntimeExecutionSnapshot
        fields = '__all__'
        read_only_fields = ['created_at', 'created_by', 'created_by_name', 'template_name', 'dataset_name']


class TestDataAssetSerializer(serializers.ModelSerializer):
    """测试数据资产序列化器。"""

    created_by_name = serializers.CharField(source='created_by.username', read_only=True, default='')
    locked_by_name = serializers.CharField(source='locked_by.username', read_only=True, default='')
    bindings = serializers.SerializerMethodField(read_only=True)
    binding_count = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = TestDataAsset
        fields = '__all__'
        read_only_fields = [
            'created_at', 'updated_at', 'created_by', 'created_by_name',
            'locked_by', 'locked_by_name', 'locked_at', 'used_at',
            'bulk_job', 'bulk_row_cursor',
        ]

    def validate_payload(self, value):
        if isinstance(value, dict):
            return value
        if not isinstance(value, list):
            raise serializers.ValidationError('资产数据必须是 JSON 对象或对象数组')
        if not value:
            raise serializers.ValidationError('参数化数据至少需要一行')
        if len(value) > 100:
            raise serializers.ValidationError('参数化数据最多支持 100 行')
        if any(not isinstance(row, dict) for row in value):
            raise serializers.ValidationError('参数化数据的每一行都必须是 JSON 对象')
        return value

    def get_bindings(self, obj):
        return TestDataAssetRequirementSerializer(
            obj.requirements.all().order_by('id'),
            many=True,
        ).data

    def get_binding_count(self, obj):
        return obj.requirements.count()


class TestDataAssetRequirementSerializer(serializers.ModelSerializer):
    """用例数据需求序列化器。"""

    created_by_name = serializers.CharField(source='created_by.username', read_only=True, default='')

    def validate(self, attrs):
        source_asset = attrs.get('source_asset')
        if source_asset is None and self.instance is not None:
            source_asset = self.instance.source_asset
        release_policy = attrs.get('release_policy')
        if release_policy is None and self.instance is not None:
            release_policy = self.instance.release_policy
        if source_asset and source_asset.bulk_job_id and release_policy != 'release':
            raise serializers.ValidationError({
                'release_policy': '大批量数据资产必须保持可用，才能按顺序引用全部数据。',
            })
        return attrs

    class Meta:
        model = TestDataAssetRequirement
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at', 'created_by', 'created_by_name']


class TestDataAssetLeaseSerializer(serializers.ModelSerializer):
    """测试数据租约序列化器。"""

    asset_name = serializers.CharField(source='asset.name', read_only=True, default='')
    asset_type = serializers.CharField(source='asset.asset_type', read_only=True, default='')
    requested_by_name = serializers.CharField(source='requested_by.username', read_only=True, default='')

    class Meta:
        model = TestDataAssetLease
        fields = '__all__'
        read_only_fields = [
            'created_at', 'updated_at', 'requested_by', 'requested_by_name',
            'asset_name', 'asset_type', 'locked_at', 'released_at',
        ]


class TestDataDecisionLogSerializer(serializers.ModelSerializer):
    """V6 测试数据决策日志序列化器。"""

    created_by_name = serializers.CharField(source='created_by.username', read_only=True, default='')
    asset_name = serializers.CharField(source='asset.name', read_only=True, default='')

    class Meta:
        model = TestDataDecisionLog
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at', 'created_by', 'created_by_name', 'asset_name']


class BulkTestDataJobSerializer(serializers.ModelSerializer):
    """大批量生成任务序列化器。"""

    created_by_name = serializers.CharField(source='created_by.username', read_only=True, default='')
    output_url = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = BulkTestDataJob
        fields = '__all__'
        read_only_fields = [
            'generated_count', 'progress', 'status', 'output_file', 'output_size',
            'checksum_sha256', 'task_id', 'cancel_requested', 'error_message',
            'started_at', 'completed_at', 'created_by', 'created_by_name',
            'created_at', 'updated_at', 'output_url', 'data_asset',
        ]

    def get_output_url(self, obj):
        if not obj.output_file:
            return ''
        request = self.context.get('request')
        url = f'/api/core/bulk-test-data-jobs/{obj.id}/download/'
        return request.build_absolute_uri(url) if request else url
