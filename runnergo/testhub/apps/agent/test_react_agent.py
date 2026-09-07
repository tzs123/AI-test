from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.agent.models import (
    AgentKnowledgeDocument,
    AgentMemory,
    AgentStep,
    AgentTask,
    TestAsset,
    TestExecutionResult,
)
from apps.agent.serializers import AgentFullCycleRequestSerializer, AgentTaskSerializer
from apps.app_automation.models import AppDevice, AppPackage, AppProject, AppTestCase
from apps.core.models import TestDataAsset, TestDataAssetRequirement
from apps.projects.models import Project
from apps.testcases.models import TestCase
from backend.agent import tools as agent_tools
from backend.agent.capability_service import AgentCapabilityService
from backend.agent.core import executor as core_executor
from backend.agent.core.planner import ReActPlanner
from backend.agent.core.tool_registry import Tool, ToolRegistry
from backend.agent.core.tool_registry import default_registry
from backend.data_agent.data_generator import DataAgent
from backend.security_agent.scanner import SecurityAgent


def test_full_cycle_serializer_rejects_removed_app_agent_mode():
    serializer = AgentFullCycleRequestSerializer(data={
        'agent_service': 'app',
        'requirement': '执行 APP 自动化测试',
        'project': None,
        'app_project': 2,
        'app_project_id': 2,
        'app_package': None,
        'app_package_id': None,
        'device_id': '14',
    })

    assert not serializer.is_valid()
    assert 'agent_service' in serializer.errors


@pytest.mark.django_db
def test_agent_task_serializer_rewrites_legacy_pending_binding_summary():
    user = get_user_model().objects.create_user(
        username='legacy_agent_summary_user',
        password='secret',
    )
    task = AgentTask.objects.create(
        task_name='历史任务',
        user_requirement='测试历史结果展示',
        current_step='已生成报告，5 个真实执行步骤待绑定资产',
        created_by=user,
    )

    assert AgentTaskSerializer(task).data['current_step'] == (
        '已生成测试报告，5 个步骤未执行，原因见执行明细'
    )


@pytest.mark.django_db
def test_agent_task_serializer_reports_executable_asset_without_reasking_saved_steps():
    user = get_user_model().objects.create_user(
        username='legacy_agent_supplement_user',
        password='secret',
    )
    task = AgentTask.objects.create(
        task_name='历史自然语言 UI 任务',
        user_requirement=(
            '测试商城申请流程\n\n--- 用户补充的执行条件 ---\n'
            '目标网址：http://example.test/apply\n'
            '可执行业务步骤：\n1. 输入手机号\n2. 点击提交\n'
            '验收标准：\n页面显示申请成功'
        ),
        status=AgentTask.STATUS_NEEDS_INPUT,
        context={'frontend_url': 'http://example.test/apply'},
        created_by=user,
    )
    task.steps.create(
        step=1,
        step_type='ui',
        action='执行 UI',
        tool_name='run_web_test',
        status=AgentStep.STATUS_SKIPPED,
        output_payload={'message': '页面探索只能证明 URL 可访问，不能证明业务功能通过。'},
    )

    serialized = AgentTaskSerializer(task).data

    assert serialized['needs_input'] == ['executable_asset']
    assert serialized['current_step'] == '缺少可执行测试资产，请选择已有 UI 自动化 YAML 用例后执行'
    assert serialized['business_steps'] == '1. 输入手机号\n2. 点击提交'
    assert serialized['acceptance_criteria'] == '页面显示申请成功'
    assert '选择已有 UI 自动化 YAML 用例' in serialized['next_action']


@pytest.mark.django_db
def test_agent_task_serializer_only_returns_current_attempt_results():
    user = get_user_model().objects.create_user(
        username='agent_current_result_serializer_user',
        password='secret',
    )
    task = AgentTask.objects.create(
        task_name='当前执行详情',
        user_requirement='只展示本次执行结果',
        created_by=user,
        started_at=timezone.now(),
    )
    TestExecutionResult.objects.create(
        task=task,
        module='rerun',
        status='SKIPPED',
        created_at=task.started_at - timedelta(minutes=5),
    )
    current = TestExecutionResult.objects.create(
        task=task,
        module='ui',
        status='FAILED',
    )

    serialized = AgentTaskSerializer(task).data['execution_results']

    assert [item['id'] for item in serialized] == [current.id]


def test_tool_registry_exposes_required_agent_tools():
    names = {item['name'] for item in default_registry.list()}
    assert {
        'analyze_requirement',
        'discover_test_points',
        'generate_test_data',
        'generate_test_case',
        'run_api_test',
        'run_web_test',
        'run_app_test',
        'run_performance_test',
        'run_security_test',
        'run_browser_agent',
        'analyze_failure',
        'self_heal_test',
        'rerun_fixed_test',
        'generate_from_api',
    }.issubset(names)
    assert {'generate_data', 'create_case', 'run_ui_test'}.isdisjoint(names)


def test_data_agent_generates_normal_boundary_and_security_cases():
    openapi = {
        'openapi': '3.0.0',
        'paths': {
            '/user/register': {
                'post': {
                    'requestBody': {
                        'content': {
                            'application/json': {
                                'schema': {
                                    'type': 'object',
                                    'required': ['username', 'phone', 'password', 'email'],
                                    'properties': {
                                        'username': {'type': 'string'},
                                        'phone': {'type': 'string'},
                                        'password': {'type': 'string'},
                                        'email': {'type': 'string', 'format': 'email'},
                                    },
                                }
                            }
                        }
                    }
                }
            }
        },
    }
    result = DataAgent().generate_from_api(
        swagger_doc=openapi,
        api='POST /user/register',
        mode=['normal', 'boundary', 'security'],
        count=2,
    )
    assert result['status'] == 'success'
    assert result['agent'] == 'AI Test Data Generator'
    assert result['asset_id']
    assert result['data_count'] > 2
    assert any(case['mode'] == 'normal' and case['data']['username'] == 'test001' for case in result['cases'])
    assert any(case.get('mode') == 'boundary' and case.get('parameter') == 'username' for case in result['cases'])
    assert any(case.get('mode') == 'security' and case.get('parameter') == 'username' for case in result['cases'])
    assert result['examples']['abnormal']


def test_security_agent_without_target_is_skipped():
    result = SecurityAgent().scan()
    assert result['status'] == 'skipped'
    assert result['risk'] == 'LOW'
    assert result['engine'] == 'yakit-compatible'
    assert 'Playwright' not in result['message']


def test_react_planner_includes_browser_security_data(monkeypatch):
    def fake_plan(self, requirement):
        return {
            'summary': '基础计划',
            'steps': [
                {'step': 1, 'type': 'ui', 'action': '执行 UI', 'tool': 'run_web_test', 'input': {}},
                {'step': 2, 'type': 'api', 'action': '执行 API', 'tool': 'run_api_test', 'input': {}},
            ],
            'test_plan': {'functional': [], 'abnormal': [], 'boundary': [], 'security': [], 'performance': []},
            'source': 'react_agent_planner',
        }

    monkeypatch.setattr('backend.agent.core.planner.TestPlanner.plan', fake_plan)
    plan = ReActPlanner().plan('测试登录页浏览器视觉和安全扫描', {
        'url': 'https://example.com/login',
        'api_url': 'https://example.com/api/login',
        'api_doc': {'openapi': '3.0.0'},
        'app_case_ids': [1],
        'performance_plan_id': 1,
    })
    tools = [step['tool'] for step in plan['steps']]
    assert plan['source'] == 'react_agent_planner'
    assert plan['react_architecture'] == ['Agent Planner', 'Action Executor', 'Observation', 'Reflection', 'Retry']
    assert 'run_browser_agent' in tools
    assert 'run_security_test' in tools
    assert 'generate_from_api' in tools
    assert 'analyze_requirement' in tools
    assert 'discover_test_points' in tools
    assert 'self_heal_test' in tools
    assert 'rerun_fixed_test' in tools
    assert 'run_app_test' in tools
    assert 'run_performance_test' in tools


def test_react_planner_fallback_builds_single_orchestration_chain(monkeypatch):
    monkeypatch.setattr('backend.agent.planner.TestPlanner._plan_with_llm', lambda self, requirement: None)
    plan = ReActPlanner().plan('打开 https://example.com 完成登录、搜索、下单并自动发现缺陷')
    tools = [step['tool'] for step in plan['steps']]
    assert tools == [
        'analyze_requirement',
        'run_browser_agent',
        'generate_test_case',
        'discover_test_points',
        'generate_test_data',
        'run_web_test',
        'analyze_failure',
        'self_heal_test',
        'rerun_fixed_test',
        'create_report',
    ]
    actions = ' '.join(step['action'] for step in plan['steps'])
    for phase in ['AI 分析需求', 'AI 理解系统', '生成测试方案', 'AI 发现测试点', '自动输出质量报告']:
        assert phase in actions
    assert '自动安全扫描' not in actions


def test_react_planner_infers_bare_ipv4_url(monkeypatch):
    monkeypatch.setattr('backend.agent.planner.TestPlanner._plan_with_llm', lambda self, requirement: None)

    plan = ReActPlanner().plan(
        '1、测试【172.16.0.88:9527/home】 2、测试【172.16.0.88:9527/result】'
    )
    browser_step = next(step for step in plan['steps'] if step['tool'] == 'run_browser_agent')
    ui_step = next(step for step in plan['steps'] if step['tool'] == 'run_web_test')

    assert browser_step['input']['url'] == 'http://172.16.0.88:9527/home'
    assert ui_step['input']['url'] == 'http://172.16.0.88:9527/home'


@pytest.mark.django_db
def test_generate_test_case_reuses_existing_normalized_titles_across_agent_tasks():
    user = get_user_model().objects.create_user(username='agent_case_dedup_user', password='x')
    project = Project.objects.create(name='Agent 用例去重项目', owner=user)
    test_plan = {
        'functional': ['  用户登录主流程  '],
        'abnormal': [],
        'boundary': [],
        'security': [],
        'performance': [],
    }
    first_task = AgentTask.objects.create(
        task_name='首次生成',
        user_requirement='测试用户登录',
        project=project,
        created_by=user,
        test_plan=test_plan,
    )
    second_task = AgentTask.objects.create(
        task_name='再次生成',
        user_requirement='测试用户登录',
        project=project,
        created_by=user,
        test_plan={**test_plan, 'functional': ['用户登录主流程']},
    )
    first_step = first_task.steps.create(
        step=1,
        step_type='case',
        action='生成测试方案',
        tool_name='generate_test_case',
    )
    second_step = second_task.steps.create(
        step=1,
        step_type='case',
        action='生成测试方案',
        tool_name='generate_test_case',
    )

    first_result = agent_tools.create_case(first_task, first_step, {})
    second_result = agent_tools.create_case(second_task, second_step, {})

    assert first_result['status'] == 'success'
    assert first_result['created_count'] == 1
    assert second_result['status'] == 'success'
    assert second_result['created_count'] == 0
    assert second_result['reused_count'] == 1
    assert second_result['case_ids'] == first_result['case_ids']
    assert TestCase.objects.filter(project=project).count() == 1
    case = TestCase.objects.get(project=project)
    assert case.step_details.count() == 2
    assert first_task.assets.filter(source_id=str(case.id), metadata__reused=False).exists()
    assert second_task.assets.filter(source_id=str(case.id), metadata__reused=True).exists()


@pytest.mark.django_db
def test_react_agent_runs_failure_analysis_and_report_after_tool_failure(monkeypatch):
    def fake_plan(self, goal, context):
        return {
            'summary': '失败闭环测试',
            'source': 'test',
            'test_plan': {'functional': ['执行失败闭环'], 'abnormal': [], 'boundary': [], 'security': [], 'performance': []},
            'steps': [{'step': 1, 'type': 'api', 'action': '执行失败工具', 'tool': 'fail_tool', 'input': {}}],
        }

    def fail_tool(task, step, payload):
        return {'status': 'failed', 'message': '接口返回 500', 'error': '500'}

    def analyze_failure(task, step, payload):
        task.failure_analysis = {
            'error_type': 'SERVER_ERROR',
            'reason': '接口返回 500',
            'solution': '查看服务端日志',
            'level': 'HIGH',
        }
        task.save(update_fields=['failure_analysis', 'updated_at'])
        return {'status': 'success', **task.failure_analysis}

    def create_report(task, step, payload):
        return {'status': 'success', 'summary': {'failed_steps': task.steps.filter(status='FAILED').count()}}

    monkeypatch.setattr(core_executor.ReActPlanner, 'plan', fake_plan)
    registry = ToolRegistry()
    registry.register(Tool('fail_tool', '故障工具', fail_tool))
    registry.register(Tool('analyze_failure', '失败分析', analyze_failure))
    registry.register(Tool('create_report', '测试报告', create_report))

    user = get_user_model().objects.create_user(username='agent_user', password='x')
    task = AgentTask.objects.create(task_name='失败闭环', user_requirement='执行失败闭环', created_by=user)

    result = core_executor.ReActAgentExecutor(task, registry=registry).run()
    result.refresh_from_db()

    assert result.status == AgentTask.STATUS_COMPLETED
    assert result.current_step == '执行存在失败，已完成失败分析'
    assert result.failure_analysis['error_type'] == 'SERVER_ERROR'
    assert result.steps.filter(tool_name='fail_tool', status='FAILED').exists()
    assert result.steps.filter(tool_name='analyze_failure', status='SUCCESS').exists()
    assert result.steps.filter(tool_name='create_report', status='SUCCESS').exists()
    assert result.execution_results.filter(module='api', status='FAILED').exists()


@pytest.mark.django_db
def test_failure_closure_finishes_healing_and_rerun_without_fake_execution(monkeypatch):
    def fake_plan(self, goal, context):
        return {
            'summary': '失败修复闭环测试',
            'source': 'test',
            'test_plan': {
                'functional': ['执行失败修复闭环'],
                'abnormal': [],
                'boundary': [],
                'security': [],
                'performance': [],
            },
            'steps': [
                {'step': 1, 'type': 'ui', 'action': '执行失败工具', 'tool': 'fail_tool', 'input': {}},
                {'step': 2, 'type': 'analysis', 'action': '失败分析', 'tool': 'analyze_failure', 'input': {}},
                {'step': 3, 'type': 'analysis', 'action': '生成修复建议', 'tool': 'self_heal_test', 'input': {}},
                {'step': 4, 'type': 'ui', 'action': '复跑判定', 'tool': 'rerun_fixed_test', 'input': {}},
                {'step': 5, 'type': 'report', 'action': '生成报告', 'tool': 'create_report', 'input': {}},
            ],
        }

    def fail_tool(task, step, payload):
        return {'status': 'failed', 'message': '页面访问超时', 'error': 'timeout'}

    monkeypatch.setattr(core_executor.ReActPlanner, 'plan', fake_plan)
    registry = ToolRegistry()
    registry.register(Tool('fail_tool', '故障工具', fail_tool))
    registry.register(Tool('analyze_failure', '失败分析', agent_tools.analyze_failure))
    registry.register(Tool('self_heal_test', '修复建议', agent_tools.self_heal_test))
    registry.register(Tool('rerun_fixed_test', '复跑判定', agent_tools.rerun_fixed_test))
    registry.register(Tool('create_report', '测试报告', agent_tools.create_report))
    user = get_user_model().objects.create_user(
        username='agent_failure_closure_steps_user',
        password='x',
    )
    task = AgentTask.objects.create(
        task_name='失败修复闭环',
        user_requirement='执行失败后完成所有收尾步骤',
        created_by=user,
    )

    result = core_executor.ReActAgentExecutor(task, registry=registry).run()
    result.refresh_from_db()

    assert result.steps.get(tool_name='analyze_failure').status == 'SUCCESS'
    assert result.steps.get(tool_name='self_heal_test').status == 'SUCCESS'
    assert result.steps.get(tool_name='rerun_fixed_test').status == 'NOT_APPLICABLE'
    assert result.steps.get(tool_name='create_report').status == 'SUCCESS'
    assert not result.steps.filter(status='PENDING').exists()
    assert not result.execution_results.filter(module='rerun').exists()


@pytest.mark.django_db
def test_react_agent_marks_conditional_step_not_applicable(monkeypatch):
    def fake_plan(self, goal, context):
        return {
            'summary': '条件分支测试',
            'source': 'test',
            'test_plan': {
                'functional': [],
                'abnormal': [],
                'boundary': [],
                'security': [],
                'performance': [],
            },
            'steps': [{
                'step': 1,
                'type': 'analysis',
                'action': '无需执行的条件步骤',
                'tool': 'conditional_tool',
                'input': {},
            }],
        }

    def conditional_tool(task, step, payload):
        return {'status': 'not_applicable', 'message': '当前条件不触发'}

    monkeypatch.setattr(core_executor.ReActPlanner, 'plan', fake_plan)
    registry = ToolRegistry()
    registry.register(Tool('conditional_tool', '条件工具', conditional_tool))

    user = get_user_model().objects.create_user(username='agent_not_applicable_user', password='x')
    task = AgentTask.objects.create(
        task_name='条件分支',
        user_requirement='验证无需执行状态',
        created_by=user,
    )

    result = core_executor.ReActAgentExecutor(task, registry=registry).run()
    result.refresh_from_db()

    assert result.status == AgentTask.STATUS_COMPLETED
    assert result.current_step == '流程已结束，当前没有需要执行的测试'
    assert result.steps.get().status == 'NOT_APPLICABLE'


@pytest.mark.django_db
def test_finish_completes_task_when_execution_result_failed_and_report_was_generated():
    user = get_user_model().objects.create_user(username='agent_failed_result_user', password='x')
    task = AgentTask.objects.create(
        task_name='执行结果失败',
        user_requirement='验证执行结果决定任务状态',
        created_by=user,
    )
    step = task.steps.create(
        step=1,
        step_type='ui',
        action='执行 UI',
        tool_name='run_web_test',
        status='SUCCESS',
    )
    TestExecutionResult.objects.create(
        task=task,
        step=step,
        module='ui',
        status='FAILED',
        response_payload={'real_execution': True},
    )

    core_executor.ReActAgentExecutor(task)._finish()
    task.refresh_from_db()

    assert task.status == AgentTask.STATUS_COMPLETED
    assert task.current_step == '执行存在失败，已完成失败分析'


@pytest.mark.django_db
def test_finish_ignores_failed_results_from_previous_attempt():
    user = get_user_model().objects.create_user(username='agent_current_attempt_user', password='x')
    task = AgentTask.objects.create(
        task_name='当前执行窗口',
        user_requirement='历史失败不影响本次通过',
        created_by=user,
        started_at=timezone.now(),
    )
    step = task.steps.create(
        step=1,
        step_type='ui',
        action='执行 UI',
        tool_name='run_web_test',
        status='SUCCESS',
    )
    TestExecutionResult.objects.create(
        task=task,
        module='ui',
        status='FAILED',
        response_payload={'real_execution': True},
        created_at=task.started_at - timedelta(minutes=5),
    )
    TestExecutionResult.objects.create(
        task=task,
        step=step,
        module='ui',
        status='PASSED',
        response_payload={'real_execution': True},
    )

    core_executor.ReActAgentExecutor(task)._finish()
    task.refresh_from_db()

    assert task.status == AgentTask.STATUS_COMPLETED
    assert task.current_step == '真实执行已完成，当前执行器未返回可查看报告'


@pytest.mark.django_db
def test_finish_marks_task_needs_input_when_required_execution_is_skipped():
    user = get_user_model().objects.create_user(username='agent_needs_input_user', password='x')
    task = AgentTask.objects.create(
        task_name='缺少执行资产',
        user_requirement='验证待补充条件',
        created_by=user,
    )
    task.steps.create(
        step=1,
        step_type='ui',
        action='执行 UI',
        tool_name='run_web_test',
        status='SKIPPED',
    )

    core_executor.ReActAgentExecutor(task)._finish()
    task.refresh_from_db()

    assert task.status == AgentTask.STATUS_NEEDS_INPUT
    assert '缺少可执行测试资产' in task.current_step
    assert task.context['needs_input'] == ['executable_asset']


@pytest.mark.django_db
def test_finish_marks_planning_only_task_needs_input_without_execution_results():
    user = get_user_model().objects.create_user(username='agent_planning_only_user', password='x')
    task = AgentTask.objects.create(
        task_name='只生成方案',
        user_requirement='测试商城页面',
        created_by=user,
        test_plan={
            'functional': ['验证商城主流程'],
            'abnormal': [],
            'boundary': [],
            'security': [],
            'performance': [],
        },
    )
    task.steps.create(
        step=1,
        step_type='report',
        action='生成报告',
        tool_name='create_report',
        status='NOT_APPLICABLE',
    )

    core_executor.ReActAgentExecutor(task)._finish()
    task.refresh_from_db()

    assert task.status == AgentTask.STATUS_NEEDS_INPUT
    assert '缺少可执行测试资产' in task.current_step
    assert task.context['needs_input'] == ['executable_asset']


@pytest.mark.django_db
def test_create_report_is_not_applicable_without_real_execution():
    user = get_user_model().objects.create_user(username='agent_no_execution_report_user', password='x')
    task = AgentTask.objects.create(
        task_name='无执行报告',
        user_requirement='只生成测试方案',
        created_by=user,
    )
    step = task.steps.create(
        step=1,
        step_type='report',
        action='生成报告',
        tool_name='create_report',
    )

    result = agent_tools.create_report(task, step, {})

    assert result['status'] == 'not_applicable'
    assert result['summary']['real_execution_results'] == 0
    assert result['report_assets'] == []
    assert not task.assets.filter(asset_type='report').exists()


@pytest.mark.django_db
def test_successful_attempt_skips_failure_healing_and_rerun_without_fake_result():
    user = get_user_model().objects.create_user(username='agent_no_failure_closure_user', password='x')
    task = AgentTask.objects.create(
        task_name='通过任务不修复',
        user_requirement='验证本次通过不生成伪复跑',
        created_by=user,
        started_at=timezone.now(),
        context={'self_heal': {'step_repair': '历史修复建议'}},
    )
    TestExecutionResult.objects.create(
        task=task,
        module='ui',
        status='FAILED',
        response_payload={'real_execution': True},
        created_at=task.started_at - timedelta(minutes=5),
    )
    analyze_step = task.steps.create(
        step=1,
        step_type='analysis',
        action='分析失败',
        tool_name='analyze_failure',
    )
    heal_step = task.steps.create(
        step=2,
        step_type='analysis',
        action='自愈',
        tool_name='self_heal_test',
    )
    rerun_step = task.steps.create(
        step=3,
        step_type='ui',
        action='复跑',
        tool_name='rerun_fixed_test',
    )

    analysis = agent_tools.analyze_failure(task, analyze_step, {})
    healing = agent_tools.self_heal_test(task, heal_step, {})
    before_count = task.execution_results.count()
    rerun = agent_tools.rerun_fixed_test(task, rerun_step, {})

    assert analysis['status'] == 'not_applicable'
    assert healing['status'] == 'not_applicable'
    assert rerun['status'] == 'not_applicable'
    assert task.execution_results.count() == before_count
    assert not task.execution_results.filter(module='rerun').exists()


@pytest.mark.django_db
def test_create_report_only_links_current_attempt_allure_result():
    user = get_user_model().objects.create_user(username='agent_current_report_user', password='x')
    task = AgentTask.objects.create(
        task_name='当前报告隔离',
        user_requirement='只关联本次 Allure 报告',
        created_by=user,
        started_at=timezone.now(),
    )
    TestExecutionResult.objects.create(
        task=task,
        module='ui',
        status='FAILED',
        report_url='/report/tasks/old-task/',
        response_payload={
            'real_execution': True,
            'auto_test_task_ids': ['old-task'],
            'results': [{'id': 'old-task', 'report_url': '/report/tasks/old-task/'}],
        },
        created_at=task.started_at - timedelta(minutes=5),
    )
    current = TestExecutionResult.objects.create(
        task=task,
        module='ui',
        status='PASSED',
        report_url='/report/tasks/current-task/',
        response_payload={
            'real_execution': True,
            'auto_test_task_ids': ['current-task'],
            'results': [{
                'id': 'current-task',
                'report_url': '/report/tasks/current-task/',
                'passed': 54,
                'failed': 0,
                'total': 54,
            }],
        },
    )
    report_step = task.steps.create(
        step=1,
        step_type='report',
        action='关联报告',
        tool_name='create_report',
    )

    result = agent_tools.create_report(task, report_step, {})

    assert result['summary']['real_execution_results'] == 1
    assert len(result['report_assets']) == 1
    report_asset = task.assets.get(asset_id=f'ui_allure_{task.id}_current-task')
    assert report_asset.metadata['execution_result_id'] == current.id
    assert report_asset.metadata['report_url'] == '/report/tasks/current-task/'
    assert not task.assets.filter(source_id='old-task').exists()


@pytest.mark.django_db
def test_execute_api_queues_task_and_deduplicates_running_clicks(monkeypatch):
    user = get_user_model().objects.create_user(username='agent_async_execute_user', password='x')
    task = AgentTask.objects.create(
        task_name='异步执行',
        user_requirement='执行已有 UI 用例',
        created_by=user,
        status=AgentTask.STATUS_FAILED,
    )
    queued = []
    monkeypatch.setattr(
        'apps.agent.views.execute_react_agent_task.delay',
        lambda task_id: queued.append(task_id),
    )
    client = APIClient()
    client.force_authenticate(user)

    first = client.post(f'/api/agent/tasks/{task.id}/execute/')
    second = client.post(f'/api/agent/tasks/{task.id}/execute/')
    task.refresh_from_db()

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.data['status'] == AgentTask.STATUS_RUNNING
    assert second.data['status'] == AgentTask.STATUS_RUNNING
    assert task.status == AgentTask.STATUS_RUNNING
    assert queued == [task.id]


@pytest.mark.django_db
def test_supplement_api_does_not_requeue_without_executable_asset(monkeypatch):
    user = get_user_model().objects.create_user(username='agent_supplement_user', password='x')
    task = AgentTask.objects.create(
        task_name='页面覆盖测试',
        user_requirement=(
            '测试 http://172.16.0.88:9527/home、'
            'http://172.16.0.88:9527/result 和 http://172.16.0.88:9527/success/'
        ),
        created_by=user,
        status=AgentTask.STATUS_NEEDS_INPUT,
        context={'frontend_url': 'http://172.16.0.88:9527/home'},
    )
    queued = []

    monkeypatch.setattr(
        'apps.agent.views.execute_react_agent_task.delay',
        lambda task_id: queued.append(task_id),
    )
    client = APIClient()
    client.force_authenticate(user)

    response = client.post(f'/api/agent/tasks/{task.id}/supplement/', {
        'auto_execute': True,
    }, format='json')

    assert response.status_code == 200
    assert response.data['task']['id'] == task.id
    assert response.data['task']['status'] == AgentTask.STATUS_NEEDS_INPUT
    assert response.data['needs_input'] == ['executable_asset']
    assert queued == []
    task.refresh_from_db()
    assert task.context.get('source') != 'ai_e2e_testing_agent'
    assert task.context['target_url'] == 'http://172.16.0.88:9527/home'
    assert '可执行业务步骤' not in task.user_requirement


@pytest.mark.django_db
def test_supplement_api_is_user_isolated_and_does_not_queue_noop(monkeypatch):
    owner = get_user_model().objects.create_user(username='agent_supplement_owner', password='x')
    other = get_user_model().objects.create_user(username='agent_supplement_other', password='x')
    task = AgentTask.objects.create(
        task_name='缺少网址',
        user_requirement='测试商城登录流程',
        created_by=owner,
        status=AgentTask.STATUS_NEEDS_INPUT,
    )
    queued = []
    monkeypatch.setattr('apps.agent.views.execute_react_agent_task.delay', lambda *args, **kwargs: queued.append(args))
    client = APIClient()
    client.force_authenticate(other)

    forbidden = client.post(f'/api/agent/tasks/{task.id}/supplement/', {}, format='json')
    assert forbidden.status_code == 404

    client.force_authenticate(owner)
    response = client.post(f'/api/agent/tasks/{task.id}/supplement/', {}, format='json')

    assert response.status_code == 200
    assert response.data['task']['status'] == AgentTask.STATUS_NEEDS_INPUT
    assert response.data['needs_input'] == ['executable_asset']
    assert queued == []


@pytest.mark.django_db
def test_supplement_api_persists_structured_text_without_duplicate_requeue(monkeypatch):
    user = get_user_model().objects.create_user(username='agent_structured_supplement_user', password='x')
    task = AgentTask.objects.create(
        task_name='自然语言申请流程',
        user_requirement='测试申请流程',
        created_by=user,
        status=AgentTask.STATUS_NEEDS_INPUT,
        context={
            'frontend_url': 'http://example.test/apply',
            'needs_input': ['executable_asset'],
        },
    )
    queued = []
    monkeypatch.setattr('apps.agent.views.execute_react_agent_task.delay', lambda *args: queued.append(args))
    client = APIClient()
    client.force_authenticate(user)
    payload = {
        'business_steps': '1. 输入手机号\n2. 点击提交',
        'acceptance_criteria': '页面显示申请成功',
        'auto_execute': True,
    }

    first = client.post(f'/api/agent/tasks/{task.id}/supplement/', payload, format='json')
    second = client.post(f'/api/agent/tasks/{task.id}/supplement/', payload, format='json')
    task.refresh_from_db()

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.data['needs_input'] == ['executable_asset']
    assert second.data['needs_input'] == ['executable_asset']
    assert task.status == AgentTask.STATUS_NEEDS_INPUT
    assert task.context['business_steps'] == payload['business_steps']
    assert task.context['acceptance_criteria'] == payload['acceptance_criteria']
    assert task.user_requirement.count('--- 用户补充的执行条件 ---') == 1
    assert queued == []


@pytest.mark.django_db
def test_stop_api_marks_running_ui_agent_task_stopped():
    user = get_user_model().objects.create_user(username='agent_stop_user', password='x')
    task = AgentTask.objects.create(
        task_name='停止异步任务',
        user_requirement='停止执行中的 UI Agent',
        created_by=user,
        status=AgentTask.STATUS_RUNNING,
        current_step='正在执行测试',
        progress=40,
    )
    running_step = AgentStep.objects.create(
        task=task,
        step=1,
        step_type='ui',
        action='执行 UI 用例',
        tool_name='run_ui_test',
        status=AgentStep.STATUS_RUNNING,
    )
    pending_step = AgentStep.objects.create(
        task=task,
        step=2,
        step_type='report',
        action='生成报告',
        tool_name='create_report',
        status=AgentStep.STATUS_PENDING,
    )
    client = APIClient()
    client.force_authenticate(user)

    response = client.post(f'/api/agent/tasks/{task.id}/stop/')
    task.refresh_from_db()
    running_step.refresh_from_db()
    pending_step.refresh_from_db()

    assert response.status_code == 200
    assert response.data['task']['status'] == AgentTask.STATUS_STOPPED
    assert task.status == AgentTask.STATUS_STOPPED
    assert task.current_step == '用户已停止 Agent 任务'
    assert task.finished_at is not None
    assert running_step.status == AgentStep.STATUS_SKIPPED
    assert pending_step.status == AgentStep.STATUS_SKIPPED
    assert task.memories.filter(content='用户已停止 Agent 任务').exists()


@pytest.mark.django_db
def test_full_cycle_api_queues_auto_execution(monkeypatch):
    user = get_user_model().objects.create_user(username='agent_async_full_cycle_user', password='x')
    queued = []
    monkeypatch.setattr(
        'apps.agent.views.execute_react_agent_task.delay',
        lambda task_id: queued.append(task_id),
    )
    client = APIClient()
    client.force_authenticate(user)

    response = client.post('/api/agent/full-cycle/', {
        'task_name': '异步全流程',
        'requirement': '执行已有 UI YAML 用例',
        'ui_case_files': ['login.yaml'],
        'auto_execute': True,
    }, format='json')

    assert response.status_code == 201
    assert response.data['status'] == 'running'
    assert response.data['task']['status'] == AgentTask.STATUS_RUNNING
    assert queued == [response.data['task']['id']]


def test_failure_analyzer_reports_locator_repair():
    from backend.agent.analyzer import FailureAnalyzer

    result = FailureAnalyzer().analyze(
        logs='Element not found: #login-btn',
        response={'error': 'selector locator failure'},
    )
    assert result['error_type'] == 'ELEMENT_CHANGED'
    assert result['old_locator']
    assert result['new_locator']


def test_failure_analyzer_identifies_rejected_input_value():
    from backend.agent.analyzer import FailureAnalyzer

    result = FailureAnalyzer()._fallback(
        logs="AssertionError: 输入值未稳定保存: 期望 '董佳'，实际 ''",
        screenshot='',
        response={},
    )

    assert result['error_type'] == 'INPUT_VALUE_REJECTED'
    assert '数据绑定' in result['suggestion']


@pytest.mark.django_db
def test_run_ui_test_url_only_is_skipped_without_executing_page_actions(monkeypatch):
    user = get_user_model().objects.create_user(username='ui_bridge_user', password='x')
    task = AgentTask.objects.create(
        task_name='Web桥接',
        user_requirement='打开 https://example.com 登录',
        created_by=user,
        context={'url': 'https://example.com'},
    )
    step = task.steps.create(step=1, step_type='ui', action='执行 Web', tool_name='run_web_test')

    called = False

    def fake_run(self, **kwargs):
        nonlocal called
        called = True
        return {'status': 'success'}

    monkeypatch.setattr('backend.browser_agent.browser_controller.WebVisionAgent.run', fake_run)

    result = agent_tools.run_ui_test(task, step, {'goal': '登录'})

    assert result['status'] == 'skipped'
    assert result['real_execution'] is False
    assert called is False
    execution = task.execution_results.get(module='ui')
    assert execution.status == 'SKIPPED'
    assert execution.report_url == ''
    assert '页面探索只能证明 URL 可访问' in execution.logs


@pytest.mark.django_db
def test_browser_exploration_with_zero_actions_is_not_real_execution(monkeypatch):
    user = get_user_model().objects.create_user(username='browser_evidence_user', password='x')
    task = AgentTask.objects.create(
        task_name='页面探索证据',
        user_requirement='打开登录页',
        created_by=user,
    )
    step = task.steps.create(
        step=1,
        step_type='browser',
        action='探索页面',
        tool_name='run_browser_agent',
    )
    monkeypatch.setattr(
        'backend.browser_agent.browser_controller.WebVisionAgent.run',
        lambda self, **kwargs: {
            'status': 'success',
            'message': '页面已打开并截图',
            'visual_summary': {'action_count': 0},
            'actions': [],
            'screenshot_url': '/media/browser-agent/evidence.png',
        },
    )

    result = default_registry.execute(
        'run_browser_agent',
        task,
        step,
        {'url': 'https://example.com/login'},
    )
    execution = task.execution_results.get(module='browser')

    assert result['status'] == 'success'
    assert result['real_execution'] is False
    assert result['evidence_type'] == 'page_exploration'
    assert execution.status == 'PASSED'
    assert execution.response_payload['real_execution'] is False
    assert execution.report_url == ''


@pytest.mark.django_db
def test_browser_exploration_without_url_is_not_applicable():
    user = get_user_model().objects.create_user(username='browser_no_url_user', password='x')
    task = AgentTask.objects.create(
        task_name='YAML 无独立 URL',
        user_requirement='执行已有 UI YAML 用例',
        created_by=user,
        context={'case_files': ['login.yaml']},
    )
    step = task.steps.create(
        step=1,
        step_type='browser',
        action='探索页面',
        tool_name='run_browser_agent',
    )

    result = default_registry.execute('run_browser_agent', task, step, {})

    assert result['status'] == 'not_applicable'
    assert not task.execution_results.exists()


@pytest.mark.django_db
def test_understand_system_browser_step_runs_analyze_only(monkeypatch):
    user = get_user_model().objects.create_user(username='browser_understand_user', password='x')
    task = AgentTask.objects.create(
        task_name='理解系统',
        user_requirement='理解目标页面',
        created_by=user,
    )
    step = task.steps.create(
        step=1,
        step_type='browser',
        action='AI 理解系统',
        tool_name='run_browser_agent',
    )
    calls = {}

    def fake_run(self, **kwargs):
        calls.update(kwargs)
        return {'status': 'success', 'message': '只分析页面', 'screenshot': '/tmp/page.png'}

    monkeypatch.setattr('backend.browser_agent.browser_controller.WebVisionAgent.run', fake_run)

    result = default_registry.execute(
        'run_browser_agent',
        task,
        step,
        {'url': 'https://example.test', 'mode': 'understand_system'},
    )

    assert result['status'] == 'success'
    assert calls['analyze_only'] is True
    assert task.execution_results.get(module='browser').status == 'PASSED'


def test_browser_controller_uses_configured_chrome_and_closes_resources(monkeypatch):
    from backend.browser_agent.browser_controller import BrowserController

    calls = {}

    class FakeContext:
        def set_default_timeout(self, value):
            calls['action_timeout'] = value

        def set_default_navigation_timeout(self, value):
            calls['navigation_timeout'] = value

        def new_page(self):
            return object()

        def close(self):
            calls['context_closed'] = True

    class FakeBrowser:
        def new_context(self, **kwargs):
            calls['context_options'] = kwargs
            return FakeContext()

        def close(self):
            calls['browser_closed'] = True

    class FakeChromium:
        def launch(self, **kwargs):
            calls['launch_options'] = kwargs
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

        def stop(self):
            calls['playwright_stopped'] = True

    class FakeManager:
        def start(self):
            return FakePlaywright()

    monkeypatch.setenv('E2E_BROWSER_EXECUTABLE_PATH', '/usr/bin/google-chrome')
    monkeypatch.setattr('playwright.sync_api.sync_playwright', lambda: FakeManager())

    with BrowserController() as controller:
        assert controller.page is not None

    assert calls['launch_options'] == {
        'headless': True,
        'executable_path': '/usr/bin/google-chrome',
    }
    assert calls['context_closed'] is True
    assert calls['browser_closed'] is True
    assert calls['playwright_stopped'] is True


def test_browser_controller_stops_playwright_when_browser_launch_fails(monkeypatch):
    from backend.browser_agent.browser_controller import BrowserController

    calls = {}

    class FakeChromium:
        def launch(self, **kwargs):
            raise RuntimeError('chrome launch failed')

    class FakePlaywright:
        chromium = FakeChromium()

        def stop(self):
            calls['playwright_stopped'] = True

    class FakeManager:
        def start(self):
            return FakePlaywright()

    monkeypatch.setattr('playwright.sync_api.sync_playwright', lambda: FakeManager())

    with pytest.raises(RuntimeError, match='chrome launch failed'):
        BrowserController().__enter__()

    assert calls['playwright_stopped'] is True


@pytest.mark.django_db
def test_agent_binds_generated_data_to_url_but_does_not_claim_execution(monkeypatch):
    user = get_user_model().objects.create_user(username='ui_url_binding_user', password='x')
    target_url = 'https://example.com/login'
    task = AgentTask.objects.create(
        task_name='URL 参数化执行',
        user_requirement=f'打开 {target_url} 登录',
        created_by=user,
        context={'url': target_url},
    )
    generate_step = task.steps.create(
        step=1,
        step_type='data',
        action='生成 URL 数据',
        tool_name='generate_test_data',
    )
    run_step = task.steps.create(
        step=2,
        step_type='ui',
        action='执行 URL 用例',
        tool_name='run_web_test',
    )
    generated = agent_tools.generate_test_data(
        task,
        generate_step,
        {'count': 2, 'fields': ['phone', 'password'], 'type': 'CUSTOM'},
    )
    result = agent_tools.run_ui_test(task, run_step, {})

    binding = generated['bindings'][0]
    requirement = TestDataAssetRequirement.objects.get(id=binding['requirement_id'])
    execution = task.execution_results.get(module='ui')
    assert binding['target_id'] == target_url
    assert requirement.source_asset_id == generated['test_data_asset_id']
    assert requirement.target_type == 'ui_automation'
    assert result['status'] == 'skipped'
    assert result['real_execution'] is False
    assert execution.status == 'SKIPPED'
    assert execution.report_url == ''


@pytest.mark.django_db
def test_run_ui_test_uses_existing_yaml_case_files(monkeypatch):
    user = get_user_model().objects.create_user(username='ui_yaml_bridge_user', password='x')
    task = AgentTask.objects.create(
        task_name='UI YAML桥接',
        user_requirement='按已有 UI YAML 登录',
        created_by=user,
        context={'case_files': ['login.yaml'], 'base_url': 'https://example.com'},
    )
    step = task.steps.create(step=1, step_type='ui', action='执行 UI YAML', tool_name='run_web_test')

    monkeypatch.delenv('AUTO_TEST_AGENT_API_URL', raising=False)

    result = agent_tools.run_ui_test(task, step, {})

    assert result['status'] == 'skipped'
    assert result['real_execution'] is False
    execution = task.execution_results.get(module='ui')
    assert execution.status == 'SKIPPED'
    assert execution.request_payload['case_files'] == ['login.yaml']
    assert 'Allure' in execution.logs


@pytest.mark.django_db
def test_run_ui_yaml_uses_auto_test_parameterized_orchestration(monkeypatch):
    user = get_user_model().objects.create_user(username='ui_remote_bridge_user', password='x')
    task = AgentTask.objects.create(
        task_name='UI 参数化桥接',
        user_requirement='按已有 UI YAML 登录',
        created_by=user,
        context={
            'case_files': ['login.yaml'],
            'base_url': 'https://example.com',
            'test_data_count': 2,
        },
    )
    step = task.steps.create(step=1, step_type='ui', action='执行 UI YAML', tool_name='run_web_test')
    TestAsset.objects.create(
        asset_id='ui_yaml_login.yaml',
        asset_type='ui',
        name='另一个任务已登记的登录用例',
        source_model='auto-test.cases.ui',
        source_id='login.yaml',
        created_by=user,
    )
    calls = {}

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_post(url, **kwargs):
        calls['post_url'] = url
        calls['post'] = kwargs
        return FakeResponse({
            'task_ids': ['ui-task-1'],
            'bindings': [{
                'case_file': 'login.yaml',
                'alias': 'agent_testhub_1',
                'runtime_override': [{
                    'stepId': 'input-user',
                    'runtimeValue': '${dataAssets.agent_testhub_1.username}',
                }],
            }],
            'assets': [{'data_center_asset': {'asset_id': 7, 'synced': True}}],
        })

    def fake_get(url, **kwargs):
        calls['get_url'] = url
        calls['get'] = kwargs
        return FakeResponse({
            'id': 'ui-task-1',
            'status': 'success',
            'passed': 2,
            'failed': 0,
            'report_url': '/report/tasks/ui-task-1/',
        })

    monkeypatch.setenv('AUTO_TEST_AGENT_API_URL', 'http://auto-test:8000')
    monkeypatch.setenv('TEST_DATA_CENTER_AGENT_TOKEN', 'shared-secret')
    monkeypatch.setattr('requests.post', fake_post)
    monkeypatch.setattr('requests.get', fake_get)

    result = agent_tools.run_ui_test(task, step, {})

    assert result['status'] == 'passed'
    assert result['real_execution'] is True
    assert result['test_data_count'] == 2
    assert result['auto_test_task_ids'] == ['ui-task-1']
    assert calls['post']['json']['test_data_count'] == 2
    assert calls['post']['json']['parent_task_id'] == f'testhub-{task.id}'
    assert calls['post']['json']['data_bindings'] == []
    assert calls['post']['headers']['X-Agent-Token'] == 'shared-secret'
    execution = task.execution_results.get(module='ui')
    assert execution.status == 'PASSED'
    assert execution.response_payload['bindings'][0]['alias'] == 'agent_testhub_1'
    assert execution.report_url == '/report/tasks/ui-task-1/'
    asset = task.assets.get(asset_type='ui', source_id='login.yaml')
    assert asset.asset_id == f'ui_yaml_{task.id}_login.yaml'
    assert asset.metadata['test_data_count'] == 2
    assert asset.metadata['auto_test_task_ids'] == ['ui-task-1']

    report_step = task.steps.create(
        step=2,
        step_type='report',
        action='关联报告',
        tool_name='create_report',
    )
    report_result = agent_tools.create_report(task, report_step, {})
    report_asset = task.assets.get(asset_type='report')
    assert report_result['report_id'] is None
    assert report_result['report_assets'] == [report_asset.asset_id]
    assert report_asset.source_model == 'auto-test.tasks'
    assert report_asset.metadata['report_format'] == 'allure'
    assert report_asset.metadata['report_url'] == '/report/tasks/ui-task-1/'
    assert report_asset.metadata['module_url'] == '/auto-test/reports?tab=ui&task_id=ui-task-1'


@pytest.mark.django_db
def test_run_ui_yaml_sends_generated_rows_with_the_current_orchestration(monkeypatch):
    user = get_user_model().objects.create_user(username='ui_direct_rows_user', password='x')
    data_asset = TestDataAsset.objects.create(
        asset_type='CUSTOM',
        name='Agent 登录数据',
        status=TestDataAsset.STATUS_AVAILABLE,
        payload=[
            {'phone': '13800138000', 'password': 'Passw0rd@1001'},
            {'phone': '13800138001', 'password': 'Passw0rd@1002'},
            {'phone': '13800138002', 'password': 'Passw0rd@1003'},
        ],
        created_by=user,
    )
    task = AgentTask.objects.create(
        task_name='UI 直接传递数据行',
        user_requirement='用生成数据执行登录',
        created_by=user,
        context={
            'case_files': ['login.yaml'],
            'test_data_count': 3,
            'generated_test_data': {
                'bindings': [{
                    'target_id': 'login.yaml',
                    'target_type': 'ui_automation',
                    'asset_id': data_asset.id,
                    'alias': 'agent_ui_yaml_direct',
                    'fields': ['phone', 'password'],
                }],
            },
        },
    )
    step = task.steps.create(step=1, step_type='ui', action='执行 UI YAML', tool_name='run_web_test')
    calls = {}

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_post(_url, **kwargs):
        calls['request'] = kwargs['json']
        return FakeResponse({'task_ids': ['ui-task-rows'], 'bindings': [], 'assets': []})

    monkeypatch.setenv('AUTO_TEST_AGENT_API_URL', 'http://auto-test:8000')
    monkeypatch.setattr('requests.post', fake_post)
    monkeypatch.setattr('requests.get', lambda *_args, **_kwargs: FakeResponse({
        'id': 'ui-task-rows',
        'status': 'success',
        'passed': 3,
        'failed': 0,
        'report_url': '/report/tasks/ui-task-rows/',
    }))

    agent_tools.run_ui_test(task, step, {})

    binding = calls['request']['data_bindings'][0]
    assert binding['alias'] == 'agent_ui_yaml_direct'
    assert binding['runtime_data_rows'] == data_asset.payload


@pytest.mark.django_db
def test_run_ui_yaml_persists_auto_test_failure_evidence(monkeypatch):
    user = get_user_model().objects.create_user(username='ui_failure_evidence_user', password='x')
    task = AgentTask.objects.create(
        task_name='UI 失败证据回传',
        user_requirement='按已有 UI YAML 登录',
        created_by=user,
        context={'case_files': ['login.yaml'], 'test_data_count': 3},
    )
    step = task.steps.create(step=1, step_type='ui', action='执行 UI YAML', tool_name='run_web_test')

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    monkeypatch.setenv('AUTO_TEST_AGENT_API_URL', 'http://auto-test:8000')
    monkeypatch.setattr('requests.post', lambda *_args, **_kwargs: FakeResponse({
        'task_ids': ['ui-task-failed'],
        'bindings': [],
        'assets': [],
    }))
    monkeypatch.setattr('requests.get', lambda *_args, **_kwargs: FakeResponse({
        'id': 'ui-task-failed',
        'status': 'failed',
        'passed': 2,
        'failed': 1,
        'total': 3,
        'report_url': '/report/tasks/ui-task-failed/',
        'failure_steps': [{
            'step_index': 2,
            'name': '输入验证码',
            'status': 'failed',
            'error': 'AssertionError: 输入值未稳定保存',
            'screenshot': '/screenshots/failure.png',
        }],
        'log_tail': 'pytest output\nAssertionError: 输入值未稳定保存',
    }))

    result = agent_tools.run_ui_test(task, step, {})
    execution = task.execution_results.get(module='ui')

    assert result['status'] == 'failed'
    assert result['real_execution'] is True
    assert execution.status == 'FAILED'
    assert '输入验证码' in execution.logs
    assert 'AssertionError: 输入值未稳定保存' in execution.logs
    assert execution.screenshot == '/screenshots/failure.png'
    assert execution.response_payload['results'][0]['failure_steps'][0]['step_index'] == 2


@pytest.mark.django_db
def test_run_ui_yaml_timeout_fails_instead_of_staying_running(monkeypatch):
    user = get_user_model().objects.create_user(username='ui_timeout_user', password='x')
    task = AgentTask.objects.create(
        task_name='UI 执行超时',
        user_requirement='按已有 UI YAML 登录',
        created_by=user,
        context={'case_files': ['login.yaml']},
    )
    step = task.steps.create(step=1, step_type='ui', action='执行 UI YAML', tool_name='run_web_test')

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    monkeypatch.setenv('AUTO_TEST_AGENT_API_URL', 'http://auto-test:8000')
    monkeypatch.setenv('AUTO_TEST_AGENT_POLL_TIMEOUT_SECONDS', '0')
    monkeypatch.setattr('requests.post', lambda *_args, **_kwargs: FakeResponse({
        'task_ids': ['ui-task-running'],
        'bindings': [],
        'assets': [],
    }))
    monkeypatch.setattr('requests.get', lambda *_args, **_kwargs: FakeResponse({
        'id': 'ui-task-running',
        'status': 'running',
    }))

    result = agent_tools.run_ui_test(task, step, {})
    task.refresh_from_db()
    execution = task.execution_results.get(module='ui')

    assert result['status'] == 'failed'
    assert '等待 Auto-test UI 执行超时' in result['message']
    assert '等待 Auto-test UI 自动化执行器返回结果' in task.current_step
    assert execution.status == 'FAILED'
    assert execution.response_payload['real_execution'] is False


@pytest.mark.django_db
def test_run_api_test_requires_api_test_object_reference():
    user = get_user_model().objects.create_user(username='api_object_bridge_user', password='x')
    task = AgentTask.objects.create(
        task_name='API对象桥接',
        user_requirement='执行 API 测试对象',
        created_by=user,
        context={
            'api_test_objects': [{
                'team_id': 'team-1',
                'target_id': 'api-1',
                'target_type': 'api',
                'name': '登录接口',
            }]
        },
    )
    step = task.steps.create(step=1, step_type='api', action='执行 API 测试对象', tool_name='run_api_test')

    result = agent_tools.run_api_test(task, step, {})

    assert result['status'] == 'skipped'
    assert result['real_execution'] is False
    execution = task.execution_results.get(module='api')
    assert execution.status == 'SKIPPED'
    assert execution.request_payload['test_object_refs'][0]['target_id'] == 'api-1'
    assert '测试对象' in execution.logs


@pytest.mark.django_db
def test_api_agent_generates_bound_parameter_data_for_referenced_object():
    user = get_user_model().objects.create_user(username='api_data_binding_user', password='x')
    task = AgentTask.objects.create(
        task_name='API 参数化绑定',
        user_requirement='使用自动生成数据执行登录接口',
        created_by=user,
        context={
            'api_test_objects': [{
                'target_id': 'login-api',
                'target_type': 'api',
                'name': '登录接口',
            }],
        },
    )
    generate_step = task.steps.create(
        step=1,
        step_type='data',
        action='生成接口数据',
        tool_name='generate_test_data',
    )
    run_step = task.steps.create(
        step=2,
        step_type='api',
        action='执行接口',
        tool_name='run_api_test',
    )

    generated = agent_tools.generate_test_data(
        task,
        generate_step,
        {'count': 2, 'fields': ['phone', 'password'], 'type': 'CUSTOM'},
    )
    result = agent_tools.run_api_test(task, run_step, {})

    data_asset = TestDataAsset.objects.get(id=generated['test_data_asset_id'])
    requirement = TestDataAssetRequirement.objects.get(
        target_type='api_automation',
        alias=f'agent_api_{task.id}_1',
    )
    execution = task.execution_results.get(module='api')
    assert len(data_asset.payload) == 2
    assert requirement.source_asset_id == data_asset.id
    assert requirement.target_case_name == '登录接口'
    assert result['status'] == 'skipped'
    assert execution.request_payload['data_bindings'][0]['asset_id'] == data_asset.id
    assert execution.request_payload['runtime_variables'] == {
        'phone': f'${{dataAssets.{requirement.alias}.phone}}',
        'password': f'${{dataAssets.{requirement.alias}.password}}',
    }


@pytest.mark.django_db
def test_agent_generates_and_binds_data_to_ui_case_before_execution():
    user = get_user_model().objects.create_user(username='ui_data_binding_user', password='x')
    project = Project.objects.create(name='UI 数据项目', owner=user)
    ui_case = TestCase.objects.create(
        project=project,
        title='用户登录',
        description='登录页',
        steps='输入账号密码',
        expected_result='进入首页',
        test_type='ui',
        author=user,
    )
    task = AgentTask.objects.create(
        task_name='UI 参数关联',
        user_requirement='用生成数据执行登录',
        created_by=user,
        project=project,
        context={'ui_case_ids': [ui_case.id]},
    )
    generate_step = task.steps.create(
        step=1,
        step_type='data',
        action='生成 UI 数据',
        tool_name='generate_test_data',
    )
    run_step = task.steps.create(
        step=2,
        step_type='ui',
        action='执行 UI 用例',
        tool_name='run_ui_test',
    )

    generated = agent_tools.generate_test_data(
        task,
        generate_step,
        {'count': 1, 'fields': ['phone', 'password'], 'type': 'CUSTOM'},
    )
    result = agent_tools.run_ui_test(task, run_step, {})

    binding = next(item for item in generated['bindings'] if item['target_type'] == 'ui_automation')
    requirement = TestDataAssetRequirement.objects.get(id=binding['requirement_id'])
    execution = task.execution_results.get(module='ui')
    assert requirement.source_asset_id == generated['test_data_asset_id']
    assert requirement.target_case_id == ui_case.id
    assert result['data_bindings'][0]['asset_id'] == generated['test_data_asset_id']
    assert execution.request_payload['data_bindings'][0]['requirement_id'] == requirement.id


@pytest.mark.django_db
def test_run_ui_test_uses_selected_ui_case_reference():
    user = get_user_model().objects.create_user(username='ui_case_bridge_user', password='x')
    project = Project.objects.create(name='UI 引用项目', owner=user)
    ui_case = TestCase.objects.create(
        project=project,
        title='登录页 UI 自动化',
        description='验证登录页交互',
        steps='输入账号密码并点击登录',
        expected_result='进入首页',
        test_type='ui',
        author=user,
    )
    task = AgentTask.objects.create(
        task_name='UI 用例引用',
        user_requirement='分析引用的 UI 自动化用例',
        project=project,
        created_by=user,
        context={'ui_case_ids': [ui_case.id], 'source_entry': 'reference'},
    )
    step = task.steps.create(step=1, step_type='ui', action='执行 UI 用例引用', tool_name='run_web_test')

    result = agent_tools.run_ui_test(task, step, {})

    assert result['status'] == 'skipped'
    assert result['real_execution'] is False
    assert result['ui_case_refs'][0]['case_id'] == ui_case.id
    assert task.assets.filter(asset_type='ui', source_id=str(ui_case.id)).exists()


@pytest.mark.django_db
def test_security_tool_persists_real_scan_result(monkeypatch):
    user = get_user_model().objects.create_user(username='security_bridge_user', password='x')
    task = AgentTask.objects.create(task_name='安全桥接', user_requirement='扫描接口', created_by=user)
    step = task.steps.create(step=1, step_type='security', action='安全扫描', tool_name='run_security_test')

    def fake_scan(self, **kwargs):
        return {
            'status': 'success',
            'risk': 'HIGH',
            'message': 'fake yakit scan',
            'findings': [{'type': 'SQL注入测试', 'evidence': 'sql error'}],
        }

    monkeypatch.setattr('backend.security_agent.scanner.SecurityAgent.scan', fake_scan)

    result = default_registry.execute('run_security_test', task, step, {'target': 'https://example.com/api'})

    assert result['real_execution'] is True
    execution = task.execution_results.get(module='security')
    assert execution.status == 'FAILED'
    assert execution.response_payload['risk'] == 'HIGH'


@pytest.mark.django_db
def test_security_tool_rejects_frontend_only_url(monkeypatch):
    user = get_user_model().objects.create_user(username='security_frontend_user', password='x')
    task = AgentTask.objects.create(
        task_name='前端页面安全边界',
        user_requirement='检查登录页面',
        created_by=user,
        context={'frontend_url': 'https://example.com/login', 'api_url': ''},
    )
    step = task.steps.create(step=1, step_type='security', action='安全扫描', tool_name='run_security_test')

    def unexpected_scan(self, **kwargs):
        raise AssertionError('frontend-only URL must not reach the API security scanner')

    monkeypatch.setattr('backend.security_agent.scanner.SecurityAgent.scan', unexpected_scan)
    result = default_registry.execute(
        'run_security_test',
        task,
        step,
        {'target': 'https://example.com/login'},
    )

    assert result['status'] == 'not_applicable'
    assert result['real_execution'] is False
    assert not task.execution_results.exists()


@pytest.mark.django_db
def test_run_app_test_bridges_to_app_agent_task(monkeypatch):
    user = get_user_model().objects.create_user(username='app_bridge_user', password='x')
    Project.objects.create(name='总项目', owner=user)
    app_project = AppProject.objects.create(name='移动商城', owner=user)
    device = AppDevice.objects.create(
        device_id='emulator-agent-1',
        name='Pixel Agent',
        platform='android',
        status='available',
        android_version='15',
    )
    package = AppPackage.objects.create(
        name='商城',
        package_name='com.runnergo.mall.agent',
        created_by=user,
    )
    app_case = AppTestCase.objects.create(
        project=app_project,
        name='登录成功',
        app_package=package,
        ui_flow={'steps': [{'type': 'touch', 'name': '登录'}]},
        created_by=user,
    )
    task = AgentTask.objects.create(task_name='APP桥接', user_requirement='打开 APP 登录', created_by=user)
    step = task.steps.create(step=1, step_type='app', action='APP执行', tool_name='run_app_test')

    def fake_run(app_task_id, execute_only=False):
        from apps.app_automation.models import AppAgentTask

        assert execute_only is True
        app_task = AppAgentTask.objects.get(id=app_task_id)
        assert app_task.plan['source'] == 'unified_ai_test_agent'
        assert app_task.plan['selected_case_ids'] == [app_case.id]
        app_task.status = 'completed'
        app_task.progress = 100
        app_task.plan = {**app_task.plan, 'coverage_gap': False}
        app_task.result_summary = {'total': 1, 'passed': 1, 'failed': 0, 'stopped': 0, 'pass_rate': 100}
        app_task.finished_at = timezone.now()
        app_task.save(update_fields=['status', 'progress', 'plan', 'result_summary', 'finished_at', 'updated_at'])

    monkeypatch.setattr('apps.app_automation.tasks.execute_app_agent_task.run', fake_run)

    result = agent_tools.run_app_test(task, step, {
        'app_project_id': app_project.id,
        'device_id': device.id,
        'app_package_id': package.id,
        'app_case_ids': [app_case.id],
    })

    assert result['status'] == 'passed'
    assert result['real_execution'] is True
    assert result['app_agent_task_id']
    execution = task.execution_results.get(module='app')
    assert execution.status == 'PASSED'
    assert result['plan']['asset_source'] == 'app_automation.ui_flow'
    assert task.assets.filter(asset_type='app').exists()


@pytest.mark.django_db
def test_run_app_test_skips_without_existing_app_case_asset():
    user = get_user_model().objects.create_user(username='app_no_asset_user', password='x')
    AppProject.objects.create(name='移动商城', owner=user)
    AppDevice.objects.create(
        device_id='emulator-agent-2',
        name='Pixel Agent 2',
        platform='android',
        status='available',
        android_version='15',
    )
    task = AgentTask.objects.create(task_name='APP无资产', user_requirement='打开 APP 登录', created_by=user)
    step = task.steps.create(step=1, step_type='app', action='APP执行', tool_name='run_app_test')

    result = agent_tools.run_app_test(task, step, {})

    assert result['status'] == 'skipped'
    assert result['real_execution'] is False
    execution = task.execution_results.get(module='app')
    assert execution.status == 'SKIPPED'
    assert '已有测试用例' in execution.logs


def test_ui_and_app_plans_use_the_same_orchestration_chain(monkeypatch):
    monkeypatch.setattr('backend.agent.planner.TestPlanner._plan_with_llm', lambda self, requirement: None)

    ui_plan = ReActPlanner().plan('验证登录流程', {'ui_case_files': ['login.yaml']})
    app_plan = ReActPlanner().plan('验证登录流程', {'app_case_ids': [101]})
    ui_tools = [item['tool'] for item in ui_plan['steps']]
    app_tools = [item['tool'] for item in app_plan['steps']]

    assert 'run_browser_agent' not in ui_tools
    assert 'run_web_test' in ui_tools
    assert 'run_app_test' in app_tools
    assert [item.replace('run_web_test', 'run_mode_test') for item in ui_tools] == [
        item.replace('run_app_test', 'run_mode_test') for item in app_tools
    ]


@pytest.mark.django_db
def test_full_cycle_only_accepts_ui_agent_mode(monkeypatch):
    user = get_user_model().objects.create_user(username='agent_service_mode_user', password='x')
    queued = []
    monkeypatch.setattr('apps.agent.views.execute_react_agent_task.delay', lambda task_id: queued.append(task_id))
    client = APIClient()
    client.force_authenticate(user)

    ui_response = client.post('/api/agent/full-cycle/', {
        'agent_service': 'ui',
        'requirement': '执行 UI 登录用例',
        'ui_case_files': ['login.yaml'],
        'auto_execute': False,
    }, format='json')
    app_response = client.post('/api/agent/full-cycle/', {
        'agent_service': 'app',
        'requirement': '执行 APP 登录用例',
        'app_project_id': 123,
        'app_case_ids': [456],
        'auto_execute': False,
    }, format='json')

    assert ui_response.status_code == 201
    assert app_response.status_code == 400
    assert ui_response.data['task']['context']['agent_service'] == 'ui'
    assert queued == []

    ui_list = client.get('/api/agent/tasks/', {'agent_service': 'ui'})
    ui_rows = ui_list.data.get('results', ui_list.data) if isinstance(ui_list.data, dict) else ui_list.data
    assert [item['id'] for item in ui_rows] == [ui_response.data['task']['id']]


@pytest.mark.django_db
def test_ui_case_draft_requires_confirmation_before_persistence(monkeypatch):
    user = get_user_model().objects.create_user(username='agent_ui_case_confirm_user', password='x')
    project = Project.objects.create(name='Agent UI 保存项目', owner=user)
    task = AgentTask.objects.create(
        task_name='UI 登录步骤',
        user_requirement='验证 UI 登录',
        project=project,
        created_by=user,
        status=AgentTask.STATUS_COMPLETED,
        context={
            'agent_service': 'ui',
            'require_case_save_confirmation': True,
            'case_save': {'status': 'pending'},
        },
        test_plan={'functional': ['登录成功'], 'abnormal': [], 'boundary': [], 'security': [], 'performance': []},
    )
    draft_step = task.steps.create(
        step=1,
        step_type='case',
        action='生成 UI 用例草稿',
        tool_name='generate_test_case',
        status=AgentStep.STATUS_SUCCESS,
    )
    task.steps.create(
        step=2,
        step_type='ui',
        action='执行 UI 登录步骤',
        tool_name='run_web_test',
        status=AgentStep.STATUS_SUCCESS,
        output_payload={'message': 'UI 登录验证通过'},
    )
    TestExecutionResult.objects.create(
        task=task,
        module='ui',
        status='PASSED',
        response_payload={'real_execution': True},
    )

    draft = agent_tools.create_case(task, draft_step, {})
    assert draft['requires_confirmation'] is True
    assert TestCase.objects.filter(project=project).count() == 0

    monkeypatch.setattr(
        'backend.agent.case_service._save_ui_automation_cases',
        lambda *_args, **_kwargs: [{
            'filename': 'test_agent_ui_login.yaml',
            'relative': 'cases/ui/test_agent_ui_login.yaml',
            'project_id': 'default',
            'name': '[Agent] UI 登录步骤',
            'source_case_file': 'cases/ui/login.yaml',
        }],
    )
    task.context = {
        **task.context,
        'ui_project_id': 'default',
        'case_files': ['cases/ui/login.yaml'],
    }
    task.save(update_fields=['context', 'updated_at'])

    client = APIClient()
    client.force_authenticate(user)
    first = client.post(f'/api/agent/tasks/{task.id}/save-test-case/', {}, format='json')
    second = client.post(f'/api/agent/tasks/{task.id}/save-test-case/', {}, format='json')

    assert first.status_code == 201
    assert first.data['created'] is True
    assert second.status_code == 200
    assert second.data['created'] is False
    assert TestCase.objects.filter(project=project).count() == 1
    task.refresh_from_db()
    assert task.context['case_save']['status'] == 'saved'
    assert task.context['case_save']['case_file'] == 'cases/ui/test_agent_ui_login.yaml'
    assert first.data['message'] == '已保存到 UI 自动化用例管理'


@pytest.mark.django_db
def test_app_agent_steps_are_saved_only_after_confirmation():
    user = get_user_model().objects.create_user(username='agent_app_case_confirm_user', password='x')
    app_project = AppProject.objects.create(name='Agent APP 保存项目', owner=user)
    package = AppPackage.objects.create(
        name='商城 APP',
        package_name='com.runnergo.mall.save',
        created_by=user,
    )
    source_case = AppTestCase.objects.create(
        project=app_project,
        name='已有登录用例',
        app_package=package,
        ui_flow=[{'type': 'touch', 'name': '点击登录'}],
        variables=[{'name': 'USERNAME', 'value': 'tester'}],
        created_by=user,
    )
    task = AgentTask.objects.create(
        task_name='APP 登录步骤',
        user_requirement='验证 APP 登录',
        created_by=user,
        status=AgentTask.STATUS_COMPLETED,
        context={
            'agent_service': 'app',
            'require_case_save_confirmation': True,
            'case_save': {'status': 'pending'},
            'app_project_id': app_project.id,
            'app_package_id': package.id,
            'app_case_ids': [source_case.id],
        },
    )
    task.steps.create(
        step=1,
        step_type='app',
        action='执行 APP 登录步骤',
        tool_name='run_app_test',
        status=AgentStep.STATUS_SUCCESS,
        output_payload={'message': 'APP 登录验证通过'},
    )
    TestExecutionResult.objects.create(
        task=task,
        module='app',
        status='PASSED',
        response_payload={'real_execution': True},
    )
    assert AppTestCase.objects.filter(project=app_project).count() == 1

    client = APIClient()
    client.force_authenticate(user)
    response = client.post(f'/api/agent/tasks/{task.id}/save-test-case/', {}, format='json')

    assert response.status_code == 201
    assert response.data['created'] is True
    saved = AppTestCase.objects.get(id=response.data['test_case']['id'])
    assert saved.project == app_project
    assert saved.app_package == package
    assert saved.ui_flow == source_case.ui_flow
    assert response.data['message'] == '已保存到 APP 自动化用例管理'
    assert AppTestCase.objects.filter(project=app_project).count() == 2

    case_list = client.get(
        f'/api/app-automation/test-cases/?project={app_project.id}&page_size=100'
    )
    assert case_list.status_code == 200
    assert saved.id in [item['id'] for item in case_list.data['results']]


@pytest.mark.django_db
def test_agent_memory_and_rag_knowledge_are_persistent():
    user = get_user_model().objects.create_user(username='agent_memory_user', password='x')
    service = AgentCapabilityService(user)

    memory = service.create_memory({
        'memory_type': 'api_knowledge',
        'role': 'user',
        'content': '商城系统登录接口: POST /api/login',
        'payload': {'flow': ['登录', '搜索', '购物车', '支付']},
    })
    document = service.upload_knowledge(
        title='商城订单PRD',
        document_type='prd',
        content='订单流程：登录 -> 搜索商品 -> 加入购物车 -> 支付。用户表 user。',
    )
    rag = service.rag_search(query='测试订单功能', limit=3)

    assert memory.id
    assert AgentMemory.objects.filter(memory_type='api_knowledge').exists()
    assert document.chunks_count >= 1
    assert AgentKnowledgeDocument.objects.filter(title='商城订单PRD').exists()
    assert rag['status'] == 'success'
    assert rag['hits']
    assert rag['test_strategy']['flow'] == ['登录', '搜索', '购物车', '支付']


@pytest.mark.django_db
def test_agent_generates_complete_asset_package_and_quality_score():
    user = get_user_model().objects.create_user(username='asset_agent_user', password='x')
    service = AgentCapabilityService(user)

    result = service.generate_assets(
        requirement='测试订单功能，覆盖登录、搜索、购物车、支付',
        base_url='https://example.com',
        save_assets=True,
    )
    score = service.quality_score(project_name='商城')

    assert result['status'] == 'success'
    assert result['created_assets']
    assert result['created_test_data_assets']
    assert result['assets']['scheduled_task']['cron'] == '0 9 * * *'
    assert TestAsset.objects.filter(task_id=result['task_id']).count() >= 3
    generated_asset = TestDataAsset.objects.latest('id')
    assert 'agent-assets' in generated_asset.tags
    assert score['status'] == 'success'
    assert {'功能覆盖', '接口覆盖', '安全风险', '稳定性'}.issubset(score['dimensions'])


@pytest.mark.django_db
def test_full_cycle_service_runs_agent_capability_closure(monkeypatch):
    user = get_user_model().objects.create_user(username='full_cycle_user', password='x')

    def fake_browser(self, **kwargs):
        return {
            'status': 'success',
            'message': 'fake browser vision ok',
            'opened': {'url': kwargs.get('url'), 'title': '商城'},
            'elements': [{'kind': 'input', 'name': '用户名'}, {'kind': 'button', 'name': '登录'}],
            'visual_summary': {'element_count': 2, 'action_count': 1},
        }

    monkeypatch.setattr('backend.browser_agent.browser_controller.WebVisionAgent.run', fake_browser)
    monkeypatch.setattr('backend.agent.tools._execute_direct_api_request', lambda payload: {
        'status': 'success',
        'message': 'fake api executed',
        'method': payload.get('method', 'GET'),
        'url': payload.get('url'),
        'status_code': 200,
        'elapsed_ms': 12,
        'response_json': {'ok': True},
    })
    result = AgentCapabilityService(user).run_full_cycle(
        requirement='测试商城登录、搜索、下单，全流程发现 Bug 并输出质量报告',
        frontend_url='https://example.com/login',
        api_url='https://example.com/api/orders',
        api_doc={
            'openapi': '3.0.0',
            'paths': {'/orders': {'post': {'requestBody': {'content': {'application/json': {'schema': {'type': 'object', 'properties': {'order_id': {'type': 'string'}}}}}}}}},
        },
        test_data_count=3,
        auto_execute=True,
    )

    task = result['task']
    task.refresh_from_db()
    tools = list(task.steps.order_by('step').values_list('tool_name', flat=True))

    assert result['status'] == 'needs_input'
    assert result['workflow']['task_id'] == task.id
    assert task.status == AgentTask.STATUS_NEEDS_INPUT
    assert 'analyze_requirement' in tools
    assert 'run_browser_agent' in tools
    assert 'run_web_test' in tools
    assert 'run_app_test' not in tools
    assert 'run_security_test' in tools
    assert 'self_heal_test' in tools
    assert 'rerun_fixed_test' in tools
    assert 'create_report' in tools
    assert task.execution_results.filter(module='browser', status='PASSED').exists()
    assert task.execution_results.filter(module='ui', status='SKIPPED').exists()
    assert not task.execution_results.filter(module='app').exists()
    assert not task.assets.filter(asset_type='report').exists()
    assert task.context['requirement_analysis']['decision'] == 'full_cycle'
    assert task.context['discovered_test_points']
    assert task.context['test_data_count'] == 3
    assert task.steps.get(tool_name='generate_test_data').input_payload['count'] == 3


def test_self_healing_suggests_new_locator_from_dom():
    service = AgentCapabilityService(user=None)
    result = service.self_heal(
        logs='Element not found: #login',
        old_locator='#login',
        new_dom='<button class="submit">登录</button>',
    )

    assert result['status'] == 'success'
    assert result['old_locator'] == '#login'
    assert result['new_locator'] == '.submit'
    assert result['approval_required'] is True
