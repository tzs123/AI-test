from rest_framework import permissions, viewsets, status
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from django.utils import timezone
from django.db import transaction
from django.db.models import Q
from .models import TestPlan, TestRun, TestRunCase, TestRunCaseHistory
from apps.testcases.models import TestCase
from apps.projects.models import Project
from .serializers import (TestPlanSerializer, TestRunSerializer, TestRunCaseSerializer, 
                         TestPlanDetailSerializer, TestRunCaseDetailSerializer, 
                         TestRunCaseHistorySerializer)
from apps.performance_rule.serializers import PerformanceResultSerializer
from apps.performance_rule.services import generate_performance_result


def accessible_projects(user):
    if user.is_superuser:
        return Project.objects.all()
    return Project.objects.filter(Q(owner=user) | Q(members=user)).distinct()


def validate_project_ids(user, project_ids):
    """Reject cross-project plan mutations instead of silently dropping them."""
    normalized = []
    for value in project_ids or []:
        try:
            project_id = int(value)
        except (TypeError, ValueError):
            raise ValidationError({'projects': '项目 ID 必须是整数'})
        if project_id not in normalized:
            normalized.append(project_id)

    allowed = set(accessible_projects(user).filter(id__in=normalized).values_list('id', flat=True))
    missing = [project_id for project_id in normalized if project_id not in allowed]
    if missing:
        raise ValidationError({'projects': f'无权访问项目: {missing}'})
    return normalized

class TestPlanViewSet(viewsets.ModelViewSet):
    """
    测试计划视图集
    """
    queryset = TestPlan.objects.all().order_by('-created_at')
    serializer_class = TestPlanSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.request.user.is_superuser:
            return queryset
        return queryset.filter(
            Q(creator=self.request.user)
            | Q(projects__in=accessible_projects(self.request.user))
        ).distinct()

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return TestPlanDetailSerializer
        return TestPlanSerializer

    def _sync_test_runs(self, test_plan, project_ids, testcase_ids):
        project_ids = validate_project_ids(self.request.user, project_ids)
        if not project_ids:
            test_plan.projects.clear()
            test_plan.test_runs.all().delete()
            return

        valid_projects = list(accessible_projects(self.request.user).filter(id__in=project_ids))
        valid_project_ids = [project.id for project in valid_projects]
        test_plan.projects.set(valid_project_ids)

        testcase_queryset = TestCase.objects.filter(
            id__in=testcase_ids,
            project_id__in=valid_project_ids
        ).select_related('project')
        testcase_map = {testcase.id: testcase for testcase in testcase_queryset}

        existing_runs = {test_run.project_id: test_run for test_run in test_plan.test_runs.all()}

        for project_id, test_run in list(existing_runs.items()):
            if project_id not in valid_project_ids:
                test_run.delete()
                existing_runs.pop(project_id, None)

        for project in valid_projects:
            test_run = existing_runs.get(project.id)
            if not test_run:
                test_run = TestRun.objects.create(
                    name=f"{test_plan.name} - {project.name} Execution",
                    test_plan=test_plan,
                    project=project,
                    version=test_plan.version,
                    creator=test_plan.creator,
                    assignee=test_plan.creator
                )
                existing_runs[project.id] = test_run
            else:
                test_run.name = f"{test_plan.name} - {project.name} Execution"
                test_run.version = test_plan.version
                test_run.save(update_fields=['name', 'version', 'updated_at'])

            run_testcases = [testcase for testcase in testcase_queryset if testcase.project_id == project.id]
            TestRunCase.objects.filter(test_run=test_run).exclude(
                testcase_id__in=[testcase.id for testcase in run_testcases]
            ).delete()

            existing_case_ids = set(
                TestRunCase.objects.filter(test_run=test_run).values_list('testcase_id', flat=True)
            )
            new_cases = [
                TestRunCase(
                    test_run=test_run,
                    testcase=testcase,
                    priority=testcase.priority
                )
                for testcase in run_testcases
                if testcase.id not in existing_case_ids
            ]
            if new_cases:
                TestRunCase.objects.bulk_create(new_cases)

            test_run.testcases.set([testcase.id for testcase in run_testcases])

    def perform_create(self, serializer):
        # 在创建TestPlan时，设置creator并自动为每个项目创建TestRun和TestRunCase
        # 获取版本信息
        version_id = self.request.data.get('version')
        version = None
        if version_id:
            from apps.versions.models import Version
            try:
                version = Version.objects.get(id=version_id)
            except Version.DoesNotExist:
                pass

        with transaction.atomic():
            test_plan = serializer.save(creator=self.request.user, version=version)

            project_ids = self.request.data.get('projects', [])
            testcase_ids = self.request.data.get('testcases', [])
            self._sync_test_runs(test_plan, project_ids, testcase_ids)

    @action(detail=False, methods=['get'])
    def testcases_by_projects(self, request):
        """
        根据项目获取测试用例
        """
        project_ids = request.query_params.getlist('project_ids')
        if not project_ids:
            return Response({
                'error': '请先选择项目',
                'detail': '请先选择项目后再选择测试用例'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            # 过滤数字字符串和空值
            project_ids = [int(pid) for pid in project_ids if pid and pid.isdigit()]
            project_ids = validate_project_ids(request.user, project_ids)
            
            if not project_ids:
                return Response({
                    'error': '无效的项目 ID',
                    'detail': '请选择有效的项目'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # 获取指定项目的测试用例
            testcases = TestCase.objects.filter(
                project_id__in=project_ids,
                status__in=['draft', 'active']  # 包含草稿和激活状态的测试用例
            ).values('id', 'title', 'priority', 'test_type', 'project__name')
            
            return Response({
                'results': list(testcases)
            })
            
        except ValueError:
            return Response({
                'error': '项目 ID 格式错误',
                'detail': '请提供有效的项目 ID'
            }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'error': '获取测试用例失败',
                'detail': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def perform_update(self, serializer):
        # 在更新TestPlan时，处理版本信息
        version_id = self.request.data.get('version')
        version = None
        if version_id:
            from apps.versions.models import Version
            try:
                version = Version.objects.get(id=version_id)
            except Version.DoesNotExist:
                pass
        
        with transaction.atomic():
            save_kwargs = {'version': version} if 'version' in self.request.data else {}
            test_plan = serializer.save(**save_kwargs)

            project_ids = self.request.data.get('projects')
            testcase_ids = self.request.data.get('testcases')
            if project_ids is None:
                project_ids = list(test_plan.projects.values_list('id', flat=True))
            if testcase_ids is None:
                testcase_ids = list(
                    TestRunCase.objects.filter(test_run__test_plan=test_plan)
                    .values_list('testcase_id', flat=True)
                    .distinct()
                )
            self._sync_test_runs(test_plan, project_ids, testcase_ids)

            assignee_ids = self.request.data.get('assignees')
            if assignee_ids is not None:
                if assignee_ids:
                    test_plan.assignees.set(assignee_ids)
                else:
                    test_plan.assignees.clear()


class TestRunViewSet(viewsets.ModelViewSet):
    """
    测试执行视图集
    """
    queryset = TestRun.objects.all().order_by('-created_at')
    serializer_class = TestRunSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = super().get_queryset().select_related('project', 'test_plan', 'creator', 'assignee')
        if self.request.user.is_superuser:
            return queryset
        return queryset.filter(project__in=accessible_projects(self.request.user))

    def _extract_performance_metrics(self, request):
        return (
            request.data.get('performance_metrics')
            or request.data.get('metrics')
            or {}
        )

    def _generate_performance_result(self, test_run, request):
        metrics = self._extract_performance_metrics(request)
        if not metrics:
            return None

        report_id = request.data.get('report_id') or str(test_run.id)
        return generate_performance_result(
            project_id=test_run.project_id,
            report_id=report_id,
            metrics=metrics,
        )

    def perform_update(self, serializer):
        test_run = serializer.save()
        if test_run.status == 'completed' and not test_run.completed_at:
            test_run.completed_at = timezone.now()
            test_run.save(update_fields=['completed_at', 'updated_at'])

        try:
            self._generate_performance_result(test_run, self.request)
        except ValueError as exc:
            raise ValidationError({'performance_rule': str(exc)})

    @action(detail=True, methods=['post'])
    def complete_with_performance(self, request, pk=None):
        """
        完成测试执行并生成 SLA 性能结果。
        """
        test_run = self.get_object()
        test_run.status = 'completed'
        test_run.completed_at = timezone.now()
        test_run.save(update_fields=['status', 'completed_at', 'updated_at'])

        try:
            performance_result = self._generate_performance_result(test_run, request)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        response_data = {'run': TestRunSerializer(test_run).data}
        if performance_result:
            response_data['performance_result'] = PerformanceResultSerializer(performance_result).data
        return Response(response_data)

class TestRunCaseViewSet(viewsets.ModelViewSet):
    """
    测试执行用例视图集
    """
    queryset = TestRunCase.objects.all()
    serializer_class = TestRunCaseSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = super().get_queryset().select_related('test_run', 'testcase', 'test_run__project')
        if self.request.user.is_superuser:
            return queryset
        return queryset.filter(test_run__project__in=accessible_projects(self.request.user))

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return TestRunCaseDetailSerializer
        return TestRunCaseSerializer

    @action(detail=True, methods=['patch'])
    def update_status(self, request, pk=None):
        """
        更新单个用例的执行状态，并自动创建历史记录
        """
        run_case = self.get_object()
        new_status = request.data.get('status')
        actual_result = request.data.get('actual_result', '')
        comments = request.data.get('comments', '')
        
        valid_statuses = {value for value, _label in TestRunCase.STATUS_CHOICES}
        if new_status not in valid_statuses:
            return Response(
                {'error': f'Status 无效，可选值: {sorted(valid_statuses)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        # 创建历史记录
        TestRunCaseHistory.objects.create(
            run_case=run_case,
            status=new_status,
            actual_result=actual_result,
            comments=comments,
            executed_by=request.user,
            executed_at=timezone.now()
        )
        
        # 更新执行用例状态
        run_case.status = new_status
        run_case.actual_result = actual_result
        run_case.comments = comments
        run_case.executed_by = request.user
        run_case.executed_at = timezone.now()
        run_case.save()
        
        return Response(TestRunCaseDetailSerializer(run_case).data)

    @action(detail=True, methods=['get'])
    def history(self, request, pk=None):
        """
        获取用例执行历史记录
        """
        run_case = self.get_object()
        history = run_case.history.all().order_by('-executed_at')
        serializer = TestRunCaseHistorySerializer(history, many=True)
        return Response(serializer.data)

class TestRunCaseHistoryViewSet(viewsets.ReadOnlyModelViewSet):
    """
    测试执行历史视图集（只读）
    """
    queryset = TestRunCaseHistory.objects.all().order_by('-executed_at')
    serializer_class = TestRunCaseHistorySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = super().get_queryset().select_related('run_case__test_run__project', 'executed_by')
        if self.request.user.is_superuser:
            return queryset
        return queryset.filter(run_case__test_run__project__in=accessible_projects(self.request.user))
