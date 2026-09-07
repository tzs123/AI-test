# -*- coding: utf-8 -*-
"""APP测试套件管理视图"""
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.filters import SearchFilter
from django_filters.rest_framework import DjangoFilterBackend
import logging

from ..models import AppTestSuite, AppTestSuiteCase, AppTestCase, AppDevice, AppTestExecution
from ..access import accessible_project_assets
from ..tasks import select_app_webhook_bots, send_manual_suite_completion_notification
from .test_case_views import AppPagination
from ..serializers import (
    AppTestSuiteSerializer,
    AppTestSuiteCreateSerializer,
    AppTestSuiteUpdateSerializer,
    AppTestSuiteCaseSerializer,
    AppTestExecutionSerializer,
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


class AppTestSuiteViewSet(viewsets.ModelViewSet):
    """APP测试套件 ViewSet"""
    queryset = AppTestSuite.objects.all()
    permission_classes = [IsAuthenticated]
    pagination_class = AppPagination
    filter_backends = [DjangoFilterBackend, SearchFilter]
    filterset_fields = ['project']
    search_fields = ['name', 'description']

    def get_queryset(self):
        return accessible_project_assets(
            super().get_queryset(),
            self.request.user,
            project_lookup='project',
            unscoped_owner_lookup='created_by',
        )

    def get_serializer_class(self):
        if self.action == 'create':
            return AppTestSuiteCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return AppTestSuiteUpdateSerializer
        return AppTestSuiteSerializer

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=False, methods=['post'], url_path='batch-delete')
    def batch_delete(self, request):
        """批量删除测试套件。"""
        ids = _parse_ids(request)
        if not ids:
            return Response({
                'success': False,
                'message': '请提供要删除的测试套件 ID 列表'
            }, status=status.HTTP_400_BAD_REQUEST)

        suites = list(self.get_queryset().filter(id__in=ids))
        found_ids = {suite.id for suite in suites}
        deleted = 0
        errors = []
        for suite in suites:
            try:
                suite.delete()
                deleted += 1
            except Exception as exc:
                logger.error("批量删除测试套件失败 suite_id=%s: %s", suite.id, exc, exc_info=True)
                errors.append({'id': suite.id, 'message': str(exc)})

        missing_ids = [item for item in ids if item not in found_ids]
        return Response({
            'success': len(errors) == 0,
            'deleted': deleted,
            'missing_ids': missing_ids,
            'errors': errors,
            'message': f'已删除 {deleted} 个测试套件',
        })

    @action(detail=False, methods=['post'], url_path='batch-notify')
    def batch_notify(self, request):
        """将选中的 APP 套件报告发送到选中的第三方集成。"""
        ids = _parse_ids(request)
        notification_ids = request.data.get('notification_ids', [])
        if not isinstance(notification_ids, list):
            notification_ids = []
        notification_ids = list(dict.fromkeys(
            str(item or '').strip()
            for item in notification_ids
            if str(item or '').strip()
        ))
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

        suites = list(self.get_queryset().filter(id__in=ids))
        suite_map = {suite.id: suite for suite in suites}
        sent = 0
        failed = 0
        results = []
        for suite_id in ids:
            suite = suite_map.get(suite_id)
            if suite is None or not suite.last_run_at:
                failed += 1
                results.append({
                    'report_id': suite_id,
                    'success': False,
                    'reason': '套件报告不存在',
                })
                continue
            execution_limit = max(1, suite.test_case_count)
            executions = list(
                AppTestExecution.objects.filter(test_suite=suite)
                .select_related('test_case')
                .order_by('-created_at')[:execution_limit]
            )
            if not executions:
                failed += 1
                results.append({
                    'report_id': suite_id,
                    'success': False,
                    'reason': '套件执行报告不存在',
                })
                continue
            executions.reverse()
            target_results = send_manual_suite_completion_notification(
                suite,
                executions,
                suite.passed_count or 0,
                suite.failed_count or 0,
                bots=bots,
                allow_stopped=True,
            ) or []
            success_count = sum(1 for item in target_results if item.get('success'))
            failure_count = len(target_results) - success_count
            sent += success_count
            failed += failure_count
            results.append({
                'report_id': suite_id,
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

    # ---------- 用例管理 ----------

    @action(detail=True, methods=['get'])
    def test_cases(self, request, pk=None):
        """获取套件中的所有用例（按顺序）"""
        suite = self.get_object()
        cases = suite.suite_cases.select_related('test_case', 'test_case__app_package').all()
        serializer = AppTestSuiteCaseSerializer(cases, many=True)
        return Response({'success': True, 'data': serializer.data})

    @action(detail=True, methods=['post'])
    def add_test_case(self, request, pk=None):
        """向套件添加用例"""
        suite = self.get_object()
        test_case_id = request.data.get('test_case_id')
        order = request.data.get('order')

        if not test_case_id:
            return Response({'success': False, 'message': '请提供 test_case_id'},
                            status=status.HTTP_400_BAD_REQUEST)

        test_case = AppTestCase.objects.filter(
            pk=test_case_id,
            project_id=suite.project_id,
        ).first()
        if test_case is None:
            return Response(
                {'success': False, 'message': '测试用例不存在或不属于当前项目'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 默认排在最后
        if order is None:
            max_order = suite.suite_cases.order_by('-order').values_list('order', flat=True).first()
            order = (max_order or 0) + 1

        try:
            sc = AppTestSuiteCase.objects.create(
                test_suite=suite,
                test_case=test_case,
                order=order
            )
            serializer = AppTestSuiteCaseSerializer(sc)
            return Response({'success': True, 'data': serializer.data},
                            status=status.HTTP_201_CREATED)
        except Exception as e:
            return Response({'success': False, 'message': str(e)},
                            status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def add_test_cases(self, request, pk=None):
        """批量添加用例到套件"""
        suite = self.get_object()
        test_case_ids = request.data.get('test_case_ids', [])

        if not test_case_ids:
            return Response({'success': False, 'message': '请提供 test_case_ids'},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            requested_ids = list(dict.fromkeys(int(item) for item in test_case_ids))
        except (TypeError, ValueError):
            return Response(
                {'success': False, 'message': 'test_case_ids 必须是整数数组'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        allowed_ids = set(
            AppTestCase.objects.filter(
                id__in=requested_ids,
                project_id=suite.project_id,
            ).values_list('id', flat=True)
        )
        invalid_ids = sorted(set(requested_ids) - allowed_ids)
        if invalid_ids:
            return Response(
                {
                    'success': False,
                    'message': f'用例不存在或不属于当前项目: {invalid_ids}',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        max_order = suite.suite_cases.order_by('-order').values_list('order', flat=True).first()
        current_order = (max_order or 0) + 1

        # 排除已存在的
        existing_ids = set(suite.suite_cases.values_list('test_case_id', flat=True))
        added = 0
        for tc_id in requested_ids:
            if tc_id not in existing_ids:
                AppTestSuiteCase.objects.create(
                    test_suite=suite,
                    test_case_id=tc_id,
                    order=current_order
                )
                current_order += 1
                added += 1

        return Response({
            'success': True,
            'message': f'成功添加 {added} 个用例',
            'added': added
        })

    @action(detail=True, methods=['post'])
    def remove_test_case(self, request, pk=None):
        """从套件移除用例"""
        suite = self.get_object()
        test_case_id = request.data.get('test_case_id')

        try:
            sc = AppTestSuiteCase.objects.get(
                test_suite=suite, test_case_id=test_case_id
            )
            sc.delete()
            return Response({'success': True, 'message': '已移除'})
        except AppTestSuiteCase.DoesNotExist:
            return Response({'success': False, 'message': '用例不在该套件中'},
                            status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=['post'])
    def update_test_case_order(self, request, pk=None):
        """更新套件中用例的顺序"""
        suite = self.get_object()
        test_case_orders = request.data.get('test_case_orders', [])

        try:
            for item in test_case_orders:
                AppTestSuiteCase.objects.filter(
                    test_suite=suite,
                    test_case_id=item['test_case_id']
                ).update(order=item['order'])
            return Response({'success': True, 'message': '顺序更新成功'})
        except Exception as e:
            return Response({'success': False, 'message': str(e)},
                            status=status.HTTP_400_BAD_REQUEST)

    # ---------- 套件执行 ----------

    @action(detail=True, methods=['post'])
    def run(self, request, pk=None):
        """执行测试套件（顺序执行所有用例）"""
        suite = self.get_object()
        device_id = request.data.get('device_id')
        package_name = request.data.get('package_name')

        if not device_id:
            return Response({'success': False, 'message': '请选择执行设备'},
                            status=status.HTTP_400_BAD_REQUEST)

        # 检查套件是否包含用例
        suite_cases = suite.suite_cases.select_related('test_case').all()
        if not suite_cases.exists():
            return Response({'success': False, 'message': '该套件未包含任何测试用例'},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            device = AppDevice.objects.get(device_id=device_id)
            if device.status == 'locked' and device.locked_by != request.user:
                return Response({'success': False, 'message': '设备已被其他用户锁定'},
                                status=status.HTTP_400_BAD_REQUEST)

            # 为每个用例创建执行记录
            executions = []
            for sc in suite_cases:
                execution = AppTestExecution.objects.create(
                    test_case=sc.test_case,
                    test_suite=suite,
                    device=device,
                    user=request.user,
                    status='pending'
                )
                executions.append(execution)

            # 更新套件状态
            suite.execution_status = 'running'
            suite.save(update_fields=['execution_status'])

            # 触发 Celery 任务，顺序执行
            from ..tasks import execute_app_suite_task
            execution_ids = [e.id for e in executions]
            task = execute_app_suite_task.delay(
                suite_id=suite.id,
                execution_ids=execution_ids,
                package_name=package_name
            )

            # 同一批套件执行共享 task_id，停止任一当前记录时都能识别并撤销该任务。
            if executions:
                AppTestExecution.objects.filter(
                    id__in=execution_ids
                ).update(task_id=task.id)

            logger.info(f"测试套件已提交执行: suite={suite.name}, "
                        f"cases={len(executions)}, task_id={task.id}")

            return Response({
                'success': True,
                'message': f'测试套件已提交执行，共 {len(executions)} 个用例',
                'data': {
                    'suite_id': suite.id,
                    'task_id': task.id,
                    'execution_ids': execution_ids,
                    'test_case_count': len(executions),
                }
            })

        except AppDevice.DoesNotExist:
            return Response({'success': False, 'message': '设备不存在'},
                            status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"执行套件失败: {str(e)}", exc_info=True)
            suite.execution_status = 'failed'
            suite.save(update_fields=['execution_status'])
            return Response({'success': False, 'message': f'执行失败: {str(e)}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['get'])
    def executions(self, request, pk=None):
        """获取套件的执行历史"""
        suite = self.get_object()
        execs = AppTestExecution.objects.filter(test_suite=suite).order_by('-created_at')[:50]
        serializer = AppTestExecutionSerializer(execs, many=True)
        return Response({'success': True, 'data': serializer.data})
