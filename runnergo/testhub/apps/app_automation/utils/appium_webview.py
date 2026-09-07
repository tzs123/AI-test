# -*- coding: utf-8 -*-
"""Minimal Appium WebDriver client for hybrid Android/iOS applications.

The project already starts an Appium 2 server, but the execution image does not
ship the Selenium/Appium Python clients. This module uses the W3C WebDriver HTTP
protocol directly so hybrid-page DOM actions remain lightweight and optional.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

W3C_ELEMENT_KEY = 'element-6066-11e4-a52e-4f735466cecf'


class AppiumWebDriverError(RuntimeError):
    """Raised when Appium returns a WebDriver protocol error."""


def xpath_literal(value: Any) -> str:
    """Return a safe XPath string literal for arbitrary user-facing text."""
    text = str(value or '')
    if "'" not in text:
        return f"'{text}'"
    if '"' not in text:
        return f'"{text}"'
    parts = text.split("'")
    expressions = []
    for index, part in enumerate(parts):
        if part:
            expressions.append(f"'{part}'")
        if index < len(parts) - 1:
            expressions.append('"\'"')
    return f"concat({', '.join(expressions)})"


class AppiumWebViewClient:
    """Lazy Appium session used only when a WebView DOM action is requested."""

    def __init__(
        self,
        server_url: str,
        device_id: str,
        automation_name: str = 'UiAutomator2',
        package_name: str = '',
        platform_name: str = 'Android',
        web_driver_agent_url: str = '',
        request_timeout: float = 15,
        remote_adb_host: str = '',
        adb_port: int = 5037,
        chromedriver_executable_dir: str = '',
        session: Optional[requests.Session] = None,
    ):
        self.server_url = str(server_url or 'http://127.0.0.1:4723').rstrip('/')
        self.device_id = str(device_id or '').strip()
        self.automation_name = str(automation_name or 'UiAutomator2').strip()
        self.package_name = str(package_name or '').strip()
        normalized_platform = str(platform_name or 'Android').strip().lower()
        self.platform_name = 'iOS' if normalized_platform == 'ios' else 'Android'
        self.web_driver_agent_url = str(web_driver_agent_url or '').strip()
        self.request_timeout = max(1.0, float(request_timeout))
        socket_value = str(os.environ.get('ADB_SERVER_SOCKET') or '').strip()
        socket_host = ''
        socket_port = adb_port
        if socket_value.startswith('tcp:'):
            socket_parts = socket_value.split(':')
            if len(socket_parts) >= 3:
                socket_host = ':'.join(socket_parts[1:-1])
                try:
                    socket_port = int(socket_parts[-1])
                except (TypeError, ValueError):
                    socket_port = adb_port
        self.remote_adb_host = str(remote_adb_host or socket_host or '').strip()
        self.adb_port = int(socket_port or 5037)
        self.chromedriver_executable_dir = str(
            chromedriver_executable_dir
            or os.environ.get('APPIUM_CHROMEDRIVER_DIR')
            or ''
        ).strip()
        self.http = session or requests.Session()
        self.session_id = ''
        self.current_context = 'NATIVE_APP'

    def _url(self, path: str) -> str:
        return f"{self.server_url}/{str(path or '').lstrip('/')}"

    @staticmethod
    def _error_message(payload: Any, fallback: str) -> str:
        value = payload.get('value') if isinstance(payload, dict) else None
        if isinstance(value, dict):
            return str(value.get('message') or value.get('error') or fallback)
        return str(fallback)

    def _request(self, method: str, path: str, **kwargs) -> Any:
        response = self.http.request(
            method,
            self._url(path),
            timeout=self.request_timeout,
            **kwargs,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {'value': response.text}
        value = payload.get('value') if isinstance(payload, dict) else None
        protocol_error = isinstance(value, dict) and value.get('error')
        if response.status_code >= 400 or protocol_error:
            raise AppiumWebDriverError(
                self._error_message(payload, f'Appium HTTP {response.status_code}')
            )
        return value

    def connect(self) -> str:
        if self.session_id:
            return self.session_id
        if not self.device_id:
            raise AppiumWebDriverError('缺少设备 ID，无法创建 Appium 会话')

        capabilities = {
            'platformName': self.platform_name,
            'appium:automationName': self.automation_name,
            'appium:udid': self.device_id,
            'appium:deviceName': self.device_id,
            'appium:noReset': True,
            'appium:autoLaunch': False,
            'appium:newCommandTimeout': 180,
        }
        if self.platform_name == 'iOS':
            if self.package_name:
                capabilities['appium:bundleId'] = self.package_name
            if self.web_driver_agent_url:
                capabilities['appium:webDriverAgentUrl'] = self.web_driver_agent_url
        else:
            capabilities['appium:dontStopAppOnReset'] = True
            if self.package_name:
                capabilities['appium:appPackage'] = self.package_name
            if self.remote_adb_host:
                capabilities['appium:remoteAdbHost'] = self.remote_adb_host
                capabilities['appium:adbPort'] = self.adb_port
            if self.chromedriver_executable_dir:
                capabilities['appium:chromedriverExecutableDir'] = (
                    self.chromedriver_executable_dir
                )

        response = self.http.post(
            self._url('/session'),
            json={
                'capabilities': {
                    'alwaysMatch': capabilities,
                    'firstMatch': [{}],
                }
            },
            timeout=max(self.request_timeout, 45),
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {'value': response.text}
        if response.status_code >= 400:
            raise AppiumWebDriverError(
                self._error_message(payload, f'创建 Appium 会话失败: HTTP {response.status_code}')
            )
        value = payload.get('value') if isinstance(payload, dict) else {}
        self.session_id = str(
            (payload.get('sessionId') if isinstance(payload, dict) else '')
            or (value.get('sessionId') if isinstance(value, dict) else '')
            or ''
        )
        if not self.session_id:
            raise AppiumWebDriverError('创建 Appium 会话失败：响应中没有 sessionId')
        logger.info('Appium WebDriver 会话已创建: device=%s', self.device_id)
        return self.session_id

    def close(self) -> None:
        if not self.session_id:
            return
        session_id = self.session_id
        self.session_id = ''
        self.current_context = 'NATIVE_APP'
        try:
            self.http.delete(
                self._url(f'/session/{session_id}'),
                timeout=self.request_timeout,
            )
        except Exception as exc:
            logger.debug('关闭 Appium WebDriver 会话失败: %s', exc)

    def contexts(self) -> List[str]:
        self.connect()
        value = self._request('GET', f'/session/{self.session_id}/contexts')
        return [str(item) for item in (value or [])]

    def switch_context(self, name: str) -> str:
        self.connect()
        self._request(
            'POST',
            f'/session/{self.session_id}/context',
            json={'name': str(name)},
        )
        self.current_context = str(name)
        return self.current_context

    def switch_to_native(self) -> str:
        if not self.session_id:
            return 'NATIVE_APP'
        return self.switch_context('NATIVE_APP')

    def switch_to_webview(
        self,
        preferred_context: str = '',
        timeout: float = 5,
        interval: float = 0.25,
    ) -> Tuple[str, Dict[str, Any]]:
        deadline = time.monotonic() + max(0.1, float(timeout))
        attempts = []
        preferred = str(preferred_context or '').strip()
        while True:
            contexts = self.contexts()
            webviews = [item for item in contexts if item.upper().startswith('WEBVIEW')]
            attempts.append(contexts)
            selected = ''
            if preferred:
                selected = next(
                    (
                        item for item in webviews
                        if item == preferred or preferred.lower() in item.lower()
                    ),
                    '',
                )
            if not selected and self.package_name:
                selected = next(
                    (item for item in webviews if self.package_name.lower() in item.lower()),
                    '',
                )
            if not selected and webviews:
                selected = webviews[0]
            if selected:
                self.switch_context(selected)
                return selected, {
                    'matched': True,
                    'selected_context': selected,
                    'context_attempts': attempts,
                }
            if time.monotonic() >= deadline:
                break
            time.sleep(min(max(0.05, float(interval)), max(0.0, deadline - time.monotonic())))
        raise AppiumWebDriverError(
            f'未发现 WEBVIEW 上下文，当前 contexts={attempts[-1] if attempts else []}'
        )

    @staticmethod
    def locator_candidates(config: Dict[str, Any]) -> List[Dict[str, str]]:
        config = dict(config or {})
        candidates: List[Dict[str, str]] = []

        def add(strategy: str, using: str, value: Any) -> None:
            normalized = str(value or '').strip()
            if normalized and all(
                item['using'] != using or item['value'] != normalized
                for item in candidates
            ):
                candidates.append({
                    'strategy': strategy,
                    'using': using,
                    'value': normalized,
                })

        add('webview_css', 'css selector', config.get('webview_css') or config.get('css_selector'))
        add('webview_xpath', 'xpath', config.get('webview_xpath'))

        resource_id = config.get('resource_id')
        if resource_id:
            add('resource_id', 'xpath', f"//*[@id={xpath_literal(resource_id)}]")

        accessibility = config.get('accessibility_id') or config.get('content_desc')
        if accessibility:
            literal = xpath_literal(accessibility)
            add(
                'accessibility',
                'xpath',
                f"//*[@aria-label={literal} or @name={literal} or @title={literal}]",
            )

        text_value = config.get('webview_text') or config.get('text')
        if text_value:
            literal = xpath_literal(text_value)
            add(
                'text',
                'xpath',
                f"//*[normalize-space(.)={literal} or @placeholder={literal} or @value={literal}]",
            )
        add('xpath', 'xpath', config.get('xpath'))
        return candidates

    def find_element(self, config: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        self.connect()
        attempts = []
        for candidate in self.locator_candidates(config):
            try:
                value = self._request(
                    'POST',
                    f'/session/{self.session_id}/element',
                    json={
                        'using': candidate['using'],
                        'value': candidate['value'],
                    },
                )
                element_id = str(
                    (value.get(W3C_ELEMENT_KEY) if isinstance(value, dict) else '')
                    or (value.get('ELEMENT') if isinstance(value, dict) else '')
                    or ''
                )
                if element_id:
                    attempts.append({**candidate, 'matched': True})
                    return element_id, {
                        'matched': True,
                        'selected_strategy': candidate['strategy'],
                        'attempts': attempts,
                    }
                attempts.append({**candidate, 'matched': False, 'error': 'missing-element-id'})
            except Exception as exc:
                attempts.append({**candidate, 'matched': False, 'error': str(exc)})
        raise AppiumWebDriverError(
            f"WebView DOM 元素定位失败，已尝试 {[item['strategy'] for item in attempts]}"
        )

    def click(self, config: Dict[str, Any]) -> Dict[str, Any]:
        element_id, diagnostics = self.find_element(config)
        self._request(
            'POST',
            f'/session/{self.session_id}/element/{element_id}/click',
            json={},
        )
        return {**diagnostics, 'action': 'click', 'element_id': element_id}

    @staticmethod
    def _normalize_checkbox_state(value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        normalized = str(value or '').strip().lower()
        if normalized in {'true', '1', 'yes', 'on', 'checked', 'selected'}:
            return True
        if normalized in {'false', '0', 'no', 'off', 'unchecked', 'unselected'}:
            return False
        return None

    def _read_checkbox_state(self, element_id: str) -> Optional[bool]:
        endpoints = (
            f'/session/{self.session_id}/element/{element_id}/selected',
            f'/session/{self.session_id}/element/{element_id}/attribute/checked',
            f'/session/{self.session_id}/element/{element_id}/attribute/aria-checked',
        )
        for endpoint in endpoints:
            try:
                state = self._normalize_checkbox_state(
                    self._request('GET', endpoint)
                )
                if state is not None:
                    return state
            except Exception:
                continue
        return None

    def _resolve_checkbox_element(self, element_id: str) -> Tuple[str, bool]:
        """从协议文字/label/link 定位结果中解析真正的 checkbox 控件。"""
        script = """
const source = arguments[0];
if (!source) return null;
const selector = 'input[type="checkbox"],input[type="radio"],[role="checkbox"],[role="switch"]';
const isControl = (node) => Boolean(node && node.matches && node.matches(selector));
if (isControl(source)) return source;

if (source.tagName && source.tagName.toLowerCase() === 'label' && source.htmlFor) {
  const linked = document.getElementById(source.htmlFor);
  if (isControl(linked)) return linked;
}

const label = source.closest ? source.closest('label') : null;
if (label) {
  if (label.htmlFor) {
    const linked = document.getElementById(label.htmlFor);
    if (isControl(linked)) return linked;
  }
  const nested = label.querySelector(selector);
  if (nested) return nested;
}

let container = source;
for (let depth = 0; depth < 4 && container; depth += 1) {
  const nested = container.querySelector ? container.querySelector(selector) : null;
  if (nested) return nested;
  container = container.parentElement;
}
return null;
"""
        try:
            value = self.execute_script(
                script,
                [{W3C_ELEMENT_KEY: element_id}],
            )
            resolved_id = str(
                (value.get(W3C_ELEMENT_KEY) if isinstance(value, dict) else '')
                or (value.get('ELEMENT') if isinstance(value, dict) else '')
                or ''
            )
            if resolved_id:
                return resolved_id, True
        except Exception as exc:
            logger.debug('解析 WebView checkbox 控件失败: %s', exc)
        return element_id, False

    def set_checkbox(
        self,
        config: Dict[str, Any],
        desired_state: str = 'checked',
    ) -> Dict[str, Any]:
        """只点击 WebView checkbox/switch 本体，绝不点击旁边文字或链接。"""
        located_element_id, diagnostics = self.find_element(config)
        element_id, control_resolved = self._resolve_checkbox_element(
            located_element_id
        )
        desired = str(desired_state or 'checked').strip().lower()
        if desired not in {'checked', 'unchecked', 'toggle'}:
            raise ValueError(f'不支持的复选框目标状态: {desired_state}')
        if not control_resolved:
            raise AppiumWebDriverError(
                '未解析到真实 checkbox/switch 控件，已拒绝点击协议文字或链接'
            )
        before = self._read_checkbox_state(element_id)
        expected = (not before) if desired == 'toggle' and before is not None else {
            'checked': True,
            'unchecked': False,
        }.get(desired)
        clicked = desired == 'toggle' or before is None or before != expected
        if clicked:
            self._request(
                'POST',
                f'/session/{self.session_id}/element/{element_id}/click',
                json={},
            )
        after = self._read_checkbox_state(element_id)
        verified = expected is not None and after == expected
        return {
            **diagnostics,
            'action': 'checkbox',
            'located_element_id': located_element_id,
            'element_id': element_id,
            'control_resolved': control_resolved,
            'desired_state': desired,
            'before': before,
            'after': after,
            'clicked': clicked,
            'verified': verified,
        }

    def _resolve_text_input_element(self, element_id: str) -> Tuple[str, bool]:
        """Resolve a label/text match to the actual editable WebView control."""
        script = """
const source = arguments[0];
if (!source) return null;
const selector = [
  'textarea',
  '[contenteditable="true"]',
  '[role="textbox"]',
  'input:not([type])',
  'input[type="text"]',
  'input[type="search"]',
  'input[type="tel"]',
  'input[type="number"]',
  'input[type="email"]',
  'input[type="password"]',
  'input[type="url"]'
].join(',');
const isControl = (node) => Boolean(node && node.matches && node.matches(selector));
const cssAttrEscape = (value) => String(value || '').replace(/["\\\\]/g, '\\\\$&');
if (isControl(source)) return source;

const byId = (id) => id ? document.getElementById(id) : null;
if (source.tagName && source.tagName.toLowerCase() === 'label') {
  const linked = byId(source.htmlFor);
  if (isControl(linked)) return linked;
}

const labelled = source.id
  ? document.querySelector(`${selector}[aria-labelledby~="${cssAttrEscape(source.id)}"]`)
  : null;
if (isControl(labelled)) return labelled;

const label = source.closest ? source.closest('label') : null;
if (label) {
  const linked = byId(label.htmlFor);
  if (isControl(linked)) return linked;
  const nested = label.querySelector(selector);
  if (isControl(nested)) return nested;
}

let container = source;
for (let depth = 0; depth < 5 && container; depth += 1) {
  const nested = container.querySelector ? container.querySelector(selector) : null;
  if (isControl(nested)) return nested;
  let sibling = container.nextElementSibling;
  for (let hops = 0; hops < 3 && sibling; hops += 1) {
    if (isControl(sibling)) return sibling;
    const nestedSibling = sibling.querySelector ? sibling.querySelector(selector) : null;
    if (isControl(nestedSibling)) return nestedSibling;
    sibling = sibling.nextElementSibling;
  }
  container = container.parentElement;
}
return null;
"""
        try:
            value = self.execute_script(
                script,
                [{W3C_ELEMENT_KEY: element_id}],
            )
            resolved_id = str(
                (value.get(W3C_ELEMENT_KEY) if isinstance(value, dict) else '')
                or (value.get('ELEMENT') if isinstance(value, dict) else '')
                or ''
            )
            if resolved_id:
                return resolved_id, True
        except Exception as exc:
            logger.debug('解析 WebView 输入控件失败: %s', exc)
        return element_id, False

    def input_text(
        self,
        config: Dict[str, Any],
        value: Any,
        clear_first: bool = True,
        send_enter: bool = False,
    ) -> Dict[str, Any]:
        located_element_id, diagnostics = self.find_element(config)
        element_id, control_resolved = self._resolve_text_input_element(located_element_id)
        if not control_resolved:
            raise AppiumWebDriverError(
                '未解析到真实 input/textarea/textbox 控件，已拒绝向普通文本节点输入'
            )
        if clear_first:
            try:
                self._request(
                    'POST',
                    f'/session/{self.session_id}/element/{element_id}/clear',
                    json={},
                )
            except Exception as exc:
                diagnostics['clear_error'] = str(exc)
        text_value = str(value or '') + ('\ue007' if send_enter else '')
        self._request(
            'POST',
            f'/session/{self.session_id}/element/{element_id}/value',
            json={'text': text_value, 'value': list(text_value)},
        )
        return {
            **diagnostics,
            'action': 'input',
            'located_element_id': located_element_id,
            'element_id': element_id,
            'control_resolved': control_resolved,
            'value_length': len(str(value or '')),
            'send_enter': bool(send_enter),
        }

    def execute_script(self, script: str, args: Optional[List[Any]] = None) -> Any:
        """在当前 WebView 上下文执行同步 JavaScript。"""
        self.connect()
        return self._request(
            'POST',
            f'/session/{self.session_id}/execute/sync',
            json={
                'script': str(script or ''),
                'args': list(args or []),
            },
        )
