# -*- coding: utf-8 -*-
"""APP测试执行管理视图"""
from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.filters import SearchFilter
from django.db.models import Q
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from django.http import FileResponse, Http404
from django.core import signing
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Q
import logging
import os
import shutil

from ..models import AppTestExecution
from ..models import AppProject
from ..access import is_platform_admin
from ..serializers import AppTestExecutionSerializer
from ..tasks import (
    get_app_notification_targets,
    select_app_webhook_bots,
    send_execution_update,
    send_manual_execution_completion_notification,
)
from ..report_urls import PUBLIC_REPORT_SALT
from .test_case_views import AppPagination

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


def _parse_notification_ids(request):
    values = request.data.get('notification_ids', [])
    if not isinstance(values, list) or not values:
        return None
    return list(dict.fromkeys(
        str(item or '').strip() for item in values if str(item or '').strip()
    ))


def _safe_remove_report_dir(report_path):
    if not report_path:
        return
    report_dir = os.path.abspath(report_path)
    media_root = os.path.abspath(settings.MEDIA_ROOT)
    if not report_dir.startswith(media_root + os.sep):
        logger.warning("跳过非媒体目录报告删除: %s", report_path)
        return
    if os.path.isdir(report_dir):
        shutil.rmtree(report_dir, ignore_errors=True)


def delete_app_execution(execution):
    """Delete an execution and its generated report files."""
    if execution.status in ('pending', 'running') and execution.task_id:
        try:
            from celery import current_app
            current_app.control.revoke(execution.task_id, terminate=True, signal='SIGTERM')
        except Exception as exc:
            logger.warning("撤销执行任务失败 execution_id=%s: %s", execution.id, exc)
    _safe_remove_report_dir(execution.report_path)
    execution.delete()


class AppTestExecutionViewSet(viewsets.ModelViewSet):
    """APP测试执行记录 ViewSet"""
    queryset = AppTestExecution.objects.all()
    serializer_class = AppTestExecutionSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter]
    filterset_fields = ['status', 'test_case', 'device']
    search_fields = ['test_case__name', 'device__name', 'device__device_id', 'user__username']
    pagination_class = AppPagination
    
    def get_queryset(self):
        queryset = super().get_queryset()
        if not is_platform_admin(self.request.user):
            queryset = queryset.filter(
                Q(user=self.request.user)
                | Q(test_case__project__owner=self.request.user)
                | Q(test_case__project__members=self.request.user)
                | Q(test_suite__project__owner=self.request.user)
                | Q(test_suite__project__members=self.request.user)
            ).distinct()

        # 支持 test_suite__isnull 过滤，用于区分单独执行和套件执行
        suite_isnull = self.request.query_params.get('test_suite__isnull')
        if suite_isnull is not None:
            queryset = queryset.filter(test_suite__isnull=(suite_isnull.lower() in ('true', '1')))

        # 支持按项目间接过滤（通过 test_case.project）
        project_id = self.request.query_params.get('project')
        if project_id:
            queryset = queryset.filter(test_case__project_id=project_id)

        search_value = (self.request.query_params.get('search') or '').strip()
        if not search_value:
            return queryset
        return queryset.filter(
            Q(test_case__name__icontains=search_value) |
            Q(device__name__icontains=search_value) |
            Q(device__device_id__icontains=search_value) |
            Q(user__username__icontains=search_value)
        )

    def perform_destroy(self, instance):
        delete_app_execution(instance)

    @action(detail=False, methods=['post'], url_path='batch-delete')
    def batch_delete(self, request):
        """批量删除执行记录，并清理对应 Allure 报告目录。"""
        ids = _parse_ids(request)
        if not ids:
            return Response({
                'success': False,
                'message': '请提供要删除的执行记录 ID 列表'
            }, status=status.HTTP_400_BAD_REQUEST)

        executions = list(self.get_queryset().filter(id__in=ids))
        found_ids = {execution.id for execution in executions}
        deleted = 0
        errors = []
        for execution in executions:
            try:
                delete_app_execution(execution)
                deleted += 1
            except Exception as exc:
                logger.error("批量删除执行记录失败 execution_id=%s: %s", execution.id, exc, exc_info=True)
                errors.append({'id': execution.id, 'message': str(exc)})

        missing_ids = [item for item in ids if item not in found_ids]
        return Response({
            'success': len(errors) == 0,
            'deleted': deleted,
            'missing_ids': missing_ids,
            'errors': errors,
            'message': f'已删除 {deleted} 条执行记录',
        })

    @action(detail=False, methods=['get'], url_path='notification-targets')
    def notification_targets(self, request):
        """列出第三方集成中可用于报告发送的启用通知，不返回密钥。"""
        return Response({'targets': get_app_notification_targets()})

    @action(detail=False, methods=['post'], url_path='batch-notify')
    def batch_notify(self, request):
        """将选中的 APP 用例报告发送到选中的第三方集成。"""
        ids = _parse_ids(request)
        notification_ids = _parse_notification_ids(request)
        if not ids or not notification_ids:
            return Response(
                {'success': False, 'message': '请选择报告和通知'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        bots = select_app_webhook_bots(notification_ids)
        if not bots:
            return Response(
                {'success': False, 'message': '选择的通知不存在或已停用'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        executions = list(self.get_queryset().filter(id__in=ids).select_related(
            'test_case', 'device', 'user',
        ))
        execution_map = {execution.id: execution for execution in executions}
        sent = 0
        failed = 0
        results = []
        for execution_id in ids:
            execution = execution_map.get(execution_id)
            if execution is None or not execution.report_path:
                failed += 1
                results.append({
                    'report_id': execution_id,
                    'success': False,
                    'reason': '报告不存在',
                })
                continue
            target_results = send_manual_execution_completion_notification(
                execution,
                bots=bots,
                allow_stopped=True,
            ) or []
            success_count = sum(1 for item in target_results if item.get('success'))
            failure_count = len(target_results) - success_count
            sent += success_count
            failed += failure_count
            results.append({
                'report_id': execution_id,
                'success': bool(target_results) and failure_count == 0,
                'targets': target_results,
            })

        found_notification_ids = {str(bot.get('id') or '') for bot in bots}
        return Response({
            'success': failed == 0,
            'report_count': len(ids),
            'notification_count': len(bots),
            'sent': sent,
            'failed': failed,
            'missing_notification_ids': [
                item for item in notification_ids
                if item not in found_notification_ids
            ],
            'results': results,
        })
    
    @action(detail=False, methods=['get'])
    def ws_status(self, request):
        """检查 WebSocket 是否可用"""
        try:
            import daphne
            from channels.layers import get_channel_layer
            channel_layer = get_channel_layer()
            ws_available = channel_layer is not None and not isinstance(
                channel_layer, type(None)
            )
            # 检查是否通过 ASGI 服务器运行（非 runserver）
            server_type = request.META.get('SERVER_SOFTWARE', '')
            is_asgi = 'daphne' in server_type.lower() or request.META.get('asgi', False)
            return Response({'websocket': ws_available and is_asgi})
        except (ImportError, Exception):
            return Response({'websocket': False})

    @action(detail=True, methods=['post'])
    def stop(self, request, pk=None):
        """停止执行"""
        execution = self.get_object()
        
        if execution.status not in ['pending', 'running']:
            return Response({
                'success': False,
                'message': '只能停止待执行或执行中的任务'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            # 先写入停止状态。运行中的 pytest 子进程会持续读取该状态并自行退出；
            # 不能强杀 Celery worker，否则 pytest 会变成孤儿进程继续操作设备。
            execution.status = 'stopped'
            execution.finished_at = timezone.now()
            if execution.started_at:
                execution.duration = (execution.finished_at - execution.started_at).total_seconds()
            execution.save(update_fields=['status', 'finished_at', 'duration', 'updated_at'])

            # revoke 用于阻止尚未开始的任务；运行中的任务通过上面的状态协作取消。
            if execution.task_id:
                try:
                    from celery import current_app
                    current_app.control.revoke(execution.task_id, terminate=False)
                    logger.info(f"Celery任务已撤销: task_id={execution.task_id}")
                except Exception as revoke_error:
                    logger.warning(
                        f"撤销Celery任务失败，执行器仍会按停止状态退出: "
                        f"task_id={execution.task_id}, error={revoke_error}"
                    )

            send_execution_update(
                execution.id,
                status='stopped',
                progress=execution.progress,
                message='任务已停止',
                report_path=execution.report_path,
                finished_at=execution.finished_at
            )
            
            return Response({
                'success': True,
                'message': '任务已停止'
            })
        except Exception as e:
            logger.error(f"停止任务失败: {str(e)}")
            return Response({
                'success': False,
                'message': '停止任务失败，请稍后重试'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def serve_report_file(request, execution_id, file_path=''):
    """提供 Allure 报告文件访问"""
    return _serve_report_file(execution_id, file_path, request_user=request.user)


@csrf_exempt
def serve_public_report_file(request, execution_id, token, file_path=''):
    """通过不可猜测的签名链接公开只读 Allure 报告，供飞书等外部通知访问。"""
    try:
        signed_execution_id = signing.TimestampSigner(
            salt=PUBLIC_REPORT_SALT
        ).unsign(
            token,
            max_age=max(
                int(getattr(settings, 'LONG_TERM_VALIDITY_SECONDS', 3650 * 24 * 60 * 60)),
                int(getattr(settings, 'PUBLIC_REPORT_MAX_AGE_SECONDS', 3650 * 24 * 60 * 60)),
            ),
        )
    except signing.BadSignature:
        raise Http404("报告链接无效")
    if str(signed_execution_id) != str(execution_id):
        raise Http404("报告链接无效")

    response = _serve_report_file(execution_id, file_path)
    response['X-Robots-Tag'] = 'noindex, nofollow'
    return response


def _serve_report_file(execution_id, file_path='', request_user=None):
    try:
        execution = AppTestExecution.objects.get(id=execution_id)
        if request_user is not None:
            project_id = None
            if execution.test_case_id and execution.test_case:
                project_id = execution.test_case.project_id
            elif execution.test_suite_id and execution.test_suite:
                project_id = execution.test_suite.project_id
            if not (
                request_user.is_staff
                or request_user.is_superuser
                or AppProject.objects.filter(
                    Q(owner=request_user) | Q(members=request_user),
                    pk=project_id,
                ).exists()
                or execution.user_id == request_user.pk
            ):
                raise Http404("报告不存在")
        if not execution.report_path:
            raise Http404("报告路径不存在")

        if not file_path:
            file_path = 'index.html'

        # 安全修复：验证 file_path 不包含路径遍历字符
        if '..' in file_path or file_path.startswith('/') or file_path.startswith('\\'):
            raise Http404("无效的文件路径")

        report_dir = execution.report_path
        full_path = os.path.join(report_dir, file_path)

        report_dir_abs = os.path.abspath(report_dir)
        full_path_abs = os.path.abspath(full_path)
        if not full_path_abs.startswith(report_dir_abs + os.sep) and full_path_abs != report_dir_abs:
            raise Http404("无效的文件路径")

        if not os.path.exists(full_path_abs) or not os.path.isfile(full_path_abs):
            raise Http404("文件不存在")

        return FileResponse(open(full_path_abs, 'rb'), content_type=_get_content_type(file_path))
    except AppTestExecution.DoesNotExist:
        raise Http404("执行记录不存在")


def _get_content_type(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    content_types = {
        '.html': 'text/html',
        '.js': 'application/javascript',
        '.css': 'text/css',
        '.json': 'application/json',
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.gif': 'image/gif',
        '.svg': 'image/svg+xml',
        '.ico': 'image/x-icon',
        '.woff': 'font/woff',
        '.woff2': 'font/woff2',
        '.ttf': 'font/ttf',
        '.eot': 'application/vnd.ms-fontobject',
        '.txt': 'text/plain',
    }
    return content_types.get(ext, 'application/octet-stream')
