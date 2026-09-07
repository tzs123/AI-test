#!/usr/bin/env python3
"""
RunnerGo Mac 设备桥接服务（运行在 macOS 宿主机上，非容器内）。

作用：Docker 里的 TestHub 后端无法直接操作宿主机的 adb / xcrun，
通过本服务把宿主机上的 Android 模拟器 / iOS 模拟器暴露给后端：

    GET  /health          健康检查（_check_device_bridge 使用）
    GET  /devices/scan    扫描宿主机在线设备（真机/模拟器）
    POST /devices/connect 准备并返回指定设备的连接信息

启动方式（默认监听 0.0.0.0:8765，容器内通过 host.docker.internal:8765 访问）：

    nohup python3 tools/device_bridge_server.py > /tmp/device_bridge.log 2>&1 &

仅使用 Python 标准库，无需安装依赖。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE_NAME = 'macos-device-bridge'
HOST_ADB_SERVER_SOCKET = 'tcp:host.docker.internal:5037'
WDA_STATUS_URL = 'http://127.0.0.1:8100/status'
WDA_CONTAINER_URL = 'http://host.docker.internal:8100'
ADB_CANDIDATES = [
    os.path.expanduser('~/Library/Android/sdk/platform-tools/adb'),
    '/usr/local/bin/adb',
    '/opt/homebrew/bin/adb',
]


def log(message):
    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] {message}', flush=True)


def run_command(args, timeout=10):
    """执行命令，返回 (returncode, stdout, stderr)。找不到命令返回 (127, '', 原因)。"""
    try:
        proc = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout,
        )
        return proc.returncode, proc.stdout or '', proc.stderr or ''
    except FileNotFoundError:
        return 127, '', f'command not found: {args[0]}'
    except subprocess.TimeoutExpired:
        return 124, '', f'command timeout: {" ".join(args)}'
    except Exception as exc:  # 防御性兜底，避免单个命令异常拖垮整个服务
        return 1, '', f'{type(exc).__name__}: {exc}'


def find_adb():
    for path in ADB_CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    code, _, _ = run_command(['adb', 'version'], timeout=5)
    return 'adb' if code == 0 else ''


def find_xcrun():
    for path in ('/usr/bin/xcrun',):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    code, _, _ = run_command(['xcrun', '--version'], timeout=5)
    return 'xcrun' if code == 0 else ''


# ---------------------------------------------------------------------------
# Android
# ---------------------------------------------------------------------------

def android_adb_devices(adb_path):
    """解析 `adb devices -l`，返回 [(serial, props_dict), ...]（仅 state=device）。"""
    code, stdout, stderr = run_command([adb_path, 'devices', '-l'], timeout=10)
    if code != 0:
        raise RuntimeError(f'adb devices 失败: {(stderr or stdout).strip()[:200]}')

    devices = []
    for line in stdout.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith('*'):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        props = {}
        for item in parts[2:]:
            if ':' in item:
                key, value = item.split(':', 1)
                props[key] = value
        devices.append((serial, state, props))
    return devices


def android_connection_type(serial):
    if serial.startswith('emulator-'):
        return 'emulator'
    if re.match(r'^\d{1,3}(\.\d{1,3}){3}:\d+$', serial) or serial.startswith('localhost:'):
        return 'remote'
    return 'usb'


def android_device_info(adb_path, serial, props=None, full=False):
    """读取单台 Android 设备的详情（getprop），失败字段留空。

    full=True 时始终通过 getprop 读取全部字段（connect 阶段使用）；
    扫描阶段传入 `adb devices -l` 的 props 走快速路径，跳过 getprop 提速。
    """
    info = {'connection_type': android_connection_type(serial)}

    def getprop(key):
        code, stdout, _ = run_command([adb_path, '-s', serial, 'shell', 'getprop', key], timeout=5)
        return stdout.strip() if code == 0 else ''

    if full or not props or 'model' not in props:
        info['model'] = getprop('ro.product.model')
        info['device_name'] = getprop('ro.product.device')
        info['android_version'] = getprop('ro.build.version.release')
        info['sdk_version'] = getprop('ro.build.version.sdk')
    else:
        info['model'] = props.get('model', '')
        info['device_name'] = props.get('device', '')
        # 扫描时为提速跳过 getprop，版本号在 connect 阶段补齐
        info['android_version'] = ''
        info['sdk_version'] = ''
    return info


def scan_android_devices(adb_path, warnings):
    devices = []
    if not adb_path:
        warnings.append('未找到 adb（已尝试 ~/Library/Android/sdk/platform-tools/adb 和 PATH）')
        return devices

    try:
        listed = android_adb_devices(adb_path)
    except RuntimeError as exc:
        warnings.append(str(exc))
        return devices

    online = [item for item in listed if item[1] == 'device']
    for serial, _state, props in online:
        info = android_device_info(adb_path, serial, props)
        devices.append({
            'platform': 'android',
            'device_id': serial,
            'name': info.get('model') or info.get('device_name') or serial,
            'connection_type': info['connection_type'],
            'android_version': info.get('android_version') or '',
            'can_connect': True,
            'issues': [],
        })
    return devices


def connect_android_device(adb_path, device_id):
    if not adb_path:
        raise RuntimeError('未找到 adb，无法连接 Android 设备')

    listed = {serial: (state, props) for serial, state, props in android_adb_devices(adb_path)}
    state = listed.get(device_id, ('', {}))[0]

    if state != 'device':
        # 网络设备自动重连一次
        if re.match(r'^[\w.\-]+:\d+$', device_id) and not device_id.startswith('emulator-'):
            run_command([adb_path, 'connect', device_id], timeout=15)
            listed = {s: (st, p) for s, st, p in android_adb_devices(adb_path)}
            state = listed.get(device_id, ('', {}))[0]
        if state != 'device':
            raise RuntimeError(f'设备 {device_id} 当前不在线（adb state={state or "unlisted"}）')

    info = android_device_info(adb_path, device_id, listed.get(device_id, ('', {}))[1], full=True)
    return {
        'platform': 'android',
        'device_id': device_id,
        'name': info.get('model') or info.get('device_name') or device_id,
        'android_version': info.get('android_version') or '',
        'connection_type': info['connection_type'],
        'adb_server_socket': HOST_ADB_SERVER_SOCKET,
    }


# ---------------------------------------------------------------------------
# iOS
# ---------------------------------------------------------------------------

def simctl_devices_json(xcrun_path):
    code, stdout, stderr = run_command([xcrun_path, 'simctl', 'list', 'devices', '--json'], timeout=15)
    if code != 0:
        raise RuntimeError(f'simctl list 失败: {(stderr or stdout).strip()[:200]}')
    return json.loads(stdout)


def simctl_runtimes_json(xcrun_path):
    code, stdout, stderr = run_command([xcrun_path, 'simctl', 'list', 'runtimes', '--json'], timeout=15)
    if code != 0:
        return {}
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    return {
        runtime.get('identifier', ''): runtime
        for runtime in data.get('runtimes', [])
    }


def probe_wda(timeout=3):
    """探测模拟器中的 WebDriverAgent，返回 (ok, status_dict, detail)。"""
    try:
        with urllib.request.urlopen(WDA_STATUS_URL, timeout=timeout) as response:
            data = json.loads(response.read().decode('utf-8', errors='ignore') or '{}')
        return True, data, ''
    except Exception as exc:
        return False, {}, f'{type(exc).__name__}: {exc}'


def find_wda_bundle_id(xcrun_path, udid):
    code, stdout, _ = run_command([xcrun_path, 'simctl', 'listapps', udid], timeout=15)
    if code != 0:
        return ''
    # listapps 输出可能夹带非 JSON 前缀，宽松匹配 WebDriverAgent bundle id
    match = re.search(r'"([\w.]*WebDriverAgent[\w.]*)"', stdout)
    return match.group(1) if match else ''


def launch_wda(xcrun_path, udid):
    """尝试启动模拟器里已安装的 WDA，返回 (ok, detail)。

    WDA 进程可能僵死（端口监听但 HTTP server 不响应，连接被 reset），
    因此先探测，已就绪直接返回；否则终止旧进程后重新拉起再探测。
    """
    bundle_id = find_wda_bundle_id(xcrun_path, udid)
    if not bundle_id:
        return False, '模拟器中未安装 WebDriverAgent'
    ok, _, _ = probe_wda(timeout=2)
    if ok:
        return True, ''
    # 进程存活但 HTTP 挂起时，simctl launch 不会重启它，先终止
    run_command([xcrun_path, 'simctl', 'terminate', udid, bundle_id], timeout=10)
    time.sleep(1)
    code, _, stderr = run_command([xcrun_path, 'simctl', 'launch', udid, bundle_id], timeout=20)
    if code != 0:
        return False, f'启动 WDA 失败: {stderr.strip()[:200]}'
    # 等 WDA HTTP server 就绪
    for _ in range(6):
        time.sleep(1)
        ok, _, _ = probe_wda(timeout=2)
        if ok:
            return True, ''
    return False, 'WDA 已启动但 8100 端口未就绪'


def scan_ios_devices(xcrun_path, warnings):
    if not xcrun_path:
        warnings.append('未找到 xcrun，无法扫描 iOS 模拟器')
        return []

    try:
        data = simctl_devices_json(xcrun_path)
    except (RuntimeError, json.JSONDecodeError) as exc:
        warnings.append(str(exc))
        return []

    runtimes = simctl_runtimes_json(xcrun_path)
    wda_ok, wda_status, wda_detail = probe_wda()

    devices = []
    booted_count = 0
    for runtime_id, entries in (data.get('devices') or {}).items():
        runtime = runtimes.get(runtime_id) or {}
        version = runtime.get('version') or ''
        if not version:
            match = re.search(r'iOS-(\d+)-(\d+)', runtime_id)
            version = f'{match.group(1)}.{match.group(2)}' if match else ''
        for entry in entries:
            if entry.get('state') != 'Booted':
                continue
            booted_count += 1
            udid = entry.get('udid', '')
            issues = []
            if not wda_ok:
                # WDA 未就绪时尝试拉起模拟器里已安装的 WDA，再探测一次
                launched, launch_detail = launch_wda(xcrun_path, udid)
                if launched:
                    wda_ok, wda_status, wda_detail = probe_wda()
                if not wda_ok:
                    issues.append(
                        f'WebDriverAgent 不可用：{launch_detail or wda_detail or "请先在模拟器中启动 WebDriverAgent"}'
                    )
            devices.append({
                'platform': 'ios',
                'device_id': udid,
                'name': entry.get('name') or 'iPhone Simulator',
                'ios_version': version,
                'is_simulator': True,
                'connection_type': 'emulator',
                'wda_ready': wda_ok,
                'wda_version': (wda_status.get('build') or {}).get('version', '') if wda_ok else '',
                'can_connect': bool(wda_ok),
                'issues': issues,
            })

    if booted_count > 1:
        warnings.append(
            f'检测到 {booted_count} 台已启动模拟器；WDA 默认通过 8100 端口访问，'
            '多模拟器同时运行时只能可靠访问其中一台，建议只保留一台。'
        )
    return devices


def connect_ios_device(xcrun_path, device_id):
    if not xcrun_path:
        raise RuntimeError('未找到 xcrun，无法连接 iOS 模拟器')

    data = simctl_devices_json(xcrun_path)
    runtimes = simctl_runtimes_json(xcrun_path)
    target = None
    for runtime_id, entries in (data.get('devices') or {}).items():
        for entry in entries:
            if entry.get('udid') == device_id:
                target = (runtime_id, entry)
                break
        if target:
            break

    if not target:
        raise RuntimeError(f'未找到 iOS 模拟器：{device_id}')
    runtime_id, entry = target
    if entry.get('state') != 'Booted':
        raise RuntimeError(f'模拟器 {entry.get("name")} 未启动，请先在 Xcode/Simulator 中启动')

    runtime = runtimes.get(runtime_id) or {}
    version = runtime.get('version') or ''
    if not version:
        match = re.search(r'iOS-(\d+)-(\d+)', runtime_id or '')
        version = f'{match.group(1)}.{match.group(2)}' if match else ''

    wda_ok, _, wda_detail = probe_wda()
    if not wda_ok:
        launched, launch_detail = launch_wda(xcrun_path, device_id)
        if launched:
            wda_ok, _, _ = probe_wda()
        else:
            wda_detail = launch_detail
    if not wda_ok:
        raise RuntimeError(
            f'WebDriverAgent 不可用（{wda_detail}）。请在模拟器中启动 WDA 后重试。'
        )

    return {
        'platform': 'ios',
        'device_id': device_id,
        'name': entry.get('name') or 'iPhone Simulator',
        'ios_version': version,
        'connection_type': 'emulator',
        'is_simulator': True,
        'wda_url': WDA_CONTAINER_URL,
        'wda_bundle_id': '',
    }


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------

class BridgeHandler(BaseHTTPRequestHandler):
    server_version = SERVICE_NAME

    def _send_json(self, status_code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status_code, message, detail=''):
        if detail:
            log(f'请求失败: {message} | {detail}')
        self._send_json(status_code, {'message': message, 'detail': detail})

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == '/health':
            self._send_json(200, {
                'status': 'ok',
                'service': SERVICE_NAME,
                'host': 'macos',
                'time': time.strftime('%Y-%m-%d %H:%M:%S'),
            })
            return
        if path == '/devices/scan':
            self.handle_scan()
            return
        self._error(404, f'未知路径: {path}')

    def do_POST(self):
        path = self.path.split('?', 1)[0]
        if path != '/devices/connect':
            self._error(404, f'未知路径: {path}')
            return

        try:
            length = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(length) if length else b'{}'
            payload = json.loads(raw.decode('utf-8') or '{}')
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._error(400, '请求体不是合法 JSON')
            return

        platform_name = str(payload.get('platform') or '').strip().lower()
        device_id = str(payload.get('device_id') or '').strip()
        if platform_name not in ('android', 'ios') or not device_id:
            self._error(400, '缺少有效的 platform 或 device_id')
            return

        adb_path = find_adb()
        xcrun_path = find_xcrun()
        try:
            if platform_name == 'android':
                device = connect_android_device(adb_path, device_id)
            else:
                device = connect_ios_device(xcrun_path, device_id)
        except RuntimeError as exc:
            self._error(422, str(exc))
            return
        except Exception as exc:
            self._error(500, f'连接设备时发生内部错误: {exc}')
            return

        log(f'连接成功: {platform_name} {device_id}')
        self._send_json(200, {'device': device})

    def handle_scan(self):
        warnings = []
        adb_path = find_adb()
        xcrun_path = find_xcrun()

        devices = []
        try:
            devices.extend(scan_android_devices(adb_path, warnings))
        except Exception as exc:
            warnings.append(f'扫描 Android 设备异常: {exc}')
        try:
            devices.extend(scan_ios_devices(xcrun_path, warnings))
        except Exception as exc:
            warnings.append(f'扫描 iOS 模拟器异常: {exc}')

        log(f'扫描完成: android/ios 共 {len(devices)} 台, warnings={warnings}')
        self._send_json(200, {
            'devices': devices,
            'warnings': warnings,
            'capabilities': {
                'adb_path': adb_path,
                'xcrun_path': xcrun_path,
                'host': 'macos',
                'wda_container_url': WDA_CONTAINER_URL,
            },
        })

    def log_message(self, fmt, *args):  # noqa: N802 - 覆盖默认日志，避免逐请求刷屏
        pass


def main():
    parser = argparse.ArgumentParser(description='RunnerGo Mac 设备桥接服务')
    parser.add_argument('--host', default='0.0.0.0', help='监听地址（默认 0.0.0.0）')
    parser.add_argument('--port', type=int, default=8765, help='监听端口（默认 8765）')
    args = parser.parse_args()

    adb_path = find_adb()
    xcrun_path = find_xcrun()
    log(f'{SERVICE_NAME} 启动: http://{args.host}:{args.port}')
    log(f'adb={adb_path or "未找到"} xcrun={xcrun_path or "未找到"}')

    server = ThreadingHTTPServer((args.host, args.port), BridgeHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log('收到中断，退出')
        server.server_close()
        sys.exit(0)


if __name__ == '__main__':
    main()
