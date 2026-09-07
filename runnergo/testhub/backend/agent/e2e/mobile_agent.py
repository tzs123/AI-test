from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from django.conf import settings
from django.db import transaction

from apps.app_automation.constants import DeviceStatus
from apps.app_automation.models import AppDevice

from .mobile_executor import MobileExecutor


class MobileAgent:
    """Appium agent that respects TestHub's shared device allocation."""

    def __init__(self, *, executor_factory: Callable[..., MobileExecutor] = MobileExecutor):
        self.executor_factory = executor_factory

    def execute(
        self,
        *,
        user,
        platform: str,
        mobile_config: dict[str, Any],
        steps: list[dict[str, Any]],
        task_id: str = '',
        trace_id: str = '',
        record_video: bool = True,
        on_step=None,
    ) -> dict[str, Any]:
        device_identifier = str(mobile_config.get('udid') or mobile_config.get('device_name') or '').strip()
        with allocated_device(device_identifier, platform=platform, user=user) as device:
            capabilities = self._capabilities(device, mobile_config, platform)
            server_url = mobile_config.get('server_url')
            if platform == 'ios':
                ios_server_url = getattr(settings, 'E2E_IOS_APPIUM_SERVER_URL', '')
                server_url = ios_server_url or server_url
                if ios_server_url and capabilities.get('appium:webDriverAgentUrl'):
                    capabilities['appium:webDriverAgentUrl'] = _mac_host_url(
                        capabilities['appium:webDriverAgentUrl']
                    )
            return self.executor_factory(on_step=on_step).run({
                'platform': platform,
                'server_url': server_url,
                'capabilities': capabilities,
                'steps': steps,
                'task_id': task_id,
                'trace_id': trace_id,
                'record_video': record_video,
                'no_reset': mobile_config.get('no_reset', True),
            })

    def _capabilities(self, device: AppDevice, config: dict[str, Any], platform: str) -> dict[str, Any]:
        caps = dict(config.get('capabilities') or {})
        caps['appium:udid'] = device.device_id
        caps['appium:deviceName'] = device.name or device.device_id
        app_path = config.get('app')
        if app_path:
            caps['appium:app'] = str(app_path)
        if platform == 'android':
            app_package = config.get('app_package') or device.default_bundle_id
            if app_package:
                caps['appium:appPackage'] = str(app_package)
            if config.get('app_activity'):
                caps['appium:appActivity'] = str(config['app_activity'])
        else:
            bundle_id = config.get('bundle_id') or device.default_bundle_id
            if bundle_id:
                caps['appium:bundleId'] = str(bundle_id)
            if device.wda_url:
                caps['appium:webDriverAgentUrl'] = device.wda_url
            if device.wda_bundle_id:
                caps['appium:updatedWDABundleId'] = device.wda_bundle_id
        return caps


def _mac_host_url(value: str) -> str:
    parsed = urlsplit(str(value or ''))
    if parsed.hostname != 'host.docker.internal':
        return str(value or '')
    netloc = '127.0.0.1'
    if parsed.port:
        netloc = f'{netloc}:{parsed.port}'
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


@contextmanager
def allocated_device(identifier: str, *, platform: str, user):
    if not identifier:
        raise RuntimeError('移动端 E2E 缺少 device_name 或 udid')
    with transaction.atomic():
        device = AppDevice.objects.select_for_update().filter(device_id=identifier).first()
        if device is None:
            device = AppDevice.objects.select_for_update().filter(name=identifier).first()
        if device is None:
            raise RuntimeError(f'设备不存在: {identifier}')
        if device.platform != platform:
            raise RuntimeError(f'设备平台不匹配: 期望 {platform}，实际 {device.platform}')
        if device.status == DeviceStatus.OFFLINE:
            raise RuntimeError(f'设备离线: {identifier}')
        if device.status == DeviceStatus.LOCKED and device.locked_by_id != user.pk:
            raise RuntimeError(f'设备已被其他用户锁定: {identifier}')
        owned_before = device.status == DeviceStatus.LOCKED and device.locked_by_id == user.pk
        if not owned_before:
            device.locked_by = user
            from django.utils import timezone
            device.locked_at = timezone.now()
            device.status = DeviceStatus.LOCKED
            device.save(update_fields=['locked_by', 'locked_at', 'status', 'updated_at'])
    try:
        yield device
    finally:
        if not owned_before:
            with transaction.atomic():
                current = AppDevice.objects.select_for_update().get(pk=device.pk)
                if current.status == DeviceStatus.LOCKED and current.locked_by_id == user.pk:
                    current.locked_by = None
                    current.locked_at = None
                    current.status = DeviceStatus.AVAILABLE
                    current.save(update_fields=['locked_by', 'locked_at', 'status', 'updated_at'])
