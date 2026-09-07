from __future__ import annotations

import logging

from celery import current_app
from django.db import transaction
from django.http import FileResponse
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .e2e_serializers import (
    E2EAnalyzeSerializer,
    E2ECreateSerializer,
    E2EReplanSerializer,
    E2ERunSerializer,
    E2ETaskListSerializer,
    E2ETaskSerializer,
)
from .e2e_analysis import analyze_e2e_task
from .e2e_service import (
    accessible_e2e_tasks,
    create_e2e_task,
    e2e_plan_requires_refresh,
    refresh_e2e_plan,
    supplemented_description,
)
from .e2e_tasks import execute_e2e_task
from .models import E2ETask
from backend.agent.e2e.report_agent import ReportAgent
from backend.agent.e2e.evidence import evidence_content_type, resolve_evidence_path


logger = logging.getLogger(__name__)


class E2ECreateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = E2ECreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            task = create_e2e_task(user=request.user, validated_data=serializer.validated_data)
        except PermissionError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        return Response({
            'task_id': task.id,
            'status': task.status,
            'trace_id': task.trace_id,
            'needs_input': task.result.get('needs_input', []),
        }, status=status.HTTP_201_CREATED)


class E2ETaskLookupMixin:
    def get_task(self, request, task_id):
        return accessible_e2e_tasks(request.user).filter(pk=task_id).first()


class E2EResultAPIView(E2ETaskLookupMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, task_id):
        task = self.get_task(request, task_id)
        if task is None:
            return Response({'detail': 'E2E 任务不存在'}, status=status.HTTP_404_NOT_FOUND)
        return Response(E2ETaskSerializer(task).data)

    @transaction.atomic
    def delete(self, request, task_id):
        task = self.get_task(request, task_id)
        if task is None:
            return Response({'detail': 'E2E 任务不存在'}, status=status.HTTP_404_NOT_FOUND)
        agent_task = task.agent_task
        task.delete()
        if agent_task is not None:
            agent_task.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class E2EListAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        queryset = accessible_e2e_tasks(request.user)
        executor_type = request.query_params.get('executor_type')
        task_status = request.query_params.get('status')
        if executor_type:
            queryset = queryset.filter(executor_type=executor_type)
        if task_status:
            queryset = queryset.filter(status=task_status)
        return Response(E2ETaskListSerializer(queryset[:100], many=True).data)


class E2EReplanAPIView(E2ETaskLookupMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    replannable_statuses = {
        E2ETask.STATUS_NEEDS_INPUT,
        E2ETask.STATUS_READY,
        E2ETask.STATUS_PASSED,
        E2ETask.STATUS_FAILED,
        E2ETask.STATUS_CANCELLED,
    }
    def post(self, request, task_id):
        serializer = E2EReplanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        task = self.get_task(request, task_id)
        if task is None:
            return Response({'detail': 'E2E 任务不存在'}, status=status.HTTP_404_NOT_FOUND)
        if task.status not in self.replannable_statuses:
            return Response(
                {'detail': f'状态 {task.status} 不允许补充或重新规划'},
                status=status.HTTP_409_CONFLICT,
            )

        data = serializer.validated_data
        business_steps = str(data.get('business_steps') or '').strip()
        acceptance_criteria = str(data.get('acceptance_criteria') or '').strip()
        description = supplemented_description(
            str(data.get('description') or task.description).strip(),
            business_steps=business_steps,
            acceptance_criteria=acceptance_criteria,
        )

        update_fields = ['description', 'updated_time']
        task.description = description
        if 'target_url' in data:
            task.target_url = data['target_url']
            update_fields.append('target_url')
        if 'mobile_config' in data:
            task.mobile_config = {**(task.mobile_config or {}), **data['mobile_config']}
            update_fields.append('mobile_config')
        task.save(update_fields=update_fields)

        if task.agent_task_id:
            context = {**(task.agent_task.context or {})}
            context['target_url'] = task.target_url
            task.agent_task.user_requirement = description
            task.agent_task.context = context
            task.agent_task.save(update_fields=['user_requirement', 'context', 'updated_at'])

        refresh_e2e_plan(task)
        task.refresh_from_db()
        return Response({
            'task': E2ETaskSerializer(task).data,
            'needs_input': (task.result or {}).get('needs_input', []),
        })


class E2EReportAPIView(E2ETaskLookupMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, task_id):
        task = self.get_task(request, task_id)
        if task is None:
            return Response({'detail': 'E2E 任务不存在'}, status=status.HTTP_404_NOT_FOUND)
        report_agent = ReportAgent()
        path = report_agent.generate(task)
        report_url = f'/api/e2e/report/{task.id}'
        if task.report_url != report_url:
            E2ETask.objects.filter(pk=task.pk).update(report_url=report_url)
        response = FileResponse(
            open(path, 'rb'),
            as_attachment=True,
            filename=f'e2e-task-{task.id}.html',
            content_type='text/html; charset=utf-8',
        )
        response['Content-Security-Policy'] = (
            "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        response['X-Content-Type-Options'] = 'nosniff'
        return response


class E2EEvidenceAPIView(E2ETaskLookupMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, task_id, log_id, kind):
        task = self.get_task(request, task_id)
        if task is None:
            return Response({'detail': 'E2E 任务不存在'}, status=status.HTTP_404_NOT_FOUND)
        execution_log = task.execution_logs.filter(pk=log_id).first()
        if execution_log is None:
            return Response({'detail': 'E2E 证据不存在'}, status=status.HTTP_404_NOT_FOUND)
        stored_value = execution_log.screenshot if kind == 'screenshot' else execution_log.video
        path = resolve_evidence_path(task.id, stored_value)
        if path is None:
            return Response({'detail': 'E2E 证据不存在'}, status=status.HTTP_404_NOT_FOUND)
        response = FileResponse(
            open(path, 'rb'),
            as_attachment=kind == 'video',
            filename=path.name,
            content_type=evidence_content_type(path),
        )
        response['X-Content-Type-Options'] = 'nosniff'
        response['Cache-Control'] = 'private, no-store'
        return response


class E2ERunAPIView(E2ETaskLookupMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, task_id):
        serializer = E2ERunSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        visible = self.get_task(request, task_id)
        if visible is None:
            return Response({'detail': 'E2E 任务不存在'}, status=status.HTTP_404_NOT_FOUND)

        visible_needs_input = (visible.result or {}).get('needs_input', [])
        if e2e_plan_requires_refresh(visible) or (
            visible.status == E2ETask.STATUS_NEEDS_INPUT
            and visible_needs_input == ['business_steps']
        ):
            refresh_e2e_plan(visible)

        with transaction.atomic():
            task = E2ETask.objects.select_for_update().get(pk=visible.pk)
            if task.status == E2ETask.STATUS_NEEDS_INPUT:
                return Response({
                    'detail': '执行条件不完整',
                    'needs_input': (task.result or {}).get('needs_input', []),
                }, status=status.HTTP_409_CONFLICT)
            if task.status in {E2ETask.STATUS_QUEUED, E2ETask.STATUS_RUNNING}:
                return Response({
                    'task_id': task.id,
                    'status': task.status,
                    'celery_task_id': task.celery_task_id,
                    'duplicate': True,
                }, status=status.HTTP_202_ACCEPTED)
            terminal = {E2ETask.STATUS_PASSED, E2ETask.STATUS_FAILED, E2ETask.STATUS_CANCELLED}
            if task.status in terminal and not serializer.validated_data['force']:
                return Response({'detail': '任务已结束，重跑需要 force=true'}, status=status.HTTP_409_CONFLICT)
            if task.status not in {E2ETask.STATUS_READY, *terminal}:
                return Response({'detail': f'状态 {task.status} 不允许执行'}, status=status.HTTP_409_CONFLICT)
            previous_status = task.status
            task.status = E2ETask.STATUS_QUEUED
            task.finished_at = None
            task.error_message = ''
            task.save(update_fields=['status', 'finished_at', 'error_message', 'updated_time'])

        try:
            celery_result = execute_e2e_task.delay(
                task.id,
                headless=serializer.validated_data['headless'],
                record_video=serializer.validated_data['record_video'],
            )
        except Exception as exc:
            E2ETask.objects.filter(pk=task.pk, status=E2ETask.STATUS_QUEUED).update(
                status=previous_status,
                error_message=str(exc)[:20000],
            )
            return Response({'detail': 'E2E 执行任务入队失败'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        E2ETask.objects.filter(pk=task.pk).update(celery_task_id=celery_result.id)
        return Response({
            'task_id': task.id,
            'status': E2ETask.STATUS_QUEUED,
            'celery_task_id': celery_result.id,
            'duplicate': False,
        }, status=status.HTTP_202_ACCEPTED)


class E2EStopAPIView(E2ETaskLookupMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    stoppable_statuses = {
        E2ETask.STATUS_CREATED,
        E2ETask.STATUS_PLANNING,
        E2ETask.STATUS_READY,
        E2ETask.STATUS_QUEUED,
        E2ETask.STATUS_RUNNING,
        E2ETask.STATUS_ANALYZING,
        E2ETask.STATUS_REPORTING,
    }

    def post(self, request, task_id):
        visible = self.get_task(request, task_id)
        if visible is None:
            return Response({'detail': 'E2E 任务不存在'}, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic():
            task = E2ETask.objects.select_for_update().get(pk=visible.pk)
            if task.status == E2ETask.STATUS_CANCELLED:
                return Response({
                    'task_id': task.id,
                    'status': task.status,
                    'duplicate': True,
                    'task': E2ETaskSerializer(task).data,
                })
            if task.status not in self.stoppable_statuses:
                return Response(
                    {'detail': f'状态 {task.status} 不允许停止'},
                    status=status.HTTP_409_CONFLICT,
                )

            previous_status = task.status
            celery_task_id = task.celery_task_id
            task.status = E2ETask.STATUS_CANCELLED
            task.finished_at = timezone.now()
            task.result = {
                **(task.result if isinstance(task.result, dict) else {}),
                'stopped': True,
                'stopped_from_status': previous_status,
            }
            task.error_message = ''
            task.save(update_fields=[
                'status', 'finished_at', 'result', 'error_message', 'updated_time',
            ])

        if celery_task_id:
            terminate = previous_status in {
                E2ETask.STATUS_RUNNING,
                E2ETask.STATUS_ANALYZING,
                E2ETask.STATUS_REPORTING,
            }
            try:
                revoke_options = {'terminate': terminate}
                if terminate:
                    revoke_options['signal'] = 'SIGTERM'
                current_app.control.revoke(celery_task_id, **revoke_options)
            except Exception:
                logger.warning('撤销 E2E Celery 任务失败: %s', celery_task_id, exc_info=True)

        return Response({
            'task_id': task.id,
            'status': task.status,
            'previous_status': previous_status,
            'duplicate': False,
            'task': E2ETaskSerializer(task).data,
        })


class E2EAnalyzeAPIView(E2ETaskLookupMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, task_id):
        serializer = E2EAnalyzeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        task = self.get_task(request, task_id)
        if task is None:
            return Response({'detail': 'E2E 任务不存在'}, status=status.HTTP_404_NOT_FOUND)
        if task.status in {
            E2ETask.STATUS_CREATED,
            E2ETask.STATUS_PLANNING,
            E2ETask.STATUS_NEEDS_INPUT,
            E2ETask.STATUS_READY,
            E2ETask.STATUS_QUEUED,
            E2ETask.STATUS_RUNNING,
        }:
            return Response({'detail': '任务尚无可分析的执行结果'}, status=status.HTTP_409_CONFLICT)
        analyzed = analyze_e2e_task(task.id)
        ReportAgent().generate(analyzed)
        if not analyzed.report_url:
            analyzed.report_url = f'/api/e2e/report/{analyzed.id}'
            analyzed.save(update_fields=['report_url', 'updated_time'])
        return Response({
            'task_id': analyzed.id,
            'status': analyzed.status,
            'analysis': analyzed.analysis,
        })
