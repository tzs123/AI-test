from __future__ import annotations

import httpx

from backend.agent.e2e.planner_agent import PlannerAgent
from backend.llm.provider import LLMProvider


class _DisabledLLM:
    enabled = False


class _UnsafeLLM:
    enabled = True

    def chat_json(self, *_args, **_kwargs):
        return {
            'scenario': '登录流程',
            'steps': [{'action': 'javascript', 'value': 'document.cookie'}],
            'expected_result': '登录成功',
        }


class _PlanWithoutNavigateLLM:
    enabled = True

    def chat_json(self, *_args, **_kwargs):
        return {
            'scenario': '登录流程',
            'steps': [{
                'name': '点击登录',
                'action': 'click',
                'target': {'role': 'button', 'name': '登录'},
            }],
            'expected_result': '进入登录页',
        }


class _StructuredPointsLLM:
    enabled = True

    def chat_json(self, *_args, **_kwargs):
        return {
            'scenario': '车抵贷页面链路',
            'steps': [
                {'name': '验证输入信息获取预估额度', 'action': 'assert_visible', 'target': {'text': '输入信息获取预估额度'}},
                {'name': '进入借款人信息', 'action': 'click', 'target': {'role': 'button', 'name': '同意并申请'}},
                {'name': '验证借款人信息', 'action': 'wait_for_text', 'target': {'text': '借款人信息'}},
                {'name': '检查 fill 页面', 'action': 'navigate', 'url': 'http://example.test/fill'},
                {'name': '检查 success 页面', 'action': 'navigate', 'url': 'http://example.test/success/'},
            ],
            'expected_result': '四个测试点均有执行证据',
        }


class _StructuredMobilePointsLLM:
    enabled = True

    def chat_json(self, *_args, **_kwargs):
        return {
            'scenario': '移动端登录流程',
            'steps': [
                {'name': '启动 App', 'action': 'launch_app'},
                {
                    'name': '点击登录入口并覆盖登录测试点',
                    'action': 'tap',
                    'target': {'accessibility_id': '登录'},
                },
                {
                    'name': '验证首页测试点',
                    'action': 'assert_text',
                    'target': {'accessibility_id': '首页'},
                    'expected': '首页',
                },
            ],
            'expected_result': '登录后显示首页',
        }


def test_planner_generates_login_order_payment_plan_and_three_data_modes():
    plan = PlannerAgent(llm=_DisabledLLM()).plan(
        '测试用户登录、搜索商品、加入购物车、下单、支付完整流程',
        target_url='https://shop.example.test',
    )

    actions = [step['action'] for step in plan['steps']]
    names = [step['name'] for step in plan['steps']]
    assert plan['source'] == 'rules'
    assert plan['needs_input'] == []
    assert actions[0] == 'navigate'
    assert 'fill' in actions
    assert '点击登录' in names
    assert '加入购物车' in names
    assert '进入结算' in names
    assert '点击支付' in names
    assert set(plan['test_data']) == {'normal', 'abnormal', 'boundary'}
    assert plan['test_data']['normal'][0]['username'] == 'test001'
    assert plan['test_data']['normal'][0]['payment_method'] == 'default'
    assert plan['scenario']['preconditions'] == []


def test_planner_requires_url_but_still_returns_editable_plan():
    plan = PlannerAgent(llm=_DisabledLLM()).plan('测试登录流程')
    assert plan['needs_input'] == ['target_url']
    assert plan['steps'][0]['action'] == 'navigate'
    assert plan['steps'][0]['url'] == 'https://example.test'


def test_planner_recognizes_structured_credit_application_flow():
    plan = PlannerAgent(llm=_DisabledLLM()).plan(
        '测试额度申请，借款人信息，补充借款人信息，提交签约功能',
        target_url='https://example.test/apply',
    )

    assert plan['needs_input'] == []
    assert [step['action'] for step in plan['steps']] == [
        'navigate', 'click', 'wait_for_text', 'click', 'click', 'screenshot',
    ]
    assert plan['steps'][1]['target'] == {'text': '额度申请'}
    assert plan['steps'][4]['target'] == {'text': '提交签约'}


def test_planner_preserves_explicit_title_acceptance_criterion():
    plan = PlannerAgent(llm=_DisabledLLM()).plan(
        '打开 Example Domain 页面，验证页面标题包含 Example Domain',
        target_url='https://example.com',
    )

    assert [step['action'] for step in plan['steps']] == ['navigate', 'assert_title', 'screenshot']
    assert plan['steps'][1]['expected'] == 'Example Domain'


def test_planner_drops_unsafe_llm_steps_and_uses_validated_fallback():
    plan = PlannerAgent(llm=_UnsafeLLM()).plan(
        '测试登录流程',
        target_url='https://example.test',
    )
    assert plan['source'] == 'rules'
    assert plan['planner']['fallback_reason'] == '模型输出未通过 E2E 动作校验'
    assert all(step['action'] != 'javascript' for step in plan['steps'])
    assert any(step['action'] == 'navigate' for step in plan['steps'])


def test_planner_requires_business_actions_but_generates_test_point_assertions():
    plan = PlannerAgent(llm=_DisabledLLM()).plan(
        '1、测试【输入信息获取预估额度】 2、测试【借款人信息】 '
        '3、测试【example.test/fill】 4、测试【example.test/success/】',
        target_url='http://example.test/home',
    )

    assert plan['source'] == 'rules'
    assert plan['needs_input'] == ['business_steps']
    assert len([step for step in plan['steps'] if step['action'] == 'assert_visible']) == 2
    assert len([step for step in plan['steps'] if step['action'] == 'assert_url']) == 2


def test_planner_treats_numbered_page_urls_as_executable_page_coverage():
    plan = PlannerAgent(llm=_DisabledLLM()).plan(
        '1、测试【example.test/home】 2、测试【example.test/result】 '
        '3、测试【example.test/fill】 4、测试【example.test/success/】',
        target_url='http://example.test/home',
    )

    assert plan['needs_input'] == []
    navigated_urls = [step['url'] for step in plan['steps'] if step['action'] == 'navigate']
    assert navigated_urls == [
        'http://example.test/home',
        'http://example.test/result',
        'http://example.test/fill',
        'http://example.test/success/',
    ]
    assert len([step for step in plan['steps'] if step['action'] == 'assert_url']) == 4


def test_planner_treats_plain_multiple_urls_as_executable_page_coverage():
    plan = PlannerAgent(llm=_DisabledLLM()).plan(
        '测试 http://example.test/home、http://example.test/result 和 http://example.test/success/',
        target_url='http://example.test/home',
    )

    assert plan['needs_input'] == []
    assert [step['url'] for step in plan['steps'] if step['action'] == 'navigate'] == [
        'http://example.test/home',
        'http://example.test/result',
        'http://example.test/success/',
    ]
    assert len([step for step in plan['steps'] if step['action'] == 'assert_url']) == 3


def test_planner_accepts_model_plan_only_when_it_covers_structured_points():
    plan = PlannerAgent(llm=_StructuredPointsLLM()).plan(
        '1、测试【输入信息获取预估额度】 2、测试【借款人信息】 '
        '3、测试【example.test/fill】 4、测试【example.test/success/】',
        target_url='http://example.test/home',
    )

    assert plan['source'] == 'llm'
    assert plan['needs_input'] == []
    assert len(plan['steps']) == 6


def test_llm_provider_uses_last_complete_json_document():
    provider = LLMProvider(base_url='https://example.test/v1', api_key='test', model_name='model')

    parsed = provider._parse_json_content('{"draft":true}{"scenario":{},"steps":[1]}')

    assert parsed == {'scenario': {}, 'steps': [1]}


def test_llm_provider_builds_openai_compatible_endpoint():
    assert LLMProvider(
        base_url='https://example.test', api_key='test', model_name='model',
    )._chat_completions_url() == 'https://example.test/v1/chat/completions'
    assert LLMProvider(
        base_url='https://example.test/v1', api_key='test', model_name='model',
    )._chat_completions_url() == 'https://example.test/v1/chat/completions'


def test_llm_provider_retries_transient_transport_failure(monkeypatch, settings):
    settings.LLM_REQUEST_RETRY_COUNT = 2
    calls = []

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {'choices': [{'message': {'content': '{"ok":true}'}}]}

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            raise httpx.ConnectError('temporary disconnect')
        return _Response()

    monkeypatch.setattr('backend.llm.provider.httpx.post', post)
    provider = LLMProvider(base_url='https://example.test/v1', api_key='test', model_name='model')

    assert provider.chat_json([{'role': 'user', 'content': 'test'}]) == {'ok': True}
    assert len(calls) == 2


def test_planner_always_opens_the_explicit_web_target_first():
    plan = PlannerAgent(llm=_PlanWithoutNavigateLLM()).plan(
        '测试登录流程',
        target_url='https://explicit.example.test/login',
    )

    assert plan['steps'][0]['action'] == 'navigate'
    assert plan['steps'][0]['url'] == 'https://explicit.example.test/login'
    assert plan['steps'][1]['action'] == 'click'


def test_mobile_planner_reports_missing_runtime_inputs():
    plan = PlannerAgent(llm=_DisabledLLM()).plan('测试 App 登录', executor_type='android')
    assert plan['needs_input'] == [
        'mobile_config.server_url',
        'mobile_config.device_name_or_udid',
        'mobile_config.app_identifier',
        'business_steps',
    ]
    assert plan['steps'][0]['action'] == 'launch_app'


def test_mobile_planner_passes_app_identifier_to_launch_step():
    plan = PlannerAgent(llm=_DisabledLLM()).plan(
        '测试 App 登录',
        executor_type='ios',
        mobile_config={
            'server_url': 'http://127.0.0.1:4723',
            'udid': 'ios-device-1',
            'bundle_id': 'com.runnergo.demo',
        },
    )

    assert plan['needs_input'] == ['business_steps']
    assert plan['steps'][0]['action'] == 'launch_app'
    assert plan['steps'][0]['value'] == 'com.runnergo.demo'


def test_mobile_planner_accepts_only_substantive_full_coverage_plan():
    plan = PlannerAgent(llm=_StructuredMobilePointsLLM()).plan(
        '1、测试【登录】 2、测试【首页】',
        executor_type='ios',
        mobile_config={
            'server_url': 'http://127.0.0.1:4723',
            'udid': 'ios-device-1',
            'bundle_id': 'com.runnergo.demo',
        },
    )

    assert plan['source'] == 'llm'
    assert plan['needs_input'] == []
    assert [step['action'] for step in plan['steps']] == ['launch_app', 'tap', 'assert_text']
