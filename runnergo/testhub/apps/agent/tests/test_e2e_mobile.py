from __future__ import annotations

import base64
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from apps.app_automation.models import AppDevice
from backend.agent.e2e.contracts import E2EContractError
from backend.agent.e2e.mobile_agent import MobileAgent, allocated_device
from backend.agent.e2e.mobile_executor import MobileExecutor, normalize_mobile_request


class _FakeDriver:
    def __init__(self):
        self.actions = []

    def launch_app(self):
        self.actions.append('launch')

    def activate_app(self, value):
        self.actions.append(('activate', value))

    def swipe(self, *args):
        self.actions.append(('swipe', args))

    def back(self):
        self.actions.append('back')

    def hide_keyboard(self):
        self.actions.append('hide_keyboard')

    def save_screenshot(self, path):
        Path(path).write_bytes(b'fake-png')
        self.actions.append(('screenshot', path))
        return True

    def start_recording_screen(self):
        self.actions.append('record-start')

    def stop_recording_screen(self):
        self.actions.append('record-stop')
        return base64.b64encode(b'fake-video').decode()

    def quit(self):
        self.actions.append('quit')


@override_settings(E2E_APPIUM_ALLOWED_HOSTS=['127.0.0.1'])
def test_mobile_contract_uses_platform_specific_w3c_capabilities():
    android = normalize_mobile_request({
        'platform': 'android',
        'server_url': 'http://127.0.0.1:4723',
        'steps': [{'action': 'launch_app'}],
    })
    ios = normalize_mobile_request({
        'platform': 'ios',
        'server_url': 'http://127.0.0.1:4723',
        'steps': [{'action': 'screenshot'}],
    })
    assert android['capabilities']['appium:automationName'] == 'UiAutomator2'
    assert ios['capabilities']['appium:automationName'] == 'XCUITest'


def test_mobile_contract_rejects_arbitrary_actions_and_untrusted_appium_host():
    with pytest.raises(E2EContractError, match='不支持'):
        normalize_mobile_request({
            'platform': 'android',
            'server_url': 'http://127.0.0.1:4723',
            'steps': [{'action': 'execute_script'}],
        })
    with pytest.raises(E2EContractError, match='受保护'):
        normalize_mobile_request({
            'platform': 'android',
            'server_url': 'http://10.0.0.8:4723',
            'steps': [{'action': 'launch_app'}],
        })


@override_settings(E2E_APPIUM_ALLOWED_HOSTS=['127.0.0.1'])
def test_mobile_executor_runs_fake_appium_session_and_saves_evidence(tmp_path):
    driver = _FakeDriver()
    executor = MobileExecutor(
        artifact_root=tmp_path,
        driver_factory=lambda _server, _request: driver,
        adb_path='/command/that/does/not/exist',
    )
    result = executor.run({
        'platform': 'android',
        'server_url': 'http://127.0.0.1:4723',
        'task_id': 'mobile-smoke',
        'record_video': True,
        'steps': [
            {'name': '启动', 'action': 'launch_app'},
            {'name': '滑动', 'action': 'swipe', 'value': {'start_x': 500, 'start_y': 800, 'end_x': 500, 'end_y': 200}},
            {'name': '返回', 'action': 'back'},
            {'name': '截图', 'action': 'screenshot'},
        ],
    })
    assert result['status'] == 'passed'
    assert result['summary'] == {'total': 4, 'executed': 4, 'passed': 4, 'failed': 0, 'skipped': 0}
    assert result['video']
    assert Path(result['video']).read_bytes() == b'fake-video'
    assert 'ADB logcat unavailable' in result['device_log']
    assert driver.actions[-1] == 'quit'


@pytest.mark.django_db
def test_device_allocation_is_atomic_and_releases_only_owned_lock():
    user = get_user_model().objects.create_user(username='mobile-owner', password='x')
    other = get_user_model().objects.create_user(username='mobile-other', password='x')
    device = AppDevice.objects.create(
        device_id='emulator-e2e-1',
        name='Pixel E2E',
        platform='android',
        status='available',
    )

    with allocated_device(device.device_id, platform='android', user=user) as allocated:
        allocated.refresh_from_db()
        assert allocated.status == 'locked'
        assert allocated.locked_by == user
        with pytest.raises(RuntimeError, match='其他用户'):
            with allocated_device(device.device_id, platform='android', user=other):
                pass

    device.refresh_from_db()
    assert device.status == 'available'
    assert device.locked_by is None


@pytest.mark.django_db
@override_settings(E2E_IOS_APPIUM_SERVER_URL='http://host.docker.internal:4723')
def test_mobile_agent_builds_ios_wda_capabilities_and_holds_device_lock():
    user = get_user_model().objects.create_user(username='ios-owner', password='x')
    device = AppDevice.objects.create(
        device_id='ios-e2e-udid',
        name='iPhone E2E',
        platform='ios',
        status='online',
        wda_url='http://host.docker.internal:8100',
        wda_bundle_id='com.runnergo.WebDriverAgentRunner',
        default_bundle_id='com.runnergo.demo',
    )
    captured = {}

    class _Executor:
        def __init__(self, **_kwargs):
            pass

        def run(self, payload):
            device.refresh_from_db()
            assert device.status == 'locked'
            captured.update(payload)
            return {'status': 'passed'}

    result = MobileAgent(executor_factory=_Executor).execute(
        user=user,
        platform='ios',
        mobile_config={
            'server_url': 'http://127.0.0.1:4723',
            'udid': device.device_id,
            'bundle_id': 'com.runnergo.demo',
        },
        steps=[{'action': 'launch_app'}],
    )
    device.refresh_from_db()
    assert result['status'] == 'passed'
    assert captured['server_url'] == 'http://host.docker.internal:4723'
    assert captured['capabilities']['appium:bundleId'] == 'com.runnergo.demo'
    assert captured['capabilities']['appium:webDriverAgentUrl'] == 'http://127.0.0.1:8100'
    assert captured['capabilities']['appium:updatedWDABundleId'] == device.wda_bundle_id
    assert device.status == 'available'
