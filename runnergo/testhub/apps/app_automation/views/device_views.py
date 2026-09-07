# -*- coding: utf-8 -*-
"""APP设备管理视图"""
import base64
import copy
import json
import math
import os
import re
import struct
import subprocess
import threading
import time
import uuid
import urllib.error
import urllib.request
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from django_filters.rest_framework import DjangoFilterBackend
from django.conf import settings
from django.utils import timezone
from django.db.models.deletion import ProtectedError
import logging

from .test_case_views import AppPagination
from ..models import AppDevice, AppTestConfig
from ..serializers import AppDeviceSerializer
from ..managers.device_manager import DeviceManager
from ..managers.ios_device_manager import IOSDeviceManager, normalize_wda_url, wda_host_port
from ..constants import DeviceStatus
from ..utils.locator_helpers import (
    fingerprint_at_point,
    fingerprint_is_replay_safe,
    normalize_point,
    parse_ui_hierarchy,
)
from ..utils.popup_selector import PopupSelectionError, select_popup_text_path
from ..utils.recorded_steps import repair_recorded_input_steps

logger = logging.getLogger(__name__)

REMOTE_CONNECTION_TYPES = {'remote', 'remote_emulator'}
LOCAL_EMULATOR_ALIAS_HOSTS = {'host.docker.internal', '127.0.0.1', 'localhost'}
APP_PACKAGE_PATTERN = re.compile(r'^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$')
RECORDING_PROCESSES = {}
ACTION_RECORDING_PROCESSES = {}
ACTION_EVENT_PATTERN = re.compile(
    r'\[\s*(?P<time>\d+(?:\.\d+)?)\]\s+(?:(?:/dev/input/event\d+):\s+)?'
    r'(?P<type>EV_\w+)\s+'
    r'(?P<code>ABS_MT_POSITION_X|ABS_MT_POSITION_Y|ABS_X|ABS_Y|ABS_MT_TRACKING_ID|BTN_TOUCH|SYN_REPORT)'
    r'\s*(?P<value>[0-9a-fA-F]+)?'
)
ACTION_KEY_EVENT_PATTERN = re.compile(
    r'\[\s*(?P<time>\d+(?:\.\d+)?)\]\s+(?:(?:/dev/input/event\d+):\s+)?'
    r'EV_KEY\s+(?P<code>KEY_[A-Z0-9_]+)\s+(?P<state>DOWN|UP|REPEAT)'
)

KEY_TEXT_MAP = {
    **{f'KEY_{number}': str(number) for number in range(10)},
    **{f'KEY_{letter}': letter.lower() for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'},
    'KEY_SPACE': ' ',
    'KEY_DOT': '.',
    'KEY_COMMA': ',',
    'KEY_MINUS': '-',
    'KEY_EQUAL': '=',
    'KEY_SLASH': '/',
    'KEY_SEMICOLON': ';',
    'KEY_APOSTROPHE': "'",
    'KEY_LEFTBRACE': '[',
    'KEY_RIGHTBRACE': ']',
    'KEY_BACKSLASH': '\\',
    'KEY_GRAVE': '`',
}
SHIFTED_KEY_TEXT_MAP = {
    'KEY_1': '!', 'KEY_2': '@', 'KEY_3': '#', 'KEY_4': '$', 'KEY_5': '%',
    'KEY_6': '^', 'KEY_7': '&', 'KEY_8': '*', 'KEY_9': '(', 'KEY_0': ')',
    'KEY_MINUS': '_', 'KEY_EQUAL': '+', 'KEY_LEFTBRACE': '{',
    'KEY_RIGHTBRACE': '}', 'KEY_BACKSLASH': '|', 'KEY_SEMICOLON': ':',
    'KEY_APOSTROPHE': '"', 'KEY_COMMA': '<', 'KEY_DOT': '>',
    'KEY_SLASH': '?', 'KEY_GRAVE': '~',
}


def get_adb_path() -> str:
    """
    获取 ADB 路径：优先使用数据库配置，否则使用默认值 'adb'
    """
    try:
        from ..models import AppTestConfig
        config = AppTestConfig.objects.first()
        return config.adb_path if config else 'adb'
    except Exception as e:
        logger.warning(f"获取 ADB 配置失败，使用默认路径: {e}")
        return 'adb'


def is_ios_device(device: AppDevice) -> bool:
    return getattr(device, 'platform', 'android') == 'ios'


def get_ios_defaults():
    try:
        config = AppTestConfig.objects.first()
        if config:
            return {
                'wda_url': config.ios_wda_url or 'http://host.docker.internal:8100',
                'wda_bundle_id': config.ios_wda_bundle_id or '',
            }
    except Exception as exc:
        logger.warning('读取 iOS 配置失败，使用默认值: %s', exc)
    return {
        'wda_url': 'http://host.docker.internal:8100',
        'wda_bundle_id': '',
    }


def get_device_bridge_url() -> str:
    try:
        config = AppTestConfig.objects.first()
        if config and config.device_bridge_url:
            return config.device_bridge_url.rstrip('/')
    except Exception as exc:
        logger.warning('读取设备桥接服务配置失败: %s', exc)
    return 'http://host.docker.internal:8765'


def device_bridge_request(path: str, payload: dict = None, timeout: int = 30) -> dict:
    url = f"{get_device_bridge_url()}/{path.lstrip('/')}"
    data = None
    headers = {'Accept': 'application/json'}
    method = 'GET'
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
        method = 'POST'
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode('utf-8', errors='ignore')
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', errors='ignore')
        try:
            error_data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            error_data = {}
        message = error_data.get('message') or f'设备桥接服务返回 HTTP {exc.code}'
        detail = error_data.get('detail') or body
        if detail:
            logger.error('设备桥接服务请求失败: url=%s status=%s detail=%s', url, exc.code, detail)
        raise RuntimeError(message) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError('无法连接 Mac 设备桥接服务，请确认服务已启动') from exc


def get_ios_manager(device: AppDevice = None, wda_url: str = '') -> IOSDeviceManager:
    defaults = get_ios_defaults()
    resolved_url = normalize_wda_url(
        wda_url or (getattr(device, 'wda_url', '') if device else '') or defaults['wda_url']
    )
    return IOSDeviceManager(resolved_url)


def decode_adb_output(value) -> str:
    """把 ADB 的 stdout/stderr 转成可读文本，避免把二进制截图打进日志。"""
    if not value:
        return ''
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace').strip()
    return str(value).strip()


def adb_detail(result) -> str:
    detail = decode_adb_output(getattr(result, 'stderr', '')) or decode_adb_output(getattr(result, 'stdout', ''))
    return detail[-500:] if detail else ''


def run_adb_text(adb_path: str, *args, timeout: int = 10):
    return subprocess.run(
        [adb_path, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=timeout
    )


def mark_device_status(device: AppDevice, status_value: str):
    if device.status != status_value:
        device.status = status_value
        device.save(update_fields=['status', 'updated_at'])


def is_adb_device_connected(adb_path: str, serial: str):
    result = run_adb_text(adb_path, '-s', serial, 'get-state', timeout=5)
    return result.returncode == 0 and result.stdout.strip() == 'device', adb_detail(result)


def remote_device_address(device: AppDevice) -> str:
    if device.ip_address:
        return f'{device.ip_address}:{device.port or 5555}'
    return device.device_id


def ensure_screenshot_device_connected(device: AppDevice, adb_path: str):
    """
    截图前确认 ADB 真实连上设备。
    数据库里 online 不代表 adb server 当前已连接，所以远程设备会自动重连一次。
    """
    if is_ios_device(device):
        try:
            get_ios_manager(device).probe()
            mark_device_status(device, DeviceStatus.ONLINE)
            return device.device_id, ''
        except Exception as exc:
            mark_device_status(device, DeviceStatus.OFFLINE)
            return '', str(exc)

    serial = device.device_id
    connected, detail = is_adb_device_connected(adb_path, serial)
    if connected:
        mark_device_status(device, DeviceStatus.ONLINE)
        return serial, ''

    if device.connection_type in REMOTE_CONNECTION_TYPES:
        serial = remote_device_address(device)
        connect_result = run_adb_text(adb_path, 'connect', serial, timeout=20)
        connect_detail = adb_detail(connect_result)
        connected, state_detail = is_adb_device_connected(adb_path, serial)
        if connected:
            mark_device_status(device, DeviceStatus.ONLINE)
            logger.info(f"设备 {serial} ADB 重连成功: {connect_detail}")
            return serial, ''
        detail = state_detail or connect_detail or detail

    mark_device_status(device, DeviceStatus.OFFLINE)
    return '', detail


def ensure_device_connected(device: AppDevice, adb_path: str):
    return ensure_screenshot_device_connected(device, adb_path)


def device_not_connected_response(device: AppDevice, detail: str):
    if is_ios_device(device):
        message = 'iPhone 未连接，请检查 Mac 上的 WebDriverAgent、开发者模式、签名和 WDA 地址'
    else:
        message = (
            f'设备未连接，请确认设备已开启并可通过 adb connect {remote_device_address(device)} 连接'
            if device.connection_type in REMOTE_CONNECTION_TYPES
            else '设备未连接，请检查 USB/模拟器连接后重新发现设备'
        )
    return Response({
        'code': 400,
        'msg': message,
        'success': False,
        'detail': detail
    }, status=status.HTTP_400_BAD_REQUEST)


def validate_package_name(package_name: str) -> bool:
    return bool(package_name and APP_PACKAGE_PATTERN.match(package_name))


def merge_local_emulator_aliases(canonical_device: AppDevice) -> int:
    """合并同一模拟器的本地序列号和 host.docker.internal TCP 别名。"""
    if canonical_device.platform != 'android':
        return 0
    match = re.fullmatch(r'emulator-(\d+)', canonical_device.device_id or '')
    if not match:
        return 0

    alias_port = int(match.group(1)) + 1
    aliases = AppDevice.objects.filter(
        platform='android',
        connection_type='remote_emulator',
        ip_address__in=LOCAL_EMULATOR_ALIAS_HOSTS,
        port=alias_port,
    ).exclude(pk=canonical_device.pk)
    if canonical_device.name:
        aliases = aliases.filter(name=canonical_device.name)

    merged = 0
    for alias in aliases:
        alias.executions.update(device=canonical_device)
        alias.scheduled_tasks.update(device=canonical_device)
        logger.info(
            '合并重复模拟器记录: alias=%s canonical=%s',
            alias.device_id,
            canonical_device.device_id,
        )
        alias.delete()
        merged += 1
    return merged


def _scale_ios_recording_point(point: dict, source_resolution: dict, target_resolution: dict):
    source_width = max(1, int(source_resolution.get('width') or 0))
    source_height = max(1, int(source_resolution.get('height') or 0))
    target_width = max(1, int(target_resolution.get('width') or 0))
    target_height = max(1, int(target_resolution.get('height') or 0))
    x = max(0, min(source_width - 1, int(round(float(point.get('x') or 0)))))
    y = max(0, min(source_height - 1, int(round(float(point.get('y') or 0)))))
    return (
        max(0, min(target_width - 1, int(round(x * target_width / source_width)))),
        max(0, min(target_height - 1, int(round(y * target_height / source_height)))),
    )


def _ios_fingerprint_at_point(manager: IOSDeviceManager, resolution: dict, x: int, y: int):
    try:
        nodes = parse_ui_hierarchy(manager.source(), resolution)
        fingerprint = fingerprint_at_point(nodes, x, y)
        return fingerprint if fingerprint_is_replay_safe(fingerprint) else {}
    except Exception as exc:
        logger.debug('iOS 录制读取语义节点失败，使用归一化坐标: %s', exc)
        return {}


def _ios_recording_fingerprint_targets_input(fingerprint: dict) -> bool:
    class_name = str((fingerprint or {}).get('class_name') or '')
    return any(token in class_name for token in (
        'TextField',
        'SecureTextField',
        'SearchField',
        'XCUIElementTypeTextView',
    ))


def recording_media_path(device: AppDevice, filename: str):
    from ..media_urls import build_public_evidence_path

    relative_dir = os.path.join('app-automation', 'recordings', f'device_{device.id}')
    absolute_dir = os.path.join(settings.MEDIA_ROOT, relative_dir)
    os.makedirs(absolute_dir, exist_ok=True)
    relative_path = os.path.join(relative_dir, filename)
    absolute_path = os.path.join(settings.MEDIA_ROOT, relative_path)
    media_url = build_public_evidence_path(relative_path.replace(os.sep, '/'))
    return absolute_path, media_url


def action_recording_path(device: AppDevice, filename: str):
    from ..media_urls import build_public_evidence_path

    relative_dir = os.path.join('app-automation', 'action-recordings', f'device_{device.id}')
    absolute_dir = os.path.join(settings.MEDIA_ROOT, relative_dir)
    os.makedirs(absolute_dir, exist_ok=True)
    relative_path = os.path.join(relative_dir, filename)
    absolute_path = os.path.join(settings.MEDIA_ROOT, relative_path)
    media_url = build_public_evidence_path(relative_path.replace(os.sep, '/'))
    return absolute_path, media_url


def get_device_screen_size(adb_path: str, serial: str):
    result = run_adb_text(adb_path, '-s', serial, 'shell', 'wm', 'size', timeout=8)
    output = result.stdout or ''
    matches = re.findall(r'(?:Physical|Override) size:\s*(\d+)x(\d+)', output)
    if matches:
        # 存在 Override size 时它位于 Physical size 后面，代表当前实际坐标空间。
        width, height = matches[-1]
        width, height = int(width), int(height)
        try:
            rotation_result = run_adb_text(
                adb_path, '-s', serial, 'shell', 'dumpsys', 'input', timeout=8
            )
            rotation_match = re.search(r'SurfaceOrientation:\s*(\d+)', rotation_result.stdout or '')
            if rotation_match and int(rotation_match.group(1)) % 2 == 1:
                width, height = height, width
        except (subprocess.TimeoutExpired, OSError):
            pass
        return width, height
    return 0, 0


def get_png_dimensions(content: bytes):
    """从 PNG IHDR 读取截图尺寸，避免横屏时 wm size 仍返回竖屏物理尺寸。"""
    if content and len(content) >= 24 and content.startswith(b'\x89PNG\r\n\x1a\n'):
        return struct.unpack('>II', content[16:24])
    return 0, 0


def get_device_ui_nodes(adb_path: str, serial: str, resolution=None):
    """抓取当前原生 UI 树并返回精简节点。

    自绘、游戏或未暴露 accessibility 语义的页面可能返回空列表；截图功能不应
    因 UI 树不可用而失败，因此这里始终降级为空列表并记录原因。
    """
    remote_path = f'/data/local/tmp/runnergo-window-{uuid.uuid4().hex[:10]}.xml'
    try:
        dump_result = run_adb_text(
            adb_path, '-s', serial, 'shell', 'uiautomator', 'dump', '--compressed', remote_path,
            timeout=12
        )
        if dump_result.returncode != 0:
            logger.info(f"设备 {serial} UI 树抓取不可用: {adb_detail(dump_result)}")
            return []
        xml_result = run_adb_text(adb_path, '-s', serial, 'shell', 'cat', remote_path, timeout=8)
        if xml_result.returncode != 0 or not xml_result.stdout:
            logger.info(f"设备 {serial} UI 树读取失败: {adb_detail(xml_result)}")
            return []
        nodes = parse_ui_hierarchy(xml_result.stdout, resolution)
        return [
            node for node in nodes
            if node.get('resource_id') or node.get('content_desc') or node.get('text') or node.get('clickable')
        ][:1500]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.info(f"设备 {serial} UI 树抓取失败，继续使用图像/坐标定位: {exc}")
        return []
    finally:
        try:
            run_adb_text(adb_path, '-s', serial, 'shell', 'rm', '-f', remote_path, timeout=3)
        except Exception:
            pass


def _parse_axis_bounds(line: str):
    min_match = re.search(r'\bmin\s+(-?\d+)', line)
    max_match = re.search(r'\bmax\s+(-?\d+)', line)
    return (
        int(min_match.group(1)) if min_match else 0,
        int(max_match.group(1)) if max_match else 0,
    )


def get_touch_input_metadata(adb_path: str, serial: str):
    result = run_adb_text(adb_path, '-s', serial, 'shell', 'getevent', '-lp', timeout=10)
    metadata = {
        'event_device': '',
        'x_min': 0,
        'x_max': 0,
        'y_min': 0,
        'y_max': 0,
        'screen_width': 0,
        'screen_height': 0,
    }
    if result.returncode != 0:
        logger.warning(f"读取触摸设备元数据失败: {adb_detail(result)}")
        return metadata

    devices = []
    current = None
    for line in (result.stdout or '').splitlines():
        device_match = re.search(r'add device \d+:\s+(\S+)', line)
        if device_match:
            if current:
                devices.append(current)
            current = {
                'event_device': device_match.group(1),
                'has_x': False,
                'has_y': False,
                'x_min': 0,
                'x_max': 0,
                'y_min': 0,
                'y_max': 0,
            }
            continue
        if not current:
            continue
        if 'ABS_MT_POSITION_X' in line or 'ABS_X' in line:
            current['has_x'] = True
            current['x_min'], current['x_max'] = _parse_axis_bounds(line)
        elif 'ABS_MT_POSITION_Y' in line or 'ABS_Y' in line:
            current['has_y'] = True
            current['y_min'], current['y_max'] = _parse_axis_bounds(line)
    if current:
        devices.append(current)

    touch_device = next((item for item in devices if item.get('has_x') and item.get('has_y')), None)
    if touch_device:
        metadata.update({
            'event_device': touch_device.get('event_device') or '',
            'x_min': touch_device.get('x_min') or 0,
            'x_max': touch_device.get('x_max') or 0,
            'y_min': touch_device.get('y_min') or 0,
            'y_max': touch_device.get('y_max') or 0,
        })

    screen_width, screen_height = get_device_screen_size(adb_path, serial)
    metadata['screen_width'] = screen_width
    metadata['screen_height'] = screen_height
    return metadata


def _parse_event_value(raw: str):
    raw = (raw or '').strip()
    if not raw:
        return 0
    if re.match(r'^[0-9a-fA-F]+$', raw):
        return int(raw, 16)
    return int(raw)


def _keycode_to_text(code: str, shifted: bool = False):
    if shifted and code in SHIFTED_KEY_TEXT_MAP:
        return SHIFTED_KEY_TEXT_MAP[code]
    value = KEY_TEXT_MAP.get(code)
    if shifted and value and len(value) == 1 and value.isalpha():
        return value.upper()
    return value


def _scale_touch_coordinate(value: int, axis_min: int, axis_max: int, size: int):
    if size <= 0:
        return int(value)
    if axis_max > axis_min:
        ratio = (int(value) - axis_min) / float(axis_max - axis_min)
        return max(0, min(size - 1, int(round(ratio * (size - 1)))))
    return max(0, min(size - 1, int(value)))


def _gesture_to_step(gesture, index: int, metadata: dict):
    if not gesture:
        return None
    width = metadata.get('screen_width') or 0
    height = metadata.get('screen_height') or 0

    points = []
    for point in gesture:
        if point.get('x') is None or point.get('y') is None:
            continue
        points.append({
            'time': point.get('time') or 0,
            'x': _scale_touch_coordinate(point['x'], metadata.get('x_min') or 0, metadata.get('x_max') or 0, width),
            'y': _scale_touch_coordinate(point['y'], metadata.get('y_min') or 0, metadata.get('y_max') or 0, height),
        })
    if not points:
        return None

    start = points[0]
    end = points[-1]
    distance = math.hypot(end['x'] - start['x'], end['y'] - start['y'])
    duration = max(0.1, min(5.0, float(end['time'] - start['time']) if end['time'] and start['time'] else 0.2))
    source_resolution = {'width': width, 'height': height}
    if distance <= 25 and duration <= 1.0:
        normalized_position = normalize_point(end['x'], end['y'], source_resolution)
        return {
            'id': f"recorded_{uuid.uuid4().hex[:10]}",
            'type': 'click',
            'name': f'录制点击 {index}',
            'config': {
                'selector_type': 'pos',
                'selector': f"{end['x']},{end['y']}",
                'source_resolution': source_resolution,
                'normalized_position': normalized_position,
                'coordinate_mode': 'normalized' if normalized_position else 'absolute',
                'timeout': 5,
                'wait_for_stable': True,
            }
        }

    direction = 'up'
    dx = end['x'] - start['x']
    dy = end['y'] - start['y']
    if abs(dx) > abs(dy):
        direction = 'right' if dx > 0 else 'left'
    else:
        direction = 'down' if dy > 0 else 'up'
    normalized_start = normalize_point(start['x'], start['y'], source_resolution)
    normalized_end = normalize_point(end['x'], end['y'], source_resolution)
    return {
        'id': f"recorded_{uuid.uuid4().hex[:10]}",
        'type': 'swipe',
        'name': f'录制滑动 {index}',
        'config': {
            'selector_type': 'pos',
            'selector': '',
            'direction': direction,
            'start': f"{start['x']},{start['y']}",
            'end': f"{end['x']},{end['y']}",
            'source_resolution': source_resolution,
            'normalized_start': normalized_start,
            'normalized_end': normalized_end,
            'coordinate_mode': 'normalized' if normalized_start and normalized_end else 'absolute',
            'duration': round(duration, 2),
        }
    }


def _input_value_from_nodes(nodes, preferred_resource_id: str = ''):
    """读取当前获得焦点的输入框文本，Chrome 地址栏作为可靠兜底。"""
    candidates = []
    for node in nodes or []:
        text_value = str(node.get('text') or '').strip()
        if not text_value:
            continue
        resource_id = str(node.get('resource_id') or '')
        class_name = str(node.get('class_name') or '')
        priority = 20
        if preferred_resource_id and resource_id == preferred_resource_id and node.get('focused'):
            priority = 0
        elif node.get('focused') and class_name.endswith('EditText'):
            priority = 1
        elif node.get('focused'):
            priority = 5
        elif preferred_resource_id and resource_id == preferred_resource_id:
            priority = 10
        elif resource_id == 'com.android.chrome:id/url_bar':
            priority = 12
        else:
            continue
        candidates.append((priority, text_value, resource_id))
    if not candidates:
        return '', ''
    candidates.sort(key=lambda item: item[0])
    _, value, resource_id = candidates[0]
    return value, resource_id


def _capture_recorded_paste(recording: dict, paste_event: dict):
    """Ctrl+V 后抓取输入框实际文本，避免只能从 getevent 得到字母 V。"""
    adb_path = recording.get('adb_path')
    serial = recording.get('device_serial')
    metadata = recording.get('metadata') or {}
    preferred_resource_id = paste_event.get('resource_id') or ''
    deadline = time.monotonic() + 2.5

    # 给 Android/Chrome 一点时间先完成粘贴，再读取 UI 树。
    time.sleep(0.12)
    while time.monotonic() < deadline:
        nodes = get_device_ui_nodes(
            adb_path,
            serial,
            {'width': metadata.get('screen_width') or 0, 'height': metadata.get('screen_height') or 0},
        )
        after_value, resource_id = _input_value_from_nodes(nodes, preferred_resource_id)
        before_value = paste_event.get('before_value') or ''
        pasted_value = after_value
        if before_value and after_value.startswith(before_value):
            pasted_value = after_value[len(before_value):]

        if pasted_value and pasted_value != before_value:
            with recording['paste_lock']:
                paste_event['value'] = pasted_value
                paste_event['after_value'] = after_value
                paste_event['resource_id'] = resource_id or preferred_resource_id
            return
        time.sleep(0.15)

    logger.warning(
        "未能读取 Ctrl+V 的实际粘贴内容: device=%s, resource_id=%s",
        serial,
        preferred_resource_id,
    )


def _record_action_stream(process, raw_path: str, metadata: dict, recording: dict):
    """持续落盘 getevent 输出，并在每个手势开始时绑定最近的 UI 树。"""
    active = False
    current_x = None
    current_y = None
    captured_current_gesture = False
    ctrl_pressed = False
    try:
        with open(raw_path, 'w', encoding='utf-8') as raw_file:
            for line in iter(process.stdout.readline, ''):
                raw_file.write(line)
                raw_file.flush()
                key_match = ACTION_KEY_EVENT_PATTERN.search(line)
                if key_match:
                    code = key_match.group('code')
                    state = key_match.group('state')
                    if code in {'KEY_LEFTCTRL', 'KEY_RIGHTCTRL'}:
                        ctrl_pressed = state != 'UP'
                    elif code == 'KEY_V' and state == 'DOWN' and ctrl_pressed:
                        with recording['ui_lock']:
                            latest_nodes = list(recording.get('latest_ui_nodes') or [])
                        before_value, resource_id = _input_value_from_nodes(latest_nodes)
                        paste_event = {
                            'time': float(key_match.group('time')),
                            'value': '',
                            'before_value': before_value,
                            'resource_id': resource_id,
                        }
                        with recording['paste_lock']:
                            recording['paste_events'].append(paste_event)
                        paste_thread = threading.Thread(
                            target=_capture_recorded_paste,
                            args=(recording, paste_event),
                            daemon=True,
                            name=f"app-action-paste-{len(recording['paste_events'])}",
                        )
                        recording['paste_threads'].append(paste_thread)
                        paste_thread.start()
                    continue
                match = ACTION_EVENT_PATTERN.search(line)
                if not match:
                    continue
                code = match.group('code')
                value_raw = match.group('value') or ''
                if code in {'ABS_MT_POSITION_X', 'ABS_X'} and value_raw:
                    current_x = _parse_event_value(value_raw)
                elif code in {'ABS_MT_POSITION_Y', 'ABS_Y'} and value_raw:
                    current_y = _parse_event_value(value_raw)
                elif code == 'ABS_MT_TRACKING_ID':
                    if value_raw.lower() == 'ffffffff':
                        active = False
                    else:
                        active = True
                        captured_current_gesture = False
                elif code == 'BTN_TOUCH':
                    active = bool(_parse_event_value(value_raw or '0'))
                    if active:
                        captured_current_gesture = False
                elif (
                    code == 'SYN_REPORT' and active and not captured_current_gesture
                    and current_x is not None and current_y is not None
                ):
                    width = metadata.get('screen_width') or 0
                    height = metadata.get('screen_height') or 0
                    screen_x = _scale_touch_coordinate(
                        current_x, metadata.get('x_min') or 0, metadata.get('x_max') or 0, width
                    )
                    screen_y = _scale_touch_coordinate(
                        current_y, metadata.get('y_min') or 0, metadata.get('y_max') or 0, height
                    )
                    with recording['ui_lock']:
                        nodes = list(recording.get('latest_ui_nodes') or [])
                    fingerprint = fingerprint_at_point(nodes, screen_x, screen_y)
                    recording['gesture_fingerprints'].append({
                        'fingerprint': fingerprint,
                        'position': {'x': screen_x, 'y': screen_y},
                        'captured_at': timezone.now().isoformat(),
                    })
                    captured_current_gesture = True
    except Exception as exc:
        logger.exception(f"操作录制流处理失败: {exc}")
    finally:
        if process.stdout:
            try:
                process.stdout.close()
            except Exception:
                pass


def _poll_recording_ui(adb_path: str, serial: str, metadata: dict, recording: dict):
    """录制期间低频刷新 UI 树，供触摸流无阻塞地取最近页面状态。"""
    resolution = {
        'width': metadata.get('screen_width') or 0,
        'height': metadata.get('screen_height') or 0,
    }
    stop_event = recording['stop_event']
    while not stop_event.wait(0.8):
        nodes = get_device_ui_nodes(adb_path, serial, resolution)
        if nodes:
            with recording['ui_lock']:
                recording['latest_ui_nodes'] = nodes
                recording['latest_ui_captured_at'] = timezone.now().isoformat()


def parse_action_recording(raw_path: str, metadata: dict, gesture_fingerprints=None, paste_events=None):
    timeline = []
    active = False
    current_x = None
    current_y = None
    current_points = []
    last_point = None
    event_order = 0
    gesture_index = 0

    def finish_gesture():
        nonlocal current_points, last_point, event_order, gesture_index
        if current_points:
            timeline.append({
                'kind': 'gesture',
                'time': current_points[0].get('time') or 0,
                'order': event_order,
                'gesture': current_points,
                'gesture_index': gesture_index,
            })
            event_order += 1
            gesture_index += 1
        current_points = []
        last_point = None

    try:
        with open(raw_path, 'r', encoding='utf-8', errors='ignore') as raw_file:
            for line in raw_file:
                key_match = ACTION_KEY_EVENT_PATTERN.search(line)
                if key_match:
                    timeline.append({
                        'kind': 'key',
                        'time': float(key_match.group('time')),
                        'order': event_order,
                        'code': key_match.group('code'),
                        'state': key_match.group('state'),
                    })
                    event_order += 1
                    continue
                match = ACTION_EVENT_PATTERN.search(line)
                if not match:
                    continue
                timestamp = float(match.group('time'))
                code = match.group('code')
                value_raw = match.group('value') or ''

                if code in {'ABS_MT_POSITION_X', 'ABS_X'} and value_raw:
                    current_x = _parse_event_value(value_raw)
                elif code in {'ABS_MT_POSITION_Y', 'ABS_Y'} and value_raw:
                    current_y = _parse_event_value(value_raw)
                elif code == 'ABS_MT_TRACKING_ID':
                    if value_raw.lower() == 'ffffffff':
                        active = False
                        finish_gesture()
                    else:
                        active = True
                        current_points = []
                        last_point = None
                elif code == 'BTN_TOUCH':
                    value = _parse_event_value(value_raw or '0')
                    if value:
                        active = True
                        current_points = []
                        last_point = None
                    else:
                        active = False
                        finish_gesture()
                elif code == 'SYN_REPORT' and active and current_x is not None and current_y is not None:
                    point = {'time': timestamp, 'x': current_x, 'y': current_y}
                    if last_point is None or point['x'] != last_point['x'] or point['y'] != last_point['y']:
                        current_points.append(point)
                        last_point = point
        finish_gesture()
    except FileNotFoundError:
        logger.warning(f"操作录制文件不存在: {raw_path}")

    steps = []
    gesture_fingerprints = gesture_fingerprints or []
    paste_events = sorted(paste_events or [], key=lambda item: item.get('time') or 0)
    paste_index = 0
    input_buffer = []
    input_count = 0
    shift_pressed = False
    ctrl_pressed = False
    caps_lock = False
    input_target_config = None

    def flush_input(send_enter=False):
        nonlocal input_buffer, input_count, input_target_config
        if not input_buffer:
            return
        input_count += 1
        config = {
            'value': ''.join(input_buffer),
            'send_enter': bool(send_enter),
        }
        if input_target_config:
            config.update(input_target_config)
        steps.append({
            'id': f"recorded_{uuid.uuid4().hex[:10]}",
            'type': 'input',
            'name': f'录制输入 {input_count}',
            'config': config,
        })
        input_buffer = []
        input_target_config = None

    for item in sorted(timeline, key=lambda value: (value.get('time') or 0, value.get('order') or 0)):
        if item.get('kind') == 'gesture':
            flush_input()
            recorded_gesture_index = item.get('gesture_index', 0)
            step = _gesture_to_step(
                item.get('gesture') or [],
                recorded_gesture_index + 1,
                metadata,
            )
            if not step:
                continue
            if step.get('type') == 'click' and recorded_gesture_index < len(gesture_fingerprints):
                fingerprint = gesture_fingerprints[recorded_gesture_index].get('fingerprint') or {}
                if fingerprint_is_replay_safe(fingerprint):
                    step['config']['fingerprint'] = fingerprint
                    step['config']['fingerprint_version'] = fingerprint.get('version', 1)
            steps.append(step)
            if step.get('type') == 'click':
                input_target_config = {
                    key: value
                    for key, value in (step.get('config') or {}).items()
                    if key in {
                        'selector_type', 'selector', 'source_resolution', 'normalized_position',
                        'coordinate_mode', 'timeout', 'wait_for_stable', 'fingerprint',
                        'fingerprint_version',
                    }
                }
            else:
                input_target_config = None
            continue

        code = item.get('code') or ''
        state = item.get('state') or ''
        if code in {'KEY_LEFTCTRL', 'KEY_RIGHTCTRL'}:
            ctrl_pressed = state != 'UP'
            continue
        if code in {'KEY_LEFTSHIFT', 'KEY_RIGHTSHIFT'}:
            shift_pressed = state != 'UP'
            continue
        if state != 'DOWN':
            continue
        if code == 'KEY_CAPSLOCK':
            caps_lock = not caps_lock
            continue
        if code == 'KEY_BACKSPACE':
            if input_buffer:
                input_buffer.pop()
            continue
        if code in {'KEY_ENTER', 'KEY_KPENTER'}:
            flush_input(send_enter=True)
            continue
        if code == 'KEY_TAB':
            flush_input()
            continue
        if code == 'KEY_V' and ctrl_pressed:
            paste_value = ''
            if paste_index < len(paste_events):
                paste_value = str(paste_events[paste_index].get('value') or '')
                paste_index += 1
            if paste_value:
                input_buffer.append(paste_value)
            else:
                logger.warning('检测到 Ctrl+V，但没有抓取到剪贴板内容，已忽略字母 V')
            continue
        text_value = _keycode_to_text(code, shifted=shift_pressed)
        if caps_lock and text_value and len(text_value) == 1 and text_value.isalpha():
            text_value = text_value.swapcase()
        if text_value is not None:
            input_buffer.append(text_value)
    flush_input()
    return steps


SEARCH_HINTS = ('search', 'query', 'keyword', 'kw', '搜索', '查询', '关键词')
SUBMIT_HINTS = ('submit', 'confirm', 'ok', 'done', 'go', 'search', 'query', '提交', '确定', '查询', '搜索')
INPUT_HINTS = ('input', 'edit', 'text', 'field', 'keyword', 'phone', '输入', '编辑', '文本', '搜索')


def _compact_text(value, max_length: int = 48) -> str:
    value = re.sub(r'\s+', ' ', str(value or '')).strip()
    if not value:
        return ''
    return value if len(value) <= max_length else f'{value[:max_length - 1]}...'


def _resource_name(value: str) -> str:
    value = str(value or '').strip()
    if not value:
        return ''
    value = value.split('/')[-1].split(':')[-1]
    value = value.replace('_', ' ').replace('-', ' ')
    return _compact_text(value)


def _step_fingerprint(step: dict) -> dict:
    return (step.get('config') or {}).get('fingerprint') or {}


def _step_target_label(step: dict) -> str:
    config = step.get('config') or {}
    fingerprint = _step_fingerprint(step)
    for key in ('text', 'content_desc', 'placeholder', 'resource_id'):
        value = fingerprint.get(key)
        if value:
            return _resource_name(value) if key == 'resource_id' else _compact_text(value)
    selector = config.get('selector') or config.get('start') or ''
    return _compact_text(selector) if selector else '当前控件'


def _target_keyword(step: dict) -> str:
    config = step.get('config') or {}
    fingerprint = _step_fingerprint(step)
    values = [
        fingerprint.get('text'),
        fingerprint.get('content_desc'),
        fingerprint.get('placeholder'),
        fingerprint.get('resource_id'),
        fingerprint.get('class_name'),
        config.get('selector'),
    ]
    return ' '.join(str(item or '').lower() for item in values)


def _is_search_related(step: dict) -> bool:
    keyword = _target_keyword(step)
    return any(hint in keyword for hint in SEARCH_HINTS)


def _is_input_target(step: dict) -> bool:
    keyword = _target_keyword(step)
    fingerprint = _step_fingerprint(step)
    class_name = str(fingerprint.get('class_name') or '').lower()
    return 'edittext' in class_name or 'textfield' in class_name or any(hint in keyword for hint in INPUT_HINTS)


def _is_submit_target(step: dict) -> bool:
    keyword = _target_keyword(step)
    fingerprint = _step_fingerprint(step)
    class_name = str(fingerprint.get('class_name') or '').lower()
    return 'button' in class_name or any(hint in keyword for hint in SUBMIT_HINTS)


def _input_values(steps) -> list:
    values = []
    for step in steps:
        if step.get('type') != 'input':
            continue
        value = _compact_text((step.get('config') or {}).get('value'), 32)
        if value:
            values.append(value)
    return values


def _describe_ai_action(step: dict, index: int) -> dict:
    step_type = step.get('type') or 'unknown'
    config = step.get('config') or {}
    target = _step_target_label(step)
    action = ''
    expected = ''

    if step_type == 'click':
        if _is_input_target(step):
            action = f'点击{target}'
            expected = '输入焦点进入目标控件'
        elif _is_submit_target(step):
            action = f'点击{target}'
            expected = '系统响应本次操作并进入下一状态'
        else:
            action = f'点击{target}'
            expected = '目标控件被触发'
    elif step_type == 'input':
        value = _compact_text(config.get('value'), 32)
        action = f'输入"{value}"' if value else '输入测试数据'
        if config.get('send_enter'):
            action = f'{action}并回车'
        expected = '输入内容被正确展示或提交'
    elif step_type == 'swipe':
        direction = config.get('direction') or ''
        direction_text = {
            'up': '向上滑动',
            'down': '向下滑动',
            'left': '向左滑动',
            'right': '向右滑动',
        }.get(direction, '滑动页面')
        action = direction_text
        expected = '页面内容按手势方向滚动'
    else:
        action = step.get('name') or f'执行录制动作 {index}'
        expected = '动作执行成功'

    return {
        'order': index,
        'step_id': step.get('id') or '',
        'type': step_type,
        'action': action,
        'target': target,
        'input_value': _compact_text(config.get('value'), 32) if step_type == 'input' else '',
        'expected': expected,
        'evidence': {
            'has_semantic_locator': bool(_step_fingerprint(step)),
            'coordinate_mode': config.get('coordinate_mode') or '',
        },
    }


def build_ai_recording_enhancement(steps, device: AppDevice = None) -> dict:
    """把底层录制动作整理成可审查的业务测试场景。"""
    steps = list(steps or [])
    actions = [_describe_ai_action(step, index + 1) for index, step in enumerate(steps)]
    input_values = _input_values(steps)
    search_flow = any(_is_search_related(step) for step in steps) and bool(input_values)
    submit_flow = any(_is_submit_target(step) for step in steps)

    if search_flow:
        scenario = '搜索商品'
        goal = f'验证输入"{input_values[-1]}"后可以完成搜索并返回结果'
    elif input_values and submit_flow:
        scenario = '表单提交'
        goal = '验证用户录制的输入和提交路径可以正常完成'
    elif any(step.get('type') == 'swipe' for step in steps):
        scenario = '浏览页面内容'
        goal = '验证页面浏览和关键操作路径可回放'
    elif steps:
        scenario = 'APP 关键流程'
        goal = '验证录制的用户操作路径可稳定回放'
    else:
        scenario = '空录制'
        goal = '未识别到可转换的用户操作'

    test_steps = [{
        'order': 1,
        'action': '打开APP',
        'expected': '进入应用首页或当前业务起点',
    }]
    for index, action in enumerate(actions, start=2):
        test_steps.append({
            'order': index,
            'action': action['action'],
            'expected': action['expected'],
        })
    if search_flow:
        test_steps.append({
            'order': len(test_steps) + 1,
            'action': '校验搜索结果',
            'expected': '结果列表或结果页包含与搜索词相关的内容',
        })
    elif steps:
        test_steps.append({
            'order': len(test_steps) + 1,
            'action': '校验流程结果',
            'expected': '页面停留在符合业务预期的成功状态',
        })

    semantic_count = sum(1 for step in steps if _step_fingerprint(step))
    confidence = 0.45
    if steps:
        confidence += min(0.35, semantic_count / max(1, len(steps)) * 0.35)
    if input_values:
        confidence += 0.1
    if search_flow or submit_flow:
        confidence += 0.1

    return {
        'enabled': True,
        'name': 'AI Test Recorder',
        'scenario': scenario,
        'goal': goal,
        'confidence': round(min(confidence, 0.98), 2),
        'device': {
            'id': getattr(device, 'device_id', '') if device else '',
            'name': getattr(device, 'name', '') if device else '',
            'platform': getattr(device, 'platform', '') if device else '',
        },
        'recorded_actions': actions,
        'test_steps': test_steps,
        'assertions': [
            item['expected']
            for item in test_steps
            if item['action'].startswith('校验')
        ],
        'notes': [
            'AI 摘要基于录制动作、输入内容和可访问性语义生成，保存前建议人工确认业务命名。',
            '低语义定位步骤会继续保留原始坐标和归一化坐标作为回放兜底。',
        ],
    }


def _action_recording_payload(device: AppDevice, recording: dict = None) -> dict:
    recording = recording or ACTION_RECORDING_PROCESSES.get(str(device.id))
    is_recording = bool(recording)
    data = {
        'device_id': device.device_id,
        'platform': device.platform,
        'recording': is_recording,
        'can_stop': is_recording,
        'started_at': recording.get('started_at') if recording else '',
        'step_count': len(recording.get('steps') or []) if recording else 0,
        'steps': list(recording.get('steps') or []) if recording else [],
    }
    if recording:
        if recording.get('platform') == 'ios':
            data.update({
                'mode': 'interactive',
                'screen': recording.get('metadata') or {},
            })
        else:
            metadata = recording.get('metadata') or {}
            data.update({
                'mode': 'device',
                'event_device': recording.get('event_device') or '',
                'filename': recording.get('filename') or '',
                'raw_url': recording.get('raw_url') or '',
                'screen': {
                    'width': metadata.get('screen_width') or 0,
                    'height': metadata.get('screen_height') or 0,
                },
            })
    return data


def _requested_step_name(request, default_name: str) -> str:
    name = str(request.data.get('name') or '').strip()
    return name[:100] if name else default_name


def _merge_recorded_step_overrides(generated_steps, request_steps=None):
    if request_steps is None or not isinstance(request_steps, list):
        return generated_steps
    if not request_steps:
        return []

    merged = []
    overrides_by_id = {
        str(step.get('id')): step
        for step in request_steps
        if isinstance(step, dict) and step.get('id')
    }

    if overrides_by_id:
        generated_by_id = {
            str(step.get('id')): step
            for step in generated_steps
            if isinstance(step, dict) and step.get('id')
        }
        for override in request_steps:
            if not isinstance(override, dict):
                continue
            step = generated_by_id.get(str(override.get('id')))
            if not step:
                continue
            updated = {**step}
            name = str(override.get('name') or '').strip()
            if name:
                updated['name'] = name[:100]
            merged.append(updated)
        return merged

    for index, step in enumerate(generated_steps[:len(request_steps)]):
        override = request_steps[index] if isinstance(request_steps[index], dict) else {}
        override = override or {}
        updated = {**step}
        name = str(override.get('name') or '').strip()
        if name:
            updated['name'] = name[:100]
        merged.append(updated)
    return merged


def _repair_action_recording_steps(steps):
    result = repair_recorded_input_steps(steps, reject_unfixed=True)
    if result.problems:
        messages = '；'.join(
            item.get('message') or item.get('reason') or '输入步骤缺少稳定定位'
            for item in result.problems
        )
        raise ValueError(f'录制步骤存在不可回放的输入定位：{messages}')
    return result.steps, result.repairs


class AppDeviceViewSet(viewsets.ModelViewSet):
    """APP设备管理 ViewSet"""
    queryset = AppDevice.objects.all()
    serializer_class = AppDeviceSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = AppPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['status', 'connection_type', 'platform']
    search_fields = ['device_id', 'name']

    ADMIN_ACTIONS = {
        'create', 'update', 'partial_update', 'destroy',
        'scan_connected', 'connect_scanned', 'discover', 'connect', 'disconnect',
    }

    def get_object(self):
        device = super().get_object()
        # 对设备产生输入、录屏、截图或启动应用等副作用的动作必须先取得租约。
        if (
            self.request.method == 'POST'
            and self.action not in {'lock', 'unlock'} | self.ADMIN_ACTIONS
            and not (self.request.user.is_staff or self.request.user.is_superuser)
            and device.locked_by_id != self.request.user.pk
        ):
            raise PermissionDenied('请先锁定设备后再执行控制操作')
        return device

    def destroy(self, request, *args, **kwargs):
        """删除设备。被 Agent 历史引用的设备返回明确业务错误，避免抛出 500。"""
        device = self.get_object()
        try:
            self.perform_destroy(device)
        except ProtectedError:
            agent_task_count = device.agent_tasks.count()
            matrix_cell_count = device.agent_matrix_cells.count()
            references = {
                'agent_tasks': agent_task_count,
                'agent_matrix_cells': matrix_cell_count,
            }
            detail_parts = []
            if agent_task_count:
                detail_parts.append(f'Agent任务 {agent_task_count} 个')
            if matrix_cell_count:
                detail_parts.append(f'兼容性矩阵记录 {matrix_cell_count} 个')
            detail = '，'.join(detail_parts) or '历史记录'
            return Response({
                'success': False,
                'message': f'设备已被{detail}引用，无法直接删除。请先删除关联 Agent 任务后重试。',
                'references': references,
            }, status=status.HTTP_409_CONFLICT)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=['get'], url_path='scan-connected')
    def scan_connected(self, request):
        """扫描连接到 Mac 的 Android/iPhone 真机和已启动模拟器。"""
        try:
            result = device_bridge_request('/devices/scan', timeout=30)
            devices = result.get('devices') or []
            existing = {
                (device.platform, device.device_id): device
                for device in AppDevice.objects.filter(
                    device_id__in=[item.get('device_id') for item in devices if item.get('device_id')]
                )
            }
            for item in devices:
                registered = existing.get((item.get('platform'), item.get('device_id')))
                item['registered'] = bool(registered)
                item['registered_device_id'] = registered.id if registered else None
                item['registered_status'] = registered.status if registered else ''
            return Response({
                'success': True,
                'message': f'扫描到 {len(devices)} 台设备',
                'devices': devices,
                'warnings': result.get('warnings') or [],
                'capabilities': result.get('capabilities') or {},
            })
        except Exception as exc:
            logger.exception('扫描电脑连接设备失败')
            return Response({
                'success': False,
                'message': str(exc),
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    @action(detail=False, methods=['post'], url_path='connect-scanned')
    def connect_scanned(self, request):
        """让 Mac 桥接服务准备设备，并登记到 TestHub。"""
        platform_name = str(request.data.get('platform') or '').strip().lower()
        device_id = str(request.data.get('device_id') or '').strip()
        if platform_name not in {'android', 'ios'} or not device_id:
            return Response({
                'success': False,
                'message': '缺少有效的设备平台或序列号',
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            result = device_bridge_request(
                '/devices/connect',
                {'platform': platform_name, 'device_id': device_id},
                timeout=180,
            )
            prepared = result.get('device') or {}

            if platform_name == 'android':
                device, created = AppDevice.objects.update_or_create(
                    device_id=device_id,
                    defaults={
                        'name': prepared.get('name') or device_id,
                        'platform': 'android',
                        'status': DeviceStatus.ONLINE,
                        'android_version': prepared.get('android_version') or '',
                        'ios_version': '',
                        'connection_type': prepared.get('connection_type') or 'usb',
                        'ip_address': '',
                        'port': 5555,
                        'wda_url': '',
                        'wda_bundle_id': '',
                        'device_specs': {
                            'bridge': 'macos',
                            'adb_server_socket': prepared.get('adb_server_socket') or '',
                        },
                    },
                )
                merge_local_emulator_aliases(device)
            else:
                wda_url = normalize_wda_url(prepared.get('wda_url'))
                manager = IOSDeviceManager(wda_url)
                probe = manager.probe()
                host, port = wda_host_port(wda_url)
                connection_type = prepared.get('connection_type') or 'ios_remote'
                device, created = AppDevice.objects.update_or_create(
                    device_id=device_id,
                    defaults={
                        'name': prepared.get('name') or probe.get('name') or 'iPhone',
                        'platform': 'ios',
                        'status': DeviceStatus.ONLINE,
                        'android_version': '',
                        'ios_version': prepared.get('ios_version') or probe.get('ios_version') or '',
                        'connection_type': connection_type,
                        'ip_address': host,
                        'port': port,
                        'wda_url': wda_url,
                        'wda_bundle_id': prepared.get('wda_bundle_id') or '',
                        'device_specs': {
                            'bridge': 'macos',
                            'is_simulator': bool(prepared.get('is_simulator')),
                            'wda_status': probe.get('status') or {},
                            'wda_device_info': probe.get('device_info') or {},
                        },
                    },
                )

            return Response({
                'success': True,
                'message': f'{device.name or device.device_id} 连接成功',
                'device': AppDeviceSerializer(device).data,
                'created': created,
            })
        except Exception as exc:
            logger.exception('一键连接设备失败: platform=%s device=%s', platform_name, device_id)
            return Response({
                'success': False,
                'message': str(exc),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=False, methods=['get'])
    def discover(self, request):
        """发现 Android ADB 设备，并刷新已登记 iOS WDA 设备的在线状态。"""
        db_devices = []
        errors = []

        try:
            adb_path = get_adb_path()
            logger.info(f"使用 ADB 路径: {adb_path}")
            manager = DeviceManager(adb_path=adb_path)
            devices_info = manager.list_devices()

            for device_info in devices_info:
                device_id = device_info['device_id']
                if ':' in device_id:
                    connection_type = 'remote_emulator'
                    ip_address = device_info.get('ip_address') or ''
                elif device_id.startswith('emulator-'):
                    connection_type = 'emulator'
                    ip_address = '127.0.0.1'
                else:
                    connection_type = 'usb'
                    ip_address = device_info.get('ip_address') or ''

                device, created = AppDevice.objects.update_or_create(
                    device_id=device_info['device_id'],
                    defaults={
                        'name': device_info.get('name') or '',
                        'platform': 'android',
                        'status': device_info.get('status') or 'offline',
                        'android_version': device_info.get('android_version') or '',
                        'ios_version': '',
                        'ip_address': ip_address,
                        'port': device_info.get('port') or 5555,
                        'connection_type': connection_type,
                    }
                )
                merge_local_emulator_aliases(device)
                db_devices.append(device)
        except Exception as e:
            logger.warning('发现 Android 设备失败: %s', e)
            errors.append(f'Android: {e}')

        ios_devices = list(AppDevice.objects.filter(platform='ios'))
        for device in ios_devices:
            try:
                probe = get_ios_manager(device).probe()
                if device.status != DeviceStatus.LOCKED:
                    device.status = DeviceStatus.ONLINE
                if probe.get('name') and not device.name:
                    device.name = probe['name']
                if probe.get('ios_version'):
                    device.ios_version = probe['ios_version']
                device.device_specs = {
                    **(device.device_specs or {}),
                    'wda_status': probe.get('status') or {},
                    'wda_device_info': probe.get('device_info') or {},
                }
                device.save(update_fields=[
                    'status', 'name', 'ios_version', 'device_specs', 'updated_at'
                ])
            except Exception as exc:
                logger.warning('刷新 iOS 设备 %s 失败: %s', device.device_id, exc)
                if device.status != DeviceStatus.LOCKED:
                    device.status = DeviceStatus.OFFLINE
                    device.save(update_fields=['status', 'updated_at'])
                errors.append(f'iOS {device.name or device.device_id}: {exc}')
            db_devices.append(device)

        # 去重并保持发现顺序。
        unique_devices = list({device.id: device for device in db_devices}.values())
        message = f'发现/刷新 {len(unique_devices)} 个设备'
        if errors:
            message += f'，{len(errors)} 项连接检查未通过'
        return Response({
            'success': True,
            'message': message,
            'devices': AppDeviceSerializer(unique_devices, many=True).data,
            'warnings': errors,
        })
    
    @action(detail=True, methods=['post'])
    def lock(self, request, pk=None):
        """锁定设备"""
        device = self.get_object()
        
        if device.status == 'locked':
            return Response({
                'success': False,
                'message': '设备已被锁定'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        device.lock(request.user)
        
        return Response({
            'success': True,
            'message': '设备锁定成功',
            'device': AppDeviceSerializer(device).data
        })
    
    @action(detail=True, methods=['post'])
    def unlock(self, request, pk=None):
        """释放设备"""
        device = self.get_object()
        
        if device.locked_by and device.locked_by != request.user:
            return Response({
                'success': False,
                'message': '无权释放他人锁定的设备'
            }, status=status.HTTP_403_FORBIDDEN)
        
        device.unlock()
        
        return Response({
            'success': True,
            'message': '设备释放成功',
            'device': AppDeviceSerializer(device).data
        })
    
    @action(detail=True, methods=['post'])
    def disconnect(self, request, pk=None):
        """断开远程设备连接"""
        device = self.get_object()

        if is_ios_device(device):
            device.status = DeviceStatus.OFFLINE
            device.save(update_fields=['status', 'updated_at'])
            return Response({
                'success': True,
                'message': f'iOS 设备 {device.name or device.device_id} 已从平台断开',
                'device': AppDeviceSerializer(device).data
            })
        
        # 只有远程设备可以断开
        if device.connection_type not in ['remote', 'remote_emulator']:
            return Response({
                'success': False,
                'message': '只能断开远程设备的连接'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            adb_path = get_adb_path()
            manager = DeviceManager(adb_path=adb_path)
            success = manager.disconnect_device(f'{device.ip_address}:{device.port}')
            
            if not success:
                return Response({
                    'success': False,
                    'message': '断开设备失败'
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
            # 更新设备状态为离线
            device.status = 'offline'
            device.save()
            
            return Response({
                'success': True,
                'message': f'设备 {device.name or device.device_id} 已断开连接',
                'device': AppDeviceSerializer(device).data
            })
            
        except Exception as e:
            return Response({
                'success': False,
                'message': '断开设备失败，请稍后重试'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=False, methods=['post'])
    def connect(self, request):
        """连接 Android ADB 设备或登记远程 iOS WDA 设备。"""
        try:
            platform_name = str(request.data.get('platform') or 'android').strip().lower()

            if platform_name == 'ios':
                device_id = str(request.data.get('device_id') or '').strip()
                if not device_id:
                    return Response({
                        'success': False,
                        'message': '请提供 iPhone UDID'
                    }, status=status.HTTP_400_BAD_REQUEST)

                defaults = get_ios_defaults()
                wda_url = normalize_wda_url(request.data.get('wda_url') or defaults['wda_url'])
                wda_bundle_id = str(
                    request.data.get('wda_bundle_id') or defaults['wda_bundle_id'] or ''
                ).strip()
                manager = IOSDeviceManager(wda_url)
                probe = manager.probe()
                host, port = wda_host_port(wda_url)

                device, created = AppDevice.objects.update_or_create(
                    device_id=device_id,
                    defaults={
                        'name': str(request.data.get('name') or probe.get('name') or 'iPhone').strip(),
                        'platform': 'ios',
                        'status': DeviceStatus.ONLINE,
                        'android_version': '',
                        'ios_version': str(
                            request.data.get('ios_version') or probe.get('ios_version') or ''
                        ).strip(),
                        'connection_type': 'ios_remote',
                        'ip_address': host,
                        'port': port,
                        'wda_url': wda_url,
                        'wda_bundle_id': wda_bundle_id,
                        'default_bundle_id': str(request.data.get('default_bundle_id') or '').strip(),
                        'device_specs': {
                            'wda_status': probe.get('status') or {},
                            'wda_device_info': probe.get('device_info') or {},
                        },
                    }
                )
                return Response({
                    'success': True,
                    'message': 'iPhone 连接成功',
                    'device': AppDeviceSerializer(device).data
                })

            if platform_name != 'android':
                return Response({
                    'success': False,
                    'message': '设备平台只支持 android 或 ios'
                }, status=status.HTTP_400_BAD_REQUEST)

            ip_address = request.data.get('ip_address')
            port = request.data.get('port', 5555)
            
            if not ip_address:
                return Response({
                    'success': False,
                    'message': '请提供设备IP地址'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            adb_path = get_adb_path()
            manager = DeviceManager(adb_path=adb_path)
            device_info = manager.connect_device(ip_address, port)
            
            # 创建或更新设备记录
            device, created = AppDevice.objects.update_or_create(
                device_id=device_info['device_id'],
                defaults={
                    'name': device_info.get('name') or '',
                    'platform': 'android',
                    'status': 'online',
                    'android_version': device_info.get('android_version', ''),
                    'ios_version': '',
                    'ip_address': ip_address,
                    'port': port,
                    'connection_type': 'remote_emulator',
                }
            )
            
            return Response({
                'success': True,
                'message': '设备连接成功',
                'device': AppDeviceSerializer(device).data
            })
        except Exception as e:
            logger.error(f"连接设备失败: {str(e)}")
            return Response({
                'success': False,
                'message': f'连接设备失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='launch-app')
    def launch_app(self, request, pk=None):
        """启动 Android 包名或 iOS Bundle ID 对应的应用。"""
        device = self.get_object()
        package_name = (
            request.data.get('package_name')
            or request.data.get('bundle_id')
            or device.default_bundle_id
            or ''
        ).strip()
        if not validate_package_name(package_name):
            return Response({
                'code': 400,
                'msg': '请输入有效的应用标识，例如 com.example.app',
                'success': False
            }, status=status.HTTP_400_BAD_REQUEST)

        if is_ios_device(device):
            try:
                manager = get_ios_manager(device)
                manager.probe()
                result = manager.launch_app(package_name)
                mark_device_status(device, DeviceStatus.ONLINE)
                if package_name != device.default_bundle_id:
                    device.default_bundle_id = package_name
                    device.save(update_fields=['default_bundle_id', 'updated_at'])
                return Response({
                    'code': 0,
                    'msg': 'iOS 应用启动成功',
                    'success': True,
                    'data': {
                        'bundle_id': package_name,
                        'device_id': device.device_id,
                        'wda_url': device.wda_url,
                        'result': result.get('response'),
                    }
                })
            except Exception as exc:
                mark_device_status(device, DeviceStatus.OFFLINE)
                logger.exception('iOS 设备 %s 启动应用失败', device.device_id)
                return Response({
                    'code': 500,
                    'msg': 'iOS 应用启动失败，请检查 Bundle ID、WDA 和设备锁屏状态',
                    'success': False,
                    'detail': str(exc),
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        adb_path = get_adb_path()
        device_serial, connect_detail = ensure_device_connected(device, adb_path)
        if not device_serial:
            return device_not_connected_response(device, connect_detail)

        package_result = run_adb_text(adb_path, '-s', device_serial, 'shell', 'pm', 'path', package_name, timeout=8)
        if package_result.returncode != 0 or not package_result.stdout.strip():
            return Response({
                'code': 404,
                'msg': f'设备上未安装应用包：{package_name}',
                'success': False,
                'detail': adb_detail(package_result)
            }, status=status.HTTP_404_NOT_FOUND)

        launch_result = run_adb_text(
            adb_path,
            '-s', device_serial,
            'shell', 'monkey',
            '-p', package_name,
            '-c', 'android.intent.category.LAUNCHER',
            '1',
            timeout=15
        )
        if launch_result.returncode != 0:
            return Response({
                'code': 500,
                'msg': '启动应用失败',
                'success': False,
                'detail': adb_detail(launch_result)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        time.sleep(2)
        focus_result = run_adb_text(adb_path, '-s', device_serial, 'shell', 'dumpsys', 'window', timeout=8)
        focus_lines = [
            line.strip()
            for line in (focus_result.stdout or '').splitlines()
            if 'mCurrentFocus' in line or 'mFocusedApp' in line
        ]

        return Response({
            'code': 0,
            'msg': '应用启动成功',
            'success': True,
            'data': {
                'package_name': package_name,
                'device_id': device_serial,
                'focus': focus_lines,
            }
        })

    @action(detail=True, methods=['post'], url_path='start-recording')
    def start_recording(self, request, pk=None):
        """开始设备录屏。"""
        device = self.get_object()
        key = str(device.id)
        existing = RECORDING_PROCESSES.get(key)
        existing_process = existing.get('process') if existing else None
        existing_active = bool(
            existing and (
                existing.get('platform') == 'ios'
                or (existing_process and existing_process.poll() is None)
            )
        )
        if existing_active:
            return Response({
                'code': 400,
                'msg': '该设备正在录屏，请先停止当前录屏',
                'success': False,
                'data': {
                    'started_at': existing.get('started_at'),
                    'remote_path': existing.get('remote_path')
                }
            }, status=status.HTTP_400_BAD_REQUEST)

        time_limit = request.data.get('time_limit') or 180
        try:
            time_limit = max(5, min(int(time_limit), 1800))
        except (TypeError, ValueError):
            time_limit = 180

        if is_ios_device(device):
            try:
                from airtest.core.ios.ios import IOS

                manager = get_ios_manager(device)
                manager.probe()
                timestamp = int(time.time())
                filename = f"device_{device.id}_{timestamp}.mp4"
                local_path, media_url = recording_media_path(device, filename)
                defaults = get_ios_defaults()
                ios_device = IOS(
                    device.wda_url or defaults['wda_url'],
                    udid=device.device_id,
                    wda_bundle_id=device.wda_bundle_id or defaults['wda_bundle_id'] or None,
                )
                ios_device.start_recording(
                    max_time=time_limit,
                    output=local_path,
                    fps=8,
                    max_size=1080,
                )
                RECORDING_PROCESSES[key] = {
                    'platform': 'ios',
                    'ios_device': ios_device,
                    'device_serial': device.device_id,
                    'filename': filename,
                    'local_path': local_path,
                    'media_url': media_url,
                    'started_at': timezone.now().isoformat(),
                }
                mark_device_status(device, DeviceStatus.ONLINE)
                return Response({
                    'code': 0,
                    'msg': 'iPhone 录屏已开始',
                    'success': True,
                    'data': {
                        'device_id': device.device_id,
                        'filename': filename,
                        'time_limit': time_limit,
                    }
                })
            except Exception as exc:
                logger.exception('iOS 设备 %s 开始录屏失败', device.device_id)
                return Response({
                    'code': 500,
                    'msg': 'iPhone 录屏启动失败，请检查 WDA 截图能力',
                    'success': False,
                    'detail': str(exc),
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        adb_path = get_adb_path()
        device_serial, connect_detail = ensure_device_connected(device, adb_path)
        if not device_serial:
            return device_not_connected_response(device, connect_detail)

        timestamp = int(time.time())
        filename = f"device_{device.id}_{timestamp}.mp4"
        remote_dir = '/sdcard/RunnerGoRecordings'
        remote_path = f'{remote_dir}/{filename}'

        mkdir_result = run_adb_text(adb_path, '-s', device_serial, 'shell', 'mkdir', '-p', remote_dir, timeout=8)
        if mkdir_result.returncode != 0:
            return Response({
                'code': 500,
                'msg': '创建设备录屏目录失败',
                'success': False,
                'detail': adb_detail(mkdir_result)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        process = subprocess.Popen(
            [
                adb_path, '-s', device_serial,
                'shell', 'screenrecord',
                '--bit-rate', '4000000',
                '--time-limit', str(time_limit),
                remote_path
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        RECORDING_PROCESSES[key] = {
            'process': process,
            'device_serial': device_serial,
            'remote_path': remote_path,
            'filename': filename,
            'started_at': timezone.now().isoformat(),
        }

        logger.info(f"设备 {device_serial} 开始录屏: {remote_path}")
        return Response({
            'code': 0,
            'msg': '录屏已开始',
            'success': True,
            'data': {
                'device_id': device_serial,
                'remote_path': remote_path,
                'filename': filename,
                'time_limit': time_limit,
            }
        })

    @action(detail=True, methods=['post'], url_path='stop-recording')
    def stop_recording(self, request, pk=None):
        """停止设备录屏并拉取到媒体目录。"""
        device = self.get_object()
        key = str(device.id)
        recording = RECORDING_PROCESSES.get(key)
        if not recording:
            return Response({
                'code': 400,
                'msg': '该设备没有正在进行的录屏',
                'success': False
            }, status=status.HTTP_400_BAD_REQUEST)

        if recording.get('platform') == 'ios':
            try:
                ios_device = recording.get('ios_device')
                if ios_device:
                    ios_device.stop_recording()
                    ios_device.disconnect()
                local_path = recording.get('local_path')
                if not local_path or not os.path.exists(local_path):
                    raise RuntimeError('iOS 录屏文件未生成')
                size = os.path.getsize(local_path)
                return Response({
                    'code': 0,
                    'msg': 'iPhone 录屏已保存',
                    'success': True,
                    'data': {
                        'device_id': device.device_id,
                        'filename': recording.get('filename'),
                        'recording_url': recording.get('media_url'),
                        'size': size,
                        'started_at': recording.get('started_at'),
                        'stopped_at': timezone.now().isoformat(),
                    }
                })
            except Exception as exc:
                logger.exception('iOS 设备 %s 停止录屏失败', device.device_id)
                return Response({
                    'code': 500,
                    'msg': 'iPhone 录屏停止失败',
                    'success': False,
                    'detail': str(exc),
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            finally:
                RECORDING_PROCESSES.pop(key, None)

        adb_path = get_adb_path()
        device_serial = recording['device_serial']
        remote_path = recording['remote_path']
        filename = recording['filename']
        process = recording.get('process')

        try:
            if process and process.poll() is None:
                run_adb_text(adb_path, '-s', device_serial, 'shell', 'pkill', '-2', 'screenrecord', timeout=5)
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
            time.sleep(1)

            local_path, media_url = recording_media_path(device, filename)
            pull_result = subprocess.run(
                [adb_path, '-s', device_serial, 'pull', remote_path, local_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=60
            )
            if pull_result.returncode != 0 or not os.path.exists(local_path):
                return Response({
                    'code': 500,
                    'msg': '录屏停止成功，但拉取视频失败',
                    'success': False,
                    'detail': adb_detail(pull_result)
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            run_adb_text(adb_path, '-s', device_serial, 'shell', 'rm', '-f', remote_path, timeout=8)
            size = os.path.getsize(local_path)
            logger.info(f"设备 {device_serial} 录屏保存成功: {local_path}")
            return Response({
                'code': 0,
                'msg': '录屏已保存',
                'success': True,
                'data': {
                    'device_id': device_serial,
                    'filename': filename,
                    'recording_url': media_url,
                    'size': size,
                    'started_at': recording.get('started_at'),
                    'stopped_at': timezone.now().isoformat(),
                }
            })
        finally:
            RECORDING_PROCESSES.pop(key, None)

    @action(detail=True, methods=['get'], url_path='action-recording-status')
    def action_recording_status(self, request, pk=None):
        """查询设备当前是否处于操作录制中，供页面重新进入后恢复控制状态。"""
        device = self.get_object()
        return Response({
            'code': 0,
            'msg': '操作录制状态已获取',
            'success': True,
            'data': _action_recording_payload(device),
        })

    @action(detail=True, methods=['post'], url_path='start-action-recording')
    def start_action_recording(self, request, pk=None):
        """开始录制设备触摸操作，停止后转换为 Airtest 可回放步骤。"""
        device = self.get_object()
        key = str(device.id)
        existing = ACTION_RECORDING_PROCESSES.get(key)
        existing_process = existing.get('process') if existing else None
        if existing and (
            existing.get('platform') == 'ios'
            or (existing_process and existing_process.poll() is None)
        ):
            return Response({
                'code': 0,
                'msg': '该设备已有操作录制，可直接停止当前录制',
                'success': True,
                'data': _action_recording_payload(device, existing),
            })

        if is_ios_device(device):
            try:
                manager = get_ios_manager(device)
                manager.probe()
                screen = manager.window_size()
                if not screen.get('width') or not screen.get('height'):
                    raise RuntimeError('WDA 未返回有效屏幕尺寸')
                recording = {
                    'platform': 'ios',
                    'device_id': device.device_id,
                    'started_at': timezone.now().isoformat(),
                    'metadata': screen,
                    'steps': [],
                    'action_lock': threading.Lock(),
                }
                ACTION_RECORDING_PROCESSES[key] = recording
                return Response({
                    'code': 0,
                    'msg': 'iOS 镜像操作录制已开始',
                    'success': True,
                    'data': {
                        'device_id': device.device_id,
                        'mode': 'interactive',
                        'platform': 'ios',
                        'started_at': recording['started_at'],
                        'screen': screen,
                    },
                })
            except Exception as exc:
                logger.exception('iOS 操作录制启动失败: device=%s', device.device_id)
                return Response({
                    'code': 500,
                    'msg': 'iOS 操作录制启动失败，请检查 WDA 连接',
                    'success': False,
                    'detail': str(exc),
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        adb_path = get_adb_path()
        device_serial, connect_detail = ensure_device_connected(device, adb_path)
        if not device_serial:
            return device_not_connected_response(device, connect_detail)

        metadata = get_touch_input_metadata(adb_path, device_serial)
        if not metadata.get('event_device'):
            return Response({
                'code': 500,
                'msg': '未找到触摸输入设备，无法录制操作',
                'success': False,
                'detail': 'adb shell getevent -lp 未返回包含 ABS_MT_POSITION_X/Y 的输入设备'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        timestamp = int(time.time())
        filename = f"device_{device.id}_{timestamp}.getevent.log"
        raw_path, raw_url = action_recording_path(device, filename)
        requested_event_device = (request.data.get('event_device') or '').strip()
        event_device = requested_event_device or 'all'
        command = [
            adb_path,
            '-s', device_serial,
            'shell', 'getevent', '-lt',
        ]
        if requested_event_device:
            command.append(requested_event_device)

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )

        recording = {
            'process': process,
            'adb_path': adb_path,
            'device_serial': device_serial,
            'raw_path': raw_path,
            'raw_url': raw_url,
            'filename': filename,
            'metadata': metadata,
            'event_device': event_device,
            'started_at': timezone.now().isoformat(),
            'stop_event': threading.Event(),
            'ui_lock': threading.Lock(),
            'paste_lock': threading.Lock(),
            'latest_ui_nodes': [],
            'latest_ui_captured_at': None,
            'gesture_fingerprints': [],
            'paste_events': [],
            'paste_threads': [],
        }
        ACTION_RECORDING_PROCESSES[key] = recording

        reader_thread = threading.Thread(
            target=_record_action_stream,
            args=(process, raw_path, metadata, recording),
            daemon=True,
            name=f'app-action-reader-{device.id}',
        )
        recording['reader_thread'] = reader_thread
        reader_thread.start()

        initial_nodes = get_device_ui_nodes(
            adb_path,
            device_serial,
            {'width': metadata.get('screen_width') or 0, 'height': metadata.get('screen_height') or 0},
        )
        if initial_nodes:
            with recording['ui_lock']:
                recording['latest_ui_nodes'] = initial_nodes
                recording['latest_ui_captured_at'] = timezone.now().isoformat()

        ui_thread = threading.Thread(
            target=_poll_recording_ui,
            args=(adb_path, device_serial, metadata, recording),
            daemon=True,
            name=f'app-action-ui-{device.id}',
        )
        recording['ui_thread'] = ui_thread
        ui_thread.start()

        logger.info(f"设备 {device_serial} 开始操作录制: {event_device} -> {raw_path}")
        return Response({
            'code': 0,
            'msg': 'Airtest 操作录制已开始',
            'success': True,
            'data': {
                'device_id': device_serial,
                'event_device': event_device,
                'filename': filename,
                'raw_url': raw_url,
                'started_at': recording['started_at'],
                'screen': {
                    'width': metadata.get('screen_width') or 0,
                    'height': metadata.get('screen_height') or 0,
                }
            }
        })

    @action(detail=True, methods=['post'], url_path='record-action')
    def record_action(self, request, pk=None):
        """执行并记录 iOS 镜像上的点击或滑动操作。"""
        device = self.get_object()
        key = str(device.id)
        recording = ACTION_RECORDING_PROCESSES.get(key)
        raw_record = request.data.get('record', True)
        record_enabled = not (
            raw_record is False
            or str(raw_record).strip().lower() in {'false', '0', 'no', 'off'}
        )
        if (not recording or recording.get('platform') != 'ios') and record_enabled:
            return Response({
                'code': 400,
                'msg': '该 iOS 设备没有正在进行的镜像操作录制',
                'success': False,
            }, status=status.HTTP_400_BAD_REQUEST)
        if recording and recording.get('platform') != 'ios':
            return Response({
                'code': 400,
                'msg': '当前操作录制不是 iOS 镜像录制',
                'success': False,
            }, status=status.HTTP_400_BAD_REQUEST)
        if not recording and not is_ios_device(device):
            return Response({
                'code': 400,
                'msg': '未开启录制时仅支持 iOS 镜像直接操作',
                'success': False,
            }, status=status.HTTP_400_BAD_REQUEST)

        action_type = str(request.data.get('type') or '').strip().lower()
        if action_type not in {'click', 'swipe', 'input', 'popup_select_text'}:
            return Response({
                'code': 400,
                'msg': 'iOS 镜像录制目前支持点击、滑动、输入和弹框选择',
                'success': False,
            }, status=status.HTTP_400_BAD_REQUEST)

        source_resolution = request.data.get('source_resolution') or {}
        if action_type in {'click', 'swipe'} and (
            not source_resolution.get('width') or not source_resolution.get('height')
        ):
            return Response({
                'code': 400,
                'msg': '缺少镜像画面尺寸',
                'success': False,
            }, status=status.HTTP_400_BAD_REQUEST)

        manager = get_ios_manager(device)
        target_resolution = (recording or {}).get('metadata') or manager.window_size()
        action_lock = (recording or {}).get('action_lock') or threading.Lock()
        if recording is not None:
            recording['action_lock'] = action_lock

        try:
            with action_lock:
                step_index = len((recording or {}).get('steps') or []) + 1
                if action_type == 'click':
                    logical_x, logical_y = _scale_ios_recording_point(
                        {'x': request.data.get('x'), 'y': request.data.get('y')},
                        source_resolution,
                        target_resolution,
                    )
                    fingerprint = _ios_fingerprint_at_point(
                        manager, target_resolution, logical_x, logical_y
                    )
                    manager.tap(logical_x, logical_y)
                    normalized_position = normalize_point(
                        logical_x, logical_y, target_resolution
                    )
                    config = {
                        'selector_type': 'pos',
                        'selector': f'{logical_x},{logical_y}',
                        'source_resolution': target_resolution,
                        'normalized_position': normalized_position,
                        'coordinate_mode': 'normalized' if normalized_position else 'absolute',
                        'timeout': 5,
                        'wait_for_stable': True,
                    }
                    if fingerprint:
                        config['fingerprint'] = fingerprint
                        config['fingerprint_version'] = fingerprint.get('version', 1)
                    if recording is not None:
                        if _ios_recording_fingerprint_targets_input(fingerprint):
                            recording['last_input_target_config'] = copy.deepcopy(config)
                        else:
                            recording.pop('last_input_target_config', None)
                    step = {
                        'id': f"recorded_{uuid.uuid4().hex[:10]}",
                        'type': 'click',
                        'name': _requested_step_name(request, f'iOS 录制点击 {step_index}'),
                        'config': config,
                    }
                elif action_type == 'swipe':
                    start = request.data.get('start') or {}
                    end = request.data.get('end') or {}
                    start_x, start_y = _scale_ios_recording_point(
                        start, source_resolution, target_resolution
                    )
                    end_x, end_y = _scale_ios_recording_point(
                        end, source_resolution, target_resolution
                    )
                    duration = max(0.1, min(5.0, float(request.data.get('duration') or 0.5)))
                    manager.swipe(start_x, start_y, end_x, end_y, duration)
                    step = {
                        'id': f"recorded_{uuid.uuid4().hex[:10]}",
                        'type': 'swipe',
                        'name': _requested_step_name(request, f'iOS 录制滑动 {step_index}'),
                        'config': {
                            'start': f'{start_x},{start_y}',
                            'end': f'{end_x},{end_y}',
                            'duration': duration,
                            'source_resolution': target_resolution,
                            'normalized_start': normalize_point(
                                start_x, start_y, target_resolution
                            ),
                            'normalized_end': normalize_point(
                                end_x, end_y, target_resolution
                            ),
                            'coordinate_mode': 'normalized',
                        },
                    }
                elif action_type == 'input':
                    value = str(request.data.get('value') or '')
                    send_enter = bool(request.data.get('send_enter'))
                    if not value and not send_enter:
                        return Response({
                            'code': 400,
                            'msg': '请输入要写入输入框的内容',
                            'success': False,
                        }, status=status.HTTP_400_BAD_REQUEST)
                    manager.input_text(value, enter=send_enter)
                    config = copy.deepcopy(
                        (recording or {}).get('last_input_target_config') or {}
                    )
                    config.update({
                        'value': value,
                        'send_enter': send_enter,
                    })
                    if request.data.get('clear_first') is not None:
                        config['clear_first'] = bool(request.data.get('clear_first'))
                    if request.data.get('sensitive') is not None:
                        config['sensitive'] = bool(request.data.get('sensitive'))
                    step = {
                        'id': f"recorded_{uuid.uuid4().hex[:10]}",
                        'type': 'input',
                        'name': _requested_step_name(request, f'iOS 录制输入 {step_index}'),
                        'config': config,
                    }
                else:
                    value = str(
                        request.data.get('value')
                        or request.data.get('option_label')
                        or ''
                    ).strip()
                    if not value:
                        return Response({
                            'code': 400,
                            'msg': '请输入弹框内容或路径',
                            'success': False,
                        }, status=status.HTTP_400_BAD_REQUEST)
                    max_swipes = max(0, min(30, int(request.data.get('max_swipes') or 8)))
                    interval = max(0.05, min(3.0, float(request.data.get('interval') or 0.25)))
                    duration = max(0.1, min(2.0, float(request.data.get('duration') or 0.35)))
                    match_mode = str(request.data.get('match_mode') or 'exact').strip().lower()
                    if match_mode not in {'exact', 'contains'}:
                        match_mode = 'exact'
                    popup_region = request.data.get('popup_region') or [0.0, 0.35, 1.0, 0.98]

                    def get_nodes():
                        return parse_ui_hierarchy(manager.source(), target_resolution)

                    select_popup_text_path(
                        value,
                        resolution=target_resolution,
                        get_nodes=get_nodes,
                        tap=lambda x, y: manager.tap(x, y),
                        swipe=lambda x1, y1, x2, y2, swipe_duration: manager.swipe(
                            x1, y1, x2, y2, swipe_duration
                        ),
                        pause=time.sleep,
                        max_swipes=max_swipes,
                        interval=interval,
                        duration=duration,
                        popup_region=popup_region,
                        match_mode=match_mode,
                    )
                    step = {
                        'id': f"recorded_{uuid.uuid4().hex[:10]}",
                        'type': 'popup_select_text',
                        'name': _requested_step_name(request, f'弹框选择 {value}'[:100]),
                        'config': {
                            'value': value,
                            'option_label': value,
                            'match_mode': match_mode,
                            'max_swipes': max_swipes,
                            'popup_region': popup_region,
                            'interval': interval,
                            'duration': duration,
                            'save_as': 'popup_select_result',
                            'scope': 'local',
                        },
                    }
                if record_enabled and recording is not None:
                    recording.setdefault('steps', []).append(step)
            return Response({
                'code': 0,
                'msg': 'iOS 操作已记录' if record_enabled else 'iOS 操作已执行',
                'success': True,
                'data': {
                    'step': step if record_enabled else None,
                    'step_count': len((recording or {}).get('steps') or []),
                    'recorded': record_enabled,
                },
            })
        except Exception as exc:
            logger.exception('iOS 镜像操作执行失败: device=%s action=%s', device.device_id, action_type)
            return Response({
                'code': 500,
                'msg': 'iOS 镜像操作执行失败，请检查 WDA 连接',
                'success': False,
                'detail': str(exc),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='stop-action-recording')
    def stop_action_recording(self, request, pk=None):
        """停止操作录制并返回可直接导入用例编排的步骤。"""
        device = self.get_object()
        key = str(device.id)
        recording = ACTION_RECORDING_PROCESSES.get(key)
        if not recording:
            return Response({
                'code': 400,
                'msg': '该设备没有正在进行的操作录制',
                'success': False
            }, status=status.HTTP_400_BAD_REQUEST)

        if recording.get('platform') == 'ios':
            ACTION_RECORDING_PROCESSES.pop(key, None)
            request_steps = request.data.get('steps') if 'steps' in request.data else None
            steps = _merge_recorded_step_overrides(
                list(recording.get('steps') or []),
                request_steps,
            )
            steps, repair_notes = _repair_action_recording_steps(steps)
            metadata = recording.get('metadata') or {}
            stopped_at = timezone.now().isoformat()
            return Response({
                'code': 0,
                'msg': 'iOS 镜像操作录制已生成步骤',
                'success': True,
                'data': {
                    'device_id': device.device_id,
                    'started_at': recording.get('started_at'),
                    'stopped_at': stopped_at,
                    'steps': steps,
                    'step_count': len(steps),
                    'semantic_fingerprint_count': sum(
                        1 for item in steps
                        if (item.get('config') or {}).get('fingerprint')
                    ),
                    'screen': metadata,
                    'ai_recorder': build_ai_recording_enhancement(steps, device),
                    'recording_repairs': repair_notes,
                },
            })

        process = recording.get('process')
        adb_path = get_adb_path()
        device_serial = recording.get('device_serial')
        raw_path = recording.get('raw_path')
        metadata = recording.get('metadata') or {}

        try:
            stop_event = recording.get('stop_event')
            if stop_event:
                stop_event.set()
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    run_adb_text(adb_path, '-s', device_serial, 'shell', 'pkill', '-2', 'getevent', timeout=5)
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
            reader_thread = recording.get('reader_thread')
            if reader_thread:
                reader_thread.join(timeout=3)
            ui_thread = recording.get('ui_thread')
            if ui_thread:
                ui_thread.join(timeout=1)
            paste_deadline = time.monotonic() + 3
            for paste_thread in recording.get('paste_threads') or []:
                remaining = paste_deadline - time.monotonic()
                if remaining <= 0:
                    break
                paste_thread.join(timeout=remaining)
            time.sleep(0.2)

            steps = parse_action_recording(
                raw_path,
                metadata,
                recording.get('gesture_fingerprints') or [],
                recording.get('paste_events') or [],
            )
            request_steps = request.data.get('steps') if 'steps' in request.data else None
            steps = _merge_recorded_step_overrides(steps, request_steps)
            steps, repair_notes = _repair_action_recording_steps(steps)
            logger.info(f"设备 {device_serial} 操作录制完成，生成 {len(steps)} 个步骤")
            stopped_at = timezone.now().isoformat()
            return Response({
                'code': 0,
                'msg': 'Airtest 操作录制已生成步骤',
                'success': True,
                'data': {
                    'device_id': device_serial,
                    'filename': recording.get('filename'),
                    'raw_url': recording.get('raw_url'),
                    'started_at': recording.get('started_at'),
                    'stopped_at': stopped_at,
                    'steps': steps,
                    'step_count': len(steps),
                    'semantic_fingerprint_count': sum(
                        1 for item in steps
                        if (item.get('config') or {}).get('fingerprint')
                    ),
                    'screen': {
                        'width': metadata.get('screen_width') or 0,
                        'height': metadata.get('screen_height') or 0,
                    },
                    'ai_recorder': build_ai_recording_enhancement(steps, device),
                    'recording_repairs': repair_notes,
                }
            })
        finally:
            ACTION_RECORDING_PROCESSES.pop(key, None)
    
    @action(detail=True, methods=['post'], url_path='screenshot')
    def screenshot(self, request, pk=None):
        """
        获取设备实时截图与可交互语义元素
        
        功能：
        1. 使用 adb screencap 获取设备截图
        2. Android 调用 UIAutomator dump，iOS 调用 WDA source
        3. 归一化 resource-id/text/accessibility/class/bounds
        4. 返回截图 data URL 与 ui_nodes
        """
        device = self.get_object()

        if is_ios_device(device):
            try:
                manager = get_ios_manager(device)
                manager.probe()
                image_bytes = manager.screenshot()
                if not image_bytes:
                    raise RuntimeError('WDA 截图接口未返回图片数据')

                screen_width, screen_height = get_png_dimensions(image_bytes)
                if not screen_width or not screen_height:
                    size = manager.window_size()
                    screen_width = size.get('width') or 0
                    screen_height = size.get('height') or 0
                screen_resolution = {'width': screen_width, 'height': screen_height}

                ui_nodes = []
                try:
                    ui_nodes = parse_ui_hierarchy(manager.source(), screen_resolution)
                except Exception as source_exc:
                    logger.debug('读取 iOS UI 树失败: %s', source_exc)

                mark_device_status(device, DeviceStatus.ONLINE)
                image_base64 = base64.b64encode(image_bytes).decode('utf-8')
                return Response({
                    'code': 0,
                    'msg': '截图成功',
                    'success': True,
                    'data': {
                        'filename': f"device_{device.id}_{int(timezone.now().timestamp())}.png",
                        'content': f"data:image/png;base64,{image_base64}",
                        'device_id': device.device_id,
                        'platform': 'ios',
                        'timestamp': int(timezone.now().timestamp()),
                        'screen': screen_resolution,
                        'ui_nodes': ui_nodes,
                        'ui_tree_available': bool(ui_nodes),
                    }
                })
            except Exception as exc:
                mark_device_status(device, DeviceStatus.OFFLINE)
                logger.exception('iOS 设备 %s 截图失败', device.device_id)
                return Response({
                    'code': 500,
                    'msg': 'iPhone 截图失败，请检查 WDA 连接',
                    'success': False,
                    'detail': str(exc),
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        if device.status == DeviceStatus.OFFLINE and device.connection_type not in REMOTE_CONNECTION_TYPES:
            return Response({
                'code': 400,
                'msg': '设备离线，无法截图',
                'success': False
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            adb_path = get_adb_path()
            device_serial, connect_detail = ensure_screenshot_device_connected(device, adb_path)
            if not device_serial:
                message = (
                    f'设备未连接，请确认设备已开启并可通过 adb connect {remote_device_address(device)} 连接'
                    if device.connection_type in REMOTE_CONNECTION_TYPES
                    else '设备未连接，请检查 USB/模拟器连接后重新发现设备'
                )
                logger.warning(f"设备 {device.device_id} 截图前连接检查失败: {connect_detail}")
                return Response({
                    'code': 400,
                    'msg': message,
                    'success': False,
                    'detail': connect_detail
                }, status=status.HTTP_400_BAD_REQUEST)

            result = subprocess.run(
                [adb_path, '-s', device_serial, 'exec-out', 'screencap', '-p'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=15
            )

            if result.returncode != 0:
                connected, state_detail = is_adb_device_connected(adb_path, device_serial)
                detail = state_detail or adb_detail(result)
                if not connected:
                    mark_device_status(device, DeviceStatus.OFFLINE)
                    logger.warning(f"设备 {device_serial} 截图失败，ADB 已断开: {detail}")
                    return Response({
                        'code': 400,
                        'msg': '设备已断开，无法截图，请重新连接设备',
                        'success': False,
                        'detail': detail
                    }, status=status.HTTP_400_BAD_REQUEST)

                logger.error(f"设备 {device_serial} screencap 执行失败: {detail}")
                return Response({
                    'code': 500,
                    'msg': '截图失败：ADB screencap 执行失败',
                    'success': False,
                    'detail': detail
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
            if not result.stdout:
                logger.error(f"设备 {device_serial} 截图失败: screencap 无返回数据")
                return Response({
                    'code': 500,
                    'msg': '截图失败：无返回数据',
                    'success': False
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
            # 转换为 Base64
            image_base64 = base64.b64encode(result.stdout).decode('utf-8')
            screen_width, screen_height = get_png_dimensions(result.stdout)
            if not screen_width or not screen_height:
                screen_width, screen_height = get_device_screen_size(adb_path, device_serial)
            screen_resolution = {'width': screen_width, 'height': screen_height}
            ui_nodes = get_device_ui_nodes(adb_path, device_serial, screen_resolution)
            
            logger.info(f"设备 {device_serial} 截图成功")
            
            return Response({
                'code': 0,
                'msg': '截图成功',
                'success': True,
                'data': {
                    'filename': f"device_{device.id}_{int(timezone.now().timestamp())}.png",
                        'content': f"data:image/png;base64,{image_base64}",
                        'device_id': device_serial,
                        'platform': 'android',
                        'timestamp': int(timezone.now().timestamp()),
                    'screen': screen_resolution,
                    'ui_nodes': ui_nodes,
                    'ui_tree_available': bool(ui_nodes),
                }
            })
            
        except subprocess.TimeoutExpired:
            logger.error(f"设备 {device.device_id} 截图超时")
            return Response({
                'code': 504,
                'msg': '截图超时，请检查设备连接',
                'success': False
            }, status=status.HTTP_504_GATEWAY_TIMEOUT)
        except FileNotFoundError:
            logger.error(f"ADB 命令不可用: {get_adb_path()}")
            return Response({
                'code': 500,
                'msg': 'ADB 命令不可用，请在 APP 自动化运行环境中检查 ADB 路径',
                'success': False
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            logger.exception(f"设备 {device.device_id} 截图失败")
            return Response({
                'code': 500,
                'msg': '截图失败，请检查设备连接',
                'success': False,
                'detail': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='tap')
    def tap_device(self, request, pk=None):
        """把截图坐标缩放到真实设备坐标并执行点击。"""
        device = self.get_object()
        source_resolution = request.data.get('source_resolution') or {}
        if (
            request.data.get('x') is None
            or request.data.get('y') is None
            or not source_resolution.get('width')
            or not source_resolution.get('height')
        ):
            return Response({
                'code': 400,
                'msg': '缺少有效的点击坐标或截图尺寸',
                'success': False,
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            if is_ios_device(device):
                manager = get_ios_manager(device)
                target_resolution = manager.window_size()
                logical_x, logical_y = _scale_ios_recording_point(
                    {'x': request.data.get('x'), 'y': request.data.get('y')},
                    source_resolution,
                    target_resolution,
                )
                manager.tap(logical_x, logical_y)
                mark_device_status(device, DeviceStatus.ONLINE)
            else:
                adb_path = get_adb_path()
                device_serial, connect_detail = ensure_device_connected(device, adb_path)
                if not device_serial:
                    return device_not_connected_response(device, connect_detail)
                width, height = get_device_screen_size(adb_path, device_serial)
                logical_x, logical_y = _scale_ios_recording_point(
                    {'x': request.data.get('x'), 'y': request.data.get('y')},
                    source_resolution,
                    {'width': width, 'height': height},
                )
                result = run_adb_text(
                    adb_path,
                    '-s', device_serial,
                    'shell', 'input', 'tap', str(logical_x), str(logical_y),
                    timeout=8,
                )
                if result.returncode != 0:
                    raise RuntimeError(adb_detail(result) or 'ADB 点击失败')

            return Response({
                'code': 0,
                'msg': '设备点击成功',
                'success': True,
                'data': {
                    'device_id': device.device_id,
                    'platform': device.platform,
                    'x': logical_x,
                    'y': logical_y,
                },
            })
        except Exception as exc:
            logger.exception('设备画面点击失败: device=%s', device.device_id)
            return Response({
                'code': 500,
                'msg': '设备点击失败，请检查设备连接',
                'success': False,
                'detail': str(exc),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='swipe')
    def swipe_device(self, request, pk=None):
        """把截图坐标缩放到真实设备坐标并执行滑动。"""
        device = self.get_object()
        source_resolution = request.data.get('source_resolution') or {}
        start = request.data.get('start') or {}
        end = request.data.get('end') or {}
        if (
            start.get('x') is None
            or start.get('y') is None
            or end.get('x') is None
            or end.get('y') is None
            or not source_resolution.get('width')
            or not source_resolution.get('height')
        ):
            return Response({
                'code': 400,
                'msg': '缺少有效的滑动坐标或截图尺寸',
                'success': False,
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            duration = max(0.1, min(5.0, float(request.data.get('duration') or 0.5)))
            if is_ios_device(device):
                manager = get_ios_manager(device)
                target_resolution = manager.window_size()
                start_x, start_y = _scale_ios_recording_point(start, source_resolution, target_resolution)
                end_x, end_y = _scale_ios_recording_point(end, source_resolution, target_resolution)
                manager.swipe(start_x, start_y, end_x, end_y, duration)
                mark_device_status(device, DeviceStatus.ONLINE)
            else:
                adb_path = get_adb_path()
                device_serial, connect_detail = ensure_device_connected(device, adb_path)
                if not device_serial:
                    return device_not_connected_response(device, connect_detail)
                width, height = get_device_screen_size(adb_path, device_serial)
                target_resolution = {'width': width, 'height': height}
                start_x, start_y = _scale_ios_recording_point(start, source_resolution, target_resolution)
                end_x, end_y = _scale_ios_recording_point(end, source_resolution, target_resolution)
                duration_ms = int(duration * 1000)
                result = run_adb_text(
                    adb_path,
                    '-s', device_serial,
                    'shell', 'input', 'swipe',
                    str(start_x), str(start_y), str(end_x), str(end_y), str(duration_ms),
                    timeout=10,
                )
                if result.returncode != 0:
                    raise RuntimeError(adb_detail(result) or 'ADB 滑动失败')
                mark_device_status(device, DeviceStatus.ONLINE)

            return Response({
                'code': 0,
                'msg': '设备滑动成功',
                'success': True,
                'data': {
                    'device_id': device.device_id,
                    'platform': device.platform,
                    'start': {'x': start_x, 'y': start_y},
                    'end': {'x': end_x, 'y': end_y},
                    'duration': duration,
                },
            })
        except Exception as exc:
            logger.exception('设备画面滑动失败: device=%s', device.device_id)
            return Response({
                'code': 500,
                'msg': '设备滑动失败，请检查设备连接',
                'success': False,
                'detail': str(exc),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='popup-select-text')
    def popup_select_text(self, request, pk=None):
        """按输入文本/路径操作当前设备上的滚轮或级联选择弹框。"""
        device = self.get_object()
        value = str(
            request.data.get('value')
            or request.data.get('option_label')
            or ''
        ).strip()
        if not value:
            return Response({
                'code': 400,
                'msg': '请输入弹框内容或路径',
                'success': False,
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            max_swipes = max(0, min(30, int(request.data.get('max_swipes') or 8)))
            interval = max(0.05, min(3.0, float(request.data.get('interval') or 0.25)))
            duration = max(0.1, min(2.0, float(request.data.get('duration') or 0.35)))
            match_mode = str(request.data.get('match_mode') or 'exact').strip().lower()
            if match_mode not in {'exact', 'contains'}:
                match_mode = 'exact'
            popup_region = request.data.get('popup_region') or [0.0, 0.35, 1.0, 0.98]

            if is_ios_device(device):
                manager = get_ios_manager(device)
                manager.probe()
                resolution = manager.window_size()

                def get_nodes():
                    return parse_ui_hierarchy(manager.source(), resolution)

                def tap_at(x, y):
                    manager.tap(x, y)

                def swipe_at(x1, y1, x2, y2, swipe_duration):
                    manager.swipe(x1, y1, x2, y2, swipe_duration)

                result = select_popup_text_path(
                    value,
                    resolution=resolution,
                    get_nodes=get_nodes,
                    tap=tap_at,
                    swipe=swipe_at,
                    pause=time.sleep,
                    max_swipes=max_swipes,
                    interval=interval,
                    duration=duration,
                    popup_region=popup_region,
                    match_mode=match_mode,
                )
                mark_device_status(device, DeviceStatus.ONLINE)
            else:
                adb_path = get_adb_path()
                device_serial, connect_detail = ensure_device_connected(device, adb_path)
                if not device_serial:
                    return device_not_connected_response(device, connect_detail)
                width, height = get_device_screen_size(adb_path, device_serial)
                resolution = {'width': width, 'height': height}

                def get_nodes():
                    return get_device_ui_nodes(adb_path, device_serial, resolution)

                def tap_at(x, y):
                    tap_result = run_adb_text(
                        adb_path, '-s', device_serial, 'shell', 'input', 'tap',
                        str(x), str(y), timeout=8,
                    )
                    if tap_result.returncode != 0:
                        raise RuntimeError(adb_detail(tap_result) or 'ADB 点击失败')

                def swipe_at(x1, y1, x2, y2, swipe_duration):
                    swipe_result = run_adb_text(
                        adb_path, '-s', device_serial, 'shell', 'input', 'swipe',
                        str(x1), str(y1), str(x2), str(y2),
                        str(int(float(swipe_duration) * 1000)), timeout=10,
                    )
                    if swipe_result.returncode != 0:
                        raise RuntimeError(adb_detail(swipe_result) or 'ADB 滑动失败')

                result = select_popup_text_path(
                    value,
                    resolution=resolution,
                    get_nodes=get_nodes,
                    tap=tap_at,
                    swipe=swipe_at,
                    pause=time.sleep,
                    max_swipes=max_swipes,
                    interval=interval,
                    duration=duration,
                    popup_region=popup_region,
                    match_mode=match_mode,
                )
                mark_device_status(device, DeviceStatus.ONLINE)

            step = {
                'type': 'popup_select_text',
                'name': f'弹框选择 {value}'[:100],
                'config': {
                    'value': value,
                    'option_label': value,
                    'match_mode': match_mode,
                    'max_swipes': max_swipes,
                    'popup_region': popup_region,
                    'interval': interval,
                    'duration': duration,
                    'save_as': 'popup_select_result',
                    'scope': 'local',
                },
            }
            return Response({
                'code': 0,
                'msg': '弹框内容已定位并生成步骤',
                'success': True,
                'data': {
                    'device_id': device.device_id,
                    'platform': device.platform,
                    'result': result,
                    'step': step,
                },
            })
        except PopupSelectionError as exc:
            return Response({
                'code': 404,
                'msg': str(exc),
                'success': False,
            }, status=status.HTTP_404_NOT_FOUND)
        except (TypeError, ValueError) as exc:
            return Response({
                'code': 400,
                'msg': str(exc),
                'success': False,
            }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception('设备弹框文本选择失败: device=%s, value=%s', device.device_id, value)
            return Response({
                'code': 500,
                'msg': '弹框选择失败，请检查设备连接和弹框状态',
                'success': False,
                'detail': str(exc),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
