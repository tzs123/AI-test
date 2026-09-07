from __future__ import annotations

import os
import time
import re
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, List
from urllib.parse import urljoin, urlsplit

from django.utils import timezone

from apps.agent.models import AgentStep, AgentTask, TestAsset, TestExecutionResult
from apps.data_factory.models import DataFactoryRecord
from apps.data_factory.tools.random_tools import RandomTools
from apps.data_factory.tools.test_data_tools import TestDataTools
from apps.testcases.models import TestCase, TestCaseStep
from apps.testcases.services import TestCaseDeduplicationService
from apps.core.data_assets import create_or_update_asset_with_bindings
from apps.core.models import TestDataAsset, TestDataAssetRequirement

from .analyzer import FailureAnalyzer
from .url_utils import infer_http_url


def _current_execution_results(task: AgentTask):
    results = task.execution_results.all()
    if task.started_at:
        results = results.filter(created_at__gte=task.started_at)
    return results


def tool_catalog() -> Dict[str, Dict[str, str]]:
    return {
        'analyze_requirement': {'name': 'analyze_requirement', 'module': 'AI需求分析', 'description': '识别业务需求、URL、接口目标、APP目标和执行策略'},
        'discover_test_points': {'name': 'discover_test_points', 'module': 'AI测试点发现', 'description': '从测试方案中抽取功能、异常、边界、安全和性能测试点'},
        'generate_data': {'name': 'generate_data', 'module': '测试数据中心', 'description': '生成结构化测试数据资产'},
        'create_case': {'name': 'create_case', 'module': 'AI用例生成', 'description': '根据测试计划生成平台测试用例'},
        'run_api_test': {'name': 'run_api_test', 'module': 'API测试', 'description': '执行或登记 API 测试结果'},
        'run_ui_test': {'name': 'run_ui_test', 'module': 'UI自动化', 'description': '执行或登记 UI 自动化结果'},
        'run_app_test': {'name': 'run_app_test', 'module': 'APP自动化', 'description': '执行或登记 APP 自动化结果'},
        'run_performance_test': {'name': 'run_performance_test', 'module': '性能测试', 'description': '执行或登记性能测试结果'},
        'analyze_failure': {'name': 'analyze_failure', 'module': 'AI结果分析', 'description': '分析日志、截图、响应并输出归因建议'},
        'self_heal_test': {'name': 'self_heal_test', 'module': 'AI修复测试', 'description': '生成定位器、数据和步骤自愈建议'},
        'rerun_fixed_test': {'name': 'rerun_fixed_test', 'module': 'AI复跑验证', 'description': '登记修复后复跑验证结果'},
        'create_report': {'name': 'create_report', 'module': '测试报告', 'description': '生成 Agent 测试报告'},
    }


def analyze_requirement(task: AgentTask, step: AgentStep, payload: Dict[str, Any]) -> Dict[str, Any]:
    context = task.context or {}
    requirement = str(payload.get('requirement') or task.user_requirement or '').strip()
    url = _first_value(payload, context, keys=['url', 'target', 'base_url', 'frontend_url', 'api_url']) or _infer_url(requirement)
    api_doc = context.get('api_doc') or payload.get('api_doc') or {}
    target_type = 'web'
    if context.get('app_project') or context.get('device') or any(word in requirement.lower() for word in ['app', 'android', 'ios', '移动端']):
        target_type = 'app'
    if context.get('api_url') or '接口' in requirement or re.search(r'\b(GET|POST|PUT|PATCH|DELETE)\s+/', requirement, re.I):
        target_type = 'api'
    if url and target_type == 'api' and not context.get('api_url'):
        context['api_url'] = url
    if url and target_type != 'api' and not context.get('url'):
        context['url'] = url
    context['requirement_analysis'] = {
        'target_type': target_type,
        'has_frontend_url': bool(context.get('url') or context.get('frontend_url')),
        'has_api_target': bool(context.get('api_url') or api_doc),
        'has_app_context': bool(context.get('app_project') or context.get('device') or context.get('app_package')),
        'decision': 'full_cycle',
    }
    task.context = context
    task.save(update_fields=['context', 'updated_at'])
    return {
        'status': 'success',
        'message': 'AI 已完成需求分析，并决定进入全能力闭环编排。',
        'analysis': context['requirement_analysis'],
        'inputs': {
            'requirement': requirement,
            'url': url,
            'api_doc_provided': bool(api_doc),
        },
        'decision': {
            'run_browser_vision': bool(context.get('url') or context.get('frontend_url')),
            'run_api_data': True,
            'run_appium': True,
            'run_yakit_security': True,
            'run_failure_analysis': True,
            'run_self_heal_and_rerun': True,
        },
    }


def discover_test_points(task: AgentTask, step: AgentStep, payload: Dict[str, Any]) -> Dict[str, Any]:
    plan = task.test_plan or {}
    points = []
    for key, label in [
        ('functional', '功能'),
        ('abnormal', '异常'),
        ('boundary', '边界'),
        ('security', '安全'),
        ('performance', '性能'),
    ]:
        for item in plan.get(key, []):
            points.append({'category': label, 'title': item, 'source': key})
    if not points:
        subject = (payload.get('requirement') or task.user_requirement or '目标功能')[:40]
        points = [
            {'category': '功能', 'title': f'{subject}主流程完成', 'source': 'fallback'},
            {'category': '异常', 'title': '异常输入和依赖失败可被识别', 'source': 'fallback'},
            {'category': '安全', 'title': '鉴权、注入和敏感信息风险可被扫描', 'source': 'fallback'},
        ]
    task.context = {
        **(task.context or {}),
        'discovered_test_points': points,
    }
    task.save(update_fields=['context', 'updated_at'])
    return {
        'status': 'success',
        'message': f'AI 已发现 {len(points)} 个测试点。',
        'test_points': points,
        'coverage': {
            'functional': len(plan.get('functional', [])),
            'abnormal': len(plan.get('abnormal', [])),
            'boundary': len(plan.get('boundary', [])),
            'security': len(plan.get('security', [])),
            'performance': len(plan.get('performance', [])),
        },
    }


def _stable_target_case_id(target_id: Any) -> int:
    text = str(target_id or '').strip()
    if text.isdigit() and 0 < int(text) <= 0x7FFFFFFF:
        return int(text)
    return (zlib.crc32(text.encode('utf-8')) & 0x7FFFFFFF) or 1


def _task_scoped_asset_id(prefix: str, task_id: int, source_id: Any) -> str:
    raw = f'{prefix}_{task_id}_{source_id}'
    if len(raw) <= 80:
        return raw
    digest = f'{zlib.crc32(raw.encode("utf-8")) & 0xFFFFFFFF:08x}'
    return f'{raw[:71]}_{digest}'


def _generated_data_binding_targets(
    task: AgentTask,
    fields: List[str],
) -> List[Dict[str, Any]]:
    targets: List[Dict[str, Any]] = []
    references = _resolve_api_test_object_refs(task, {})
    for index, reference in enumerate(references, 1):
        target_id = reference['target_id']
        target_case_id = _stable_target_case_id(target_id)
        alias = re.sub(r'[^A-Za-z0-9_]', '_', f'agent_api_{task.id}_{index}')[:100]
        binding_tag = f'agent-bind-api-{task.id}-{target_case_id}'
        targets.append({
            'source_target_id': target_id,
            'target_type': 'api_automation',
            'target_case_id': target_case_id,
            'target_case_name': reference.get('name') or f'API对象 {target_id}',
            'alias': alias,
            'binding_tag': binding_tag,
            'fields': list(fields),
        })

    ui_cases = _resolve_ui_test_cases(task, {})
    for index, case in enumerate(ui_cases, 1):
        alias = re.sub(r'[^A-Za-z0-9_]', '_', f'agent_ui_{task.id}_{index}')[:100]
        targets.append({
            'source_target_id': str(case.id),
            'target_type': 'ui_automation',
            'target_case_id': case.id,
            'target_case_name': case.title,
            'alias': alias,
            'binding_tag': f'agent-bind-ui-{task.id}-{case.id}',
            'fields': list(fields),
        })

    ui_case_files = _resolve_ui_case_files(task, {})
    for index, case_file in enumerate(ui_case_files, 1):
        target_case_id = _stable_target_case_id(case_file)
        alias = re.sub(r'[^A-Za-z0-9_]', '_', f'agent_ui_yaml_{task.id}_{index}')[:100]
        targets.append({
            'source_target_id': case_file,
            'target_type': 'ui_automation',
            'target_case_id': target_case_id,
            'target_case_name': case_file,
            'alias': alias,
            'binding_tag': f'agent-bind-ui-yaml-{task.id}-{target_case_id}',
            'fields': list(fields),
        })

    if not ui_cases and not ui_case_files:
        context = task.context or {}
        target_url = _first_value(context, keys=['url', 'frontend_url', 'target', 'base_url']) \
            or _infer_url(task.user_requirement)
        if target_url:
            target_case_id = _stable_target_case_id(target_url)
            targets.append({
                'source_target_id': target_url,
                'target_type': 'ui_automation',
                'target_case_id': target_case_id,
                'target_case_name': target_url,
                'alias': f'agent_ui_url_{task.id}',
                'binding_tag': f'agent-bind-ui-url-{task.id}-{target_case_id}',
                'fields': list(fields),
            })

    app_cases = _resolve_app_test_cases(task, {})
    for index, case in enumerate(app_cases, 1):
        alias = re.sub(r'[^A-Za-z0-9_]', '_', f'agent_data_app_{task.id}_{index}')[:100]
        targets.append({
            'source_target_id': str(case.id),
            'target_type': 'app_automation',
            'target_case_id': case.id,
            'target_case_name': case.name,
            'alias': alias,
            'binding_tag': f'agent-bind-app-{task.id}-{case.id}',
            'fields': list(fields),
        })
    return targets


def _bind_generated_data(
    task: AgentTask,
    data_asset: TestDataAsset,
    targets: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    binding_payloads = [{
        'target_type': item['target_type'],
        'target_case_id': item['target_case_id'],
        'target_case_name': item['target_case_name'],
        'alias': item['alias'],
        'asset_type': data_asset.asset_type,
        'quantity': 1,
        'filters': {},
        'tags': [item['binding_tag']],
        'release_policy': 'release',
        'is_active': True,
    } for item in targets]
    _, requirements = create_or_update_asset_with_bindings(
        asset_values={
            'tags': list(dict.fromkeys([
                *(data_asset.tags or []),
                *(item['binding_tag'] for item in targets),
            ])),
        },
        bindings=binding_payloads,
        user=task.created_by,
        asset=data_asset,
    )
    return [{
        'target_id': target['source_target_id'],
        'target_type': target['target_type'],
        'target_case_id': target['target_case_id'],
        'target_case_name': requirement.target_case_name,
        'requirement_id': requirement.id,
        'asset_id': data_asset.id,
        'alias': requirement.alias,
        'fields': target['fields'],
        'binding_tag': target['binding_tag'],
    } for target, requirement in zip(targets, requirements)]


def generate_test_data(task: AgentTask, step: AgentStep, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        count = min(max(int(payload.get('count') or 1), 1), 10000)
    except (TypeError, ValueError):
        count = 100
    fields = payload.get('fields') or ['name', 'phone', 'email', 'password']
    if not isinstance(fields, list):
        fields = ['name', 'phone', 'email', 'password']
    records = []
    for index in range(count):
        item: Dict[str, Any] = {}
        if 'name' in fields:
            item['name'] = _single_result(TestDataTools.generate_chinese_name())
        if 'phone' in fields:
            item['phone'] = _single_result(TestDataTools.generate_chinese_phone())
        if 'email' in fields:
            item['email'] = _single_result(TestDataTools.generate_chinese_email())
        if 'password' in fields:
            item['password'] = RandomTools.random_password(length=12).get('result', f'P@ssword{index:04d}')
        if 'address' in fields:
            item['address'] = _single_result(TestDataTools.generate_chinese_address())
        if 'id_card' in fields:
            item['id_card'] = _single_result(TestDataTools.generate_id_card())
        records.append(item)

    asset_id = f'data_{timezone.now().strftime("%Y%m%d%H%M%S")}_{step.id}'
    data_record = DataFactoryRecord.objects.create(
        user=task.created_by,
        tool_name='agent_generate_data',
        tool_category='test_data',
        tool_scenario='test_data',
        input_data={'type': payload.get('type', 'USER'), 'count': count, 'fields': fields},
        output_data={'asset_id': asset_id, 'count': count, 'rows': records},
        is_saved=True,
        tags=['AI Agent', str(payload.get('type', 'USER')).upper()],
    )
    asset = TestAsset.objects.create(
        asset_id=asset_id,
        asset_type='data',
        name=f'{payload.get("type", "USER")} 测试数据 {count} 条',
        task=task,
        project=task.project,
        source_model='apps.data_factory.DataFactoryRecord',
        source_id=str(data_record.id),
        metadata={'type': payload.get('type', 'USER'), 'count': count, 'fields': fields, 'preview': records[:10]},
        created_by=task.created_by,
    )
    binding_targets = _generated_data_binding_targets(task, fields)
    data_asset, _ = create_or_update_asset_with_bindings(
        asset_values={
            'asset_type': str(payload.get('type') or 'CUSTOM').upper(),
            'name': f'{payload.get("type", "USER")} Agent 参数化数据 {count} 条',
            'status': TestDataAsset.STATUS_AVAILABLE,
            'payload': records,
            'tags': ['ai-generated', 'unified-test-agent', f'agent-task-{task.id}'],
        },
        bindings=[],
        user=task.created_by,
    )
    data_bindings = _bind_generated_data(task, data_asset, binding_targets)
    api_bindings = [item for item in data_bindings if item['target_type'] == 'api_automation']
    task.context = {
        **(task.context or {}),
        'generated_test_data': {
            'asset_id': data_asset.id,
            'count': count,
            'fields': fields,
            'bindings': data_bindings,
            'api_bindings': api_bindings,
        },
    }
    task.save(update_fields=['context', 'updated_at'])
    return {
        'asset_id': asset.asset_id,
        'test_data_asset_id': data_asset.id,
        'record_id': data_record.id,
        'count': count,
        'status': 'success',
        'message': f'已生成 {count} 条测试数据并关联 {len(data_bindings)} 个目标用例。',
        'preview': records[:10],
        'bindings': data_bindings,
        'api_bindings': api_bindings,
    }


def create_case(task: AgentTask, step: AgentStep, payload: Dict[str, Any]) -> Dict[str, Any]:
    if (task.context or {}).get('require_case_save_confirmation'):
        plan = task.test_plan if isinstance(task.test_plan, dict) else {}
        draft = {
            'name': f'{task.task_name}-Agent执行步骤'[:200],
            'description': task.user_requirement,
            'sections': {
                key: list(plan.get(key) or [])
                for key in ('functional', 'abnormal', 'boundary', 'security', 'performance')
            },
            'status': 'pending_confirmation',
        }
        task.context = {
            **(task.context or {}),
            'generated_case_draft': draft,
            'case_save': {'status': 'pending'},
        }
        task.save(update_fields=['context', 'updated_at'])
        return {
            'status': 'success',
            'message': '测试用例草稿已生成；Agent 执行结束后将询问是否保存，不会自动写入用例库。',
            'draft': draft,
            'requires_confirmation': True,
        }
    if not task.project:
        return {'status': 'skipped', 'message': '未关联项目，仅生成测试计划，不创建平台用例'}
    case_ids: List[int] = []
    created_count = 0
    reused_count = 0
    plan = task.test_plan or {}
    case_rows = []
    for category, test_type in [
        ('functional', 'functional'),
        ('abnormal', 'functional'),
        ('boundary', 'functional'),
        ('security', 'security'),
        ('performance', 'performance'),
    ]:
        for title in plan.get(category, [])[:3]:
            case_rows.append((category, test_type, title))
    for category, test_type, title in case_rows:
        normalized_title = TestCaseDeduplicationService.normalize_title(title)
        case, created = TestCaseDeduplicationService.create(
            project=task.project,
            title=f'[Agent]{normalized_title}'[:500],
            description=f'来源需求：{task.user_requirement}',
            preconditions='由 AI Test Agent 自动生成，请结合真实环境补充前置数据。',
            steps='; '.join([
                '准备测试数据',
                '执行目标操作或接口请求',
                '校验响应、页面状态和业务数据',
            ]),
            expected_result='结果符合测试计划预期，无异常错误。',
            priority='high' if category in {'security', 'performance'} else 'medium',
            status='draft',
            test_type=test_type,
            tags=['AI Agent', category],
            author=task.created_by,
        )
        if created:
            TestCaseStep.objects.bulk_create([
                TestCaseStep(
                    testcase=case,
                    step_number=1,
                    action='准备测试数据和环境',
                    expected='数据可用，环境状态正确',
                ),
                TestCaseStep(
                    testcase=case,
                    step_number=2,
                    action=normalized_title,
                    expected='功能行为符合预期',
                ),
            ])
            created_count += 1
        else:
            reused_count += 1
        if case.id in case_ids:
            continue
        case_ids.append(case.id)
        _link_asset(
            task,
            'case',
            f'agent_{task.id}_case_{case.id}',
            case.title,
            'apps.testcases.TestCase',
            case.id,
            {
                'category': category,
                'test_type': test_type,
                'reused': not created,
            },
        )
    return {
        'status': 'success',
        'message': f'已关联 {len(case_ids)} 条测试用例，新建 {created_count} 条，复用 {reused_count} 条。',
        'case_ids': case_ids,
        'count': len(case_ids),
        'created_count': created_count,
        'reused_count': reused_count,
    }


def run_api_test(task: AgentTask, step: AgentStep, payload: Dict[str, Any]) -> Dict[str, Any]:
    references = _resolve_api_test_object_refs(task, payload)
    if references:
        generated = (task.context or {}).get('generated_test_data') or {}
        bindings = generated.get('api_bindings') or []
        runtime_bindings = []
        for reference in references:
            binding = next(
                (item for item in bindings if str(item.get('target_id')) == str(reference.get('target_id'))),
                None,
            )
            if binding:
                runtime_bindings.append(binding)
        request_payload = {
            'test_object_refs': references,
            'data_bindings': runtime_bindings,
            'runtime_variables': {
                field: f"${{dataAssets.{binding['alias']}.{field}}}"
                for binding in runtime_bindings
                for field in binding.get('fields') or []
            },
            **(payload or {}),
        }
        generated_asset = None
        asset_id = generated.get('asset_id')
        if asset_id:
            generated_asset = TestDataAsset.objects.filter(id=asset_id).first()
        rows = generated_asset.payload if generated_asset and isinstance(generated_asset.payload, list) else []
        executable_results = []
        for reference in references:
            raw_url = str(reference.get('url') or reference.get('path') or '').strip()
            base_url = str(
                reference.get('base_url')
                or (task.context or {}).get('api_base_url')
                or (task.context or {}).get('base_url')
                or (task.context or {}).get('api_url')
                or ''
            ).strip()
            if not raw_url or (not urlsplit(raw_url).scheme and not base_url):
                continue
            for row_index, row in enumerate(rows or [{}], 1):
                method = str(reference.get('method') or 'GET').upper()
                direct_payload = {
                    'method': method,
                    'url': raw_url,
                    'base_url': base_url,
                    'headers': reference.get('headers') or {},
                    'body': row if method not in {'GET', 'HEAD', 'OPTIONS'} else None,
                    'timeout': int(payload.get('timeout') or 15),
                }
                executable_results.append({
                    'target_id': reference['target_id'],
                    'row': row_index,
                    **_execute_direct_api_request(direct_payload),
                })
        if executable_results:
            started = timezone.now()
            failed = [item for item in executable_results if item.get('status') != 'success']
            status = 'FAILED' if failed else 'PASSED'
            execution = _persist_execution_result(
                task,
                step,
                'api',
                request_payload,
                status,
                f'API 参数化执行完成：{len(executable_results)} 轮，失败 {len(failed)} 轮',
                {'results': executable_results, 'data_bindings': runtime_bindings},
                started,
            )
            return {
                'status': 'failed' if failed else 'success',
                'execution_result_id': execution.id,
                'real_execution': True,
                'total': len(executable_results),
                'failed': len(failed),
                'data_bindings': runtime_bindings,
            }
        return _record_execution(
            task,
            step,
            'api',
            request_payload,
            'SKIPPED',
            '已将参数化测试数据绑定到 API 测试对象；对象未提供可执行环境地址，等待 RunnerGo API 执行器接管真实发送',
        )
    http_payload = _resolve_direct_api_request(task, payload)
    if http_payload.get('url') and payload.get('allow_direct_api') is True:
        started = timezone.now()
        result = _execute_direct_api_request(http_payload)
        if result.get('status') == 'skipped':
            status = 'SKIPPED'
        else:
            status = 'PASSED' if result.get('status') == 'success' else 'FAILED'
        execution = _persist_execution_result(
            task,
            step,
            'api',
            payload,
            status,
            result.get('message') or result.get('error') or 'API 测试执行完成',
            result,
            started,
        )
        return {**result, 'execution_result_id': execution.id, 'real_execution': status != 'SKIPPED'}
    return _record_execution(
        task,
        step,
        'api',
        payload,
        'SKIPPED',
        '未绑定 API 测试模块的测试对象；请在统一编排上下文传入 test_object_refs/api_test_objects 后再执行真实 API 测试',
    )


def run_ui_test(task: AgentTask, step: AgentStep, payload: Dict[str, Any]) -> Dict[str, Any]:
    ui_cases = _resolve_ui_test_cases(task, payload)
    if ui_cases:
        return _run_ui_test_case_refs(task, step, payload, ui_cases)

    case_files = _resolve_ui_case_files(task, payload)
    if case_files:
        return _run_ui_yaml_cases(task, step, payload, case_files)

    target_url = _first_value(payload, task.context or {}, keys=['url', 'target', 'base_url']) or _infer_url(task.user_requirement)
    if not target_url:
        return _record_execution(
            task,
            step,
            'ui',
            payload,
            'SKIPPED',
            '未绑定 UI 自动化 YAML 用例，也未提供浏览器探索 URL，UI 自动化真实执行跳过',
        )

    message = (
        '页面探索只能证明 URL 可访问，不能证明业务功能通过。'
        '请在“引用来源”中选择可执行的 UI 自动化 YAML 用例后重新创建任务。'
    )
    return _record_execution(
        task,
        step,
        'ui',
        payload,
        'SKIPPED',
        message,
    )


def run_app_test(task: AgentTask, step: AgentStep, payload: Dict[str, Any]) -> Dict[str, Any]:
    bridge = _build_app_agent_bridge(task, payload)
    if bridge.get('status') == 'skipped':
        return _record_execution(task, step, 'app', payload, 'SKIPPED', bridge['message'])

    from apps.app_automation.tasks import execute_app_agent_task

    app_task = bridge['task']
    started = timezone.now()
    try:
        execute_app_agent_task.run(app_task.id, execute_only=True)
        app_task.refresh_from_db()
    except Exception as exc:
        app_task.refresh_from_db()
        result = {
            'status': 'failed',
            'message': f'APP Agent 真实执行异常：{exc}',
            'app_agent_task_id': app_task.id,
            'error': str(exc),
        }
        execution = _persist_execution_result(
            task, step, 'app', payload, 'FAILED', result['message'], result, started
        )
        return {**result, 'execution_result_id': execution.id, 'real_execution': False}

    summary = app_task.result_summary or {}
    from apps.app_automation.models import AppTestExecution
    from apps.app_automation.report_urls import build_internal_report_path, build_public_report_path

    app_executions = list(AppTestExecution.objects.filter(id__in=app_task.execution_ids or []))
    app_reports = [
        {
            'execution_id': execution.id,
            'case_id': execution.test_case_id,
            'report_url': build_internal_report_path(execution.id),
            'public_report_url': build_public_report_path(execution.id),
        }
        for execution in app_executions
        if execution.report_path
    ]
    failed = int(summary.get('failed') or 0)
    stopped = int(summary.get('stopped') or 0)
    total = int(summary.get('total') or 0)
    coverage_gap = bool(summary.get('coverage_gap') or app_task.plan.get('coverage_gap'))
    if app_task.status == 'failed' or failed:
        status = 'FAILED'
    elif coverage_gap or stopped or not total:
        status = 'SKIPPED'
    else:
        status = 'PASSED'

    message = (
        app_task.error_message or
        app_task.plan.get('coverage_message') or
        f"APP Agent 执行完成：total={total}, passed={summary.get('passed', 0)}, failed={failed}, stopped={stopped}"
    )
    generated_bindings = (task.context or {}).get('generated_test_data', {}).get('bindings') or []
    app_bindings = [item for item in generated_bindings if item.get('target_type') == 'app_automation']
    result = {
        'status': status.lower(),
        'message': message,
        'app_agent_task_id': app_task.id,
        'app_agent_status': app_task.status,
        'result_summary': summary,
        'plan': app_task.plan,
        'execution_ids': app_task.execution_ids,
        'reports': app_reports,
        'report_url': app_reports[0]['report_url'] if app_reports else '',
        'data_bindings': app_bindings,
        'real_execution': status != 'SKIPPED',
    }
    execution = _persist_execution_result(
        task,
        step,
        'app',
        {'data_bindings': app_bindings, **(payload or {})},
        status,
        message,
        result,
        started,
        report_url=result['report_url'],
    )
    _link_asset(
        task,
        'app',
        f'app_agent_{app_task.id}',
        f'APP Agent 任务 #{app_task.id}',
        'apps.app_automation.AppAgentTask',
        app_task.id,
        {'status': app_task.status, 'summary': summary},
    )
    return {**result, 'execution_result_id': execution.id}


def run_performance_test(task: AgentTask, step: AgentStep, payload: Dict[str, Any]) -> Dict[str, Any]:
    message = '未绑定真实性能测试场景或压测计划，已登记待执行项；请关联性能资产后再执行真实压测'
    return _record_execution(
        task,
        step,
        'performance',
        payload or {},
        'SKIPPED',
        message,
    )


def analyze_failure(task: AgentTask, step: AgentStep, payload: Dict[str, Any]) -> Dict[str, Any]:
    failed_results = _current_execution_results(task).filter(status='FAILED').order_by('-created_at')
    if not failed_results.exists():
        result = {
            'status': 'not_applicable',
            'reason': '当前执行未发现失败结果',
            'level': 'LOW',
            'suggestion': '当前执行已通过，无需失败归因',
        }
    else:
        latest = failed_results.first()
        result = FailureAnalyzer().analyze(
            logs=latest.logs,
            screenshot=latest.screenshot,
            response=latest.response_payload,
        )
    task.failure_analysis = result
    task.save(update_fields=['failure_analysis', 'updated_at'])
    return result


def self_heal_test(task: AgentTask, step: AgentStep, payload: Dict[str, Any]) -> Dict[str, Any]:
    analysis = task.failure_analysis or {}
    if not analysis or analysis.get('reason') == '当前执行未发现失败结果':
        return {
            'status': 'not_applicable',
            'message': '未发现失败证据，无需执行 AI 修复测试。',
            'healing': {
                'locator_repair': None,
                'data_repair': None,
                'step_repair': None,
            },
        }

    healing = {
        'locator_repair': {
            'old_locator': analysis.get('old_locator') or payload.get('old_locator') or '',
            'new_locator': analysis.get('new_locator') or payload.get('new_locator') or '',
            'confidence': analysis.get('confidence', 0.72),
        },
        'data_repair': analysis.get('data_repair') or '根据失败响应补充边界值、必填值和鉴权数据。',
        'step_repair': analysis.get('suggestion') or analysis.get('solution') or '根据失败步骤调整等待、定位器或断言。',
        'apply_mode': 'suggestion_only',
    }
    task.context = {
        **(task.context or {}),
        'self_heal': healing,
    }
    task.save(update_fields=['context', 'updated_at'])
    return {
        'status': 'success',
        'message': 'AI 已生成测试修复建议，等待真实用例或执行器应用。',
        'healing': healing,
    }


def rerun_fixed_test(task: AgentTask, step: AgentStep, payload: Dict[str, Any]) -> Dict[str, Any]:
    healing = (task.context or {}).get('self_heal') or {}
    if not healing:
        return {
            'status': 'not_applicable',
            'message': '没有需要验证的修复建议，无需执行修复后复跑。',
        }
    failed_results = _current_execution_results(task).filter(status='FAILED')
    if not failed_results.exists():
        return {
            'status': 'not_applicable',
            'message': '当前执行已通过，没有需要应用修复并复跑的失败结果。',
        }
    return {
        'status': 'not_applicable',
        'message': '已生成修复建议，但当前执行器未应用该修复，因此未触发无效复跑。',
        'healing': healing,
        'real_execution': False,
    }


def create_report(task: AgentTask, step: AgentStep, payload: Dict[str, Any]) -> Dict[str, Any]:
    executions = list(_current_execution_results(task).order_by('created_at', 'id'))
    real_executions = [
        item for item in executions
        if item.module not in {'browser'}
        and item.status in {'PASSED', 'FAILED'}
        and (item.response_payload or {}).get('real_execution') is not False
    ]
    report_assets = _sync_module_report_assets(task, executions)
    summary = {
        'agent_task_id': task.id,
        'total_steps': task.steps.count(),
        'success_steps': task.steps.filter(status='SUCCESS').count(),
        'skipped_steps': task.steps.filter(status='SKIPPED').count(),
        'not_applicable_steps': task.steps.filter(status='NOT_APPLICABLE').count(),
        'failed_steps': task.steps.filter(status='FAILED').count(),
        'real_execution_results': len(real_executions),
        'skipped_execution_results': sum(item.status == 'SKIPPED' for item in executions),
        'not_applicable_execution_results': sum(item.status == 'NOT_APPLICABLE' for item in executions),
        'assets': list(task.assets.values('asset_id', 'asset_type', 'name')),
        'failure_analysis': task.failure_analysis,
        'self_heal': (task.context or {}).get('self_heal') or {},
        'discovered_test_points': (task.context or {}).get('discovered_test_points') or [],
        'requirement_analysis': (task.context or {}).get('requirement_analysis') or {},
        'module_reports': [asset.metadata for asset in report_assets],
        'note': 'SKIPPED 表示应执行但缺少真实测试资产；NOT_APPLICABLE 表示当前条件不触发该分支。',
    }
    has_execution_evidence = bool(real_executions or report_assets)
    message = (
        f'已关联 {len(report_assets)} 个真实模块执行报告，可查看用例、步骤、耗时、错误和截图详情。'
        if report_assets else
        '真实执行已完成，但执行器未返回可查看的模块报告。'
        if real_executions else
        '未发生真实测试执行，因此不生成质量报告。'
    )
    return {
        'status': 'success' if has_execution_evidence else 'not_applicable',
        'message': message,
        'report_id': None,
        'report_assets': [asset.asset_id for asset in report_assets],
        'summary': summary,
    }


def _sync_module_report_assets(
    task: AgentTask,
    executions: List[TestExecutionResult],
) -> List[TestAsset]:
    assets: List[TestAsset] = []
    for execution in executions:
        response = execution.response_payload if isinstance(execution.response_payload, dict) else {}
        if execution.module == 'app':
            for report in response.get('reports') or []:
                execution_id = report.get('execution_id')
                report_url = str(report.get('report_url') or '')
                if not execution_id or not report_url:
                    continue
                assets.append(_link_asset(
                    task,
                    'report',
                    f'app_allure_{task.id}_{execution_id}',
                    f'APP Allure 报告 · {task.task_name}',
                    'apps.app_automation.AppTestExecution',
                    execution_id,
                    {
                        'module': 'app',
                        'execution_result_id': execution.id,
                        'execution_id': execution_id,
                        'case_id': report.get('case_id'),
                        'report_url': report_url,
                        'public_report_url': report.get('public_report_url') or '',
                        'report_format': 'allure',
                        'status': execution.status,
                    },
                ))
            continue
        if execution.module != 'ui' or not execution.report_url:
            continue
        task_ids = [str(item) for item in response.get('auto_test_task_ids') or [] if str(item)]
        result_rows = response.get('results') if isinstance(response.get('results'), list) else []
        if not task_ids:
            task_ids = [str(item.get('id')) for item in result_rows if item.get('id')]
        if not task_ids or '/report/tasks/' not in execution.report_url:
            continue
        report_urls = {
            str(item.get('id')): str(item.get('report_url') or '')
            for item in result_rows
            if item.get('id')
        }
        for execution_task_id in task_ids:
            report_url = report_urls.get(execution_task_id) or execution.report_url
            module_url = f'/auto-test/reports?tab=ui&task_id={execution_task_id}'
            asset = _link_asset(
                task,
                'report',
                f'ui_allure_{task.id}_{execution_task_id}',
                f'UI Allure 报告 · {task.task_name}',
                'auto-test.tasks',
                execution_task_id,
                {
                    'module': 'ui',
                    'execution_result_id': execution.id,
                    'execution_task_id': execution_task_id,
                    'report_url': report_url,
                    'module_url': module_url,
                    'report_format': 'allure',
                    'status': execution.status,
                    'passed': next((item.get('passed') for item in result_rows if str(item.get('id')) == execution_task_id), None),
                    'failed': next((item.get('failed') for item in result_rows if str(item.get('id')) == execution_task_id), None),
                    'total': next((item.get('total') for item in result_rows if str(item.get('id')) == execution_task_id), None),
                },
            )
            assets.append(asset)
    return assets


TOOLS: Dict[str, Callable[[AgentTask, AgentStep, Dict[str, Any]], Dict[str, Any]]] = {
    'analyze_requirement': analyze_requirement,
    'discover_test_points': discover_test_points,
    'generate_data': generate_test_data,
    'create_case': create_case,
    'run_api_test': run_api_test,
    'run_ui_test': run_ui_test,
    'run_app_test': run_app_test,
    'run_performance_test': run_performance_test,
    'analyze_failure': analyze_failure,
    'self_heal_test': self_heal_test,
    'rerun_fixed_test': rerun_fixed_test,
    'create_report': create_report,
}


def _single_result(result: Dict[str, Any]) -> Any:
    return result.get('result')


def _first_value(*sources: Dict[str, Any], keys: List[str]) -> str:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = source.get(key)
            if value:
                return str(value).strip()
    return ''


def _infer_url(text: str) -> str:
    return infer_http_url(text)


def _normalize_list(value: Any) -> List[Any]:
    if value in (None, ''):
        return []
    if isinstance(value, (list, tuple, set)):
        return [item for item in value if item not in (None, '')]
    return [value]


def _merge_sources(task: AgentTask, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    sources = [payload or {}, task.context or {}]
    if isinstance(task.test_plan, dict):
        sources.append(task.test_plan)
    return sources


def _resolve_api_test_object_refs(task: AgentTask, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    keys = ['api_test_objects', 'test_object_refs', 'test_objects', 'runnergo_test_objects', 'api_targets']
    for source in _merge_sources(task, payload):
        for key in keys:
            for item in _normalize_list(source.get(key)):
                normalized = _normalize_test_object_ref(item, source)
                if normalized:
                    refs.append(normalized)
        normalized = _normalize_test_object_ref(source.get('test_object_ref'), source)
        if normalized:
            refs.append(normalized)

    seen = set()
    unique: List[Dict[str, Any]] = []
    for ref in refs:
        key = (ref.get('team_id'), ref.get('target_id'), ref.get('target_type'))
        if key in seen:
            continue
        seen.add(key)
        unique.append(ref)
    return unique


def _normalize_test_object_ref(value: Any, fallback: Dict[str, Any] | None = None) -> Dict[str, Any] | None:
    fallback = fallback or {}
    if not value:
        target_id = fallback.get('target_id') or fallback.get('api_target_id')
        team_id = fallback.get('team_id') or fallback.get('api_team_id')
        target_type = fallback.get('target_type') or fallback.get('api_target_type') or 'api'
        name = fallback.get('name') or fallback.get('api_name') or ''
    elif isinstance(value, str):
        target_id = value
        team_id = fallback.get('team_id') or fallback.get('api_team_id')
        target_type = fallback.get('target_type') or fallback.get('api_target_type') or 'api'
        name = fallback.get('name') or fallback.get('api_name') or ''
    elif isinstance(value, dict):
        ref = value.get('test_object_ref') if isinstance(value.get('test_object_ref'), dict) else value
        target_id = ref.get('target_id') or ref.get('id') or ref.get('api_id')
        team_id = ref.get('team_id') or fallback.get('team_id') or fallback.get('api_team_id')
        target_type = ref.get('target_type') or ref.get('type') or fallback.get('target_type') or 'api'
        name = ref.get('name') or ref.get('title') or ''
    else:
        return None

    target_id = str(target_id or '').strip()
    team_id = str(team_id or '').strip()
    target_type = str(target_type or 'api').strip().lower()
    if not target_id:
        return None
    normalized = {
        'team_id': team_id,
        'target_id': target_id,
        'target_type': target_type if target_type in {'api', 'sql'} else 'api',
        'name': str(name or '').strip(),
        'source': 'api_test_module',
    }
    if isinstance(value, dict):
        for key in ('method', 'path', 'url', 'base_url', 'headers', 'body'):
            if value.get(key) not in (None, ''):
                normalized[key] = value.get(key)
    return normalized


def _resolve_direct_api_request(task: AgentTask, payload: Dict[str, Any]) -> Dict[str, Any]:
    source = payload or {}
    context = task.context or {}
    url = _first_value(source, context, keys=['url', 'api_url', 'target'])
    base_url = _first_value(source, context, keys=['base_url', 'api_base_url'])
    method = _first_value(source, context, keys=['method', 'http_method']) or 'GET'
    return {
        'method': method.upper(),
        'url': url,
        'base_url': base_url,
        'headers': source.get('headers') or context.get('headers') or {},
        'body': source.get('body') if 'body' in source else context.get('body'),
        'timeout': int(source.get('timeout') or context.get('timeout') or 15),
    }


def _execute_direct_api_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    import requests
    from apps.core.outbound import validate_outbound_http_url

    raw_url = str(payload.get('url') or '').strip()
    base_url = str(payload.get('base_url') or '').strip()
    if urlsplit(raw_url).scheme:
        target = raw_url
    elif base_url:
        target = urljoin(base_url.rstrip('/') + '/', raw_url.lstrip('/'))
    else:
        return {'status': 'skipped', 'message': f'API 请求缺少 base_url，无法执行：{raw_url}'}
    try:
        target = validate_outbound_http_url(target, label='API 测试地址')
    except ValueError as exc:
        return {'status': 'failed', 'error': f'API 地址被安全策略拦截: {exc}'}
    started = time.time()
    method = str(payload.get('method') or 'GET').upper()
    headers = payload.get('headers') or {}
    try:
        response = requests.request(
            method,
            target,
            headers=headers,
            json=payload.get('body') if payload.get('body') not in (None, '') else None,
            timeout=int(payload.get('timeout') or 15),
        )
    except requests.RequestException as exc:
        return {
            'status': 'failed',
            'method': method,
            'url': target,
            'error': f'API 请求失败: {exc}',
            'elapsed_ms': int((time.time() - started) * 1000),
        }
    result = {
        'status': 'success' if response.status_code < 400 else 'failed',
        'method': method,
        'url': target,
        'status_code': response.status_code,
        'elapsed_ms': int((time.time() - started) * 1000),
        'response': response.text[:2000],
    }
    try:
        result['response_json'] = response.json()
    except ValueError:
        pass
    return result


def _resolve_ui_case_files(task: AgentTask, payload: Dict[str, Any]) -> List[str]:
    files: List[str] = []
    for source in _merge_sources(task, payload):
        for key in ['ui_case_files', 'case_files', 'yaml_files', 'case_file', 'ui_case_file']:
            files.extend(str(item).strip() for item in _normalize_list(source.get(key)) if str(item).strip())
    seen = set()
    result = []
    for item in files:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _resolve_ui_test_cases(task: AgentTask, payload: Dict[str, Any]) -> List[Any]:
    from django.db.models import Q
    from apps.testcases.models import TestCase

    explicit_ids: List[Any] = []
    explicit_names: List[str] = []
    for source in _merge_sources(task, payload):
        for key in ['ui_case_ids', 'ui_test_case_ids', 'test_case_ids']:
            explicit_ids.extend(_normalize_list(source.get(key)))
        for key in ['ui_case_names', 'ui_test_case_names', 'test_case_names']:
            explicit_names.extend(str(item).strip() for item in _normalize_list(source.get(key)) if str(item).strip())

    queryset = TestCase.objects.filter(
        Q(project__owner=task.created_by) |
        Q(project__members=task.created_by),
        test_type='ui',
    ).select_related('project').distinct()
    if task.project_id:
        queryset = queryset.filter(project_id=task.project_id)

    selected = []
    clean_ids = [item for item in explicit_ids if str(item).strip()]
    if clean_ids:
        selected = list(queryset.filter(id__in=clean_ids).order_by('id'))
    if explicit_names:
        name_query = Q()
        for name in explicit_names:
            name_query |= Q(title=name) | Q(title__icontains=os.path.splitext(os.path.basename(name))[0])
        selected.extend(list(queryset.filter(name_query).order_by('-updated_at')))

    seen = set()
    unique = []
    for case in selected:
        if case.id in seen:
            continue
        seen.add(case.id)
        unique.append(case)
    return unique


def _run_ui_test_case_refs(
    task: AgentTask,
    step: AgentStep,
    payload: Dict[str, Any],
    cases: List[Any],
) -> Dict[str, Any]:
    started = timezone.now()
    summaries = [
        {
            'case_id': case.id,
            'title': case.title,
            'description': case.description or '',
            'steps': case.steps or '',
            'expected_result': case.expected_result or '',
            'project_id': case.project_id,
            'project_name': case.project.name if case.project else '',
        }
        for case in cases
    ]
    generated_bindings = (task.context or {}).get('generated_test_data', {}).get('bindings') or []
    data_bindings = [
        item for item in generated_bindings
        if item.get('target_type') == 'ui_automation'
        and str(item.get('target_case_id')) in {str(case.id) for case in cases}
    ]
    message = f'已引用 {len(summaries)} 个 UI 自动化用例并挂载测试数据；当前用例未配置可执行浏览器编排。'
    execution = _persist_execution_result(
        task,
        step,
        'ui',
        {
            'ui_case_ids': [item['case_id'] for item in summaries],
            'data_bindings': data_bindings,
            **(payload or {}),
        },
        'SKIPPED',
        message,
        {
            'status': 'skipped',
            'ui_case_refs': summaries,
            'data_bindings': data_bindings,
            'real_execution': False,
        },
        started,
    )
    for item in summaries:
        _link_asset(
            task,
            'ui',
            _task_scoped_asset_id('ui_case', task.id, item['case_id']),
            item['title'],
            'testcases',
            item['case_id'],
            item,
        )
    return {
        'status': 'skipped',
        'message': message,
        'ui_case_refs': summaries,
        'data_bindings': data_bindings,
        'execution_result_id': execution.id,
        'real_execution': False,
    }


def _run_ui_yaml_cases(
    task: AgentTask,
    step: AgentStep,
    payload: Dict[str, Any],
    case_files: List[str],
) -> Dict[str, Any]:
    auto_test_url = os.environ.get('AUTO_TEST_AGENT_API_URL', '').strip()
    if auto_test_url:
        return _run_ui_yaml_cases_via_auto_test(
            task,
            step,
            payload,
            case_files,
            auto_test_url=auto_test_url,
        )
    return _record_execution(
        task,
        step,
        'ui',
        {'case_files': case_files, **(payload or {})},
        'SKIPPED',
        '已选择 UI YAML 用例，但 Auto-test 执行服务未配置，无法生成包含步骤、耗时、错误和截图的 Allure 报告。',
    )


def _run_ui_yaml_cases_via_auto_test(
    task: AgentTask,
    step: AgentStep,
    payload: Dict[str, Any],
    case_files: List[str],
    *,
    auto_test_url: str,
) -> Dict[str, Any]:
    import requests

    context = task.context or {}
    project_id = _first_value(payload, context, keys=['ui_project_id', 'project_id']) or str(task.project_id or 'default')
    base_url = _first_value(payload, context, keys=['base_url', 'url', 'target']) or _infer_url(task.user_requirement)
    env = _first_value(payload, context, keys=['env', 'environment']) or 'test'
    try:
        count = max(1, min(int(payload.get('test_data_count') or context.get('test_data_count') or 3), 20))
    except (TypeError, ValueError):
        count = 3
    token = os.environ.get('TEST_DATA_CENTER_AGENT_TOKEN', 'runnergo-local-agent-token')
    headers = {'X-Agent-Token': token}
    started = timezone.now()
    orchestration_url = f"{auto_test_url.rstrip('/')}/api/agent/internal/orchestrate-ui"
    generated_bindings = (context.get('generated_test_data') or {}).get('bindings') or []
    case_file_ids = {str(case_file) for case_file in case_files}
    ui_bindings = [
        dict(item) for item in generated_bindings
        if item.get('target_type') == 'ui_automation'
        and str(item.get('target_id')) in case_file_ids
    ]
    _attach_runtime_data_rows(ui_bindings, count)
    try:
        response = requests.post(
            orchestration_url,
            headers=headers,
            json={
                'project_id': project_id,
                'case_files': case_files,
                'base_url': base_url,
                'env': env,
                'test_data_count': count,
                'parent_task_id': f'testhub-{task.id}',
                'data_bindings': ui_bindings,
            },
            timeout=int(os.environ.get('AUTO_TEST_AGENT_REQUEST_TIMEOUT_SECONDS', '30')),
        )
        response.raise_for_status()
        orchestration = response.json()
        task_ids = [str(item) for item in orchestration.get('task_ids') or [] if str(item)]
        if not task_ids and orchestration.get('task_id'):
            task_ids = [str(orchestration['task_id'])]
        if not task_ids:
            raise RuntimeError('Auto-test 编排未返回执行任务 ID')

        deadline = time.monotonic() + int(os.environ.get('AUTO_TEST_AGENT_POLL_TIMEOUT_SECONDS', '600'))
        pending = set(task_ids)
        reports: Dict[str, Dict[str, Any]] = {}
        # 轮询期间 Auto-test 可能重启/瞬时抖动：单次网络错误不应终止整个编排，
        # 容忍连续失败一段时间（默认约 60s），恢复后继续轮询真实结果
        poll_grace = int(os.environ.get('AUTO_TEST_AGENT_POLL_ERROR_GRACE_SECONDS', '60'))
        consecutive_errors = 0
        while pending:
            task.current_step = (
                f'等待 Auto-test UI 自动化执行器返回结果：'
                f'{len(task_ids) - len(pending)}/{len(task_ids)} 已完成'
            )
            task.save(update_fields=['current_step', 'updated_at'])
            try:
                for execution_task_id in list(pending):
                    status_response = requests.get(
                        f"{auto_test_url.rstrip('/')}/api/agent/internal/tasks/{execution_task_id}",
                        headers=headers,
                        timeout=int(os.environ.get('AUTO_TEST_AGENT_STATUS_TIMEOUT_SECONDS', '10')),
                    )
                    status_response.raise_for_status()
                    report = status_response.json()
                    reports[execution_task_id] = report
                    if str(report.get('status') or '').lower() in {'success', 'failed', 'stopped'}:
                        pending.remove(execution_task_id)
                consecutive_errors = 0
            except (requests.RequestException, ValueError) as exc:
                if time.monotonic() >= deadline or consecutive_errors * 2 >= poll_grace:
                    raise
                consecutive_errors += 1
                task.current_step = (
                    f'Auto-test 状态查询暂时不可用（{consecutive_errors} 次，{exc}），继续等待'
                )
                task.save(update_fields=['current_step', 'updated_at'])
            if pending:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"等待 Auto-test UI 执行超时: {', '.join(sorted(pending))}")
                time.sleep(float(os.environ.get('AUTO_TEST_AGENT_POLL_INTERVAL_SECONDS', '2')))
    except (requests.RequestException, ValueError, RuntimeError, TimeoutError) as exc:
        message = f'Auto-test UI 参数化编排失败: {exc}'
        execution = _persist_execution_result(
            task,
            step,
            'ui',
            {'case_files': case_files, **(payload or {})},
            'FAILED',
            message,
            {
                'status': 'failed',
                'case_files': case_files,
                'real_execution': False,
                'error': str(exc),
            },
            started,
        )
        return {
            'status': 'failed',
            'message': message,
            'case_files': case_files,
            'execution_result_id': execution.id,
            'real_execution': False,
        }

    results = [reports[task_id] for task_id in task_ids]
    failed = sum(
        1 for item in results
        if str(item.get('status') or '').lower() in {'failed', 'stopped'}
        or int(item.get('failed') or 0) > 0
    )
    executed = sum(int(item.get('total') or 0) for item in results)
    if not executed:
        executed = sum(int(item.get('passed') or 0) + int(item.get('failed') or 0) for item in results)
    has_allure_report = all(
        '/report/tasks/' in str(item.get('report_url') or '')
        for item in results
    )
    if failed:
        status = 'FAILED'
    elif not executed:
        status = 'SKIPPED'
    elif not has_allure_report:
        status = 'FAILED'
    else:
        status = 'PASSED'
    bindings = orchestration.get('bindings') or orchestration.get('data_bindings') or []
    message = (
        f'UI YAML 参数化执行完成：cases={len(case_files)}, rounds={count}, '
        f'tasks={len(task_ids)}, failed={failed}'
    )
    response_payload = {
        'status': status.lower(),
        'case_files': case_files,
        'test_data_count': count,
        'auto_test_task_ids': task_ids,
        'bindings': bindings,
        'assets': orchestration.get('assets') or [],
        'results': results,
        'real_execution': status in {'PASSED', 'FAILED'} and bool(executed),
    }
    evidence_parts = [message]
    for item in results:
        task_id = str(item.get('id') or item.get('task_id') or '')
        failure_steps = item.get('failure_steps') or []
        if failure_steps:
            evidence_parts.append(f"Auto-test 任务 {task_id} 失败步骤:")
            for failure_step in failure_steps:
                evidence_parts.append(
                    f"- 第{failure_step.get('step_index') or failure_step.get('index') or '?'}步 "
                    f"{failure_step.get('name') or failure_step.get('action') or ''}: "
                    f"{failure_step.get('error') or failure_step.get('log') or '执行失败'}"
                )
        log_tail = str(item.get('log_tail') or '').strip()
        if log_tail:
            evidence_parts.append(f"Auto-test 任务 {task_id} 日志尾部:\n{log_tail}")
    execution_logs = '\n'.join(evidence_parts)
    failure_screenshot = next((
        str(failure_step.get('screenshot') or '')
        for item in results
        for failure_step in (item.get('failure_steps') or [])
        if failure_step.get('screenshot')
    ), '')
    execution = _persist_execution_result(
        task,
        step,
        'ui',
        {'case_files': case_files, 'test_data_count': count, **(payload or {})},
        status,
        execution_logs,
        response_payload,
        started,
        screenshot=failure_screenshot,
        report_url=_first_artifact(results, 'report_url') if has_allure_report else '',
    )
    for case_file in case_files:
        _link_asset(
            task,
            'ui',
            _task_scoped_asset_id('ui_yaml', task.id, case_file),
            f'UI YAML 用例 {os.path.basename(case_file)}',
            'auto-test.cases.ui',
            case_file,
            {
                'case_file': case_file,
                'project_id': project_id,
                'test_data_count': count,
                'auto_test_task_ids': task_ids,
                'bindings': [item for item in bindings if item.get('case_file') == case_file],
            },
        )
    return {
        'status': status.lower(),
        'message': message,
        **response_payload,
        'execution_result_id': execution.id,
        'report_url': execution.report_url,
    }


def _attach_runtime_data_rows(bindings: List[Dict[str, Any]], row_limit: int) -> None:
    """Embed rows generated for this Agent run to avoid cross-container SQLite timing."""
    asset_ids = {
        int(item['asset_id'])
        for item in bindings
        if str(item.get('asset_id') or '').isdigit()
    }
    if not asset_ids:
        return
    assets = {
        asset.id: asset
        for asset in TestDataAsset.objects.filter(
            id__in=asset_ids,
            status=TestDataAsset.STATUS_AVAILABLE,
        ).only('id', 'payload')
    }
    for binding in bindings:
        try:
            asset = assets.get(int(binding.get('asset_id')))
        except (TypeError, ValueError):
            asset = None
        if asset is None:
            continue
        payload = asset.payload
        candidates = payload if isinstance(payload, list) else [payload]
        rows = [dict(item) for item in candidates if isinstance(item, dict)]
        if rows:
            binding['runtime_data_rows'] = rows[:row_limit]


def _first_artifact(results: List[Dict[str, Any]], key: str) -> str:
    for item in results:
        values = item.get(key)
        if isinstance(values, list) and values:
            return str(values[0])
        if isinstance(values, str) and values:
            return values
    return ''


def _persist_execution_result(
    task: AgentTask,
    step: AgentStep,
    module: str,
    payload: Dict[str, Any],
    status: str,
    logs: str,
    response_payload: Dict[str, Any],
    started,
    *,
    screenshot: str = '',
    report_url: str = '',
) -> TestExecutionResult:
    finished = timezone.now()
    return TestExecutionResult.objects.create(
        task=task,
        step=step,
        module=module,
        status=status,
        request_payload=payload or {},
        response_payload=response_payload or {},
        logs=logs,
        screenshot=screenshot or '',
        report_url=report_url or '',
        duration_ms=max(int((finished - started).total_seconds() * 1000), 1),
        started_at=started,
        finished_at=finished,
    )


def _link_asset(
    task: AgentTask,
    asset_type: str,
    asset_id: str,
    name: str,
    source_model: str,
    source_id: Any,
    metadata: Dict[str, Any] | None = None,
) -> TestAsset:
    asset, _ = TestAsset.objects.update_or_create(
        asset_id=asset_id,
        task=task,
        defaults={
            'asset_type': asset_type,
            'name': name[:200],
            'project': task.project,
            'source_model': source_model,
            'source_id': str(source_id),
            'metadata': metadata or {},
            'created_by': task.created_by,
        },
    )
    return asset


def _build_app_agent_bridge(task: AgentTask, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from apps.app_automation.constants import DeviceStatus
        from apps.app_automation.models import AppAgentTask, AppDevice, AppPackage
    except Exception as exc:
        return {'status': 'skipped', 'message': f'APP 自动化模块不可用：{exc}'}

    context = task.context or {}
    cases = _resolve_app_test_cases(task, payload)
    if not cases:
        return {
            'status': 'skipped',
            'message': '未绑定 APP 自动化已有测试用例/编排文件；请传入 app_case_ids/app_test_case_ids，或让需求命中已有 AppTestCase.ui_flow',
        }

    device_id = payload.get('device') or payload.get('device_id') or context.get('device') or context.get('device_id')
    package_id = payload.get('app_package') or payload.get('app_package_id') or context.get('app_package') or context.get('app_package_id')

    first_case = cases[0]
    app_project = first_case.project
    if not app_project:
        return {'status': 'skipped', 'message': 'APP 用例未关联项目，无法进入真实设备执行'}

    device_qs = AppDevice.objects.filter(status__in=[DeviceStatus.AVAILABLE, DeviceStatus.ONLINE])
    if device_id:
        device = device_qs.filter(id=device_id).first() or device_qs.filter(device_id=device_id).first()
    else:
        device = device_qs.first()
    if not device:
        return {'status': 'skipped', 'message': '未找到可用 APP 设备/模拟器，APP 自动化真实执行跳过'}

    case_package_ids = [item.app_package_id for item in cases if item.app_package_id]
    if not package_id and case_package_ids:
        package_id = case_package_ids[0]
    package_qs = AppPackage.objects.filter(created_by=task.created_by) | AppPackage.objects.filter(created_by__isnull=True)
    package_qs = package_qs.distinct()
    app_package = package_qs.filter(id=package_id).first() if package_id else package_qs.first()
    if package_id and not app_package:
        return {'status': 'skipped', 'message': '指定 APP 包不存在或当前用户无权使用，APP 自动化真实执行跳过'}

    selected_case_ids = [case.id for case in cases]
    generated_bindings = (context.get('generated_test_data') or {}).get('bindings') or []
    app_data_bindings = [
        item for item in generated_bindings
        if item.get('target_type') == 'app_automation'
        and str(item.get('target_case_id')) in {str(case_id) for case_id in selected_case_ids}
    ]
    scenarios = []
    for index, case in enumerate(cases, start=1):
        flow = case.ui_flow if isinstance(case.ui_flow, list) else (case.ui_flow or {}).get('steps', [])
        scenarios.append({
            'order': index,
            'case_id': case.id,
            'name': case.name,
            'description': case.description or '',
            'package_name': case.app_package.package_name if case.app_package else '',
            'step_count': len(flow),
            'reason': '统一 AI Test Agent 引用已有 APP 编排用例',
        })

    app_task = AppAgentTask.objects.create(
        user=task.created_by,
        project=app_project,
        device=device,
        app_package=app_package,
        goal=payload.get('goal') or payload.get('requirement') or task.user_requirement,
        auto_execute=True,
        plan={
            'goal': payload.get('goal') or payload.get('requirement') or task.user_requirement,
            'mode': 'existing_cases',
            'source': 'unified_ai_test_agent',
            'summary': f'统一编排已引用 {len(selected_case_ids)} 个已有 APP 用例/编排文件。',
            'selected_case_ids': selected_case_ids,
            'scenarios': scenarios,
            'coverage_gap': False,
            'coverage_message': '',
            'review_required': False,
            'test_data_count': max(1, min(int(payload.get('test_data_count') or (task.context or {}).get('test_data_count') or 3), 20)),
            'generated_data_bindings': app_data_bindings,
            'asset_source': 'app_automation.ui_flow',
            'device': {
                'id': device.id,
                'device_id': device.device_id,
                'name': device.name or device.device_id,
                'platform': device.platform,
                'status': device.status,
            },
            'package': {
                'id': app_package.id if app_package else None,
                'name': app_package.name if app_package else '',
                'package_name': app_package.package_name if app_package else '',
            },
        },
    )
    return {'status': 'success', 'task': app_task}


def _resolve_app_test_cases(task: AgentTask, payload: Dict[str, Any]) -> List[Any]:
    from django.db.models import Q
    from apps.app_automation.models import AppTestCase

    explicit_ids: List[Any] = []
    explicit_names: List[str] = []
    for source in _merge_sources(task, payload):
        for key in ['app_case_ids', 'app_test_case_ids', 'case_ids', 'app_yaml_case_ids']:
            explicit_ids.extend(_normalize_list(source.get(key)))
        for key in ['app_case_names', 'app_test_case_names', 'app_case_files', 'app_yaml_files']:
            explicit_names.extend(str(item).strip() for item in _normalize_list(source.get(key)) if str(item).strip())
        single_id = source.get('app_case_id') or source.get('app_test_case_id')
        if single_id:
            explicit_ids.append(single_id)

    project_id = payload.get('app_project') or payload.get('app_project_id') or (task.context or {}).get('app_project_id')
    package_id = payload.get('app_package') or payload.get('app_package_id') or (task.context or {}).get('app_package_id')
    queryset = AppTestCase.objects.filter(
        Q(created_by=task.created_by) |
        Q(project__owner=task.created_by) |
        Q(project__members=task.created_by)
    ).select_related('project', 'app_package').distinct()
    if project_id:
        queryset = queryset.filter(project_id=project_id)
    if package_id:
        queryset = queryset.filter(Q(app_package_id=package_id) | Q(app_package__isnull=True))

    selected = []
    if explicit_ids:
        clean_ids = [item for item in explicit_ids if str(item).strip()]
        selected = list(queryset.filter(id__in=clean_ids).order_by('id'))
    if explicit_names:
        name_query = Q()
        for name in explicit_names:
            name_query |= Q(name=name) | Q(name__icontains=os.path.splitext(os.path.basename(name))[0])
        selected.extend(list(queryset.filter(name_query).order_by('-updated_at')))
    if not selected:
        keywords = _keywords(task.user_requirement)
        if keywords:
            query = Q()
            for keyword in keywords:
                query |= Q(name__icontains=keyword) | Q(description__icontains=keyword)
            selected = list(queryset.filter(query).order_by('-updated_at')[:3])

    seen = set()
    usable = []
    for case in selected:
        if case.id in seen or not _usable_app_flow(case.ui_flow):
            continue
        seen.add(case.id)
        usable.append(case)
    return usable


def _usable_app_flow(ui_flow: Any) -> bool:
    if isinstance(ui_flow, list):
        return bool(ui_flow)
    if isinstance(ui_flow, dict):
        return bool(ui_flow.get('steps'))
    return False


def _keywords(text: str) -> List[str]:
    raw = re.findall(r'[\w\u4e00-\u9fff]{2,}', str(text or '').lower())
    stop_words = {'测试', '执行', '自动化', 'app', 'agent', '功能', '系统', '页面', '用例'}
    return [item for item in raw if item not in stop_words][:8]


def _record_execution(
    task: AgentTask,
    step: AgentStep,
    module: str,
    payload: Dict[str, Any],
    result_status: str,
    logs: str,
) -> Dict[str, Any]:
    started = timezone.now()
    time.sleep(0.01)
    result = _persist_execution_result(
        task,
        step,
        module,
        payload,
        result_status,
        logs,
        {'message': logs, 'requirement': task.user_requirement},
        started,
    )
    return {
        'status': result_status.lower(),
        'execution_result_id': result.id,
        'real_execution': result_status not in {'SKIPPED', 'PENDING'},
        'message': logs,
    }
