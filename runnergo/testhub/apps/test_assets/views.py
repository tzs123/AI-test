from django.db import models
from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response

from apps.projects.models import Project
from apps.requirement_analysis.models import BusinessRequirement

from .models import (
    ApiAsset,
    AutomationScriptAsset,
    DefectAsset,
    PageAsset,
    TestAssetLink,
    TestAssetRequirement,
)
from .serializers import (
    ApiAssetSerializer,
    AutomationScriptAssetSerializer,
    DefectAssetSerializer,
    PageAssetSerializer,
    TestAssetLinkSerializer,
    TestAssetRequirementSerializer,
    build_asset_options,
    requirement_dashboard,
)


def get_accessible_projects(user):
    return Project.objects.filter(
        models.Q(owner=user) | models.Q(members=user)
    ).distinct()


class AccessibleProjectMixin:
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]

    def get_accessible_projects(self):
        return get_accessible_projects(self.request.user)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['accessible_projects'] = self.get_accessible_projects()
        return context


class TestAssetRequirementViewSet(AccessibleProjectMixin, viewsets.ModelViewSet):
    serializer_class = TestAssetRequirementSerializer
    search_fields = ['requirement_key', 'title', 'module', 'description']
    ordering_fields = ['created_at', 'updated_at', 'priority', 'requirement_key']
    ordering = ['-updated_at']

    def get_queryset(self):
        queryset = TestAssetRequirement.objects.filter(
            project__in=self.get_accessible_projects()
        ).select_related(
            'project', 'owner', 'created_by', 'business_requirement'
        ).prefetch_related(
            'links__api', 'links__page', 'links__testcase', 'links__automation_script',
            'links__defect', 'links__report', 'links__generated_case',
            'links__business_requirement', 'links__created_by'
        ).distinct()
        project_id = self.request.query_params.get('project')
        status_value = self.request.query_params.get('status')
        priority = self.request.query_params.get('priority')
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        if status_value:
            queryset = queryset.filter(status=status_value)
        if priority:
            queryset = queryset.filter(priority=priority)
        return queryset

    @action(detail=False, methods=['post'], url_path='sync-ai')
    def sync_ai_requirements(self, request):
        project_id = request.data.get('project_id') or request.query_params.get('project')
        accessible_projects = self.get_accessible_projects()
        if project_id:
            accessible_projects = accessible_projects.filter(id=project_id)

        business_requirements = BusinessRequirement.objects.filter(
            analysis__document__project__in=accessible_projects
        ).select_related('analysis__document__project')

        created_count = 0
        for item in business_requirements:
            project = item.analysis.document.project
            if not project:
                continue
            _, created = TestAssetRequirement.objects.get_or_create(
                project=project,
                requirement_key=item.requirement_id,
                defaults={
                    'business_requirement': item,
                    'title': item.requirement_name,
                    'module': item.module,
                    'description': item.description,
                    'acceptance_criteria': item.acceptance_criteria,
                    'priority': item.requirement_level if item.requirement_level in ['low', 'medium', 'high'] else 'medium',
                    'status': 'approved',
                    'source': 'ai',
                    'owner': request.user,
                    'created_by': request.user,
                }
            )
            if created:
                created_count += 1

        return Response({'created': created_count, 'total_ai_requirements': business_requirements.count()})


class ApiAssetViewSet(AccessibleProjectMixin, viewsets.ModelViewSet):
    serializer_class = ApiAssetSerializer
    search_fields = ['name', 'method', 'path', 'service', 'description']
    ordering_fields = ['created_at', 'updated_at', 'method', 'path']

    def get_queryset(self):
        queryset = ApiAsset.objects.filter(project__in=self.get_accessible_projects()).select_related('project', 'created_by')
        project_id = self.request.query_params.get('project')
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        return queryset


class PageAssetViewSet(AccessibleProjectMixin, viewsets.ModelViewSet):
    serializer_class = PageAssetSerializer
    search_fields = ['name', 'route', 'element_name', 'selector', 'description']
    ordering_fields = ['created_at', 'updated_at', 'name', 'platform']

    def get_queryset(self):
        queryset = PageAsset.objects.filter(project__in=self.get_accessible_projects()).select_related('project', 'created_by')
        project_id = self.request.query_params.get('project')
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        return queryset


class AutomationScriptAssetViewSet(AccessibleProjectMixin, viewsets.ModelViewSet):
    serializer_class = AutomationScriptAssetSerializer
    search_fields = ['name', 'path', 'repository', 'entrypoint', 'description']
    ordering_fields = ['created_at', 'updated_at', 'name', 'script_type']

    def get_queryset(self):
        queryset = AutomationScriptAsset.objects.filter(project__in=self.get_accessible_projects()).select_related('project', 'created_by')
        project_id = self.request.query_params.get('project')
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        return queryset


class DefectAssetViewSet(AccessibleProjectMixin, viewsets.ModelViewSet):
    serializer_class = DefectAssetSerializer
    search_fields = ['defect_key', 'title', 'description']
    ordering_fields = ['created_at', 'updated_at', 'severity', 'status']

    def get_queryset(self):
        queryset = DefectAsset.objects.filter(project__in=self.get_accessible_projects()).select_related('project', 'created_by')
        project_id = self.request.query_params.get('project')
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        return queryset


class TestAssetLinkViewSet(AccessibleProjectMixin, viewsets.ModelViewSet):
    serializer_class = TestAssetLinkSerializer
    search_fields = ['note']
    ordering_fields = ['created_at', 'asset_type']

    def get_queryset(self):
        queryset = TestAssetLink.objects.filter(
            project__in=self.get_accessible_projects()
        ).select_related(
            'project', 'requirement', 'api', 'page', 'testcase', 'automation_script',
            'defect', 'report', 'generated_case', 'business_requirement', 'created_by'
        )
        requirement_id = self.request.query_params.get('requirement')
        project_id = self.request.query_params.get('project')
        if requirement_id:
            queryset = queryset.filter(requirement_id=requirement_id)
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        return queryset


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def dashboard(request):
    project_id = request.query_params.get('project')
    return Response(requirement_dashboard(get_accessible_projects(request.user), project_id))


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def options(request):
    project_id = request.query_params.get('project')
    return Response(build_asset_options(get_accessible_projects(request.user), project_id))
