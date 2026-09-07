# -*- coding: utf-8 -*-
"""APP自动化定时任务视图"""
import json
import logging

from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone

from .test_case_views import AppPagination
from ..models import (
    AppScheduledTask, AppNotificationLog,
    AppTestSuite, AppTestCase, AppDevice,
)
from ..access import accessible_project_assets
from ..serializers import (
    AppScheduledTaskSerializer,
    AppNotificationLogSerializer,
)

logger = logging.getLogger(__name__)


def _parse_ids(request):
    ids = request.data.get('ids', [])
    if not isinstance(ids, list) or not ids:
        return None
    parsed_ids = []
    for item in ids:
        try:
            parsed_ids.append(int(item))
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(parsed_ids))


class AppScheduledTaskViewSet(viewsets.ModelViewSet):
    """APP定时任务视图集"""
    queryset = AppScheduledTask.objects.all()
    serializer_class = AppScheduledTaskSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = AppPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['task_type', 'status', 'trigger_type', 'project']
    search_fields = ['name', 'description']
    ordering_fields = ['created_at', 'next_run_time', 'last_run_time']
    ordering = ['-created_at']

    def get_queryset(self):
        return accessible_project_assets(
            super().get_queryset(),
            self.request.user,
            project_lookup='project',
            unscoped_owner_lookup='created_by',
        )

    @action(detail=True, methods=['post'])
    def pause(self, request, pk=None):
        task = self.get_object()
        task.status = 'PAUSED'
        task.save(update_fields=['status'])
        return Response({'success': True, 'message': '任务已暂停'})

    @action(detail=True, methods=['post'])
    def resume(self, request, pk=None):
        task = self.get_object()
        task.status = 'ACTIVE'
        task.next_run_time = task.calculate_next_run()
        task.save(update_fields=['status', 'next_run_time'])
        return Response({'success': True, 'message': '任务已恢复'})

    @action(detail=True, methods=['post'])
    def run_now(self, request, pk=None):
        """立即运行任务"""
        task = self.get_object()

        try:
            if not task.device:
                return Response({'success': False, 'message': '该任务未配置执行设备'},
                                status=status.HTTP_400_BAD_REQUEST)

            device = task.device
            if device.status == 'locked' and device.locked_by != request.user:
                return Response({'success': False, 'message': '设备已被其他用户锁定'},
                                status=status.HTTP_400_BAD_REQUEST)

            # 更新统计
            task.last_run_time = timezone.now()
            task.total_runs += 1
            task.next_run_time = task.calculate_next_run()
            task.save()

            package_name = task.app_package.package_name if task.app_package else ''

            if task.task_type == 'TEST_SUITE':
                if not task.test_suite:
                    return Response({'success': False, 'message': '该任务未配置测试套件'},
                                    status=status.HTTP_400_BAD_REQUEST)

                suite_cases = task.test_suite.suite_cases.select_related('test_case').all()
                if not suite_cases.exists():
                    return Response({'success': False, 'message': '测试套件没有用例'},
                                    status=status.HTTP_400_BAD_REQUEST)

                # 创建执行记录并调用 Celery
                from ..models import AppTestExecution
                from ..tasks import execute_app_suite_task

                executions = []
                for sc in suite_cases:
                    execution = AppTestExecution.objects.create(
                        test_case=sc.test_case,
                        test_suite=task.test_suite,
                        device=device,
                        user=request.user,
                        status='pending'
                    )
                    executions.append(execution)

                task.test_suite.execution_status = 'running'
                task.test_suite.save(update_fields=['execution_status'])

                celery_task = execute_app_suite_task.delay(
                    suite_id=task.test_suite.id,
                    execution_ids=[e.id for e in executions],
                    package_name=package_name,
                    scheduled_task_id=task.id,
                )
                AppTestExecution.objects.filter(
                    id__in=[e.id for e in executions]
                ).update(task_id=celery_task.id)

                return Response({
                    'success': True,
                    'message': f'测试套件开始执行，共 {len(executions)} 个用例',
                    'data': {'task_id': celery_task.id, 'test_case_count': len(executions)}
                })

            elif task.task_type == 'TEST_CASE':
                if not task.test_case:
                    return Response({'success': False, 'message': '该任务未配置测试用例'},
                                    status=status.HTTP_400_BAD_REQUEST)

                from ..models import AppTestExecution
                from ..tasks import execute_app_test_task

                execution = AppTestExecution.objects.create(
                    test_case=task.test_case,
                    device=device,
                    user=request.user,
                    status='pending'
                )
                celery_task = execute_app_test_task.delay(
                    execution.id,
                    package_name=package_name,
                    scheduled_task_id=task.id,
                )
                execution.task_id = celery_task.id
                execution.save(update_fields=['task_id'])

                return Response({
                    'success': True,
                    'message': '测试用例开始执行',
                    'data': {'task_id': celery_task.id}
                })

            return Response({'success': False, 'message': '不支持的任务类型'},
                            status=status.HTTP_400_BAD_REQUEST)

        except Exception as e:
            logger.error(f'执行定时任务失败: {str(e)}', exc_info=True)
            return Response({'success': False, 'message': f'执行失败: {str(e)}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AppNotificationLogViewSet(viewsets.ReadOnlyModelViewSet):
    """APP通知日志视图集（只读）"""
    queryset = AppNotificationLog.objects.all()
    serializer_class = AppNotificationLogSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = AppPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'notification_type']
    search_fields = ['task_name', 'notification_content']
    ordering_fields = ['created_at', 'sent_at']
    ordering = ['-created_at']

    @action(detail=False, methods=['post'], url_path='batch-delete')
    def batch_delete(self, request):
        """批量删除通知日志。"""
        ids = _parse_ids(request)
        if not ids:
            return Response({
                'success': False,
                'message': '请提供要删除的通知日志 ID 列表'
            }, status=status.HTTP_400_BAD_REQUEST)

        queryset = self.get_queryset().filter(id__in=ids)
        found_ids = set(queryset.values_list('id', flat=True))
        deleted, _ = queryset.delete()
        missing_ids = [item for item in ids if item not in found_ids]
        return Response({
            'success': True,
            'deleted': deleted,
            'missing_ids': missing_ids,
            'message': f'已删除 {deleted} 条通知日志',
        })

    @action(detail=True, methods=['post'])
    def retry(self, request, pk=None):
        log = self.get_object()
        if log.status == 'failed':
            log.retry_count += 1
            log.is_retried = True
            log.save(update_fields=['retry_count', 'is_retried'])
            return Response({'success': True, 'message': '通知已加入重试队列'})
        return Response({'success': False, 'message': '只能重试失败的通知'},
                        status=status.HTTP_400_BAD_REQUEST)
