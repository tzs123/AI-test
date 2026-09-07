from __future__ import annotations

import logging
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from django.conf import settings

from apps.core.outbound import validate_outbound_http_url

from .browser_executor import _redact
from .contracts import E2EContractError

logger = logging.getLogger(__name__)

ALLOWED_MOBILE_ACTIONS = frozenset({
    'launch_app', 'tap', 'input', 'swipe', 'wait', 'assert_text',
    'assert_visible', 'screenshot', 'back', 'hide_keyboard',
})


class MobileExecutor:
    """Execute validated Android/iOS steps through Appium 2."""

    def __init__(
        self,
        *,
        artifact_root: str | os.PathLike[str] | None = None,
        driver_factory: Callable[..., Any] | None = None,
        adb_path: str = 'adb',
        on_step: Callable[[dict[str, Any]], None] | None = None,
    ):
        media_root = Path(getattr(settings, 'MEDIA_ROOT', Path.cwd() / 'media'))
        self.artifact_root = Path(artifact_root or media_root / 'e2e-mobile')
        self.driver_factory = driver_factory or self._create_driver
        self.adb_path = adb_path
        self.on_step = on_step

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = normalize_mobile_request(payload)
        run_id = request['trace_id'] or uuid.uuid4().hex
        run_dir = self.artifact_root / _slug(request['task_id'] or 'adhoc') / _slug(run_id)
        screenshots_dir = run_dir / 'screenshots'
        videos_dir = run_dir / 'videos'
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        if request['record_video']:
            videos_dir.mkdir(parents=True, exist_ok=True)

        driver = None
        video_path = ''
        step_results = []
        fatal_error = ''
        started_at = _utc_now()
        started_clock = time.monotonic()
        try:
            driver = self.driver_factory(request['server_url'], request)
            if request['record_video']:
                self._start_recording(driver, request['platform'])
            for sequence, step in enumerate(request['steps'], 1):
                result = self._run_step(driver, step, sequence, screenshots_dir)
                step_results.append(result)
                if self.on_step:
                    self.on_step(result)
                if result['status'] == 'failed' and not step['continue_on_failure']:
                    break
        except Exception as exc:
            logger.exception('Appium E2E run failed')
            fatal_error = str(exc)[:20000]
            if driver is not None:
                try:
                    error_path = screenshots_dir / 'fatal-error.png'
                    driver.save_screenshot(str(error_path))
                except Exception:
                    logger.debug('Unable to capture fatal mobile screenshot', exc_info=True)
        finally:
            if driver is not None and request['record_video']:
                video_path = self._stop_recording(driver, request['platform'], videos_dir)
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    logger.debug('Unable to quit Appium driver', exc_info=True)

        device_log = self._device_log(request)
        failed = [item for item in step_results if item['status'] == 'failed']
        return {
            'status': 'failed' if fatal_error or failed else 'passed',
            'executor': 'appium',
            'platform': request['platform'],
            'automation_name': request['automation_name'],
            'task_id': request['task_id'],
            'trace_id': run_id,
            'started_at': started_at,
            'finished_at': _utc_now(),
            'duration_ms': int((time.monotonic() - started_clock) * 1000),
            'summary': {
                'total': len(request['steps']),
                'executed': len(step_results),
                'passed': len([item for item in step_results if item['status'] == 'passed']),
                'failed': len(failed) + (1 if fatal_error and not failed else 0),
                'skipped': max(len(request['steps']) - len(step_results), 0),
            },
            'steps': step_results,
            'screenshots': [item['screenshot'] for item in step_results if item.get('screenshot')],
            'video': self._artifact(video_path),
            'device_log': device_log,
            'error': fatal_error,
        }

    def _create_driver(self, server_url: str, request: dict[str, Any]):
        try:
            from appium import webdriver
            if request['platform'] == 'android':
                from appium.options.android import UiAutomator2Options
                options = UiAutomator2Options().load_capabilities(request['capabilities'])
            else:
                from appium.options.ios import XCUITestOptions
                options = XCUITestOptions().load_capabilities(request['capabilities'])
        except ImportError as exc:
            raise RuntimeError('Appium Python Client 未安装，请安装 Appium-Python-Client') from exc
        return webdriver.Remote(command_executor=server_url, options=options)

    def _run_step(self, driver: Any, step: dict[str, Any], sequence: int, screenshots_dir: Path) -> dict[str, Any]:
        started_at = _utc_now()
        started_clock = time.monotonic()
        status = 'passed'
        error = ''
        actual: Any = None
        try:
            actual = self._dispatch(driver, step)
        except Exception as exc:
            status = 'failed'
            error = str(exc)[:20000]
        screenshot = ''
        if step['action'] == 'screenshot' or status == 'failed' or step.get('screenshot', True):
            path = screenshots_dir / f'{sequence:03d}-{_slug(step["name"])}.png'
            try:
                if driver.save_screenshot(str(path)):
                    screenshot = self._artifact(str(path))
            except Exception:
                logger.debug('Unable to capture mobile step screenshot', exc_info=True)
        return {
            'sequence': sequence,
            'id': step['id'],
            'name': step['name'],
            'action': step['action'],
            'status': status,
            'actual': _redact(actual),
            'expected': _redact(step.get('expected')),
            'error': error,
            'screenshot': screenshot,
            'started_at': started_at,
            'finished_at': _utc_now(),
            'duration_ms': int((time.monotonic() - started_clock) * 1000),
        }

    def _dispatch(self, driver: Any, step: dict[str, Any]) -> Any:
        action = step['action']
        timeout = step['timeout_ms'] / 1000
        if action == 'launch_app':
            if hasattr(driver, 'activate_app') and step.get('value'):
                driver.activate_app(str(step['value']))
            else:
                driver.launch_app()
            return {'launched': True}
        if action == 'wait':
            time.sleep(min(max(float(step.get('value') or 0.5), 0), 10))
            return {'waited': True}
        if action == 'swipe':
            value = step.get('value') or {}
            if not isinstance(value, dict):
                raise E2EContractError('swipe value 必须包含坐标对象')
            driver.swipe(
                int(value['start_x']), int(value['start_y']),
                int(value['end_x']), int(value['end_y']),
                int(value.get('duration_ms') or 500),
            )
            return {'swiped': True}
        if action == 'back':
            driver.back()
            return {'back': True}
        if action == 'hide_keyboard':
            driver.hide_keyboard()
            return {'keyboard_hidden': True}
        if action == 'screenshot':
            return {'captured': True}

        element = self._find_element(driver, step.get('target') or {}, timeout)
        if action == 'tap':
            element.click()
            return {'tapped': True}
        if action == 'input':
            if bool((step.get('target') or {}).get('clear', True)):
                element.clear()
            element.send_keys(str(step.get('value') or ''))
            return {'input': True}
        if action == 'assert_visible':
            if not element.is_displayed():
                raise AssertionError('移动端元素不可见')
            return True
        if action == 'assert_text':
            expected = str(step.get('expected') or step.get('value') or '')
            actual = str(getattr(element, 'text', '') or element.get_attribute('text') or '')
            if expected not in actual:
                raise AssertionError(f'文本断言失败，期望包含 {expected!r}，实际为 {actual!r}')
            return actual
        raise E2EContractError(f'未实现的移动端动作: {action}')

    def _find_element(self, driver: Any, target: dict[str, Any], timeout: float):
        try:
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            raise RuntimeError('selenium 未安装，Appium 元素等待不可用') from exc
        strategies = [
            ('accessibility id', target.get('accessibility_id')),
            ('id', target.get('id') or target.get('resource_id')),
            ('class name', target.get('class_name')),
            ('-android uiautomator', target.get('android_uiautomator')),
            ('-ios predicate string', target.get('ios_predicate')),
            ('xpath', target.get('xpath')),
        ]
        by, value = next(((by, value) for by, value in strategies if value), (None, None))
        if not by:
            raise E2EContractError('移动端动作缺少语义定位 target')
        return WebDriverWait(driver, timeout).until(lambda current: current.find_element(by, str(value)))

    def _start_recording(self, driver: Any, platform: str) -> None:
        try:
            driver.start_recording_screen()
        except Exception:
            logger.warning('%s screen recording is unavailable', platform, exc_info=True)

    def _stop_recording(self, driver: Any, platform: str, videos_dir: Path) -> str:
        try:
            import base64
            payload = driver.stop_recording_screen()
            if not payload:
                return ''
            suffix = 'mp4'
            path = videos_dir / f'{platform}-{uuid.uuid4().hex[:12]}.{suffix}'
            path.write_bytes(base64.b64decode(payload))
            return str(path)
        except Exception:
            logger.warning('%s screen recording could not be saved', platform, exc_info=True)
            return ''

    def _device_log(self, request: dict[str, Any]) -> str:
        if request['platform'] != 'android':
            return 'iOS execution log is available through Appium/XCUITest and WDA server logs.'
        udid = request['capabilities'].get('appium:udid') or request['capabilities'].get('udid')
        command = [self.adb_path]
        if udid:
            command.extend(['-s', str(udid)])
        command.extend(['logcat', '-d', '-t', '1000'])
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
            output = completed.stdout or completed.stderr
            return str(_redact(output))[:100000]
        except Exception as exc:
            return f'ADB logcat unavailable: {exc}'[:2000]

    def _artifact(self, path: str) -> str:
        if not path:
            return ''
        media_root = Path(getattr(settings, 'MEDIA_ROOT', self.artifact_root.parent)).resolve()
        try:
            relative = Path(path).resolve().relative_to(media_root)
        except ValueError:
            return str(Path(path).resolve())
        return f"{getattr(settings, 'MEDIA_URL', '/media/')}{relative.as_posix()}"


def normalize_mobile_request(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise E2EContractError('移动端执行请求必须是对象')
    platform = str(payload.get('platform') or '').lower()
    if platform not in {'android', 'ios'}:
        raise E2EContractError('platform 仅支持 android/ios')
    server_url = str(payload.get('server_url') or '').strip()
    try:
        server_url = validate_outbound_http_url(
            server_url,
            label='Appium Server 地址',
            allowed_hosts=getattr(settings, 'E2E_APPIUM_ALLOWED_HOSTS', []),
        )
    except ValueError as exc:
        raise E2EContractError(str(exc)) from exc
    raw_steps = payload.get('steps')
    if not isinstance(raw_steps, list) or not raw_steps or len(raw_steps) > 200:
        raise E2EContractError('steps 必须是 1 到 200 个步骤的数组')
    steps = []
    for index, raw in enumerate(raw_steps, 1):
        if not isinstance(raw, dict):
            raise E2EContractError(f'第 {index} 步必须是对象')
        action = str(raw.get('action') or '').lower()
        if action not in ALLOWED_MOBILE_ACTIONS:
            raise E2EContractError(f'不支持的移动端动作: {action}')
        target = raw.get('target') or {}
        if not isinstance(target, dict):
            raise E2EContractError(f'第 {index} 步 target 必须是对象')
        steps.append({
            'id': str(raw.get('id') or index)[:64],
            'name': str(raw.get('name') or raw.get('description') or f'步骤 {index}')[:300],
            'action': action,
            'target': target,
            'value': raw.get('value'),
            'expected': raw.get('expected'),
            'timeout_ms': min(max(int(raw.get('timeout_ms') or 15000), 100), 120000),
            'continue_on_failure': bool(raw.get('continue_on_failure', False)),
            'screenshot': bool(raw.get('screenshot', True)),
        })
    capabilities = dict(payload.get('capabilities') or {})
    automation_name = 'UiAutomator2' if platform == 'android' else 'XCUITest'
    capabilities.setdefault('platformName', 'Android' if platform == 'android' else 'iOS')
    capabilities.setdefault('appium:automationName', automation_name)
    capabilities.setdefault('appium:newCommandTimeout', 300)
    capabilities.setdefault('appium:noReset', bool(payload.get('no_reset', True)))
    return {
        'platform': platform,
        'automation_name': automation_name,
        'server_url': server_url.rstrip('/'),
        'capabilities': capabilities,
        'steps': steps,
        'task_id': str(payload.get('task_id') or '')[:128],
        'trace_id': str(payload.get('trace_id') or '')[:128],
        'record_video': bool(payload.get('record_video', True)),
    }


def _slug(value: Any) -> str:
    return re.sub(r'[^A-Za-z0-9._-]+', '-', str(value or '')).strip('-._')[:80] or 'run'


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
