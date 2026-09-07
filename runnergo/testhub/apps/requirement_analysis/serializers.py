import json

from rest_framework import serializers
from .models import (
    RequirementDocument, RequirementAnalysis, BusinessRequirement,
    GeneratedTestCase, AnalysisTask, AIModelConfig, PromptConfig, TestSkill, TestCaseGenerationTask,
    GenerationConfig, AIModelService, BusinessRuleKnowledge, BusinessRuleUsage,
    PrototypeUnderstandingAsset, KnowledgeNode, KnowledgeRelation, BusinessWorkflow
)
from apps.projects.models import Project


def validate_project_access(project, request):
    if project is None:
        return project
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        raise serializers.ValidationError('必须登录后才能关联项目')
    if user.is_superuser or project.owner_id == user.pk or project.members.filter(pk=user.pk).exists():
        return project
    raise serializers.ValidationError('无权访问所选项目')


class RequirementDocumentSerializer(serializers.ModelSerializer):
    uploaded_by_name = serializers.CharField(source='uploaded_by.username', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    document_type_display = serializers.CharField(source='get_document_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    file_url = serializers.SerializerMethodField()
    
    class Meta:
        model = RequirementDocument
        fields = ['id', 'title', 'file', 'file_url', 'document_type', 'document_type_display', 
                 'status', 'status_display', 'uploaded_by', 'uploaded_by_name', 'project', 
                 'project_name', 'created_at', 'updated_at', 'file_size', 'content_hash', 'extracted_text']
        read_only_fields = ['uploaded_by', 'file_size', 'content_hash', 'extracted_text']
        extra_kwargs = {'file': {'write_only': True}}
    
    def get_file_url(self, obj):
        if obj.file:
            return f'/api/requirement-analysis/documents/{obj.id}/download/'
        return None

    def validate_project(self, project):
        return validate_project_access(project, self.context.get('request'))


class BusinessRequirementSerializer(serializers.ModelSerializer):
    requirement_type_display = serializers.CharField(source='get_requirement_type_display', read_only=True)
    requirement_level_display = serializers.CharField(source='get_requirement_level_display', read_only=True)
    parent_requirement_name = serializers.CharField(source='parent_requirement.requirement_name', read_only=True)
    
    class Meta:
        model = BusinessRequirement
        fields = ['id', 'requirement_id', 'requirement_name', 'requirement_type', 
                 'requirement_type_display', 'parent_requirement', 'parent_requirement_name',
                 'module', 'requirement_level', 'requirement_level_display', 'reviewer', 
                 'estimated_hours', 'description', 'acceptance_criteria', 'created_at', 'updated_at']


class RequirementAnalysisSerializer(serializers.ModelSerializer):
    document_title = serializers.CharField(source='document.title', read_only=True)
    document_id = serializers.IntegerField(source='document.id', read_only=True)
    requirements = BusinessRequirementSerializer(many=True, read_only=True)
    
    class Meta:
        model = RequirementAnalysis
        fields = ['id', 'document_id', 'document_title', 'analysis_report', 
                 'requirements_count', 'analysis_time', 'created_at', 'updated_at', 'requirements']


class GeneratedTestCaseSerializer(serializers.ModelSerializer):
    priority_display = serializers.CharField(source='get_priority_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    requirement_name = serializers.CharField(source='requirement.requirement_name', read_only=True)
    requirement_id_display = serializers.CharField(source='requirement.requirement_id', read_only=True)
    
    class Meta:
        model = GeneratedTestCase
        fields = ['id', 'case_id', 'title', 'priority', 'priority_display', 'precondition',
                 'test_steps', 'expected_result', 'status', 'status_display', 'generated_by_ai',
                 'reviewed_by_ai', 'review_comments', 'requirement', 'requirement_name', 
                 'requirement_id_display', 'created_at', 'updated_at']


class AnalysisTaskSerializer(serializers.ModelSerializer):
    task_type_display = serializers.CharField(source='get_task_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    document_title = serializers.CharField(source='document.title', read_only=True)
    duration = serializers.SerializerMethodField()
    
    class Meta:
        model = AnalysisTask
        fields = ['id', 'task_id', 'task_type', 'task_type_display', 'document', 'document_title',
                 'status', 'status_display', 'progress', 'result', 'error_message', 
                 'started_at', 'completed_at', 'created_at', 'duration']
        read_only_fields = ['task_id', 'result', 'error_message', 'started_at', 'completed_at']
    
    def get_duration(self, obj):
        if obj.started_at and obj.completed_at:
            return (obj.completed_at - obj.started_at).total_seconds()
        return None


class DocumentUploadSerializer(serializers.ModelSerializer):
    """文档上传专用序列化器"""
    class Meta:
        model = RequirementDocument
        fields = ['id', 'title', 'file', 'project']
        extra_kwargs = {'file': {'write_only': True}}

    def validate_file(self, file):
        import os
        allowed_extensions = {
            '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.xlsm', '.txt', '.md', '.markdown',
            '.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif',
            '.yaml', '.yml', '.json', '.har', '.xml', '.csv', '.sql',
        }
        extension = os.path.splitext(file.name)[1].lower()
        if extension not in allowed_extensions:
            raise serializers.ValidationError(
                '不支持的文件格式。支持 PDF、Excel、Word、TXT、Markdown、图片、Swagger JSON、OpenAPI、Postman Collection、HAR、yaml、json、xml、CSV、SQL。'
            )
        max_size = 20 * 1024 * 1024
        if file.size > max_size:
            raise serializers.ValidationError('文件不能超过 20MB。')
        return file

    def validate_project(self, project):
        return validate_project_access(project, self.context.get('request'))
    
    def create(self, validated_data):
        from .prototype_understanding import file_sha256

        # 自动设置上传者（如果用户已登录）
        user = self.context['request'].user
        if user.is_authenticated:
            validated_data['uploaded_by'] = user
        else:
            # 如果是匿名用户，使用第一个超级用户作为默认用户
            from apps.users.models import User
            default_user = User.objects.filter(is_superuser=True).first()
            if not default_user:
                default_user = User.objects.first()
            validated_data['uploaded_by'] = default_user
        
        # 根据文件扩展名设置文档类型
        file = validated_data['file']
        if file.name.lower().endswith('.pdf'):
            validated_data['document_type'] = 'pdf'
        elif file.name.lower().endswith('.doc'):
            validated_data['document_type'] = 'doc'
        elif file.name.lower().endswith('.docx'):
            validated_data['document_type'] = 'docx'
        elif file.name.lower().endswith(('.xls', '.xlsx', '.xlsm')):
            validated_data['document_type'] = 'excel'
        elif file.name.lower().endswith('.txt'):
            validated_data['document_type'] = 'txt'
        elif file.name.lower().endswith(('.md', '.markdown')):
            validated_data['document_type'] = 'md'
        elif file.name.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif')):
            validated_data['document_type'] = 'image'
        elif file.name.lower().endswith(('.yaml', '.yml')):
            validated_data['document_type'] = 'yaml'
        elif file.name.lower().endswith('.json'):
            validated_data['document_type'] = 'json'
        elif file.name.lower().endswith('.har'):
            validated_data['document_type'] = 'har'
        elif file.name.lower().endswith('.xml'):
            validated_data['document_type'] = 'xml'
        elif file.name.lower().endswith('.csv'):
            validated_data['document_type'] = 'csv'
        elif file.name.lower().endswith('.sql'):
            validated_data['document_type'] = 'sql'
        
        # 设置文件大小
        validated_data['file_size'] = file.size
        validated_data['content_hash'] = file_sha256(file)

        # 哈希只记录本次文档的真实内容，唯一性校验由当前生成任务的文件列表负责；
        # 不对历史文档做全局唯一限制，允许下一轮生成再次上传同一文件。
        return super().create(validated_data)


class PrototypeUnderstandingAssetSerializer(serializers.ModelSerializer):
    """产品原型/页面资料理解资产序列化器。"""
    uploaded_by_name = serializers.CharField(source='uploaded_by.username', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    asset_type_display = serializers.CharField(source='get_asset_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = PrototypeUnderstandingAsset
        fields = [
            'id', 'title', 'file', 'file_url', 'original_name',
            'asset_type', 'asset_type_display', 'status', 'status_display',
            'project', 'project_name', 'uploaded_by', 'uploaded_by_name',
            'file_size', 'content_hash', 'extracted_text', 'analysis_result',
            'ui_flow_draft', 'error_message', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'uploaded_by', 'original_name', 'asset_type', 'status',
            'file_size', 'content_hash', 'extracted_text', 'analysis_result',
            'ui_flow_draft', 'error_message', 'created_at', 'updated_at'
        ]
        extra_kwargs = {'file': {'write_only': True}}

    def get_file_url(self, obj):
        if obj.file:
            return f'/api/requirement-analysis/prototype-understanding/{obj.id}/download/'
        return None

    def validate_file(self, file):
        from .prototype_understanding import validate_prototype_upload

        try:
            validate_prototype_upload(file)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc))
        return file

    def validate_project(self, project):
        return validate_project_access(project, self.context.get('request'))

    def create(self, validated_data):
        from .prototype_understanding import detect_asset_type, file_sha256

        request = self.context.get('request')
        user = request.user if request else None
        if user and user.is_authenticated:
            validated_data['uploaded_by'] = user
        else:
            from apps.users.models import User
            validated_data['uploaded_by'] = User.objects.filter(is_superuser=True).first() or User.objects.first()

        file = validated_data['file']
        validated_data['original_name'] = file.name
        validated_data['asset_type'] = detect_asset_type(file.name, getattr(file, 'content_type', ''))
        validated_data['file_size'] = file.size
        validated_data['content_hash'] = file_sha256(file)
        return super().create(validated_data)


class TestCaseGenerationRequestSerializer(serializers.Serializer):
    """测试用例生成请求序列化器"""
    requirement_ids = serializers.ListField(
        child=serializers.IntegerField(),
        help_text="需求ID列表"
    )
    test_level = serializers.ChoiceField(
        choices=[('unit', '单元测试'), ('integration', '集成测试'), ('system', '系统测试'), ('acceptance', '验收测试')],
        default='system',
        help_text="测试级别"
    )
    test_priority = serializers.ChoiceField(
        choices=[('P0', '最高优先级'), ('P1', '高优先级'), ('P2', '中优先级'), ('P3', '低优先级')],
        default='P1',
        help_text="测试优先级"
    )
    test_case_count = serializers.IntegerField(
        min_value=1,
        max_value=200,
        default=50,
        help_text="生成测试用例数量"
    )


class TestCaseReviewRequestSerializer(serializers.Serializer):
    """测试用例评审请求序列化器"""
    test_case_ids = serializers.ListField(
        child=serializers.IntegerField(),
        help_text="测试用例ID列表"
    )
    review_criteria = serializers.CharField(
        max_length=500,
        default="检查测试用例的完整性、准确性和可执行性",
        help_text="评审标准"
    )


class AIModelConfigSerializer(serializers.ModelSerializer):
    """AI模型配置序列化器"""
    model_type_display = serializers.CharField(source='get_model_type_display', read_only=True)
    role_display = serializers.CharField(source='get_role_display', read_only=True)
    model_usage_display = serializers.CharField(source='get_model_usage_display', read_only=True)
    model_status_display = serializers.CharField(source='get_model_status_display', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    api_key_masked = serializers.SerializerMethodField(read_only=True)
    
    class Meta:
        model = AIModelConfig
        fields = ['id', 'name', 'model_type', 'model_type_display', 'role', 'role_display',
                 'model_usage', 'model_usage_display', 'capability_tags', 'api_key', 'api_key_masked',
                 'base_url', 'model_name', 'model_version', 'max_tokens', 'max_output_tokens',
                 'temperature', 'top_p', 'retry_count', 'model_status',
                 'model_status_display', 'is_active', 'created_by',
                 'created_by_name', 'created_at', 'updated_at']
        read_only_fields = ['created_by', 'created_by_name']
        extra_kwargs = {
            'api_key': {'write_only': True}  # API Key只用于写入，不在响应中返回
        }
    
    def get_api_key_masked(self, obj):
        """返回掩码版本的API Key"""
        api_key = obj.get_api_key()
        if api_key:
            # 显示前3个字符和后4个字符，中间用*替代
            if len(api_key) > 7:
                return f"{api_key[:3]}{'*' * (len(api_key) - 7)}{api_key[-4:]}"
            else:
                return '*' * len(api_key)
        return ''

    def validate_base_url(self, value):
        from apps.core.outbound import validate_outbound_http_url

        try:
            return validate_outbound_http_url(value, resolve=False, label='AI API 地址')
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc
    
    def create(self, validated_data):
        # 自动设置创建者
        validated_data['bound_prompt'] = None
        user = self.context['request'].user
        if user.is_authenticated:
            validated_data['created_by'] = user
        else:
            # 如果是匿名用户，使用第一个超级用户作为默认用户
            from apps.users.models import User
            default_user = User.objects.filter(is_superuser=True).first()
            if not default_user:
                default_user = User.objects.first()
            validated_data['created_by'] = default_user
        
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data['bound_prompt'] = None
        return super().update(instance, validated_data)


class PromptConfigSerializer(serializers.ModelSerializer):
    """提示词配置序列化器"""
    prompt_type_display = serializers.CharField(source='get_prompt_type_display', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    
    class Meta:
        model = PromptConfig
        fields = ['id', 'name', 'prompt_version', 'prompt_type', 'prompt_type_display', 'content', 'is_active',
                 'created_by', 'created_by_name', 'created_at', 'updated_at']
        read_only_fields = ['created_by', 'created_by_name']
    
    def create(self, validated_data):
        # 自动设置创建者
        user = self.context['request'].user
        if user.is_authenticated:
            validated_data['created_by'] = user
        else:
            # 如果是匿名用户，使用第一个超级用户作为默认用户
            from apps.users.models import User
            default_user = User.objects.filter(is_superuser=True).first()
            if not default_user:
                default_user = User.objects.first()
            validated_data['created_by'] = default_user
        
        return super().create(validated_data)


class TestSkillSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    usage_count = serializers.SerializerMethodField()
    command = serializers.CharField(read_only=True)

    class Meta:
        model = TestSkill
        fields = [
            'id', 'name', 'identifier', 'command', 'version', 'description', 'tags',
            'project', 'project_name', 'entrypoint', 'execution_config', 'manifest', 'content_hash',
            'file_count', 'total_size', 'is_active', 'created_by',
            'created_by_name', 'usage_count', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'name', 'identifier', 'version', 'description', 'tags', 'entrypoint', 'execution_config',
            'manifest', 'content_hash', 'file_count', 'total_size', 'created_by',
            'created_by_name', 'usage_count', 'created_at', 'updated_at',
        ]

    def get_usage_count(self, obj):
        return obj.generation_tasks.count()

    def validate_project(self, project):
        return validate_project_access(project, self.context.get('request'))


class TestCaseGenerationTaskSerializer(serializers.ModelSerializer):
    """测试用例生成任务序列化器"""
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    writer_model_name = serializers.CharField(source='writer_model_config.name', read_only=True)
    reviewer_model_name = serializers.CharField(source='reviewer_model_config.name', read_only=True)
    output_mode_display = serializers.CharField(source='get_output_mode_display', read_only=True)
    generation_mode_display = serializers.CharField(source='get_generation_mode_display', read_only=True)
    quality_completed = serializers.SerializerMethodField()
    error_message = serializers.SerializerMethodField()
    generation_events = serializers.SerializerMethodField()

    def get_quality_completed(self, obj):
        return (
            obj.status == 'completed'
            and bool(obj.final_test_cases)
        )

    def get_error_message(self, obj):
        return AIModelService.sanitize_error_message(obj.error_message) if obj.error_message else ''

    def get_generation_events(self, obj):
        """Return the structured execution timeline stored in generation_log."""
        try:
            events = json.loads(obj.generation_log or '[]')
        except (TypeError, ValueError, json.JSONDecodeError):
            events = []
        return events if isinstance(events, list) else []

    def validate_project(self, project):
        return validate_project_access(project, self.context.get('request'))
    
    class Meta:
        model = TestCaseGenerationTask
        fields = ['id', 'task_id', 'title', 'requirement_text', 'status', 'status_display',
                 'progress', 'output_mode', 'output_mode_display', 'generation_mode',
                 'generation_mode_display', 'case_type_rules', 'knowledge_rule_context', 'selected_skills',
                 'skill_context', 'skill_execution_results', 'project', 'project_name', 'writer_model_config', 'writer_model_name',
                 'reviewer_model_config', 'reviewer_model_name', 'generated_test_cases',
                 'review_feedback', 'final_test_cases', 'review_score', 'review_round',
                 'review_pending', 'review_history', 'best_review_score',
                 'quality_completed', 'generation_log', 'generation_events', 'error_message',
                 'created_by', 'created_by_name', 'created_at', 'updated_at', 'completed_at']
        read_only_fields = ['task_id', 'status', 'progress', 'generated_test_cases', 
                          'review_feedback', 'final_test_cases', 'review_score', 'review_round',
                          'review_pending', 'review_history', 'best_review_score',
                          'quality_completed', 'generation_log',
                          'error_message', 'created_by', 'completed_at', 'knowledge_rule_context',
                          'selected_skills', 'skill_context', 'skill_execution_results']
    
    def create(self, validated_data):
        # 自动设置创建者和任务ID
        import uuid
        user = self.context['request'].user
        if user.is_authenticated:
            validated_data['created_by'] = user
        else:
            from apps.users.models import User
            default_user = User.objects.filter(is_superuser=True).first()
            if not default_user:
                default_user = User.objects.first()
            validated_data['created_by'] = default_user
        
        validated_data['task_id'] = f"TASK_{uuid.uuid4().hex[:8].upper()}"
        
        return super().create(validated_data)


class TestCaseGenerationRequestSerializer(serializers.Serializer):
    """新的测试用例生成请求序列化器"""
    title = serializers.CharField(max_length=200, help_text="任务标题")
    requirement_text = serializers.CharField(help_text="需求描述")
    use_writer_model = serializers.BooleanField(default=True, help_text="是否使用编写模型")
    use_reviewer_model = serializers.BooleanField(default=True, help_text="是否使用评审模型")
    project = serializers.PrimaryKeyRelatedField(
        queryset=Project.objects.all(),
        required=False,
        allow_null=True,
        help_text="关联项目"
    )
    output_mode = serializers.ChoiceField(
        choices=[('stream', '实时流式输出'), ('complete', '完整输出')],
        required=False,
        help_text="输出模式"
    )
    generation_mode = serializers.ChoiceField(
        choices=[('quick', '快速生成'), ('deep', '深度测试设计'), ('security', '安全专项测试')],
        required=False,
        default='quick',
        help_text="测试生成模式"
    )
    case_type_rules = serializers.JSONField(
        required=False,
        default=dict,
        help_text="用例类型配额规则"
    )
    selected_rule_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        default=list,
        help_text='本次生成采纳的业务规则ID，支持一次性选择当前筛选结果中的全部规则'
    )
    selected_workflow_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        default=list,
        max_length=10,
        help_text='本次生成关联的业务工作流ID'
    )
    selected_skill_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        default=list,
        max_length=8,
        help_text='本次生成选择的测试Skill ID；按列表顺序串行执行',
    )
    save_current_rule = serializers.BooleanField(
        required=False,
        default=False,
        help_text='是否将本次填写的重点规则保存到知识库'
    )
    current_rule_content = serializers.CharField(max_length=500, required=False, allow_blank=True)
    current_rule_title = serializers.CharField(max_length=200, required=False, allow_blank=True)
    current_rule_module = serializers.CharField(max_length=100, required=False, allow_blank=True)
    current_rule_type = serializers.ChoiceField(
        choices=BusinessRuleKnowledge.RULE_TYPE_CHOICES,
        required=False,
        default='business'
    )
    current_rule_parent_node = serializers.PrimaryKeyRelatedField(
        queryset=KnowledgeNode.objects.all(),
        required=False,
        allow_null=True,
        help_text='本次保存规则挂载到的知识拓扑节点'
    )

    def validate_case_type_rules(self, value):
        if not value:
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError('用例类型配额规则必须是对象')

        enabled = bool(value.get('enabled'))
        focus_keywords = str(value.get('focus_keywords') or '').strip()
        if len(focus_keywords) > 500:
            raise serializers.ValidationError('重点关键词或业务规则不能超过 500 个字符')
        if not enabled:
            return {'enabled': False, 'focus_keywords': focus_keywords}

        allowed_types = {
            'functional', 'exception', 'boundary',
            'equivalence', 'security', 'compatibility'
        }
        focused_types = value.get('focused_types') or []
        if not isinstance(focused_types, list):
            raise serializers.ValidationError('重点测试类型必须是数组')

        focused_types = list(dict.fromkeys(focused_types))
        invalid_types = [item for item in focused_types if item not in allowed_types]
        if invalid_types:
            raise serializers.ValidationError(f'不支持的测试类型: {", ".join(invalid_types)}')
        if not focused_types:
            raise serializers.ValidationError('启用配额规则后请至少选择一个重点测试类型')

        try:
            focused_count = int(value.get('focused_count', 12))
        except (TypeError, ValueError):
            raise serializers.ValidationError('重点类型数量必须是整数')
        if focused_count < 4 or focused_count > 50:
            raise serializers.ValidationError('重点类型数量必须在 4 到 50 之间')

        return {
            'enabled': True,
            'focused_types': focused_types,
            'focused_count': focused_count,
            'default_count': 3,
            'focus_keywords': focus_keywords,
        }

    def validate_project(self, project):
        return validate_project_access(project, self.context.get('request'))

    def validate_selected_rule_ids(self, value):
        return list(dict.fromkeys(value))

    def validate_selected_workflow_ids(self, value):
        return list(dict.fromkeys(value))

    def validate_selected_skill_ids(self, value):
        return list(dict.fromkeys(value))

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.context.get('require_primary_skill') and not attrs.get('selected_skill_ids'):
            raise serializers.ValidationError({
                'selected_skill_ids': 'AI用例生成必须选择至少一个 Skill 作为执行工作流'
            })
        return attrs


class BusinessRuleKnowledgeSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    rule_type_display = serializers.CharField(source='get_rule_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    risk_level_display = serializers.CharField(source='get_risk_level_display', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    reviewed_by_name = serializers.CharField(source='reviewed_by.username', read_only=True)
    source_task_id = serializers.CharField(source='source_task.task_id', read_only=True)
    graph_path = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = BusinessRuleKnowledge
        fields = [
            'id', 'title', 'content', 'keywords', 'project', 'project_name', 'module',
            'business_domain', 'entity_name', 'attribute_name', 'state_values',
            'relation_nodes', 'test_strategies', 'risk_level', 'risk_level_display',
            'graph_path',
            'rule_type', 'rule_type_display', 'applicable_conditions', 'expected_behavior',
            'exceptions', 'source_requirement', 'source_task', 'source_task_id', 'status',
            'status_display', 'version', 'usage_count', 'accepted_count', 'created_by',
            'created_by_name', 'reviewed_by', 'reviewed_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'source_task', 'usage_count', 'accepted_count', 'created_by', 'reviewed_by',
            'created_at', 'updated_at'
        ]

    def validate_keywords(self, value):
        if value in (None, ''):
            return []
        if isinstance(value, str):
            value = [item.strip() for item in value.replace('，', ',').split(',') if item.strip()]
        if not isinstance(value, list):
            raise serializers.ValidationError('关键词必须是数组或逗号分隔文本')
        result = []
        for item in value:
            keyword = str(item).strip()
            if keyword and keyword not in result:
                result.append(keyword[:50])
        return result[:20]

    def validate_project(self, project):
        return validate_project_access(project, self.context.get('request'))

    def _normalize_text_list(self, value, field_name, limit=30, max_length=80):
        if value in (None, ''):
            return []
        if isinstance(value, str):
            value = [
                item.strip()
                for item in value.replace('，', ',').replace('\n', ',').split(',')
                if item.strip()
            ]
        if not isinstance(value, list):
            raise serializers.ValidationError(f'{field_name}必须是数组、逗号分隔文本或换行文本')
        result = []
        for item in value:
            if isinstance(item, dict):
                compact = {
                    str(key).strip()[:30]: str(item[key]).strip()[:max_length]
                    for key in item
                    if str(key).strip() and str(item[key]).strip()
                }
                if compact and compact not in result:
                    result.append(compact)
                continue
            text = str(item).strip()
            if text and text not in result:
                result.append(text[:max_length])
            if len(result) >= limit:
                break
        return result[:limit]

    def validate_state_values(self, value):
        return self._normalize_text_list(value, '状态/枚举值')

    def validate_relation_nodes(self, value):
        return self._normalize_text_list(value, '关联节点')

    def validate_test_strategies(self, value):
        return self._normalize_text_list(value, '测试生成策略')

    def get_graph_path(self, obj):
        parts = [
            obj.business_domain,
            obj.entity_name,
            obj.attribute_name,
        ]
        return ' / '.join(str(part).strip() for part in parts if str(part or '').strip())

    def create(self, validated_data):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            validated_data['created_by'] = request.user
            if validated_data.get('status') == 'approved':
                validated_data['reviewed_by'] = request.user
        else:
            from apps.users.models import User
            default_user = User.objects.filter(is_superuser=True).first() or User.objects.first()
            validated_data['created_by'] = default_user
            if validated_data.get('status') == 'approved':
                validated_data['reviewed_by'] = default_user
        return super().create(validated_data)

    def update(self, instance, validated_data):
        request = self.context.get('request')
        if validated_data.get('status') == 'approved' and request and request.user.is_authenticated:
            validated_data['reviewed_by'] = request.user
        version_fields = (
            'content', 'applicable_conditions', 'expected_behavior', 'exceptions',
            'business_domain', 'entity_name', 'attribute_name', 'state_values',
            'relation_nodes', 'test_strategies', 'risk_level',
        )
        if any(field in validated_data for field in version_fields):
            validated_data['version'] = instance.version + 1
        return super().update(instance, validated_data)


class BusinessWorkflowSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    source_file_url = serializers.SerializerMethodField()
    node_count = serializers.SerializerMethodField()
    edge_count = serializers.SerializerMethodField()

    class Meta:
        model = BusinessWorkflow
        fields = [
            'id', 'name', 'identifier', 'category', 'version', 'project',
            'project_name', 'description', 'definition', 'source_file',
            'source_file_url', 'source_text', 'status', 'status_display',
            'node_count', 'edge_count', 'created_by', 'created_by_name',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['created_by', 'created_at', 'updated_at']
        extra_kwargs = {'source_file': {'write_only': True}}

    def get_source_file_url(self, obj):
        if obj.source_file:
            return f'/api/requirement-analysis/workflows/{obj.id}/download/'
        return None

    def get_node_count(self, obj):
        definition = obj.definition if isinstance(obj.definition, dict) else {}
        return len(definition.get('nodes') or [])

    def get_edge_count(self, obj):
        definition = obj.definition if isinstance(obj.definition, dict) else {}
        return len(definition.get('edges') or [])

    def validate_definition(self, value):
        if value in (None, ''):
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError('工作流定义必须是对象')
        nodes = value.get('nodes') or []
        edges = value.get('edges') or []
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise serializers.ValidationError('工作流定义必须包含 nodes/edges 数组')
        return value

    def validate_project(self, project):
        return validate_project_access(project, self.context.get('request'))

    def create(self, validated_data):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            validated_data['created_by'] = request.user
        else:
            from apps.users.models import User
            validated_data['created_by'] = User.objects.filter(is_superuser=True).first() or User.objects.first()
        return super().create(validated_data)

    def update(self, instance, validated_data):
        version_fields = ('definition', 'description', 'name', 'identifier', 'category')
        if any(field in validated_data for field in version_fields):
            validated_data['version'] = instance.version + 1
        return super().update(instance, validated_data)


class BusinessRuleUsageSerializer(serializers.ModelSerializer):
    rule_title = serializers.CharField(source='rule.title', read_only=True)
    task_id = serializers.CharField(source='task.task_id', read_only=True)

    class Meta:
        model = BusinessRuleUsage
        fields = [
            'id', 'rule', 'rule_title', 'task', 'task_id', 'action',
            'similarity_score', 'rule_snapshot', 'created_at'
        ]
        read_only_fields = fields


class KnowledgeNodeSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    type_display = serializers.CharField(source='get_type_display', read_only=True)
    rule_title = serializers.CharField(source='rule.title', read_only=True)

    class Meta:
        model = KnowledgeNode
        fields = [
            'id', 'name', 'type', 'type_display', 'content', 'project',
            'project_name', 'rule', 'rule_title', 'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']

    def validate_project(self, project):
        return validate_project_access(project, self.context.get('request'))

    def validate(self, attrs):
        attrs = super().validate(attrs)
        instance = self.instance
        requested_type = attrs.get('type')
        if (
            instance
            and instance.type == 'domain'
            and requested_type
            and requested_type != 'domain'
        ):
            raise serializers.ValidationError({
                'type': '知识文件不能修改为其他节点类型',
            })
        return attrs


class KnowledgeRelationSerializer(serializers.ModelSerializer):
    source_name = serializers.CharField(source='source.name', read_only=True)
    target_name = serializers.CharField(source='target.name', read_only=True)
    relation_display = serializers.CharField(source='get_relation_type_display', read_only=True)

    class Meta:
        model = KnowledgeRelation
        fields = [
            'id', 'source', 'source_name', 'target', 'target_name',
            'relation_type', 'relation_display', 'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']

    def validate(self, attrs):
        source = attrs.get('source') or getattr(self.instance, 'source', None)
        target = attrs.get('target') or getattr(self.instance, 'target', None)
        request = self.context.get('request')
        if source and target and source.id == target.id:
            raise serializers.ValidationError('知识关系不能指向自身')
        if source and target:
            validate_project_access(source.project, request)
            validate_project_access(target.project, request)
            source_project_id = source.project_id
            target_project_id = target.project_id
            if source_project_id and target_project_id and source_project_id != target_project_id:
                raise serializers.ValidationError('不能在两个不同项目的知识节点之间创建关系')
        return attrs


class GenerationConfigSerializer(serializers.ModelSerializer):
    """生成行为配置序列化器"""
    default_output_mode_display = serializers.CharField(source='get_default_output_mode_display', read_only=True)

    class Meta:
        model = GenerationConfig
        fields = [
            'id', 'name', 'default_output_mode', 'default_output_mode_display',
            'enable_auto_review', 'review_timeout',
            'is_active', 'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']
