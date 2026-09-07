from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.app_automation.constants import DeviceStatus
from apps.app_automation.models import AppAgentTask, AppDevice, AppProject
from apps.app_automation.views.device_views import AppDeviceViewSet


User = get_user_model()


class DeviceBridgeApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='bridge_tester', password='secret', is_staff=True,
        )
        self.factory = APIRequestFactory()

    @patch('apps.app_automation.views.device_views.device_bridge_request')
    def test_scan_connected_marks_registered_devices(self, bridge_request):
        AppDevice.objects.create(
            device_id='ANDROID-USB-1',
            name='Pixel 9',
            platform='android',
            connection_type='usb',
        )
        bridge_request.return_value = {
            'devices': [{
                'platform': 'android',
                'device_id': 'ANDROID-USB-1',
                'name': 'Pixel 9',
                'can_connect': True,
                'issues': [],
            }],
            'warnings': [],
            'capabilities': {'adb_path': '/host/adb'},
        }
        request = self.factory.get('/devices/scan-connected/')
        force_authenticate(request, user=self.user)

        response = AppDeviceViewSet.as_view({'get': 'scan_connected'})(request)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['success'])
        self.assertTrue(response.data['devices'][0]['registered'])
        self.assertEqual(response.data['capabilities']['adb_path'], '/host/adb')
        bridge_request.assert_called_once_with('/devices/scan', timeout=30)

    def test_delete_offline_device_without_history_succeeds(self):
        device = AppDevice.objects.create(
            device_id='OFFLINE-ANDROID-1',
            name='Offline Android',
            platform='android',
            status=DeviceStatus.OFFLINE,
            connection_type='usb',
        )
        request = self.factory.delete(f'/devices/{device.pk}/')
        force_authenticate(request, user=self.user)

        response = AppDeviceViewSet.as_view({'delete': 'destroy'})(request, pk=device.pk)

        self.assertEqual(response.status_code, 204)
        self.assertFalse(AppDevice.objects.filter(pk=device.pk).exists())

    def test_delete_offline_device_with_agent_history_returns_conflict(self):
        device = AppDevice.objects.create(
            device_id='OFFLINE-ANDROID-2',
            name='Offline Android With History',
            platform='android',
            status=DeviceStatus.OFFLINE,
            connection_type='usb',
        )
        project = AppProject.objects.create(
            name='App Project',
            owner=self.user,
        )
        AppAgentTask.objects.create(
            user=self.user,
            project=project,
            device=device,
            goal='测试自动化',
            status='stopped',
        )
        request = self.factory.delete(f'/devices/{device.pk}/')
        force_authenticate(request, user=self.user)

        response = AppDeviceViewSet.as_view({'delete': 'destroy'})(request, pk=device.pk)

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.data['success'])
        self.assertIn('Agent任务 1 个', response.data['message'])
        self.assertTrue(AppDevice.objects.filter(pk=device.pk).exists())

    @patch('apps.app_automation.views.device_views.device_bridge_request')
    def test_connect_scanned_android_registers_host_adb_device(self, bridge_request):
        bridge_request.return_value = {
            'device': {
                'platform': 'android',
                'device_id': 'ANDROID-USB-2',
                'name': 'Pixel 10',
                'android_version': '16',
                'connection_type': 'usb',
                'adb_server_socket': 'tcp:host.docker.internal:5037',
            },
        }
        request = self.factory.post('/devices/connect-scanned/', {
            'platform': 'android',
            'device_id': 'ANDROID-USB-2',
        }, format='json')
        force_authenticate(request, user=self.user)

        response = AppDeviceViewSet.as_view({'post': 'connect_scanned'})(request)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['success'])
        device = AppDevice.objects.get(device_id='ANDROID-USB-2')
        self.assertEqual(device.platform, 'android')
        self.assertEqual(device.connection_type, 'usb')
        self.assertEqual(device.android_version, '16')
        self.assertEqual(
            device.device_specs['adb_server_socket'],
            'tcp:host.docker.internal:5037',
        )
        bridge_request.assert_called_once_with(
            '/devices/connect',
            {'platform': 'android', 'device_id': 'ANDROID-USB-2'},
            timeout=180,
        )

    @patch('apps.app_automation.views.device_views.device_bridge_request')
    def test_connect_scanned_local_emulator_removes_tcp_alias(self, bridge_request):
        alias = AppDevice.objects.create(
            device_id='host.docker.internal:5555',
            name='sdk_gphone64_arm64',
            platform='android',
            status='available',
            connection_type='remote_emulator',
            ip_address='host.docker.internal',
            port=5555,
        )
        bridge_request.return_value = {
            'device': {
                'platform': 'android',
                'device_id': 'emulator-5554',
                'name': 'sdk_gphone64_arm64',
                'android_version': '15',
                'connection_type': 'emulator',
                'adb_server_socket': 'tcp:host.docker.internal:5037',
            },
        }
        request = self.factory.post('/devices/connect-scanned/', {
            'platform': 'android',
            'device_id': 'emulator-5554',
        }, format='json')
        force_authenticate(request, user=self.user)

        response = AppDeviceViewSet.as_view({'post': 'connect_scanned'})(request)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(AppDevice.objects.filter(pk=alias.pk).exists())
        self.assertTrue(AppDevice.objects.filter(device_id='emulator-5554').exists())

    @patch('apps.app_automation.views.device_views.IOSDeviceManager.probe')
    @patch('apps.app_automation.views.device_views.device_bridge_request')
    def test_connect_scanned_ios_simulator_preserves_local_type(self, bridge_request, probe):
        bridge_request.return_value = {
            'device': {
                'platform': 'ios',
                'device_id': 'SIMULATOR-UDID',
                'name': 'RunnerGo iPhone 17 Pro',
                'ios_version': '26.5',
                'connection_type': 'emulator',
                'is_simulator': True,
                'wda_url': 'http://host.docker.internal:8165',
                'wda_bundle_id': '',
            },
        }
        probe.return_value = {
            'name': 'RunnerGo iPhone 17 Pro',
            'ios_version': '26.5',
            'status': {'os': {'version': '26.5'}},
            'device_info': {'name': 'RunnerGo iPhone 17 Pro'},
        }
        request = self.factory.post('/devices/connect-scanned/', {
            'platform': 'ios',
            'device_id': 'SIMULATOR-UDID',
        }, format='json')
        force_authenticate(request, user=self.user)

        response = AppDeviceViewSet.as_view({'post': 'connect_scanned'})(request)

        self.assertEqual(response.status_code, 200)
        device = AppDevice.objects.get(device_id='SIMULATOR-UDID')
        self.assertEqual(device.platform, 'ios')
        self.assertEqual(device.connection_type, 'emulator')
        self.assertEqual(device.wda_url, 'http://host.docker.internal:8165')
        self.assertTrue(device.device_specs['is_simulator'])

    @patch('apps.app_automation.views.device_views.get_ios_manager')
    def test_ios_tap_scales_screenshot_coordinate_to_wda_window(self, get_manager):
        device = AppDevice.objects.create(
            device_id='IOS-SIMULATOR-UDID',
            name='RunnerGo iPhone',
            platform='ios',
            status='online',
            connection_type='emulator',
            wda_url='http://host.docker.internal:8128',
        )
        manager = Mock()
        manager.window_size.return_value = {'width': 402, 'height': 874}
        get_manager.return_value = manager
        request = self.factory.post(f'/devices/{device.pk}/tap/', {
            'x': 100,
            'y': 200,
            'source_resolution': {'width': 804, 'height': 1748},
        }, format='json')
        force_authenticate(request, user=self.user)

        response = AppDeviceViewSet.as_view({'post': 'tap_device'})(request, pk=device.pk)

        self.assertEqual(response.status_code, 200)
        manager.tap.assert_called_once_with(50, 100)
        self.assertEqual(response.data['data']['x'], 50)
        self.assertEqual(response.data['data']['y'], 100)

    @patch('apps.app_automation.views.device_views.run_adb_text')
    @patch('apps.app_automation.views.device_views.get_device_ui_nodes')
    @patch('apps.app_automation.views.device_views.get_device_screen_size')
    @patch('apps.app_automation.views.device_views.ensure_device_connected')
    @patch('apps.app_automation.views.device_views.get_adb_path')
    def test_android_popup_select_text_executes_and_returns_replay_step(
        self,
        get_adb_path,
        ensure_device_connected,
        get_device_screen_size,
        get_device_ui_nodes,
        run_adb_text,
    ):
        device = AppDevice.objects.create(
            device_id='ANDROID-POPUP-1',
            name='Popup Android',
            platform='android',
            status='online',
            connection_type='usb',
        )
        get_adb_path.return_value = '/usr/bin/adb'
        ensure_device_connected.return_value = ('ANDROID-POPUP-1', '')
        get_device_screen_size.return_value = (1080, 1920)
        get_device_ui_nodes.return_value = [{
            'text': '北京市',
            'class_name': 'android.view.View',
            'visible': True,
            'enabled': True,
            'depth': 5,
            'bounds': {'x1': 360, 'y1': 1200, 'x2': 720, 'y2': 1280},
        }]
        run_adb_text.return_value = Mock(returncode=0, stdout='', stderr='')
        request = self.factory.post(f'/devices/{device.pk}/popup-select-text/', {
            'value': '北京市',
            'max_swipes': 0,
        }, format='json')
        force_authenticate(request, user=self.user)

        response = AppDeviceViewSet.as_view({'post': 'popup_select_text'})(
            request, pk=device.pk
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['success'])
        self.assertEqual(response.data['data']['step']['type'], 'popup_select_text')
        self.assertEqual(response.data['data']['step']['config']['value'], '北京市')
        self.assertIn('tap', run_adb_text.call_args.args)
