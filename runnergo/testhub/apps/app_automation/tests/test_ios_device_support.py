import os
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.app_automation.managers.ios_device_manager import (
    IOSDeviceManager,
    normalize_wda_url,
    validate_wda_url,
    wda_host_port,
)
from apps.app_automation.models import AppDevice
from apps.app_automation.serializers import AppDeviceSerializer
from apps.app_automation.utils.ios_flow import (
    ios_flow_should_start_from_home,
    ios_should_open_browser_start_url,
    strip_ios_browser_bootstrap_steps,
)
from apps.app_automation.utils.airtest_base import AirtestBase
from apps.app_automation.views.device_views import ACTION_RECORDING_PROCESSES, AppDeviceViewSet

User = get_user_model()


class IOSDeviceManagerTests(SimpleTestCase):
    def test_wda_url_helpers_accept_docker_host_name(self):
        url = normalize_wda_url('host.docker.internal:8100/')

        self.assertEqual(url, 'http://host.docker.internal:8100')
        self.assertTrue(validate_wda_url(url))
        self.assertEqual(wda_host_port(url), ('host.docker.internal', 8100))

    def test_probe_and_basic_operations_use_remote_wda(self):
        client = Mock()
        client.status.return_value = {
            'os': {'name': 'iOS', 'version': '17.5'},
            'deviceName': '测试 iPhone',
        }
        client.device_info.return_value = {'name': 'iPhone 15 Pro'}
        client.screenshot.return_value = b'png-data'
        client.source.return_value = '<AppiumAUT />'
        client.app_launch.return_value = {'value': True}

        manager = IOSDeviceManager('http://mac-host:8100')
        with patch.object(manager, '_get_client', return_value=client):
            probe = manager.probe()
            screenshot = manager.screenshot()
            source = manager.source()
            manager.tap(12, 34)
            manager.swipe(10, 20, 30, 40, 0.8)
            launch = manager.launch_app('com.example.app')

        self.assertEqual(probe['name'], 'iPhone 15 Pro')
        self.assertEqual(probe['ios_version'], '17.5')
        self.assertEqual(screenshot, b'png-data')
        self.assertEqual(source, '<AppiumAUT />')
        client.click.assert_called_once_with(12, 34)
        client.swipe.assert_called_once_with(10, 20, 30, 40, duration=0.8)
        self.assertEqual(launch['bundle_id'], 'com.example.app')


class IOSAirtestConnectionTests(SimpleTestCase):
    @patch('apps.app_automation.utils.airtest_base.init_device')
    def test_ios_setup_uses_wda_url_instead_of_adb(self, init_device_mock):
        airtest = AirtestBase(
            device_id='00008110-TEST-UDID',
            platform='ios',
            wda_url='http://host.docker.internal:8100',
            wda_bundle_id='com.example.WebDriverAgentRunner',
            username='tester',
        )

        connected = airtest.setup_airtest({
            'RETRY_COUNT': 1,
            'RETRY_INTERVAL': 0,
            'DEVICE_CONNECT_TIMEOUT': 2,
        })

        self.assertTrue(connected)
        init_device_mock.assert_called_once_with(
            platform='IOS',
            uuid='http://host.docker.internal:8100',
            udid='00008110-TEST-UDID',
            wda_bundle_id='com.example.WebDriverAgentRunner',
        )

    @patch('apps.app_automation.utils.airtest_base.home')
    def test_go_home_uses_airtest_home(self, home_mock):
        airtest = AirtestBase(
            device_id='00008110-TEST-UDID',
            platform='ios',
            username='tester',
        )
        airtest.is_connected = True

        self.assertTrue(airtest.go_home(settle_seconds=0))

        home_mock.assert_called_once_with()

    @patch('apps.app_automation.utils.airtest_base.G')
    def test_open_url_uses_wda_driver_on_ios(self, global_device_mock):
        airtest = AirtestBase(
            device_id='00008110-TEST-UDID',
            platform='ios',
            username='tester',
        )
        airtest.is_connected = True

        self.assertTrue(airtest.open_url('http://172.16.0.88:9527/home', settle_seconds=0))

        global_device_mock.DEVICE.driver.open_url.assert_called_once_with(
            'http://172.16.0.88:9527/home'
        )

    @patch('apps.app_automation.utils.airtest_base.G')
    def test_open_url_quotes_android_shell_argument(self, global_device_mock):
        airtest = AirtestBase(
            device_id='emulator-5554',
            platform='android',
            username='tester',
        )
        airtest.is_connected = True

        self.assertTrue(airtest.open_url(
            "https://example.test/path?next=a'b&channel=app",
            settle_seconds=0,
        ))

        global_device_mock.DEVICE.shell.assert_called_once_with(
            "am start -a android.intent.action.VIEW -d 'https://example.test/path?next=a'\"'\"'b&channel=app'"
        )

    @patch('apps.app_automation.utils.airtest_base.G')
    def test_open_url_rejects_non_http_scheme(self, global_device_mock):
        airtest = AirtestBase(
            device_id='emulator-5554',
            platform='android',
            username='tester',
        )
        airtest.is_connected = True

        self.assertFalse(airtest.open_url('intent://example.test/#Intent;end', settle_seconds=0))
        global_device_mock.DEVICE.shell.assert_not_called()

    def test_ios_browser_first_step_requires_home_reset(self):
        self.assertTrue(ios_flow_should_start_from_home([{
            'name': '智能点击浏览器',
            'config': {'selector': 'Safari_backup.png'},
        }]))

    def test_regular_first_step_does_not_require_home_reset(self):
        self.assertFalse(ios_flow_should_start_from_home([{
            'name': '智能点击手机号',
            'config': {'resource_id': '手机'},
        }]))

    def test_ios_safari_package_still_opens_configured_start_url(self):
        self.assertTrue(ios_should_open_browser_start_url(
            'ios',
            'com.apple.mobilesafari',
            [{'name': '智能点击浏览器', 'config': {'selector': 'Safari_backup.png'}}],
            'http://172.16.0.88:9527/home',
        ))

    def test_ios_without_package_opens_configured_start_url(self):
        self.assertTrue(ios_should_open_browser_start_url(
            'ios',
            '',
            [{'name': 'iOS 录制点击 2', 'config': {'resource_id': '手机'}}],
            'http://172.16.0.88:9527/home',
        ))

    def test_ios_native_package_does_not_use_browser_start_url(self):
        self.assertFalse(ios_should_open_browser_start_url(
            'ios',
            'com.runnergo.demo',
            [{'name': '智能点击浏览器', 'config': {'selector': 'Safari_backup.png'}}],
            'http://172.16.0.88:9527/home',
        ))

    def test_ios_browser_bootstrap_cleanup_removes_address_bar_input(self):
        flow = [
            {'name': '点击 Safari浏览器', 'type': 'self_heal_click', 'config': {'selector': 'Safari浏览器'}},
            {
                'name': '输入 搜索或输入网站名称',
                'type': 'self_heal_input',
                'config': {
                    'selector': 'TabBarItemTitle',
                    'value': '${TEST_URL}',
                    'locator_strategies': [{'type': 'text', 'value': '搜索或输入网站名称'}],
                },
            },
            {'name': '输入手机号', 'type': 'self_heal_input', 'config': {'selector': '手机'}},
        ]

        cleaned = strip_ios_browser_bootstrap_steps(
            flow,
            platform='ios',
            package_name='',
            browser_start_url='http://172.16.0.88:9527/home',
        )

        self.assertEqual([step['name'] for step in cleaned], ['输入手机号'])

    def test_ios_browser_bootstrap_cleanup_removes_address_bar_click_and_input(self):
        flow = [
            {'name': '点击 Safari浏览器', 'type': 'self_heal_click', 'config': {'selector': 'Safari浏览器'}},
            {
                'name': '点击 搜索或输入网站名称',
                'type': 'self_heal_click',
                'config': {
                    'selector': 'TabBarItemTitle',
                    'locator_strategies': [{'type': 'text', 'value': '搜索或输入网站名称'}],
                },
            },
            {
                'name': '输入 搜索或输入网站名称',
                'type': 'self_heal_input',
                'config': {
                    'selector': 'TabBarItemTitle',
                    'value': '${TEST_URL}',
                    'locator_strategies': [{'type': 'text', 'value': '搜索或输入网站名称'}],
                },
            },
            {'name': '输入手机号', 'type': 'self_heal_input', 'config': {'selector': '手机'}},
        ]

        cleaned = strip_ios_browser_bootstrap_steps(
            flow,
            platform='ios',
            package_name='',
            browser_start_url='http://172.16.0.88:9527/home',
        )

        self.assertEqual([step['name'] for step in cleaned], ['输入手机号'])

    def test_ios_browser_start_url_keeps_business_first_step(self):
        flow = [
            {'name': 'iOS 录制点击 2', 'type': 'click', 'config': {'resource_id': '手机'}},
            {'name': 'iOS 录制输入 3', 'type': 'input', 'config': {'value': '18877570423'}},
        ]

        cleaned = strip_ios_browser_bootstrap_steps(
            flow,
            platform='ios',
            package_name='',
            browser_start_url='http://172.16.0.88:9527/home',
        )

        self.assertEqual([step['name'] for step in cleaned], ['iOS 录制点击 2', 'iOS 录制输入 3'])

    def test_ios_browser_bootstrap_cleanup_handles_runtime_expanded_steps(self):
        flow = [
            {'name': '点击 Safari浏览器', 'type': 'self_heal_click', 'selector': 'Safari浏览器'},
            {
                'name': '点击 搜索或输入网站名称',
                'type': 'self_heal_click',
                'selector': 'TabBarItemTitle',
                'locator_strategies': [{'type': 'text', 'value': '搜索或输入网站名称'}],
            },
            {
                'name': '输入 搜索或输入网站名称',
                'type': 'self_heal_input',
                'selector': 'TabBarItemTitle',
                'value': 'http://172.16.0.88:9527/home',
                'locator_strategies': [{'type': 'text', 'value': '搜索或输入网站名称'}],
            },
            {'name': '输入手机号', 'type': 'self_heal_input', 'selector': '手机'},
        ]

        cleaned = strip_ios_browser_bootstrap_steps(
            flow,
            platform='ios',
            package_name='',
            browser_start_url='http://172.16.0.88:9527/home',
        )

        self.assertEqual([step['name'] for step in cleaned], ['输入手机号'])


class AndroidAirtestConnectionTests(SimpleTestCase):
    @patch.dict(os.environ, {'ADB_SERVER_SOCKET': 'tcp:host.docker.internal:5037'})
    @patch('apps.app_automation.utils.airtest_base.init_device')
    @patch('apps.app_automation.utils.airtest_base._resolve_adb_path', return_value='/usr/bin/adb')
    def test_remote_adb_uses_screencap_and_adb_touch(
        self,
        _resolve_adb_path_mock,
        init_device_mock,
    ):
        airtest = AirtestBase(
            device_id='emulator-5554',
            platform='android',
            username='tester',
        )

        connected = airtest.setup_airtest({
            'RETRY_COUNT': 1,
            'RETRY_INTERVAL': 0,
            'DEVICE_CONNECT_TIMEOUT': 2,
        })

        self.assertTrue(connected)
        init_device_mock.assert_called_once_with(
            platform='Android',
            uuid='emulator-5554',
            adb_path='/usr/bin/adb',
            cap_method='ADBCAP',
            touch_method='ADBTOUCH',
        )


class IOSDeviceApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='ios_tester', password='secret', is_staff=True,
        )
        self.factory = APIRequestFactory()

    def tearDown(self):
        ACTION_RECORDING_PROCESSES.clear()

    @patch('apps.app_automation.views.device_views.IOSDeviceManager.probe')
    def test_connect_registers_ios_device_and_serializes_platform_fields(self, probe_mock):
        probe_mock.return_value = {
            'name': 'iPhone 15 Pro',
            'ios_version': '17.5',
            'status': {'os': {'version': '17.5'}},
            'device_info': {'name': 'iPhone 15 Pro'},
        }
        request = self.factory.post('/devices/connect/', {
            'platform': 'ios',
            'device_id': '00008110-TEST-UDID',
            'wda_url': 'http://host.docker.internal:8100',
            'default_bundle_id': 'com.example.app',
        }, format='json')
        force_authenticate(request, user=self.user)

        response = AppDeviceViewSet.as_view({'post': 'connect'})(request)

        self.assertEqual(response.status_code, 200)
        device = AppDevice.objects.get(device_id='00008110-TEST-UDID')
        self.assertEqual(device.platform, 'ios')
        self.assertEqual(device.connection_type, 'ios_remote')
        self.assertEqual(device.ios_version, '17.5')
        self.assertEqual(device.default_bundle_id, 'com.example.app')
        serialized = AppDeviceSerializer(device).data
        self.assertEqual(serialized['platform_display'], 'iOS')
        self.assertEqual(serialized['os_version'], '17.5')

    @patch('apps.app_automation.views.device_views.get_ios_manager')
    def test_ios_mirror_action_recording_executes_and_returns_steps(self, get_manager):
        device = AppDevice.objects.create(
            device_id='IOS-RECORD-UDID',
            name='录制 iPhone',
            platform='ios',
            status='online',
            connection_type='ios_remote',
            wda_url='http://host.docker.internal:8140',
        )
        manager = Mock()
        manager.probe.return_value = {'online': True}
        manager.window_size.return_value = {'width': 400, 'height': 800}
        manager.source.return_value = (
            '<?xml version="1.0"?><AppiumAUT>'
            '<XCUIElementTypeButton type="XCUIElementTypeButton" name="login_button" '
            'label="登录" x="100" y="300" width="200" height="200" enabled="true" />'
            '</AppiumAUT>'
        )
        get_manager.return_value = manager

        start_request = self.factory.post('/devices/start-action-recording/', {}, format='json')
        force_authenticate(start_request, user=self.user)
        start_response = AppDeviceViewSet.as_view({'post': 'start_action_recording'})(
            start_request, pk=device.pk
        )

        record_request = self.factory.post('/devices/record-action/', {
            'type': 'click',
            'x': 600,
            'y': 1200,
            'source_resolution': {'width': 1200, 'height': 2400},
        }, format='json')
        force_authenticate(record_request, user=self.user)
        record_response = AppDeviceViewSet.as_view({'post': 'record_action'})(
            record_request, pk=device.pk
        )

        stop_request = self.factory.post('/devices/stop-action-recording/', {}, format='json')
        force_authenticate(stop_request, user=self.user)
        stop_response = AppDeviceViewSet.as_view({'post': 'stop_action_recording'})(
            stop_request, pk=device.pk
        )

        self.assertEqual(start_response.status_code, 200)
        self.assertEqual(start_response.data['data']['mode'], 'interactive')
        self.assertEqual(record_response.status_code, 200)
        manager.tap.assert_called_once_with(200, 400)
        self.assertEqual(stop_response.status_code, 200)
        self.assertEqual(stop_response.data['data']['step_count'], 1)
        step = stop_response.data['data']['steps'][0]
        self.assertEqual(step['type'], 'click')
        self.assertEqual(step['config']['selector'], '200,400')

    @patch('apps.app_automation.views.device_views.get_ios_manager')
    def test_ios_recorded_input_reuses_last_tapped_input_locator(self, get_manager):
        device = AppDevice.objects.create(
            device_id='IOS-RECORD-INPUT-UDID',
            name='录制输入 iPhone',
            platform='ios',
            status='online',
            connection_type='ios_remote',
            wda_url='http://host.docker.internal:8140',
        )
        manager = Mock()
        manager.probe.return_value = {'online': True}
        manager.window_size.return_value = {'width': 400, 'height': 800}
        manager.source.return_value = (
            '<?xml version="1.0"?><AppiumAUT>'
            '<XCUIElementTypeTextField type="XCUIElementTypeTextField" name="手机" '
            'label="手机" value="请输入手机号" x="160" y="380" width="180" height="60" '
            'enabled="true" />'
            '</AppiumAUT>'
        )
        get_manager.return_value = manager

        start_request = self.factory.post('/devices/start-action-recording/', {}, format='json')
        force_authenticate(start_request, user=self.user)
        AppDeviceViewSet.as_view({'post': 'start_action_recording'})(
            start_request, pk=device.pk
        )

        click_request = self.factory.post('/devices/record-action/', {
            'type': 'click',
            'x': 600,
            'y': 1200,
            'source_resolution': {'width': 1200, 'height': 2400},
        }, format='json')
        force_authenticate(click_request, user=self.user)
        AppDeviceViewSet.as_view({'post': 'record_action'})(click_request, pk=device.pk)

        input_request = self.factory.post('/devices/record-action/', {
            'type': 'input',
            'value': '18877570423',
            'send_enter': True,
            'clear_first': True,
        }, format='json')
        force_authenticate(input_request, user=self.user)
        input_response = AppDeviceViewSet.as_view({'post': 'record_action'})(
            input_request, pk=device.pk
        )

        stop_request = self.factory.post('/devices/stop-action-recording/', {}, format='json')
        force_authenticate(stop_request, user=self.user)
        stop_response = AppDeviceViewSet.as_view({'post': 'stop_action_recording'})(
            stop_request, pk=device.pk
        )

        self.assertEqual(input_response.status_code, 200)
        manager.tap.assert_called_once_with(200, 400)
        manager.input_text.assert_called_once_with('18877570423', enter=True)
        steps = stop_response.data['data']['steps']
        self.assertEqual(len(steps), 2)
        input_config = steps[1]['config']
        self.assertEqual(input_config['value'], '18877570423')
        self.assertEqual(input_config['selector'], '200,400')
        self.assertEqual(input_config['fingerprint']['resource_id'], '手机')
        self.assertEqual(input_config['fingerprint']['class_name'], 'XCUIElementTypeTextField')

    @patch('apps.app_automation.views.device_views.get_ios_manager')
    def test_ios_mirror_action_can_execute_without_recording_steps(self, get_manager):
        device = AppDevice.objects.create(
            device_id='IOS-OPERATE-UDID',
            name='只操作 iPhone',
            platform='ios',
            status='online',
            connection_type='ios_remote',
            wda_url='http://host.docker.internal:8140',
        )
        manager = Mock()
        manager.window_size.return_value = {'width': 400, 'height': 800}
        manager.source.return_value = '<AppiumAUT />'
        get_manager.return_value = manager

        record_request = self.factory.post('/devices/record-action/', {
            'type': 'click',
            'record': False,
            'x': 600,
            'y': 1200,
            'source_resolution': {'width': 1200, 'height': 2400},
        }, format='json')
        force_authenticate(record_request, user=self.user)
        record_response = AppDeviceViewSet.as_view({'post': 'record_action'})(
            record_request, pk=device.pk
        )

        self.assertEqual(record_response.status_code, 200)
        manager.tap.assert_called_once_with(200, 400)
        self.assertFalse(record_response.data['data']['recorded'])
        self.assertIsNone(record_response.data['data']['step'])
        self.assertNotIn(str(device.id), ACTION_RECORDING_PROCESSES)
