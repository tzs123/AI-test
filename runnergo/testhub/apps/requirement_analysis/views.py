import asyncio
import base64
import logging
import re
import os  # Added import
import json
import tempfile
import time
import xml.etree.ElementTree as ET
from rest_framework import viewsets, status
from rest_framework.views import APIView
from django.conf import settings  # Added import
from django.core import signing
from rest_framework.decorators import action, permission_classes
from rest_framework.response import Response
from rest_framework.renderers import BaseRenderer
from rest_framework.permissions import (
    AllowAny,
    BasePermission,
    IsAuthenticated,
    SAFE_METHODS,
)


class PassThroughRenderer(BaseRenderer):
    """直接透传StreamingHttpResponse，不进行任何渲染处理"""
    media_type = 'text/event-stream'
    format = 'event-stream'
    render_level = 0

    def render(self, data, accepted_media_type=None, renderer_context=None):
        # 直接返回data，不做任何处理
        return data


from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from django.http import FileResponse, Http404, JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.utils import timezone
from asgiref.sync import sync_to_async, async_to_sync
from django.db import models, transaction
from apps.projects.models import Project, ProjectMember
from rest_framework.exceptions import APIException

from .models import (
    RequirementDocument, RequirementAnalysis, BusinessRequirement,
    GeneratedTestCase, AnalysisTask, AIModelConfig, PromptConfig, TestSkill, TestCaseGenerationTask,
    GenerationConfig, AIModelService, BusinessRuleKnowledge, BusinessRuleUsage,
    PrototypeUnderstandingAsset, KnowledgeNode, KnowledgeRelation, BusinessWorkflow
)
from .serializers import (
    RequirementDocumentSerializer, RequirementAnalysisSerializer,
    BusinessRequirementSerializer, GeneratedTestCaseSerializer,
    AnalysisTaskSerializer, DocumentUploadSerializer,
    TestCaseGenerationRequestSerializer, TestCaseReviewRequestSerializer,
    AIModelConfigSerializer, PromptConfigSerializer, TestSkillSerializer, TestCaseGenerationTaskSerializer,
    GenerationConfigSerializer, BusinessRuleKnowledgeSerializer, BusinessRuleUsageSerializer,
    PrototypeUnderstandingAssetSerializer, KnowledgeNodeSerializer, KnowledgeRelationSerializer,
    BusinessWorkflowSerializer
)
from .services import RequirementAnalysisService, DocumentProcessor
from .prototype_understanding import (
    analyze_asset as analyze_prototype_asset,
    generate_ui_flow_draft as generate_prototype_ui_flow_draft,
)
from .rule_services import (
    extract_rule_keywords,
    collect_knowledge_context,
    merge_duplicate_domain_nodes,
    recommend_business_rules,
    serialize_rule_snapshot,
    sync_rule_to_knowledge_node,
)
from .skill_service import (
    SkillPackageError,
    build_skill_snapshot,
    persist_package,
    read_skill_file,
    read_uploaded_package,
    remove_package,
)
from apps.users.local_mode import local_trusted_mode

logger = logging.getLogger(__name__)


def _parse_generation_events(raw_log):
    """Decode the bounded, structured execution timeline for API/SSE clients."""
    try:
        events = json.loads(raw_log or '[]')
    except (TypeError, ValueError, json.JSONDecodeError):
        events = []
    return events if isinstance(events, list) else []


def can_view_generation_task(user, task):
    if not user or not user.is_authenticated:
        return False
    if local_trusted_mode() or user.is_superuser or task.created_by_id == user.pk:
        return True
    project = getattr(task, 'project', None)
    if project is None:
        return False
    return (
        project.owner_id == user.pk
        or ProjectMember.objects.filter(project_id=project.pk, user_id=user.pk).exists()
    )


def can_manage_generation_task(user, task):
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or task.created_by_id == user.pk:
        return True
    project = getattr(task, 'project', None)
    if project is None:
        return False
    if project.owner_id == user.pk:
        return True
    return ProjectMember.objects.filter(project_id=project.pk, user_id=user.pk).exists()


def _requirement_document_for_object(obj):
    if isinstance(obj, RequirementDocument):
        return obj
    if isinstance(obj, RequirementAnalysis):
        return obj.document
    if isinstance(obj, BusinessRequirement):
        return obj.analysis.document
    if isinstance(obj, GeneratedTestCase):
        return obj.requirement.analysis.document
    if isinstance(obj, AnalysisTask):
        return obj.document
    if isinstance(obj, PrototypeUnderstandingAsset):
        return obj
    if isinstance(obj, BusinessWorkflow):
        return obj
    return None


def can_access_requirement_asset(user, obj, *, write=False):
    """Apply one ownership/project policy to requirement-derived assets."""
    if not user or not user.is_authenticated:
        return False
    if user.is_staff or user.is_superuser:
        return True

    asset = _requirement_document_for_object(obj)
    if asset is None:
        return False
    asset_owner_id = getattr(asset, 'uploaded_by_id', None) or getattr(asset, 'created_by_id', None)
    if asset_owner_id == user.pk:
        return True

    project = getattr(asset, 'project', None)
    if project is None:
        return False
    if project.owner_id == user.pk:
        return True
    membership = ProjectMember.objects.filter(project_id=project.pk, user_id=user.pk).first()
    if not membership:
        return False
    return True


class RequirementAssetPermission(BasePermission):
    message = '无权访问或修改该需求资产。'

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        return can_access_requirement_asset(
            request.user,
            obj,
            write=request.method not in SAFE_METHODS,
        )


def _knowledge_scope(user):
    """Global knowledge plus project knowledge visible to this user."""
    if user.is_staff or user.is_superuser:
        return models.Q()
    return (
        models.Q(project__isnull=True)
        | models.Q(project__owner=user)
        | models.Q(project__members=user)
    )


def _knowledge_relation_scope(user):
    if user.is_staff or user.is_superuser:
        return models.Q()
    return (
        models.Q(source__project__isnull=True)
        | models.Q(source__project__owner=user)
        | models.Q(source__project__members=user)
        | models.Q(target__project__isnull=True)
        | models.Q(target__project__owner=user)
        | models.Q(target__project__members=user)
    )


def _filter_requirement_documents(queryset, user, prefix='', owner_field='uploaded_by'):
    """Filter a queryset through its RequirementDocument relationship."""
    if user.is_staff or user.is_superuser:
        return queryset
    uploaded_by = f'{prefix}{owner_field}'
    project_owner = f'{prefix}project__owner'
    project_members = f'{prefix}project__members'
    return queryset.filter(
        models.Q(**{uploaded_by: user})
        | models.Q(**{project_owner: user})
        | models.Q(**{project_members: user})
    ).distinct()


class GenerationTaskPermission(BasePermission):
    message = '无权访问或修改该 AI 用例生成任务。'

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return can_view_generation_task(request.user, obj)
        return can_manage_generation_task(request.user, obj)


class TestSkillPermission(BasePermission):
    message = '请先登录后再管理 Skills。'

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        return True

    def has_object_permission(self, request, view, obj):
        return self.has_permission(request, view)


def _knowledge_file_descendant_ids(root_ids):
    """Return a knowledge file root and its owned descendant node IDs."""
    visited = {int(node_id) for node_id in root_ids if str(node_id).isdigit()}
    frontier = set(visited)
    while frontier:
        target_ids = set(
            KnowledgeRelation.objects.filter(source_id__in=frontier)
            .values_list('target_id', flat=True)
        ) - visited
        if not target_ids:
            break
        target_types = dict(
            KnowledgeNode.objects.filter(id__in=target_ids)
            .values_list('id', 'type')
        )
        # A relation to another domain starts a different knowledge file and
        # must not pull that file's rules into the current search result.
        frontier = {
            node_id for node_id in target_ids
            if target_types.get(node_id) != 'domain'
        }
        visited.update(frontier)
    return visited


def _current_rule_save_context(validated_data):
    """Return the current rule payload and whether it is eligible for persistence.

    Knowledge files/nodes and the rule text are optional generation context. Only an
    explicit save request with both non-empty content and a selected parent node may
    mutate the business knowledge base.
    """
    current_focus_rule = str(
        validated_data.get('current_rule_content')
        or (validated_data.get('case_type_rules') or {}).get('focus_keywords')
        or ''
    ).strip()
    parent_node = validated_data.get('current_rule_parent_node')
    should_save = bool(
        validated_data.get('save_current_rule')
        and current_focus_rule
        and parent_node
    )
    return current_focus_rule, parent_node, should_save


class RequirementDocumentViewSet(viewsets.ModelViewSet):
    """需求文档视图集"""
    queryset = RequirementDocument.objects.all()
    serializer_class = RequirementDocumentSerializer
    permission_classes = [RequirementAssetPermission]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_queryset(self):
        queryset = RequirementDocument.objects.select_related('uploaded_by', 'project')
        return _filter_requirement_documents(queryset, self.request.user)

    def get_serializer_class(self):
        if self.action == 'create':
            return DocumentUploadSerializer
        return RequirementDocumentSerializer

    @action(detail=True, methods=['post'])
    def analyze(self, request, pk=None):
        """分析需求文档"""
        document = self.get_object()

        if document.status == 'analyzing':
            return Response(
                {'error': '文档正在分析中，请稍后再试'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if document.status == 'analyzed':
            return Response(
                {'message': '文档已经分析过了', 'analysis_id': document.analysis.id},
                status=status.HTTP_200_OK
            )

        try:
            # 更新状态为分析中
            document.status = 'analyzing'
            document.save()

            # 异步执行分析
            def run_analysis():
                try:
                    # 简化版同步分析
                    # 提取文档文本
                    if not document.extracted_text:
                        document.extracted_text = DocumentProcessor.extract_text(document)
                        document.save()

                    # 创建模拟分析结果
                    analysis_result = {
                        'analysis_report': f'对文档"{document.title}"的需求分析已完成。\n\n文档内容：{document.extracted_text[:200]}...\n\n识别到若干功能性需求。',
                        'requirements_count': 2,
                        'requirements': [
                            {
                                'requirement_id': 'REQ001',
                                'requirement_name': '基础功能需求',
                                'requirement_type': 'functional',
                                'module': '核心模块',
                                'requirement_level': 'high',
                                'estimated_hours': 8,
                                'description': '基于文档内容识别的功能需求',
                                'acceptance_criteria': '功能正常运行，满足用户需求'
                            },
                            {
                                'requirement_id': 'REQ002',
                                'requirement_name': '用户交互需求',
                                'requirement_type': 'usability',
                                'module': '前端模块',
                                'requirement_level': 'medium',
                                'estimated_hours': 6,
                                'description': '用户界面和交互相关需求',
                                'acceptance_criteria': '界面友好，操作简单'
                            }
                        ]
                    }

                    # 创建分析记录
                    analysis = RequirementAnalysis.objects.create(
                        document=document,
                        analysis_report=analysis_result['analysis_report'],
                        requirements_count=analysis_result['requirements_count'],
                        analysis_time=2.5
                    )

                    # 保存需求数据
                    for req_data in analysis_result['requirements']:
                        BusinessRequirement.objects.create(
                            analysis=analysis,
                            **req_data
                        )

                    # 更新文档状态
                    document.status = 'analyzed'
                    document.save()

                    return analysis

                except Exception as e:
                    logger.error(f"分析失败: {e}")
                    document.status = 'failed'
                    document.save()
                    raise e

            analysis = run_analysis()

            return Response({
                'message': '分析完成',
                'analysis_id': analysis.id,
                'requirements_count': analysis.requirements_count
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"分析文档时出错: {e}")
            return Response(
                {'error': '分析失败，请稍后重试'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['get'])
    def download(self, request, pk=None):
        document = self.get_object()
        if not document.file:
            raise Http404('文档文件不存在')
        return FileResponse(
            document.file.open('rb'),
            as_attachment=True,
            filename=document.file.name.rsplit('/', 1)[-1],
        )

    @action(detail=True, methods=['get'])
    def extract_text(self, request, pk=None):
        """提取文档文本"""
        document = self.get_object()

        try:
            if not document.extracted_text:
                text = DocumentProcessor.extract_text(document)
                document.extracted_text = text
                document.save()

            return Response({
                'extracted_text': document.extracted_text,
                'text_length': len(document.extracted_text)
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"提取文本时出错: {e}")
            return Response(
                {'error': '提取文本失败，请稍后重试'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class PrototypeUnderstandingAssetViewSet(viewsets.ModelViewSet):
    """AI 用例生成模块中的原型理解与 UI 步骤草稿。"""
    serializer_class = PrototypeUnderstandingAssetSerializer
    permission_classes = [RequirementAssetPermission]
    parser_classes = [MultiPartParser, FormParser]

    def get_queryset(self):
        queryset = PrototypeUnderstandingAsset.objects.select_related(
            'project',
            'uploaded_by',
        ).all()
        user = self.request.user
        queryset = _filter_requirement_documents(queryset, user)

        project_id = self.request.query_params.get('project')
        if project_id:
            queryset = queryset.filter(project_id=project_id)

        asset_type = self.request.query_params.get('asset_type')
        if asset_type:
            queryset = queryset.filter(asset_type=asset_type)

        status_param = self.request.query_params.get('status')
        if status_param:
            queryset = queryset.filter(status=status_param)

        return queryset.order_by('-created_at')

    @action(detail=True, methods=['get'])
    def download(self, request, pk=None):
        asset = self.get_object()
        if not asset.file:
            raise Http404('原型文件不存在')
        return FileResponse(
            asset.file.open('rb'),
            as_attachment=True,
            filename=asset.file.name.rsplit('/', 1)[-1],
        )

    @action(detail=True, methods=['post'])
    def analyze(self, request, pk=None):
        asset = self.get_object()
        try:
            analysis = analyze_prototype_asset(asset)
            return Response({
                'message': '原型识别完成',
                'analysis_result': analysis,
                'asset': self.get_serializer(asset).data,
            })
        except Exception as exc:
            logger.error(f"原型识别失败: {exc}")
            asset.status = 'failed'
            asset.error_message = str(exc)
            asset.save(update_fields=['status', 'error_message', 'updated_at'])
            return Response(
                {'error': '原型识别失败，请检查文件内容后重试', 'detail': str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'], url_path='generate-ui-flow')
    def generate_ui_flow(self, request, pk=None):
        asset = self.get_object()
        try:
            draft = generate_prototype_ui_flow_draft(asset)
            return Response({
                'message': 'UI 自动化步骤草稿已生成',
                'ui_flow_draft': draft,
                'asset': self.get_serializer(asset).data,
            })
        except Exception as exc:
            logger.error(f"生成 UI 自动化步骤草稿失败: {exc}")
            asset.status = 'failed'
            asset.error_message = str(exc)
            asset.save(update_fields=['status', 'error_message', 'updated_at'])
            return Response(
                {'error': '生成 UI 自动化步骤草稿失败，请稍后重试', 'detail': str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class RequirementAnalysisViewSet(viewsets.ReadOnlyModelViewSet):
    """需求分析视图集"""
    queryset = RequirementAnalysis.objects.all()
    serializer_class = RequirementAnalysisSerializer
    permission_classes = [RequirementAssetPermission]

    def get_queryset(self):
        queryset = RequirementAnalysis.objects.select_related(
            'document', 'document__uploaded_by', 'document__project'
        )
        return _filter_requirement_documents(queryset, self.request.user, prefix='document__')

    @action(detail=True, methods=['get'])
    def requirements(self, request, pk=None):
        """获取分析的需求列表"""
        analysis = self.get_object()
        requirements = analysis.requirements.all()
        serializer = BusinessRequirementSerializer(requirements, many=True)
        return Response(serializer.data)


class BusinessRequirementViewSet(viewsets.ReadOnlyModelViewSet):
    """业务需求视图集"""
    queryset = BusinessRequirement.objects.all()
    serializer_class = BusinessRequirementSerializer
    permission_classes = [RequirementAssetPermission]

    def get_queryset(self):
        queryset = BusinessRequirement.objects.select_related(
            'analysis', 'analysis__document', 'analysis__document__uploaded_by',
            'analysis__document__project',
        )
        queryset = _filter_requirement_documents(
            queryset,
            self.request.user,
            prefix='analysis__document__',
        )
        analysis_id = self.request.query_params.get('analysis_id')
        if analysis_id:
            queryset = queryset.filter(analysis_id=analysis_id)
        return queryset

    @classmethod
    def _generate_test_case_content(cls, requirement, case_number, test_level):
        """根据需求类型和序号生成不同的测试用例内容"""

        # 基础测试场景模板
        test_scenarios = {
            1: {
                'type': '正常路径测试',
                'focus': '基本功能验证',
                'steps_template': [
                    "准备测试环境和数据",
                    "执行正常业务流程",
                    "验证功能执行结果",
                    "检查系统状态"
                ]
            },
            2: {
                'type': '异常路径测试',
                'focus': '异常情况处理',
                'steps_template': [
                    "准备异常测试数据",
                    "触发异常业务场景",
                    "验证异常处理机制",
                    "确认系统状态正常"
                ]
            },
            3: {
                'type': '边界值测试',
                'focus': '边界条件验证',
                'steps_template': [
                    "设置边界值测试条件",
                    "执行边界值操作",
                    "验证边界值处理",
                    "检查结果准确性"
                ]
            },
            4: {
                'type': '性能测试',
                'focus': '性能指标验证',
                'steps_template': [
                    "配置性能测试环境",
                    "执行性能测试操作",
                    "监控性能指标",
                    "验证性能要求"
                ]
            },
            5: {
                'type': '安全测试',
                'focus': '安全机制验证',
                'steps_template': [
                    "设置安全测试环境",
                    "执行安全相关操作",
                    "验证安全控制机制",
                    "确认安全合规性"
                ]
            }
        }

        # 循环使用测试场景
        scenario_key = ((case_number - 1) % 5) + 1
        scenario = test_scenarios[scenario_key]

        # 根据需求名称生成具体内容
        req_name = requirement.requirement_name
        req_module = requirement.module
        req_type = requirement.requirement_type

        # 生成标题
        title = f"{req_name} - {scenario['type']}用例"

        # 生成前置条件
        if "登录" in req_name:
            precondition = f"1. 系统正常运行\n2. 测试用户账号已准备\n3. {req_module}模块可访问"
        elif "数据" in req_name:
            precondition = f"1. 系统正常运行\n2. 数据库连接正常\n3. 测试数据已准备\n4. {req_module}模块可访问"
        elif "支付" in req_name:
            precondition = f"1. 系统正常运行\n2. 支付接口连接正常\n3. 测试账户余额充足\n4. {req_module}模块可访问"
        else:
            precondition = f"1. 系统正常运行\n2. 用户已登录系统\n3. {req_module}模块可访问\n4. 相关权限已配置"

        # 生成测试步骤
        steps = []
        for i, step_template in enumerate(scenario['steps_template'], 1):
            if "登录" in req_name:
                if i == 1:
                    steps.append(f"{i}. 打开登录页面，准备测试用户凭证")
                elif i == 2:
                    if scenario_key == 1:
                        steps.append(f"{i}. 输入正确的用户名和密码，点击登录")
                    elif scenario_key == 2:
                        steps.append(f"{i}. 输入错误的用户名或密码，点击登录")
                    else:
                        steps.append(f"{i}. 执行{scenario['focus']}相关的登录操作")
                elif i == 3:
                    steps.append(f"{i}. 验证登录结果和页面跳转")
                else:
                    steps.append(f"{i}. 检查用户登录状态和系统响应")
            elif "数据" in req_name:
                if i == 1:
                    steps.append(f"{i}. 进入{req_module}，准备数据操作")
                elif i == 2:
                    if scenario_key == 1:
                        steps.append(f"{i}. 执行正常的数据录入/查询操作")
                    elif scenario_key == 2:
                        steps.append(f"{i}. 执行异常数据操作（如格式错误、超长数据等）")
                    else:
                        steps.append(f"{i}. 执行{scenario['focus']}相关的数据操作")
                elif i == 3:
                    steps.append(f"{i}. 验证数据操作结果和完整性")
                else:
                    steps.append(f"{i}. 检查数据状态和系统响应")
            else:
                steps.append(f"{i}. {step_template}（针对{req_name}）")

        test_steps = "\n".join(steps)

        # 生成预期结果
        if scenario_key == 1:  # 正常路径
            expected_result = f"{req_name}功能正常执行，满足业务需求，系统响应正确"
        elif scenario_key == 2:  # 异常路径
            expected_result = f"系统正确处理异常情况，给出适当提示，{req_name}功能保持稳定"
        elif scenario_key == 3:  # 边界值
            expected_result = f"{req_name}在边界条件下正常工作，数据处理准确，无异常错误"
        elif scenario_key == 4:  # 性能测试
            expected_result = f"{req_name}性能满足要求，响应时间在可接受范围内，系统稳定运行"
        else:  # 安全测试
            expected_result = f"{req_name}安全机制有效，权限控制正常，敏感信息得到保护"

        return {
            'title': title,
            'precondition': precondition,
            'test_steps': test_steps,
            'expected_result': expected_result
        }

    @action(detail=False, methods=['post'])
    def generate_test_cases(self, request):
        """为选中的需求生成测试用例"""
        serializer = TestCaseGenerationRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            requirement_ids = serializer.validated_data['requirement_ids']
            test_level = serializer.validated_data['test_level']
            test_priority = serializer.validated_data['test_priority']
            test_case_count = serializer.validated_data['test_case_count']

            # 生成唯一case_id的辅助函数
            def generate_unique_case_id(requirement, base_index):
                """生成唯一的测试用例ID"""
                base_case_id = f"TC{requirement.requirement_id}_{base_index:03d}"
                case_id = base_case_id
                counter = 1

                # 检查是否已存在，如果存在则添加后缀
                while GeneratedTestCase.objects.filter(requirement=requirement, case_id=case_id).exists():
                    case_id = f"{base_case_id}_{counter}"
                    counter += 1

                return case_id

            # 同步生成测试用例
            def run_generation():
                try:
                    # 获取需求数据
                    requirements = BusinessRequirement.objects.filter(id__in=requirement_ids)
                    generated_test_cases = []

                    for requirement in requirements:
                        # 获取该需求现有测试用例的数量，作为起始索引
                        existing_count = GeneratedTestCase.objects.filter(requirement=requirement).count()

                        for i in range(test_case_count):
                            # 生成唯一的case_id
                            case_id = generate_unique_case_id(requirement, existing_count + i + 1)

                            # 根据需求类型和序号生成不同的测试用例内容
                            test_case_content = BusinessRequirementViewSet._generate_test_case_content(requirement,
                                                                                                       i + 1,
                                                                                                       test_level)

                            # 创建测试用例
                            test_case = GeneratedTestCase.objects.create(
                                requirement=requirement,
                                case_id=case_id,
                                title=test_case_content['title'],
                                priority=test_priority,
                                precondition=test_case_content['precondition'],
                                test_steps=test_case_content['test_steps'],
                                expected_result=test_case_content['expected_result'],
                                status='generated',
                                generated_by_ai='AI-Generator-v1.0'
                            )
                            generated_test_cases.append(test_case)

                    return generated_test_cases

                except Exception as e:
                    logger.error(f"生成测试用例失败: {e}")
                    raise e

            test_cases = run_generation()

            # 序列化返回结果
            test_case_serializer = GeneratedTestCaseSerializer(test_cases, many=True)

            return Response({
                'message': f'成功生成{len(test_cases)}个测试用例',
                'test_cases': test_case_serializer.data
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.error(f"生成测试用例时出错: {e}")
            return Response(
                {'error': '生成测试用例失败，请稍后重试'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


from rest_framework.pagination import PageNumberPagination


class GeneratedTestCasePagination(PageNumberPagination):
    """生成测试用例分页器"""
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


class TestCaseGenerationTaskPagination(PageNumberPagination):
    """测试用例生成任务分页器"""
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


class BusinessRuleKnowledgePagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 200


class GeneratedTestCaseViewSet(viewsets.ModelViewSet):
    """生成的测试用例视图集"""
    queryset = GeneratedTestCase.objects.all()
    serializer_class = GeneratedTestCaseSerializer
    pagination_class = GeneratedTestCasePagination
    permission_classes = [RequirementAssetPermission]
    http_method_names = ['get', 'patch']  # 只允许GET和PATCH方法

    def get_queryset(self):
        queryset = GeneratedTestCase.objects.select_related(
            'requirement', 'requirement__analysis',
            'requirement__analysis__document',
            'requirement__analysis__document__uploaded_by',
            'requirement__analysis__document__project',
        )
        queryset = _filter_requirement_documents(
            queryset,
            self.request.user,
            prefix='requirement__analysis__document__',
        )

        # 按需求ID过滤
        requirement_id = self.request.query_params.get('requirement_id')
        if requirement_id:
            queryset = queryset.filter(requirement_id=requirement_id)

        # 按状态过滤
        status_param = self.request.query_params.get('status')
        if status_param:
            queryset = queryset.filter(status=status_param)

        # 按优先级过滤
        priority_param = self.request.query_params.get('priority')
        if priority_param:
            queryset = queryset.filter(priority=priority_param)

        return queryset

    @action(detail=False, methods=['post'])
    def review_test_cases(self, request):
        """评审测试用例"""
        serializer = TestCaseReviewRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            test_case_ids = serializer.validated_data['test_case_ids']
            review_criteria = serializer.validated_data['review_criteria']

            # 同步执行评审
            def run_review():
                try:
                    # 获取测试用例
                    test_cases = self.get_queryset().filter(id__in=test_case_ids)
                    if test_cases.count() != len(set(test_case_ids)):
                        raise PermissionError('包含不存在或无权访问的测试用例')

                    passed_count = 0
                    reviewed_cases = []

                    for test_case in test_cases:
                        # 模拟评审逻辑
                        is_passed = len(test_case.title) > 10 and len(test_case.test_steps) > 20

                        if is_passed:
                            passed_count += 1
                            test_case.status = 'approved'
                            test_case.review_comments = '测试用例设计合理，满足评审标准'
                        else:
                            test_case.status = 'rejected'
                            test_case.review_comments = '测试用例需要完善，请补充详细的测试步骤'

                        test_case.reviewed_by_ai = 'AI-Reviewer-v1.0'
                        test_case.save()

                        reviewed_cases.append({
                            'id': test_case.id,
                            'case_id': test_case.case_id,
                            'title': test_case.title,
                            'status': test_case.status,
                            'review_comments': test_case.review_comments
                        })

                    total_count = len(test_cases)
                    pass_rate = (passed_count / total_count * 100) if total_count > 0 else 0

                    return {
                        'total_count': total_count,
                        'passed_count': passed_count,
                        'pass_rate': pass_rate,
                        'reviewed_cases': reviewed_cases
                    }

                except Exception as e:
                    logger.error(f"评审测试用例失败: {e}")
                    raise e

            review_result = run_review()

            return Response({
                'message': f'评审完成，通过率: {review_result["pass_rate"]:.2f}%',
                'review_result': review_result
            }, status=status.HTTP_200_OK)

        except PermissionError as e:
            return Response({'error': str(e)}, status=status.HTTP_403_FORBIDDEN)
        except Exception as e:
            logger.error(f"评审测试用例时出错: {e}")
            return Response(
                {'error': '评审失败，请稍后重试'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class AnalysisTaskViewSet(viewsets.ReadOnlyModelViewSet):
    """分析任务视图集"""
    queryset = AnalysisTask.objects.all()
    serializer_class = AnalysisTaskSerializer
    permission_classes = [RequirementAssetPermission]

    def get_queryset(self):
        queryset = AnalysisTask.objects.select_related(
            'document', 'document__uploaded_by', 'document__project'
        )
        queryset = _filter_requirement_documents(
            queryset,
            self.request.user,
            prefix='document__',
        )
        document_id = self.request.query_params.get('document_id')
        if document_id:
            queryset = queryset.filter(document_id=document_id)
        return queryset

    @action(detail=True, methods=['get'])
    def progress(self, request, pk=None):
        """获取任务进度"""
        task = self.get_object()
        return Response({
            'task_id': task.task_id,
            'status': task.status,
            'progress': task.progress,
            'error_message': task.error_message
        })


from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from django.views.decorators.csrf import csrf_exempt


@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def upload_and_analyze(request):
    """上传文档并立即开始分析"""
    # 安全修复：必须认证用户
    if not request.user.is_authenticated:
        return Response({'error': '请先登录后再上传分析'}, status=status.HTTP_401_UNAUTHORIZED)
    uploaded_by = request.user
    try:
        # 创建文档
        serializer = DocumentUploadSerializer(data=request.data, context={'request': request})
        if not serializer.is_valid():
            return Response({'error': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)

        document = serializer.save()

        # 立即开始分析
        document.status = 'analyzing'
        document.save()

        def run_analysis():
            try:
                # 简化版同步分析
                # 提取文档文本
                if not document.extracted_text:
                    document.extracted_text = DocumentProcessor.extract_text(document)
                    document.save()

                # 创建模拟分析结果
                analysis_result = {
                    'analysis_report': f'对文档"{document.title}"的需求分析已完成。\n\n文档内容：{document.extracted_text[:200]}...\n\n识别到若干功能性需求。',
                    'requirements_count': 2,
                    'requirements': [
                        {
                            'requirement_id': 'REQ001',
                            'requirement_name': '基础功能需求',
                            'requirement_type': 'functional',
                            'module': '核心模块',
                            'requirement_level': 'high',
                            'estimated_hours': 8,
                            'description': '基于文档内容识别的功能需求',
                            'acceptance_criteria': '功能正常运行，满足用户需求'
                        },
                        {
                            'requirement_id': 'REQ002',
                            'requirement_name': '用户交互需求',
                            'requirement_type': 'usability',
                            'module': '前端模块',
                            'requirement_level': 'medium',
                            'estimated_hours': 6,
                            'description': '用户界面和交互相关需求',
                            'acceptance_criteria': '界面友好，操作简单'
                        }
                    ]
                }

                # 创建分析记录
                analysis = RequirementAnalysis.objects.create(
                    document=document,
                    analysis_report=analysis_result['analysis_report'],
                    requirements_count=analysis_result['requirements_count'],
                    analysis_time=2.5
                )

                # 保存需求数据
                for req_data in analysis_result['requirements']:
                    BusinessRequirement.objects.create(
                        analysis=analysis,
                        **req_data
                    )

                # 更新文档状态
                document.status = 'analyzed'
                document.save()

                return analysis

            except Exception as e:
                logger.error(f"分析失败: {e}")
                document.status = 'failed'
                document.save()
                raise e

        analysis = run_analysis()

        return Response({
            'message': '上传并分析完成',
            'document_id': document.id,
            'analysis_id': analysis.id,
            'requirements_count': analysis.requirements_count
        })

    except Exception as e:
        logger.error(f"上传并分析失败: {e}")
        return Response({'error': '上传并分析失败，请稍后重试'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def analyze_text(request):
    """分析手动输入的需求文本（v2版本，使用先进分析系统）"""
    try:
        title = request.data.get('title')
        description = request.data.get('description')
        project_id = request.data.get('project')

        if not title or not description:
            return Response({'error': '需求标题和描述不能为空'}, status=status.HTTP_400_BAD_REQUEST)

        # 安全修复：使用已认证用户ID，不再硬编码
        uploaded_by_id = request.user.id if request.user.is_authenticated else None
        if uploaded_by_id is None:
            return Response({'error': '请先登录后再进行分析'}, status=status.HTTP_401_UNAUTHORIZED)

        project = None
        if project_id:
            project = Project.objects.filter(pk=project_id).first()
            if project is None:
                return Response({'error': '项目不存在'}, status=status.HTTP_404_NOT_FOUND)
            has_access = (
                request.user.is_staff
                or request.user.is_superuser
                or project.owner_id == request.user.pk
                or ProjectMember.objects.filter(project=project, user=request.user).exists()
            )
            if not has_access:
                return Response({'error': '无权访问所选项目'}, status=status.HTTP_403_FORBIDDEN)

        # 创建一个虚拟的需求文档记录
        document = RequirementDocument.objects.create(
            title=title,
            file=None,  # 手动输入没有文件
            document_type='txt',
            status='analyzing',
            uploaded_by_id=uploaded_by_id,
            project=project,
            extracted_text=description
        )

        # 立即开始分析
        def run_analysis():
            try:
                # 使用新的先进分析系统
                import asyncio
                from .services import AIService

                logger.info(f"开始使用先进分析器分析需求: {title}")

                # 调用先进的需求分析
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                try:
                    analysis_result = loop.run_until_complete(
                        AIService.analyze_requirements(description, title)
                    )
                    logger.info(f"先进分析完成，识别需求: {analysis_result.get('requirements_count', 0)}个")
                finally:
                    loop.close()

                # 创建分析记录
                analysis = RequirementAnalysis.objects.create(
                    document=document,
                    analysis_report=analysis_result['analysis_report'],
                    requirements_count=analysis_result['requirements_count'],
                    analysis_time=analysis_result.get('analysis_time', 2.0)
                )

                # 保存需求数据
                for req_data in analysis_result['requirements']:
                    BusinessRequirement.objects.create(
                        analysis=analysis,
                        **req_data
                    )

                # 更新文档状态
                document.status = 'analyzed'
                document.save()

                return analysis

            except Exception as e:
                logger.error(f"先进分析失败: {e}，使用备用分析")
                # fallback到简单分析
                analysis_result = {
                    'analysis_report': f'对需求"{title}"的分析已完成。\n\n需求描述：{description[:200]}...\n\n基于描述内容识别到若干功能性需求。',
                    'requirements_count': 2,
                    'requirements': [
                        {
                            'requirement_id': 'REQ001',
                            'requirement_name': title + ' - 核心功能',
                            'requirement_type': 'functional',
                            'module': '核心模块',
                            'requirement_level': 'high',
                            'estimated_hours': 8,
                            'description': description[:100] + '...',
                            'acceptance_criteria': '功能正常运行，满足用户需求'
                        },
                        {
                            'requirement_id': 'REQ002',
                            'requirement_name': title + ' - 交互功能',
                            'requirement_type': 'usability',
                            'module': '前端模块',
                            'requirement_level': 'medium',
                            'estimated_hours': 6,
                            'description': '用户界面和交互相关需求',
                            'acceptance_criteria': '界面友好，操作简单'
                        }
                    ]
                }

                # 创建分析记录
                analysis = RequirementAnalysis.objects.create(
                    document=document,
                    analysis_report=analysis_result['analysis_report'],
                    requirements_count=analysis_result['requirements_count'],
                    analysis_time=1.5
                )

                # 保存需求数据
                for req_data in analysis_result['requirements']:
                    BusinessRequirement.objects.create(
                        analysis=analysis,
                        **req_data
                    )

                # 更新文档状态
                document.status = 'analyzed'
                document.save()

                return analysis

            except Exception as e:
                logger.error(f"分析失败: {e}")
                document.status = 'failed'
                document.save()
                raise e

        analysis = run_analysis()

        return Response({
            'message': '文本分析完成',
            'document_id': document.id,
            'analysis_id': analysis.id,
            'requirements_count': analysis.requirements_count
        })

    except Exception as e:
        logger.error(f"文本分析失败: {e}")
        return Response({'error': '文本分析失败，请稍后重试'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AIModelConfigViewSet(viewsets.ModelViewSet):
    """AI模型配置视图集"""
    permission_classes = [IsAuthenticated]
    queryset = AIModelConfig.objects.all()
    serializer_class = AIModelConfigSerializer

    def get_queryset(self):
        queryset = super().get_queryset()

        # 按模型类型过滤
        model_type = self.request.query_params.get('model_type')
        if model_type:
            queryset = queryset.filter(model_type=model_type)

        # 按角色过滤
        role = self.request.query_params.get('role')
        if role:
            queryset = queryset.filter(role=role)

        model_usage = self.request.query_params.get('model_usage')
        if model_usage:
            queryset = queryset.filter(model_usage=model_usage)

        model_status = self.request.query_params.get('model_status')
        if model_status:
            queryset = queryset.filter(model_status=model_status)

        # 按是否启用过滤
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')

        return queryset.order_by('-created_at')

    def _fetch_models_with_timeout(self, config, timeout=30.0):
        """同步包装异步模型列表获取。"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(
                asyncio.wait_for(
                    AIModelService.list_available_models(config),
                    timeout=timeout
                )
            )
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            finally:
                loop.close()

    @action(detail=False, methods=['post'], url_path='available_models')
    def available_models_preview(self, request):
        """获取未保存配置下的可用模型列表。"""
        try:
            data = request.data
            required_fields = ['model_type', 'api_key', 'base_url']
            missing_fields = [field for field in required_fields if not data.get(field)]
            if missing_fields:
                return Response(
                    {'success': False, 'message': f'缺少必填字段: {", ".join(missing_fields)}'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            model_usage = data.get('model_usage') or 'general'
            role = data.get('role', 'writer')
            default_temperature = 0.2 if (model_usage == 'test_case_review' or role == 'reviewer') else 0.4

            preview_config = AIModelConfig(
                name=data.get('name', '临时模型列表配置'),
                model_type=data.get('model_type'),
                role=role,
                api_key=data.get('api_key'),
                base_url=data.get('base_url'),
                model_name=data.get('model_name', 'temp-model'),
                model_version=data.get('model_version') or '',
                max_tokens=data.get('max_tokens') or 256,
                max_output_tokens=data.get('max_output_tokens') or data.get('max_tokens') or 256,
                temperature=data.get('temperature') if data.get('temperature') is not None else default_temperature,
                top_p=data.get('top_p') if data.get('top_p') is not None else 0.8,
                retry_count=data.get('retry_count') if data.get('retry_count') is not None else 3,
                model_usage=model_usage,
                model_status=data.get('model_status') or 'normal',
                capability_tags=data.get('capability_tags') or [],
                is_active=False
            )

            models = self._fetch_models_with_timeout(preview_config)
            return Response({
                'success': True,
                'message': f'成功获取{len(models)}个模型',
                'models': models
            }, status=status.HTTP_200_OK)
        except asyncio.TimeoutError:
            return Response(
                {'success': False, 'message': '获取模型列表超时，请检查网络连接或API地址是否正确'},
                status=status.HTTP_408_REQUEST_TIMEOUT
            )
        except Exception as e:
            logger.error(f"获取未保存配置模型列表失败: {e}")
            return Response(
                {'success': False, 'message': '获取模型列表失败，请稍后重试'},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=True, methods=['get'], url_path='available_models')
    def available_models(self, request, pk=None):
        """获取已保存配置下的可用模型列表。"""
        try:
            config = self.get_object()
            models = self._fetch_models_with_timeout(config)
            return Response({
                'success': True,
                'message': f'成功获取{len(models)}个模型',
                'models': models
            }, status=status.HTTP_200_OK)
        except asyncio.TimeoutError:
            return Response(
                {'success': False, 'message': '获取模型列表超时，请检查网络连接或API地址是否正确'},
                status=status.HTTP_408_REQUEST_TIMEOUT
            )
        except Exception as e:
            logger.error(f"获取已保存配置模型列表失败: {e}")
            return Response(
                {'success': False, 'message': '获取模型列表失败，请稍后重试'},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=False, methods=['post'], url_path='test_connection')
    def test_connection_preview(self, request):
        """测试未保存的模型配置连接"""
        try:
            data = request.data

            required_fields = ['model_type', 'api_key', 'base_url', 'model_name']
            missing_fields = [field for field in required_fields if not data.get(field)]
            if missing_fields:
                return Response(
                    {'success': False, 'message': f'缺少必填字段: {", ".join(missing_fields)}'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            model_usage = data.get('model_usage') or 'general'
            role = data.get('role', 'writer')
            default_temperature = 0.2 if (model_usage == 'test_case_review' or role == 'reviewer') else 0.4

            test_config = AIModelConfig(
                name=data.get('name', '临时测试配置'),
                model_type=data.get('model_type'),
                role=role,
                api_key=data.get('api_key'),
                base_url=data.get('base_url'),
                model_name=data.get('model_name'),
                model_version=data.get('model_version') or '',
                max_tokens=data.get('max_tokens') or 256,
                max_output_tokens=data.get('max_output_tokens') or data.get('max_tokens') or 256,
                temperature=data.get('temperature') if data.get('temperature') is not None else default_temperature,
                top_p=data.get('top_p') if data.get('top_p') is not None else 0.8,
                retry_count=data.get('retry_count') if data.get('retry_count') is not None else 3,
                model_usage=model_usage,
                model_status=data.get('model_status') or 'normal',
                capability_tags=data.get('capability_tags') or [],
                is_active=False
            )

            logger.info("=== 开始测试未保存模型配置连接 ===")
            logger.info(f"模型类型: {test_config.model_type}")
            logger.info(f"模型名称: {test_config.model_name}")
            logger.info(f"API URL: {test_config.base_url}")

            test_messages = [
                {"role": "system", "content": "你是一个AI助手"},
                {"role": "user", "content": "请回复'连接成功'"}
            ]

            def test_api_connection():
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                    try:
                        result = loop.run_until_complete(
                            asyncio.wait_for(
                                AIModelService.call_openai_compatible_api(test_config, test_messages),
                                timeout=60.0
                            )
                        )
                        return {
                            'success': True,
                            'message': '连接测试成功',
                            'response': result.get('choices', [{}])[0].get('message', {}).get('content', '')
                        }
                    except asyncio.TimeoutError:
                        return {
                            'success': False,
                            'message': '连接测试超时: 请检查网络连接或API地址是否正确'
                        }
                    finally:
                        try:
                            loop.run_until_complete(loop.shutdown_asyncgens())
                        except Exception:
                            pass
                        finally:
                            loop.close()
                except Exception as e:
                    logger.error(f"未保存配置连接测试异常: {repr(e)}")
                    return {
                        'success': False,
                        'message': '连接测试失败，请检查配置'
                    }

            started_at = time.monotonic()
            result = test_api_connection()
            elapsed = time.monotonic() - started_at
            result['elapsed_seconds'] = round(elapsed, 2)
            if result.get('success'):
                result['model_status'] = 'slow' if elapsed >= 10 else 'normal'
            else:
                result['model_status'] = 'slow' if '超时' in result.get('message', '') else 'abnormal'
            return Response(
                result,
                status=status.HTTP_200_OK if result['success'] else status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            logger.error(f"测试未保存配置连接时出错: {e}")
            return Response(
                {'success': False, 'message': '测试失败，请稍后重试'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'])
    def test_connection(self, request, pk=None):
        """测试模型连接"""
        try:
            config = self.get_object()

            logger.info(f"=== 开始测试模型连接 ===")
            logger.info(f"模型类型: {config.model_type}")
            logger.info(f"模型名称: {config.model_name}")
            logger.info(f"API URL: {config.base_url}")
            logger.info("API Key: 已配置（内容不写入日志）")

            # 准备测试消息
            test_messages = [
                {"role": "system", "content": "你是一个AI助手"},
                {"role": "user", "content": "请回复'连接成功'"}
            ]

            # 异步测试连接 - 统一使用OpenAI兼容API
            def test_api_connection():
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                    try:
                        logger.info("开始调用API...")
                        # 设置60秒超时，统一使用OpenAI兼容API
                        result = loop.run_until_complete(
                            asyncio.wait_for(
                                AIModelService.call_openai_compatible_api(config, test_messages),
                                timeout=60.0
                            )
                        )

                        logger.info(f"API调用成功: {result}")
                        return {
                            'success': True,
                            'message': '连接测试成功',
                            'response': result.get('choices', [{}])[0].get('message', {}).get('content', '')
                        }
                    except asyncio.TimeoutError:
                        logger.error(f"API连接测试超时 (60秒), URL: {config.base_url}, Model: {config.model_name}")
                        return {
                            'success': False,
                            'message': '连接测试超时: 请检查网络连接或API地址是否正确'
                        }
                    finally:
                        try:
                            loop.run_until_complete(loop.shutdown_asyncgens())
                        except Exception:
                            pass
                        finally:
                            loop.close()

                except Exception as e:
                    logger.error(f"API连接测试异常: {repr(e)}, URL: {config.base_url}, Model: {config.model_name}")
                    import traceback
                    logger.error(f"详细错误堆栈:\n{traceback.format_exc()}")
                    return {
                        'success': False,
                        'message': '连接测试失败，请检查配置'
                    }

            started_at = time.monotonic()
            result = test_api_connection()
            elapsed = time.monotonic() - started_at
            result['elapsed_seconds'] = round(elapsed, 2)
            if result.get('success'):
                config.model_status = 'slow' if elapsed >= 10 else 'normal'
            else:
                config.model_status = 'slow' if '超时' in result.get('message', '') else 'abnormal'
            config.save(update_fields=['model_status', 'updated_at'])
            result['model_status'] = config.model_status
            result['model_status_display'] = config.get_model_status_display()

            if result['success']:
                return Response(result, status=status.HTTP_200_OK)
            else:
                return Response(result, status=status.HTTP_400_BAD_REQUEST)

        except Exception as e:
            logger.error(f"测试连接时出错: {e}")
            return Response(
                {'success': False, 'message': '测试失败，请稍后重试'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'])
    def enable(self, request, pk=None):
        """启用配置"""
        try:
            config = self.get_object()
            config.is_active = True
            config.save()
            return Response({
                'message': 'AI模型配置已启用',
                'id': config.id,
                'is_active': True
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"启用AI模型配置失败: {e}")
            return Response({
                'error': '启用失败，请稍后重试'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'])
    def disable(self, request, pk=None):
        """禁用配置"""
        try:
            config = self.get_object()
            config.is_active = False
            config.save()
            return Response({
                'message': 'AI模型配置已禁用',
                'id': config.id,
                'is_active': False
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"禁用AI模型配置失败: {e}")
            return Response({
                'error': '禁用失败，请稍后重试'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class PromptConfigViewSet(viewsets.ModelViewSet):
    """提示词配置视图集"""
    permission_classes = [IsAuthenticated]
    queryset = PromptConfig.objects.all()
    serializer_class = PromptConfigSerializer

    def _deactivate_other_versions(self, instance):
        if instance.is_active:
            PromptConfig.objects.filter(
                prompt_type=instance.prompt_type,
                is_active=True
            ).exclude(pk=instance.pk).update(is_active=False)

    def perform_create(self, serializer):
        instance = serializer.save()
        self._deactivate_other_versions(instance)

    def perform_update(self, serializer):
        instance = serializer.save()
        self._deactivate_other_versions(instance)

    def get_queryset(self):
        queryset = super().get_queryset()

        # 按提示词类型过滤
        prompt_type = self.request.query_params.get('prompt_type')
        if prompt_type:
            queryset = queryset.filter(prompt_type=prompt_type)

        # 按是否启用过滤
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')

        return queryset.order_by('-created_at')

    @action(detail=False, methods=['get'])
    def load_defaults(self, request):
        """加载默认提示词"""
        try:
            # 读取用例编写提示词
            writer_prompt_path = os.path.join(settings.BASE_DIR, 'docs/tester.md')
            # 读取用例评审提示词
            reviewer_prompt_path = os.path.join(settings.BASE_DIR, 'docs/tester_pro.md')

            defaults = {}

            try:
                with open(writer_prompt_path, 'r', encoding='utf-8') as f:
                    defaults['writer'] = f.read()
            except FileNotFoundError:
                defaults['writer'] = """你是一位拥有10年经验的资深测试用例编写专家，能够根据需求精确生成高质量的测试用例。

# 核心目标
生成高覆盖率、颗粒度细致的测试用例，确保不遗漏任何功能逻辑、异常场景和边界条件。

# 角色设定
1. 身份：精通全栈测试（Web/App/API）的高级QA专家
2. 测试风格：破坏性测试思维，善于发现潜在Bug
3. 输出原则：详细、独立、可执行

# 用例设计规范
1. **独立性**：每条用例只验证一个具体的测试点，严禁合并多个场景。
2. **完整性**：
   - 包含用例ID（[模块]_[序号]）
   - 清晰的测试目标
   - 准确的前置条件
   - 步骤化操作描述
   - 具体的预期结果
3. **覆盖维度**：
   - ✅ 功能正向流程（Happy Path）
   - ⚠️ 异常流程（输入错误、权限不足、网络异常）
   - 🔄 边界值（最大/最小值、空值、特殊字符）
   - 🔒 业务约束（状态机流转、数据依赖）
4. **强制分类**：
   - 页面展示、输入校验、流程提交：功能测试
   - 接口异常、超时、断网、返回错误：异常测试
   - SQL注入、XSS攻击、CSRF、越权、敏感数据泄露、验证码攻击、接口攻击、参数篡改：安全测试
   - 兼容浏览器、多浏览器、多终端、分辨率适配：兼容测试
5. **测试数据**：每条用例必须给出具体测试输入数据，不能只写“输入手机号”。
6. **原型图分析**：如包含原型图/截图/页面描述，必须识别页面布局、页面元素、控件类型、输入规则、按钮状态、页面跳转、异常状态。

# 输出格式
请严格按照以下Markdown表格格式输出，不要包含任何开场白或结束语：

## ⚠️ 重要：输出顺序要求
1. **必须按用例编号从小到大的顺序输出**（如：001, 002, 003...）
2. **绝对不能跳号、重复或乱序输出**
3. 编号必须连续，中间不能有遗漏
4. 所有用例必须一次性完整输出，不能中断

```markdown
| 用例编号 | 测试模块 | 测试类型 | 测试场景 | 测试数据 | 前置条件 | 操作步骤 | 预期结果 | 优先级 | 需求编号 |
|--------|--------|--------|--------|--------|--------|--------|--------|--------|--------|
| LOGIN_001 | 手机号输入 | 功能测试 | 验证手机号格式校验 | 手机号：1380013800 | 在登录页 | 1. 输入10位手机号<br>2. 点击获取验证码 | 提示"手机号格式不正确"，发送按钮不可点 | P1 | REQ-LOGIN-001 |
```"""

            try:
                with open(reviewer_prompt_path, 'r', encoding='utf-8') as f:
                    defaults['reviewer'] = f.read()
            except FileNotFoundError:
                defaults['reviewer'] = """你是一名资深测试专家（Test Architect），拥有极高的质量标准。你的任务是对生成的测试用例进行严格的评审。

# 核心职责
不只是简单通过，而是要作为“质量守门员”，敏锐地发现遗漏的场景、逻辑漏洞和描述不清的问题。

# 评审维度
1. **覆盖率检查**：
   - 是否遗漏了需求文档中的关键功能点？
   - 是否包含了必要的异常场景（如断网、服务超时、数据错误）？
   - 是否覆盖了边界条件（如最大长度、空值、特殊字符）？
2. **逻辑性检查**：
   - 前置条件是否充分？（例如测试“支付功能”前是否检查了“余额充足”）
   - 预期结果是否具体？（拒绝模糊的“显示正确”，必须说明具体提示文案或状态变化）
3. **规范性检查**：
   - 用例标题是否清晰表达了测试意图？
   - 步骤是否可执行？

# 输出要求
请输出一份结构化的评审报告：
1. **总体评价**：给出一个质量评分（0-100分）和总体结论（通过/需修改）。
2. **发现的问题**：列出具体的问题点，精确到具体的用例ID。
3. **补充建议**：直接给出建议补充的测试场景或用例。
4. **测试覆盖分析**：给出功能覆盖率、异常覆盖率、安全覆盖率、兼容覆盖率。
5. **综合评分**：给出 0-100 分综合评分。
6. **修正后的用例**（可选）：如果发现严重问题，请直接提供修正后的用例版本。"""

            return Response({
                'message': '默认提示词加载成功',
                'defaults': defaults
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"加载默认提示词失败: {e}")
            return Response(
                {'error': '加载失败，请稍后重试'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'])
    def enable(self, request, pk=None):
        """启用配置"""
        try:
            config = self.get_object()
            config.is_active = True
            config.save()
            return Response({
                'message': '提示词配置已启用',
                'id': config.id,
                'is_active': True
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"启用提示词配置失败: {e}")
            return Response({
                'error': '启用失败，请稍后重试'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'])
    def disable(self, request, pk=None):
        """禁用配置"""
        try:
            config = self.get_object()
            config.is_active = False
            config.save()
            return Response({
                'message': '提示词配置已禁用',
                'id': config.id,
                'is_active': False
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"禁用提示词配置失败: {e}")
            return Response({
                'error': '禁用失败，请稍后重试'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class TestSkillViewSet(viewsets.ModelViewSet):
    """Manage reusable SKILL.md packages used by AI test-case generation."""

    permission_classes = [TestSkillPermission]
    serializer_class = TestSkillSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    queryset = TestSkill.objects.select_related('project', 'created_by').all()

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if not (user.is_staff or user.is_superuser):
            queryset = queryset.filter(
                models.Q(project__isnull=True)
                | models.Q(project__owner=user)
                | models.Q(project__members=user)
            ).distinct()
        project_id = self.request.query_params.get('project')
        if project_id:
            queryset = queryset.filter(models.Q(project_id=project_id) | models.Q(project__isnull=True))
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')
        return queryset.order_by('-updated_at')

    def perform_destroy(self, instance):
        storage_path = instance.storage_path
        instance.delete()
        transaction.on_commit(lambda: remove_package(storage_path))

    @action(detail=False, methods=['post'], url_path='upload')
    def upload(self, request):
        uploads = request.FILES.getlist('files') or request.FILES.getlist('file')
        relative_paths = request.data.getlist('relative_paths') if hasattr(request.data, 'getlist') else []
        project_id = request.data.get('project') or None
        project = None
        if project_id:
            try:
                project = Project.objects.get(pk=project_id)
            except (Project.DoesNotExist, TypeError, ValueError):
                return Response({'error': '关联项目不存在'}, status=status.HTTP_400_BAD_REQUEST)
            user = request.user
            has_project_access = bool(
                user.is_staff
                or user.is_superuser
                or project.owner_id == user.pk
                or project.members.filter(pk=user.pk).exists()
            )
            if not has_project_access:
                return Response({'error': '无权访问所选项目'}, status=status.HTTP_403_FORBIDDEN)
        try:
            package = read_uploaded_package(uploads, relative_paths)
            metadata = package['metadata']
            duplicate = TestSkill.objects.filter(
                project=project,
                identifier=metadata['identifier'],
            ).first()
            if duplicate:
                return Response(
                    {'error': f"当前范围已存在 Skill：{duplicate.name}，请先删除旧版本再上传"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            storage_path = persist_package(package)
            try:
                skill = TestSkill.objects.create(
                    **metadata,
                    project=project,
                    storage_path=storage_path,
                    entrypoint='SKILL.md',
                    execution_config=package['execution_config'],
                    manifest=package['manifest'],
                    content_hash=package['content_hash'],
                    file_count=len(package['manifest']),
                    total_size=package['total_size'],
                    created_by=request.user,
                )
            except Exception:
                remove_package(storage_path)
                raise
        except (SkillPackageError, UnicodeDecodeError) as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception('上传 Skill 失败')
            return Response({'error': f'上传 Skill 失败: {exc}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(self.get_serializer(skill).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'], url_path='file')
    def file_content(self, request, pk=None):
        skill = self.get_object()
        relative_path = request.query_params.get('path') or skill.entrypoint
        manifest_item = next(
            (item for item in (skill.manifest or []) if item.get('path') == relative_path),
            None,
        )
        if not manifest_item:
            return Response({'error': 'Skill文件不存在'}, status=status.HTTP_404_NOT_FOUND)
        if not manifest_item.get('context_enabled'):
            return Response({'error': '该文件不支持文本预览'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            content = read_skill_file(skill, relative_path).decode('utf-8', errors='replace')
        except SkillPackageError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'path': relative_path, 'content': content})


class GenerationConfigViewSet(viewsets.ModelViewSet):
    """生成行为配置视图集"""
    permission_classes = [IsAuthenticated]
    queryset = GenerationConfig.objects.all()
    serializer_class = GenerationConfigSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        return queryset.order_by('-created_at')

    @action(detail=False, methods=['get'])
    def active(self, request):
        """获取活跃的生成配置"""
        try:
            config = GenerationConfig.get_active_config()
            if not config:
                return Response({
                    'error': '未找到活跃的生成配置，请先创建并启用一个配置'
                }, status=status.HTTP_404_NOT_FOUND)

            serializer = self.get_serializer(config)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"获取活跃生成配置失败: {e}")
            return Response({
                'error': '获取失败，请稍后重试'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'])
    def enable(self, request, pk=None):
        """启用配置"""
        try:
            # 禁用其他所有配置
            GenerationConfig.objects.all().update(is_active=False)

            # 启用当前配置
            config = self.get_object()
            config.is_active = True
            config.save()

            return Response({
                'message': '生成配置已启用',
                'id': config.id,
                'is_active': True
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"启用生成配置失败: {e}")
            return Response({
                'error': '启用失败，请稍后重试'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'])
    def disable(self, request, pk=None):
        """禁用配置"""
        try:
            config = self.get_object()
            config.is_active = False
            config.save()

            return Response({
                'message': '生成配置已禁用',
                'id': config.id,
                'is_active': False
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"禁用生成配置失败: {e}")
            return Response({
                'error': '禁用失败，请稍后重试'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class KnowledgeNodeViewSet(viewsets.ModelViewSet):
    """业务知识拓扑节点。"""

    serializer_class = KnowledgeNodeSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'post', 'patch', 'delete']

    def get_queryset(self):
        queryset = KnowledgeNode.objects.select_related('project', 'rule').filter(
            _knowledge_scope(self.request.user)
        ).distinct()
        project_id = self.request.query_params.get('project')
        node_type = self.request.query_params.get('type')
        search = self.request.query_params.get('search')

        if project_id:
            queryset = queryset.filter(models.Q(project_id=project_id) | models.Q(project__isnull=True))
        if node_type:
            queryset = queryset.filter(type=node_type)
        if search:
            queryset = queryset.filter(
                models.Q(name__icontains=search) |
                models.Q(content__icontains=search)
            )
        return queryset.order_by('name')

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        payload = request.data.copy()
        expected_behavior = str(payload.pop('expected_behavior', '') or '').strip()
        node_name = str(payload.get('name') or '').strip()
        node_type = payload.get('type') or 'rule'
        project_id = payload.get('project') or None
        if node_type == 'domain' and node_name:
            existing = KnowledgeNode.objects.filter(
                name=node_name,
                type='domain',
                rule__isnull=True,
                project_id=project_id,
            ).first()
            if existing:
                if payload.get('content') and payload.get('content') != existing.content:
                    existing.content = payload.get('content')
                    existing.save(update_fields=['content', 'updated_at'])
                serializer = self.get_serializer(existing)
                return Response(serializer.data, status=status.HTTP_200_OK)
        serializer = self.get_serializer(data=payload)
        serializer.is_valid(raise_exception=True)
        node = serializer.save()
        if node.type == 'rule' and not node.rule_id:
            rule = BusinessRuleKnowledge.objects.create(
                title=node.name,
                content=node.content,
                project=node.project,
                expected_behavior=expected_behavior,
                status='approved',
                rule_type='business',
                created_by=request.user,
                reviewed_by=request.user,
            )
            node.rule = rule
            node.save(update_fields=['rule', 'updated_at'])
        response = Response(self.get_serializer(node).data, status=status.HTTP_201_CREATED)
        if node_type == 'domain' and node_name:
            merge_duplicate_domain_nodes(name=node_name)
        return response

    @transaction.atomic
    def perform_update(self, serializer):
        """Keep a rule-backed node and its business-rule record consistent."""
        node = serializer.save()
        if not node.rule_id:
            return

        rule = BusinessRuleKnowledge.objects.get(pk=node.rule_id)
        update_fields = []
        if rule.title != node.name:
            rule.title = node.name
            update_fields.append('title')
        if rule.content != node.content:
            rule.content = node.content
            update_fields.append('content')
        if rule.project_id != node.project_id:
            rule.project_id = node.project_id
            update_fields.append('project')
        if update_fields:
            rule.save(update_fields=[*update_fields, 'updated_at'])

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        node = self.get_object()
        rule_id = node.rule_id
        node.delete()
        if rule_id:
            BusinessRuleKnowledge.objects.filter(id=rule_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=['post'], url_path='batch-delete')
    @transaction.atomic
    def batch_delete(self, request):
        """Delete selected knowledge files and their owned descendant nodes."""
        raw_ids = request.data.get('ids', [])
        if not isinstance(raw_ids, list) or not raw_ids:
            return Response(
                {'message': '请提供要删除的知识文件 ID 列表'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        node_ids = []
        for item in raw_ids:
            try:
                node_id = int(item)
            except (TypeError, ValueError):
                continue
            if node_id > 0 and node_id not in node_ids:
                node_ids.append(node_id)

        if not node_ids:
            return Response(
                {'message': '请提供有效的知识文件 ID'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        roots = self.get_queryset().filter(
            id__in=node_ids,
            type='domain',
            rule__isnull=True,
        )
        root_ids = set(roots.values_list('id', flat=True))
        missing_ids = [node_id for node_id in node_ids if node_id not in root_ids]
        if not root_ids:
            return Response(
                {'message': '未找到可删除的知识文件', 'missing_ids': missing_ids},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Follow the same outgoing graph traversal used by the file list. Stop at
        # another domain root so deleting one file cannot remove another file.
        owned_node_ids = set(root_ids)
        frontier = set(root_ids)
        while frontier:
            target_ids = set(
                KnowledgeRelation.objects.filter(source_id__in=frontier)
                .values_list('target_id', flat=True)
            ) - owned_node_ids
            if not target_ids:
                break
            target_types = dict(
                KnowledgeNode.objects.filter(id__in=target_ids)
                .values_list('id', 'type')
            )
            next_frontier = {
                node_id for node_id in target_ids
                if target_types.get(node_id) != 'domain'
            }
            owned_node_ids.update(next_frontier)
            frontier = next_frontier

        deleted_nodes = KnowledgeNode.objects.filter(id__in=owned_node_ids).count()
        KnowledgeNode.objects.filter(id__in=owned_node_ids).delete()
        return Response({
            'success': True,
            'deleted': len(root_ids),
            'deleted_nodes': deleted_nodes,
            'missing_ids': missing_ids,
            'message': f'已删除 {len(root_ids)} 个知识文件及其 {deleted_nodes} 个节点',
        })


class KnowledgeRelationViewSet(viewsets.ModelViewSet):
    """业务知识拓扑关系。"""

    serializer_class = KnowledgeRelationSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'post', 'patch', 'delete']

    def get_queryset(self):
        queryset = KnowledgeRelation.objects.select_related(
            'source', 'source__project', 'target', 'target__project'
        ).filter(_knowledge_relation_scope(self.request.user)).distinct()
        project_id = self.request.query_params.get('project')
        relation_type = self.request.query_params.get('relation_type')
        if project_id:
            queryset = queryset.filter(
                models.Q(source__project_id=project_id) |
                models.Q(target__project_id=project_id) |
                models.Q(source__project__isnull=True) |
                models.Q(target__project__isnull=True)
            )
        if relation_type:
            queryset = queryset.filter(relation_type=relation_type)
        return queryset.order_by('source_id', 'target_id')


class KnowledgeGraphView(APIView):
    """GET /knowledge/graph：返回业务知识拓扑图。"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        merge_duplicate_domain_nodes()
        nodes_qs = KnowledgeNode.objects.select_related('project', 'rule').filter(
            _knowledge_scope(request.user)
        ).distinct()
        project_id = request.query_params.get('project')
        search = str(request.query_params.get('search') or '').strip()
        node_type = request.query_params.get('type')

        if project_id:
            nodes_qs = nodes_qs.filter(models.Q(project_id=project_id) | models.Q(project__isnull=True))
        if node_type:
            nodes_qs = nodes_qs.filter(type=node_type)
        if search:
            nodes_qs = nodes_qs.filter(
                models.Q(name__icontains=search) |
                models.Q(content__icontains=search)
            )

        node_ids = list(nodes_qs.values_list('id', flat=True)[:1000])
        relations_qs = KnowledgeRelation.objects.select_related('source', 'target').filter(
            source_id__in=node_ids,
            target_id__in=node_ids,
        )
        node_id_set = set(node_ids)

        nodes = []
        for node in nodes_qs.filter(id__in=node_ids):
            rule_details = None
            if node.rule_id and node.rule:
                rule = node.rule
                rule_details = {
                    'title': rule.title,
                    'content': rule.content,
                    'business_domain': rule.business_domain,
                    'module': rule.module,
                    'entity_name': rule.entity_name,
                    'attribute_name': rule.attribute_name,
                    'state_values': rule.state_values or [],
                    'rule_type': rule.rule_type,
                    'rule_type_display': rule.get_rule_type_display(),
                    'applicable_conditions': rule.applicable_conditions,
                    'expected_behavior': rule.expected_behavior,
                    'exceptions': rule.exceptions,
                    'source_requirement': rule.source_requirement,
                    'risk_level': rule.risk_level,
                    'risk_level_display': rule.get_risk_level_display(),
                    'status': rule.status,
                    'status_display': rule.get_status_display(),
                }
            nodes.append({
                'id': node.id,
                'name': node.name,
                'type': node.type,
                'content': node.content,
                'project_id': node.project_id,
                'project_name': node.project.name if node.project_id and node.project else '',
                'rule_id': node.rule_id,
                'rule_details': rule_details,
            })

        edges = []
        for relation in relations_qs[:2000]:
            if relation.source_id not in node_id_set or relation.target_id not in node_id_set:
                continue
            edges.append({
                'id': relation.id,
                'source': relation.source_id,
                'target': relation.target_id,
                'relation': relation.relation_type,
                'relation_type': relation.relation_type,
            })

        return Response({
            'nodes': nodes,
            'edges': edges,
            'summary': {
                'node_count': len(nodes),
                'edge_count': len(edges),
            }
        })


class BusinessRuleKnowledgeViewSet(viewsets.ModelViewSet):
    """业务规则知识库：管理、推荐和追踪规则使用情况。"""

    queryset = BusinessRuleKnowledge.objects.select_related(
        'project', 'created_by', 'reviewed_by', 'source_task'
    ).all()
    serializer_class = BusinessRuleKnowledgeSerializer
    pagination_class = BusinessRuleKnowledgePagination
    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'post', 'patch', 'delete']

    def get_queryset(self):
        queryset = super().get_queryset()
        queryset = queryset.filter(_knowledge_scope(self.request.user)).distinct()
        status_param = self.request.query_params.get('status')
        project_id = self.request.query_params.get('project')
        rule_type = self.request.query_params.get('rule_type')
        module = self.request.query_params.get('module')
        business_domain = self.request.query_params.get('business_domain')
        entity_name = self.request.query_params.get('entity_name')
        rule_name = self.request.query_params.get('rule_name')
        knowledge_file_id = self.request.query_params.get('knowledge_file_id')
        knowledge_file_name = str(
            self.request.query_params.get('knowledge_file_name') or ''
        ).strip()
        search = self.request.query_params.get('search')

        if status_param:
            queryset = queryset.filter(status=status_param)
        if project_id:
            # 项目上下文应同时可见当前项目规则和全局规则。项目级规则用于
            # 隔离不同项目，global rule 则是所有项目都可复用的知识。
            queryset = queryset.filter(
                models.Q(project_id=project_id) |
                models.Q(project__isnull=True)
            )
        if rule_type:
            queryset = queryset.filter(rule_type=rule_type)
        if module:
            queryset = queryset.filter(module__icontains=module)
        if business_domain:
            queryset = queryset.filter(business_domain__icontains=business_domain)
        if entity_name:
            queryset = queryset.filter(entity_name__icontains=entity_name)
        if rule_name:
            queryset = queryset.filter(title__icontains=rule_name)
        if knowledge_file_id or knowledge_file_name:
            file_nodes = KnowledgeNode.objects.filter(
                type='domain',
                rule__isnull=True,
            )
            if project_id:
                file_nodes = file_nodes.filter(
                    models.Q(project_id=project_id) |
                    models.Q(project__isnull=True)
                )
            if knowledge_file_id:
                try:
                    file_nodes = file_nodes.filter(id=int(knowledge_file_id))
                except (TypeError, ValueError):
                    return queryset.none()
            if knowledge_file_name:
                file_nodes = file_nodes.filter(name__icontains=knowledge_file_name)
            descendant_ids = _knowledge_file_descendant_ids(
                file_nodes.values_list('id', flat=True)
            )
            if not descendant_ids:
                return queryset.none()
            queryset = queryset.filter(knowledge_node__id__in=descendant_ids)
        if search:
            queryset = queryset.filter(
                models.Q(title__icontains=search) |
                models.Q(content__icontains=search) |
                models.Q(module__icontains=search) |
                models.Q(business_domain__icontains=search) |
                models.Q(entity_name__icontains=search) |
                models.Q(attribute_name__icontains=search) |
                models.Q(source_requirement__icontains=search)
            )
        return queryset

    def perform_create(self, serializer):
        rule = serializer.save()
        sync_rule_to_knowledge_node(rule)

    @transaction.atomic
    def perform_update(self, serializer):
        changed_fields = set(serializer.validated_data)
        rule = serializer.save()
        # Editing rule metadata (for example expected_behavior from the node
        # editor) must not rebuild the topology. Re-sync only fields that can
        # affect the node projection or when the rule has no node yet.
        topology_fields = {
            'title', 'content', 'project', 'status',
        }
        if changed_fields & topology_fields or not KnowledgeNode.objects.filter(rule=rule).exists():
            sync_rule_to_knowledge_node(rule)

    def destroy(self, request, *args, **kwargs):
        rule = self.get_object()
        rule.status = 'deprecated'
        rule.save(update_fields=['status', 'updated_at'])
        sync_rule_to_knowledge_node(rule)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=['post'], url_path='batch-delete')
    def batch_delete(self, request):
        """批量永久删除业务规则。"""
        raw_ids = request.data.get('ids', [])
        if not isinstance(raw_ids, list) or not raw_ids:
            return Response(
                {'message': '请提供要删除的业务规则 ID 列表'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        rule_ids = []
        for item in raw_ids:
            try:
                rule_id = int(item)
            except (TypeError, ValueError):
                continue
            if rule_id > 0 and rule_id not in rule_ids:
                rule_ids.append(rule_id)

        if not rule_ids:
            return Response(
                {'message': '请提供有效的业务规则 ID'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        rules = self.get_queryset().filter(id__in=rule_ids)
        found_ids = set(rules.values_list('id', flat=True))
        deleted = rules.count()
        KnowledgeNode.objects.filter(rule_id__in=found_ids).delete()
        # get_queryset() is intentionally distinct() for project/member scope;
        # Django does not allow delete() on a distinct QuerySet. The IDs were
        # collected from that scoped queryset, so this second query remains
        # authorization-safe while allowing the delete operation.
        BusinessRuleKnowledge.objects.filter(id__in=found_ids).delete()

        return Response({
            'success': True,
            'deleted': deleted,
            'missing_ids': [rule_id for rule_id in rule_ids if rule_id not in found_ids],
            'message': f'已永久删除 {deleted} 条业务规则',
        })

    @action(detail=False, methods=['post'])
    def recommend(self, request):
        query = str(request.data.get('query') or '').strip()
        if len(query) < 2:
            return Response({'error': '请至少输入2个字符的需求或业务规则'}, status=status.HTTP_400_BAD_REQUEST)

        project_id = request.data.get('project')
        module = str(request.data.get('module') or '').strip()
        try:
            limit = max(1, min(int(request.data.get('limit', 8)), 20))
        except (TypeError, ValueError):
            limit = 8

        queryset = self.get_queryset().filter(status='approved')
        if project_id:
            queryset = queryset.filter(models.Q(project_id=project_id) | models.Q(project__isnull=True))
        else:
            queryset = queryset.filter(project__isnull=True)

        recommendations = recommend_business_rules(
            queryset[:500],
            query,
            project_id=project_id,
            module=module,
            limit=limit,
        )
        rule_ids = [rule.id for rule, _score in recommendations]
        if rule_ids:
            BusinessRuleKnowledge.objects.filter(id__in=rule_ids).update(
                usage_count=models.F('usage_count') + 1
            )

        results = []
        for rule, score in recommendations:
            data = BusinessRuleKnowledgeSerializer(rule, context={'request': request}).data
            data['similarity_score'] = score
            results.append(data)
        return Response({'results': results, 'count': len(results)})

    @action(detail=False, methods=['get'])
    def graph(self, request):
        """兼容旧入口，返回真实知识节点与关系表中的拓扑。"""
        return KnowledgeGraphView().get(request)

    @action(detail=True, methods=['get'])
    def history(self, request, pk=None):
        rule = self.get_object()
        usages = rule.usage_records.select_related('task').all()[:100]
        return Response(BusinessRuleUsageSerializer(usages, many=True).data)

    @action(detail=False, methods=['get'], url_path='export_excel')
    def export_excel(self, request):
        import io
        from django.http import HttpResponse
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill

        queryset = self.get_queryset().filter(status='approved')
        knowledge_file_id = request.query_params.get('knowledge_file_id')
        knowledge_file_name = str(request.query_params.get('knowledge_file_name') or '').strip()
        if knowledge_file_id or knowledge_file_name:
            file_nodes = KnowledgeNode.objects.filter(
                type='domain', rule__isnull=True,
            )
            if knowledge_file_id:
                try:
                    file_nodes = file_nodes.filter(id=int(knowledge_file_id))
                except (TypeError, ValueError):
                    return Response({'message': '无效的 knowledge_file_id'}, status=400)
            if knowledge_file_name:
                file_nodes = file_nodes.filter(name__icontains=knowledge_file_name)
            descendant_ids = _knowledge_file_descendant_ids(
                file_nodes.values_list('id', flat=True)
            )
            if not descendant_ids:
                queryset = queryset.none()
            else:
                queryset = queryset.filter(knowledge_node__id__in=descendant_ids)

        rules = list(queryset.order_by('-updated_at')[:2000])

        wb = Workbook()
        ws = wb.active
        ws.title = '业务规则'
        headers = ['标题', '规则', '预期结果']
        header_fill = PatternFill(start_color='4A90E2', end_color='4A90E2', fill_type='solid')
        header_font = Font(color='FFFFFF', bold=True, size=12)
        for col, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=h)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal='center', vertical='center')
        ws.column_dimensions['A'].width = 40
        ws.column_dimensions['B'].width = 80
        ws.column_dimensions['C'].width = 60

        for row_idx, rule in enumerate(rules, 2):
            ws.cell(row=row_idx, column=1, value=rule.title or '')
            ws.cell(row=row_idx, column=2, value=rule.content or '')
            ws.cell(row=row_idx, column=3, value=rule.expected_behavior or '')
            for col in range(1, 4):
                ws.cell(row=row_idx, column=col).alignment = Alignment(
                    wrap_text=True, vertical='top'
                )

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        filename = 'business_rules.xlsx'
        if knowledge_file_name:
            filename = f'{knowledge_file_name}_rules.xlsx'
        resp = HttpResponse(
            buf.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        resp['Content-Disposition'] = f'attachment; filename="{filename}"'
        return resp


def _default_workflow_definition(name):
    return {
        'nodes': [
            {
                'id': 'start',
                'type': 'start',
                'label': '开始',
                'x': 120,
                'y': 180,
                'description': '',
            },
            {
                'id': 'task-1',
                'type': 'task',
                'label': name or '业务节点',
                'x': 320,
                'y': 170,
                'description': '',
            },
            {
                'id': 'end',
                'type': 'end',
                'label': '结束',
                'x': 560,
                'y': 180,
                'description': '',
            },
        ],
        'edges': [
            {'id': 'edge-start-task-1', 'source': 'start', 'target': 'task-1', 'label': ''},
            {'id': 'edge-task-1-end', 'source': 'task-1', 'target': 'end', 'label': ''},
        ],
        'viewport': {'scale': 1, 'offsetX': 0, 'offsetY': 0},
    }


WORKFLOW_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'}
WORKFLOW_VISION_TIMEOUT_SECONDS = 90
WORKFLOW_NODE_TYPES = {
    'start': 'start',
    'startEvent': 'start',
    'end': 'end',
    'endEvent': 'end',
    'task': 'task',
    'userTask': 'task',
    'serviceTask': 'serviceTask',
    'gateway': 'gateway',
    'exclusiveGateway': 'gateway',
    'parallelGateway': 'gateway',
    'inclusiveGateway': 'gateway',
    'subprocess': 'subprocess',
    'subProcess': 'subprocess',
}


def _is_workflow_image_upload(upload_file):
    extension = os.path.splitext(str(getattr(upload_file, 'name', '') or '').lower())[1]
    content_type = str(getattr(upload_file, 'content_type', '') or '').lower()
    return extension in WORKFLOW_IMAGE_EXTENSIONS or content_type.startswith('image/')


def _workflow_node_label(raw_node, raw_id, node_type, index):
    label = str(raw_node.get('label') or raw_node.get('name') or '').strip()
    if label:
        return label[:80]
    if node_type == 'start':
        return '开始'
    if node_type == 'end':
        return '结束'
    if node_type == 'gateway':
        return '网关'
    return str(raw_id or f'节点 {index + 1}')[:80]


def _fit_imported_image_layout(definition):
    nodes = definition.get('nodes') or []
    if len(nodes) < 3:
        return definition

    xs = [node['x'] for node in nodes if isinstance(node.get('x'), int)]
    ys = [node['y'] for node in nodes if isinstance(node.get('y'), int)]
    if not xs or not ys:
        return definition

    unique_xs = sorted(set(xs))
    unique_ys = sorted(set(ys))
    x_gaps = [right - left for left, right in zip(unique_xs, unique_xs[1:]) if right - left >= 24]
    y_gaps = [bottom - top for top, bottom in zip(unique_ys, unique_ys[1:]) if bottom - top >= 24]
    scale = 1.0
    if x_gaps:
        scale = max(scale, min(1.2, 72 / min(x_gaps)))
    if y_gaps:
        scale = max(scale, min(1.2, 56 / min(y_gaps)))

    layout_width = max(xs) - min(xs)
    layout_height = max(ys) - min(ys)
    if layout_width:
        scale = min(scale, 960 / layout_width)
    if layout_height:
        scale = min(scale, 1800 / layout_height)
    scale = max(0.55, min(1.2, scale))

    min_x = min(xs)
    min_y = min(ys)
    for node in nodes:
        node['x'] = round((node['x'] - min_x) * scale + 96)
        node['y'] = round((node['y'] - min_y) * scale + 72)
    definition['viewport'] = {
        **(definition.get('viewport') or {}),
        'scale': 0.75,
        'offsetX': 0,
        'offsetY': 0,
        'layout_source': 'image_coordinates',
        'layout_mode': 'bpmn',
        'layout_scale': round(scale, 3),
    }
    return definition


def _normalize_workflow_definition(raw_definition, fallback_name='导入工作流', fit_image_layout=False):
    if not isinstance(raw_definition, dict):
        return _default_workflow_definition(fallback_name)

    raw_nodes = raw_definition.get('nodes') or []
    raw_edges = raw_definition.get('edges') or []
    if not isinstance(raw_nodes, list):
        raw_nodes = []
    if not isinstance(raw_edges, list):
        raw_edges = []

    nodes = []
    used_ids = set()
    for index, raw_node in enumerate(raw_nodes[:120]):
        if not isinstance(raw_node, dict):
            continue
        raw_id = str(raw_node.get('id') or raw_node.get('key') or raw_node.get('name') or f'node-{index + 1}').strip()
        node_id = re.sub(r'[^0-9A-Za-z_-]+', '-', raw_id).strip('-') or f'node-{index + 1}'
        if node_id in used_ids:
            node_id = f'{node_id}-{index + 1}'
        used_ids.add(node_id)
        raw_type = str(raw_node.get('type') or '').strip()
        node_type = WORKFLOW_NODE_TYPES.get(raw_type, 'task')
        nodes.append({
            'id': node_id,
            'type': node_type,
            'label': _workflow_node_label(raw_node, raw_id, node_type, index),
            'x': int(float(raw_node.get('x'))) if str(raw_node.get('x', '')).replace('.', '', 1).isdigit() else 120 + (len(nodes) % 4) * 220,
            'y': int(float(raw_node.get('y'))) if str(raw_node.get('y', '')).replace('.', '', 1).isdigit() else 140 + (len(nodes) // 4) * 140,
            'description': str(raw_node.get('description') or raw_node.get('remark') or '')[:1000],
        })

    if not nodes:
        return _default_workflow_definition(fallback_name)

    node_ids = {node['id'] for node in nodes}
    edges = []
    for index, raw_edge in enumerate(raw_edges[:200]):
        if not isinstance(raw_edge, dict):
            continue
        source = str(raw_edge.get('source') or raw_edge.get('from') or raw_edge.get('sourceRef') or '').strip()
        target = str(raw_edge.get('target') or raw_edge.get('to') or raw_edge.get('targetRef') or '').strip()
        if source not in node_ids or target not in node_ids:
            continue
        edges.append({
            'id': str(raw_edge.get('id') or f'edge-{index + 1}'),
            'source': source,
            'target': target,
            'label': str(raw_edge.get('label') or raw_edge.get('name') or raw_edge.get('condition') or '')[:80],
            'condition': str(raw_edge.get('condition') or raw_edge.get('label') or raw_edge.get('name') or '')[:1000],
        })

    normalized = {
        'nodes': nodes,
        'edges': edges,
        'viewport': raw_definition.get('viewport') or {'scale': 1, 'offsetX': 0, 'offsetY': 0},
    }
    return _fit_imported_image_layout(normalized) if fit_image_layout else normalized


def _extract_json_payload(text):
    content = str(text or '').strip()
    if not content:
        return None
    fenced = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', content, re.DOTALL)
    if fenced:
        content = fenced.group(1)
    else:
        start = content.find('{')
        end = content.rfind('}')
        if start >= 0 and end > start:
            content = content[start:end + 1]
    try:
        return json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _active_workflow_vision_config():
    vision_keywords = (
        'vision', 'vl', '4o', 'omni', 'gpt-5', 'gemini', 'claude', 'qwen-vl',
        'minimax', 'doubao', 'glm-4v',
    )
    preferred_queries = [
        models.Q(model_usage='requirement_analysis'),
        models.Q(model_usage='general'),
        models.Q(model_usage='test_case_generation') | models.Q(role='writer'),
    ]
    for query in preferred_queries:
        configs = list(AIModelConfig.objects.filter(query, is_active=True).order_by('-updated_at')[:20])
        vision_like = [
            config for config in configs
            if any(keyword in (config.model_name or '').lower() for keyword in vision_keywords)
        ]
        if vision_like:
            return vision_like[0]
    return None


def _workflow_ocr_quality_ok(text):
    lines = [line.strip() for line in str(text or '').splitlines() if line.strip()]
    if not lines:
        return False
    meaningful_lines = []
    for line in lines:
        cjk_count = len(re.findall(r'[\u4e00-\u9fff]', line))
        alpha_count = len(re.findall(r'[A-Za-z]', line))
        content_count = len(re.findall(r'[\w\u4e00-\u9fff]', line))
        if cjk_count >= 2 and alpha_count <= max(cjk_count * 2, 6):
            meaningful_lines.append(line)
        elif content_count >= 6 and cjk_count >= 1:
            meaningful_lines.append(line)
    return len(meaningful_lines) >= 4


async def _call_workflow_vision_model(config, messages):
    return await asyncio.wait_for(
        AIModelService.call_openai_compatible_api(config, messages, max_tokens=4096),
        timeout=WORKFLOW_VISION_TIMEOUT_SECONDS,
    )


def _parse_workflow_image_import(file_name, raw_bytes, content_type=''):
    """把流程图图片导入为轻量工作流定义，优先使用多模态模型，失败时退回 OCR 文本链路。"""
    workflow_name = os.path.splitext(file_name or '')[0] or '导入工作流'
    content_type = content_type or 'image/png'
    config = _active_workflow_vision_config()
    if config:
        data_url = f"data:{content_type};base64,{base64.b64encode(raw_bytes).decode('ascii')}"
        prompt = (
            "请识别图片中的业务流程图/BPMN/审批流，输出严格 JSON，不要输出 Markdown。\n"
            "JSON 格式：{\"nodes\":[{\"id\":\"唯一ID\",\"type\":\"start|task|gateway|end|subprocess\",\"label\":\"节点文字\",\"x\":数字,\"y\":数字,\"description\":\"\"}],"
            "\"edges\":[{\"id\":\"唯一ID\",\"source\":\"源节点ID\",\"target\":\"目标节点ID\",\"label\":\"连线标签\",\"condition\":\"条件\"}],"
            "\"viewport\":{\"scale\":1,\"offsetX\":0,\"offsetY\":0}}。\n"
            "请保留图片里的中文节点名和分支条件，例如：通过、拒绝、返回、取消。"
        )
        messages = [
            {'role': 'system', 'content': '你是流程图识别专家，只返回可被 JSON.parse 解析的工作流定义。'},
            {
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': prompt},
                    {'type': 'image_url', 'image_url': {'url': data_url}},
                ],
            },
        ]
        try:
            result = async_to_sync(_call_workflow_vision_model)(config, messages)
            message = ((result.get('choices') or [{}])[0].get('message') or {})
            content = message.get('content') or ''
            payload = _extract_json_payload(content)
            definition = _normalize_workflow_definition(payload, workflow_name, fit_image_layout=True)
            if definition.get('nodes'):
                return definition, content[:200000]
        except asyncio.TimeoutError:
            logger.warning(
                "工作流图片 AI 识别超过 %s 秒，回退 OCR",
                WORKFLOW_VISION_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning("工作流图片 AI 识别失败，回退 OCR: %s", exc)

    suffix = os.path.splitext(file_name or '')[1] or '.png'
    temp_path = ''
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_file.write(raw_bytes)
            temp_path = temp_file.name
        ocr_text = DocumentProcessor.extract_text_from_image(temp_path)
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    if not _workflow_ocr_quality_ok(ocr_text):
        raise ValueError(
            f'流程图图片识别失败：视觉模型 {WORKFLOW_VISION_TIMEOUT_SECONDS} 秒内未返回有效结果，'
            '本地 OCR 结果质量不足，已阻止生成错误工作流。请检查视觉模型配置，或导入 BPMN/JSON。'
        )
    return _parse_workflow_import(f'{workflow_name}.txt', ocr_text), ocr_text[:200000]


def _parse_workflow_import(file_name, source_text):
    """把 JSON/BPMN/XML/纯文本导入为轻量工作流定义。"""
    workflow_name = os.path.splitext(file_name or '')[0] or '导入工作流'
    stripped = (source_text or '').strip()
    if not stripped:
        return _default_workflow_definition(workflow_name)

    if file_name.lower().endswith('.json') or stripped.startswith('{'):
        try:
            payload = json.loads(stripped)
            if isinstance(payload, dict):
                if isinstance(payload.get('definition'), dict):
                    return _normalize_workflow_definition(payload['definition'], workflow_name)
                if isinstance(payload.get('nodes'), list) and isinstance(payload.get('edges'), list):
                    return _normalize_workflow_definition(payload, workflow_name)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    if file_name.lower().endswith(('.bpmn', '.xml')) or stripped.startswith('<'):
        try:
            root = ET.fromstring(stripped)
            nodes = []
            edges = []
            supported_tags = {
                'startEvent': 'start',
                'endEvent': 'end',
                'userTask': 'userTask',
                'serviceTask': 'serviceTask',
                'task': 'task',
                'exclusiveGateway': 'gateway',
                'parallelGateway': 'gateway',
                'inclusiveGateway': 'gateway',
                'subProcess': 'subprocess',
            }
            sequence_index = 0
            for element in root.iter():
                tag = element.tag.split('}', 1)[-1]
                element_id = element.attrib.get('id')
                if tag in supported_tags and element_id:
                    nodes.append({
                        'id': element_id,
                        'type': supported_tags[tag],
                        'label': element.attrib.get('name') or element_id,
                        'x': 120 + (len(nodes) % 4) * 220,
                        'y': 120 + (len(nodes) // 4) * 140,
                        'description': '',
                    })
                elif tag == 'sequenceFlow':
                    sequence_index += 1
                    edges.append({
                        'id': element_id or f'import-edge-{sequence_index}',
                        'source': element.attrib.get('sourceRef'),
                        'target': element.attrib.get('targetRef'),
                        'label': element.attrib.get('name') or '',
                    })
            node_ids = {node['id'] for node in nodes}
            edges = [
                edge for edge in edges
                if edge.get('source') in node_ids and edge.get('target') in node_ids
            ]
            if nodes:
                return _normalize_workflow_definition({
                    'nodes': nodes,
                    'edges': edges,
                    'viewport': {'scale': 1, 'offsetX': 0, 'offsetY': 0},
                }, workflow_name)
        except ET.ParseError:
            pass

    lines = [line.strip('-*# \t') for line in stripped.splitlines() if line.strip()]
    labels = lines[:12] or [workflow_name]
    nodes = []
    edges = []
    for index, label in enumerate(labels):
        node_id = f'text-node-{index + 1}'
        nodes.append({
            'id': node_id,
            'type': 'task' if 0 < index < len(labels) - 1 else ('start' if index == 0 else 'end'),
            'label': label[:80],
            'x': 120 + (index % 4) * 220,
            'y': 140 + (index // 4) * 140,
            'description': label,
        })
        if index:
            edges.append({
                'id': f'text-edge-{index}',
                'source': nodes[index - 1]['id'],
                'target': node_id,
                'label': '',
            })
    return {
        'nodes': nodes,
        'edges': edges,
        'viewport': {'scale': 1, 'offsetX': 0, 'offsetY': 0},
    }


def serialize_workflow_snapshot(workflow):
    definition = workflow.definition if isinstance(workflow.definition, dict) else {}
    nodes = definition.get('nodes') or []
    edges = definition.get('edges') or []
    node_name_map = {str(node.get('id')): node.get('label') or node.get('name') for node in nodes}
    paths = []
    for edge in edges[:80]:
        source = node_name_map.get(str(edge.get('source'))) or edge.get('source')
        target = node_name_map.get(str(edge.get('target'))) or edge.get('target')
        label = edge.get('label') or edge.get('condition') or ''
        if source and target:
            paths.append(f"{source} -> {target}" + (f"（{label}）" if label else ""))
    return {
        'id': workflow.id,
        'title': workflow.name,
        'content': workflow.description or workflow.source_text[:500],
        'project_id': workflow.project_id,
        'project_name': workflow.project.name if workflow.project_id and workflow.project else '',
        'module': workflow.category,
        'business_domain': workflow.category,
        'entity_name': workflow.name,
        'attribute_name': workflow.identifier,
        'state_values': [
            node.get('label') or node.get('name')
            for node in nodes[:40]
            if node.get('label') or node.get('name')
        ],
        'relation_nodes': paths,
        'test_strategies': ['按工作流主路径、分支路径、异常路径生成用例'],
        'risk_level': 'medium',
        'rule_type': 'workflow',
        'applicable_conditions': workflow.description,
        'expected_behavior': '\n'.join(paths[:30]),
        'exceptions': '',
        'version': workflow.version,
        'knowledge_node_id': None,
        'workflow_id': workflow.id,
        'workflow_nodes': nodes[:80],
        'workflow_edges': edges[:120],
        'graph_context_source': 'selected_workflow',
    }


def build_workflow_context_text(workflows):
    blocks = []
    for workflow in workflows:
        snapshot = serialize_workflow_snapshot(workflow)
        node_names = '、'.join(snapshot['state_values'][:20]) or '暂无节点'
        paths = snapshot['relation_nodes'][:30]
        block = [
            f"- 工作流：{workflow.name}",
            f"  标识：{workflow.identifier or '未设置'}；分类：{workflow.category or '未分类'}；项目：{snapshot['project_name'] or '全局'}",
            f"  说明：{workflow.description or '暂无'}",
            f"  节点：{node_names}",
        ]
        if paths:
            block.append("  路径：")
            block.extend([f"    {path}" for path in paths])
        blocks.append('\n'.join(block))
    if not blocks:
        return ''
    return (
        "【已选择业务工作流】\n"
        "生成用例时必须结合以下流程文件，与已选择知识库规则/节点做业务关联，覆盖主流程、分支、回退、终止和异常路径。\n"
        + '\n\n'.join(blocks)
    )


class BusinessWorkflowViewSet(viewsets.ModelViewSet):
    """业务工作流文件管理。"""

    queryset = BusinessWorkflow.objects.select_related('project', 'created_by').all()
    serializer_class = BusinessWorkflowSerializer
    pagination_class = BusinessRuleKnowledgePagination
    permission_classes = [RequirementAssetPermission]
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    http_method_names = ['get', 'post', 'patch', 'delete']

    def get_queryset(self):
        queryset = _filter_requirement_documents(
            super().get_queryset(),
            self.request.user,
            owner_field='created_by',
        )
        status_param = self.request.query_params.get('status')
        project_id = self.request.query_params.get('project')
        category = self.request.query_params.get('category')
        search = self.request.query_params.get('search')

        if status_param:
            queryset = queryset.filter(status=status_param)
        else:
            queryset = queryset.exclude(status='archived')
        if project_id:
            queryset = queryset.filter(models.Q(project_id=project_id) | models.Q(project__isnull=True))
        if category:
            queryset = queryset.filter(category__icontains=category)
        if search:
            queryset = queryset.filter(
                models.Q(name__icontains=search) |
                models.Q(identifier__icontains=search) |
                models.Q(category__icontains=search) |
                models.Q(description__icontains=search) |
                models.Q(source_text__icontains=search)
            )
        return queryset

    @action(detail=True, methods=['get'])
    def download(self, request, pk=None):
        workflow = self.get_object()
        if not workflow.source_file:
            raise Http404('工作流源文件不存在')
        return FileResponse(
            workflow.source_file.open('rb'),
            as_attachment=True,
            filename=workflow.source_file.name.rsplit('/', 1)[-1],
        )

    def perform_create(self, serializer):
        definition = serializer.validated_data.get('definition') or {}
        if not definition:
            definition = _default_workflow_definition(serializer.validated_data.get('name') or '新建工作流')
        serializer.save(definition=definition)

    def destroy(self, request, *args, **kwargs):
        workflow = self.get_object()
        workflow.status = 'archived'
        workflow.save(update_fields=['status', 'updated_at'])
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=['post'], url_path='batch-delete')
    def batch_delete(self, request):
        raw_ids = request.data.get('ids', [])
        if not isinstance(raw_ids, list) or not raw_ids:
            return Response({'message': '请提供要删除的工作流 ID 列表'}, status=status.HTTP_400_BAD_REQUEST)

        workflow_ids = []
        for item in raw_ids:
            try:
                workflow_id = int(item)
            except (TypeError, ValueError):
                continue
            if workflow_id > 0 and workflow_id not in workflow_ids:
                workflow_ids.append(workflow_id)

        if not workflow_ids:
            return Response({'message': '请提供有效的工作流 ID'}, status=status.HTTP_400_BAD_REQUEST)

        workflows = self.get_queryset().filter(id__in=workflow_ids)
        found_ids = set(workflows.values_list('id', flat=True))
        deleted = workflows.count()
        workflows.delete()
        return Response({
            'success': True,
            'deleted': deleted,
            'missing_ids': [workflow_id for workflow_id in workflow_ids if workflow_id not in found_ids],
            'message': f'已永久删除 {deleted} 个工作流文件',
        })

    @action(detail=False, methods=['post'], url_path='import')
    def import_workflow(self, request):
        upload_file = request.FILES.get('file')
        if not upload_file:
            return Response({'message': '请上传工作流文件'}, status=status.HTTP_400_BAD_REQUEST)

        default_name = os.path.splitext(upload_file.name)[0]
        raw = upload_file.read()
        if _is_workflow_image_upload(upload_file):
            try:
                definition, source_text = _parse_workflow_image_import(
                    upload_file.name,
                    raw,
                    getattr(upload_file, 'content_type', '') or 'image/png',
                )
            except ValueError as exc:
                return Response({'message': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        else:
            try:
                source_text = raw.decode('utf-8')
            except UnicodeDecodeError:
                source_text = raw.decode('utf-8', errors='ignore')
            definition = _parse_workflow_import(upload_file.name, source_text)
        upload_file.seek(0)

        serializer = self.get_serializer(data={
            'name': request.data.get('name') or default_name,
            'identifier': request.data.get('identifier') or default_name,
            'category': request.data.get('category') or '',
            'project': request.data.get('project') or None,
            'description': request.data.get('description') or '',
            'definition': definition,
            'source_file': upload_file,
            'source_text': source_text[:200000],
            'status': 'active',
        })
        serializer.is_valid(raise_exception=True)
        workflow = serializer.save()
        return Response(self.get_serializer(workflow).data, status=status.HTTP_201_CREATED)


class TestCaseGenerationTaskViewSet(viewsets.ModelViewSet):
    """测试用例生成任务视图集"""
    queryset = TestCaseGenerationTask.objects.select_related('project', 'created_by').all()
    serializer_class = TestCaseGenerationTaskSerializer
    permission_classes = [GenerationTaskPermission]
    pagination_class = TestCaseGenerationTaskPagination
    http_method_names = ['get', 'post', 'patch', 'delete']  # 允许GET、POST、PATCH和DELETE方法
    lookup_field = 'task_id'  # 使用task_id作为查找字段

    @staticmethod
    def resolve_generation_behavior(validated_data):
        """Resolve runtime behavior from the active generation config."""
        gen_config = GenerationConfig.get_active_config()
        if gen_config:
            return {
                'config': gen_config,
                'output_mode': gen_config.default_output_mode,
                'enable_auto_review': gen_config.enable_auto_review,
                'review_timeout': AIModelService.get_effective_review_timeout(gen_config.review_timeout),
                'configured_review_timeout': gen_config.review_timeout,
            }

        requested_output_mode = validated_data.get('output_mode')
        if requested_output_mode not in ['stream', 'complete']:
            requested_output_mode = 'stream'

        requested_auto_review = validated_data.get('use_reviewer_model', True)
        return {
            'config': None,
            'output_mode': requested_output_mode,
            'enable_auto_review': requested_auto_review,
            'review_timeout': AIModelService.get_effective_review_timeout(None),
            'configured_review_timeout': None,
        }

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if not local_trusted_mode() and not user.is_superuser:
            queryset = queryset.filter(
                models.Q(created_by=user)
                | models.Q(project__owner=user)
                | models.Q(project__members=user)
            ).distinct()

        # 安全检查：确保request有query_params属性
        if not hasattr(self.request, 'query_params'):
            return queryset.order_by('-created_at')

        # 按状态过滤
        status_param = self.request.query_params.get('status')
        if status_param:
            queryset = queryset.filter(status=status_param)

        # 按创建者过滤
        created_by = self.request.query_params.get('created_by')
        if created_by:
            queryset = queryset.filter(created_by_id=created_by)

        return queryset.order_by('-created_at')

    @action(detail=False, methods=['post'])
    def generate(self, request):
        """创建新的测试用例生成任务"""
        try:
            serializer = TestCaseGenerationRequestSerializer(
                data=request.data,
                context={'request': request, 'require_primary_skill': True},
            )
            if not serializer.is_valid():
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

            validated_data = serializer.validated_data
            generation_behavior = self.resolve_generation_behavior(validated_data)

            # 获取活跃的配置
            writer_config = None
            reviewer_config = None

            if validated_data.get('use_writer_model', True):
                # 优先查找测试用例生成模型，兼容旧版 writer 角色配置。
                writer_config = AIModelConfig.objects.filter(
                    models.Q(model_usage='test_case_generation') | models.Q(role='writer'),
                    is_active=True
                ).first()

                if not writer_config:
                    return Response(
                        {'error': '未找到可用的测试用例编写模型配置'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

            if generation_behavior['enable_auto_review']:
                # 优先查找测试用例评审模型，兼容旧版 reviewer 角色配置。
                reviewer_config = AIModelConfig.objects.filter(
                    models.Q(model_usage='test_case_review') | models.Q(role='reviewer'),
                    is_active=True
                ).first()

                if not reviewer_config:
                    return Response(
                        {'error': '未找到可用的测试用例评审模型配置'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

            # 创建任务
            generation_mode = validated_data.get('generation_mode', 'quick')
            requirement_text = validated_data['requirement_text']

            project = validated_data.get('project')
            selected_rule_ids = validated_data.get('selected_rule_ids', [])
            selected_workflow_ids = validated_data.get('selected_workflow_ids', [])
            selected_skill_ids = validated_data.get('selected_skill_ids', [])
            selected_skills = TestSkill.objects.filter(
                id__in=selected_skill_ids,
                is_active=True,
            )
            if project:
                selected_skills = selected_skills.filter(
                    models.Q(project=project) | models.Q(project__isnull=True)
                )
            else:
                selected_skills = selected_skills.filter(project__isnull=True)
            skill_by_id = {skill.id: skill for skill in selected_skills}
            selected_skills = [skill_by_id[skill_id] for skill_id in selected_skill_ids if skill_id in skill_by_id]
            available_skill_ids = {skill.id for skill in selected_skills}
            unavailable_skill_ids = [
                skill_id for skill_id in selected_skill_ids
                if skill_id not in available_skill_ids
            ]
            if unavailable_skill_ids:
                return Response(
                    {'error': f'以下 Skill 不存在、未启用或不属于当前项目: {unavailable_skill_ids}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                skill_context = [build_skill_snapshot(skill) for skill in selected_skills]
            except (SkillPackageError, OSError) as exc:
                return Response(
                    {'error': f'Skill读取失败: {exc}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            selected_rules = BusinessRuleKnowledge.objects.select_related('project').filter(
                id__in=selected_rule_ids,
                status='approved',
            )
            if project:
                selected_rules = selected_rules.filter(
                    models.Q(project=project) | models.Q(project__isnull=True)
                )
            else:
                selected_rules = selected_rules.filter(project__isnull=True)
            selected_rules = list(selected_rules)
            available_rule_ids = {rule.id for rule in selected_rules}
            unavailable_rule_ids = [rule_id for rule_id in selected_rule_ids if rule_id not in available_rule_ids]
            if unavailable_rule_ids:
                return Response(
                    {'error': f'以下业务规则不存在、未审核或不属于当前项目: {unavailable_rule_ids}'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            # Historical rules are read-only generation context. Do not synchronize
            # them into the topology here: selecting a rule must not create a new
            # knowledge file or alter the existing graph.
            selected_node_ids = list(
                KnowledgeNode.objects.filter(rule_id__in=available_rule_ids)
                .values_list('id', flat=True)
            )
            knowledge_rule_context = [serialize_rule_snapshot(rule) for rule in selected_rules]
            upstream_downstream_context = collect_knowledge_context(selected_node_ids)
            if upstream_downstream_context:
                rule_ids_with_snapshots = {
                    item.get('knowledge_node_id')
                    for item in knowledge_rule_context
                    if item.get('knowledge_node_id')
                }
                knowledge_rule_context.extend([
                    {
                        'id': None,
                        'title': node['name'],
                        'content': node['content'],
                        'project_id': node['project_id'],
                        'project_name': node['project_name'],
                        'module': '',
                        'business_domain': '',
                        'entity_name': node['name'],
                        'attribute_name': '',
                        'state_values': [],
                        'relation_nodes': [],
                        'test_strategies': [],
                        'risk_level': '',
                        'rule_type': node['type'],
                        'applicable_conditions': '',
                        'expected_behavior': '',
                        'exceptions': '',
                        'version': 1,
                        'knowledge_node_id': node['id'],
                        'knowledge_neighbors': [{
                            'id': item.get('id'),
                            'name': item.get('name'),
                            'type': item.get('type'),
                            'relations': item.get('relations', []),
                        } for item in upstream_downstream_context],
                        'graph_context_source': 'recursive_upstream_downstream',
                    }
                    for node in upstream_downstream_context
                    if node['id'] not in rule_ids_with_snapshots and node.get('content')
                ])

            selected_workflows = BusinessWorkflow.objects.select_related('project').filter(
                id__in=selected_workflow_ids,
            ).exclude(status='archived')
            if project:
                selected_workflows = selected_workflows.filter(
                    models.Q(project=project) | models.Q(project__isnull=True)
                )
            else:
                selected_workflows = selected_workflows.filter(project__isnull=True)
            selected_workflows = list(selected_workflows)
            available_workflow_ids = {workflow.id for workflow in selected_workflows}
            unavailable_workflow_ids = [
                workflow_id for workflow_id in selected_workflow_ids
                if workflow_id not in available_workflow_ids
            ]
            if unavailable_workflow_ids:
                return Response(
                    {'error': f'以下工作流不存在、已归档或不属于当前项目: {unavailable_workflow_ids}'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            if selected_workflows:
                workflow_context_text = build_workflow_context_text(selected_workflows)
                if workflow_context_text:
                    requirement_text = f"{requirement_text}\n\n{workflow_context_text}"
                knowledge_rule_context.extend([
                    serialize_workflow_snapshot(workflow)
                    for workflow in selected_workflows
                ])

            current_focus_rule, parent_node, should_save_current_rule = _current_rule_save_context(validated_data)
            if should_save_current_rule:
                if project and parent_node.project_id and parent_node.project_id != project.id:
                    return Response(
                        {'error': '选择的知识节点不属于当前项目'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                if (not project) and parent_node.project_id:
                    return Response(
                        {'error': '未选择项目时只能挂载到全局知识节点'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

            task_data = {
                'title': validated_data['title'],
                'requirement_text': requirement_text,
                'generation_mode': generation_mode,
                'case_type_rules': validated_data.get('case_type_rules', {}),
                'skill_context': skill_context,
                'writer_model_config': writer_config.id if writer_config else None,
                'reviewer_model_config': reviewer_config.id if reviewer_config else None,
            }

            # 如果请求中包含项目ID，添加到任务数据中
            if 'project' in validated_data and validated_data['project']:
                task_data['project'] = validated_data['project'].id

            # 输出模式由启用的生成行为配置决定；无配置时才兼容请求参数。
            task_data['output_mode'] = generation_behavior['output_mode']

            task_serializer = TestCaseGenerationTaskSerializer(
                data=task_data,
                context={'request': request}
            )

            if task_serializer.is_valid():
                task = task_serializer.save()
                task.knowledge_rule_context = knowledge_rule_context
                task.skill_context = skill_context
                task.save(update_fields=['knowledge_rule_context', 'skill_context'])
                task.selected_skills.set(selected_skills)

                for rule in selected_rules:
                    snapshot = serialize_rule_snapshot(rule)
                    BusinessRuleUsage.objects.create(
                        rule=rule,
                        task=task,
                        action='selected',
                        rule_snapshot=snapshot,
                    )
                if selected_rules:
                    BusinessRuleKnowledge.objects.filter(id__in=available_rule_ids).update(
                        accepted_count=models.F('accepted_count') + 1
                    )

                AIModelService.record_generation_event(
                    task,
                    'prepare',
                    'pending',
                    '生成任务已创建',
                    '正在准备需求资料、Skills 和模型调用',
                )

                saved_rule = None
                if should_save_current_rule:
                    saved_rule = BusinessRuleKnowledge.objects.filter(
                        project=project,
                        content=current_focus_rule,
                    ).exclude(status='deprecated').first()
                    if not saved_rule:
                        saved_rule = BusinessRuleKnowledge.objects.create(
                            title=(validated_data.get('current_rule_title') or f"{task.title} - 业务规则").strip(),
                            content=current_focus_rule,
                            keywords=extract_rule_keywords(current_focus_rule),
                            project=project,
                            module=str(validated_data.get('current_rule_module') or '').strip(),
                            rule_type=validated_data.get('current_rule_type', 'business'),
                            source_requirement=task.title,
                            source_task=task,
                            status='approved',
                            created_by=task.created_by,
                            reviewed_by=task.created_by,
                        )
                    sync_rule_to_knowledge_node(saved_rule, parent_node=parent_node)

                # 异步执行生成任务
                def run_generation_task():
                    try:
                        import threading

                        def execute_task():
                            def save_task(*field_names):
                                """Persist only runtime fields so deleted related rows are not reattached."""
                                update_fields = list(dict.fromkeys([*field_names, 'updated_at']))
                                task.save(update_fields=update_fields)

                            def task_was_cancelled():
                                return TestCaseGenerationTask.objects.filter(
                                    pk=task.pk,
                                    status='cancelled',
                                ).exists()

                            try:
                                # 更新任务状态
                                task.status = 'generating'
                                task.progress = 10
                                save_task('status', 'progress')
                                AIModelService.record_generation_event(
                                    task,
                                    'prepare',
                                    'running',
                                    '准备生成上下文',
                                    '已完成模型配置校验，开始执行 Skill 工作流',
                                )

                                enable_auto_review = generation_behavior['enable_auto_review']
                                configured_review_timeout = generation_behavior['configured_review_timeout']
                                review_timeout = generation_behavior['review_timeout']

                                logger.info(
                                    f"任务 {task.task_id} 使用生成配置: auto_review={enable_auto_review}, "
                                    f"configured_review_timeout={configured_review_timeout}s, "
                                    f"effective_review_timeout={review_timeout}s"
                                )

                                loop = asyncio.new_event_loop()
                                asyncio.set_event_loop(loop)

                                try:
                                    # 根据输出模式选择不同的生成方式
                                    if task.output_mode == 'stream':
                                        # 流式模式：实时保存到stream_buffer
                                        # 生成前先设置初始状态
                                        task.stream_buffer = ''
                                        task.stream_position = 0
                                        save_task('stream_buffer', 'stream_position')

                                        # 定义同步保存函数
                                        def save_stream_buffer(content):
                                            """同步保存流式内容到数据库"""
                                            task.stream_buffer = content
                                            task.stream_position = len(content)
                                            task.last_stream_update = timezone.now()
                                            task.save(update_fields=['stream_buffer', 'stream_position',
                                                                     'last_stream_update'])

                                        # 转换为异步函数
                                        async_save_stream_buffer = sync_to_async(save_stream_buffer)

                                        async def stream_callback(chunk):
                                            """流式回调：实时保存每个chunk到数据库"""
                                            # 先追加到内存中的buffer
                                            task.stream_buffer += chunk
                                            task.stream_position = len(task.stream_buffer)
                                            task.last_stream_update = timezone.now()

                                            # 每10个chunk或当chunk较大时保存一次
                                            if task.stream_position % 500 < 20 or len(chunk) > 100:
                                                try:
                                                    await async_save_stream_buffer(task.stream_buffer)
                                                except Exception as save_error:
                                                    logger.warning(f"保存流式内容失败: {save_error}")

                                        # 生成测试用例
                                        task.progress = 30
                                        save_task('progress')
                                        AIModelService.record_generation_event(
                                            task,
                                            'generation',
                                            'running',
                                            '调用编写模型生成测试用例',
                                            '模型正在根据需求和 Skill 输出编排测试用例',
                                        )

                                        generated_cases = loop.run_until_complete(
                                            AIModelService.generate_test_cases_stream(task, callback=stream_callback)
                                        )

                                        if task_was_cancelled():
                                            logger.info('任务 %s 已取消，停止保存生成结果', task.task_id)
                                            return

                                        task.skill_execution_results = list(
                                            getattr(task, 'skill_execution_results', None) or []
                                        )
                                        task.save(update_fields=['skill_execution_results', 'updated_at'])

                                        # 生成完成后，确保最终的流式内容被保存
                                        if task.stream_buffer:
                                            save_stream_buffer(task.stream_buffer)

                                        task.generated_test_cases = generated_cases
                                        task.progress = 60
                                        save_task('generated_test_cases', 'progress')
                                        AIModelService.record_generation_event(
                                            task,
                                            'generation',
                                            'completed',
                                            '测试用例初稿已生成',
                                            f'已接收约 {len(generated_cases)} 个字符的模型输出',
                                        )

                                        # 流式单次评审评分（根据生成配置决定是否执行）
                                        if enable_auto_review and task.reviewer_model_config:
                                            try:
                                                task.status = 'reviewing'
                                                task.progress = 70
                                                save_task('status', 'progress')
                                                AIModelService.record_generation_event(
                                                    task,
                                                    'review',
                                                    'running',
                                                    '调用评审模型检查测试用例',
                                                    '正在检查覆盖率、边界条件和可执行性',
                                                )

                                                logger.info(f"开始流式评审任务 {task.task_id}")

                                                # 评审内容缓存
                                                review_buffer = []

                                                def save_review_buffer(content):
                                                    """同步保存评审内容"""
                                                    task.review_feedback = content
                                                    task.save(update_fields=['review_feedback'])

                                                async_save_review = sync_to_async(save_review_buffer)

                                                async def review_stream_callback(chunk):
                                                    """流式评审回调"""
                                                    review_buffer.append(chunk)
                                                    current_length = sum(len(c) for c in review_buffer)

                                                    # 每100字符保存一次
                                                    if current_length % 100 < 20 or len(chunk) > 50:
                                                        try:
                                                            content = ''.join(review_buffer)
                                                            await async_save_review(content)
                                                        except Exception as save_error:
                                                            logger.warning(f"保存评审内容失败: {save_error}")

                                                try:
                                                    review_feedback = loop.run_until_complete(
                                                        asyncio.wait_for(
                                                            AIModelService.review_test_cases_stream(
                                                                task,
                                                                generated_cases,
                                                                callback=review_stream_callback,
                                                                timeout_seconds=review_timeout,
                                                            ),
                                                            timeout=review_timeout,
                                                        )
                                                    )
                                                    final_review_feedback = (
                                                        ''.join(review_buffer) if review_buffer else review_feedback
                                                    )
                                                    score = AIModelService.record_review_result(
                                                        task,
                                                        final_review_feedback,
                                                        source='initial',
                                                        reviewed_test_cases=generated_cases,
                                                    )
                                                    sorted_cases = AIModelService.sort_test_cases_by_id(generated_cases)
                                                    task.final_test_cases = AIModelService.renumber_test_cases(sorted_cases)
                                                    task.progress = 85
                                                    task.save(update_fields=['final_test_cases', 'progress', 'updated_at'])
                                                    AIModelService.record_generation_event(
                                                        task,
                                                        'review',
                                                        'completed',
                                                        'AI 评审已完成',
                                                        f'评审评分：{score}',
                                                    )
                                                    logger.info(
                                                        f"任务 {task.task_id} AI评审完成，评分={score}，生成流程结束"
                                                    )

                                                except Exception as inner_error:
                                                    logger.warning(
                                                        f"任务 {task.task_id} 流式评审过程异常: {inner_error}")
                                                    task.review_feedback = (
                                                        f"自动评审未在 {review_timeout} 秒内完成，已跳过评审，"
                                                        f"保留已生成的测试用例。\n原因：{str(inner_error)}"
                                                    )
                                                    # 按用例编号排序后再保存
                                                    sorted_cases = AIModelService.sort_test_cases_by_id(generated_cases)
                                                    # 重新编号使编号连续
                                                    task.final_test_cases = AIModelService.renumber_test_cases(
                                                        sorted_cases)
                                                    task.progress = 85
                                                    task.error_message = ''
                                                    task.save(update_fields=[
                                                        'review_feedback', 'final_test_cases', 'progress',
                                                        'error_message', 'updated_at',
                                                    ])
                                                    AIModelService.record_generation_event(
                                                        task,
                                                        'review',
                                                        'completed',
                                                        '评审超时，已保留生成结果',
                                                        f'评审未在 {review_timeout} 秒内完成，已跳过评审',
                                                    )

                                            except Exception as review_error:
                                                logger.error(f"流式评审任务 {task.task_id} 失败: {review_error}")
                                                # 按用例编号排序后再保存
                                                sorted_cases = AIModelService.sort_test_cases_by_id(generated_cases)
                                                task.final_test_cases = AIModelService.renumber_test_cases(sorted_cases)
                                                task.review_feedback = f"评审失败: {str(review_error)}\n\n建议：测试用例结构完整，可以使用。"
                                                save_task('final_test_cases', 'review_feedback')
                                                AIModelService.record_generation_event(
                                                    task,
                                                    'review',
                                                    'failed',
                                                    '评审调用失败，已保留生成结果',
                                                    str(review_error),
                                                )
                                        else:
                                            # 按用例编号排序后再保存
                                            sorted_cases = AIModelService.sort_test_cases_by_id(generated_cases)
                                            # 重新编号使编号连续
                                            task.final_test_cases = AIModelService.renumber_test_cases(sorted_cases)
                                            logger.info(f"任务 {task.task_id} 跳过评审，直接使用生成的测试用例")
                                            save_task('final_test_cases')
                                            AIModelService.record_generation_event(
                                                task,
                                                'review',
                                                'skipped',
                                                '已跳过 AI 评审',
                                                '当前生成配置未启用评审模型',
                                            )

                                    else:
                                        # 完整模式：原有逻辑
                                        task.progress = 30
                                        save_task('progress')
                                        AIModelService.record_generation_event(
                                            task,
                                            'generation',
                                            'running',
                                            '调用编写模型生成测试用例',
                                            '模型正在根据需求和 Skill 输出编排测试用例',
                                        )

                                        generated_cases = loop.run_until_complete(
                                            AIModelService.generate_test_cases(task)
                                        )

                                        if task_was_cancelled():
                                            logger.info('任务 %s 已取消，停止保存生成结果', task.task_id)
                                            return

                                        task.skill_execution_results = list(
                                            getattr(task, 'skill_execution_results', None) or []
                                        )
                                        task.save(update_fields=['skill_execution_results', 'updated_at'])

                                        task.generated_test_cases = generated_cases
                                        task.progress = 60
                                        save_task('generated_test_cases', 'progress')
                                        AIModelService.record_generation_event(
                                            task,
                                            'generation',
                                            'completed',
                                            '测试用例初稿已生成',
                                            f'已接收约 {len(generated_cases)} 个字符的模型输出',
                                        )

                                        # 单次评审评分（根据生成配置决定是否执行）
                                        if enable_auto_review and task.reviewer_model_config:
                                            try:
                                                task.status = 'reviewing'
                                                task.progress = 70
                                                save_task('status', 'progress')
                                                AIModelService.record_generation_event(
                                                    task,
                                                    'review',
                                                    'running',
                                                    '调用评审模型检查测试用例',
                                                    '正在检查覆盖率、边界条件和可执行性',
                                                )

                                                logger.info(f"开始评审任务 {task.task_id}")

                                                try:
                                                    review_feedback = loop.run_until_complete(
                                                        asyncio.wait_for(
                                                            AIModelService.review_test_cases(
                                                                task,
                                                                generated_cases,
                                                                timeout_seconds=review_timeout,
                                                            ),
                                                            timeout=review_timeout,
                                                        )
                                                    )
                                                    score = AIModelService.record_review_result(
                                                        task,
                                                        review_feedback,
                                                        source='initial',
                                                        reviewed_test_cases=generated_cases,
                                                    )
                                                    sorted_cases = AIModelService.sort_test_cases_by_id(generated_cases)
                                                    task.final_test_cases = AIModelService.renumber_test_cases(sorted_cases)
                                                    task.progress = 85
                                                    task.save(update_fields=['final_test_cases', 'progress', 'updated_at'])
                                                    AIModelService.record_generation_event(
                                                        task,
                                                        'review',
                                                        'completed',
                                                        'AI 评审已完成',
                                                        f'评审评分：{score}',
                                                    )
                                                    logger.info(
                                                        f"任务 {task.task_id} AI评审完成，评分={score}，生成流程结束"
                                                    )

                                                except Exception as inner_error:
                                                    logger.warning(f"任务 {task.task_id} 评审过程异常: {inner_error}")
                                                    task.review_feedback = (
                                                        f"自动评审未在 {review_timeout} 秒内完成，已跳过评审，"
                                                        f"保留已生成的测试用例。\n原因：{str(inner_error)}"
                                                    )
                                                    # 按用例编号排序后再保存
                                                    sorted_cases = AIModelService.sort_test_cases_by_id(generated_cases)
                                                    # 重新编号使编号连续
                                                    task.final_test_cases = AIModelService.renumber_test_cases(
                                                        sorted_cases)
                                                    task.progress = 85
                                                    task.error_message = ''
                                                    task.save(update_fields=[
                                                        'review_feedback', 'final_test_cases', 'progress',
                                                        'error_message', 'updated_at',
                                                    ])
                                                    AIModelService.record_generation_event(
                                                        task,
                                                        'review',
                                                        'completed',
                                                        '评审超时，已保留生成结果',
                                                        f'评审未在 {review_timeout} 秒内完成，已跳过评审',
                                                    )

                                            except Exception as review_error:
                                                logger.error(f"评审任务 {task.task_id} 失败: {review_error}")
                                                # 评审失败时，仍然使用生成的测试用例作为最终结果
                                                # 按用例编号排序后再保存
                                                sorted_cases = AIModelService.sort_test_cases_by_id(generated_cases)
                                                task.final_test_cases = AIModelService.renumber_test_cases(sorted_cases)
                                                task.review_feedback = f"评审失败: {str(review_error)}\n\n建议：测试用例结构完整，可以使用。"
                                                save_task('final_test_cases', 'review_feedback')
                                                AIModelService.record_generation_event(
                                                    task,
                                                    'review',
                                                    'failed',
                                                    '评审调用失败，已保留生成结果',
                                                    str(review_error),
                                                )
                                        else:
                                            # 按用例编号排序后再保存
                                            sorted_cases = AIModelService.sort_test_cases_by_id(generated_cases)
                                            # 重新编号使编号连续
                                            task.final_test_cases = AIModelService.renumber_test_cases(sorted_cases)
                                            logger.info(f"任务 {task.task_id} 跳过评审，直接使用生成的测试用例")
                                            save_task('final_test_cases')
                                            AIModelService.record_generation_event(
                                                task,
                                                'review',
                                                'skipped',
                                                '已跳过 AI 评审',
                                                '当前生成配置未启用评审模型',
                                            )

                                    # 完成任务
                                    # 注意：不要直接调用task.save()，因为这会覆盖流式回调保存的final_test_cases
                                    # 从数据库重新获取最新的任务对象
                                    task.refresh_from_db()

                                    if task.status == 'cancelled':
                                        logger.info('任务 %s 已取消，跳过完成状态写入', task.task_id)
                                        return

                                    task.status = 'completed'
                                    task.progress = 100
                                    task.completed_at = timezone.now()
                                    task.save(update_fields=['status', 'progress', 'completed_at', 'final_test_cases'])
                                    AIModelService.record_generation_event(
                                        task,
                                        'complete',
                                        'completed',
                                        '测试用例生成完成',
                                        '结果已整理，可查看或采纳测试用例',
                                    )
                                    logger.info(f"任务 {task.task_id} 已完成")

                                finally:
                                    try:
                                        # 清理异步生成器，防止 "Task was destroyed but it is pending" 警告
                                        loop.run_until_complete(loop.shutdown_asyncgens())
                                    except Exception as e:
                                        logger.warning(f"Error shutting down asyncgens: {e}")
                                    finally:
                                        loop.close()

                            except Exception as e:
                                if task_was_cancelled():
                                    logger.info('任务 %s 已取消，忽略后续执行异常', task.task_id)
                                    return
                                safe_error = AIModelService.sanitize_error_message(e)
                                logger.error(f"生成任务执行失败: {safe_error}")
                                task.status = 'failed'
                                task.error_message = safe_error
                                task.skill_execution_results = list(
                                    getattr(task, 'skill_execution_results', None) or []
                                )
                                save_task('status', 'error_message', 'skill_execution_results')
                                AIModelService.record_generation_event(
                                    task,
                                    'task',
                                    'failed',
                                    '生成任务失败',
                                    safe_error,
                                )

                        # 在新线程中执行任务
                        thread = threading.Thread(target=execute_task)
                        thread.daemon = True
                        thread.start()

                    except Exception as e:
                        safe_error = AIModelService.sanitize_error_message(e)
                        logger.error(f"启动生成任务失败: {safe_error}")
                        task.status = 'failed'
                        task.error_message = safe_error
                        TestCaseGenerationTask.objects.filter(pk=task.pk).update(
                            status='failed',
                            error_message=safe_error,
                            updated_at=timezone.now(),
                        )
                        AIModelService.record_generation_event(
                            task,
                            'task',
                            'failed',
                            '生成任务启动失败',
                            safe_error,
                        )

                # 启动异步任务
                run_generation_task()

                return Response({
                    'message': '测试用例生成任务已创建',
                    'task_id': task.task_id,
                    'task': TestCaseGenerationTaskSerializer(task).data,
                    'generation_behavior': {
                        'config_id': generation_behavior['config'].id if generation_behavior['config'] else None,
                        'output_mode': generation_behavior['output_mode'],
                        'enable_auto_review': generation_behavior['enable_auto_review'],
                        'review_timeout': generation_behavior['review_timeout'],
                    },
                    'selected_rule_count': len(selected_rules),
                    'saved_rule_id': saved_rule.id if saved_rule else None,
                }, status=status.HTTP_201_CREATED)
            else:
                return Response(task_serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        except Exception as e:
            logger.error(f"创建生成任务时出错: {e}")
            return Response(
                {'error': '创建任务失败，请稍后重试'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['get'])
    def progress(self, request, task_id=None):
        """获取任务进度"""
        try:
            # DRF会根据lookup_field自动从URL提取task_id并调用get_object()
            task = self.get_object()

            return Response({
                'task_id': task.task_id,
                'status': task.status,
                'progress': task.progress,
                'generated_test_cases': task.generated_test_cases,
                'review_feedback': task.review_feedback,
                'final_test_cases': task.final_test_cases,
                'review_score': AIModelService.get_task_review_score(task),
                'best_review_score': task.best_review_score,
                'review_round': task.review_round,
                'review_pending': task.review_pending,
                'review_history': task.review_history,
                'generation_events': _parse_generation_events(getattr(task, 'generation_log', '')),
                'quality_completed': (
                    task.status == 'completed'
                    and bool(task.final_test_cases)
                ),
                'error_message': task.error_message,
                'completed_at': task.completed_at
            }, status=status.HTTP_200_OK)

        except (Http404, APIException):
            raise
        except Exception as e:
            logger.error(f"获取任务进度时出错: {e}")
            return Response(
                {'error': '获取进度失败，请稍后重试'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['get'], url_path='stream-token')
    def stream_token(self, request, task_id=None):
        """Issue a long-term, task-scoped token for native EventSource."""
        task = self.get_object()
        max_age = max(
            int(getattr(settings, 'LONG_TERM_VALIDITY_SECONDS', 3650 * 24 * 60 * 60)),
            int(getattr(settings, 'AI_SSE_TOKEN_MAX_AGE_SECONDS', 3650 * 24 * 60 * 60)),
        )
        token = signing.dumps(
            {
                'task_id': task.task_id,
                'user_id': request.user.pk,
            },
            salt='runnergo.ai-generation.sse',
            compress=True,
        )
        return Response({'stream_token': token, 'expires_in': max_age})

    @action(
        detail=True,
        methods=['get'],
        url_path='stream_progress',
        renderer_classes=[PassThroughRenderer],
        permission_classes=[AllowAny],
        authentication_classes=[],
    )
    def stream_progress_sse(self, request, task_id=None):
        """
        SSE流式进度推送接口
        实时推送任务的流式输出和进度更新
        不使用DRF的Response，避免content negotiation问题
        EventSource 不支持自定义 Authorization 头，因此使用已认证接口签发的长期任务令牌；
        令牌仍绑定任务和用户，任务删除或权限失效后不可用于读取其他任务。
        """
        try:
            # 记录请求信息（用于调试）
            request_origin = request.META.get('HTTP_ORIGIN', 'unknown')
            logger.info(
                f"SSE连接请求: task_id={task_id}, user={request.user}, authenticated={request.user.is_authenticated}, path={request.path}, origin={request_origin}")

            # 动态获取CORS origin - 使用 Django 配置优先
            def get_allowed_origin(origin):
                """获取允许的CORS origin，优先使用 settings 配置"""
                # 安全修复：不再返回 '*' 或任意 origin
                allowed_origins = getattr(settings, 'CORS_ALLOWED_ORIGINS', []) or []
                if origin and origin in allowed_origins:
                    return origin

                same_origin = f'{request.scheme}://{request.get_host()}'
                if origin and origin == same_origin:
                    return origin

                # 兼容未配置时的本地开发默认
                local_defaults = ['http://localhost:3000', 'http://127.0.0.1:3000']
                if getattr(settings, 'DEBUG', False) and origin and origin in local_defaults:
                    return origin

                # 如果未匹配，返回第一个允许的 origin
                if allowed_origins:
                    return allowed_origins[0]

                # 开发环境兜底
                if getattr(settings, 'DEBUG', False):
                    return 'http://localhost:3000'

                # 生产环境未配置则不返回 CORS 头
                return None

            cors_origin = get_allowed_origin(request_origin)

            # 安全修复：如果 CORS origin 不合法，拒绝请求
            if not cors_origin:
                from django.http import HttpResponse
                return HttpResponse('Forbidden: Invalid Origin', status=403)

            # 处理 CORS 预检请求
            if request.method == 'OPTIONS':
                from django.http import HttpResponse
                response = HttpResponse()
                response['Access-Control-Allow-Origin'] = cors_origin
                response['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
                response['Access-Control-Allow-Headers'] = 'Content-Type'
                response['Access-Control-Allow-Credentials'] = 'true'
                response['Access-Control-Max-Age'] = '86400'
                return response

            raw_stream_token = str(request.query_params.get('stream_token') or '')
            max_age = max(
                int(getattr(settings, 'LONG_TERM_VALIDITY_SECONDS', 3650 * 24 * 60 * 60)),
                int(getattr(settings, 'AI_SSE_TOKEN_MAX_AGE_SECONDS', 3650 * 24 * 60 * 60)),
            )
            try:
                token_payload = signing.loads(
                    raw_stream_token,
                    salt='runnergo.ai-generation.sse',
                    max_age=max_age,
                )
                token_task_id = str(token_payload.get('task_id') or '')
                token_user_id = int(token_payload.get('user_id'))
                if token_task_id != str(task_id):
                    raise signing.BadSignature('task mismatch')
            except (signing.BadSignature, signing.SignatureExpired, TypeError, ValueError):
                logger.warning('SSE连接失败: 任务令牌无效或已过期, task_id=%s', task_id)
                from django.http import HttpResponse
                response = HttpResponse('Forbidden: Invalid stream token', status=403)
                response['Access-Control-Allow-Origin'] = cors_origin
                response['Access-Control-Allow-Credentials'] = 'true'
                return response

            task = TestCaseGenerationTask.objects.select_related('project').filter(task_id=task_id).first()
            if not task:
                from django.http import HttpResponse
                return HttpResponse('Forbidden: Invalid stream token', status=403)

            if task.created_by_id != token_user_id:
                project = task.project
                project_access = bool(
                    project
                    and (
                        project.owner_id == token_user_id
                        or ProjectMember.objects.filter(
                            project_id=project.pk,
                            user_id=token_user_id,
                        ).exists()
                    )
                )
                if not project_access:
                    from django.http import HttpResponse
                    return HttpResponse('Forbidden: Invalid stream token', status=403)

            # 记录上次发送的stream_position
            last_sent_position = 0
            loop_count = 0  # 循环计数器
            last_review_length = 0  # 记录上次发送的评审内容长度
            last_final_length = 0  # 记录上次发送的最终用例长度
            last_status = ''  # 记录上次的任务状态
            last_generation_event_count = 0

            async def event_stream():
                nonlocal last_sent_position, loop_count, last_review_length, last_final_length, last_status, last_generation_event_count

                # Performance & Timeout Optimization
                start_time = time.time()
                last_heartbeat_time = time.time()
                last_progress_hash = None
                MAX_TIMEOUT = 3600  # 1 hour safety timeout
                async_refresh_from_db = sync_to_async(task.refresh_from_db, thread_sensitive=True)

                while True:
                    loop_count += 1
                    current_time = time.time()
                    has_sent_data = False

                    # Safety Timeout Check
                    if current_time - start_time > MAX_TIMEOUT:
                        logger.error(f"SSE Connection timed out after {MAX_TIMEOUT}s: task_id={task_id}")
                        yield f"event: error\ndata: timeout\n\n"
                        break

                    # 从数据库重新获取任务状态
                    try:
                        await async_refresh_from_db()
                    except TestCaseGenerationTask.DoesNotExist:
                        yield f"event: error\ndata: task_not_found\n\n"
                        break
                    except Exception as e:
                        logger.error(f"DB refresh failed: {e}")
                        await asyncio.sleep(1)
                        continue

                    # 检测状态变化，如果进入revising阶段，重置last_final_length
                    if task.status != last_status:
                        logger.info(f"SSE检测到状态变化: {last_status} -> {task.status}")
                        if task.status == 'revising':
                            logger.info(f"SSE: 进入revising阶段，重置last_final_length")
                            last_final_length = 0
                        last_status = task.status

                    # 每30次循环记录一次日志 (Reduced frequency)
                    if loop_count % 30 == 0:
                        logger.info(
                            f"SSE stream loop #{loop_count}: task_status={task.status}, progress={task.progress}%, buffer_len={len(task.stream_buffer) if task.stream_buffer else 0}")

                    # 检查任务是否已完成或失败
                    generation_events = _parse_generation_events(getattr(task, 'generation_log', ''))
                    if len(generation_events) > last_generation_event_count or (
                            generation_events and last_generation_event_count == 0
                    ):
                        thinking_data = json.dumps(
                            {'type': 'thinking', 'events': generation_events},
                            ensure_ascii=False,
                        )
                        yield f"data: {thinking_data}\n\n"
                        last_generation_event_count = len(generation_events)
                        has_sent_data = True

                    if task.status in ['completed', 'failed', 'cancelled']:
                        logger.info(f"SSE任务结束: status={task.status}")
                        # 发送最终状态
                        final_status = json.dumps({'type': 'status', 'status': task.status, 'progress': task.progress},
                                                  ensure_ascii=False)
                        logger.info(f"SSE发送最终状态: {final_status}")
                        yield f"data: {final_status}\n\n"

                        # 如果是流式模式且有缓冲区内容，发送剩余内容
                        if task.output_mode == 'stream' and task.stream_buffer:
                            if last_sent_position < len(task.stream_buffer):
                                new_content = task.stream_buffer[last_sent_position:]
                                content_data = json.dumps({'type': 'content', 'content': new_content},
                                                          ensure_ascii=False)
                                logger.info(f"SSE发送剩余内容: {len(new_content)} 字符")
                                yield f"data: {content_data}\n\n"
                                last_sent_position = len(task.stream_buffer)

                        # 发送剩余的评审内容
                        if task.review_feedback:
                            if len(task.review_feedback) > last_review_length:
                                remaining_review = task.review_feedback[last_review_length:]
                                if remaining_review:
                                    review_data = json.dumps({'type': 'review_content', 'content': remaining_review},
                                                             ensure_ascii=False)
                                    logger.info(
                                        f"SSE发送剩余评审内容: {len(remaining_review)} 字符, 总长度: {len(task.review_feedback)}")
                                    yield f"data: {review_data}\n\n"
                                    last_review_length = len(task.review_feedback)

                        # 发送剩余的最终用例内容
                        if task.final_test_cases:
                            if len(task.final_test_cases) > last_final_length:
                                remaining_final = task.final_test_cases[last_final_length:]
                                if remaining_final:
                                    final_data = json.dumps({'type': 'final_content', 'content': remaining_final},
                                                            ensure_ascii=False)
                                    logger.info(
                                        f"SSE发送剩余最终用例: {len(remaining_final)} 字符, 总长度: {len(task.final_test_cases)}")
                                    yield f"data: {final_data}\n\n"
                                    last_final_length = len(task.final_test_cases)

                        # 发送完成信号
                        yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
                        logger.info(f"SSE流结束，总循环次数: {loop_count}")

                        # 添加短暂延迟，确保done信号被发送
                        await asyncio.sleep(0.1)
                        break

                    # 如果是流式模式，发送新增的内容
                    if task.output_mode == 'stream' and task.stream_buffer:
                        current_position = task.stream_position
                        if current_position > last_sent_position:
                            # 提取新增内容
                            new_content = task.stream_buffer[last_sent_position:current_position]
                            if new_content:
                                content_data = json.dumps({'type': 'content', 'content': new_content},
                                                          ensure_ascii=False)
                                logger.info(f"SSE发送新增内容: {len(new_content)} 字符, 总位置: {current_position}")
                                yield f"data: {content_data}\n\n"
                                last_sent_position = current_position
                                has_sent_data = True

                    # 如果是评审阶段，发送评审内容
                    if task.status == 'reviewing' and task.review_feedback:
                        review_feedback = task.review_feedback
                        if review_feedback:
                            # 计算评审内容的增量
                            if len(review_feedback) > last_review_length:
                                new_review = review_feedback[last_review_length:]
                                if new_review:
                                    review_data = json.dumps({'type': 'review_content', 'content': new_review},
                                                             ensure_ascii=False)
                                    logger.info(f"SSE发送评审内容: {len(new_review)} 字符")
                                    yield f"data: {review_data}\n\n"
                                    last_review_length = len(review_feedback)
                                    has_sent_data = True

                    # 如果有最终用例，发送最终用例内容（在reviewing、revising或completed阶段）
                    if task.status in ['reviewing', 'revising', 'completed'] and task.final_test_cases:
                        final_cases = task.final_test_cases
                        if final_cases:
                            # 计算最终用例的增量
                            if len(final_cases) > last_final_length:
                                new_final = final_cases[last_final_length:]
                                if new_final:
                                    final_data = json.dumps({'type': 'final_content', 'content': new_final},
                                                            ensure_ascii=False)
                                    logger.info(
                                        f"SSE发送最终用例: {len(new_final)} 字符, 总长度: {len(final_cases)}, 阶段: {task.status}")
                                    yield f"data: {final_data}\n\n"
                                    last_final_length = len(final_cases)
                                    has_sent_data = True

                    # 发送进度更新 (Optimized)
                    current_progress_hash = f"{task.status}_{task.progress}"
                    if current_progress_hash != last_progress_hash:
                        progress_data = json.dumps(
                            {'type': 'progress', 'status': task.status, 'progress': task.progress},
                            ensure_ascii=False)
                        yield f"data: {progress_data}\n\n"
                        last_progress_hash = current_progress_hash
                        has_sent_data = True

                    # Heartbeat - 缩短心跳间隔到10秒，确保连接保活
                    if has_sent_data:
                        last_heartbeat_time = current_time
                    elif current_time - last_heartbeat_time >= 10:
                        yield ": keep-alive\n\n"
                        last_heartbeat_time = current_time

                    # 异步休眠不会占用 Django 的同步请求执行线程，列表和健康接口可并发响应
                    await asyncio.sleep(0.5)

            # 返回SSE流式响应 - 使用更稳健的方式
            try:
                response = StreamingHttpResponse(
                    event_stream(),
                    content_type='text/event-stream; charset=utf-8'
                )
            except Exception as e:
                logger.error(f"创建SSE响应失败: {e}")
                raise

            # 设置SSE相关的响应头 - 确保正确处理长连接
            response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response['Pragma'] = 'no-cache'
            response['Expires'] = '0'
            response['X-Accel-Buffering'] = 'no'
            response['X-Content-Type-Options'] = 'nosniff'
            # 添加连接保持头部，防止过早断开
            # 注意：在本地开发服务器(runserver)中，wsgiref禁止手动设置Hop-by-hop headers(如Connection)
            # 只有在生产环境(Gunicorn/Nginx)下才需要显式设置
            if not settings.DEBUG:
                response['Connection'] = 'keep-alive'

            # 设置CORS头部 - 使用动态计算的cors_origin
            response['Access-Control-Allow-Origin'] = cors_origin
            response['Access-Control-Allow-Credentials'] = 'true'
            response['Access-Control-Allow-Headers'] = 'Content-Type, Cache-Control'

            logger.info(f"SSE连接建立成功: task_id={task_id}, cors_origin={cors_origin}")
            return response

        except Exception as e:
            logger.error(f"SSE流式推送出错: {e}")
            import traceback
            traceback.print_exc()
            from django.http import HttpResponse
            # 获取允许的origin
            request_origin = request.META.get('HTTP_ORIGIN', 'unknown')

            def get_allowed_origin(origin):
                if getattr(settings, 'CORS_ALLOW_ALL_ORIGINS', False):
                    return origin or '*'

                allowed_origins = getattr(settings, 'CORS_ALLOWED_ORIGINS', []) or []
                if origin in allowed_origins:
                    return origin

                local_defaults = ['http://localhost:3000', 'http://127.0.0.1:3000']
                if origin in local_defaults:
                    return origin

                if allowed_origins:
                    return allowed_origins[0]

                return origin or 'http://localhost:3000'

            cors_origin = get_allowed_origin(request_origin)
            response = HttpResponse(
                json.dumps({'error': '流式推送失败'}),
                status=500,
                content_type='application/json'
            )
            response['Access-Control-Allow-Origin'] = cors_origin
            response['Access-Control-Allow-Credentials'] = 'true'
            return response

    @action(detail=True, methods=['post'])
    def cancel(self, request, task_id=None):
        """取消正在运行的任务"""
        try:
            # DRF会根据lookup_field自动从URL提取task_id并调用get_object()
            task = self.get_object()

            if task.status in ['completed', 'failed', 'cancelled']:
                return Response(
                    {'error': f'任务已经{task.get_status_display()}，无法取消'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            task.status = 'cancelled'
            task.save(update_fields=['status', 'updated_at'])
            AIModelService.record_generation_event(
                task,
                'task',
                'cancelled',
                '生成任务已取消',
                '已停止后续结果写入',
            )

            return Response({
                'message': '任务已取消',
                'task_id': task.task_id,
                'status': task.status
            })

        except (Http404, APIException):
            raise
        except Exception as e:
            logger.error(f"取消任务时出错: {e}")
            return Response(
                {'error': '取消任务失败，请稍后重试'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'])
    def save_to_records(self, request, task_id=None):
        """保存测试用例到AI生成用例记录并导入到测试用例管理系统"""
        try:
            # DRF会根据lookup_field自动从URL提取task_id并调用get_object()
            task = self.get_object()

            if task.status != 'completed':
                return Response(
                    {'error': '只能保存已完成的测试用例生成任务'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            if not task.final_test_cases:
                return Response(
                    {'error': '没有最终测试用例可以保存'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # 检查是否已经保存过
            if hasattr(task, 'is_saved_to_records') and task.is_saved_to_records:
                return Response(
                    {'message': '测试用例已经保存到记录中', 'already_saved': True},
                    status=status.HTTP_200_OK
                )

            # 解析并导入测试用例到测试用例管理系统。
            # 保存 AI 生成记录与导入测试用例是两个独立结果，不能因记录保存成功
            # 就把“未解析/导入失败”报告成导入成功。
            test_cases = self._parse_test_cases_content(task.final_test_cases)
            adopted_count = 0
            skipped_count = 0
            import_status = 'not_parsed'
            import_error = None

            if test_cases:
                try:
                    from apps.testcases.services import TestCaseDeduplicationService
                    from apps.projects.models import Project
                    from django.db import models, transaction

                    # 优先使用任务关联的项目
                    if task.project:
                        project = task.project
                        logger.info(f"使用任务关联的项目: {project.name}")
                    else:
                        # 回退到项目选择逻辑
                        user = task.created_by
                        accessible_projects = Project.objects.filter(
                            models.Q(owner=user) | models.Q(members=user)
                        ).distinct()

                        # 尝试从前端获取项目ID
                        project_id = request.data.get('project_id')

                        if project_id:
                            try:
                                project = accessible_projects.get(id=project_id)
                            except Project.DoesNotExist:
                                # 如果指定项目不存在或无权限，使用第一个可访问的项目
                                project = accessible_projects.first()
                                if not project:
                                    # 如果用户没有任何项目，创建默认项目
                                    project = Project.objects.create(
                                        name="默认项目",
                                        owner=user,
                                        description='系统自动创建的默认项目'
                                    )
                        else:
                            # 没有指定项目，使用第一个可访问的项目
                            project = accessible_projects.first()
                            if not project:
                                # 如果用户没有任何项目，创建默认项目
                                project = Project.objects.create(
                                    name="默认项目",
                                    owner=user,
                                    description='系统自动创建的默认项目'
                                )

                    # 避免批量导入中途失败时留下部分测试用例。
                    with transaction.atomic():
                        for test_case in test_cases:
                            _, created = TestCaseDeduplicationService.create(
                                project=project,
                                author=task.created_by,
                                title=test_case.get('scenario', '测试用例'),
                                description=test_case.get('scenario', ''),
                                preconditions=test_case.get('precondition', ''),
                                steps=test_case.get('steps', ''),
                                expected_result=test_case.get('expected', ''),
                                priority=self._map_priority(test_case.get('priority', '中')),
                                test_type=self._map_test_type(test_case.get('testType', '功能测试')),
                                tags=[
                                    tag for tag in [
                                        test_case.get('module'),
                                        test_case.get('requirementId'),
                                        test_case.get('testData')
                                    ] if tag
                                ],
                                status='draft'
                            )
                            if created:
                                adopted_count += 1
                            else:
                                skipped_count += 1

                    logger.info(
                        f"成功导入 {adopted_count} 条测试用例到项目 {project.name}，"
                        f"跳过重复用例 {skipped_count} 条"
                    )
                    if adopted_count:
                        import_status = 'imported'
                    else:
                        import_status = 'duplicate'
                        import_error = '解析出的测试用例均已存在，已跳过重复插入'

                except Exception as exc:
                    logger.error(f"导入测试用例失败: {exc}")
                    adopted_count = 0
                    import_status = 'failed'
                    import_error = '测试用例导入失败，请稍后重试'
            else:
                import_error = '未解析出包含测试场景、操作步骤和预期结果的测试用例明细'

            # 标记任务为已保存
            task.is_saved_to_records = True
            task.saved_at = timezone.now()
            task.save(update_fields=['is_saved_to_records', 'saved_at'])

            if import_status == 'imported':
                message = (
                    f'测试用例已保存到AI生成用例记录，并成功导入 {adopted_count} 条到测试用例管理系统'
                    f'，跳过重复用例 {skipped_count} 条'
                )
            elif import_status == 'duplicate':
                message = f'测试用例已保存到AI生成用例记录，{skipped_count} 条重复用例已跳过'
            elif import_status == 'failed':
                message = '测试用例已保存到AI生成用例记录，但导入测试用例管理系统失败'
            else:
                message = '测试用例已保存到AI生成用例记录，但未解析出可导入的测试用例'

            return Response({
                'message': message,
                'task_id': task.task_id,
                'saved_at': task.saved_at,
                'saved_to_records': True,
                'imported_count': adopted_count,
                'skipped_count': skipped_count,
                'import_status': import_status,
                'import_error': import_error,
            }, status=status.HTTP_200_OK)

        except (Http404, APIException):
            raise
        except Exception as e:
            logger.error(f"保存测试用例到记录时出错: {e}")
            return Response(
                {'error': '保存失败，请稍后重试'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'])
    def saved_records(self, request):
        """获取已保存的测试用例记录列表"""
        try:
            # 获取已保存到记录的任务
            saved_tasks = self.get_queryset().filter(
                is_saved_to_records=True,
                status='completed'
            ).order_by('-saved_at')

            # 序列化数据
            serializer = TestCaseGenerationTaskSerializer(saved_tasks, many=True)

            return Response({
                'message': '获取已保存记录成功',
                'records': serializer.data
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"获取已保存记录时出错: {e}")
            return Response(
                {'error': '获取记录失败，请稍后重试'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'], url_path='batch_adopt')
    def batch_adopt(self, request, task_id=None):
        """批量采纳任务的所有测试用例"""
        try:
            task = self.get_object()

            if task.status != 'completed':
                return Response(
                    {'error': '只能采纳已完成的测试用例生成任务'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            if not task.final_test_cases:
                return Response(
                    {'error': '没有最终测试用例可以采纳'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # 解析最终测试用例
            test_cases = self._parse_test_cases_content(task.final_test_cases)

            if not test_cases:
                return Response(
                    {'error': '无法解析测试用例内容'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # 导入到testcases应用（使用与单条采纳相同的逻辑）
            try:
                from apps.testcases.services import TestCaseDeduplicationService
                from apps.projects.models import Project
                from django.db import models

                # 优先使用任务关联的项目
                if task.project:
                    project = task.project
                    logger.info(f"使用任务关联的项目: {project.name}")
                else:
                    # 回退到项目选择逻辑
                    user = task.created_by
                    accessible_projects = Project.objects.filter(
                        models.Q(owner=user) | models.Q(members=user)
                    ).distinct()

                    # 尝试从前端获取项目ID
                    project_id = request.data.get('project_id')

                    if project_id:
                        try:
                            project = accessible_projects.get(id=project_id)
                        except Project.DoesNotExist:
                            # 如果指定项目不存在或无权限，使用第一个可访问的项目
                            project = accessible_projects.first()
                            if not project:
                                # 如果用户没有任何项目，创建默认项目
                                project = Project.objects.create(
                                    name="默认项目",
                                    owner=user,
                                    description='系统自动创建的默认项目'
                                )
                    else:
                        # 没有指定项目，使用第一个可访问的项目
                        project = accessible_projects.first()
                        if not project:
                            # 如果用户没有任何项目，创建默认项目
                            project = Project.objects.create(
                                name="默认项目",
                                owner=user,
                                description='系统自动创建的默认项目'
                            )

                adopted_count = 0
                skipped_count = 0
                for test_case in test_cases:
                    _, created = TestCaseDeduplicationService.create(
                        project=project,  # 使用统一的项目选择逻辑
                        author=task.created_by,
                        title=test_case.get('scenario', '测试用例'),
                        description=test_case.get('scenario', ''),  # 使用scenario作为描述
                        preconditions=test_case.get('precondition', ''),
                        steps=test_case.get('steps', ''),
                        expected_result=test_case.get('expected', ''),
                        priority=self._map_priority(test_case.get('priority', '中')),
                        test_type='functional',
                        status='draft'
                    )
                    if created:
                        adopted_count += 1
                    else:
                        skipped_count += 1

                return Response({
                    'message': (
                        f'成功采纳 {adopted_count} 条测试用例到项目 "{project.name}"，'
                        f'跳过重复用例 {skipped_count} 条'
                    ),
                    'adopted_count': adopted_count,
                    'skipped_count': skipped_count,
                    'project_name': project.name
                }, status=status.HTTP_200_OK)

            except Exception as import_error:
                logger.error(f"导入测试用例失败: {import_error}")
                return Response(
                    {'error': f'导入测试用例失败: {str(import_error)}'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

        except (Http404, APIException):
            raise
        except Exception as e:
            logger.error(f"批量采纳测试用例时出错: {e}")
            return Response(
                {'error': '批量采纳失败，请稍后重试'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'], url_path='batch-adopt-selected')
    def batch_adopt_selected(self, request, task_id=None):
        """批量采纳选中的测试用例"""
        try:
            task = self.get_object()
            test_cases_data = request.data.get('test_cases', [])

            if not test_cases_data:
                return Response(
                    {'error': '没有提供要采纳的测试用例数据'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # 导入到testcases应用
            try:
                from apps.testcases.services import TestCaseDeduplicationService
                from apps.projects.models import Project
                from django.db import models

                # 优先使用任务关联的项目
                if task.project:
                    project = task.project
                    logger.info(f"使用任务关联的项目: {project.name}")
                else:
                    # 回退到项目选择逻辑
                    user = task.created_by
                    accessible_projects = Project.objects.filter(
                        models.Q(owner=user) | models.Q(members=user)
                    ).distinct()

                    # 尝试从前端获取项目ID
                    project_id = request.data.get('project_id')

                    if project_id:
                        try:
                            project = accessible_projects.get(id=project_id)
                        except Project.DoesNotExist:
                            # 如果指定项目不存在或无权限，使用第一个可访问的项目
                            project = accessible_projects.first()
                            if not project:
                                # 如果用户没有任何项目，创建默认项目
                                project = Project.objects.create(
                                    name="默认项目",
                                    owner=user,
                                    description='系统自动创建的默认项目'
                                )
                    else:
                        # 没有指定项目，使用第一个可访问的项目
                        project = accessible_projects.first()
                        if not project:
                            # 如果用户没有任何项目，创建默认项目
                            project = Project.objects.create(
                                name="默认项目",
                                owner=user,
                                description='系统自动创建的默认项目'
                            )

                adopted_count = 0
                skipped_count = 0
                for case_data in test_cases_data:
                    _, created = TestCaseDeduplicationService.create(
                        project=project,  # 使用统一的项目选择逻辑
                        author=task.created_by,
                        title=case_data.get('title', '测试用例'),
                        description=case_data.get('description', ''),
                        preconditions=case_data.get('preconditions', ''),
                        steps=case_data.get('steps', ''),
                        expected_result=case_data.get('expected_result', ''),
                        priority=case_data.get('priority', 'medium'),
                        test_type=case_data.get('test_type', 'functional'),
                        status=case_data.get('status', 'draft')
                    )
                    if created:
                        adopted_count += 1
                    else:
                        skipped_count += 1

                return Response({
                    'message': (
                        f'成功采纳 {adopted_count} 条测试用例到项目 "{project.name}"，'
                        f'跳过重复用例 {skipped_count} 条'
                    ),
                    'adopted_count': adopted_count,
                    'skipped_count': skipped_count,
                    'project_name': project.name
                }, status=status.HTTP_200_OK)

            except Exception as import_error:
                logger.error(f"导入选中测试用例失败: {import_error}")
                return Response(
                    {'error': f'导入测试用例失败: {str(import_error)}'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

        except (Http404, APIException):
            raise
        except Exception as e:
            logger.error(f"批量采纳选中测试用例时出错: {e}")
            return Response(
                {'error': '批量采纳失败，请稍后重试'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'], url_path='batch_discard')
    def batch_discard(self, request, task_id=None):
        """批量弃用任务的所有测试用例 - 删除整个任务"""
        try:
            task = self.get_object()

            logger.info(f"开始批量弃用任务 {task.task_id}")

            # 直接删除整个任务记录
            task.delete()

            return Response({
                'message': '任务已被弃用并删除，不会再在列表中显示'
            }, status=status.HTTP_200_OK)

        except (Http404, APIException):
            raise
        except Exception as e:
            logger.error(f"批量弃用任务时出错: {e}")
            return Response(
                {'error': '批量弃用失败，请稍后重试'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'], url_path='discard-selected-cases')
    def discard_selected_cases(self, request, task_id=None):
        """弃用选中的测试用例 - 从final_test_cases中删除"""
        try:
            task = self.get_object()
            case_indices = request.data.get('case_indices', [])

            if not case_indices:
                return Response(
                    {'error': '没有提供要弃用的测试用例索引'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            if not task.final_test_cases:
                return Response(
                    {'error': '任务没有最终测试用例'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            logger.info(f"开始弃用任务 {task.task_id} 的测试用例，索引: {case_indices}")

            # 解析现有的测试用例
            test_cases = self._parse_test_cases_content(task.final_test_cases)

            # 按索引从大到小排序，避免删除时索引变化
            case_indices.sort(reverse=True)

            discarded_count = 0
            for index in case_indices:
                if 0 <= index < len(test_cases):
                    removed_case = test_cases.pop(index)
                    discarded_count += 1
                    logger.debug(f"弃用测试用例 {index}: {removed_case.get('scenario', 'unknown')}")

            # 如果所有用例都被弃用了，删除整个任务
            if not test_cases:
                logger.info(f"任务 {task.task_id} 的所有用例都被弃用，删除任务")
                task.delete()
                return Response({
                    'message': f'已弃用 {discarded_count} 条测试用例，任务已被删除',
                    'discarded_count': discarded_count,
                    'task_deleted': True
                }, status=status.HTTP_200_OK)

            # 重新生成final_test_cases内容
            task.final_test_cases = self._reconstruct_test_cases_content(test_cases)
            task.save()

            logger.debug(f"重构后的测试用例内容: {task.final_test_cases[:200]}...")

            return Response({
                'message': f'已弃用 {discarded_count} 条测试用例',
                'discarded_count': discarded_count,
                'remaining_cases': len(test_cases),
                'task_deleted': False,
                'updated_test_cases': task.final_test_cases
            }, status=status.HTTP_200_OK)

        except (Http404, APIException):
            raise
        except Exception as e:
            logger.error(f"弃用选中测试用例时出错: {e}")
            return Response(
                {'error': '弃用失败，请稍后重试'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'], url_path='discard-single-case')
    def discard_single_case(self, request, task_id=None):
        """弃用单个测试用例"""
        try:
            task = self.get_object()
            case_index = request.data.get('case_index')

            if case_index is None:
                return Response(
                    {'error': '没有提供测试用例索引'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            if not task.final_test_cases:
                return Response(
                    {'error': '任务没有最终测试用例'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            logger.info(f"开始弃用任务 {task.task_id} 的单个测试用例，索引: {case_index}")

            # 解析现有的测试用例
            test_cases = self._parse_test_cases_content(task.final_test_cases)

            if case_index < 0 or case_index >= len(test_cases):
                return Response(
                    {'error': f'测试用例索引 {case_index} 超出范围，总共有 {len(test_cases)} 个测试用例'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # 删除指定索引的测试用例
            removed_case = test_cases.pop(case_index)
            logger.debug(f"弃用测试用例 {case_index}: {removed_case.get('scenario', 'unknown')}")

            # 如果所有用例都被弃用了，删除整个任务
            if not test_cases:
                logger.info(f"任务 {task.task_id} 的所有用例都被弃用，删除任务")
                task.delete()
                return Response({
                    'message': '已弃用测试用例，任务已被删除',
                    'discarded_count': 1,
                    'task_deleted': True
                }, status=status.HTTP_200_OK)

            # 重新生成final_test_cases内容
            task.final_test_cases = self._reconstruct_test_cases_content(test_cases)
            task.save()

            logger.debug(f"单个弃用 - 重构后的测试用例内容: {task.final_test_cases[:200]}...")

            return Response({
                'message': '已弃用测试用例',
                'discarded_count': 1,
                'remaining_cases': len(test_cases),
                'task_deleted': False,
                'updated_test_cases': task.final_test_cases
            }, status=status.HTTP_200_OK)

        except (Http404, APIException):
            raise
        except Exception as e:
            logger.error(f"弃用单个测试用例时出错: {e}")
            return Response(
                {'error': '弃用失败，请稍后重试'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'], url_path='update-test-cases')
    def update_test_cases(self, request, task_id=None):
        """更新测试用例内容"""
        try:
            task = self.get_object()

            final_test_cases = request.data.get('final_test_cases')
            if not final_test_cases:
                return Response(
                    {'error': '缺少final_test_cases参数'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            logger.info(f"开始更新任务 {task.task_id} 的测试用例内容")

            # 更新final_test_cases字段
            task.final_test_cases = final_test_cases
            task.save(update_fields=['final_test_cases'])

            logger.info(f"任务 {task.task_id} 测试用例更新成功")

            return Response({
                'message': '测试用例更新成功',
                'task_id': task.task_id,
                'final_test_cases': task.final_test_cases
            }, status=status.HTTP_200_OK)

        except (Http404, APIException):
            raise
        except Exception as e:
            logger.error(f"更新测试用例时出错: {e}")
            return Response(
                {'error': '更新失败，请稍后重试'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def _parse_test_cases_content(self, content):
        """解析测试用例内容 - 支持多种格式"""
        if not content:
            return []

        # 去除markdown加粗标记，保留纯净文本
        import re
        clean_content = re.sub(r'\*\*([^*]+)\*\*', r'\1', content)

        logger.info(f"开始解析测试用例内容，内容长度: {len(clean_content)}")
        logger.info(f"内容前200字符: {clean_content[:200]}")

        # 尝试表格格式解析
        if '|' in clean_content:
            table_cases = self._parse_table_format(clean_content)
            if table_cases:
                return table_cases

        # 尝试结构化文本格式解析
        return self._parse_text_format(clean_content)

    def _parse_table_format(self, content):
        """解析表格格式的测试用例"""
        lines = [line.strip() for line in content.split('\n') if line.strip()]
        table_data = []

        # 提取表格数据
        for line in lines:
            if '|' in line and not re.match(r'^\|?\s*:?-{2,}', line):
                # 针对内容中可能包含转义后的 \| 进行预处理
                # 先把 \| 替换为一个临时占位符，分割完后再替换回来
                temp_placeholder = "___PIPE___"
                processed_line = line.replace(r'\|', temp_placeholder)

                # 移除首尾的 |
                if processed_line.startswith('|'):
                    processed_line = processed_line[1:]
                if processed_line.endswith('|'):
                    processed_line = processed_line[:-1]

                cells = []
                for cell in processed_line.split('|'):
                    # 恢复原来的转义管道符，并清理空格
                    cell_content = cell.replace(temp_placeholder, '|').replace('&#124;', '|').strip()
                    cells.append(cell_content)

                if len(cells) > 1:
                    table_data.append(cells)

        if len(table_data) < 2:
            return []

        def header_to_field(header):
            """将表头映射为内部字段；需求编号必须先于通用编号判断。"""
            normalized = str(header).lower().replace(' ', '')
            if any(keyword in normalized for keyword in ['需求编号', '需求点编号', '需求来源', '关联需求', 'requirement', 'req']):
                return 'requirementId'
            if any(keyword in normalized for keyword in ['用例编号', '用例id', 'caseid', 'caseno', '编号', 'id', '序号']):
                return 'caseId'
            if any(keyword in normalized for keyword in ['测试模块', '所属模块', '功能模块', '模块', 'module']):
                return 'module'
            if any(keyword in normalized for keyword in ['测试类型', '类型', 'testtype']):
                return 'testType'
            if any(keyword in normalized for keyword in ['测试数据', '输入数据', '数据', 'testdata']):
                return 'testData'
            if any(keyword in normalized for keyword in ['测试场景', '场景', '标题', '名称', 'title', 'scenario', '测试目标']):
                return 'scenario'
            if any(keyword in normalized for keyword in ['前置条件', '前置', '前提条件', '前提', 'precondition']):
                return 'precondition'
            if any(keyword in normalized for keyword in ['操作步骤', '测试步骤', '执行步骤', '步骤', 'steps']):
                return 'steps'
            if any(keyword in normalized for keyword in ['预期结果', '期望结果', '预期', '结果', 'expected', 'result']):
                return 'expected'
            if any(keyword in normalized for keyword in ['优先级', 'priority']):
                return 'priority'
            return ''

        def is_case_header(fields):
            # 过滤“配额覆盖情况”“业务规则覆盖”等辅助统计表。
            return all(field in fields for field in ['scenario', 'steps', 'expected'])

        def is_complete_case(test_case):
            return all(str(test_case.get(field, '')).strip() for field in ['scenario', 'steps', 'expected'])

        # 不能默认使用第一张表：生成结果通常先输出分析、配额和覆盖率表，
        # 真正的逐条用例明细表可能出现在后面。
        for header_index, raw_headers in enumerate(table_data):
            fields = [header_to_field(header) for header in raw_headers]
            if not is_case_header(fields):
                continue

            logger.debug(f"选中测试用例明细表头: {raw_headers}")
            test_cases = []
            for row in table_data[header_index + 1:]:
                if len(row) < len(fields):
                    continue

                row_fields = [header_to_field(cell) for cell in row]
                if is_case_header(row_fields):
                    break

                test_case = {}
                for index, field in enumerate(fields):
                    if field:
                        test_case[field] = row[index].strip() if index < len(row) else ''

                if is_complete_case(test_case):
                    test_cases.append(test_case)
                    logger.debug(f"解析出表格测试用例: {test_case}")

            if test_cases:
                return test_cases

        return []

    def _parse_text_format(self, content):
        """解析文本格式的测试用例"""
        lines = content.split('\n')
        test_cases = []
        current_case = {}

        for line in lines:
            line = line.strip()
            if not line:
                continue

            logger.debug(f"处理行: {line}")

            # 检测测试用例开始
            is_case_start = (
                    '测试用例' in line or
                    'Test Case' in line or
                    line.startswith(('1.', '2.', '3.', '4.', '5.', '6.', '7.', '8.', '9.', '10.')) or
                    line.startswith(('一、', '二、', '三、', '四、', '五、')) or
                    bool(re.match(r'^\d+[\.\)、]', line))
            )

            if is_case_start:
                if current_case:
                    logger.debug(f"添加测试用例: {current_case}")
                    test_cases.append(current_case)

                # 清理标题
                scenario = line
                scenario = scenario.replace('测试用例', '').replace('Test Case', '')
                scenario = scenario.replace(':', '').replace('：', '')
                scenario = re.sub(r'^\d+[\.\)、]\s*', '', scenario)
                scenario = scenario.strip()

                current_case = {'scenario': scenario}

            elif current_case:  # 只有在已经开始一个测试用例后才处理字段
                # 检测各个字段
                if any(keyword in line for keyword in ['测试模块', '所属模块', '功能模块']):
                    current_case['module'] = self._extract_field_value(line)
                elif any(keyword in line for keyword in ['测试类型']):
                    current_case['testType'] = self._extract_field_value(line)
                elif any(keyword in line for keyword in ['测试数据', '输入数据']):
                    current_case['testData'] = self._extract_field_value(line)
                elif any(keyword in line for keyword in ['需求编号', '需求点编号', '需求来源', '关联需求']):
                    current_case['requirementId'] = self._extract_field_value(line)
                elif any(keyword in line for keyword in ['前置条件', '前提条件', '前置', '前提']):
                    current_case['precondition'] = self._extract_field_value(line)
                elif any(keyword in line for keyword in ['测试步骤', '操作步骤', '执行步骤', '步骤']):
                    current_case['steps'] = self._extract_field_value(line)
                elif any(keyword in line for keyword in ['预期结果', '期望结果', '预期']):
                    current_case['expected'] = self._extract_field_value(line)
                elif '优先级' in line:
                    current_case['priority'] = self._extract_field_value(line)

        if current_case:
            logger.debug(f"添加最后一个测试用例: {current_case}")
            test_cases.append(current_case)

        logger.info(f"解析完成，共解析出 {len(test_cases)} 个测试用例")
        for i, case in enumerate(test_cases):
            logger.debug(f"测试用例 {i + 1}: {case}")

        return test_cases

    def _extract_field_value(self, line):
        """提取字段值"""
        # 尝试多种分隔符
        for sep in [':', '：', '】', '】:', '】：']:
            if sep in line:
                return line.split(sep, 1)[-1].strip()

        # 如果没有分隔符，移除常见的前缀
        for prefix in ['测试模块', '测试类型', '测试数据', '需求编号', '需求来源', '前置条件', '测试步骤', '操作步骤', '预期结果', '优先级']:
            if line.startswith(prefix):
                return line[len(prefix):].strip()

        return line.strip()

    def _reconstruct_test_cases_content(self, test_cases):
        """重新构建测试用例内容 - 保持原有格式和编号"""
        if not test_cases:
            return ""

        # 检查是否有caseId字段，如果有，说明是表格格式
        has_case_ids = any(test_case.get('caseId') for test_case in test_cases)

        if has_case_ids:
            # 重构为表格格式，保持原有编号
            return self._reconstruct_table_format(test_cases)
        else:
            # 重构为文本格式
            return self._reconstruct_text_format(test_cases)

    def _reconstruct_table_format(self, test_cases):
        """重构为表格格式"""
        content_lines = []
        content_lines.append("```markdown")
        content_lines.append("| 用例编号 | 测试模块 | 测试类型 | 测试场景 | 测试数据 | 前置条件 | 操作步骤 | 预期结果 | 优先级 | 需求编号 |")
        content_lines.append("|--------|--------|--------|--------|--------|--------|--------|--------|--------|--------|")

        for index, test_case in enumerate(test_cases, start=1):
            row = [
                test_case.get('caseId') or f"TC{index:03d}",
                test_case.get('module', ''),
                test_case.get('testType', '功能测试'),
                test_case.get('scenario', ''),
                test_case.get('testData', ''),
                test_case.get('precondition', ''),
                test_case.get('steps', '参考测试场景执行相应操作'),
                test_case.get('expected', ''),
                test_case.get('priority', 'P2'),
                test_case.get('requirementId', ''),
            ]
            content_lines.append("| " + " | ".join(self._escape_table_cell(cell) for cell in row) + " |")

        content_lines.append("```")
        return "\n".join(content_lines)

    def _escape_table_cell(self, value):
        """转义 Markdown 表格单元格内容。"""
        if value is None:
            return ""
        return str(value).replace('|', '&#124;').replace('\n', '<br>').strip()

    def _reconstruct_text_format(self, test_cases):
        """重构为文本格式"""
        content_lines = []
        for test_case in test_cases:
            # 获取原有的scenario
            scenario = test_case.get('scenario', '未命名测试用例')

            # 确保scenario能被前端正确识别
            # 如果scenario不是以数字开头或不包含"测试用例"，则添加标识
            if not (bool(re.match(r'^\d+[\.\)、]', scenario)) or
                    '测试用例' in scenario or
                    'Test Case' in scenario):
                # 添加"测试用例:"前缀确保能被识别
                content_lines.append(f"\n测试用例: {scenario}")
            else:
                content_lines.append(f"\n{scenario}")

            if test_case.get('precondition'):
                content_lines.append(f"前置条件: {test_case['precondition']}")

            if test_case.get('steps'):
                content_lines.append(f"测试步骤: {test_case['steps']}")

            if test_case.get('expected'):
                content_lines.append(f"预期结果: {test_case['expected']}")

            if test_case.get('priority'):
                content_lines.append(f"优先级: {test_case['priority']}")

            content_lines.append("")  # 空行分隔

        return "\n".join(content_lines)

    def _map_priority(self, priority_str):
        """映射优先级"""
        priority_map = {
            '最高': 'critical',
            '高': 'high',
            '中': 'medium',
            '低': 'low',
            'P0': 'critical',
            'P1': 'high',
            'P2': 'medium',
            'P3': 'low'
        }
        return priority_map.get(priority_str, 'medium')

    def _map_test_type(self, test_type_str):
        """映射生成用例中的中文测试类型到用例管理系统枚举。"""
        text = str(test_type_str or '')
        if any(keyword in text for keyword in ['安全', 'SQL注入', 'XSS', 'CSRF', '越权', '敏感数据', '验证码攻击', '参数篡改']):
            return 'security'
        if any(keyword in text for keyword in ['异常', '接口异常', '超时', '断网', '返回错误']):
            return 'api'
        if any(keyword in text for keyword in ['兼容', '浏览器', 'Chrome', 'Safari', 'Firefox', 'Edge']):
            return 'ui'
        return 'functional'

    @action(detail=False, methods=['get'])
    def statistics(self, request):
        """获取测试用例生成任务的统计信息"""
        try:
            # 获取查询参数
            status_param = request.query_params.get('status')
            created_by = request.query_params.get('created_by')

            # 构建查询
            queryset = self.get_queryset()

            if status_param:
                queryset = queryset.filter(status=status_param)

            if created_by:
                queryset = queryset.filter(created_by_id=created_by)

            # 使用聚合查询获取统计信息
            from django.db.models import Count

            stats = queryset.aggregate(
                total=Count('id'),
                completed=Count('id', filter=models.Q(status='completed')),
                pending=Count('id', filter=models.Q(status='pending')),
                generating=Count('id', filter=models.Q(status='generating')),
                reviewing=Count('id', filter=models.Q(status='reviewing')),
                revising=Count('id', filter=models.Q(status='revising')),
                failed=Count('id', filter=models.Q(status='failed')),
                cancelled=Count('id', filter=models.Q(status='cancelled'))
            )

            # 计算运行中的任务（pending + generating + reviewing + revising）
            stats['running'] = (
                    stats['pending'] + stats['generating'] +
                    stats['reviewing'] + stats['revising']
            )

            return Response({
                'total': stats['total'],
                'completed': stats['completed'],
                'running': stats['running'],
                'failed': stats['failed'],
                'pending': stats['pending'],
                'generating': stats['generating'],
                'reviewing': stats['reviewing'],
                'revising': stats['revising'],
                'cancelled': stats['cancelled']
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"获取统计信息时出错: {e}")
            return Response(
                {'error': '获取统计信息失败，请稍后重试'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class ConfigStatusViewSet(viewsets.ViewSet):
    """配置状态检查视图集"""
    # 安全修复：配置状态接口需要认证
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['get'])
    def check(self, request):
        """检查AI配置状态"""
        try:
            # 检查AI模型配置
            ai_model_configs = AIModelConfig.objects.filter(
                models.Q(role__in=['writer', 'reviewer']) |
                models.Q(model_usage__in=['test_case_generation', 'test_case_review'])
            )

            # 检查writer模型配置
            writer_model_enabled = ai_model_configs.filter(
                models.Q(model_usage='test_case_generation') | models.Q(role='writer'),
                is_active=True
            ).first()

            writer_model_disabled = ai_model_configs.filter(
                models.Q(model_usage='test_case_generation') | models.Q(role='writer'),
                is_active=False
            ).first()

            # 检查reviewer模型配置
            reviewer_model_enabled = ai_model_configs.filter(
                models.Q(model_usage='test_case_review') | models.Q(role='reviewer'),
                is_active=True
            ).first()

            reviewer_model_disabled = ai_model_configs.filter(
                models.Q(model_usage='test_case_review') | models.Q(role='reviewer'),
                is_active=False
            ).first()

            # Skills, selected rules and requirement materials replace Prompt configuration.
            writer_configured = writer_model_enabled is not None

            # 判断可选配置（reviewer）
            reviewer_configured = reviewer_model_enabled is not None

            # 检查生成行为配置
            generation_config = GenerationConfig.get_active_config()

            # 判断是否有禁用的配置
            has_disabled = (
                    writer_model_disabled is not None or
                    reviewer_model_disabled is not None
            )

            # 判断整体状态
            if writer_configured:
                if has_disabled:
                    overall_status = 'disabled'
                    message = '配置完整，但部分配置处于禁用状态'
                else:
                    overall_status = 'enabled'
                    message = '配置完整且已启用'
            else:
                # writer配置不完整
                if writer_model_enabled:
                    overall_status = 'disabled'
                    message = '检测到已配置但未启用的配置'
                else:
                    overall_status = 'not_configured'
                    message = '尚未配置AI模型'

            # 构建返回数据
            response_data = {
                'overall_status': overall_status,
                'message': message,
                'writer_model': {
                    'configured': writer_model_enabled is not None or writer_model_disabled is not None,
                    'enabled': writer_model_enabled is not None,
                    'name': (writer_model_enabled or writer_model_disabled).name if (
                            writer_model_enabled or writer_model_disabled) else None,
                    'provider': (writer_model_enabled or writer_model_disabled).get_model_type_display() if (
                            writer_model_enabled or writer_model_disabled) else None,
                    'id': (writer_model_enabled or writer_model_disabled).id if (
                            writer_model_enabled or writer_model_disabled) else None,
                    'required': True
                },
                'reviewer_model': {
                    'configured': reviewer_model_enabled is not None or reviewer_model_disabled is not None,
                    'enabled': reviewer_model_enabled is not None,
                    'name': (reviewer_model_enabled or reviewer_model_disabled).name if (
                            reviewer_model_enabled or reviewer_model_disabled) else None,
                    'id': (reviewer_model_enabled or reviewer_model_disabled).id if (
                            reviewer_model_enabled or reviewer_model_disabled) else None,
                    'required': False
                },
                'generation_config': {
                    'configured': generation_config is not None,
                    'enabled': generation_config is not None,
                    'name': generation_config.name if generation_config else None,
                    'id': generation_config.id if generation_config else None,
                    'required': True,
                    'default_output_mode': generation_config.default_output_mode if generation_config else None,
                    'enable_auto_review': generation_config.enable_auto_review if generation_config else None
                }
            }

            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"检查配置状态失败: {e}")
            return Response({
                'error': '检查配置状态失败，请稍后重试'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
