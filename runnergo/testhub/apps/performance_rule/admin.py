from django.contrib import admin

from .models import PerformanceResult, PerformanceRule


@admin.register(PerformanceRule)
class PerformanceRuleAdmin(admin.ModelAdmin):
    list_display = ('id', 'project', 'min_tps', 'max_p95', 'max_error_rate', 'max_cpu', 'create_time')
    list_filter = ('project',)
    search_fields = ('project__name',)


@admin.register(PerformanceResult)
class PerformanceResultAdmin(admin.ModelAdmin):
    list_display = ('id', 'project', 'report_id', 'overall_status', 'tps', 'p95', 'error_rate', 'cpu', 'create_time')
    list_filter = ('overall_status', 'project')
    search_fields = ('report_id', 'project__name')
