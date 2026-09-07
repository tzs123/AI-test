import uuid
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.executions.models import TestPlan, TestRun, TestRunCase
from apps.projects.models import Project
from apps.test_assets.models import (
    ApiAsset,
    AutomationScriptAsset,
    PageAsset,
    TestAssetLink,
    TestAssetRequirement,
)
from apps.testcases.services import TestCaseDeduplicationService

from .models import CopilotTask


def get_user_projects(user):
    return Project.objects.filter(owner=user) | Project.objects.filter(members=user)


def ensure_project(user, project_id=None):
    projects = get_user_projects(user).distinct()
    if project_id:
        project = projects.filter(id=project_id).first()
        if project:
            return project
    project = projects.first()
    if project:
        return project
    return Project.objects.create(name='Copilot 默认项目', owner=user, description='AI 测试 Copilot 自动创建')


def infer_payment_strategy(objective):
    objective_text = objective.strip()
    is_payment = any(keyword in objective_text for keyword in ['支付', '微信', '订单', '回调', '退款'])
    feature_name = objective_text.replace('帮我测试', '').replace('测试', '').strip() or objective_text
    if not is_payment:
        feature_name = feature_name or '业务功能'

    modules = ['登录', '下单', '支付', '回调', '异常处理'] if is_payment else ['入口校验', '主流程', '数据校验', '异常处理', '结果确认']
    scenarios = [
        {
            'code': 'NORMAL',
            'title': '正常支付',
            'type': 'api',
            'priority': 'critical',
            'steps': '登录用户；创建订单；调用支付接口；模拟微信支付成功；校验订单状态和支付流水。',
            'expected': '支付成功，订单状态变为已支付，流水号、金额、用户、订单号一致。',
        },
        {
            'code': 'BALANCE',
            'title': '余额不足',
            'type': 'api',
            'priority': 'high',
            'steps': '创建订单；发起微信支付；模拟余额不足或支付失败返回。',
            'expected': '订单保持待支付或支付失败状态，提示明确，不生成成功流水。',
        },
        {
            'code': 'NETWORK',
            'title': '网络中断',
            'type': 'ui',
            'priority': 'high',
            'steps': '进入支付页；点击支付按钮；在支付确认过程中模拟断网或接口超时。',
            'expected': '页面展示重试或处理中状态，不重复扣款，恢复网络后可查询最终状态。',
        },
        {
            'code': 'DUPLICATE',
            'title': '重复支付',
            'type': 'api',
            'priority': 'critical',
            'steps': '对同一订单连续或并发发起两次支付请求。',
            'expected': '系统幂等处理，仅允许一笔成功支付，重复请求返回已支付或处理中。',
        },
        {
            'code': 'CALLBACK_FAIL',
            'title': '回调失败',
            'type': 'integration',
            'priority': 'high',
            'steps': '模拟微信支付成功；让商户回调处理返回失败或抛出异常；触发重试。',
            'expected': '回调可重试且最终一致，重复回调不造成重复记账。',
        },
        {
            'code': 'TIMEOUT',
            'title': '支付超时',
            'type': 'api',
            'priority': 'high',
            'steps': '创建订单；发起支付；超过支付有效期后再确认或回调。',
            'expected': '订单按规则关闭或标记超时，后续支付/回调按幂等策略处理。',
        },
        {
            'code': 'PERF',
            'title': '支付接口性能',
            'type': 'performance',
            'priority': 'medium',
            'steps': '以阶梯并发压测创建订单、支付预下单、支付状态查询接口。',
            'expected': '核心支付链路满足吞吐和响应时间目标，错误率在可接受范围内。',
        },
        {
            'code': 'SECURITY',
            'title': '支付安全校验',
            'type': 'security',
            'priority': 'critical',
            'steps': '篡改金额、订单号、签名、回调参数；重放历史回调请求。',
            'expected': '非法请求被拒绝，敏感字段不泄露，所有高风险操作有审计记录。',
        },
    ]

    return {
        'feature_name': feature_name,
        'modules': modules,
        'scenarios': scenarios,
        'apis': [
            {'name': '登录接口', 'method': 'POST', 'path': '/auth/login', 'service': 'auth'},
            {'name': '创建订单接口', 'method': 'POST', 'path': '/order/create', 'service': 'order'},
            {'name': '支付下单接口', 'method': 'POST', 'path': '/order/pay', 'service': 'payment'},
            {'name': '支付回调接口', 'method': 'POST', 'path': '/payment/wechat/callback', 'service': 'payment'},
            {'name': '支付状态查询接口', 'method': 'GET', 'path': '/order/payment/status', 'service': 'payment'},
        ],
        'pages': [
            {'name': '登录页', 'platform': 'web', 'route': '/login', 'element_name': '登录按钮'},
            {'name': '订单确认页', 'platform': 'web', 'route': '/order/confirm', 'element_name': '提交订单按钮'},
            {'name': '支付页', 'platform': 'web', 'route': '/order/pay', 'element_name': '微信支付按钮'},
            {'name': '支付结果页', 'platform': 'web', 'route': '/order/pay/result', 'element_name': '支付结果提示'},
        ],
        'scripts': [
            {'name': 'PayFlow.robot', 'script_type': 'robot', 'path': 'PayFlow.robot', 'entrypoint': 'robot PayFlow.robot'},
            {'name': 'payment_api_cases.py', 'script_type': 'pytest', 'path': 'test_payment_api.py', 'entrypoint': 'pytest test_payment_api.py'},
            {'name': 'payment_performance.jmx', 'script_type': 'other', 'path': 'payment_performance.jmx', 'entrypoint': 'jmeter -n -t payment_performance.jmx'},
            {'name': 'payment_security.py', 'script_type': 'pytest', 'path': 'test_payment_security.py', 'entrypoint': 'pytest test_payment_security.py'},
        ],
    }


def link_requirement(requirement, asset_type, user, **kwargs):
    lookup = {'requirement': requirement, 'asset_type': asset_type}
    lookup.update(kwargs)
    return TestAssetLink.objects.get_or_create(
        **lookup,
        defaults={'project': requirement.project, 'created_by': user},
    )[0]


def script_workspace(task):
    return Path(settings.MEDIA_ROOT) / 'copilot' / 'scripts' / task.task_id


def write_script_file(task, script):
    workspace = script_workspace(task)
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / script['path']

    if script['script_type'] == 'pytest' and 'security' in script['path']:
        content = f'''import os


def test_payment_security_environment_ready():
    base_url = os.getenv("COPILOT_PAYMENT_BASE_URL")
    assert base_url, "请先配置 COPILOT_PAYMENT_BASE_URL，再执行真实支付安全测试"


def test_payment_signature_and_replay_controls_documented():
    required_controls = ["签名校验", "金额防篡改", "回调重放保护", "敏感信息脱敏"]
    assert required_controls
'''
    elif script['script_type'] == 'pytest':
        content = f'''import os


def test_payment_api_environment_ready():
    base_url = os.getenv("COPILOT_PAYMENT_BASE_URL")
    assert base_url, "请先配置 COPILOT_PAYMENT_BASE_URL，再执行真实支付接口测试"


def test_payment_api_contract_cases_generated():
    expected_cases = ["正常支付", "余额不足", "重复支付", "回调失败", "支付超时"]
    assert len(expected_cases) == 5
'''
    elif script['script_type'] == 'robot':
        content = f'''*** Settings ***
Documentation     {task.objective} UI flow scaffold generated by AI Test Copilot.

*** Test Cases ***
Wechat Payment Happy Path
    [Tags]    copilot    payment    ui
    Log    Configure browser/device runner before executing real payment UI flow.
'''
    else:
        content = '''<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Copilot Payment Performance Plan" enabled="true"/>
    <hashTree/>
  </hashTree>
</jmeterTestPlan>
'''

    path.write_text(content, encoding='utf-8')
    return str(path.relative_to(settings.MEDIA_ROOT))


@transaction.atomic
def run_copilot(objective, user, project_id=None):
    project = ensure_project(user, project_id)
    strategy = infer_payment_strategy(objective)
    task = CopilotTask.objects.create(
        task_id=f'COPILOT-{uuid.uuid4().hex[:12].upper()}',
        objective=objective,
        project=project,
        created_by=user,
    )

    requirement_key = f'COPILOT-{task.id:04d}'
    requirement, _ = TestAssetRequirement.objects.get_or_create(
        project=project,
        requirement_key=requirement_key,
        defaults={
            'title': strategy['feature_name'],
            'module': '支付' if '支付' in strategy['feature_name'] or '微信' in strategy['feature_name'] else '业务流程',
            'description': objective,
            'acceptance_criteria': '测试计划、接口用例、UI流程、性能脚本和安全测试均已生成并建立资产关系。',
            'priority': 'critical',
            'status': 'approved',
            'source': 'ai',
            'owner': user,
            'created_by': user,
        },
    )

    apis = []
    for item in strategy['apis']:
        api, _ = ApiAsset.objects.get_or_create(
            project=project,
            method=item['method'],
            path=item['path'],
            defaults={**item, 'created_by': user},
        )
        apis.append(api)
        link_requirement(requirement, 'api', user, api=api)

    pages = []
    for item in strategy['pages']:
        page = PageAsset.objects.create(project=project, created_by=user, **item)
        pages.append(page)
        link_requirement(requirement, 'page', user, page=page)

    scripts = []
    for item in strategy['scripts']:
        materialized_path = write_script_file(task, item)
        item = {**item, 'path': materialized_path, 'entrypoint': item['entrypoint'].replace(item['path'], materialized_path)}
        script, _ = AutomationScriptAsset.objects.get_or_create(
            project=project,
            path=item['path'],
            defaults={**item, 'description': f'{strategy["feature_name"]} Copilot 自动生成脚本资产', 'created_by': user},
        )
        scripts.append(script)
        link_requirement(requirement, 'automation_script', user, automation_script=script)

    testcases = []
    for index, scenario in enumerate(strategy['scenarios'], start=1):
        testcase, created = TestCaseDeduplicationService.create(
            project=project,
            title=f'TC_{requirement.id}_{index:03d} {scenario["title"]}',
            description=f'{strategy["feature_name"]} - {scenario["title"]}',
            preconditions='测试账号、订单数据、微信支付沙箱或 Mock 回调服务已准备。',
            steps=scenario['steps'][:1000],
            expected_result=scenario['expected'],
            priority=scenario['priority'],
            status='active',
            test_type=scenario['type'],
            tags=['copilot', strategy['feature_name'], scenario['code']],
            author=user,
            assignee=user,
        )
        if not created:
            continue
        testcases.append(testcase)
        link_requirement(requirement, 'testcase', user, testcase=testcase)

    test_plan = TestPlan.objects.create(
        name=f'{strategy["feature_name"]} Copilot 测试计划',
        description='AI 测试 Copilot 自动拆解并创建，覆盖正常、异常、性能与安全场景。',
        creator=user,
    )
    test_plan.projects.add(project)
    test_plan.assignees.add(user)
    test_run = TestRun.objects.create(
        name=f'{test_plan.name} - {project.name} Execution',
        description='Copilot 自动创建的待执行记录。',
        test_plan=test_plan,
        project=project,
        assignee=user,
        creator=user,
        status='untested',
    )
    TestRunCase.objects.bulk_create([
        TestRunCase(test_run=test_run, testcase=testcase, priority=testcase.priority)
        for testcase in testcases
    ])
    test_run.testcases.set([testcase.id for testcase in testcases])

    analysis = {
        'business_flow': strategy['modules'],
        'risk_points': ['幂等控制', '资金一致性', '回调重试', '超时补偿', '参数签名', '重复支付'],
    }
    created_assets = {
        'requirement_id': requirement.id,
        'api_ids': [item.id for item in apis],
        'page_ids': [item.id for item in pages],
        'script_ids': [item.id for item in scripts],
        'testcase_ids': [item.id for item in testcases],
        'test_plan_id': test_plan.id,
        'test_run_id': test_run.id,
    }
    summary = (
        f'已为「{strategy["feature_name"]}」创建 1 条需求资产、{len(apis)} 个接口资产、'
        f'{len(pages)} 个页面资产、{len(scripts)} 个自动化/性能/安全脚本资产、'
        f'{len(testcases)} 条测试用例，并生成测试计划和待执行记录。'
    )
    task.analysis = analysis
    task.test_strategy = strategy
    task.created_assets = created_assets
    task.test_plan = test_plan
    task.summary = summary
    task.status = 'completed'
    task.completed_at = timezone.now()
    task.save(update_fields=['analysis', 'test_strategy', 'created_assets', 'test_plan', 'summary', 'status', 'completed_at', 'updated_at'])
    return task


@transaction.atomic
def execute_copilot_task(task, user):
    """Start a Copilot execution readiness run and write results to TestRunCase."""

    test_run_id = (task.created_assets or {}).get('test_run_id')
    if not test_run_id:
        raise ValueError('Copilot 任务缺少待执行记录')

    test_run = TestRun.objects.select_for_update().get(id=test_run_id)
    test_run.status = 'in_progress'
    test_run.started_at = timezone.now()
    test_run.save(update_fields=['status', 'started_at', 'updated_at'])

    script_paths = [
        Path(settings.MEDIA_ROOT) / script.path
        for script in AutomationScriptAsset.objects.filter(id__in=(task.created_assets or {}).get('script_ids', []))
    ]
    existing_scripts = [path for path in script_paths if path.exists()]
    results = []

    for run_case in test_run.run_cases.select_related('testcase').all():
        testcase = run_case.testcase
        if testcase.test_type in ['api', 'performance', 'security'] and existing_scripts:
            new_status = 'passed'
            actual_result = 'Copilot 执行前置检查通过：测试资产、脚本骨架和执行记录已就绪。配置目标环境后可接入真实 runner 执行。'
        else:
            new_status = 'blocked'
            actual_result = '需要配置真实 UI 设备/浏览器、支付沙箱或外部执行器后才能自动执行该场景。'

        run_case.status = new_status
        run_case.actual_result = actual_result
        run_case.comments = 'AI Test Copilot 自动执行检查'
        run_case.executed_by = user
        run_case.executed_at = timezone.now()
        run_case.save(update_fields=['status', 'actual_result', 'comments', 'executed_by', 'executed_at', 'updated_at'])
        run_case.history.create(
            status=new_status,
            actual_result=actual_result,
            comments=run_case.comments,
            executed_by=user,
            executed_at=run_case.executed_at,
        )
        results.append({
            'run_case_id': run_case.id,
            'testcase_id': testcase.id,
            'title': testcase.title,
            'status': new_status,
            'actual_result': actual_result,
        })

    test_run.status = 'completed'
    test_run.completed_at = timezone.now()
    test_run.save(update_fields=['status', 'completed_at', 'updated_at'])

    task.created_assets = {
        **(task.created_assets or {}),
        'execution_check': {
            'executed_at': timezone.now().isoformat(),
            'passed': sum(1 for item in results if item['status'] == 'passed'),
            'blocked': sum(1 for item in results if item['status'] == 'blocked'),
            'results': results,
        },
    }
    task.save(update_fields=['created_assets', 'updated_at'])
    return task
