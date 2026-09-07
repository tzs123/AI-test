from django.contrib import admin
from .models import (
    RequirementDocument, RequirementAnalysis, BusinessRequirement,
    GeneratedTestCase, AnalysisTask, AIModelConfig, PromptConfig, TestSkill,
    GenerationConfig, TestCaseGenerationTask, BusinessRuleKnowledge, BusinessRuleUsage,
    KnowledgeNode, KnowledgeRelation, BusinessWorkflow
)


@admin.register(RequirementDocument)
class RequirementDocumentAdmin(admin.ModelAdmin):
    list_display = ['title', 'document_type', 'status', 'uploaded_by', 'created_at']
    list_filter = ['document_type', 'status', 'created_at']
    search_fields = ['title', 'uploaded_by__username']
    readonly_fields = ['file_size', 'extracted_text', 'created_at', 'updated_at']


@admin.register(RequirementAnalysis)
class RequirementAnalysisAdmin(admin.ModelAdmin):
    list_display = ['document', 'requirements_count', 'analysis_time', 'created_at']
    list_filter = ['created_at']
    search_fields = ['document__title']
    readonly_fields = ['analysis_time', 'created_at', 'updated_at']


@admin.register(BusinessRequirement)
class BusinessRequirementAdmin(admin.ModelAdmin):
    list_display = ['requirement_id', 'requirement_name', 'requirement_type', 'requirement_level', 'module']
    list_filter = ['requirement_type', 'requirement_level', 'module', 'created_at']
    search_fields = ['requirement_id', 'requirement_name', 'description']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(GeneratedTestCase)
class GeneratedTestCaseAdmin(admin.ModelAdmin):
    list_display = ['case_id', 'title', 'priority', 'status', 'generated_by_ai', 'reviewed_by_ai']
    list_filter = ['priority', 'status', 'generated_by_ai', 'reviewed_by_ai', 'created_at']
    search_fields = ['case_id', 'title', 'requirement__requirement_name']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(AnalysisTask)
class AnalysisTaskAdmin(admin.ModelAdmin):
    list_display = ['task_id', 'task_type', 'status', 'progress', 'created_at']
    list_filter = ['task_type', 'status', 'created_at']
    search_fields = ['task_id', 'document__title']
    readonly_fields = ['task_id', 'started_at', 'completed_at', 'created_at']


@admin.register(AIModelConfig)
class AIModelConfigAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'model_type', 'role', 'model_usage', 'model_name', 'model_version',
        'capability_tags_display', 'model_status', 'is_active', 'created_at'
    ]
    list_filter = ['model_type', 'role', 'model_usage', 'model_status', 'is_active', 'created_at']
    search_fields = ['name', 'model_name', 'model_version']
    readonly_fields = ['created_at', 'updated_at']

    def capability_tags_display(self, obj):
        return '、'.join(obj.capability_tags or [])

    capability_tags_display.short_description = '能力标签'


@admin.register(PromptConfig)
class PromptConfigAdmin(admin.ModelAdmin):
    list_display = ['name', 'prompt_type', 'is_active', 'created_by', 'created_at']
    list_filter = ['prompt_type', 'is_active', 'created_at']
    search_fields = ['name', 'content']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(TestSkill)
class TestSkillAdmin(admin.ModelAdmin):
    list_display = ['name', 'identifier', 'version', 'project', 'is_active', 'file_count', 'updated_at']
    list_filter = ['is_active', 'project', 'updated_at']
    search_fields = ['name', 'identifier', 'description']
    readonly_fields = ['storage_path', 'execution_config', 'manifest', 'content_hash', 'file_count', 'total_size', 'created_at', 'updated_at']


@admin.register(GenerationConfig)
class GenerationConfigAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'default_output_mode', 'enable_auto_review', 'is_active', 'updated_at'
    ]
    list_filter = ['default_output_mode', 'enable_auto_review', 'is_active']
    search_fields = ['name']
    readonly_fields = ['created_at', 'updated_at']

    fieldsets = (
        ('基本配置', {
            'fields': ('name', 'is_active')
        }),
        ('输出模式配置', {
            'fields': ('default_output_mode',)
        }),
        ('自动化配置', {
            'fields': ('enable_auto_review',)
        }),
        ('超时配置', {
            'fields': ('review_timeout',)
        }),
        ('时间戳', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(TestCaseGenerationTask)
class TestCaseGenerationTaskAdmin(admin.ModelAdmin):
    list_display = ['task_id', 'title', 'status', 'progress', 'output_mode', 'created_at']
    list_filter = ['status', 'output_mode', 'created_at']
    search_fields = ['task_id', 'title', 'requirement_text']
    readonly_fields = ['task_id', 'created_at', 'updated_at']


@admin.register(BusinessRuleKnowledge)
class BusinessRuleKnowledgeAdmin(admin.ModelAdmin):
    list_display = ['title', 'project', 'module', 'rule_type', 'status', 'version', 'accepted_count', 'updated_at']
    list_filter = ['status', 'rule_type', 'project', 'updated_at']
    search_fields = ['title', 'content', 'module', 'source_requirement']
    readonly_fields = ['usage_count', 'accepted_count', 'created_at', 'updated_at']


@admin.register(BusinessRuleUsage)
class BusinessRuleUsageAdmin(admin.ModelAdmin):
    list_display = ['rule', 'task', 'action', 'similarity_score', 'created_at']
    list_filter = ['action', 'created_at']
    search_fields = ['rule__title', 'task__task_id', 'task__title']
    readonly_fields = ['created_at']


@admin.register(BusinessWorkflow)
class BusinessWorkflowAdmin(admin.ModelAdmin):
    list_display = ['name', 'identifier', 'category', 'project', 'status', 'version', 'updated_at']
    list_filter = ['status', 'category', 'project', 'updated_at']
    search_fields = ['name', 'identifier', 'category', 'description', 'source_text']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(KnowledgeNode)
class KnowledgeNodeAdmin(admin.ModelAdmin):
    list_display = ['name', 'type', 'project', 'rule', 'updated_at']
    list_filter = ['type', 'project', 'updated_at']
    search_fields = ['name', 'content', 'rule__title']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(KnowledgeRelation)
class KnowledgeRelationAdmin(admin.ModelAdmin):
    list_display = ['source', 'target', 'relation_type', 'updated_at']
    list_filter = ['relation_type', 'updated_at']
    search_fields = ['source__name', 'target__name']
    readonly_fields = ['created_at', 'updated_at']
