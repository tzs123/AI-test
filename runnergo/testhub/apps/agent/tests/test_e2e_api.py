from __future__ import annotations

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.agent.models import AgentMemory, AgentTask, E2ETask
from apps.projects.models import Project


pytestmark = pytest.mark.django_db


@pytest.fixture()
def api_user():
    return get_user_model().objects.create_user(username='e2e-api-user', password='test-password')


@pytest.fixture()
def api_client(api_user):
    client = APIClient()
    client.force_authenticate(api_user)
    return client


def test_create_e2e_task_persists_plan_and_returns_task_id(api_client, api_user):
    project = Project.objects.create(name='E2E Project', owner=api_user)
    response = api_client.post('/api/e2e/create', {
        'project_id': project.id,
        'description': '测试用户登录流程',
        'target_url': 'https://example.test/login',
    }, format='json')

    assert response.status_code == 201
    task = E2ETask.objects.select_related('agent_task').get(pk=response.data['task_id'])
    assert task.status == E2ETask.STATUS_READY
    assert task.project == project
    assert task.agent_task is not None
    assert task.target_url == 'https://example.test/login'
    assert task.steps[0]['action'] == 'navigate'
    assert task.steps[0]['url'] == 'https://example.test/login'
    assert task.test_data['normal'][0]['username'] == 'test001'
    assert AgentMemory.objects.filter(task=task.agent_task, memory_type='plan').exists()


def test_create_without_url_returns_needs_input(api_client):
    response = api_client.post('/api/e2e/create', {'description': '测试登录流程'}, format='json')
    assert response.status_code == 201
    assert response.data['status'] == E2ETask.STATUS_NEEDS_INPUT
    assert response.data['needs_input'] == ['target_url']

    run_response = api_client.post(f"/api/e2e/run/{response.data['task_id']}", {}, format='json')
    assert run_response.status_code == 409
    assert run_response.data['needs_input'] == ['target_url']


def test_rule_fallback_business_flow_requires_executable_steps(api_client):
    response = api_client.post('/api/e2e/create', {
        'description': '测试额度申请流程',
        'target_url': 'https://example.test/apply',
    }, format='json')

    assert response.status_code == 201
    assert response.data['status'] == E2ETask.STATUS_NEEDS_INPUT
    assert response.data['needs_input'] == ['business_steps']

    run_response = api_client.post(f"/api/e2e/run/{response.data['task_id']}", {}, format='json')
    assert run_response.status_code == 409
    assert run_response.data['needs_input'] == ['business_steps']


def test_replan_updates_same_task_with_user_execution_conditions(api_client):
    create_response = api_client.post('/api/e2e/create', {
        'description': '测试额度申请流程',
        'target_url': 'https://example.test/apply',
    }, format='json')
    task_id = create_response.data['task_id']

    response = api_client.post(f'/api/e2e/replan/{task_id}', {
        'business_steps': '点击额度申请、填写借款人信息、点击提交签约',
        'acceptance_criteria': '页面显示申请成功',
    }, format='json')

    assert response.status_code == 200
    assert response.data['task']['id'] == task_id
    assert response.data['needs_input'] == []
    task = E2ETask.objects.select_related('agent_task').get(pk=task_id)
    assert task.status == E2ETask.STATUS_READY
    assert '用户补充的执行条件' in task.description
    assert task.agent_task.user_requirement == task.description
    assert any(step['action'] == 'click' for step in task.steps)


def test_replan_isolated_between_users(api_client):
    create_response = api_client.post('/api/e2e/create', {
        'description': '测试额度申请流程',
        'target_url': 'https://example.test/apply',
    }, format='json')
    other = get_user_model().objects.create_user(username='e2e-replan-other', password='x')
    api_client.force_authenticate(other)

    response = api_client.post(
        f"/api/e2e/replan/{create_response.data['task_id']}",
        {'business_steps': '点击申请'},
        format='json',
    )

    assert response.status_code == 404


@patch('apps.agent.e2e_views.execute_e2e_task.delay')
def test_run_replans_passed_legacy_smoke_task(delay_mock, api_client):
    delay_mock.return_value.id = 'celery-e2e-replan-1'
    create_response = api_client.post('/api/e2e/create', {
        'description': '测试额度申请，借款人信息，补充借款人信息，提交签约功能',
        'target_url': 'https://example.test/apply',
    }, format='json')
    task = E2ETask.objects.select_related('agent_task').get(pk=create_response.data['task_id'])
    task.status = E2ETask.STATUS_PASSED
    task.steps = [
        {'id': '1', 'name': '打开目标页面', 'action': 'navigate', 'url': task.target_url},
        {'id': '2', 'name': '保存最终页面证据', 'action': 'screenshot'},
    ]
    task.result = {
        'plan_source': 'rules',
        'needs_input': [],
        'summary': {'total': 2, 'executed': 2, 'passed': 2, 'failed': 0},
    }
    task.report_url = '/api/e2e/report/legacy'
    task.analysis = {'reason': 'stale analysis'}
    task.save(update_fields=['status', 'steps', 'result', 'report_url', 'analysis'])
    task.agent_task.status = 'COMPLETED'
    task.agent_task.save(update_fields=['status'])

    response = api_client.post(f'/api/e2e/run/{task.id}', {}, format='json')

    assert response.status_code == 202
    assert response.data['status'] == E2ETask.STATUS_QUEUED
    task.refresh_from_db()
    assert task.status == E2ETask.STATUS_QUEUED
    assert task.result['needs_input'] == []
    assert 'summary' not in task.result
    assert task.report_url == ''
    assert task.analysis == {}
    assert any(step['action'] == 'click' for step in task.steps)
    delay_mock.assert_called_once()


def test_create_rejects_inaccessible_project(api_client):
    other = get_user_model().objects.create_user(username='e2e-other-owner', password='x')
    project = Project.objects.create(name='Private Project', owner=other)
    response = api_client.post('/api/e2e/create', {
        'project_id': project.id,
        'description': '测试登录',
        'target_url': 'https://example.test',
    }, format='json')
    assert response.status_code == 403


def test_result_isolated_between_users(api_client, api_user):
    create_response = api_client.post('/api/e2e/create', {
        'description': '测试登录',
        'target_url': 'https://example.test',
    }, format='json')
    task_id = create_response.data['task_id']
    own = api_client.get(f'/api/e2e/result/{task_id}')
    assert own.status_code == 200
    assert own.data['id'] == task_id

    other = get_user_model().objects.create_user(username='e2e-result-other', password='x')
    api_client.force_authenticate(other)
    hidden = api_client.get(f'/api/e2e/result/{task_id}')
    assert hidden.status_code == 404


def test_list_and_detail_mark_legacy_mobile_smoke_task_for_replan(api_client, api_user):
    task = E2ETask.objects.create(
        created_by=api_user,
        task_name='legacy ios smoke',
        description='1、测试【输入信息获取预估额度】 2、测试【借款人信息】',
        executor_type=E2ETask.EXECUTOR_IOS,
        status=E2ETask.STATUS_PASSED,
        trace_id='legacy-ios-smoke-list',
        steps=[
            {'id': '1', 'name': '启动 App', 'action': 'launch_app'},
            {'id': '2', 'name': '采集启动页', 'action': 'screenshot'},
        ],
    )

    list_response = api_client.get('/api/e2e/tasks', {'executor_type': 'ios'})
    detail_response = api_client.get(f'/api/e2e/result/{task.id}')

    assert list_response.status_code == 200
    listed = next(item for item in list_response.data if item['id'] == task.id)
    assert listed['requires_replan'] is True
    assert detail_response.status_code == 200
    assert detail_response.data['requires_replan'] is True


def test_delete_removes_e2e_task_and_linked_agent_task(api_client):
    create_response = api_client.post('/api/e2e/create', {
        'description': '测试登录',
        'target_url': 'https://example.test',
    }, format='json')
    task = E2ETask.objects.select_related('agent_task').get(pk=create_response.data['task_id'])
    agent_task_id = task.agent_task_id

    response = api_client.delete(f'/api/e2e/result/{task.id}')

    assert response.status_code == 204
    assert not E2ETask.objects.filter(pk=task.id).exists()
    assert not AgentTask.objects.filter(pk=agent_task_id).exists()


@patch('apps.agent.e2e_views.execute_e2e_task.delay')
def test_run_queues_once_and_duplicate_request_reuses_state(delay_mock, api_client):
    delay_mock.return_value.id = 'celery-e2e-1'
    create_response = api_client.post('/api/e2e/create', {
        'description': '测试登录',
        'target_url': 'https://example.test',
    }, format='json')
    task_id = create_response.data['task_id']

    first = api_client.post(f'/api/e2e/run/{task_id}', {}, format='json')
    second = api_client.post(f'/api/e2e/run/{task_id}', {}, format='json')

    assert first.status_code == 202
    assert first.data['duplicate'] is False
    assert second.status_code == 202
    assert second.data['duplicate'] is True
    delay_mock.assert_called_once()
    task = E2ETask.objects.get(pk=task_id)
    assert task.status == E2ETask.STATUS_QUEUED
    assert task.celery_task_id == 'celery-e2e-1'


@patch('apps.agent.e2e_views.current_app.control.revoke')
def test_stop_queued_task_is_idempotent(revoke_mock, api_client):
    create_response = api_client.post('/api/e2e/create', {
        'description': '测试登录',
        'target_url': 'https://example.test',
    }, format='json')
    task = E2ETask.objects.get(pk=create_response.data['task_id'])
    task.status = E2ETask.STATUS_QUEUED
    task.celery_task_id = 'celery-e2e-stop-queued'
    task.save(update_fields=['status', 'celery_task_id'])

    first = api_client.post(f'/api/e2e/stop/{task.id}', {}, format='json')
    second = api_client.post(f'/api/e2e/stop/{task.id}', {}, format='json')

    assert first.status_code == 200
    assert first.data['status'] == E2ETask.STATUS_CANCELLED
    assert first.data['previous_status'] == E2ETask.STATUS_QUEUED
    assert first.data['duplicate'] is False
    assert second.status_code == 200
    assert second.data['duplicate'] is True
    task.refresh_from_db()
    assert task.status == E2ETask.STATUS_CANCELLED
    assert task.finished_at is not None
    assert task.result['stopped'] is True
    assert task.result['stopped_from_status'] == E2ETask.STATUS_QUEUED
    revoke_mock.assert_called_once_with('celery-e2e-stop-queued', terminate=False)


@patch('apps.agent.e2e_views.current_app.control.revoke')
def test_stop_running_task_terminates_worker_process(revoke_mock, api_client):
    create_response = api_client.post('/api/e2e/create', {
        'description': '测试登录',
        'target_url': 'https://example.test',
    }, format='json')
    task = E2ETask.objects.get(pk=create_response.data['task_id'])
    task.status = E2ETask.STATUS_RUNNING
    task.celery_task_id = 'celery-e2e-stop-running'
    task.save(update_fields=['status', 'celery_task_id'])

    response = api_client.post(f'/api/e2e/stop/{task.id}', {}, format='json')

    assert response.status_code == 200
    revoke_mock.assert_called_once_with(
        'celery-e2e-stop-running',
        terminate=True,
        signal='SIGTERM',
    )


@patch('apps.agent.e2e_views.execute_e2e_task.delay')
def test_stopped_task_can_continue_as_new_attempt(delay_mock, api_client):
    delay_mock.return_value.id = 'celery-e2e-resume-1'
    create_response = api_client.post('/api/e2e/create', {
        'description': '测试登录',
        'target_url': 'https://example.test',
    }, format='json')
    task = E2ETask.objects.get(pk=create_response.data['task_id'])
    task.status = E2ETask.STATUS_CANCELLED
    task.finished_at = task.created_time
    task.result = {'stopped': True}
    task.save(update_fields=['status', 'finished_at', 'result'])

    response = api_client.post(
        f'/api/e2e/run/{task.id}',
        {'force': True},
        format='json',
    )

    assert response.status_code == 202
    assert response.data['status'] == E2ETask.STATUS_QUEUED
    task.refresh_from_db()
    assert task.status == E2ETask.STATUS_QUEUED
    assert task.finished_at is None
    assert task.celery_task_id == 'celery-e2e-resume-1'
    delay_mock.assert_called_once()
