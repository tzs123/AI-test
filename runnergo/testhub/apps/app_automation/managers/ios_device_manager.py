# -*- coding: utf-8 -*-
"""iOS 真机管理。

RunnerGo 后端运行在 Linux 容器中，不能直接启动 Xcode/WDA。iPhone 由 Mac
主机上的 WebDriverAgent 提供远程 HTTP 服务，容器通过 wda_url 访问。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def normalize_wda_url(value: str) -> str:
    url = str(value or '').strip().rstrip('/')
    if not url:
        return ''
    if not url.startswith(('http://', 'https://')):
        url = f'http://{url}'
    return url


def validate_wda_url(value: str) -> bool:
    try:
        parsed = urlparse(normalize_wda_url(value))
        return parsed.scheme in {'http', 'https'} and bool(parsed.hostname)
    except Exception:
        return False


def wda_host_port(value: str) -> Tuple[str, int]:
    parsed = urlparse(normalize_wda_url(value))
    return parsed.hostname or '', parsed.port or (443 if parsed.scheme == 'https' else 8100)


class IOSDeviceManager:
    """通过远程 WebDriverAgent 管理 iOS 真机。"""

    def __init__(self, wda_url: str, timeout: int = 15):
        self.wda_url = normalize_wda_url(wda_url)
        self.timeout = timeout
        if not validate_wda_url(self.wda_url):
            raise ValueError('请输入有效的 WDA 地址，例如 http://host.docker.internal:8100')

    def _get_client(self):
        # facebook-wda 由 Airtest 依赖安装。延迟导入避免 Android-only 环境启动失败。
        import wda
        return wda.Client(self.wda_url)

    @staticmethod
    def _extract_version(status_data: Dict[str, Any]) -> str:
        os_info = status_data.get('os') or {}
        value = (
            os_info.get('version')
            or os_info.get('sdkVersion')
            or status_data.get('sdkVersion')
            or status_data.get('iosVersion')
        )
        return str(value or '')

    @staticmethod
    def _extract_name(status_data: Dict[str, Any], device_info: Dict[str, Any]) -> str:
        os_info = status_data.get('os') or {}
        return str(
            device_info.get('name')
            or device_info.get('deviceName')
            or status_data.get('deviceName')
            or os_info.get('name')
            or 'iPhone'
        )

    def probe(self) -> Dict[str, Any]:
        """验证 WDA 可达并返回尽可能完整的设备信息。"""
        client = self._get_client()
        status_data = client.status() or {}
        device_info = {}
        try:
            device_info = client.device_info() or {}
        except Exception as exc:
            # 某些 WDA 版本只有建立应用 session 后才开放 device/info。
            logger.debug('读取 iOS device_info 失败，继续使用 status: %s', exc)

        return {
            'online': True,
            'name': self._extract_name(status_data, device_info),
            'ios_version': self._extract_version(status_data),
            'status': status_data,
            'device_info': device_info,
        }

    def screenshot(self) -> bytes:
        return self._get_client().screenshot(format='raw')

    def source(self) -> str:
        return str(self._get_client().source(format='xml') or '')

    def window_size(self) -> Dict[str, int]:
        size = self._get_client().window_size()
        return {
            'width': int(getattr(size, 'width', 0) or size[0]),
            'height': int(getattr(size, 'height', 0) or size[1]),
        }

    def tap(self, x: int, y: int) -> Any:
        return self._get_client().click(int(x), int(y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> Any:
        return self._get_client().swipe(
            int(x1), int(y1), int(x2), int(y2), duration=float(duration)
        )

    def input_text(self, value: str, enter: bool = False) -> Dict[str, Any]:
        """Send text to the currently focused iOS input control."""
        client = self._get_client()
        text = str(value or '')
        response = None
        if text:
            if hasattr(client, 'send_keys'):
                response = client.send_keys(text)
            elif hasattr(client, 'keyboard'):
                response = client.keyboard.send_keys(text)
            else:
                raise RuntimeError('当前 WDA 客户端不支持 send_keys 输入')
        if enter:
            if hasattr(client, 'send_keys'):
                client.send_keys('\n')
            elif hasattr(client, 'keyboard'):
                client.keyboard.send_keys('\n')
            elif hasattr(client, 'press'):
                client.press('enter')
        return {
            'value_length': len(text),
            'enter': bool(enter),
            'response': getattr(response, 'value', response),
        }

    def launch_app(self, bundle_id: str) -> Dict[str, Any]:
        response = self._get_client().app_launch(bundle_id)
        return {
            'bundle_id': bundle_id,
            'response': getattr(response, 'value', response),
        }
