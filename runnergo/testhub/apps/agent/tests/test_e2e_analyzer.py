from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.agent.e2e_analysis import analyze_e2e_task
from apps.agent.models import E2EExecutionLog, E2ETask
from backend.agent.e2e.analyzer_agent import AnalyzerAgent


class _DisabledLLM:
    enabled = False


def test_analyzer_agent_outputs_p0_p3_bug_contract():
    analyzer = AnalyzerAgent(llm=_DisabledLLM())
    report = analyzer.analyze(
        task_name='商城支付流程',
        steps=[
            {'name': '登录', 'status': 'PASSED'},
            {'name': '支付', 'status': 'FAILED'},
        ],
        network_logs=[{'status': 500, 'url': 'https://shop.example.test/pay'}],
        error='支付失败：500 Internal Server Error',
    )
    assert set(report) >= {
        'title', 'severity', 'reason', 'root_cause', 'reproduction_steps',
        'fix_suggestion', 'error_type', 'evidence_summary',
    }
    assert report['severity'] == 'P1'
    assert report['reproduction_steps'] == ['1. 登录', '2. 支付']
    assert report['evidence_summary']['http_error_count'] == 1


@pytest.mark.django_db
def test_e2e_analysis_persists_bug_report():
    user = get_user_model().objects.create_user(username='analyzer-user', password='x')
    task = E2ETask.objects.create(
        created_by=user,
        task_name='移动支付',
        description='测试移动支付',
        executor_type='android',
        status=E2ETask.STATUS_FAILED,
        trace_id='trace-analyzer-1',
        execution_attempt=1,
        result={'status': 'failed', 'error': 'app crashed'},
    )
    E2EExecutionLog.objects.create(
        task=task,
        attempt=1,
        step=1,
        step_name='提交支付',
        action='tap',
        status='FAILED',
        error='app crashed',
        device_log='FATAL EXCEPTION main',
    )
    analyzed = analyze_e2e_task(task.id)
    again = analyze_e2e_task(task.id)

    assert analyzed.status == E2ETask.STATUS_FAILED
    assert analyzed.analysis['severity'] == 'P1'
    assert again.analysis['severity'] == 'P1'


@pytest.mark.django_db
def test_analyze_api_rejects_unexecuted_task_and_returns_bug_report():
    user = get_user_model().objects.create_user(username='analyzer-api-user', password='x')
    client = APIClient()
    client.force_authenticate(user)
    create = client.post('/api/e2e/create', {
        'description': '测试登录',
        'target_url': 'https://example.test',
    }, format='json')
    task_id = create.data['task_id']
    assert client.post(f'/api/e2e/analyze/{task_id}', {}, format='json').status_code == 409

    task = E2ETask.objects.get(pk=task_id)
    task.status = E2ETask.STATUS_FAILED
    task.execution_attempt = 1
    task.result = {'status': 'failed', 'error': 'TimeoutError'}
    task.save(update_fields=['status', 'execution_attempt', 'result', 'updated_time'])
    E2EExecutionLog.objects.create(
        task=task,
        attempt=1,
        step=1,
        step_name='等待登录结果',
        action='assert_text',
        status='FAILED',
        error='TimeoutError',
    )

    response = client.post(f'/api/e2e/analyze/{task_id}', {}, format='json')
    assert response.status_code == 200
    assert response.data['status'] == E2ETask.STATUS_FAILED
    assert response.data['analysis']['severity'] == 'P2'
    assert 'error_event_id' not in response.data
