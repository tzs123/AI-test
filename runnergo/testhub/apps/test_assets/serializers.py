from django.db import models
from rest_framework import serializers

from apps.projects.models import Project
from apps.requirement_analysis.models import BusinessRequirement, GeneratedTestCase
from apps.reports.models import TestReport
from apps.testcases.models import TestCase

from .models import (
    ApiAsset,
    AutomationScriptAsset,
    DefectAsset,
    PageAsset,
    TestAssetLink,
    TestAssetRequirement,
)


def asset_label(link):
    if link.api_id:
        return f'{link.api.method} {link.api.path}'
    if link.page_id:
        suffix = f' · {link.page.element_name}' if link.page.element_name else ''
        return f'{link.page.name}{suffix}'
    if link.testcase_id:
        return link.testcase.title
    if link.automation_script_id:
        return link.automation_script.name
    if link.defect_id:
        return f'{link.defect.defect_key} {link.defect.title}'
    if link.report_id:
        return link.report.name
    if link.generated_case_id:
        return f'{link.generated_case.case_id} {link.generated_case.title}'
    if link.business_requirement_id:
        return f'{link.business_requirement.requirement_id} {link.business_requirement.requirement_name}'
    return '-'


class ProjectOwnedCreateSerializer(serializers.ModelSerializer):
    project_id = serializers.IntegerField(write_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)

    def validate_project_id(self, value):
        accessible_projects = self.context.get('accessible_projects')
        if accessible_projects is not None and not accessible_projects.filter(id=value).exists():
            raise serializers.ValidationError('项目不存在或无权限访问')
        return value

    def create(self, validated_data):
        validated_data['project_id'] = validated_data.pop('project_id')
        validated_data['created_by'] = self.context['request'].user
        return super().create(validated_data)


class ApiAssetSerializer(ProjectOwnedCreateSerializer):
    class Meta:
        model = ApiAsset
        fields = [
            'id', 'project', 'project_id', 'project_name', 'name', 'method', 'path',
            'service', 'description', 'status', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'project', 'created_at', 'updated_at']


class PageAssetSerializer(ProjectOwnedCreateSerializer):
    class Meta:
        model = PageAsset
        fields = [
            'id', 'project', 'project_id', 'project_name', 'name', 'platform', 'route',
            'element_name', 'selector', 'description', 'status', 'created_by_name',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'project', 'created_at', 'updated_at']


class AutomationScriptAssetSerializer(ProjectOwnedCreateSerializer):
    class Meta:
        model = AutomationScriptAsset
        fields = [
            'id', 'project', 'project_id', 'project_name', 'name', 'script_type', 'path',
            'repository', 'entrypoint', 'description', 'status', 'created_by_name',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'project', 'created_at', 'updated_at']


class DefectAssetSerializer(ProjectOwnedCreateSerializer):
    class Meta:
        model = DefectAsset
        fields = [
            'id', 'project', 'project_id', 'project_name', 'defect_key', 'title',
            'severity', 'status', 'external_url', 'description', 'created_by_name',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'project', 'created_at', 'updated_at']


class TestAssetLinkSerializer(serializers.ModelSerializer):
    asset_label = serializers.SerializerMethodField()
    asset_display_type = serializers.CharField(source='get_asset_type_display', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)

    class Meta:
        model = TestAssetLink
        fields = [
            'id', 'project', 'requirement', 'asset_type', 'asset_display_type',
            'api', 'page', 'testcase', 'automation_script', 'defect', 'report',
            'generated_case', 'business_requirement', 'asset_label', 'note',
            'created_by_name', 'created_at'
        ]
        read_only_fields = ['id', 'project', 'created_at']

    def get_asset_label(self, obj):
        return asset_label(obj)

    def validate(self, attrs):
        requirement = attrs.get('requirement') or self.instance.requirement
        asset_type = attrs.get('asset_type') or self.instance.asset_type
        field_name = {
            'api': 'api',
            'page': 'page',
            'testcase': 'testcase',
            'automation_script': 'automation_script',
            'defect': 'defect',
            'report': 'report',
            'generated_case': 'generated_case',
            'business_requirement': 'business_requirement',
        }[asset_type]
        if not attrs.get(field_name) and not getattr(self.instance, f'{field_name}_id', None):
            raise serializers.ValidationError({'asset_type': '请选择对应类型的资产'})

        accessible_projects = self.context.get('accessible_projects')
        if accessible_projects is not None and not accessible_projects.filter(id=requirement.project_id).exists():
            raise serializers.ValidationError({'requirement': '需求不存在或无权限访问'})

        target = attrs.get(field_name) or getattr(self.instance, field_name, None)
        target_project_id = getattr(target, 'project_id', None)
        if target_project_id and target_project_id != requirement.project_id:
            raise serializers.ValidationError({field_name: '资产必须属于同一个项目'})
        return attrs

    def create(self, validated_data):
        requirement = validated_data['requirement']
        validated_data['project'] = requirement.project
        validated_data['created_by'] = self.context['request'].user
        return super().create(validated_data)


class TestAssetRequirementSerializer(serializers.ModelSerializer):
    project_id = serializers.IntegerField(write_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    owner_name = serializers.CharField(source='owner.username', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    priority_display = serializers.CharField(source='get_priority_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    coverage_status = serializers.SerializerMethodField()
    asset_counts = serializers.SerializerMethodField()
    links = TestAssetLinkSerializer(many=True, read_only=True)

    class Meta:
        model = TestAssetRequirement
        fields = [
            'id', 'project', 'project_id', 'project_name', 'business_requirement',
            'requirement_key', 'title', 'module', 'description', 'acceptance_criteria',
            'priority', 'priority_display', 'status', 'status_display', 'source',
            'external_url', 'owner', 'owner_name', 'created_by_name',
            'coverage_status', 'asset_counts', 'links', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'project', 'created_at', 'updated_at']

    def get_coverage_status(self, obj):
        counts = self.get_asset_counts(obj)
        if counts['testcase'] > 0 and (counts['report'] > 0 or counts['automation_script'] > 0):
            return 'tested'
        if counts['testcase'] > 0 or counts['generated_case'] > 0:
            return 'designed'
        if counts['api'] > 0 or counts['page'] > 0:
            return 'analyzed'
        return 'uncovered'

    def get_asset_counts(self, obj):
        links = list(getattr(obj, '_prefetched_objects_cache', {}).get('links', obj.links.all()))
        counts = {
            'api': 0,
            'page': 0,
            'testcase': 0,
            'automation_script': 0,
            'defect': 0,
            'report': 0,
            'generated_case': 0,
            'business_requirement': 0,
        }
        for link in links:
            counts[link.asset_type] = counts.get(link.asset_type, 0) + 1
        return counts

    def validate_project_id(self, value):
        accessible_projects = self.context.get('accessible_projects')
        if accessible_projects is not None and not accessible_projects.filter(id=value).exists():
            raise serializers.ValidationError('项目不存在或无权限访问')
        return value

    def create(self, validated_data):
        validated_data['project_id'] = validated_data.pop('project_id')
        validated_data['created_by'] = self.context['request'].user
        if not validated_data.get('owner'):
            validated_data['owner'] = self.context['request'].user
        return super().create(validated_data)


class AssetOptionSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    label = serializers.CharField()
    subtitle = serializers.CharField(allow_blank=True)


def build_asset_options(projects, project_id=None):
    if project_id:
        projects = projects.filter(id=project_id)

    testcases = TestCase.objects.filter(project__in=projects).select_related('project')[:200]
    reports = TestReport.objects.filter(project__in=projects).select_related('project')[:200]
    apis = ApiAsset.objects.filter(project__in=projects).select_related('project')[:200]
    pages = PageAsset.objects.filter(project__in=projects).select_related('project')[:200]
    scripts = AutomationScriptAsset.objects.filter(project__in=projects).select_related('project')[:200]
    defects = DefectAsset.objects.filter(project__in=projects).select_related('project')[:200]
    business_requirements = BusinessRequirement.objects.filter(
        analysis__document__project__in=projects
    ).select_related('analysis__document__project')[:200]
    generated_cases = GeneratedTestCase.objects.filter(
        requirement__analysis__document__project__in=projects
    ).select_related('requirement__analysis__document__project')[:200]

    return {
        'api': [{'id': item.id, 'label': f'{item.method} {item.path}', 'subtitle': item.name} for item in apis],
        'page': [{'id': item.id, 'label': item.name, 'subtitle': item.route or item.element_name} for item in pages],
        'testcase': [{'id': item.id, 'label': item.title, 'subtitle': item.project.name} for item in testcases],
        'automation_script': [{'id': item.id, 'label': item.name, 'subtitle': item.path} for item in scripts],
        'defect': [{'id': item.id, 'label': f'{item.defect_key} {item.title}', 'subtitle': item.get_status_display()} for item in defects],
        'report': [{'id': item.id, 'label': item.name, 'subtitle': item.project.name} for item in reports],
        'business_requirement': [
            {'id': item.id, 'label': f'{item.requirement_id} {item.requirement_name}', 'subtitle': item.module}
            for item in business_requirements
        ],
        'generated_case': [
            {'id': item.id, 'label': f'{item.case_id} {item.title}', 'subtitle': item.get_status_display()}
            for item in generated_cases
        ],
    }


def requirement_dashboard(projects, project_id=None):
    requirements = TestAssetRequirement.objects.filter(project__in=projects)
    if project_id:
        requirements = requirements.filter(project_id=project_id)

    total = requirements.count()
    linked = requirements.filter(links__isnull=False).distinct().count()
    tested = requirements.filter(
        links__asset_type='testcase'
    ).filter(
        models.Q(links__asset_type='report') | models.Q(links__asset_type='automation_script')
    ).distinct().count()

    return {
        'total_requirements': total,
        'linked_requirements': linked,
        'tested_requirements': tested,
        'uncovered_requirements': max(total - linked, 0),
        'coverage_rate': round(linked / total * 100, 1) if total else 0,
        'tested_rate': round(tested / total * 100, 1) if total else 0,
    }
