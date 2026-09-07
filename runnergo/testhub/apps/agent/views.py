from django.db.models import Q
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from backend.agent.analyzer import FailureAnalyzer
from backend.agent.capability_service import AgentCapabilityService
from backend.agent.case_service import save_agent_task_as_test_case
from backend.agent.core.executor import ReActAgentExecutor
from backend.agent.core.planner import ReActPlanner
from backend.agent.planner import TestPlanner
from backend.agent.core.tool_registry import default_registry
from backend.agent.needs_input import derive_task_needs_input, needs_input_message
from backend.browser_agent.browser_controller import WebVisionAgent
from backend.data_agent.data_generator import DataAgent
from backend.security_agent.scanner import SecurityAgent

from .models import AgentKnowledgeDocument, AgentMemory, AgentStep, AgentTask
from .tasks import execute_react_agent_task
from .serializers import (
    AgentAssetGenerateSerializer,
    AgentExploreRequestSerializer,
    AgentFullCycleRequestSerializer,
    AgentKnowledgeDocumentSerializer,
    AgentKnowledgeUploadSerializer,
    AgentMemoryCreateSerializer,
    AgentMemorySerializer,
    AgentPlanRequestSerializer,
    AgentQualityScoreSerializer,
    AgentRagSearchSerializer,
    AgentSelfHealSerializer,
    AgentTaskCreateSerializer,
    AgentTaskSerializer,
    AgentTaskSupplementSerializer,
    AgentTestCaseSaveSerializer,
    FailureAnalyzeRequestSerializer,
)


class AgentTaskViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.action == 'create':
            return AgentTaskCreateSerializer
        return AgentTaskSerializer

    def get_queryset(self):
        queryset = AgentTask.objects.filter(
            Q(created_by=self.request.user) |
            Q(project__owner=self.request.user) |
            Q(project__members=self.request.user)
        ).select_related('project', 'created_by').prefetch_related(
            'steps', 'memories', 'execution_logs', 'assets', 'execution_results'
        ).distinct()
        agent_service = str(self.request.query_params.get('agent_service') or '').strip().lower()
        if agent_service == 'app':
            queryset = queryset.filter(context__agent_service='app')
        elif agent_service == 'ui':
            queryset = queryset.filter(
                Q(context__agent_service='ui') |
                Q(context__agent_service__isnull=True)
            )
        return queryset

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        auto_execute = serializer.validated_data.pop('auto_execute', True)
        task = serializer.save(created_by=request.user)
        if auto_execute:
            task.status = AgentTask.STATUS_RUNNING
            task.progress = 1
            task.current_step = 'Agent 工作流已进入执行队列'
            task.save(update_fields=['status', 'progress', 'current_step', 'updated_at'])
            execute_react_agent_task.delay(task.id)
            task.refresh_from_db()
        return Response(AgentTaskSerializer(task).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def execute(self, request, pk=None):
        task = self.get_object()
        if task.status in {
            AgentTask.STATUS_ANALYZING,
            AgentTask.STATUS_GENERATING_DATA,
            AgentTask.STATUS_GENERATING_CASE,
            AgentTask.STATUS_RUNNING,
            AgentTask.STATUS_ANALYZING_RESULT,
        }:
            return Response(AgentTaskSerializer(task).data, status=status.HTTP_202_ACCEPTED)
        task.status = AgentTask.STATUS_RUNNING
        task.progress = 1
        task.current_step = 'Agent 工作流已进入执行队列'
        task.finished_at = None
        task.error_message = ''
        task.save(update_fields=[
            'status', 'progress', 'current_step', 'finished_at',
            'error_message', 'updated_at',
        ])
        execute_react_agent_task.delay(task.id)
        task.refresh_from_db()
        return Response(AgentTaskSerializer(task).data, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=['post'])
    def supplement(self, request, pk=None):
        serializer = AgentTaskSupplementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        task = self.get_object()
        if task.status != AgentTask.STATUS_NEEDS_INPUT:
            return Response(
                {'detail': f'状态 {task.status} 不允许补充执行条件'},
                status=status.HTTP_409_CONFLICT,
            )

        data = serializer.validated_data
        supplements = []
        context = dict(task.context or {})
        target_url = str(
            data.get('target_url')
            or context.get('target_url')
            or context.get('frontend_url')
            or context.get('url')
            or ''
        ).strip()
        business_steps = str(data.get('business_steps') or context.get('business_steps') or '').strip()
        acceptance_criteria = str(
            data.get('acceptance_criteria') or context.get('acceptance_criteria') or ''
        ).strip()
        if target_url:
            supplements.append(f'目标网址：{target_url}')
        if business_steps:
            supplements.append(f'可执行业务步骤：\n{business_steps}')
        if acceptance_criteria:
            supplements.append(f'验收标准：\n{acceptance_criteria}')

        if target_url:
            context.update({'target_url': target_url, 'frontend_url': target_url})
        if business_steps:
            context['business_steps'] = business_steps
        if acceptance_criteria:
            context['acceptance_criteria'] = acceptance_criteria
        base_requirement = str(task.user_requirement or '').split('--- 用户补充的执行条件 ---', 1)[0].rstrip()
        if supplements:
            task.user_requirement = (
                f'{base_requirement}\n\n--- 用户补充的执行条件 ---\n'
                + '\n'.join(supplements)
            )
        task.context = context
        remaining = derive_task_needs_input(task)
        blocking_keys = {'executable_asset', 'execution_service'}
        if blocking_keys.intersection(remaining):
            context['needs_input'] = remaining
            task.context = context
            task.status = AgentTask.STATUS_NEEDS_INPUT
            task.progress = 100
            task.current_step = needs_input_message(remaining)
            task.finished_at = timezone.now()
            task.error_message = ''
            task.save(update_fields=[
                'user_requirement', 'context', 'status', 'progress', 'current_step',
                'finished_at', 'error_message', 'updated_at',
            ])
            return Response({
                'task': AgentTaskSerializer(task).data,
                'needs_input': remaining,
            })

        context.pop('needs_input', None)
        task.context = context
        task.status = AgentTask.STATUS_CREATED
        task.progress = 0
        task.current_step = '已补充执行条件，等待 Agent 执行'
        task.finished_at = None
        task.error_message = ''
        task.save(update_fields=[
            'user_requirement', 'context', 'status', 'progress', 'current_step',
            'finished_at', 'error_message', 'updated_at',
        ])

        if data.get('auto_execute', True):
            task.status = AgentTask.STATUS_RUNNING
            task.progress = 1
            task.current_step = 'Agent 工作流已重新进入执行队列'
            task.save(update_fields=['status', 'progress', 'current_step', 'updated_at'])
            try:
                execute_react_agent_task.delay(task.id)
            except Exception as exc:
                task.status = AgentTask.STATUS_CREATED
                task.progress = 0
                task.current_step = '已补充执行条件，等待 Agent 执行'
                task.error_message = str(exc)[:20000]
                task.save(update_fields=['status', 'progress', 'current_step', 'error_message', 'updated_at'])
                return Response(
                    {'detail': 'Agent 执行任务入队失败'},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            task.refresh_from_db()

        return Response({
            'task': AgentTaskSerializer(task).data,
            'needs_input': [],
        })

    @action(detail=True, methods=['post'])
    def stop(self, request, pk=None):
        task = self.get_object()
        active_statuses = {
            AgentTask.STATUS_CREATED,
            AgentTask.STATUS_ANALYZING,
            AgentTask.STATUS_GENERATING_DATA,
            AgentTask.STATUS_GENERATING_CASE,
            AgentTask.STATUS_RUNNING,
            AgentTask.STATUS_ANALYZING_RESULT,
        }
        if task.status not in active_statuses:
            return Response(
                {'success': False, 'message': '只能停止等待中或运行中的任务'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        now = timezone.now()
        task.status = AgentTask.STATUS_STOPPED
        task.current_step = '用户已停止 Agent 任务'
        task.finished_at = now
        task.save(update_fields=['status', 'current_step', 'finished_at', 'updated_at'])
        task.steps.filter(
            status__in=[AgentStep.STATUS_PENDING, AgentStep.STATUS_RUNNING]
        ).update(
            status=AgentStep.STATUS_SKIPPED,
            error_message='用户已停止 Agent 任务',
            finished_at=now,
            updated_at=now,
        )
        AgentMemory.objects.create(
            task=task,
            project=task.project,
            memory_type='event',
            content='用户已停止 Agent 任务',
            payload={'status': AgentTask.STATUS_STOPPED},
        )
        task.refresh_from_db()
        return Response({'success': True, 'task': AgentTaskSerializer(task).data})

    @action(detail=True, methods=['get'])
    def timeline(self, request, pk=None):
        task = self.get_object()
        data = AgentTaskSerializer(task).data
        return Response({
            'task': data,
            'timeline': [
                {
                    'id': step['id'],
                    'step': step['step'],
                    'type': step['type'],
                    'action': step['action'],
                    'status': step['status'],
                    'message': step['error_message'] or step['output_payload'].get('message', ''),
                    'started_at': step['started_at'],
                    'finished_at': step['finished_at'],
                }
                for step in data['steps']
            ],
            'brain': data.get('execution_logs', []),
        })

    @action(detail=True, methods=['post'], url_path='save-test-case')
    def save_test_case(self, request, pk=None):
        serializer = AgentTestCaseSaveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        task = self.get_object()
        if task.status not in {AgentTask.STATUS_COMPLETED, AgentTask.STATUS_FAILED}:
            return Response(
                {'detail': 'Agent 执行结束后才能保存测试用例'},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            saved = save_agent_task_as_test_case(
                task,
                user=request.user,
                name=serializer.validated_data.get('name', ''),
                description=serializer.validated_data.get('description', ''),
            )
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        task.refresh_from_db()
        return Response({
            'message': saved['message'],
            'created': saved['created'],
            'test_case': saved['test_case'],
            'task': AgentTaskSerializer(task).data,
        }, status=status.HTTP_201_CREATED if saved['created'] else status.HTTP_200_OK)


class AgentPlanAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = AgentPlanRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        planner = ReActPlanner()
        return Response(planner.plan(serializer.validated_data['requirement']))


class AgentToolsAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(default_registry.list())


class AgentFullCycleAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = AgentFullCycleRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        result = AgentCapabilityService(request.user).run_full_cycle(
            requirement=data['requirement'],
            agent_service=data.get('agent_service', 'ui'),
            task_name=data.get('task_name', ''),
            project_id=data.get('project'),
            frontend_url=data.get('frontend_url', ''),
            api_url=data.get('api_url', ''),
            api_doc=data.get('api_doc') or {},
            api_test_objects=data.get('api_test_objects') or [],
            test_object_refs=data.get('test_object_refs') or [],
            ui_case_ids=data.get('ui_case_ids') or [],
            ui_test_case_ids=data.get('ui_test_case_ids') or [],
            ui_case_names=data.get('ui_case_names') or [],
            ui_test_case_names=data.get('ui_test_case_names') or [],
            ui_case_files=data.get('ui_case_files') or [],
            case_files=data.get('case_files') or [],
            ui_project_id=data.get('ui_project_id', ''),
            app_project=data.get('app_project'),
            app_project_id=data.get('app_project_id'),
            device=data.get('device', ''),
            device_id=data.get('device_id', ''),
            app_package=data.get('app_package'),
            app_package_id=data.get('app_package_id'),
            app_test_object_refs=data.get('app_test_object_refs') or [],
            source_entry=data.get('source_entry', ''),
            reference_module=data.get('reference_module', ''),
            app_case_ids=data.get('app_case_ids') or [],
            app_test_case_ids=data.get('app_test_case_ids') or [],
            test_data_count=data.get('test_data_count', 3),
            auto_execute=False,
        )
        task = result.pop('task')
        if data.get('auto_execute', True):
            task.status = AgentTask.STATUS_RUNNING
            task.progress = 1
            task.current_step = 'Agent 工作流已进入执行队列'
            task.save(update_fields=['status', 'progress', 'current_step', 'updated_at'])
            execute_react_agent_task.delay(task.id)
            task.refresh_from_db()
            result['status'] = 'running'
            result['message'] = 'AI Test Agent 全能力闭环任务已进入执行队列。'
        return Response({
            **result,
            'task': AgentTaskSerializer(task).data,
        }, status=status.HTTP_201_CREATED)


class AgentFailureAnalyzeAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = FailureAnalyzeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = FailureAnalyzer().analyze(**serializer.validated_data)
        if result.get('old_locator') or result.get('new_locator'):
            result.setdefault('locator_repair', {
                'old_locator': result.get('old_locator', ''),
                'new_locator': result.get('new_locator', ''),
                'suggestion': result.get('suggestion', ''),
            })
        return Response(result)


class AgentExplorerAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = AgentExploreRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = AgentCapabilityService(request.user).explore_web_system(
            url=serializer.validated_data['url'],
            goal=serializer.validated_data.get('goal', ''),
            inputs=serializer.validated_data.get('inputs') or {},
            project_id=serializer.validated_data.get('project'),
            save_memory=serializer.validated_data.get('save_memory', True),
        )
        http_status = status.HTTP_200_OK if result.get('status') != 'failed' else status.HTTP_400_BAD_REQUEST
        return Response(result, status=http_status)


class AgentMemoryAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        project = request.query_params.get('project')
        result = AgentCapabilityService(request.user).list_memories(
            project_id=int(project) if project else None,
            memory_type=request.query_params.get('memory_type', ''),
            query=request.query_params.get('query', ''),
        )
        return Response({'status': 'success', 'results': result})

    def post(self, request):
        serializer = AgentMemoryCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        memory = AgentCapabilityService(request.user).create_memory(serializer.validated_data)
        return Response(AgentMemorySerializer(memory).data, status=status.HTTP_201_CREATED)


class AgentKnowledgeAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        queryset = AgentKnowledgeDocument.objects.filter(
            Q(uploaded_by=request.user) |
            Q(project__owner=request.user) |
            Q(project__members=request.user)
        ).prefetch_related('chunks').distinct()
        project = request.query_params.get('project')
        if project:
            queryset = queryset.filter(project_id=project)
        return Response(AgentKnowledgeDocumentSerializer(queryset[:50], many=True).data)

    def post(self, request):
        serializer = AgentKnowledgeUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        document = AgentCapabilityService(request.user).upload_knowledge(
            title=serializer.validated_data['title'],
            document_type=serializer.validated_data.get('document_type', 'other'),
            project_id=serializer.validated_data.get('project'),
            content=serializer.validated_data['content'],
            metadata=serializer.validated_data.get('metadata') or {},
        )
        return Response(AgentKnowledgeDocumentSerializer(document).data, status=status.HTTP_201_CREATED)


class AgentRagSearchAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = AgentRagSearchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = AgentCapabilityService(request.user).rag_search(
            query=serializer.validated_data['query'],
            project_id=serializer.validated_data.get('project'),
            limit=serializer.validated_data.get('limit', 5),
        )
        return Response(result)


class AgentAssetGenerateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = AgentAssetGenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = AgentCapabilityService(request.user).generate_assets(
            requirement=serializer.validated_data['requirement'],
            project_id=serializer.validated_data.get('project'),
            base_url=serializer.validated_data.get('base_url', ''),
            api_doc=serializer.validated_data.get('api_doc') or {},
            schedule=serializer.validated_data.get('schedule', 'daily'),
            save_assets=serializer.validated_data.get('save_assets', True),
        )
        return Response(result)


class AgentSelfHealAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = AgentSelfHealSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(AgentCapabilityService(request.user).self_heal(**serializer.validated_data))


class AgentQualityScoreAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        serializer = AgentQualityScoreSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        project = serializer.validated_data.get('project')
        return Response(AgentCapabilityService(request.user).quality_score(
            project_id=project,
            project_name=serializer.validated_data.get('project_name', ''),
        ))


class DataAgentGenerateFromAPIAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        result = DataAgent().generate_from_api(
            swagger_url=request.data.get('swagger_url', ''),
            swagger_doc=request.data.get('swagger_doc') or request.data.get('api_doc'),
            api=request.data.get('api', ''),
            mode=request.data.get('mode') or ['normal', 'boundary', 'security'],
            count=request.data.get('count', 1000),
        )
        return Response(result)


class BrowserAgentRunAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        result = WebVisionAgent().run(
            url=request.data.get('url', ''),
            goal=request.data.get('goal', ''),
            inputs=request.data.get('inputs') or {},
        )
        http_status = status.HTTP_200_OK if result.get('status') != 'failed' else status.HTTP_400_BAD_REQUEST
        return Response(result, status=http_status)


class SecurityAgentScanAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        result = SecurityAgent().scan(
            api_doc=request.data.get('api_doc', ''),
            target=request.data.get('target', ''),
            parameters=request.data.get('parameters') or None,
            headers=request.data.get('headers') or {},
            timeout=request.data.get('timeout', 8),
        )
        http_status = status.HTTP_200_OK if result.get('status') != 'failed' else status.HTTP_400_BAD_REQUEST
        return Response(result, status=http_status)
