from django.db import models
from rest_framework import permissions, status, views, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.projects.models import Project

from .models import PerformanceResult, PerformanceRule
from .serializers import (
    ExporterTargetDeleteSerializer,
    ExporterTargetInputSerializer,
    PerformanceMetricInputSerializer,
    PerformanceResultSerializer,
    PerformanceRuleSerializer,
    TestedDatabaseConfigSerializer,
)
from .exporter_targets import (
    list_exporter_targets,
    reload_prometheus,
    remove_exporter_target,
    upsert_exporter_target,
)
from .database_diagnostics import load_database_diagnostics
from .tested_database_config import (
    load_tested_database_config,
    save_tested_database_config,
    ui_tested_database_config,
)


def get_accessible_projects(user):
    return Project.objects.filter(
        models.Q(owner=user) | models.Q(members=user)
    ).distinct()


class NoStoreMixin:
    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store, no-cache, must-revalidate, proxy-revalidate'
        response['Pragma'] = 'no-cache'
        response['Expires'] = '0'
        return response


class NoStoreAPIView(NoStoreMixin, views.APIView):
    pass


class PerformanceRuleViewSet(NoStoreMixin, viewsets.ModelViewSet):
    serializer_class = PerformanceRuleSerializer
    permission_classes = [permissions.IsAuthenticated]
    filterset_fields = ['project']

    def get_queryset(self):
        return PerformanceRule.objects.filter(
            project__in=get_accessible_projects(self.request.user)
        ).select_related('project')

    def _ensure_project_manage_access(self, project):
        if not project:
            raise PermissionDenied('必须指定项目')
        if not get_accessible_projects(self.request.user).filter(pk=project.pk).exists():
            raise PermissionDenied('无权限访问该项目')

    def perform_create(self, serializer):
        self._ensure_project_manage_access(serializer.validated_data.get('project'))
        serializer.save()

    def perform_update(self, serializer):
        self._ensure_project_manage_access(
            serializer.validated_data.get('project') or serializer.instance.project
        )
        serializer.save()


class PerformanceResultViewSet(NoStoreMixin, viewsets.ReadOnlyModelViewSet):
    serializer_class = PerformanceResultSerializer
    permission_classes = [permissions.IsAuthenticated]
    filterset_fields = ['project', 'report_id', 'overall_status']

    def get_queryset(self):
        return PerformanceResult.objects.filter(
            project__in=get_accessible_projects(self.request.user)
        ).select_related('project', 'rule')

    @action(detail=False, methods=['post'])
    def evaluate(self, request):
        serializer = PerformanceMetricInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        project_id = serializer.validated_data['project_id']
        if not get_accessible_projects(request.user).filter(id=project_id).exists():
            return Response({'error': '无权限访问该项目'}, status=status.HTTP_403_FORBIDDEN)

        try:
            result = serializer.save()
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(PerformanceResultSerializer(result).data, status=status.HTTP_201_CREATED)


class ExporterTargetView(NoStoreAPIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(list_exporter_targets())

    def post(self, request):
        serializer = ExporterTargetInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        labels = {
            'role': 'target',
            'app': data['app'],
            'app_url': data['app_url'],
        }
        if data.get('database_name'):
            labels['database'] = data['database_name']

        if data.get('process_target'):
            upsert_exporter_target('process', data['process_target'], labels)
        if data.get('jmx_target'):
            upsert_exporter_target('jmx', data['jmx_target'], labels)
        if data.get('mysql_target'):
            upsert_exporter_target('mysql', data['mysql_target'], {
                **labels,
                'role': 'database',
                'db_access': 'readonly_metrics',
            })

        payload = list_exporter_targets()
        payload['reload'] = reload_prometheus() if data.get('reload') else {
            'status': 'skipped',
            'message': 'reload disabled by request',
        }
        return Response(payload, status=status.HTTP_200_OK)

    def delete(self, request):
        serializer = ExporterTargetDeleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        removed = remove_exporter_target(data['target_type'], data['target'])
        payload = list_exporter_targets()
        payload['removed'] = removed
        payload['reload'] = reload_prometheus() if removed else {
            'status': 'skipped',
            'message': 'target not found',
        }
        return Response(payload, status=status.HTTP_200_OK)


class PrometheusReloadView(NoStoreAPIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        return Response({'reload': reload_prometheus(recreate_mysql_exporter=True)}, status=status.HTTP_200_OK)


class TestedDatabaseConfigView(NoStoreAPIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response({
            'config': ui_tested_database_config(load_tested_database_config()),
            'access_mode': 'readonly_metrics',
            'restart_required': False,
        }, status=status.HTTP_200_OK)

    def post(self, request):
        serializer = TestedDatabaseConfigSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        save_tested_database_config(serializer.validated_data)
        return Response({
            'config': ui_tested_database_config(load_tested_database_config()),
            'access_mode': 'readonly_metrics',
            'restart_required': True,
            'restart_hint': '点击 Exporter配置 里的“重载 Prometheus”后，后台会自动重建 mysql-exporter 和 prometheus',
        }, status=status.HTTP_200_OK)


class DatabaseDiagnosticsView(NoStoreAPIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            return Response(load_database_diagnostics(), status=status.HTTP_200_OK)
        except Exception as exc:
            return Response(
                {'error': str(exc), 'access_mode': 'readonly_select_only'},
                status=status.HTTP_400_BAD_REQUEST,
            )
