# -*- coding: utf-8 -*-
from unittest import mock

from django.test import SimpleTestCase

from apps.app_automation.utils.appium_webview import (
    AppiumWebDriverError,
    AppiumWebViewClient,
    W3C_ELEMENT_KEY,
    xpath_literal,
)


class AppiumWebViewClientTests(SimpleTestCase):
    def test_xpath_literal_supports_mixed_quotes(self):
        literal = xpath_literal('他说"it\'s ok"')

        self.assertTrue(literal.startswith('concat('))
        self.assertIn('"\'"', literal)

    def test_connect_uses_non_relaunching_uiautomator_capabilities(self):
        http = mock.Mock()
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            'value': {'sessionId': 'session-1', 'capabilities': {}},
        }
        http.post.return_value = response
        client = AppiumWebViewClient(
            'http://127.0.0.1:4723',
            'emulator-5554',
            package_name='com.demo',
            remote_adb_host='host.docker.internal',
            adb_port=5037,
            chromedriver_executable_dir='/tmp/chromedrivers',
            session=http,
        )

        session_id = client.connect()

        self.assertEqual(session_id, 'session-1')
        payload = http.post.call_args.kwargs['json']
        caps = payload['capabilities']['alwaysMatch']
        self.assertEqual(caps['appium:udid'], 'emulator-5554')
        self.assertFalse(caps['appium:autoLaunch'])
        self.assertTrue(caps['appium:dontStopAppOnReset'])
        self.assertEqual(caps['appium:remoteAdbHost'], 'host.docker.internal')
        self.assertEqual(caps['appium:adbPort'], 5037)
        self.assertEqual(
            caps['appium:chromedriverExecutableDir'],
            '/tmp/chromedrivers',
        )

    def test_connect_supports_ios_xcuitest_webview_capabilities(self):
        http = mock.Mock()
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            'value': {'sessionId': 'ios-session', 'capabilities': {}},
        }
        http.post.return_value = response
        client = AppiumWebViewClient(
            'http://127.0.0.1:4723',
            '00008110-TEST',
            automation_name='XCUITest',
            package_name='com.demo.ios',
            platform_name='iOS',
            web_driver_agent_url='http://host.docker.internal:8100',
            session=http,
        )

        self.assertEqual(client.connect(), 'ios-session')
        caps = http.post.call_args.kwargs['json']['capabilities']['alwaysMatch']
        self.assertEqual(caps['platformName'], 'iOS')
        self.assertEqual(caps['appium:automationName'], 'XCUITest')
        self.assertEqual(caps['appium:bundleId'], 'com.demo.ios')
        self.assertEqual(
            caps['appium:webDriverAgentUrl'],
            'http://host.docker.internal:8100',
        )
        self.assertNotIn('appium:remoteAdbHost', caps)

    def test_switch_to_webview_prefers_package_context(self):
        client = AppiumWebViewClient(
            'http://127.0.0.1:4723',
            'emulator-5554',
            package_name='com.demo',
        )
        client.session_id = 'session-1'
        client.contexts = mock.Mock(return_value=[
            'NATIVE_APP',
            'WEBVIEW_com.other',
            'WEBVIEW_com.demo',
        ])
        client.switch_context = mock.Mock(side_effect=lambda name: name)

        selected, diagnostics = client.switch_to_webview(timeout=0.1)

        self.assertEqual(selected, 'WEBVIEW_com.demo')
        self.assertTrue(diagnostics['matched'])
        client.switch_context.assert_called_once_with('WEBVIEW_com.demo')

    def test_click_uses_css_before_other_dom_locators(self):
        client = AppiumWebViewClient(
            'http://127.0.0.1:4723',
            'emulator-5554',
        )
        client.session_id = 'session-1'
        client._request = mock.Mock(side_effect=[
            {W3C_ELEMENT_KEY: 'element-1'},
            None,
        ])

        diagnostics = client.click({
            'webview_css': '#submit',
            'text': '同意并申请',
        })

        first_call = client._request.call_args_list[0]
        self.assertEqual(first_call.kwargs['json']['using'], 'css selector')
        self.assertEqual(first_call.kwargs['json']['value'], '#submit')
        self.assertEqual(diagnostics['selected_strategy'], 'webview_css')
        self.assertEqual(diagnostics['action'], 'click')

    def test_set_checkbox_clicks_when_current_state_does_not_match(self):
        client = AppiumWebViewClient(
            'http://127.0.0.1:4723',
            'emulator-5554',
        )
        client.session_id = 'session-1'
        client.find_element = mock.Mock(return_value=(
            'checkbox-1',
            {'selected_strategy': 'webview_css'},
        ))
        client._resolve_checkbox_element = mock.Mock(return_value=(
            'checkbox-1',
            True,
        ))
        client._read_checkbox_state = mock.Mock(side_effect=[False, True])
        client._request = mock.Mock(return_value=None)

        result = client.set_checkbox(
            {'webview_css': '#agreement'},
            desired_state='checked',
        )

        client._request.assert_called_once_with(
            'POST',
            '/session/session-1/element/checkbox-1/click',
            json={},
        )
        self.assertFalse(result['before'])
        self.assertTrue(result['after'])
        self.assertTrue(result['clicked'])
        self.assertTrue(result['verified'])

    def test_set_checkbox_does_not_click_when_state_already_matches(self):
        client = AppiumWebViewClient(
            'http://127.0.0.1:4723',
            'emulator-5554',
        )
        client.session_id = 'session-1'
        client.find_element = mock.Mock(return_value=('checkbox-1', {}))
        client._resolve_checkbox_element = mock.Mock(return_value=(
            'checkbox-1',
            True,
        ))
        client._read_checkbox_state = mock.Mock(side_effect=[True, True])
        client._request = mock.Mock(return_value=None)

        result = client.set_checkbox({}, desired_state='checked')

        client._request.assert_not_called()
        self.assertFalse(result['clicked'])
        self.assertTrue(result['verified'])

    def test_set_checkbox_resolves_link_to_actual_checkbox_control(self):
        client = AppiumWebViewClient(
            'http://127.0.0.1:4723',
            'emulator-5554',
        )
        client.session_id = 'session-1'
        client.find_element = mock.Mock(return_value=('agreement-link', {}))
        client._resolve_checkbox_element = mock.Mock(return_value=(
            'agreement-checkbox',
            True,
        ))
        client._read_checkbox_state = mock.Mock(side_effect=[False, True])
        client._request = mock.Mock(return_value=None)

        result = client.set_checkbox(
            {'webview_text': '隐私政策'},
            desired_state='checked',
        )

        client._request.assert_called_once_with(
            'POST',
            '/session/session-1/element/agreement-checkbox/click',
            json={},
        )
        self.assertEqual(result['located_element_id'], 'agreement-link')
        self.assertEqual(result['element_id'], 'agreement-checkbox')
        self.assertTrue(result['control_resolved'])

    def test_set_checkbox_refuses_to_click_unresolved_link_or_text(self):
        client = AppiumWebViewClient(
            'http://127.0.0.1:4723',
            'emulator-5554',
        )
        client.session_id = 'session-1'
        client.find_element = mock.Mock(return_value=('agreement-link', {}))
        client._resolve_checkbox_element = mock.Mock(return_value=(
            'agreement-link',
            False,
        ))
        client._read_checkbox_state = mock.Mock(return_value=None)
        client._request = mock.Mock(return_value=None)

        with self.assertRaisesRegex(AppiumWebDriverError, '拒绝点击协议文字或链接'):
            client.set_checkbox(
                {'webview_text': '隐私政策'},
                desired_state='checked',
            )

        client._request.assert_not_called()

    def test_input_text_resolves_label_to_real_text_control(self):
        client = AppiumWebViewClient(
            'http://127.0.0.1:4723',
            'emulator-5554',
        )
        client.session_id = 'session-1'
        client.find_element = mock.Mock(return_value=(
            'address-label',
            {'selected_strategy': 'text'},
        ))
        client._resolve_text_input_element = mock.Mock(return_value=(
            'address-input',
            True,
        ))
        client._request = mock.Mock(return_value=None)

        result = client.input_text(
            {'webview_text': '详细地址'},
            '北京市海淀区',
            clear_first=True,
        )

        self.assertEqual(client._request.call_args_list[0].args[:2], (
            'POST',
            '/session/session-1/element/address-input/clear',
        ))
        self.assertEqual(client._request.call_args_list[1].args[:2], (
            'POST',
            '/session/session-1/element/address-input/value',
        ))
        self.assertEqual(result['located_element_id'], 'address-label')
        self.assertEqual(result['element_id'], 'address-input')
        self.assertTrue(result['control_resolved'])

    def test_input_text_refuses_unresolved_text_node(self):
        client = AppiumWebViewClient(
            'http://127.0.0.1:4723',
            'emulator-5554',
        )
        client.session_id = 'session-1'
        client.find_element = mock.Mock(return_value=(
            'address-label',
            {'selected_strategy': 'text'},
        ))
        client._resolve_text_input_element = mock.Mock(return_value=(
            'address-label',
            False,
        ))
        client._request = mock.Mock(return_value=None)

        with self.assertRaisesRegex(AppiumWebDriverError, '真实 input/textarea/textbox'):
            client.input_text({'webview_text': '详细地址'}, '北京市海淀区')

        client._request.assert_not_called()

    def test_resolve_checkbox_element_returns_dom_checkbox_descendant(self):
        client = AppiumWebViewClient(
            'http://127.0.0.1:4723',
            'emulator-5554',
        )
        client.session_id = 'session-1'
        client._request = mock.Mock(return_value={
            W3C_ELEMENT_KEY: 'agreement-checkbox',
        })

        element_id, resolved = client._resolve_checkbox_element('agreement-link')

        self.assertEqual(element_id, 'agreement-checkbox')
        self.assertTrue(resolved)
        request = client._request.call_args
        self.assertEqual(request.args[:2], (
            'POST',
            '/session/session-1/execute/sync',
        ))
        self.assertEqual(
            request.kwargs['json']['args'],
            [{W3C_ELEMENT_KEY: 'agreement-link'}],
        )
        self.assertIn('closest', request.kwargs['json']['script'])

    def test_read_checkbox_state_falls_back_to_aria_checked(self):
        client = AppiumWebViewClient(
            'http://127.0.0.1:4723',
            'emulator-5554',
        )
        client.session_id = 'session-1'
        client._request = mock.Mock(side_effect=[None, None, 'true'])

        state = client._read_checkbox_state('checkbox-1')

        self.assertTrue(state)
        self.assertEqual(client._request.call_count, 3)
        self.assertTrue(
            client._request.call_args.args[1].endswith('/attribute/aria-checked')
        )

    def test_execute_script_uses_w3c_sync_endpoint(self):
        client = AppiumWebViewClient(
            'http://127.0.0.1:4723',
            'emulator-5554',
        )
        client.session_id = 'session-1'
        client._request = mock.Mock(return_value={'title': 'TestHub'})

        result = client.execute_script('return {title: document.title}', [])

        self.assertEqual(result, {'title': 'TestHub'})
        client._request.assert_called_once_with(
            'POST',
            '/session/session-1/execute/sync',
            json={
                'script': 'return {title: document.title}',
                'args': [],
            },
        )
