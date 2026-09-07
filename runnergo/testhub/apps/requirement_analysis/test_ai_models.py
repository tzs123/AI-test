"""
AIModelService 纯逻辑方法测试
覆盖 models.py 中 AIModelService.get_openai_compatible_headers 和 AIModelService.build_openai_compatible_url
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase as DjangoTestCase

from apps.projects.models import Project
from apps.requirement_analysis.models import (
    AIModelService,
    GenerationConfig,
    TestCaseGenerationTask,
)
from apps.requirement_analysis.serializers import TestCaseGenerationTaskSerializer
from apps.requirement_analysis.views import TestCaseGenerationTaskViewSet
from apps.testcases.models import TestCase


class _BrokenStreamResponse:
    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def aiter_lines(self):
        yield 'data: {"choices":[{"delta":{"content":"| 用例编号 |"},"finish_reason":null}]}'
        raise httpx.RemoteProtocolError(
            'peer closed connection without sending complete message body (incomplete chunked read)'
        )


class _BrokenStreamClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def stream(self, *args, **kwargs):
        return _BrokenStreamResponse()


class _EmptyBrokenStreamResponse:
    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def aiter_lines(self):
        if False:
            yield ''
        raise httpx.RemoteProtocolError('TLS record layer failure')


class _EmptyBrokenStreamClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def stream(self, *args, **kwargs):
        return _EmptyBrokenStreamResponse()


class _RecoveredStreamResponse:
    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def aiter_lines(self):
        yield 'data: {"choices":[{"delta":{"content":" 测试模块 |\\n|---|---|"},"finish_reason":"stop"}]}'
        yield 'data: [DONE]'


class _RecoveringStreamClient:
    stream_calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def stream(self, *args, **kwargs):
        type(self).stream_calls.append(kwargs.get('json') or {})
        if len(type(self).stream_calls) == 1:
            return _BrokenStreamResponse()
        return _RecoveredStreamResponse()


class _DoubleRecoveringStreamResponse:
    status_code = 200

    def __init__(self, content, broken=True):
        self.content = content
        self.broken = broken

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def aiter_lines(self):
        yield f'data: {{"choices":[{{"delta":{{"content":"{self.content}"}},"finish_reason":null}}]}}'
        if self.broken:
            raise httpx.RemoteProtocolError('connection reset during recovery')
        yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
        yield 'data: [DONE]'


class _DoubleRecoveringStreamClient:
    stream_calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def stream(self, *args, **kwargs):
        type(self).stream_calls.append(kwargs.get('json') or {})
        call_number = len(type(self).stream_calls)
        if call_number == 1:
            return _DoubleRecoveringStreamResponse('第一段', broken=True)
        if call_number == 2:
            return _DoubleRecoveringStreamResponse('第二段', broken=True)
        return _DoubleRecoveringStreamResponse('第三段', broken=False)


class _RegularJSONStreamResponse:
    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def aiter_lines(self):
        yield '{"choices":[{"message":{"content":"| 用例编号 | 测试模块 |"},"finish_reason":"stop"}]}'


class _RegularJSONStreamClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def stream(self, *args, **kwargs):
        return _RegularJSONStreamResponse()


class _DailyLimitResponse:
    status_code = 429

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def aread(self):
        return b'error: code=429 reason="DAILY_LIMIT_EXCEEDED" message="daily usage limit exceeded"'


class _DailyLimitClient:
    stream_calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def stream(self, *args, **kwargs):
        type(self).stream_calls += 1
        return _DailyLimitResponse()


class StreamRecoveryTests(DjangoTestCase):
    def test_empty_stream_failure_falls_back_to_non_streaming_response(self):
        config = SimpleNamespace(
            model_name='gpt-test',
            base_url='https://api.example.com/v1',
            temperature=0.2,
            top_p=1.0,
            max_tokens=8192,
            max_output_tokens=None,
            retry_count=0,
            api_key='sk-test',
        )
        callback_chunks = []

        async def callback(chunk):
            callback_chunks.append(chunk)

        async def consume():
            generator = AIModelService.call_openai_compatible_api_stream(
                config,
                [{'role': 'user', 'content': '生成测试用例'}],
                callback=callback,
            )
            return ''.join([chunk async for chunk in generator])

        fallback_mock = AsyncMock(return_value={
            'choices': [{
                'message': {'content': '| 用例编号 | 测试模块 |'},
                'finish_reason': 'stop',
            }],
        })
        with patch(
                'apps.requirement_analysis.models.httpx.AsyncClient',
                _EmptyBrokenStreamClient,
        ), patch(
                'apps.core.outbound.validate_outbound_http_url',
                side_effect=lambda url, **kwargs: url,
        ), patch.object(
                AIModelService,
                'call_openai_compatible_api',
                new=fallback_mock,
        ):
            content = async_to_sync(consume)()

        self.assertEqual(content, '| 用例编号 | 测试模块 |')
        self.assertEqual(callback_chunks, ['| 用例编号 | 测试模块 |'])
        self.assertEqual(fallback_mock.await_count, 1)
        self.assertEqual(fallback_mock.await_args.kwargs['max_retries'], 0)

    def test_streaming_api_accepts_regular_json_gateway_response(self):
        config = SimpleNamespace(
            model_name='gpt-test',
            base_url='https://api.example.com/v1',
            temperature=0.2,
            top_p=1.0,
            max_tokens=8192,
            max_output_tokens=None,
            retry_count=0,
            api_key='sk-test',
        )
        callback_chunks = []

        async def callback(chunk):
            callback_chunks.append(chunk)

        async def consume():
            generator = AIModelService.call_openai_compatible_api_stream(
                config,
                [{'role': 'user', 'content': '生成测试用例'}],
                callback=callback,
            )
            return ''.join([chunk async for chunk in generator])

        with patch(
                'apps.requirement_analysis.models.httpx.AsyncClient',
                _RegularJSONStreamClient,
        ), patch(
                'apps.core.outbound.validate_outbound_http_url',
                side_effect=lambda url, **kwargs: url,
        ):
            content = async_to_sync(consume)()

        self.assertEqual(content, '| 用例编号 | 测试模块 |')
        self.assertEqual(callback_chunks, ['| 用例编号 | 测试模块 |'])

    def test_cloudflare_html_error_is_reduced_to_actionable_message(self):
        html = '<!DOCTYPE html><html><title>524: A timeout occurred</title></html>'

        message = AIModelService.format_api_error('其他', 524, html)

        self.assertIn('HTTP 524', message)
        self.assertIn('上游模型未在网关时限内响应', message)
        self.assertNotIn('<html>', message)

    def test_legacy_html_task_error_is_sanitized(self):
        legacy_error = '其他 API返回错误 524: <!DOCTYPE html><html>timeout</html>'

        message = AIModelService.sanitize_error_message(legacy_error)

        self.assertIn('HTTP 524', message)
        self.assertNotIn('<!DOCTYPE', message)

    def test_traceback_error_keeps_the_actionable_final_exception(self):
        traceback_error = (
            'Traceback (most recent call last):\n'
            '  File "/tmp/skill.py", line 10, in <module>\n'
            '    client.chat.completions.create()\n'
            'openai.APITimeoutError: Request timed out'
        )

        message = AIModelService.sanitize_error_message(traceback_error)

        self.assertEqual(message, 'openai.APITimeoutError: Request timed out')
        self.assertNotIn('/tmp/skill.py', message)

    def test_truncated_traceback_returns_recovery_guidance(self):
        truncated_error = (
            'Traceback (most recent call last):\n'
            '  File "/tmp/skill.py", line 10, in <module>\n'
            '    return func(*args, **kwargs)'
        )

        message = AIModelService.sanitize_error_message(truncated_error)

        self.assertIn('旧版错误日志被截断', message)
        self.assertNotIn('return func', message)

    def test_daily_limit_error_is_clear_and_not_retried(self):
        config = SimpleNamespace(
            model_name='gpt-test',
            base_url='https://api.example.com/v1',
            temperature=0.2,
            top_p=1.0,
            max_tokens=8192,
            max_output_tokens=None,
            retry_count=3,
            api_key='sk-test',
            get_model_type_display=lambda: '其他',
        )
        _DailyLimitClient.stream_calls = 0

        async def consume():
            generator = AIModelService.call_openai_compatible_api_stream(
                config,
                [{'role': 'user', 'content': '生成测试用例'}],
            )
            async for _ in generator:
                pass

        with patch(
                'apps.requirement_analysis.models.httpx.AsyncClient',
                _DailyLimitClient,
        ), patch(
                'apps.core.outbound.validate_outbound_http_url',
                side_effect=lambda url, **kwargs: url,
        ):
            with self.assertRaisesRegex(Exception, '今日额度已用完'):
                async_to_sync(consume)()

        self.assertEqual(_DailyLimitClient.stream_calls, 1)

    def test_intermediate_skill_stage_uses_streaming_api(self):
        task = SimpleNamespace(
            writer_prompt_config=SimpleNamespace(content='编写提示词'),
            writer_model_config=SimpleNamespace(),
            requirement_text='需求正文',
            case_type_rules={},
            knowledge_rule_context=[],
            skill_context=[
                {
                    'id': 1,
                    'name': 'Requirement Analyst',
                    'identifier': 'requirement-analyst',
                    'files': [{'path': 'SKILL.md', 'content': '# Analyze'}],
                },
                {
                    'id': 2,
                    'name': 'Testcase Writer',
                    'identifier': 'testcase-writer',
                    'files': [{'path': 'SKILL.md', 'content': '# Write'}],
                },
            ],
            skill_execution_results=[],
        )

        async def stream_response(*args, **kwargs):
            yield '阶段'
            yield '结果'

        non_streaming_mock = AsyncMock()
        with patch.object(
                AIModelService,
                'call_openai_compatible_api_stream',
                new=stream_response,
        ), patch.object(
                AIModelService,
                'call_openai_compatible_api',
                new=non_streaming_mock,
        ):
            async_to_sync(AIModelService.prepare_skill_pipeline)(task)

        self.assertEqual(task._skill_pipeline_previous_output, '阶段结果')
        self.assertEqual(task.skill_execution_results[0]['output'], '阶段结果')
        non_streaming_mock.assert_not_awaited()

    def test_partial_stream_disconnect_recovers_without_duplicate_content(self):
        config = SimpleNamespace(
            model_name='gpt-test',
            base_url='https://api.example.com/v1',
            temperature=0.2,
            top_p=1.0,
            max_tokens=8192,
            max_output_tokens=None,
            retry_count=3,
            api_key='sk-test',
        )
        callback_chunks = []

        async def callback(chunk):
            callback_chunks.append(chunk)

        _RecoveringStreamClient.stream_calls = []
        non_streaming_mock = AsyncMock()

        async def consume():
            chunks = []
            generator = AIModelService.call_openai_compatible_api_stream(
                config,
                [{'role': 'user', 'content': '生成测试用例'}],
                callback=callback,
            )
            async for chunk in generator:
                chunks.append(chunk)
            return ''.join(chunks)

        with patch(
                'apps.requirement_analysis.models.httpx.AsyncClient',
                _RecoveringStreamClient,
        ), patch(
                'apps.core.outbound.validate_outbound_http_url',
                side_effect=lambda url, **kwargs: url,
        ), patch.object(
                AIModelService,
                'call_openai_compatible_api',
                new=non_streaming_mock,
        ):
            content = async_to_sync(consume)()

        self.assertEqual(content, '| 用例编号 | 测试模块 |\n|---|---|')
        self.assertEqual(''.join(callback_chunks), content)
        non_streaming_mock.assert_not_awaited()
        self.assertEqual(len(_RecoveringStreamClient.stream_calls), 2)
        recovery_messages = _RecoveringStreamClient.stream_calls[1]['messages']
        self.assertEqual(recovery_messages[-2], {
            'role': 'assistant',
            'content': '| 用例编号 |',
        })
        self.assertIn('禁止重复任何已输出内容', recovery_messages[-1]['content'])

    def test_overlap_trimming_keeps_only_new_recovery_content(self):
        self.assertEqual(
            AIModelService.trim_stream_recovery_overlap('ABCDEF', 'DEFGHI'),
            'GHI',
        )
        self.assertEqual(
            AIModelService.trim_stream_recovery_overlap('ABCDEF', 'XYZ'),
            'XYZ',
        )

    def test_repeated_disconnects_merge_recovery_context(self):
        config = SimpleNamespace(
            model_name='gpt-test',
            base_url='https://api.example.com/v1',
            temperature=0.2,
            top_p=1.0,
            max_tokens=8192,
            max_output_tokens=None,
            retry_count=0,
            api_key='sk-test',
        )
        _DoubleRecoveringStreamClient.stream_calls = []

        async def consume():
            generator = AIModelService.call_openai_compatible_api_stream(
                config,
                [{'role': 'user', 'content': '生成测试用例'}],
            )
            return ''.join([chunk async for chunk in generator])

        with patch(
                'apps.requirement_analysis.models.httpx.AsyncClient',
                _DoubleRecoveringStreamClient,
        ), patch(
                'apps.core.outbound.validate_outbound_http_url',
                side_effect=lambda url, **kwargs: url,
        ):
            content = async_to_sync(consume)()

        self.assertEqual(content, '第一段第二段第三段')
        self.assertEqual(len(_DoubleRecoveringStreamClient.stream_calls), 3)
        self.assertEqual(
            _DoubleRecoveringStreamClient.stream_calls[2]['messages'][-2],
            {'role': 'assistant', 'content': '第一段第二段'},
        )

    def test_review_timeout_does_not_retry_or_start_non_streaming_fallback(self):
        task = SimpleNamespace(
            reviewer_prompt_config=SimpleNamespace(content='评审提示词'),
            reviewer_model_config=SimpleNamespace(),
            case_type_rules={},
            knowledge_rule_context=[],
            requirement_text='车抵贷需求',
            skill_context=[{
                'name': 'Testcase Reviewer',
                'identifier': 'testcase-reviewer',
                'files': [{'path': 'SKILL.md', 'content': '# Review workflow'}],
            }],
        )
        callback_chunks = []
        stream_kwargs = []

        async def callback(chunk):
            callback_chunks.append(chunk)

        async def broken_stream(*args, **kwargs):
            stream_kwargs.append(kwargs)
            if False:
                yield ''
            raise asyncio.TimeoutError('review deadline exceeded')

        fallback_mock = AsyncMock(return_value={
            'choices': [{
                'message': {'content': '不应被调用'},
                'finish_reason': 'stop',
            }],
        })

        with patch.object(
                AIModelService,
                'call_openai_compatible_api_stream',
                new=broken_stream,
        ), patch.object(
                AIModelService,
                'call_openai_compatible_api',
                new=fallback_mock,
        ):
            with self.assertRaises(asyncio.TimeoutError):
                async_to_sync(AIModelService.review_test_cases_stream)(
                    task,
                    '测试用例正文',
                    callback=callback,
                )

        self.assertEqual(callback_chunks, [])
        self.assertEqual(len(stream_kwargs), 1)
        self.assertEqual(stream_kwargs[0]['max_retries'], 0)
        self.assertEqual(stream_kwargs[0]['max_continuations'], 0)
        self.assertFalse(stream_kwargs[0]['allow_stream_recovery'])
        fallback_mock.assert_not_awaited()

    def test_review_timeout_honors_supported_configured_value(self):
        self.assertEqual(AIModelService.get_effective_review_timeout(1500), 1500)
        self.assertEqual(AIModelService.get_effective_review_timeout(3600), 3600)
        self.assertEqual(AIModelService.get_effective_review_timeout(7200), 3600)
        self.assertEqual(AIModelService.get_effective_review_timeout(None), 60)
        self.assertEqual(AIModelService.get_effective_review_timeout(1), 10)

    def test_complete_review_uses_one_attempt_and_propagates_timeout(self):
        task = SimpleNamespace(
            reviewer_prompt_config=SimpleNamespace(content='评审提示词'),
            reviewer_model_config=SimpleNamespace(),
            case_type_rules={},
            knowledge_rule_context=[],
            requirement_text='车抵贷需求',
            skill_context=[{
                'name': 'Testcase Reviewer',
                'identifier': 'testcase-reviewer',
                'files': [{'path': 'SKILL.md', 'content': '# Review workflow'}],
            }],
            best_review_score=None,
            review_round=0,
        )
        api_mock = AsyncMock(side_effect=asyncio.TimeoutError('review deadline exceeded'))

        with patch.object(
                AIModelService,
                'call_openai_compatible_api',
                new=api_mock,
        ):
            with self.assertRaises(asyncio.TimeoutError):
                async_to_sync(AIModelService.review_test_cases)(
                    task,
                    '测试用例正文',
                    timeout_seconds=23,
                )

        self.assertEqual(api_mock.await_args.kwargs['max_retries'], 0)
        self.assertEqual(api_mock.await_args.kwargs['read_timeout_seconds'], 23)


class CaseTypeQuotaTests(DjangoTestCase):
    """用例类型配额规则测试。"""

    def setUp(self):
        self.task = SimpleNamespace(case_type_rules={
            'enabled': True,
            'focused_types': ['functional'],
            'focused_count': 5,
            'default_count': 3,
            'focus_keywords': '验证码60秒倒计时、重复提交、渠道参数',
        }, requirement_text='需求描述：验证码发送后进入60秒倒计时，渠道参数必须来自正式接口契约。')

    def test_quota_targets_keep_non_focused_types_at_three(self):
        targets = AIModelService.get_case_type_quota_targets(self.task)
        self.assertEqual(targets, {
            'functional': 5,
            'exception': 3,
            'boundary': 3,
            'equivalence': 3,
            'security': 3,
            'compatibility': 3,
        })

    def test_quota_instruction_contains_exact_counts_and_total(self):
        instruction = AIModelService.get_case_type_quota_instruction(self.task)
        self.assertIn('功能测试：严格生成 5 条', instruction)
        self.assertIn('异常测试：严格生成 3 条', instruction)
        self.assertIn('总用例数必须为 20 条', instruction)
        self.assertIn('验证码60秒倒计时、重复提交、渠道参数', instruction)
        self.assertIn('具体测试点必须来自用户需求描述和上传材料', instruction)
        self.assertIn('同类语义延伸', instruction)
        self.assertIn('待业务确认', instruction)

    def test_grounding_context_keeps_short_requirement_material(self):
        context = AIModelService.get_requirement_grounding_context(self.task)
        self.assertEqual(context, self.task.requirement_text)

    def test_apply_quotas_trims_extra_rows_per_type(self):
        rows = [
            '| 用例编号 | 测试模块 | 测试类型 | 测试场景 | 测试数据 | 前置条件 | 操作步骤 | 预期结果 | 优先级 | 需求编号 |',
            '|---|---|---|---|---|---|---|---|---|---|',
        ]
        case_id = 1
        for case_type, count in [
            ('功能测试', 6), ('异常测试', 4), ('边界值测试', 4),
            ('等价类测试', 4), ('安全测试', 3), ('兼容测试', 3)
        ]:
            for _ in range(count):
                rows.append(
                    f'| TC{case_id:03d} | 模块 | {case_type} | 场景{case_id} | 无 | 无 | 执行 | 通过 | P1 | REQ-{case_id:03d} |'
                )
                case_id += 1

        result = AIModelService.apply_case_type_quotas(self.task, '\n'.join(rows))

        self.assertEqual(result.count('| 功能测试 |'), 5)
        self.assertEqual(result.count('| 异常测试 |'), 3)
        self.assertEqual(result.count('| 边界值测试 |'), 3)
        self.assertEqual(result.count('| 等价类测试 |'), 3)
        self.assertEqual(result.count('| 安全测试 |'), 3)
        self.assertEqual(result.count('| 兼容测试 |'), 3)

    def test_disabled_rules_leave_content_unchanged(self):
        task = SimpleNamespace(case_type_rules={'enabled': False})
        content = '| 用例编号 | 测试类型 |\n|---|---|\n| TC001 | 功能测试 |'
        self.assertEqual(AIModelService.apply_case_type_quotas(task, content), content)

    def test_knowledge_rules_are_rendered_with_conflict_priority(self):
        task = SimpleNamespace(
            case_type_rules={'enabled': False, 'focus_keywords': '年龄必须大于22岁'},
            knowledge_rule_context=[{
                'id': 1,
                'title': '贷款年龄限制',
                'content': '申请人年龄范围为22至60岁',
                'project_name': '汽车金融',
                'module': '额度评估',
                'applicable_conditions': '个人贷款申请',
            }],
        )

        instruction = AIModelService.get_knowledge_rule_instruction(task)

        self.assertIn('年龄必须大于22岁', instruction)
        self.assertIn('贷款年龄限制', instruction)
        self.assertIn('汽车金融 / 额度评估', instruction)
        self.assertIn('当前需求与历史规则冲突时，以当前需求为准', instruction)


class AIModelPromptInstructionTests(DjangoTestCase):
    def test_sort_and_renumber_preserve_analysis_and_numbered_footer(self):
        content = """## 需求分析

| 分析项 | 结论 |
|---|---|
| 风险 | 并发与超时 |

## 测试用例

| 用例编号 | 测试模块 | 测试类型 | 测试场景 | 测试数据 | 前置条件 | 操作步骤 | 预期结果 | 优先级 | 需求编号 |
|---|---|---|---|---|---|---|---|---|---|
| TC-010 | 模块B | 功能测试 | 场景B | 无 | 无 | 步骤B | 结果B | P1 | REQ-B |
| TC-002 | 模块A | 功能测试 | 场景A | 无 | 无 | 步骤A | 结果A | P1 | REQ-A |

## 待确认事项
1. 确认状态枚举。
2. 提供接口契约。"""

        sorted_content = AIModelService.sort_test_cases_by_id(content)
        result = AIModelService.renumber_test_cases(sorted_content)

        self.assertIn('## 需求分析', result)
        self.assertIn('| 风险 | 并发与超时 |', result)
        self.assertIn('## 待确认事项\n1. 确认状态枚举。\n2. 提供接口契约。', result)
        self.assertLess(result.index('场景A'), result.index('场景B'))
        self.assertIn('| TC-001 | 模块A |', result)
        self.assertIn('| TC-002 | 模块B |', result)

    def test_business_design_instruction_covers_equivalence_boundary_and_mobile(self):
        instruction = AIModelService.get_business_test_design_instruction()

        self.assertIn('业务目标', instruction)
        self.assertIn('有效等价类和无效等价类', instruction)
        self.assertIn('min-1/min/min+1', instruction)
        self.assertIn('iOS/Android', instruction)
        self.assertIn('前后台/锁屏/进程被回收/恢复', instruction)

    def test_source_traceability_instruction_connects_workflow_knowledge_and_upload(self):
        task = SimpleNamespace(
            requirement_text='【文件：申请接口.yaml】\nOpenAPI paths /apply',
            knowledge_rule_context=[
                {
                    'title': '申请状态规则',
                    'content': '提交后进入审核中',
                    'rule_type': 'state_transition',
                },
                {
                    'title': '借款申请流程',
                    'content': '填写 -> 提交 -> 审核',
                    'rule_type': 'workflow',
                    'workflow_id': 7,
                },
            ],
        )

        instruction = AIModelService.get_source_traceability_instruction(task)

        self.assertIn('来源追溯与业务场景链路', instruction)
        self.assertIn('工作流节点和连线', instruction)
        self.assertIn('知识库规则/知识图谱', instruction)
        self.assertIn('上传材料中的每个关键接口/页面流程', instruction)

    def test_extract_review_score_uses_formal_first_line_score(self):
        feedback = 'AI评分：71/100\n综合评分：88/100\n某维度：100/100'

        self.assertEqual(AIModelService.extract_review_score(feedback), 71)

    def test_extract_review_score_does_not_accept_dimension_score(self):
        feedback = 'AI评分：64/100\n功能覆盖率：100/100\n综合评分：64/100'

        self.assertEqual(AIModelService.extract_review_score(feedback), 64)


class TestCaseContentParserTests(DjangoTestCase):
    """测试用例解析必须跳过前置分析/配额统计表，定位逐条明细表。"""

    def test_parser_ignores_quota_summary_rows(self):
        content = """
### 配额覆盖情况
| 测试类型 | 要求数量 | 实际数量 | 用例编号 |
|---|---:|---:|---|
| 功能测试 | 12 | 12 | 001-012 |
| 异常测试 | 12 | 12 | 013-024 |

### 测试用例明细
| 用例编号 | 测试模块 | 测试类型 | 测试场景 | 测试数据 | 前置条件 | 操作步骤 | 预期结果 | 优先级 | 需求编号 |
|---|---|---|---|---|---|---|---|---|---|
| 001 | 登录 | 功能测试 | 使用有效账号登录 | 用户名=a；密码=b | 账号已注册 | 1. 打开登录页<br>2. 输入账号密码<br>3. 点击登录 | 登录成功并进入首页 | P1 | REQ-LOGIN-001 |
| 002 | 登录 | 异常测试 | 使用错误密码登录 | 用户名=a；密码=wrong | 账号已注册 | 1. 打开登录页<br>2. 输入错误密码<br>3. 点击登录 | 页面提示密码错误且不创建会话 | P1 | REQ-LOGIN-002 |
"""

        parsed = TestCaseGenerationTaskViewSet()._parse_table_format(content)

        self.assertEqual([case['caseId'] for case in parsed], ['001', '002'])
        self.assertEqual(parsed[0]['scenario'], '使用有效账号登录')
        self.assertIn('点击登录', parsed[0]['steps'])
        self.assertIn('进入首页', parsed[0]['expected'])
        self.assertEqual(parsed[0]['requirementId'], 'REQ-LOGIN-001')

class GenerationCompletionTests(DjangoTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='generation-completion-tester',
            password='test-password',
        )
        self.generated_cases = '| 用例编号 | 测试类型 | 测试场景 |\n|---|---|---|\n| TC001 | 功能测试 | 业务主流程 |'
        self.task = TestCaseGenerationTask.objects.create(
            task_id='TASK_COMPLETED_WITH_SCORE',
            title='生成完成测试',
            requirement_text='业务需求',
            created_by=self.user,
            status='completed',
            progress=100,
            generated_test_cases=self.generated_cases,
            final_test_cases=self.generated_cases,
            review_feedback='AI评分：63/100',
            review_score=63,
            review_pending=True,
        )

    def test_completed_task_is_complete_regardless_of_review_score(self):
        data = TestCaseGenerationTaskSerializer(self.task).data

        self.assertTrue(data['quality_completed'])
        self.assertEqual(data['review_score'], 63)

    def test_task_serializer_exposes_structured_generation_events(self):
        self.task.generation_log = (
            '[{"stage":"skill","status":"running","title":"执行 Skill 1/2",'
            '"message":"需求分析"}]'
        )

        data = TestCaseGenerationTaskSerializer(self.task).data

        self.assertEqual(data['generation_events'][0]['stage'], 'skill')
        self.assertEqual(data['generation_events'][0]['title'], '执行 Skill 1/2')

    def test_async_server_skill_fallback_events_are_persisted(self):
        self.task.status = 'generating'
        self.task.progress = 30
        self.task.skill_context = [{
            'id': 7,
            'name': 'AITestCaseSkill',
            'identifier': 'ai-test-case-skill',
            'execution': {
                'mode': 'server',
                'runtime': 'python',
                'entrypoint': 'scripts/skill.py',
            },
            'files': [
                {'path': 'SKILL.md', 'content': '# Generate cases'},
                {'path': 'scripts/skill.py', 'content': 'print("run")'},
            ],
        }]
        self.task.save(update_fields=['status', 'progress', 'skill_context'])
        runtime_mock = AsyncMock(side_effect=RuntimeError(
            'Skill 调用 AI 模型失败：无法连接模型服务'
        ))

        with patch(
                'apps.requirement_analysis.skill_runtime.execute_skill_snapshot',
                new=runtime_mock,
        ):
            async_to_sync(AIModelService.prepare_skill_pipeline)(self.task)

        self.task.refresh_from_db()
        events = TestCaseGenerationTaskSerializer(self.task).data['generation_events']
        self.assertEqual(self.task.progress, 40)
        self.assertEqual(
            [event['title'] for event in events],
            [
                '执行 Skill 1/1',
                'Skill 1 执行失败',
                '切换平台模型继续执行当前 Skill',
            ],
        )

    def test_final_server_skill_timeout_falls_back_to_platform_model(self):
        task = SimpleNamespace(
            writer_prompt_config=None,
            writer_model_config=SimpleNamespace(),
            generation_mode='quick',
            case_type_rules={'enabled': False},
            knowledge_rule_context=[],
            requirement_text='需求：登录失败三次锁定账号。',
            skill_context=[{
                'id': 33,
                'name': 'Slow Server Skill',
                'identifier': 'slow-server-skill',
                'execution': {
                    'mode': 'server',
                    'runtime': 'python',
                    'entrypoint': 'scripts/skill.py',
                    'timeout_seconds': 900,
                },
                'files': [
                    {'path': 'SKILL.md', 'content': '# Generate executable login cases'},
                    {'path': 'scripts/skill.py', 'content': 'print("run")'},
                ],
            }],
            skill_execution_results=[],
        )
        final_output = '| 用例编号 | 测试模块 |\n|---|---|\n| TC-001 | 登录 |'
        runtime_mock = AsyncMock(side_effect=RuntimeError('Skill执行超时（60秒）'))
        formatter_mock = AsyncMock(return_value={
            'choices': [{'message': {'content': final_output}}],
        })

        with patch(
                'apps.requirement_analysis.skill_runtime.execute_skill_snapshot',
                new=runtime_mock,
        ), patch.object(
                AIModelService,
                'call_openai_compatible_api',
                new=formatter_mock,
        ):
            result = async_to_sync(AIModelService.generate_test_cases)(task)

        self.assertEqual(result, final_output)
        formatter_mock.assert_awaited_once()
        self.assertEqual(task.skill_execution_results[0]['status'], 'completed')
        self.assertEqual(task.skill_execution_results[0]['execution_mode'], 'model')
        self.assertIn('切换平台模型继续执行当前 Skill', task.generation_log)

    def test_cancel_marks_task_and_records_timeline_event(self):
        self.task.status = 'generating'
        self.task.progress = 30
        self.task.save(update_fields=['status', 'progress'])
        view = TestCaseGenerationTaskViewSet()
        view.get_object = lambda: self.task

        response = view.cancel(SimpleNamespace(), task_id=self.task.task_id)

        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'cancelled')
        events = TestCaseGenerationTaskSerializer(self.task).data['generation_events']
        self.assertEqual(events[-1]['status'], 'cancelled')
        self.assertEqual(events[-1]['title'], '生成任务已取消')

    def test_review_loop_actions_are_not_exposed(self):
        action_paths = {
            action.url_path
            for action in TestCaseGenerationTaskViewSet.get_extra_actions()
        }

        self.assertNotIn('optimize-test-cases', action_paths)
        self.assertNotIn('review-test-cases', action_paths)
        self.assertNotIn('regenerate-from-review', action_paths)

    def test_low_score_completed_task_can_be_saved(self):
        self.task.is_saved_to_records = True
        self.task.save(update_fields=['is_saved_to_records'])
        view = TestCaseGenerationTaskViewSet()
        view.get_object = lambda: self.task

        response = view.save_to_records(SimpleNamespace(), task_id=self.task.task_id)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['already_saved'])

    def test_save_reports_unparsed_cases_without_successful_import(self):
        self.task.final_test_cases = (
            '| 模块 | 业务对象 | 主要内容 |\n'
            '|---|---|---|\n'
            '| 申请入口 | 借款申请 | 手机号、验证码、车牌号 |'
        )
        self.task.save(update_fields=['final_test_cases'])

        view = TestCaseGenerationTaskViewSet()
        view.get_object = lambda: self.task

        response = view.save_to_records(
            SimpleNamespace(data={}),
            task_id=self.task.task_id,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['saved_to_records'])
        self.assertEqual(response.data['imported_count'], 0)
        self.assertEqual(response.data['import_status'], 'not_parsed')
        self.assertIn('未解析出', response.data['import_error'])
        self.task.refresh_from_db()
        self.assertTrue(self.task.is_saved_to_records)

    def test_save_to_records_skips_cases_already_in_target_project(self):
        project = Project.objects.create(name='重复保存项目', owner=self.user)
        self.task.project = project
        self.task.final_test_cases = (
            '| 用例编号 | 测试模块 | 测试类型 | 测试场景 | 测试数据 | 前置条件 | 操作步骤 | 预期结果 | 优先级 | 需求编号 |\n'
            '|---|---|---|---|---|---|---|---|---|---|\n'
            '| TC-001 | 登录 | 功能测试 | 重复登录场景 | 用户名=a | 账号已注册 | 输入账号并登录 | 进入首页 | P1 | REQ-001 |'
        )
        self.task.save(update_fields=['project', 'final_test_cases'])
        TestCase.objects.create(
            project=project,
            author=self.user,
            title='  重复登录场景  ',
            steps='旧步骤',
            expected_result='旧结果',
        )
        view = TestCaseGenerationTaskViewSet()
        view.get_object = lambda: self.task

        response = view.save_to_records(
            SimpleNamespace(data={}),
            task_id=self.task.task_id,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['import_status'], 'duplicate')
        self.assertEqual(response.data['imported_count'], 0)
        self.assertEqual(response.data['skipped_count'], 1)
        self.assertEqual(TestCase.objects.filter(project=project).count(), 1)

    def test_low_score_task_reaches_selected_adoption_validation(self):
        view = TestCaseGenerationTaskViewSet()
        view.get_object = lambda: self.task

        response = view.batch_adopt_selected(
            SimpleNamespace(data={}),
            task_id=self.task.task_id,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error'], '没有提供要采纳的测试用例数据')

    def test_selected_adoption_skips_duplicate_titles(self):
        project = Project.objects.create(name='生成用例项目', owner=self.user)
        self.task.project = project
        self.task.save(update_fields=['project'])
        TestCase.objects.create(
            project=project,
            author=self.user,
            title='重复场景',
            steps='原步骤',
            expected_result='原结果',
        )
        view = TestCaseGenerationTaskViewSet()
        view.get_object = lambda: self.task

        response = view.batch_adopt_selected(
            SimpleNamespace(data={
                'test_cases': [
                    {
                        'title': '  重复场景  ',
                        'steps': '新步骤',
                        'expected_result': '新结果',
                    },
                    {
                        'title': '新增场景',
                        'steps': '新增步骤',
                        'expected_result': '新增结果',
                    },
                ],
            }),
            task_id=self.task.task_id,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['adopted_count'], 1)
        self.assertEqual(response.data['skipped_count'], 1)
        self.assertEqual(TestCase.objects.filter(project=project).count(), 2)


class GenerationBehaviorConfigTests(DjangoTestCase):
    def test_active_generation_config_overrides_request_behavior(self):
        config = GenerationConfig.objects.create(
            name='完整输出且关闭评审',
            default_output_mode='complete',
            enable_auto_review=False,
            review_timeout=1500,
            is_active=True,
        )

        behavior = TestCaseGenerationTaskViewSet.resolve_generation_behavior({
            'output_mode': 'stream',
            'use_reviewer_model': True,
        })

        self.assertEqual(behavior['config'], config)
        self.assertEqual(behavior['output_mode'], 'complete')
        self.assertFalse(behavior['enable_auto_review'])
        self.assertEqual(behavior['configured_review_timeout'], 1500)
        self.assertEqual(behavior['review_timeout'], 1500)

    def test_request_output_mode_is_used_only_without_generation_config(self):
        behavior = TestCaseGenerationTaskViewSet.resolve_generation_behavior({
            'output_mode': 'complete',
            'use_reviewer_model': False,
        })

        self.assertIsNone(behavior['config'])
        self.assertEqual(behavior['output_mode'], 'complete')
        self.assertFalse(behavior['enable_auto_review'])


# ============================================================
# get_openai_compatible_headers 测试
# ============================================================

class GetOpenaiCompatibleHeadersPositiveTests(DjangoTestCase):
    """get_openai_compatible_headers - 正向正常流程"""

    def test_normal_api_key_generates_correct_authorization(self):
        """
        用例标题：正常 api_key 生成正确的 Authorization 请求头
        前置：无
        步骤：1. 传入正常 api_key='sk-abc123' 调用 get_openai_compatible_headers
        预期结果：返回的 headers 中 Authorization 值为 'Bearer sk-abc123'
        """
        result = AIModelService.get_openai_compatible_headers('sk-abc123')
        self.assertEqual(result['Authorization'], 'Bearer sk-abc123')

    def test_normal_api_key_generates_correct_api_key_header(self):
        """
        用例标题：正常 api_key 生成正确的 api-key 请求头
        前置：无
        步骤：1. 传入正常 api_key='sk-abc123' 调用 get_openai_compatible_headers
        预期结果：返回的 headers 中 api-key 值为 'sk-abc123'
        """
        result = AIModelService.get_openai_compatible_headers('sk-abc123')
        self.assertEqual(result['api-key'], 'sk-abc123')

    def test_normal_api_key_generates_content_type(self):
        """
        用例标题：正常 api_key 生成正确的 Content-Type 请求头
        前置：无
        步骤：1. 传入正常 api_key='sk-abc123' 调用 get_openai_compatible_headers
        预期结果：返回的 headers 中 Content-Type 值为 'application/json'
        """
        result = AIModelService.get_openai_compatible_headers('sk-abc123')
        self.assertEqual(result['Content-Type'], 'application/json')

    def test_result_contains_exactly_three_headers(self):
        """
        用例标题：返回结果恰好包含三个请求头
        前置：无
        步骤：1. 传入正常 api_key 调用 get_openai_compatible_headers
        预期结果：返回字典恰好包含 3 个键
        """
        result = AIModelService.get_openai_compatible_headers('sk-abc123')
        self.assertEqual(len(result), 3)
        self.assertIn('Authorization', result)
        self.assertIn('api-key', result)
        self.assertIn('Content-Type', result)


class GetOpenaiCompatibleHeadersBoundaryTests(DjangoTestCase):
    """get_openai_compatible_headers - 边界极值"""

    def test_empty_string_api_key(self):
        """
        用例标题：空字符串 api_key 仍能生成请求头结构
        前置：无
        步骤：1. 传入 api_key='' 调用 get_openai_compatible_headers
        预期结果：返回 headers 中 Authorization 为 'Bearer '，api-key 为 ''
        """
        result = AIModelService.get_openai_compatible_headers('')
        self.assertEqual(result['Authorization'], 'Bearer ')
        self.assertEqual(result['api-key'], '')

    def test_api_key_with_special_characters(self):
        """
        用例标题：包含特殊字符的 api_key 原样保留
        前置：无
        步骤：1. 传入 api_key='sk-abc+123/XYZ==' 调用 get_openai_compatible_headers
        预期结果：返回 headers 中 Authorization 为 'Bearer sk-abc+123/XYZ=='，
                  api-key 为 'sk-abc+123/XYZ=='
        """
        special_key = 'sk-abc+123/XYZ=='
        result = AIModelService.get_openai_compatible_headers(special_key)
        self.assertEqual(result['Authorization'], f'Bearer {special_key}')
        self.assertEqual(result['api-key'], special_key)

    def test_api_key_with_unicode_characters(self):
        """
        用例标题：包含 Unicode 字符的 api_key 原样保留
        前置：无
        步骤：1. 传入 api_key='密钥123' 调用 get_openai_compatible_headers
        预期结果：返回 headers 中 Authorization 为 'Bearer 密钥123'
        """
        unicode_key = '密钥123'
        result = AIModelService.get_openai_compatible_headers(unicode_key)
        self.assertEqual(result['Authorization'], f'Bearer {unicode_key}')

    def test_api_key_with_whitespace(self):
        """
        用例标题：包含空白的 api_key 原样保留（不做 trim）
        前置：无
        步骤：1. 传入 api_key=' sk-abc ' 调用 get_openai_compatible_headers
        预期结果：返回 headers 中 api-key 为 ' sk-abc '（保留前后空格）
        """
        whitespace_key = ' sk-abc '
        result = AIModelService.get_openai_compatible_headers(whitespace_key)
        self.assertEqual(result['api-key'], whitespace_key)


class GetOpenaiCompatibleHeadersIllegalTests(DjangoTestCase):
    """get_openai_compatible_headers - 非法参数"""

    def test_none_api_key_converts_to_string(self):
        """
        用例标题：None 作为 api_key，f-string 转为 'None'，直接赋值保留 None
        前置：无
        步骤：1. 传入 api_key=None 调用 get_openai_compatible_headers
        预期结果：f-string 将 None 转为字符串 'None'，Authorization 为 'Bearer None'；
                  直接赋值的 api-key 保留原始 None 值
        """
        result = AIModelService.get_openai_compatible_headers(None)
        self.assertEqual(result['Authorization'], 'Bearer None')
        self.assertIsNone(result['api-key'])


# ============================================================
# build_openai_compatible_url 测试
# ============================================================

class BuildOpenaiCompatibleUrlPositiveTests(DjangoTestCase):
    """build_openai_compatible_url - 正向正常流程"""

    def test_base_url_with_chat_completions_endpoint(self):
        """
        用例标题：基础 URL + /chat/completions 端点自动添加 /v1 前缀
        前置：无
        步骤：1. 传入 base_url='https://api.openai.com', endpoint='/chat/completions'
        预期结果：返回 'https://api.openai.com/v1/chat/completions'
        """
        result = AIModelService.build_openai_compatible_url(
            'https://api.openai.com', '/chat/completions'
        )
        self.assertEqual(result, 'https://api.openai.com/v1/chat/completions')

    def test_base_url_with_models_endpoint(self):
        """
        用例标题：基础 URL + /models 端点自动添加 /v1 前缀
        前置：无
        步骤：1. 传入 base_url='https://api.openai.com', endpoint='/models'
        预期结果：返回 'https://api.openai.com/v1/models'
        """
        result = AIModelService.build_openai_compatible_url(
            'https://api.openai.com', '/models'
        )
        self.assertEqual(result, 'https://api.openai.com/v1/models')

    def test_url_already_contains_v1_version(self):
        """
        用例标题：URL 已包含版本号 /v1 不再添加 /v1
        前置：无
        步骤：1. 传入 base_url='https://api.openai.com/v1', endpoint='/chat/completions'
        预期结果：返回 'https://api.openai.com/v1/chat/completions'
        """
        result = AIModelService.build_openai_compatible_url(
            'https://api.openai.com/v1', '/chat/completions'
        )
        self.assertEqual(result, 'https://api.openai.com/v1/chat/completions')

    def test_url_already_contains_v4_version(self):
        """
        用例标题：URL 已包含版本号 /v4 不再添加 /v1
        前置：无
        步骤：1. 传入 base_url='https://api.example.com/v4', endpoint='/chat/completions'
        预期结果：返回 'https://api.example.com/v4/chat/completions'
        """
        result = AIModelService.build_openai_compatible_url(
            'https://api.example.com/v4', '/chat/completions'
        )
        self.assertEqual(result, 'https://api.example.com/v4/chat/completions')

    def test_url_already_ends_with_endpoint(self):
        """
        用例标题：URL 已以 endpoint 结尾直接返回
        前置：无
        步骤：1. 传入 base_url='https://api.openai.com/v1/chat/completions',
                 endpoint='/chat/completions'
        预期结果：返回 'https://api.openai.com/v1/chat/completions'
        """
        result = AIModelService.build_openai_compatible_url(
            'https://api.openai.com/v1/chat/completions', '/chat/completions'
        )
        self.assertEqual(result, 'https://api.openai.com/v1/chat/completions')

    def test_url_already_ends_with_chat_completions_stripped_and_rebuilt(self):
        """
        用例标题：URL 已以 /chat/completions 结尾去掉后重新构建
        前置：无
        步骤：1. 传入 base_url='https://api.openai.com/chat/completions',
                 endpoint='/models'
        预期结果：先去掉 /chat/completions 得到 'https://api.openai.com'，
                  再添加 /v1/models，返回 'https://api.openai.com/v1/models'
        """
        result = AIModelService.build_openai_compatible_url(
            'https://api.openai.com/chat/completions', '/models'
        )
        self.assertEqual(result, 'https://api.openai.com/v1/models')

    def test_url_already_ends_with_models_stripped_and_rebuilt(self):
        """
        用例标题：URL 已以 /models 结尾去掉后重新构建
        前置：无
        步骤：1. 传入 base_url='https://api.openai.com/models',
                 endpoint='/chat/completions'
        预期结果：先去掉 /models 得到 'https://api.openai.com'，
                  再添加 /v1/chat/completions，返回 'https://api.openai.com/v1/chat/completions'
        """
        result = AIModelService.build_openai_compatible_url(
            'https://api.openai.com/models', '/chat/completions'
        )
        self.assertEqual(result, 'https://api.openai.com/v1/chat/completions')

    def test_url_ends_with_endpoint_for_models(self):
        """
        用例标题：URL 已以 /models endpoint 结尾直接返回
        前置：无
        步骤：1. 传入 base_url='https://api.openai.com/v1/models',
                 endpoint='/models'
        预期结果：返回 'https://api.openai.com/v1/models'
        """
        result = AIModelService.build_openai_compatible_url(
            'https://api.openai.com/v1/models', '/models'
        )
        self.assertEqual(result, 'https://api.openai.com/v1/models')


class BuildOpenaiCompatibleUrlBoundaryTests(DjangoTestCase):
    """build_openai_compatible_url - 边界极值"""

    def test_trailing_slash_removed(self):
        """
        用例标题：URL 末尾有斜杠去除后处理
        前置：无
        步骤：1. 传入 base_url='https://api.openai.com/', endpoint='/chat/completions'
        预期结果：去除末尾斜杠后添加 /v1/chat/completions，
                  返回 'https://api.openai.com/v1/chat/completions'
        """
        result = AIModelService.build_openai_compatible_url(
            'https://api.openai.com/', '/chat/completions'
        )
        self.assertEqual(result, 'https://api.openai.com/v1/chat/completions')

    def test_trailing_slash_with_v1(self):
        """
        用例标题：URL 末尾 /v1/ 带斜杠去除后仍识别版本号
        前置：无
        步骤：1. 传入 base_url='https://api.openai.com/v1/', endpoint='/chat/completions'
        预期结果：去除末尾斜杠后识别到 /v1 版本号，
                  返回 'https://api.openai.com/v1/chat/completions'
        """
        result = AIModelService.build_openai_compatible_url(
            'https://api.openai.com/v1/', '/chat/completions'
        )
        self.assertEqual(result, 'https://api.openai.com/v1/chat/completions')

    def test_empty_string_base_url(self):
        """
        用例标题：空字符串 URL 构建
        前置：无
        步骤：1. 传入 base_url='', endpoint='/chat/completions'
        预期结果：空字符串去除斜杠后仍为空，添加 /v1/chat/completions，
                  返回 '/v1/chat/completions'
        """
        result = AIModelService.build_openai_compatible_url(
            '', '/chat/completions'
        )
        self.assertEqual(result, '/v1/chat/completions')

    def test_domain_only_url(self):
        """
        用例标题：URL 只有域名无路径
        前置：无
        步骤：1. 传入 base_url='https://api.openai.com', endpoint='/chat/completions'
        预期结果：直接添加 /v1/chat/completions，
                  返回 'https://api.openai.com/v1/chat/completions'
        """
        result = AIModelService.build_openai_compatible_url(
            'https://api.openai.com', '/chat/completions'
        )
        self.assertEqual(result, 'https://api.openai.com/v1/chat/completions')

    def test_url_with_v1_and_extra_path(self):
        """
        用例标题：URL 已包含 /v1/ 且有其他路径
        前置：无
        步骤：1. 传入 base_url='https://api.openai.com/v1/custom', endpoint='/chat/completions'
        预期结果：/v1 后面还有路径 /custom，不匹配版本号正则 /v(\\d+)/?$，
                  走默认逻辑添加 /v1，返回 'https://api.openai.com/v1/custom/v1/chat/completions'
        """
        result = AIModelService.build_openai_compatible_url(
            'https://api.openai.com/v1/custom', '/chat/completions'
        )
        self.assertEqual(result, 'https://api.openai.com/v1/custom/v1/chat/completions')

    def test_v2_version_number(self):
        """
        用例标题：URL 包含 /v2 版本号正确处理
        前置：无
        步骤：1. 传入 base_url='https://api.example.com/v2', endpoint='/chat/completions'
        预期结果：识别到 /v2 版本号，不再添加 /v1，
                  返回 'https://api.example.com/v2/chat/completions'
        """
        result = AIModelService.build_openai_compatible_url(
            'https://api.example.com/v2', '/chat/completions'
        )
        self.assertEqual(result, 'https://api.example.com/v2/chat/completions')

    def test_v99_version_number(self):
        """
        用例标题：URL 包含极大版本号 /v99 正确处理
        前置：无
        步骤：1. 传入 base_url='https://api.example.com/v99', endpoint='/models'
        预期结果：识别到 /v99 版本号，不再添加 /v1，
                  返回 'https://api.example.com/v99/models'
        """
        result = AIModelService.build_openai_compatible_url(
            'https://api.example.com/v99', '/models'
        )
        self.assertEqual(result, 'https://api.example.com/v99/models')

    def test_multiple_trailing_slashes(self):
        """
        用例标题：URL 末尾有多个斜杠只去除一个
        前置：无
        步骤：1. 传入 base_url='https://api.openai.com//', endpoint='/chat/completions'
        预期结果：rstrip('/') 去除所有末尾斜杠，得到 'https://api.openai.com'，
                  添加 /v1/chat/completions，返回 'https://api.openai.com/v1/chat/completions'
        """
        result = AIModelService.build_openai_compatible_url(
            'https://api.openai.com//', '/chat/completions'
        )
        self.assertEqual(result, 'https://api.openai.com/v1/chat/completions')

    def test_trailing_slash_after_known_endpoint(self):
        """
        用例标题：已知端点后带斜杠的处理
        前置：无
        步骤：1. 传入 base_url='https://api.openai.com/chat/completions/',
                 endpoint='/models'
        预期结果：去除末尾斜杠后以 /chat/completions 结尾，
                  去掉 /chat/completions 后添加 /v1/models，
                  返回 'https://api.openai.com/v1/models'
        """
        result = AIModelService.build_openai_compatible_url(
            'https://api.openai.com/chat/completions/', '/models'
        )
        self.assertEqual(result, 'https://api.openai.com/v1/models')


class BuildOpenaiCompatibleUrlReverseTests(DjangoTestCase):
    """build_openai_compatible_url - 反向异常入参"""

    def test_none_base_url_raises_error(self):
        """
        用例标题：None 作为 base_url 应抛出异常
        前置：无
        步骤：1. 传入 base_url=None, endpoint='/chat/completions'
        预期结果：抛出 AttributeError（None 没有 rstrip 方法）
        """
        with self.assertRaises(AttributeError):
            AIModelService.build_openai_compatible_url(None, '/chat/completions')

    def test_none_endpoint_raises_error(self):
        """
        用例标题：None 作为 endpoint 应抛出异常
        前置：无
        步骤：1. 传入 base_url='https://api.openai.com', endpoint=None
        预期结果：抛出 TypeError（endswith 参数不能为 None）
        """
        with self.assertRaises(TypeError):
            AIModelService.build_openai_compatible_url(
                'https://api.openai.com', None
            )


class BuildOpenaiCompatibleUrlIllegalTests(DjangoTestCase):
    """build_openai_compatible_url - 非法参数"""

    def test_numeric_base_url(self):
        """
        用例标题：数字类型作为 base_url 应抛出异常
        前置：无
        步骤：1. 传入 base_url=12345, endpoint='/chat/completions'
        预期结果：抛出 AttributeError（int 没有 rstrip 方法）
        """
        with self.assertRaises(AttributeError):
            AIModelService.build_openai_compatible_url(12345, '/chat/completions')

    def test_numeric_endpoint(self):
        """
        用例标题：数字类型作为 endpoint 应抛出异常
        前置：无
        步骤：1. 传入 base_url='https://api.openai.com', endpoint=12345
        预期结果：抛出 TypeError（endswith 参数类型不匹配）
        """
        with self.assertRaises(TypeError):
            AIModelService.build_openai_compatible_url(
                'https://api.openai.com', 12345
            )

    def test_both_empty_strings(self):
        """
        用例标题：base_url 和 endpoint 均为空字符串
        前置：无
        步骤：1. 传入 base_url='', endpoint=''
        预期结果：空字符串以空 endpoint 结尾（Python str.endswith('') 为 True），
                  直接返回空字符串 ''
        """
        result = AIModelService.build_openai_compatible_url('', '')
        self.assertEqual(result, '')
