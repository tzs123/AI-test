# -*- coding: utf-8 -*-
"""APP Agent 四期的设备性能、崩溃日志与视觉变化联合观察。"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import timedelta
from typing import Any, Dict, Optional

from django.conf import settings
from django.utils import timezone


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(',', '').strip())
    except (TypeError, ValueError):
        return default


def _adb_output(device_id: str, args: list[str], timeout: float = 4.0) -> str:
    if not device_id:
        return ''
    try:
        from .utils.airtest_base import _resolve_adb_path

        adb_path = _resolve_adb_path()
        result = subprocess.run(
            [adb_path, '-s', device_id, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='ignore',
            timeout=timeout,
            check=False,
        )
        return str(result.stdout or '')[:100000]
    except Exception:
        return ''


def _parse_cpu(text: str, package_name: str) -> Optional[float]:
    escaped = re.escape(package_name)
    patterns = (
        rf'([\d.]+)%\s+\d+/{escaped}(?:\s|:|$)',
        rf'([\d.]+)%\s+TOTAL.*{escaped}',
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return round(_number(match.group(1)), 2)
    return None


def _parse_memory(text: str) -> Dict[str, Any]:
    pss_match = re.search(r'TOTAL\s+PSS:\s*([\d,]+)', text, re.I)
    private_match = re.search(r'TOTAL\s+PRIVATE\s+DIRTY:\s*([\d,]+)', text, re.I)
    if not pss_match:
        pss_match = re.search(r'^\s*TOTAL\s+([\d,]+)', text, re.I | re.M)
    total_pss_kb = int(_number(pss_match.group(1))) if pss_match else None
    return {
        'total_pss_kb': total_pss_kb,
        'memory_mb': round(total_pss_kb / 1024, 2) if total_pss_kb is not None else None,
        'private_dirty_kb': int(_number(private_match.group(1))) if private_match else None,
    }


def _parse_gfx(text: str) -> Dict[str, Any]:
    total_match = re.search(r'Total frames rendered:\s*(\d+)', text, re.I)
    janky_match = re.search(r'Janky frames:\s*(\d+)\s*\(([\d.]+)%\)', text, re.I)
    percentile_95 = re.search(r'95th percentile:\s*(\d+)ms', text, re.I)
    jank_percent = _number(janky_match.group(2)) if janky_match else None
    return {
        'total_frames': int(total_match.group(1)) if total_match else None,
        'janky_frames': int(janky_match.group(1)) if janky_match else None,
        'jank_percent': round(jank_percent, 2) if jank_percent is not None else None,
        'fps_estimate': round(max(0.0, 60 * (1 - jank_percent / 100)), 2) if jank_percent is not None else None,
        'frame_time_p95_ms': int(percentile_95.group(1)) if percentile_95 else None,
    }


def _parse_battery(text: str) -> Dict[str, Any]:
    def field(name: str):
        match = re.search(rf'^\s*{re.escape(name)}:\s*(.+)$', text, re.I | re.M)
        return match.group(1).strip() if match else None

    level = field('level')
    temperature = field('temperature')
    return {
        'level_percent': int(_number(level)) if level is not None else None,
        'temperature_c': round(_number(temperature) / 10, 1) if temperature is not None else None,
        'status': field('status'),
        'powered_usb': str(field('USB powered') or '').lower() == 'true',
        'powered_ac': str(field('AC powered') or '').lower() == 'true',
    }


def _redact_log_line(line: str) -> str:
    line = re.sub(r'(?<!\d)1\d{10}(?!\d)', '1**********', str(line or ''))
    line = re.sub(r'(?<!\d)\d{8,}(?!\d)', lambda match: f'{match.group(0)[:2]}***{match.group(0)[-2:]}', line)
    line = re.sub(r'(?i)bearer\s+[a-z0-9._~+\-/]+=*', 'Bearer ***', line)
    line = re.sub(r'eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+', 'JWT.***', line)
    line = re.sub(r'(?i)(password|passwd|token|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+', r'\1=***', line)
    line = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '***@***', line)
    return line[:500]


def _parse_crash_log(text: str, package_name: str) -> Dict[str, Any]:
    lines = [line for line in str(text or '').splitlines() if line.strip()]
    package_indexes = [
        index for index, line in enumerate(lines)
        if not package_name or package_name in line
    ]
    scoped_indexes = set()
    for index in package_indexes:
        scoped_indexes.update(range(max(0, index - 6), min(len(lines), index + 7)))
    scoped_lines = [lines[index] for index in sorted(scoped_indexes)]
    package_lines = [line for line in scoped_lines if not package_name or package_name in line]
    crash_lines = [
        line for line in scoped_lines
        if any(keyword in line.casefold() for keyword in (
            'fatal exception', 'androidruntime', 'anr in', 'fatal signal',
            'tombstoned', 'outofmemoryerror', 'failed to allocate',
        ))
    ]
    evidence_lines = list(dict.fromkeys([*package_lines, *crash_lines]))
    signatures = []
    joined = '\n'.join(scoped_lines).casefold()
    rules = (
        ('fatal_exception', ('fatal exception', 'androidruntime')),
        ('anr', ('anr in', 'input dispatching timed out')),
        ('native_crash', ('fatal signal', 'tombstoned')),
        ('out_of_memory', ('outofmemoryerror', 'failed to allocate')),
    )
    for name, keywords in rules:
        if any(keyword in joined for keyword in keywords):
            signatures.append(name)
    return {
        'detected': bool(signatures),
        'signatures': signatures,
        'log_excerpt': [_redact_log_line(line) for line in evidence_lines[-30:]],
    }


def _absolute_media_path(evidence: Dict[str, Any]) -> str:
    path = str((evidence or {}).get('path') or '')
    if not path:
        return ''
    if os.path.isabs(path):
        return path
    return os.path.join(settings.MEDIA_ROOT, path)


def _visual_metrics(current_path: str, previous_evidence: Dict[str, Any]) -> Dict[str, Any]:
    result = {
        'available': False,
        'change_ratio': None,
        'entropy': None,
        'perceptual_hash': '',
    }
    if not current_path or not os.path.isfile(current_path):
        return result
    try:
        from PIL import Image, ImageChops, ImageStat

        with Image.open(current_path) as current_image:
            current = current_image.convert('L').resize((64, 64))
            mean = ImageStat.Stat(current).mean[0]
            bits = ''.join('1' if pixel >= mean else '0' for pixel in current.getdata())
            result.update({
                'available': True,
                'entropy': round(float(current.entropy()), 3),
                'perceptual_hash': f'{int(bits, 2):0256x}',
            })
            previous_path = _absolute_media_path(previous_evidence)
            if previous_path and os.path.isfile(previous_path):
                with Image.open(previous_path) as previous_image:
                    previous = previous_image.convert('L').resize((64, 64))
                    difference = ImageChops.difference(current, previous)
                    result['change_ratio'] = round(ImageStat.Stat(difference).mean[0] / 255, 4)
    except Exception:
        return result
    return result


def collect_runtime_telemetry(session, runner, screenshot_path: str) -> Dict[str, Any]:
    """采集一份有超时上限的遥测快照；iOS 暂保留视觉指标并声明设备指标不可用。"""
    task = session.task
    device = task.device
    package_name = task.app_package.package_name if task.app_package else runner.package_name
    previous_screenshot = (session.current_observation or {}).get('screenshot') or {}
    telemetry = {
        'captured_at': timezone.now().isoformat(),
        'platform': device.platform,
        'device_metrics_available': device.platform == 'android',
        'performance': {},
        'battery': {},
        'crash': {'detected': False, 'signatures': [], 'log_excerpt': []},
        'visual': _visual_metrics(screenshot_path, previous_screenshot),
        'risks': [],
    }
    if device.platform != 'android' or not package_name:
        return telemetry

    cpu_text = _adb_output(device.device_id, ['shell', 'dumpsys', 'cpuinfo', package_name])
    memory_text = _adb_output(device.device_id, ['shell', 'dumpsys', 'meminfo', package_name])
    gfx_text = _adb_output(device.device_id, ['shell', 'dumpsys', 'gfxinfo', package_name])
    battery_text = _adb_output(device.device_id, ['shell', 'dumpsys', 'battery'])
    log_since = (timezone.localtime() - timedelta(minutes=3)).strftime('%m-%d %H:%M:%S.000')
    log_text = _adb_output(
        device.device_id,
        ['logcat', '-d', '-T', log_since, '*:E'],
        timeout=5,
    )
    cpu_percent = _parse_cpu(cpu_text, package_name)
    telemetry['performance'] = {
        'cpu_percent': cpu_percent,
        **_parse_memory(memory_text),
        **_parse_gfx(gfx_text),
    }
    telemetry['battery'] = _parse_battery(battery_text)
    telemetry['crash'] = _parse_crash_log(log_text, package_name)

    performance = telemetry['performance']
    battery = telemetry['battery']
    if cpu_percent is not None and cpu_percent >= 80:
        telemetry['risks'].append('high_cpu')
    if performance.get('memory_mb') is not None and performance['memory_mb'] >= 1024:
        telemetry['risks'].append('high_memory')
    if performance.get('fps_estimate') is not None and performance['fps_estimate'] < 30:
        telemetry['risks'].append('low_fps')
    if battery.get('temperature_c') is not None and battery['temperature_c'] >= 45:
        telemetry['risks'].append('overheating')
    if telemetry['crash']['detected']:
        telemetry['risks'].append('crash_or_anr')
    return telemetry


def merge_telemetry_summary(summary: Dict[str, Any], telemetry: Dict[str, Any]) -> Dict[str, Any]:
    summary = dict(summary or {})
    samples = int(summary.get('samples') or 0) + 1
    performance = telemetry.get('performance') or {}
    battery = telemetry.get('battery') or {}
    cpu = performance.get('cpu_percent')
    memory = performance.get('memory_mb')
    fps = performance.get('fps_estimate')
    level = battery.get('level_percent')
    temperature = battery.get('temperature_c')

    def maximum(key: str, value: Any):
        if value is not None:
            summary[key] = max(float(summary.get(key) or value), float(value))

    def minimum(key: str, value: Any):
        if value is not None:
            summary[key] = min(float(summary.get(key) if summary.get(key) is not None else value), float(value))

    maximum('max_cpu_percent', cpu)
    maximum('max_memory_mb', memory)
    maximum('max_temperature_c', temperature)
    minimum('min_fps_estimate', fps)
    minimum('min_battery_percent', level)
    summary['samples'] = samples
    summary['crash_count'] = int(summary.get('crash_count') or 0) + int(bool((telemetry.get('crash') or {}).get('detected')))
    summary['risks'] = sorted(set((summary.get('risks') or []) + (telemetry.get('risks') or [])))
    summary['latest'] = telemetry
    return summary
