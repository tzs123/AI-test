# -*- coding: utf-8 -*-
from django.contrib import admin
from .models import (
    AppDevice,
    AppElement,
    AppComponent,
    AppCustomComponent,
    AppComponentPackage,
    AppPackage,
    AppTestCase,
    AppTestExecution,
    AppAgentTask,
    AppAgentEvent,
    AppAgentRuntimeSession,
    AppAgentRuntimeStep,
    AppAgentMatrixRun,
    AppAgentMatrixCell,
    AppLocatorKnowledge,
)


@admin.register(AppDevice)
class AppDeviceAdmin(admin.ModelAdmin):
    list_display = ('device_id', 'name', 'platform', 'status', 'android_version', 'ios_version', 'connection_type', 'locked_by', 'updated_at')
    list_filter = ('platform', 'status', 'connection_type')
    search_fields = ('device_id', 'name')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(AppElement)
class AppElementAdmin(admin.ModelAdmin):
    list_display = ('name', 'element_type', 'usage_count', 'is_active', 'created_at')
    list_filter = ('element_type', 'is_active')
    search_fields = ('name',)
    readonly_fields = ('created_at', 'updated_at', 'usage_count', 'last_used_at')


@admin.register(AppComponent)
class AppComponentAdmin(admin.ModelAdmin):
    list_display = ('name', 'type', 'category', 'enabled', 'sort_order')
    list_filter = ('category', 'enabled')
    search_fields = ('name', 'type')


@admin.register(AppCustomComponent)
class AppCustomComponentAdmin(admin.ModelAdmin):
    list_display = ('name', 'type', 'enabled', 'sort_order')
    list_filter = ('enabled',)
    search_fields = ('name', 'type')


@admin.register(AppComponentPackage)
class AppComponentPackageAdmin(admin.ModelAdmin):
    list_display = ('name', 'version', 'author', 'source', 'created_at')
    list_filter = ('source',)
    search_fields = ('name', 'author')


@admin.register(AppPackage)
class AppPackageAdmin(admin.ModelAdmin):
    list_display = ('name', 'package_name', 'created_by', 'created_at')
    search_fields = ('name', 'package_name')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(AppTestCase)
class AppTestCaseAdmin(admin.ModelAdmin):
    list_display = ('name', 'app_package', 'timeout', 'created_by', 'created_at')
    search_fields = ('name',)
    readonly_fields = ('created_at', 'updated_at')


@admin.register(AppTestExecution)
class AppTestExecutionAdmin(admin.ModelAdmin):
    list_display = ('test_case', 'device', 'status', 'progress', 'pass_rate', 'created_at')
    list_filter = ('status',)
    search_fields = ('test_case__name',)
    readonly_fields = ('created_at', 'updated_at', 'started_at', 'finished_at', 'duration')


@admin.register(AppAgentTask)
class AppAgentTaskAdmin(admin.ModelAdmin):
    list_display = ('id', 'goal', 'project', 'device', 'status', 'progress', 'user', 'created_at')
    list_filter = ('status', 'auto_execute', 'project')
    search_fields = ('goal', 'project__name', 'device__device_id', 'user__username')
    readonly_fields = ('created_at', 'updated_at', 'started_at', 'finished_at')


@admin.register(AppAgentEvent)
class AppAgentEventAdmin(admin.ModelAdmin):
    list_display = ('task', 'phase', 'level', 'message', 'created_at')
    list_filter = ('phase', 'level')
    search_fields = ('message', 'task__goal')
    readonly_fields = ('created_at',)


@admin.register(AppAgentRuntimeSession)
class AppAgentRuntimeSessionAdmin(admin.ModelAdmin):
    list_display = ('id', 'task', 'status', 'used_steps', 'max_steps', 'used_model_tokens', 'created_at')
    list_filter = ('status',)
    search_fields = ('task__goal', 'task__user__username')
    readonly_fields = ('created_at', 'updated_at', 'started_at', 'finished_at')


@admin.register(AppAgentRuntimeStep)
class AppAgentRuntimeStepAdmin(admin.ModelAdmin):
    list_display = ('session', 'sequence', 'status', 'approval_status', 'approval_category', 'element', 'created_at')
    list_filter = ('status', 'approval_status', 'approval_category')
    search_fields = ('session__task__goal', 'action', 'decision')
    readonly_fields = ('created_at', 'started_at', 'finished_at', 'approved_at')


@admin.register(AppLocatorKnowledge)
class AppLocatorKnowledgeAdmin(admin.ModelAdmin):
    list_display = ('project', 'element', 'platform', 'platform_version', 'strategy', 'confidence', 'updated_at')
    list_filter = ('platform', 'strategy')
    search_fields = ('project__name', 'element__name', 'app_package')
    readonly_fields = ('created_at', 'updated_at', 'last_success_at')


@admin.register(AppAgentMatrixRun)
class AppAgentMatrixRunAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'base_task', 'status', 'max_concurrency', 'created_at')
    list_filter = ('status', 'max_concurrency')
    search_fields = ('name', 'base_task__goal', 'base_task__user__username')
    readonly_fields = ('created_at', 'updated_at', 'started_at', 'finished_at')


@admin.register(AppAgentMatrixCell)
class AppAgentMatrixCellAdmin(admin.ModelAdmin):
    list_display = ('matrix_run', 'device', 'platform', 'platform_version', 'status', 'compatibility')
    list_filter = ('status', 'compatibility', 'platform')
    search_fields = ('matrix_run__name', 'device__name', 'device__device_id')
    readonly_fields = ('created_at', 'updated_at', 'started_at', 'finished_at')
