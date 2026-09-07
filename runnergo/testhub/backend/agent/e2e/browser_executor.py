from __future__ import annotations

import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from django.conf import settings

from apps.core.outbound import validate_outbound_http_url

from .contracts import BrowserRunRequest, E2EContractError

logger = logging.getLogger(__name__)

_SENSITIVE_KEY = re.compile(r'(authorization|cookie|token|secret|password|passwd|pwd|验证码|密码)', re.I)
_BEARER_VALUE = re.compile(r'(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+')


class BrowserExecutor:
    """Execute validated E2E steps with Playwright and retain bounded evidence."""

    def __init__(
        self,
        *,
        artifact_root: str | os.PathLike[str] | None = None,
        channel: str | None = None,
        executable_path: str | os.PathLike[str] | None = None,
        max_events: int = 1000,
        on_step: Callable[[dict[str, Any]], None] | None = None,
    ):
        media_root = Path(getattr(settings, 'MEDIA_ROOT', Path.cwd() / 'media'))
        self.artifact_root = Path(artifact_root or media_root / 'e2e')
        configured_channel = getattr(settings, 'E2E_BROWSER_CHANNEL', os.getenv('E2E_BROWSER_CHANNEL', ''))
        self.channel = (channel if channel is not None else configured_channel).strip() or None
        configured_executable = getattr(
            settings,
            'E2E_BROWSER_EXECUTABLE_PATH',
            os.getenv('E2E_BROWSER_EXECUTABLE_PATH', ''),
        )
        raw_executable = executable_path if executable_path is not None else configured_executable
        self.executable_path = str(raw_executable).strip() or None
        self.max_events = min(max(int(max_events), 100), 5000)
        self.on_step = on_step
        self.console_logs: list[dict[str, Any]] = []
        self.network_logs: list[dict[str, Any]] = []
        self.page_errors: list[dict[str, Any]] = []
        self._truncated = {'console': False, 'network': False, 'page_errors': False}

    def run(self, payload: BrowserRunRequest | dict[str, Any]) -> dict[str, Any]:
        request = payload if isinstance(payload, BrowserRunRequest) else BrowserRunRequest.from_payload(payload)
        run_id = request.trace_id or uuid.uuid4().hex
        run_dir = self._run_dir(request.task_id, run_id)
        screenshots_dir = run_dir / 'screenshots'
        videos_dir = run_dir / 'videos'
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        if request.record_video:
            videos_dir.mkdir(parents=True, exist_ok=True)

        started_at = _utc_now()
        started_clock = time.monotonic()
        step_results: list[dict[str, Any]] = []
        video = None
        video_path = ''
        browser = context = page = playwright = None
        fatal_error = ''

        try:
            from playwright.sync_api import sync_playwright

            playwright = sync_playwright().start()
            launch_options: dict[str, Any] = {'headless': request.headless}
            if self.channel:
                launch_options['channel'] = self.channel
            elif self.executable_path:
                launch_options['executable_path'] = self.executable_path
            try:
                browser = playwright.chromium.launch(**launch_options)
            except Exception as exc:
                fallback_channel = str(getattr(settings, 'E2E_BROWSER_FALLBACK_CHANNEL', 'chrome') or '').strip()
                if (
                    self.channel
                    or self.executable_path
                    or not fallback_channel
                    or 'Executable doesn\'t exist' not in str(exc)
                ):
                    raise
                logger.info('Bundled Playwright browser is unavailable; trying channel=%s', fallback_channel)
                browser = playwright.chromium.launch(headless=request.headless, channel=fallback_channel)
                self.channel = fallback_channel
            context_options: dict[str, Any] = {
                'viewport': request.viewport,
                'ignore_https_errors': bool(getattr(settings, 'E2E_IGNORE_HTTPS_ERRORS', False)),
            }
            if request.record_video:
                context_options.update({
                    'record_video_dir': str(videos_dir),
                    'record_video_size': request.viewport,
                })
            context = browser.new_context(**context_options)
            context.set_default_timeout(int(getattr(settings, 'E2E_ACTION_TIMEOUT_MS', 15000)))
            context.set_default_navigation_timeout(int(getattr(settings, 'E2E_NAVIGATION_TIMEOUT_MS', 30000)))
            page = context.new_page()
            video = page.video
            self._attach_observers(page)
            self._attach_route_guard(context)

            if request.base_url:
                self._validate_url(request.base_url)

            for sequence, step in enumerate(request.steps, 1):
                if self.on_step:
                    self.on_step(self._running_step_event(page, step, sequence))
                result = self._run_step(page, step, sequence, request.base_url, screenshots_dir, request.screenshot_each_step)
                step_results.append(result)
                if self.on_step:
                    self.on_step(result)
                if result['status'] == 'failed' and not step.get('continue_on_failure'):
                    break
        except Exception as exc:
            logger.exception('Playwright E2E run failed')
            fatal_error = _safe_text(exc)
            if page is not None:
                error_shot = screenshots_dir / 'fatal-error.png'
                try:
                    page.screenshot(path=str(error_shot), full_page=True)
                except Exception:
                    logger.debug('Unable to capture fatal browser screenshot', exc_info=True)
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    logger.debug('Unable to close browser context cleanly', exc_info=True)
            if video is not None and request.record_video:
                try:
                    video_path = str(video.path())
                except Exception:
                    logger.debug('Unable to resolve Playwright video path', exc_info=True)
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    logger.debug('Unable to close browser cleanly', exc_info=True)
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    logger.debug('Unable to stop Playwright cleanly', exc_info=True)

        failed_steps = [item for item in step_results if item['status'] == 'failed']
        status = 'failed' if fatal_error or failed_steps else 'passed'
        finished_at = _utc_now()
        return {
            'status': status,
            'executor': 'playwright',
            'browser': self.channel or (Path(self.executable_path).name if self.executable_path else 'chromium'),
            'task_id': request.task_id,
            'trace_id': run_id,
            'started_at': started_at,
            'finished_at': finished_at,
            'duration_ms': int((time.monotonic() - started_clock) * 1000),
            'summary': {
                'total': len(request.steps),
                'executed': len(step_results),
                'passed': len([item for item in step_results if item['status'] == 'passed']),
                'failed': len(failed_steps) + (1 if fatal_error and not failed_steps else 0),
                'skipped': max(len(request.steps) - len(step_results), 0),
            },
            'steps': step_results,
            'screenshots': [item['screenshot'] for item in step_results if item.get('screenshot')],
            'video': self._artifact(video_path),
            'console_logs': self.console_logs,
            'network_logs': self.network_logs,
            'page_errors': self.page_errors,
            'logs_truncated': self._truncated,
            'error': fatal_error,
        }

    def _run_step(
        self,
        page: Any,
        step: dict[str, Any],
        sequence: int,
        base_url: str,
        screenshots_dir: Path,
        screenshot_each_step: bool,
    ) -> dict[str, Any]:
        started_at = _utc_now()
        started_clock = time.monotonic()
        console_offset = len(self.console_logs)
        network_offset = len(self.network_logs)
        error_offset = len(self.page_errors)
        status = 'passed'
        error = ''
        screenshot = ''
        actual: Any = None

        try:
            actual = self._dispatch(page, step, base_url)
        except Exception as exc:
            status = 'failed'
            error = _safe_text(exc)

        if screenshot_each_step or step['action'] == 'screenshot' or status == 'failed':
            screenshot_path = screenshots_dir / f'{sequence:03d}-{_slug(step["name"])}.png'
            try:
                page.screenshot(path=str(screenshot_path), full_page=True)
                screenshot = self._artifact(str(screenshot_path))
            except Exception as exc:
                logger.warning('Unable to capture screenshot for step %s: %s', sequence, exc)

        return {
            'sequence': sequence,
            'id': step['id'],
            'name': step['name'],
            'action': step['action'],
            'status': status,
            'url': _safe_url(getattr(page, 'url', '')),
            'actual': _redact(actual),
            'expected': _redact(step.get('expected')),
            'error': error,
            'screenshot': screenshot,
            'console_logs': self.console_logs[console_offset:],
            'network_logs': self.network_logs[network_offset:],
            'page_errors': self.page_errors[error_offset:],
            'started_at': started_at,
            'finished_at': _utc_now(),
            'duration_ms': int((time.monotonic() - started_clock) * 1000),
        }

    def _running_step_event(self, page: Any, step: dict[str, Any], sequence: int) -> dict[str, Any]:
        return {
            'sequence': sequence,
            'id': step['id'],
            'name': step['name'],
            'action': step['action'],
            'status': 'running',
            'url': _safe_url(getattr(page, 'url', '')),
            'actual': None,
            'expected': _redact(step.get('expected')),
            'error': '',
            'screenshot': '',
            'console_logs': [],
            'network_logs': [],
            'page_errors': [],
            'started_at': _utc_now(),
            'finished_at': '',
            'duration_ms': 0,
        }

    def _dispatch(self, page: Any, step: dict[str, Any], base_url: str) -> Any:
        action = step['action']
        timeout = step['timeout_ms']
        if action == 'navigate':
            target_url = urljoin(base_url, str(step.get('url') or step.get('value') or ''))
            target_url = self._validate_url(target_url)
            response = page.goto(target_url, wait_until='domcontentloaded', timeout=timeout)
            page.wait_for_load_state('domcontentloaded', timeout=timeout)
            return {'url': _safe_url(page.url), 'status_code': response.status if response else None, 'title': page.title()}
        if action == 'wait_for_url':
            expected = str(step.get('expected') or step.get('value') or step.get('url') or '')
            page.wait_for_url(expected, timeout=timeout)
            return page.url
        if action == 'wait_for_text':
            expected = str(step.get('expected') or step.get('value') or '')
            page.get_by_text(expected, exact=False).first.wait_for(state='visible', timeout=timeout)
            return expected
        if action == 'assert_url':
            expected = str(step.get('expected') or '')
            if expected not in page.url:
                raise AssertionError(f'URL 断言失败，期望包含 {expected!r}，实际为 {_safe_url(page.url)!r}')
            return page.url
        if action == 'assert_title':
            expected = str(step.get('expected') or '')
            page.wait_for_function(
                'expected => document.title.includes(expected)',
                arg=expected,
                timeout=timeout,
            )
            actual = page.title()
            if expected not in actual:
                raise AssertionError(f'标题断言失败，期望包含 {expected!r}，实际为 {actual!r}')
            return actual
        if action == 'screenshot':
            return {'captured': True}
        if action == 'submit' and not step.get('target'):
            page.keyboard.press('Enter')
            return {'submitted': True}

        locator = self._locator(page, step.get('target') or {})
        if action == 'wait_for_element':
            locator.wait_for(state='visible', timeout=timeout)
            return True
        if action == 'assert_visible':
            locator.wait_for(state='visible', timeout=timeout)
            if not locator.is_visible():
                raise AssertionError('元素不可见')
            return True
        if action == 'assert_text':
            expected = str(step.get('expected') or '')
            actual = locator.inner_text(timeout=timeout)
            if expected not in actual:
                raise AssertionError(f'文本断言失败，期望包含 {expected!r}，实际为 {actual!r}')
            return actual
        if action == 'assert_value':
            expected = str(step.get('expected') or '')
            actual = locator.input_value(timeout=timeout)
            if actual != expected:
                raise AssertionError(f'值断言失败，期望 {expected!r}，实际为 {actual!r}')
            return actual
        if action == 'fill':
            locator.fill(str(step.get('value') or ''), timeout=timeout)
            return {'filled': True}
        if action == 'click':
            locator.click(timeout=timeout)
            return {'clicked': True}
        if action == 'submit':
            locator.click(timeout=timeout)
            return {'submitted': True}
        if action == 'select':
            selected = locator.select_option(str(step.get('value')), timeout=timeout)
            return {'selected': selected}
        if action == 'check':
            locator.check(timeout=timeout)
            return {'checked': True}
        if action == 'uncheck':
            locator.uncheck(timeout=timeout)
            return {'checked': False}
        if action == 'press':
            locator.press(str(step.get('value')), timeout=timeout)
            return {'pressed': str(step.get('value'))}
        if action == 'hover':
            locator.hover(timeout=timeout)
            return {'hovered': True}
        raise E2EContractError(f'未实现的浏览器动作: {action}')

    def _locator(self, page: Any, target: dict[str, Any]):
        index = int(target.get('index') or 0)
        exact = bool(target.get('exact', False))
        if target.get('test_id'):
            locator = page.get_by_test_id(str(target['test_id']))
        elif target.get('label'):
            locator = page.get_by_label(str(target['label']), exact=exact)
        elif target.get('placeholder'):
            locator = page.get_by_placeholder(str(target['placeholder']), exact=exact)
        elif target.get('role'):
            options = {'exact': exact}
            if target.get('name'):
                options['name'] = str(target['name'])
            locator = page.get_by_role(str(target['role']), **options)
        elif target.get('text'):
            locator = page.get_by_text(str(target['text']), exact=exact)
        elif target.get('css'):
            locator = page.locator(str(target['css']))
        else:
            raise E2EContractError('动作缺少可定位的 target')
        locator = locator.nth(index)
        if locator.count() < 1:
            raise AssertionError(f'未找到目标元素: {_redact(target)}')
        return locator

    def _attach_observers(self, page: Any) -> None:
        page.on('console', lambda message: self._append_event('console', {
            'time': _utc_now(),
            'type': message.type,
            'text': _safe_text(message.text),
            'location': _redact(message.location),
        }))
        page.on('pageerror', lambda error: self._append_event('page_errors', {
            'time': _utc_now(),
            'error': _safe_text(error),
        }))
        page.on('request', lambda request: self._append_event('network', {
            'time': _utc_now(),
            'event': 'request',
            'method': request.method,
            'resource_type': request.resource_type,
            'url': _safe_url(request.url),
        }))
        page.on('response', lambda response: self._append_event('network', {
            'time': _utc_now(),
            'event': 'response',
            'status': response.status,
            'ok': response.ok,
            'url': _safe_url(response.url),
        }))
        page.on('requestfailed', lambda request: self._append_event('network', {
            'time': _utc_now(),
            'event': 'request_failed',
            'method': request.method,
            'url': _safe_url(request.url),
            'failure': _redact(request.failure),
        }))

    def _attach_route_guard(self, context: Any) -> None:
        def guard(route: Any) -> None:
            url = route.request.url
            scheme = urlsplit(url).scheme.lower()
            if scheme in {'http', 'https'}:
                try:
                    self._validate_url(url)
                except ValueError as exc:
                    self._append_event('network', {
                        'time': _utc_now(),
                        'event': 'blocked',
                        'url': _safe_url(url),
                        'failure': str(exc),
                    })
                    route.abort('blockedbyclient')
                    return
            route.continue_()

        context.route('**/*', guard)

    def _validate_url(self, value: str) -> str:
        return validate_outbound_http_url(value, label='E2E 测试地址')

    def _append_event(self, kind: str, event: dict[str, Any]) -> None:
        target = {
            'console': self.console_logs,
            'network': self.network_logs,
            'page_errors': self.page_errors,
        }[kind]
        if len(target) >= self.max_events:
            self._truncated[kind] = True
            return
        target.append(_redact(event))

    def _run_dir(self, task_id: str, run_id: str) -> Path:
        safe_task = _slug(task_id or 'adhoc')
        safe_run = _slug(run_id)
        path = self.artifact_root / safe_task / safe_run
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _artifact(self, path: str) -> str:
        if not path:
            return ''
        media_root = Path(getattr(settings, 'MEDIA_ROOT', self.artifact_root.parent)).resolve()
        try:
            relative = Path(path).resolve().relative_to(media_root)
        except ValueError:
            return str(Path(path).resolve())
        return f"{getattr(settings, 'MEDIA_URL', '/media/')}{relative.as_posix()}"


def _redact(value: Any, key: str = '') -> Any:
    if _SENSITIVE_KEY.search(str(key)):
        return '***'
    if isinstance(value, dict):
        return {str(item_key): _redact(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value[:200]]
    if isinstance(value, tuple):
        return [_redact(item) for item in value[:200]]
    if isinstance(value, str):
        return _BEARER_VALUE.sub(r'\1***', value)[:10000]
    return value


def _safe_text(value: Any) -> str:
    return str(_redact(str(value)))[:10000]


def _safe_url(value: Any) -> str:
    try:
        parsed = urlsplit(str(value or ''))
        host = parsed.hostname or ''
        netloc = host
        if parsed.port:
            netloc = f'{host}:{parsed.port}'
        return parsed._replace(netloc=netloc, fragment='').geturl()[:2048]
    except ValueError:
        return ''


def _slug(value: Any) -> str:
    normalized = re.sub(r'[^A-Za-z0-9._-]+', '-', str(value or '')).strip('-._')
    return normalized[:80] or uuid.uuid4().hex[:12]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
