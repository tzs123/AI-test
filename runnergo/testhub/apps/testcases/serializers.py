from django.db import IntegrityError, transaction
from rest_framework import serializers
from .models import TestCase, TestCaseStep, TestCaseAttachment, TestCaseComment, TestCaseImportRecord
from apps.users.serializers import UserSerializer
from apps.versions.serializers import VersionSimpleSerializer
from .services import TestCaseDeduplicationService

class TestCaseStepSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestCaseStep
        fields = '__all__'

class TestCaseAttachmentSerializer(serializers.ModelSerializer):
    uploaded_by = UserSerializer(read_only=True)
    
    class Meta:
        model = TestCaseAttachment
        fields = '__all__'

class TestCaseCommentSerializer(serializers.ModelSerializer):
    author = UserSerializer(read_only=True)
    
    class Meta:
        model = TestCaseComment
        fields = '__all__'

class ProjectSimpleSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()

class TestCaseSerializer(serializers.ModelSerializer):
    author = UserSerializer(read_only=True)
    assignee = UserSerializer(read_only=True)
    project = ProjectSimpleSerializer(read_only=True)
    versions = VersionSimpleSerializer(many=True, read_only=True)
    step_details = TestCaseStepSerializer(many=True, read_only=True)
    attachments = TestCaseAttachmentSerializer(many=True, read_only=True)
    comments = TestCaseCommentSerializer(many=True, read_only=True)
    
    class Meta:
        model = TestCase
        exclude = ['title_key']
        read_only_fields = ['id', 'created_at', 'updated_at']

class TestCaseListSerializer(serializers.ModelSerializer):
    author = serializers.SerializerMethodField()
    assignee = serializers.SerializerMethodField()
    project = serializers.SerializerMethodField()
    versions = serializers.SerializerMethodField()
    
    class Meta:
        model = TestCase
        fields = [
            'id', 'title', 'description', 'preconditions', 'steps', 'expected_result',
            'priority', 'test_type',
            'author', 'assignee', 'project', 'versions', 'tags', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def get_author(self, obj):
        return {'id': obj.author.id, 'username': obj.author.username} if obj.author else None
    
    def get_assignee(self, obj):
        return {'id': obj.assignee.id, 'username': obj.assignee.username} if obj.assignee else None
    
    def get_project(self, obj):
        return {'id': obj.project.id, 'name': obj.project.name} if obj.project else None
    
    def get_versions(self, obj):
        return [{'id': v.id, 'name': v.name, 'is_baseline': v.is_baseline} for v in obj.versions.all()]

class TestCaseCreateSerializer(serializers.ModelSerializer):
    project_id = serializers.IntegerField(required=False, allow_null=True, help_text="项目ID，可选")
    version_ids = serializers.ListField(
        child=serializers.IntegerField(), 
        required=False, 
        allow_empty=True,
        help_text="关联版本ID列表"
    )
    
    class Meta:
        model = TestCase
        fields = [
            'title', 'description', 'preconditions', 'steps', 'expected_result',
            'priority', 'test_type', 'tags', 'project_id', 'version_ids'
        ]
    
    def create(self, validated_data):
        version_ids = validated_data.pop('version_ids', [])
        # project_id会在视图的perform_create中处理
        validated_data.pop('project_id', None)

        project = validated_data.pop('project')
        title = validated_data.pop('title')
        testcase, created = TestCaseDeduplicationService.create(
            project=project,
            title=title,
            **validated_data,
        )
        if not created:
            raise serializers.ValidationError({
                'title': '当前项目下已存在同标题的测试用例'
            })
        
        # 设置版本关联
        if version_ids:
            testcase.versions.set(version_ids)
        
        return testcase

class TestCaseUpdateSerializer(serializers.ModelSerializer):
    project_id = serializers.IntegerField(required=False, allow_null=True, help_text="项目ID，可选")
    version_ids = serializers.ListField(
        child=serializers.IntegerField(), 
        required=False, 
        allow_empty=True,
        help_text="关联版本ID列表"
    )
    
    class Meta:
        model = TestCase
        fields = [
            'title', 'description', 'preconditions', 'steps', 'expected_result',
            'priority', 'test_type', 'tags', 'project_id', 'version_ids'
        ]
    
    def update(self, instance, validated_data):
        version_ids = validated_data.pop('version_ids', None)
        # project_id会在视图中处理
        validated_data.pop('project_id', None)

        project = validated_data.get('project', instance.project)
        title = TestCaseDeduplicationService.normalize_title(
            validated_data.get('title', instance.title)
        )
        if TestCaseDeduplicationService.find_duplicate(
                project_id=project.id,
                title=title,
                exclude_id=instance.id):
            raise serializers.ValidationError({
                'title': '当前项目下已存在同标题的测试用例'
            })
        validated_data['title'] = title

        try:
            with transaction.atomic():
                instance = super().update(instance, validated_data)
        except IntegrityError:
            raise serializers.ValidationError({
                'title': '当前项目下已存在同标题的测试用例'
            })
        
        # 更新版本关联
        if version_ids is not None:
            instance.versions.set(version_ids)

        return instance


class TestCaseImportRecordListSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)

    class Meta:
        model = TestCaseImportRecord
        fields = [
            'id', 'import_no', 'project', 'project_name', 'status', 'progress',
            'total_rows', 'success_count', 'failed_count', 'skip_count',
            'error_message', 'template_version', 'created_by_name',
            'created_at', 'completed_at'
        ]


class TestCaseImportRecordDetailSerializer(TestCaseImportRecordListSerializer):
    failure_report_url = serializers.SerializerMethodField()

    class Meta(TestCaseImportRecordListSerializer.Meta):
        fields = TestCaseImportRecordListSerializer.Meta.fields + [
            'failure_details', 'failure_report_file', 'failure_report_url'
        ]
        extra_kwargs = {'failure_report_file': {'write_only': True}}

    def get_failure_report_url(self, obj):
        if obj.failure_report_file:
            return f'/api/testcases/import-records/{obj.id}/failure-report/'
        return None
