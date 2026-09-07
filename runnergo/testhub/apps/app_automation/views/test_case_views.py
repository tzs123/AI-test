# -*- coding: utf-8 -*-
"""APP测试用例管理视图"""
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination
from rest_framework.filters import SearchFilter
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import Q
import logging

from ..models import AppPackage, AppTestCase, AppDevice, AppTestExecution
from ..access import accessible_project_assets, is_platform_admin
from ..serializers import AppPackageSerializer, AppTestCaseSerializer, AppTestExecutionSerializer
from apps.core.models import RuntimeDataSet, RuntimeDataTemplate
from apps.core.data_assets import APP_AGENT_DATA_ALIAS_PREFIXES, request_assets_for_case
from apps.core.runtime_case import RuntimeCase, normalize_overrides, public_input_fields
from apps.core.runtime_orchestration import (
    RuntimeContext,
    build_runtime_override_from_row,
    merge_runtime_overrides,
    scan_case_input_fields,
)
from apps.core.smart_data_orchestration import prepare_smart_test_data
from ..utils.element_references import missing_active_element_ids

logger = logging.getLogger(__name__)


def _bool_from_request(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {'0', 'false', 'no', 'off'}


class AppPagination(PageNumberPagination):
    """APP自动化模块通用分页"""
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class AppPackageViewSet(viewsets.ModelViewSet):
    """APP应用包名管理 ViewSet"""
    queryset = AppPackage.objects.all()
    serializer_class = AppPackageSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = AppPagination
    search_fields = ['name', 'package_name']

    def get_queryset(self):
        queryset = super().get_queryset()
        if not is_platform_admin(self.request.user):
            queryset = queryset.filter(
                Q(created_by=self.request.user)
                | Q(test_cases__project__owner=self.request.user)
                | Q(test_cases__project__members=self.request.user)
            ).distinct()
        project_id = self.request.query_params.get('project')
        if not project_id:
            return queryset
        try:
            project_id = int(project_id)
        except (TypeError, ValueError):
            return queryset.none()
        return queryset.filter(test_cases__project_id=project_id).distinct()
    
    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class AppTestCaseViewSet(viewsets.ModelViewSet):
    """APP测试用例 ViewSet"""
    queryset = AppTestCase.objects.all()
    serializer_class = AppTestCaseSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = AppPagination
    filter_backends = [DjangoFilterBackend, SearchFilter]
    filterset_fields = ['project', 'app_package']
    search_fields = ['name']

    def get_queryset(self):
        return accessible_project_assets(
            super().get_queryset(),
            self.request.user,
            project_lookup='project',
            unscoped_owner_lookup='created_by',
        )
    
    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=['get'], url_path='input-data')
    def input_data(self, request, pk=None):
        """扫描当前 APP/UI Flow 中可在执行前覆盖的输入步骤。"""
        test_case = self.get_object()
        return Response({
            'caseId': test_case.id,
            'caseName': test_case.name,
            'fields': public_input_fields(test_case.ui_flow),
        })

    def _runtime_payload_from_request(self, request, test_case, raw_runtime_override):
        template_id = request.data.get('template_id') or request.data.get('runtime_template_id')
        runtime_context = request.data.get('runtimeContext', request.data.get('runtime_context', {})) or {}
        cleanup_config = request.data.get('cleanupConfig', request.data.get('cleanup_config', {})) or {}
        template = None
        template_override = []

        if template_id:
            template = RuntimeDataTemplate.objects.get(
                id=template_id,
                target_type__in=['app_automation', 'ui_automation'],
                target_case_id=test_case.id,
                is_active=True,
            )
            template_override = template.runtime_override or []
            runtime_context = {
                **(template.runtime_context or {}),
                **runtime_context,
            }
            cleanup_config = {
                **(template.cleanup_config or {}),
                **cleanup_config,
            }

        runtime_override = merge_runtime_overrides(template_override, raw_runtime_override)
        RuntimeCase(test_case.ui_flow, runtime_override).build(context=RuntimeContext(runtime_context))
        return template, runtime_override, runtime_context, cleanup_config
    
    @action(detail=True, methods=['post'])
    def execute(self, request, pk=None):
        """执行测试用例"""
        test_case = self.get_object()
        device_id = request.data.get('device_id')
        package_name = request.data.get('package_name')
        raw_runtime_override = request.data.get(
            'runtimeOverride', request.data.get('runtime_override', [])
        )

        missing_ids = missing_active_element_ids(test_case.ui_flow)
        if missing_ids:
            joined = ', '.join(str(element_id) for element_id in missing_ids)
            return Response({
                'success': False,
                'message': f'用例引用了不存在或已停用的元素: {joined}，请重新绑定后再执行。',
                'missing_element_ids': missing_ids,
            }, status=status.HTTP_400_BAD_REQUEST)
        
        if not device_id:
            return Response({
                'success': False,
                'message': '请选择执行设备'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            template, runtime_override, runtime_context, cleanup_config = self._runtime_payload_from_request(
                request, test_case, raw_runtime_override
            )

            # 检查设备是否可用
            device = AppDevice.objects.get(device_id=device_id)
            if device.status == 'locked' and device.locked_by != request.user:
                return Response({
                    'success': False,
                    'message': '设备已被其他用户锁定'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # 创建执行记录
            execution = AppTestExecution.objects.create(
                test_case=test_case,
                device=device,
                user=request.user,
                status='pending',
                runtime_override=runtime_override,
                runtime_context=runtime_context,
                cleanup_config=cleanup_config,
                runtime_template_id=template.id if template else None,
            )
            runtime_context, leases = request_assets_for_case(
                target_type='app_automation',
                case_id=test_case.id,
                execution_type='app_automation',
                execution_id=execution.id,
                user=request.user,
                runtime_context=runtime_context,
                case_name=test_case.name,
                skip_alias_prefixes=APP_AGENT_DATA_ALIAS_PREFIXES,
            )
            runtime_context, runtime_override, smart_logs, smart_leases = prepare_smart_test_data(
                target_type='app_automation',
                case_id=test_case.id,
                execution_type='app_automation',
                execution_id=execution.id,
                user=request.user,
                runtime_context=runtime_context,
                runtime_override=runtime_override,
                case_payload=test_case.ui_flow,
                case_name=test_case.name,
                enabled=_bool_from_request(
                    request.data.get('smartDataEnabled', request.data.get('smart_data_enabled'))
                ),
            )
            RuntimeCase(test_case.ui_flow, runtime_override).build(context=RuntimeContext(runtime_context))
            if leases or smart_logs or smart_leases:
                execution.runtime_context = runtime_context
                execution.runtime_override = runtime_override
                execution.save(update_fields=['runtime_context', 'runtime_override', 'updated_at'])
            
            # 调用 Celery 任务异步执行
            from ..tasks import execute_app_test_task
            task = execute_app_test_task.delay(execution.id, package_name=package_name)
            execution.task_id = task.id
            execution.save()
            
            logger.info(f"测试已提交执行: execution_id={execution.id}, task_id={task.id}")
            
            return Response({
                'success': True,
                'message': '测试已提交执行',
                'execution': AppTestExecutionSerializer(execution).data
            })
            
        except ValueError as e:
            return Response({
                'success': False,
                'message': str(e),
            }, status=status.HTTP_400_BAD_REQUEST)
        except RuntimeDataTemplate.DoesNotExist:
            return Response({
                'success': False,
                'message': '数据模板不存在或不属于当前用例',
            }, status=status.HTTP_404_NOT_FOUND)
        except AppDevice.DoesNotExist:
            return Response({
                'success': False,
                'message': '设备不存在'
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"执行测试失败: {str(e)}")
            return Response({
                'success': False,
                'message': f'执行测试失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='batch-execute')
    def batch_execute(self, request, pk=None):
        """按数据集多行数据循环执行同一个用例。"""
        test_case = self.get_object()
        device_id = request.data.get('device_id')
        package_name = request.data.get('package_name')
        dataset_id = request.data.get('dataset_id') or request.data.get('runtime_dataset_id')
        template_id = request.data.get('template_id') or request.data.get('runtime_template_id')
        raw_runtime_override = request.data.get('runtimeOverride', request.data.get('runtime_override', []))

        if not device_id:
            return Response({'success': False, 'message': '请选择执行设备'}, status=status.HTTP_400_BAD_REQUEST)
        if not dataset_id:
            return Response({'success': False, 'message': '请选择批量数据集'}, status=status.HTTP_400_BAD_REQUEST)

        missing_ids = missing_active_element_ids(test_case.ui_flow)
        if missing_ids:
            joined = ', '.join(str(element_id) for element_id in missing_ids)
            return Response({
                'success': False,
                'message': f'用例引用了不存在或已停用的元素: {joined}，请重新绑定后再执行。',
                'missing_element_ids': missing_ids,
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            device = AppDevice.objects.get(device_id=device_id)
            if device.status == 'locked' and device.locked_by != request.user:
                return Response({'success': False, 'message': '设备已被其他用户锁定'}, status=status.HTTP_400_BAD_REQUEST)

            dataset = RuntimeDataSet.objects.get(
                id=dataset_id,
                target_type__in=['app_automation', 'ui_automation'],
                target_case_id=test_case.id,
            )
            template = None
            template_override = []
            runtime_context_base = request.data.get('runtimeContext', request.data.get('runtime_context', {})) or {}
            cleanup_config_base = request.data.get('cleanupConfig', request.data.get('cleanup_config', {})) or {}
            if template_id:
                template = RuntimeDataTemplate.objects.get(
                    id=template_id,
                    target_type__in=['app_automation', 'ui_automation'],
                    target_case_id=test_case.id,
                    is_active=True,
                )
                template_override = template.runtime_override or []
                runtime_context_base = {
                    **(template.runtime_context or {}),
                    **runtime_context_base,
                }
                cleanup_config_base = {
                    **(template.cleanup_config or {}),
                    **cleanup_config_base,
                }

            fields = scan_case_input_fields('app_automation', test_case.id)['fields']
            executions = []
            for index, row in enumerate(dataset.rows or []):
                row_override = build_runtime_override_from_row(fields, row)
                runtime_override = merge_runtime_overrides(template_override, row_override, raw_runtime_override)
                row_context = {**runtime_context_base, 'data': row}
                RuntimeCase(test_case.ui_flow, runtime_override).build(context=RuntimeContext(row_context))
                execution = AppTestExecution.objects.create(
                    test_case=test_case,
                    device=device,
                    user=request.user,
                    status='pending',
                    runtime_override=runtime_override,
                    runtime_context=row_context,
                    cleanup_config=cleanup_config_base,
                    runtime_template_id=template.id if template else None,
                    runtime_dataset_id=dataset.id,
                    runtime_dataset_row_index=index,
                )
                row_context, leases = request_assets_for_case(
                    target_type='app_automation',
                    case_id=test_case.id,
                    execution_type='app_automation',
                    execution_id=execution.id,
                    user=request.user,
                    runtime_context=row_context,
                    case_name=test_case.name,
                    skip_alias_prefixes=APP_AGENT_DATA_ALIAS_PREFIXES,
                )
                row_context, runtime_override, smart_logs, smart_leases = prepare_smart_test_data(
                    target_type='app_automation',
                    case_id=test_case.id,
                    execution_type='app_automation',
                    execution_id=execution.id,
                    user=request.user,
                    runtime_context=row_context,
                    runtime_override=runtime_override,
                    case_payload=test_case.ui_flow,
                    case_name=test_case.name,
                    enabled=_bool_from_request(
                        request.data.get('smartDataEnabled', request.data.get('smart_data_enabled'))
                    ),
                )
                RuntimeCase(test_case.ui_flow, runtime_override).build(context=RuntimeContext(row_context))
                if leases or smart_logs or smart_leases:
                    execution.runtime_context = row_context
                    execution.runtime_override = runtime_override
                    execution.save(update_fields=['runtime_context', 'runtime_override', 'updated_at'])
                executions.append(execution)

            from ..tasks import execute_app_test_task
            for execution in executions:
                task = execute_app_test_task.delay(execution.id, package_name=package_name)
                execution.task_id = task.id
                execution.save(update_fields=['task_id', 'updated_at'])

            return Response({
                'success': True,
                'message': f'已提交 {len(executions)} 组数据执行',
                'execution_ids': [item.id for item in executions],
                'executions': AppTestExecutionSerializer(executions, many=True).data,
            })
        except (ValueError, RuntimeDataSet.DoesNotExist, RuntimeDataTemplate.DoesNotExist) as e:
            return Response({'success': False, 'message': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except AppDevice.DoesNotExist:
            return Response({'success': False, 'message': '设备不存在'}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"批量执行测试失败: {str(e)}", exc_info=True)
            return Response({'success': False, 'message': f'批量执行失败: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
