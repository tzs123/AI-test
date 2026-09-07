from __future__ import annotations

from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient

from apps.agent.models import E2EExecutionLog, E2ETask


pytestmark = pytest.mark.django_db


@pytest.fixture()
def report_user():
    return get_user_model().objects.create_user(username='e2e-report-user', password='test-password')


@pytest.fixture()
def report_client(report_user):
    client = APIClient()
    client.force_authenticate(report_user)
    return client


def create_finished_task(client: APIClient) -> E2ETask:
    response = client.post('/api/e2e/create', {
        'description': '测试登录流程',
        'target_url': 'https://example.test/login',
    }, format='json')
    assert response.status_code == 201
    task = E2ETask.objects.get(pk=response.data['task_id'])
    task.status = E2ETask.STATUS_FAILED
    task.execution_attempt = 1
    task.result = {'summary': {'executed': 1, 'passed': 0, 'failed': 1}, 'duration_ms': 125}
    task.analysis = {
        'title': '登录失败',
        'severity': 'P1',
        'root_cause': '登录接口返回异常',
        'reproduction_steps': ['1. 打开登录页'],
        'fix_suggestion': '检查认证服务',
    }
    task.save()
    return task


def response_body(response) -> bytes:
    return b''.join(response.streaming_content)


def test_report_is_escaped_authenticated_and_contains_execution_summary(
    report_client, report_user, tmp_path,
):
    with override_settings(MEDIA_ROOT=tmp_path):
        task = create_finished_task(report_client)
        task.task_name = '<script>alert("task")</script>'
        task.description = '<img src=x onerror=alert(1)>'
        task.error_message = '<script>alert("error")</script>'
        task.save()

        screenshot = tmp_path / 'e2e' / str(task.id) / task.trace_id / 'screenshots' / '001.png'
        screenshot.parent.mkdir(parents=True)
        screenshot.write_bytes(b'\x89PNG\r\n\x1a\nreport-evidence')
        E2EExecutionLog.objects.create(
            task=task,
            attempt=1,
            step=1,
            step_name='提交登录',
            action='click',
            status='FAILED',
            screenshot=f'/media/{screenshot.relative_to(tmp_path).as_posix()}',
            error='<svg onload=alert(1)>',
            duration_ms=125,
        )

        response = report_client.get(f'/api/e2e/report/{task.id}')
        assert response.status_code == 200
        assert response['Content-Type'] == 'text/html; charset=utf-8'
        assert response['X-Content-Type-Options'] == 'nosniff'
        assert "default-src 'none'" in response['Content-Security-Policy']
        assert f'e2e-task-{task.id}.html' in response['Content-Disposition']
        html = response_body(response).decode('utf-8')

    assert '<script>alert' not in html
    assert '&lt;script&gt;alert' in html
    assert '&lt;svg onload=alert(1)&gt;' in html
    assert '登录失败' in html
    assert '125 ms' in html
    assert 'data:image/png;base64,' in html
    task.refresh_from_db()
    assert task.report_url == f'/api/e2e/report/{task.id}'

    other = get_user_model().objects.create_user(username='e2e-report-other', password='x')
    report_client.force_authenticate(other)
    hidden = report_client.get(f'/api/e2e/report/{task.id}')
    assert hidden.status_code == 404


def test_evidence_download_enforces_task_access_and_artifact_root(
    report_client, tmp_path,
):
    with override_settings(MEDIA_ROOT=tmp_path):
        task = create_finished_task(report_client)
        screenshot = tmp_path / 'e2e' / str(task.id) / task.trace_id / 'screenshots' / 'step.png'
        screenshot.parent.mkdir(parents=True)
        screenshot.write_bytes(b'\x89PNG\r\n\x1a\nprivate-evidence')
        log = E2EExecutionLog.objects.create(
            task=task,
            attempt=1,
            step=1,
            step_name='截图',
            action='screenshot',
            status='PASSED',
            screenshot=f'/media/{screenshot.relative_to(tmp_path).as_posix()}',
        )

        result = report_client.get(f'/api/e2e/result/{task.id}')
        evidence_url = result.data['execution_logs'][0]['screenshot']
        assert evidence_url == f'/api/e2e/evidence/{task.id}/{log.id}/screenshot'

        own = report_client.get(evidence_url)
        assert own.status_code == 200
        assert own['Content-Type'] == 'image/png'
        assert own['Cache-Control'] == 'private, no-store'
        assert response_body(own).endswith(b'private-evidence')

        outside = tmp_path / 'outside.png'
        outside.write_bytes(b'not-task-evidence')
        log.screenshot = str(outside)
        log.save(update_fields=['screenshot'])
        blocked = report_client.get(evidence_url)
        assert blocked.status_code == 404

        other = get_user_model().objects.create_user(username='e2e-evidence-other', password='x')
        report_client.force_authenticate(other)
        hidden = report_client.get(evidence_url)
        assert hidden.status_code == 404


def test_task_list_uses_compact_serializer(report_client):
    task = create_finished_task(report_client)
    E2EExecutionLog.objects.create(
        task=task,
        attempt=1,
        step=1,
        step_name='打开登录页',
        action='navigate',
        status='FAILED',
    )

    response = report_client.get('/api/e2e/tasks')
    assert response.status_code == 200
    row = next(item for item in response.data if item['id'] == task.id)
    assert row['task_name'] == task.task_name
    assert 'execution_logs' not in row
    assert 'steps' not in row
    assert 'result' not in row
    assert 'analysis' not in row
