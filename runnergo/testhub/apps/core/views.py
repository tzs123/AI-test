"""
Core 应用视图
"""
from rest_framework import mixins, viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.exceptions import ValidationError
from django.http import FileResponse
from django.db import transaction
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters

from .ai_data_orchestration import analyze_case_test_data
from .ai_test_data import generate_ai_test_data
from .bulk_data_generation import _iter_rows, read_generated_rows, validate_generation_request
from .data_assets import (
    create_or_update_asset_with_bindings,
    delete_asset_and_generated_data,
    delete_generated_data_for_job,
    delete_requirement_and_generated_data,
    release_lease,
    request_assets_for_case,
)
from .image_data_assets import recognize_image_fields
from .models import (
    RuntimeDataSet,
    RuntimeDataTemplate,
    RuntimeExecutionSnapshot,
    BulkTestDataJob,
    TestDataAsset,
    TestDataDecisionLog,
    TestDataAssetLease,
    TestDataAssetRequirement,
)
from apps.app_automation.models import AppTestCase
from apps.testcases.models import TestCase
from .runtime_orchestration import (
    build_runtime_override_from_row,
    get_case_payload,
    normalize_target_type,
    parse_dataset_file,
    scan_case_input_fields,
)
from .smart_data_orchestration import analyze_data_requirements, prepare_smart_test_data
from .serializers import (
    RuntimeDataSetSerializer,
    RuntimeDataTemplateSerializer,
    RuntimeExecutionSnapshotSerializer,
    TestDataDecisionLogSerializer,
    TestDataAssetLeaseSerializer,
    TestDataAssetRequirementSerializer,
    TestDataAssetSerializer,
    BulkTestDataJobSerializer,
)

import logging
import os
logger = logging.getLogger(__name__)


def _default_case_id_for_target_type(target_type):
    """Fallback to the first available case for a target type."""
    normalized_type = normalize_target_type(target_type or 'api_automation')
    if normalized_type == 'app_automation':
        case = AppTestCase.objects.order_by('-updated_at', '-created_at', '-id').first()
    elif normalized_type == 'ui_automation':
        case = AppTestCase.objects.order_by('-updated_at', '-created_at', '-id').first()
    else:
        case = TestCase.objects.filter(test_type='api').order_by('-updated_at', '-created_at', '-id').first()
        if case is None:
            case = TestCase.objects.order_by('-updated_at', '-created_at', '-id').first()
    if not case:
        raise ValueError('当前没有可用测试对象，请先创建 API / UI / APP 用例')
    return case.id


class RuntimeDataTemplateViewSet(viewsets.ModelViewSet):
    """测试数据模板管理。"""

    queryset = RuntimeDataTemplate.objects.all()
    serializer_class = RuntimeDataTemplateSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['target_type', 'target_case_id', 'is_active']
    search_fields = ['name', 'target_case_name']
    ordering_fields = ['created_at', 'updated_at']
    ordering = ['-updated_at']

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=False, methods=['get'], url_path='scan-fields')
    def scan_fields(self, request):
        """扫描 UI/APP/API 用例输入字段。"""
        try:
            target_type = normalize_target_type(request.query_params.get('target_type'))
            case_id = int(request.query_params.get('case_id'))
            return Response(scan_case_input_fields(target_type, case_id))
        except Exception as exc:
            return Response({'message': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['get'], url_path='smart-analyze')
    def smart_analyze(self, request):
        """V6 智能分析 UI/APP/API 用例测试数据需求。"""
        try:
            target_type = normalize_target_type(request.query_params.get('target_type'))
            case_id = int(request.query_params.get('case_id'))
            payload = scan_case_input_fields(target_type, case_id)
            case_payload, _ = get_case_payload(target_type, case_id)
            payload['smartRequirements'] = analyze_data_requirements(case_payload)
            return Response(payload)
        except Exception as exc:
            return Response({'message': str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class RuntimeDataSetViewSet(viewsets.ModelViewSet):
    """批量测试数据集管理。"""

    queryset = RuntimeDataSet.objects.all()
    serializer_class = RuntimeDataSetSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['target_type', 'target_case_id']
    search_fields = ['name', 'target_case_name', 'source_filename']
    ordering_fields = ['created_at', 'updated_at']
    ordering = ['-updated_at']

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=False, methods=['post'], url_path='import-file')
    def import_file(self, request):
        """导入 CSV/Excel 数据集。"""
        upload = request.FILES.get('file')
        if not upload:
            return Response({'message': '请上传 CSV 或 Excel 文件'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            target_type = normalize_target_type(request.data.get('target_type'))
            target_case_id = int(request.data.get('target_case_id'))
            target_case_name = request.data.get('target_case_name') or ''
            columns, rows = parse_dataset_file(upload)
            dataset = RuntimeDataSet.objects.create(
                name=request.data.get('name') or upload.name,
                description=request.data.get('description') or '',
                target_type=target_type,
                target_case_id=target_case_id,
                target_case_name=target_case_name,
                columns=columns,
                rows=rows,
                row_count=len(rows),
                source_filename=upload.name,
                created_by=request.user,
            )
            return Response(RuntimeDataSetSerializer(dataset).data, status=status.HTTP_201_CREATED)
        except Exception as exc:
            return Response({'message': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='preview-overrides')
    def preview_overrides(self, request, pk=None):
        """预览某一行数据将生成的 runtimeOverride。"""
        dataset = self.get_object()
        row_index = int(request.data.get('row_index') or 0)
        try:
            fields = scan_case_input_fields(dataset.target_type, dataset.target_case_id)['fields']
            row = dataset.rows[row_index]
            return Response({
                'rowIndex': row_index,
                'row': row,
                'runtimeOverride': build_runtime_override_from_row(fields, row),
            })
        except Exception as exc:
            return Response({'message': str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class RuntimeExecutionSnapshotViewSet(viewsets.ReadOnlyModelViewSet):
    """运行态执行数据快照，只读查询。"""

    queryset = RuntimeExecutionSnapshot.objects.all()
    serializer_class = RuntimeExecutionSnapshotSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['target_type', 'target_case_id', 'execution_type', 'execution_id', 'template', 'dataset']
    search_fields = ['target_case_name']
    ordering_fields = ['created_at']
    ordering = ['-created_at']


class BulkTestDataJobViewSet(viewsets.ModelViewSet):
    """测试数据中心的大批量生成任务。"""

    serializer_class = BulkTestDataJobSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'asset_type', 'output_format']
    search_fields = ['name']
    ordering_fields = ['created_at', 'updated_at', 'total_count', 'progress']
    ordering = ['-created_at']

    def get_queryset(self):
        return BulkTestDataJob.objects.filter(created_by=self.request.user)

    @action(detail=False, methods=['post'])
    def generate(self, request):
        try:
            payload = validate_generation_request(request.data)
        except (TypeError, ValueError) as exc:
            return Response({'message': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        job = BulkTestDataJob.objects.create(created_by=request.user, **payload)
        try:
            from .tasks import generate_bulk_test_data
            async_result = generate_bulk_test_data.delay(job.id)
            job.task_id = async_result.id or ''
            job.save(update_fields=['task_id', 'updated_at'])
        except Exception as exc:
            job.status = BulkTestDataJob.STATUS_FAILED
            job.error_message = f'提交后台任务失败：{exc}'
            job.progress = 100
            job.completed_at = timezone.now()
            job.save(update_fields=['status', 'error_message', 'progress', 'completed_at', 'updated_at'])
            return Response({'message': job.error_message}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(self.get_serializer(job).data, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        job = self.get_object()
        if job.status not in {BulkTestDataJob.STATUS_PENDING, BulkTestDataJob.STATUS_RUNNING}:
            return Response({'message': '当前任务不可取消'}, status=status.HTTP_400_BAD_REQUEST)
        job.cancel_requested = True
        if job.status == BulkTestDataJob.STATUS_PENDING:
            job.status = BulkTestDataJob.STATUS_CANCELED
            job.progress = 100
            job.completed_at = timezone.now()
        job.save(update_fields=['cancel_requested', 'status', 'progress', 'completed_at', 'updated_at'])
        return Response(self.get_serializer(job).data)

    @action(detail=True, methods=['get'])
    def preview(self, request, pk=None):
        job = self.get_object()
        if job.status != BulkTestDataJob.STATUS_COMPLETED or not job.output_file:
            return Response({'message': '任务尚未生成完成'}, status=status.HTTP_409_CONFLICT)
        try:
            limit = max(1, min(100, int(request.query_params.get('limit') or 20)))
            rows = []
            with job.output_file.open('r') as stream:
                if job.output_format == BulkTestDataJob.FORMAT_CSV:
                    reader = __import__('csv').DictReader(stream)
                    columns = reader.fieldnames or []
                    for row in reader:
                        rows.append(dict(row))
                        if len(rows) >= limit:
                            break
                elif job.output_format == BulkTestDataJob.FORMAT_JSONL:
                    columns = [item['name'] for item in job.field_definitions]
                    for line in stream:
                        if line.strip():
                            rows.append(__import__('json').loads(line))
                        if len(rows) >= limit:
                            break
                else:
                    # SQL 文件同时包含建表语句和 INSERT 语句，预览直接复用同一随机种子生成前几行，
                    # 避免反解析不同数据库方言的 SQL 文本。
                    columns = [item['name'] for item in job.field_definitions]
                    rows = []
                    for row in _iter_rows(job.field_definitions, job.total_count, job.seed or 0):
                        rows.append(row)
                        if len(rows) >= limit:
                            break
            return Response({'columns': columns, 'rows': rows, 'total_count': job.total_count})
        except Exception as exc:
            return Response({'message': f'预览失败：{exc}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['get'])
    def download(self, request, pk=None):
        job = self.get_object()
        if job.status != BulkTestDataJob.STATUS_COMPLETED or not job.output_file:
            return Response({'message': '任务尚未生成完成'}, status=status.HTTP_409_CONFLICT)
        try:
            response = FileResponse(job.output_file.open('rb'), as_attachment=True, filename=f'{job.name}.{job.output_format}')
            return response
        except FileNotFoundError:
            return Response({'message': '生成文件不存在'}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=['post'], url_path='bulk-delete')
    def bulk_delete(self, request):
        raw_ids = request.data.get('ids') or request.data.get('job_ids') or []
        if not isinstance(raw_ids, list) or not raw_ids:
            return Response({'message': '请至少选择一个任务'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            job_ids = [int(item) for item in raw_ids]
        except (TypeError, ValueError):
            return Response({'message': '任务ID必须是整数数组'}, status=status.HTTP_400_BAD_REQUEST)

        jobs = list(self.get_queryset().filter(id__in=set(job_ids)))
        missing = sorted(set(job_ids) - {job.id for job in jobs})
        if missing:
            return Response({'message': f'任务不存在或无权限：{missing}'}, status=status.HTTP_404_NOT_FOUND)

        blocked = [
            job for job in jobs
            if job.status in {BulkTestDataJob.STATUS_PENDING, BulkTestDataJob.STATUS_RUNNING}
        ]
        if blocked:
            return Response({
                'message': '排队中或生成中的任务不能删除，请先取消任务',
                'blocked_ids': [job.id for job in blocked],
            }, status=status.HTTP_409_CONFLICT)

        with transaction.atomic():
            for job in jobs:
                delete_generated_data_for_job(job)
        return Response({'deleted_ids': [job.id for job in jobs]})

    def perform_destroy(self, instance):
        if instance.status in {BulkTestDataJob.STATUS_PENDING, BulkTestDataJob.STATUS_RUNNING}:
            raise ValidationError('排队中或生成中的任务不能删除，请先取消任务')
        delete_generated_data_for_job(instance)


class TestDataAssetViewSet(viewsets.ModelViewSet):
    """测试数据资产池管理。"""

    queryset = TestDataAsset.objects.all().prefetch_related('requirements')
    serializer_class = TestDataAssetSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['asset_type', 'status']
    search_fields = ['name']
    ordering_fields = ['created_at', 'updated_at', 'expires_at']
    ordering = ['-updated_at']

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def perform_destroy(self, instance):
        delete_asset_and_generated_data(instance)

    def _save_and_bind(self, request, asset=None):
        asset_payload = request.data.get('asset')
        binding_payload = request.data.get('binding')
        if not isinstance(asset_payload, dict):
            raise ValidationError({'asset': 'asset 必须是对象'})
        if not isinstance(binding_payload, dict):
            raise ValidationError({'binding': '保存数据资产时必须同时关联用例'})

        asset_serializer = self.get_serializer(
            asset,
            data=asset_payload,
            partial=asset is not None,
        )
        asset_serializer.is_valid(raise_exception=True)

        raw_binding = dict(binding_payload)
        binding_id = raw_binding.pop('id', None) or raw_binding.pop('binding_id', None)
        binding_serializer = TestDataAssetRequirementSerializer(data=raw_binding)
        binding_serializer.is_valid(raise_exception=True)
        validated_binding = dict(binding_serializer.validated_data)
        if binding_id:
            validated_binding['id'] = binding_id

        try:
            saved_asset, saved_bindings = create_or_update_asset_with_bindings(
                asset_values=asset_serializer.validated_data,
                bindings=[validated_binding],
                user=request.user,
                asset=asset,
                replace_bindings=True,
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        saved_asset = self.get_queryset().get(id=saved_asset.id)
        return Response(
            {
                'asset': self.get_serializer(saved_asset).data,
                'binding': TestDataAssetRequirementSerializer(saved_bindings[0]).data,
            },
            status=status.HTTP_200_OK if asset else status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=['post'], url_path='save-and-bind')
    def save_and_bind(self, request):
        return self._save_and_bind(request)

    @action(detail=True, methods=['patch'], url_path='save-and-bind')
    def update_and_bind(self, request, pk=None):
        return self._save_and_bind(request, self.get_object())

    @action(detail=False, methods=['post'], url_path='bulk-delete')
    def bulk_delete(self, request):
        raw_ids = request.data.get('ids') or []
        if not isinstance(raw_ids, list) or not raw_ids:
            return Response({'message': '请至少选择一条数据资产'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            asset_ids = list(dict.fromkeys(int(item) for item in raw_ids))
        except (TypeError, ValueError):
            return Response({'message': '数据资产ID必须是整数数组'}, status=status.HTTP_400_BAD_REQUEST)

        assets = list(self.get_queryset().filter(id__in=asset_ids))
        missing = sorted(set(asset_ids) - {asset.id for asset in assets})
        if missing:
            return Response({'message': f'数据资产不存在或无权限：{missing}'}, status=status.HTTP_404_NOT_FOUND)

        deleted_ids = [asset.id for asset in assets]
        with transaction.atomic():
            for asset in assets:
                # 删除批量生成任务时可能连带删除同批次的其他资产。
                if TestDataAsset.objects.filter(id=asset.id).exists():
                    delete_asset_and_generated_data(asset)
        return Response({'deleted_ids': deleted_ids})

    @action(
        detail=False,
        methods=['post'],
        url_path='recognize-image',
        parser_classes=[MultiPartParser, FormParser],
    )
    def recognize_image(self, request):
        """识别表单截图中的字段，供前端确认后生成资产或大批量数据。"""
        try:
            result = recognize_image_fields(request.FILES.get('file'))
            result['asset_type'] = request.data.get('asset_type') or 'CUSTOM'
            result['asset_name'] = request.data.get('asset_name') or '图片识别测试数据'
            return Response(result)
        except Exception as exc:
            return Response({'message': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(
        detail=False,
        methods=['post'],
        url_path='agent-publish',
        permission_classes=[],
        authentication_classes=[],
    )
    def agent_publish(self, request):
        """Publish generated rows and optionally bind them to an execution target."""
        expected_token = os.environ.get('TEST_DATA_CENTER_AGENT_TOKEN', 'runnergo-local-agent-token')
        provided_token = request.headers.get('X-Agent-Token') or request.data.get('agent_token')
        if expected_token and provided_token != expected_token:
            return Response({'message': '无效的 Agent 同步令牌'}, status=status.HTTP_403_FORBIDDEN)

        rows = request.data.get('rows') or request.data.get('payload') or []
        if isinstance(rows, dict):
            rows = [rows]
        rows = [row for row in rows if isinstance(row, dict)]
        if not rows:
            return Response({'message': 'rows 不能为空'}, status=status.HTTP_400_BAD_REQUEST)


        asset = TestDataAsset.objects.create(
            asset_type=request.data.get('asset_type') or request.data.get('assetType') or 'CUSTOM',
            name=request.data.get('name') or 'AI Agent 生成测试数据',
            status=request.data.get('status') or TestDataAsset.STATUS_AVAILABLE,
            payload=rows,
            tags=list(dict.fromkeys(request.data.get('tags') or ['ai-generated', 'auto-test-agent'])),
            created_by=None,
        )
        requirement = None
        target_case_id = request.data.get('target_case_id') or request.data.get('targetCaseId')
        alias = str(request.data.get('alias') or '').strip()
        if target_case_id and alias:
            requirement, _ = TestDataAssetRequirement.objects.update_or_create(
                target_type=request.data.get('target_type') or request.data.get('targetType') or 'ui_automation',
                target_case_id=int(target_case_id),
                alias=alias,
                defaults={
                    'target_case_name': request.data.get('target_case_name') or request.data.get('targetCaseName') or '',
                    'asset_type': asset.asset_type,
                    'source_asset': asset,
                    'quantity': 1,
                    'filters': request.data.get('filters') or {},
                    'tags': list(dict.fromkeys(request.data.get('binding_tags') or asset.tags or [])),
                    'release_policy': request.data.get('release_policy') or 'release',
                    'inject_path': request.data.get('inject_path') or '',
                    'is_active': True,
                    'created_by': None,
                },
            )
        payload = TestDataAssetSerializer(asset).data
        payload['requirement'] = (
            TestDataAssetRequirementSerializer(requirement).data if requirement else None
        )
        return Response(payload, status=status.HTTP_201_CREATED)

    @action(
        detail=False,
        methods=['get'],
        url_path='agent-context',
        permission_classes=[],
        authentication_classes=[],
    )
    def agent_context(self, request):
        """Agent 只读拉取数据资产上下文，替代跨容器直读共享 SQLite 文件。

        返回 aliases 对应的活跃 requirement 与可用 asset（含大批量资产的前
        MAX 行预览），数据形状与 auto-test executor 的文件读取路径一致。
        """
        expected_token = os.environ.get('TEST_DATA_CENTER_AGENT_TOKEN', 'runnergo-local-agent-token')
        provided_token = request.headers.get('X-Agent-Token') or request.query_params.get('agent_token')
        if expected_token and provided_token != expected_token:
            return Response({'message': '无效的 Agent 同步令牌'}, status=status.HTTP_403_FORBIDDEN)

        raw_aliases = str(request.query_params.get('aliases') or '')
        aliases = [item.strip() for item in raw_aliases.split(',') if item.strip()][:50]
        if not aliases:
            return Response({'requirements': [], 'assets': []})

        requirements = list(
            TestDataAssetRequirement.objects
            .filter(is_active=True, alias__in=aliases)
            .order_by('id')
            .values(
                'id', 'target_type', 'target_case_id', 'target_case_name', 'alias',
                'asset_type', 'source_asset_id', 'quantity', 'filters', 'tags',
                'release_policy', 'inject_path', 'lock_ttl_seconds', 'is_active',
            )
        )
        assets = []
        for asset in TestDataAsset.objects.filter(
            status=TestDataAsset.STATUS_AVAILABLE
        ).order_by('id'):
            # payload 可能是 dict（bulk/单对象）也可能是 list（AI/Agent 生成的多行数据），
            # 只放行 dict 会把 Agent 生成数据丢成 {}，导致执行端 ${dataAssets.*} 无法解析
            payload = asset.payload if isinstance(asset.payload, (dict, list)) else {}
            item = {
                'id': asset.id,
                'asset_type': asset.asset_type,
                'status': asset.status,
                'payload': payload,
                'tags': asset.tags if isinstance(asset.tags, list) else [],
            }
            if isinstance(payload, dict) and payload.get('_runnergo_source') == 'bulk_test_data' and asset.bulk_job_id:
                item['bulk_rows'] = read_generated_rows(asset.bulk_job, limit=100)
            assets.append(item)
        return Response({'requirements': requirements, 'assets': assets})

    @action(detail=False, methods=['post'], url_path='ai-generate')
    def ai_generate(self, request):
        """AI测试数据生成，可选择保存为资产并创建自动注入需求。"""
        try:
            interface_name = request.data.get('interface_name') or request.data.get('interfaceName') or '接口'
            result = generate_ai_test_data(
                interface_name=interface_name,
                fields=request.data.get('fields') or [],
                count=int(request.data.get('count') or 1),
                include_security=bool(request.data.get('include_security', True)),
            )

            created_assets = []
            save_to_assets = bool(request.data.get('save_to_assets') or request.data.get('saveToAssets'))
            asset_type = request.data.get('asset_type') or request.data.get('assetType') or 'USER'
            status_value = request.data.get('status') or TestDataAsset.STATUS_AVAILABLE
            batch_tag = request.data.get('batch_tag') or f'ai-generated-{request.user.id}-{timezone.now().strftime("%Y%m%d%H%M%S")}'

            if save_to_assets:
                for index, case in enumerate(result['cases'], start=1):
                    tags = list(dict.fromkeys([
                        'ai-generated',
                        batch_tag,
                        case['type'],
                        *case.get('tags', []),
                    ]))
                    asset = TestDataAsset.objects.create(
                        asset_type=asset_type,
                        name=f'{interface_name}-{case["title"]}-{index}',
                        status=status_value,
                        payload={
                            'interface': interface_name,
                            'case_id': case['id'],
                            'case_type': case['type'],
                            'title': case['title'],
                            'expected': case['expected'],
                            'data': case['payload'],
                        },
                        tags=tags,
                        created_by=request.user,
                    )
                    created_assets.append(asset)

            requirement = None
            auto_requirement = bool(
                request.data.get('auto_create_requirement') or request.data.get('autoCreateRequirement')
            )
            target_case_id = request.data.get('target_case_id') or request.data.get('targetCaseId')
            if save_to_assets and auto_requirement and target_case_id:
                requirement = TestDataAssetRequirement.objects.create(
                    target_type=request.data.get('target_type') or request.data.get('targetType') or 'api_automation',
                    target_case_id=int(target_case_id),
                    target_case_name=request.data.get('target_case_name') or request.data.get('targetCaseName') or interface_name,
                    alias=request.data.get('alias') or 'aiTestData',
                    asset_type=asset_type,
                    quantity=max(1, min(len(created_assets), int(request.data.get('requirement_quantity') or request.data.get('requirementQuantity') or 1))),
                    filters=request.data.get('filters') or {},
                    tags=[batch_tag],
                    release_policy=request.data.get('release_policy') or request.data.get('releasePolicy') or 'release',
                    inject_path=request.data.get('inject_path') or request.data.get('injectPath') or '',
                    created_by=request.user,
                )

            response_data = {
                **result,
                'batch_tag': batch_tag,
                'created_assets': TestDataAssetSerializer(created_assets, many=True).data,
                'created_requirement': (
                    TestDataAssetRequirementSerializer(requirement).data if requirement else None
                ),
            }
            return Response(response_data)
        except Exception as exc:
            return Response({'message': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['post'], url_path='ai-analyze-case')
    def ai_analyze_case(self, request):
        """引用 API/UI/APP 用例内容，生成正常、异常、安全和关联链路测试数据。"""
        try:
            target_type = request.data.get('target_type') or request.data.get('targetType')
            case_id = request.data.get('target_case_id') or request.data.get('targetCaseId')
            if not case_id:
                case_id = _default_case_id_for_target_type(target_type)
            result = analyze_case_test_data(
                target_type=target_type,
                case_id=case_id,
                goal=request.data.get('goal') or request.data.get('test_target') or request.data.get('testTarget') or '',
                fields=request.data.get('fields') or None,
                count=int(request.data.get('count') or 1),
                include_security=bool(request.data.get('include_security', True)),
                requested_counts=request.data.get('requested_counts') or request.data.get('requestedCounts') or {},
            )

            created_assets = []
            created_requirement = None
            save_to_assets = bool(request.data.get('save_to_assets') or request.data.get('saveToAssets'))
            if save_to_assets:
                target = result['target']
                asset_type = request.data.get('asset_type') or request.data.get('assetType') or 'CUSTOM'
                batch_tag = request.data.get('batch_tag') or f'ai-case-{request.user.id}-{timezone.now().strftime("%Y%m%d%H%M%S")}'
                for index, case in enumerate(result['generation']['cases'], start=1):
                    asset = TestDataAsset.objects.create(
                        asset_type=asset_type,
                        name=f'{target["caseName"]}-{case["title"]}-{index}',
                        status=request.data.get('status') or TestDataAsset.STATUS_AVAILABLE,
                        payload={
                            'target': target,
                            'case_id': case['id'],
                            'case_type': case['type'],
                            'title': case['title'],
                            'expected': case['expected'],
                            'data': case['payload'],
                            'data_chain': result['dataChain'],
                            'agent_plan': result['agentPlan'],
                        },
                        tags=list(dict.fromkeys(['ai-generated', 'ai-case-analysis', batch_tag, case['type'], *case.get('tags', [])])),
                        created_by=request.user,
                    )
                    created_assets.append(asset)

                if bool(request.data.get('auto_create_requirement') or request.data.get('autoCreateRequirement')):
                    created_requirement = TestDataAssetRequirement.objects.create(
                        target_type=target['targetType'],
                        target_case_id=target['caseId'],
                        target_case_name=target['caseName'],
                        alias=request.data.get('alias') or 'aiTestData',
                        asset_type=asset_type,
                        quantity=max(1, min(len(created_assets), int(request.data.get('requirement_quantity') or 1))),
                        filters=request.data.get('filters') or {},
                        tags=[batch_tag],
                        release_policy=request.data.get('release_policy') or 'release',
                        inject_path=request.data.get('inject_path') or '',
                        created_by=request.user,
                    )

            result['created_assets'] = TestDataAssetSerializer(created_assets, many=True).data
            result['created_requirement'] = (
                TestDataAssetRequirementSerializer(created_requirement).data if created_requirement else None
            )
            return Response(result)
        except Exception as exc:
            return Response({'message': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def release(self, request, pk=None):
        asset = self.get_object()
        from django.utils import timezone
        now = timezone.now()
        asset.status = TestDataAsset.STATUS_AVAILABLE
        asset.locked_by = None
        asset.locked_at = None
        asset.save(update_fields=['status', 'locked_by', 'locked_at', 'updated_at'])
        TestDataAssetLease.objects.filter(
            asset=asset,
            status=TestDataAssetLease.STATUS_LOCKED,
        ).update(status=TestDataAssetLease.STATUS_RELEASED, released_at=now, updated_at=now)
        return Response(self.get_serializer(asset).data)

    @action(detail=True, methods=['post'], url_path='mark-used')
    def mark_used(self, request, pk=None):
        asset = self.get_object()
        from django.utils import timezone
        asset.status = TestDataAsset.STATUS_USED
        asset.used_at = timezone.now()
        asset.locked_by = None
        asset.locked_at = None
        asset.save(update_fields=['status', 'used_at', 'locked_by', 'locked_at', 'updated_at'])
        return Response(self.get_serializer(asset).data)

    @action(detail=True, methods=['post'])
    def expire(self, request, pk=None):
        asset = self.get_object()
        asset.status = TestDataAsset.STATUS_EXPIRED
        asset.locked_by = None
        asset.locked_at = None
        asset.save(update_fields=['status', 'locked_by', 'locked_at', 'updated_at'])
        return Response(self.get_serializer(asset).data)


class TestDataAssetRequirementViewSet(viewsets.ModelViewSet):
    """用例数据需求声明。"""

    queryset = TestDataAssetRequirement.objects.all()
    serializer_class = TestDataAssetRequirementSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['target_type', 'target_case_id', 'asset_type', 'is_active']
    search_fields = ['target_case_name', 'alias', 'asset_type']
    ordering_fields = ['created_at', 'updated_at']
    ordering = ['-updated_at']

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def perform_destroy(self, instance):
        delete_requirement_and_generated_data(instance)

    @action(detail=False, methods=['post'], url_path='request-preview')
    def request_preview(self, request):
        """预申请并注入 Runtime Context，可用于调试数据需求。"""
        try:
            runtime_context, leases = request_assets_for_case(
                target_type=request.data.get('target_type'),
                case_id=int(request.data.get('target_case_id')),
                execution_type=request.data.get('execution_type') or request.data.get('target_type'),
                execution_id=request.data.get('execution_id') or f'preview-{request.user.id}',
                user=request.user,
                runtime_context=request.data.get('runtime_context') or {},
                case_name=request.data.get('target_case_name') or '',
            )
            return Response({
                'runtime_context': runtime_context,
                'leases': TestDataAssetLeaseSerializer(leases, many=True).data,
            })
        except Exception as exc:
            return Response({'message': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['post'], url_path='smart-prepare-preview')
    def smart_prepare_preview(self, request):
        """V6 智能准备测试数据预览：资产池优先，无资产则规则生成。"""
        try:
            target_type = normalize_target_type(request.data.get('target_type'))
            case_id = int(request.data.get('target_case_id'))
            runtime_context, runtime_override, logs, leases = prepare_smart_test_data(
                target_type=target_type,
                case_id=case_id,
                execution_type=request.data.get('execution_type') or target_type,
                execution_id=request.data.get('execution_id') or f'smart-preview-{request.user.id}',
                user=request.user,
                runtime_context=request.data.get('runtime_context') or {},
                runtime_override=request.data.get('runtime_override') or request.data.get('runtimeOverride') or [],
                case_name=request.data.get('target_case_name') or '',
            )
            return Response({
                'runtime_context': runtime_context,
                'runtime_override': runtime_override,
                'decision_logs': TestDataDecisionLogSerializer(logs, many=True).data,
                'leases': TestDataAssetLeaseSerializer(leases, many=True).data,
            })
        except Exception as exc:
            return Response({'message': str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class TestDataAssetLeaseViewSet(mixins.DestroyModelMixin, viewsets.ReadOnlyModelViewSet):
    """测试数据资产租约记录。"""

    queryset = TestDataAssetLease.objects.select_related('asset', 'requested_by', 'requirement').all()
    serializer_class = TestDataAssetLeaseSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['target_type', 'target_case_id', 'execution_type', 'execution_id', 'alias', 'status']
    search_fields = ['target_case_name', 'alias', 'asset__name', 'asset__asset_type']
    ordering_fields = ['created_at', 'released_at']
    ordering = ['-created_at']

    @action(detail=True, methods=['post'])
    def release(self, request, pk=None):
        lease = self.get_object()
        result = release_lease(lease, request.data.get('release_policy'))
        lease.refresh_from_db()
        return Response({
            'result': result,
            'lease': self.get_serializer(lease).data,
        })

    def perform_destroy(self, instance):
        if instance.status == TestDataAssetLease.STATUS_LOCKED:
            release_lease(instance)
        instance.delete()


class TestDataDecisionLogViewSet(viewsets.ReadOnlyModelViewSet):
    """V6 测试数据决策日志，只读查询。"""

    queryset = TestDataDecisionLog.objects.select_related('asset', 'lease', 'created_by').all()
    serializer_class = TestDataDecisionLogSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = [
        'target_type', 'target_case_id', 'execution_type', 'execution_id',
        'field_type', 'source', 'status',
    ]
    search_fields = ['target_case_name', 'field_key', 'field_label', 'alias']
    ordering_fields = ['created_at', 'updated_at']
    ordering = ['-created_at']
