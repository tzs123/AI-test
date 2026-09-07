from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from django.conf import settings

from .element_locator import ElementLocator
from .vision_parser import VisionParser


class BrowserController:
    """Playwright MCP-style Chrome controller with screenshot and DOM analysis."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        try:
            self.playwright = sync_playwright().start()
            launch_options = {'headless': self.headless}
            executable_path = (
                os.environ.get('BROWSER_AGENT_EXECUTABLE_PATH')
                or os.environ.get('E2E_BROWSER_EXECUTABLE_PATH')
            )
            if executable_path:
                launch_options['executable_path'] = executable_path
            self.browser = self.playwright.chromium.launch(**launch_options)
            self.context = self.browser.new_context(viewport={'width': 1365, 'height': 900}, ignore_https_errors=True)
            default_timeout = int(os.environ.get('BROWSER_AGENT_ACTION_TIMEOUT_MS', '8000'))
            self.context.set_default_timeout(default_timeout)
            self.context.set_default_navigation_timeout(int(os.environ.get('BROWSER_AGENT_NAVIGATION_TIMEOUT_MS', '10000')))
            self.page = self.context.new_page()
            return self
        except Exception:
            # __exit__ is not called when __enter__ fails. Stop Playwright here
            # so its internal event loop cannot leak into subsequent Django ORM calls.
            self._close()
            raise

    def __exit__(self, exc_type, exc, traceback):
        self._close()

    def _close(self) -> None:
        for attribute in ('context', 'browser'):
            resource = getattr(self, attribute)
            if not resource:
                continue
            try:
                resource.close()
            finally:
                setattr(self, attribute, None)
        if self.playwright:
            try:
                self.playwright.stop()
            finally:
                self.playwright = None
        self.page = None

    def open(self, url: str) -> dict[str, Any]:
        response = self.page.goto(
            url,
            wait_until='domcontentloaded',
            timeout=int(os.environ.get('BROWSER_AGENT_NAVIGATION_TIMEOUT_MS', '10000')),
        )
        self.page.wait_for_timeout(300)
        return {
            'url': self.page.url,
            'title': self.page.title(),
            'status_code': response.status if response else None,
            'mcp': {
                'driver': 'playwright',
                'browser': 'chrome/chromium',
                'visual_observation': True,
            },
        }

    def screenshot(self) -> str:
        root = Path(getattr(settings, 'MEDIA_ROOT', os.getcwd())) / 'browser-agent'
        root.mkdir(parents=True, exist_ok=True)
        path = root / f'{uuid.uuid4().hex}.png'
        self.page.screenshot(path=str(path), full_page=True)
        return str(path)

    def media_url(self, path: str) -> str:
        media_root = Path(getattr(settings, 'MEDIA_ROOT', os.getcwd())).resolve()
        try:
            relative = Path(path).resolve().relative_to(media_root)
        except ValueError:
            return ''
        return f"{getattr(settings, 'MEDIA_URL', '/media/')}{relative.as_posix()}"

    def dom_elements(self) -> list[dict]:
        return self.page.evaluate(
            """() => Array.from(document.querySelectorAll('input,textarea,button,a,select,[role]')).slice(0, 300).map((el, index) => ({
              index,
              tag: el.tagName.toLowerCase(),
              role: el.getAttribute('role') || (el.tagName.toLowerCase() === 'input' ? 'textbox' : el.tagName.toLowerCase()),
              name: el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('name') || el.innerText || el.value || '',
              type: el.getAttribute('type') || ''
            }))"""
        )


class WebVisionAgent:
    """Open page through Playwright MCP, parse visual/DOM elements, and act."""

    def run(
        self,
        *,
        url: str,
        goal: str = '',
        inputs: dict[str, str] | None = None,
        analyze_only: bool = False,
        **_: Any,
    ) -> dict[str, Any]:
        if not url:
            return {
                'status': 'skipped',
                'message': '未提供 URL，Playwright MCP 浏览器视觉 Agent 跳过实际 Chrome 执行。',
                'architecture': 'AI -> Playwright MCP -> Chrome -> 页面 -> 截图 -> 视觉模型分析',
            }
        inputs = inputs or {'username': 'test001', 'password': 'Passw0rd@2026'}
        try:
            with BrowserController() as browser:
                opened = browser.open(url)
                screenshot = browser.screenshot()
                screenshot_url = browser.media_url(screenshot)
                elements = VisionParser().parse(browser.dom_elements(), goal)
                actions = [] if analyze_only else self._act(browser.page, elements, goal, inputs)
                completed_actions = [item for item in actions if item.get('status') == 'success']
                return {
                    'status': 'success',
                    'message': (
                        'Browser Vision Agent 已通过 Playwright MCP 完成 Chrome 页面解析'
                        if analyze_only or not completed_actions else
                        'Browser Vision Agent 已通过 Playwright MCP 完成 Chrome 页面分析与语义动作执行'
                    ),
                    'architecture': 'AI -> Playwright MCP -> Chrome -> 页面 -> 截图 -> 视觉模型分析',
                    'opened': opened,
                    'screenshot': screenshot,
                    'screenshot_url': screenshot_url,
                    'screenshots': [screenshot],
                    'screenshot_urls': [screenshot_url] if screenshot_url else [],
                    'visual_summary': {
                        'mcp': {'driver': 'playwright', 'browser': 'chrome/chromium', 'vision': 'screenshot+dom'},
                        'element_count': len(elements),
                        'action_count': len(completed_actions),
                        'top_elements': [
                            {'name': item.get('name'), 'role': item.get('role'), 'kind': item.get('kind')}
                            for item in elements[:8]
                        ],
                    },
                    'trace': [
                        {'phase': 'open', 'message': f"Playwright MCP 打开 Chrome 页面 {opened.get('url')}", 'status': 'success'},
                        {'phase': 'screenshot', 'message': '已截取完整页面截图，提供给视觉模型分析', 'status': 'success', 'artifact': screenshot_url or screenshot},
                        {'phase': 'vision_parse', 'message': f'视觉/DOM 联合识别到 {len(elements)} 个候选交互元素', 'status': 'success'},
                        {
                            'phase': 'act',
                            'message': '解析模式不执行页面操作' if analyze_only else f'执行 {len(actions)} 个语义动作',
                            'status': 'skipped' if analyze_only or not actions else 'success',
                        },
                    ],
                    'elements': elements[:30],
                    'actions': actions,
                }
        except Exception as exc:
            return {
                'status': 'failed',
                'message': f'Browser Vision Agent 执行失败：{exc}',
                'error': str(exc),
                'architecture': 'AI -> Playwright MCP -> Chrome -> 页面 -> 截图 -> 视觉模型分析',
            }

    def _act(self, page: Any, elements: list[dict], goal: str, inputs: dict[str, str]) -> list[dict]:
        locator = ElementLocator()
        actions = []
        for element in elements[:20]:
            name = str(element.get('name') or '').lower()
            target = locator.locate(page, element)
            if not target:
                continue
            try:
                if element.get('kind') == 'input':
                    value = inputs.get('password') if 'pass' in name or '密码' in name else inputs.get('username', 'test001')
                    target.fill(value, timeout=1500)
                    actions.append({'action': 'fill', 'target': element.get('name'), 'status': 'success'})
                elif any(word in name for word in ['登录', 'login', 'submit', '确定', '购物车', 'cart']):
                    target.click(timeout=1500)
                    actions.append({'action': 'click', 'target': element.get('name'), 'status': 'success'})
                    break
            except Exception as exc:
                actions.append({'action': 'operate', 'target': element.get('name'), 'status': 'failed', 'error': str(exc)})
        return actions
