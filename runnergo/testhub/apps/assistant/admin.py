from django.contrib import admin

from .models import AssistantMessage, AssistantSession, ChatMessage, CopilotTask, DifyConfig


@admin.register(DifyConfig)
class DifyConfigAdmin(admin.ModelAdmin):
    list_display = ['api_url', 'is_active', 'created_at', 'updated_at']
    list_filter = ['is_active', 'created_at']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(AssistantSession)
class AssistantSessionAdmin(admin.ModelAdmin):
    list_display = ['user', 'title', 'session_id', 'created_at', 'updated_at']
    list_filter = ['created_at', 'updated_at']
    search_fields = ['user__username', 'title', 'session_id']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ['session', 'role', 'content_preview', 'created_at']
    list_filter = ['role', 'created_at']
    search_fields = ['session__title', 'content']
    readonly_fields = ['created_at']

    def content_preview(self, obj):
        return obj.content[:100] + '...' if len(obj.content) > 100 else obj.content

    content_preview.short_description = '消息内容预览'


@admin.register(AssistantMessage)
class AssistantMessageAdmin(admin.ModelAdmin):
    list_display = ['session', 'message_type', 'content_preview', 'created_at']
    list_filter = ['message_type', 'created_at']
    search_fields = ['session__title', 'content']
    readonly_fields = ['created_at']

    def content_preview(self, obj):
        return obj.content[:100] + '...' if len(obj.content) > 100 else obj.content

    content_preview.short_description = '消息内容预览'


@admin.register(CopilotTask)
class CopilotTaskAdmin(admin.ModelAdmin):
    list_display = ['task_id', 'objective', 'project', 'status', 'created_by', 'created_at']
    list_filter = ['status', 'created_at']
    search_fields = ['task_id', 'objective', 'summary']
    readonly_fields = ['task_id', 'created_at', 'updated_at', 'completed_at']
