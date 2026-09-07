"""浏览器控制器：通过 Playwright MCP 驱动 Chrome 页面、截图和 DOM 分析。

不依赖固定 XPath：所有定位都通过语义（role/name/label/placeholder/文本）完成。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Optional

from backend import settings
from backend.url_security import validate_outbound_url

# ---- 移动端 H5 虚拟键盘 / 弹框 TouchEvent 派发脚本（与平台执行器一致） ----

_TAP_KEY_JS = r"""(char) => {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const fire = el => {
    try {
      el.dispatchEvent(new TouchEvent('touchstart', {bubbles: true, cancelable: true}));
      el.dispatchEvent(new TouchEvent('touchend', {bubbles: true, cancelable: true}));
    } catch (_) { el.click(); }
  };
  const selectors = [
    '.car-keyboard-grids-btn',
    '[class*="keyboard"] button', '[class*="keyboard"] [role="button"]',
    '[class*="keyboard"] li', '[class*="keyboard"] span', '[class*="keyboard"] div',
    '[role="dialog"] button', '[role="dialog"] [role="button"]', '[role="dialog"] span', '[role="dialog"] div'
  ];
  const candidates = Array.from(document.querySelectorAll(selectors.join(','))).filter(el =>
    visible(el) && clean(el.textContent) === clean(char) &&
    (el.matches('.car-keyboard-grids-btn,button,[role="button"],li,span') || el.children.length === 0)
  );
  if (!candidates.length) return false;
  fire(candidates[0]);
  return true;
}
"""

_SWITCH_KEYBOARD_JS = r"""() => {
  const visible = el => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const fire = el => {
    try {
      el.dispatchEvent(new TouchEvent('touchstart', {bubbles: true, cancelable: true}));
      el.dispatchEvent(new TouchEvent('touchend', {bubbles: true, cancelable: true}));
    } catch (_) { el.click(); }
  };
  const specific = document.querySelector('.car-keyboard-change');
  if (specific && visible(specific)) {
    const zh = specific.querySelector('.zh');
    if (!zh || zh.classList.contains('active')) fire(specific);
    return true;
  }
  const candidate = Array.from(document.querySelectorAll(
    '[class*="keyboard"] button,[class*="keyboard"] [role="button"],[role="dialog"] button,[role="dialog"] [role="button"]'
  )).find(el => visible(el) && /中\s*\/\s*英|英文|字母/.test(String(el.textContent || '').trim()));
  if (!candidate) return false;
  fire(candidate);
  return true;
}
"""

_CONFIRM_KEYBOARD_JS = r"""() => {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const fire = el => {
    try {
      el.dispatchEvent(new TouchEvent('touchstart', {bubbles: true, cancelable: true}));
      el.dispatchEvent(new TouchEvent('touchend', {bubbles: true, cancelable: true}));
    } catch (_) { el.click(); }
  };
  const specific = document.querySelector('.car-tooltips-submit');
  if (specific && visible(specific)) { fire(specific); return true; }
  const candidate = Array.from(document.querySelectorAll(
    '[class*="keyboard"] button,[class*="keyboard"] [role="button"],[role="dialog"] button,[role="dialog"] [role="button"],.van-popup button'
  )).find(el => visible(el) && /^(确认|完成|确定)$/.test(clean(el.textContent)));
  if (!candidate) return false;
  fire(candidate);
  return true;
}
"""

_TAP_TEXT_JS = r"""(text) => {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const fire = el => {
    try {
      el.dispatchEvent(new TouchEvent('touchstart', {bubbles: true, cancelable: true}));
      el.dispatchEvent(new TouchEvent('touchend', {bubbles: true, cancelable: true}));
    } catch (_) { el.click(); }
  };
  const selectors = [
    '.van-picker-column-item, .van-picker__columns li, li',
    '[class*="picker"] li, [class*="picker"] [role="button"], [class*="picker"] button',
    '.van-popup li, .van-popup button, .van-popup [role="button"]',
    'button', '[role="button"]', 'li'
  ];
  const all = Array.from(document.querySelectorAll(selectors.join(','))).filter(el =>
    visible(el) && clean(el.textContent) === clean(text)
  );
  const leaf = all.find(el => el.children.length === 0) || all.find(el =>
    el.matches('li,button,[role="button"],span,.van-picker-column-item')
  );
  if (!leaf) return false;
  fire(leaf);
  return true;
}
"""
_TAP_HANDLE_JS = r"""(el) => {
  if (!(el instanceof Element)) return false;
  try {
    el.dispatchEvent(new TouchEvent('touchstart', {bubbles: true, cancelable: true}));
    el.dispatchEvent(new TouchEvent('touchend', {bubbles: true, cancelable: true}));
  } catch (_) { el.click(); }
  return true;
}
"""



def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class BrowserController:
    """Playwright MCP 同步封装。"""

    def __init__(self, *, headless: bool = True, channel: str = "") -> None:
        self.headless = headless
        # 支持系统浏览器（如 BROWSER_AGENT_CHANNEL=chrome），避免下载 Playwright 浏览器
        self.channel = (channel or os.environ.get("BROWSER_AGENT_CHANNEL", "")).strip() or None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def open(self, url: str, timeout_ms: int = 30000) -> dict:
        from playwright.sync_api import sync_playwright

        try:
            validate_outbound_url(url)
        except ValueError as exc:
            raise ValueError(f"目标地址被安全策略拦截: {exc}") from exc
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.headless,
            channel=self.channel,
        )
        self._context = self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=True,
        )
        self._page = self._context.new_page()
        response = self._page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        self._page.wait_for_timeout(800)
        return {
            "url": self._page.url,
            "title": self._page.title(),
            "status_code": response.status if response else None,
            "mcp": {
                "driver": "playwright",
                "browser": "chrome" if self.channel == "chrome" else "chromium",
                "visual_observation": True,
            },
        }

    def screenshot(self, name: str = "browser") -> str:
        if not self._page:
            raise RuntimeError("页面未打开")
        os.makedirs(settings.SCREENSHOTS_DIR, exist_ok=True)
        filename = f"{name}-{uuid.uuid4().hex[:8]}.png"
        path = os.path.join(settings.SCREENSHOTS_DIR, filename)
        self._page.screenshot(path=path, full_page=True)
        return path

    def accessibility_snapshot(self, max_entries: int = 300) -> list[dict]:
        """使用 CDP Accessibility 快照识别可交互元素（role/name）。"""
        if not self._page:
            return []
        try:
            cdp = self._context.new_cdp_session(self._page)
            result = cdp.send("Accessibility.getFullAXTree")
            entries = []
            for node in result.get("nodes", []):
                name = (node.get("name") or {}).get("value", "")
                role = (node.get("role") or {}).get("value", "")
                if not role or role in {"generic", "none", "text", "StaticText"}:
                    continue
                backend_node = node.get("backendDOMNodeId")
                if not name and role not in {"textbox", "button", "link"}:
                    continue
                entries.append({
                    "role": role,
                    "name": name,
                    "node_id": backend_node,
                })
            return entries[:max_entries]
        except Exception:  # noqa: BLE001 - 快照失败时退回 DOM 分析
            return self._dom_interactive_elements()

    def _dom_interactive_elements(self) -> list[dict]:
        """DOM 兜底：提取 input/button/textarea/select 的语义信息。"""
        return self._page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                document.querySelectorAll('input,button,textarea,select,a,[role]').forEach((el, idx) => {
                    const tag = el.tagName.toLowerCase();
                    const role = el.getAttribute('role') || tag;
                    let name = '';
                    if (el.id) name = el.id;
                    if (!name && el.getAttribute('placeholder')) name = el.getAttribute('placeholder');
                    if (!name && el.getAttribute('aria-label')) name = el.getAttribute('aria-label');
                    if (!name && el.getAttribute('name')) name = el.getAttribute('name');
                    if (!name && el.textContent) name = el.textContent.trim().slice(0, 60);
                    if (!name && el.getAttribute('type') === 'text') name = 'text-input-' + idx;
                    if (!name) return;
                    const key = tag + '|' + name;
                    if (seen.has(key)) return;
                    seen.add(key);
                    out.push({role, name, tag, index: idx});
                });
                return out.slice(0, 300);
            }"""
        )

    def fill_by(self, *, label: str = "", placeholder: str = "", role: str = "textbox", value: str, index: int = 0) -> bool:
        """语义填充：优先按 placeholder/label/role 定位，不依赖 XPath。"""
        page = self._page
        if placeholder:
            locator = page.get_by_placeholder(placeholder)
            if locator.count() > index:
                locator.nth(index).fill(value)
                return True
        if label:
            locator = page.get_by_label(label, exact=False)
            if locator.count() > index:
                locator.nth(index).fill(value)
                return True
        locator = page.get_by_role(role)
        if locator.count() > index:
            locator.nth(index).fill(value)
            return True
        if role == "textbox":
            locator = page.locator("input[type='text'], input:not([type]), textarea")
            if locator.count() > index:
                locator.nth(index).fill(value)
                return True
        return False

    def click_by(self, *, text: str = "", role: str = "button", index: int = 0) -> bool:
        """语义点击：按文本/角色定位。"""
        page = self._page
        if text:
            locator = page.get_by_role(role, name=text, exact=False)
            if locator.count() > index:
                locator.nth(index).click()
                return True
            link = page.get_by_text(text, exact=False)
            if link.count() > index:
                link.nth(index).click()
                return True
        locator = page.get_by_role(role)
        if locator.count() > index:
            locator.nth(index).click()
            return True
        return False

    def press_enter(self) -> None:
        if self._page:
            self._page.keyboard.press("Enter")

    def wait(self, ms: int = 500) -> None:
        if self._page:
            self._page.wait_for_timeout(ms)

    def current_url(self) -> str:
        return self._page.url if self._page else ""

    def page_text(self) -> str:
        if not self._page:
            return ""
        try:
            return self._page.inner_text("body")[:3000]
        except Exception:  # noqa: BLE001
            return ""
    # ============ YAML 用例语义执行能力 ============

    def locator(self, selector: str):
        """按 CSS 选择器定位（兜底使用）。"""
        if not self._page:
            raise RuntimeError("页面未打开")
        return self._page.locator(selector)

    def locator_count(self, locator) -> int:
        try:
            return locator.count()
        except Exception:  # noqa: BLE001
            return 0

    def locator_visible(self, locator) -> bool:
        try:
            return self.locator_count(locator) > 0 and locator.first.is_visible()
        except Exception:  # noqa: BLE001
            return False

    def semantic_locator(self, *, role: str = "", name: str = "", label: str = "",
                         placeholder: str = "", element_id: str = "", css: str = ""):
        """语义优先定位：role/name -> label -> placeholder -> id -> css。

        同名元素存在多个时（如两个“联系人姓名”），优先用可见的唯一 id 消歧，
        否则退回第一个可见元素，避免填错字段。
        """
        page = self._page
        if role and name:
            loc = page.get_by_role(role, name=name, exact=False)
            count = self.locator_count(loc)
            if count == 1:
                return loc
            if count > 1:
                if element_id:
                    try:
                        id_loc = page.locator(f"#{element_id}")
                        if self.locator_count(id_loc) == 1 and id_loc.is_visible():
                            return id_loc
                    except Exception:  # noqa: BLE001
                        pass
                return self._first_visible(loc)
        if label:
            loc = page.get_by_label(label, exact=False)
            if self.locator_count(loc) > 0:
                return loc
        if placeholder:
            loc = page.get_by_placeholder(placeholder)
            if self.locator_count(loc) > 0:
                return loc
        if element_id:
            loc = page.locator(f"#{element_id}")
            if self.locator_count(loc) > 0:
                return loc
        if css:
            try:
                loc = page.locator(css)
                if self.locator_count(loc) > 0:
                    return loc
            except Exception:  # noqa: BLE001
                pass
        return None

    def _first_visible(self, locator):
        """多匹配时返回第一个可见元素，避免选中隐藏的校验/占位字段。"""
        count = self.locator_count(locator)
        if count == 0:
            return None
        for index in range(min(count, 10)):
            try:
                if locator.nth(index).is_visible():
                    return locator.nth(index)
            except Exception:  # noqa: BLE001
                continue
        return locator.nth(0)

    def click_locator(self, locator, *, double: bool = False, index: int = 0) -> bool:
        try:
            target = locator.nth(index) if self.locator_count(locator) > index else locator
            if double:
                target.dblclick()
            else:
                target.click()
            return True
        except Exception:  # noqa: BLE001
            pass
        # 移动端 H5 组件兜底：TouchEvent 派发
        try:
            target = locator.nth(index) if self.locator_count(locator) > index else locator
            handle = target.element_handle()
            return bool(self._page.evaluate(_TAP_HANDLE_JS, handle))
        except Exception:  # noqa: BLE001
            return False

    def fill_locator(self, locator, value: str, index: int = 0) -> bool:
        try:
            target = locator.nth(index) if self.locator_count(locator) > index else locator
            target.fill(str(value))
            return True
        except Exception:  # noqa: BLE001
            try:
                target = locator.nth(index) if self.locator_count(locator) > index else locator
                target.click()
                self._page.keyboard.type(str(value))
                return True
            except Exception:  # noqa: BLE001
                return False

    def click_keyboard_key(self, key: str) -> bool:
        """虚拟键盘按键：TouchEvent 派发点击（移动端 H5 组件 click() 不生效）。"""
        page = self._page
        try:
            if page.evaluate(_TAP_KEY_JS, key):
                self.wait(200)
                return True
            # 找不到按键：先切换到字母数字键盘再试
            if page.evaluate(_SWITCH_KEYBOARD_JS):
                self.wait(200)
                if page.evaluate(_TAP_KEY_JS, key):
                    self.wait(200)
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def switch_plate_keyboard(self) -> bool:
        """切换车牌键盘（省份 -> 字母数字）。"""
        try:
            return bool(self._page.evaluate(_SWITCH_KEYBOARD_JS))
        except Exception:  # noqa: BLE001
            return False

    def confirm_plate_keyboard(self) -> bool:
        """点击车牌键盘确认（该组件监听原生 click，需真实点击而非 TouchEvent）。"""
        page = self._page
        selectors = ['.car-tooltips-submit', '.car-tooltips', '.car-tooltip']
        for selector in selectors:
            try:
                loc = page.locator(selector)
                if self.locator_count(loc) > 0:
                    loc.first.click(force=True, timeout=5000)
                    self.wait(300)
                    return True
            except Exception:  # noqa: BLE001
                continue
        try:
            return bool(page.evaluate(_CONFIRM_KEYBOARD_JS))
        except Exception:  # noqa: BLE001
            return False

    def click_popup_text(self, text: str) -> bool:
        """点击弹框/页面中的选项文本（TouchEvent 优先，失败回退真实点击），失败自动重试。"""
        page = self._page
        picker_selector = (
            ".van-picker-column__item, .van-picker-column-item, .van-picker__columns li, "
            ".van-picker__columns [role=option], .van-cascader__option, .van-cascader__options li, .van-popup li"
        )
        for attempt in range(3):
            if attempt:
                self.wait(600)
            try:
                if page.evaluate(_TAP_TEXT_JS, text):
                    return True
            except Exception:  # noqa: BLE001
                pass
            # 精确匹配 picker 列项，用 Playwright 真实点击（自动滚动到可见）
            try:
                index = page.evaluate(
                    """(wanted) => {
                      const clean = v => String(v || '').replace(/\s+/g, ' ').trim();
                      const items = Array.from(document.querySelectorAll(
                        '.van-picker-column__item, .van-picker-column-item, .van-picker__columns li, ' +
                        '.van-picker__columns [role=option], .van-cascader__option, .van-cascader__options li, .van-popup li'
                      ));
                      const target = clean(wanted);
                      const i = items.findIndex(el => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.visibility !== 'hidden' && style.display !== 'none' &&
                          rect.width > 1 && rect.height > 1 && clean(el.textContent) === target;
                      });
                      return i;
                    }""",
                    text,
                )
                if isinstance(index, int) and index >= 0:
                    try:
                        page.locator(picker_selector).nth(index).click(timeout=3000)
                        return True
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass
            candidates = [
                page.get_by_role("button", name=text, exact=True),
                page.get_by_role("option", name=text, exact=True),
                page.get_by_role("listitem", name=text, exact=True),
                page.get_by_text(text, exact=True),
            ]
            for loc in candidates:
                if self.locator_count(loc) > 0:
                    try:
                        loc.first.click(force=True, timeout=3000)
                        return True
                    except Exception:  # noqa: BLE001
                        continue
        return False

    def upload_files(self, files, index: int = 0) -> bool:
        """上传文件：定位 input[type=file] 并按序号 set_input_files（支持正反面多上传框）。"""
        page = self._page
        loc = page.locator("input[type=file]")
        count = self.locator_count(loc)
        if count == 0:
            return False
        try:
            target = loc.nth(min(index, count - 1))
            target.set_input_files(list(files))
            self.wait(2500)  # 等待上传 + OCR 识别
            return True
        except Exception:  # noqa: BLE001
            return False

    def scroll_by(self, delta_y: int) -> bool:
        try:
            self._page.evaluate(f"window.scrollBy(0, {int(delta_y)})")
            self._page.wait_for_timeout(200)
            return True
        except Exception:  # noqa: BLE001
            try:
                self._page.mouse.wheel(0, int(delta_y))
                return True
            except Exception:  # noqa: BLE001
                return False

    def detect_error_toast(self) -> str:
        """检测页面错误提示（toast/dialog/错误文案），返回提示文本，无则空串。"""
        page = self._page
        try:
            error_selectors = [
                ".van-toast--fail, .van-toast--error, [class*='toast'][class*='error']",
                ".van-dialog__message, [class*='dialog']",
                ".van-notify--danger, [class*='notify'][class*='danger']",
                "[class*='error-msg'], [class*='error_msg'], [class*='err-msg']",
            ]
            for selector in error_selectors:
                loc = page.locator(selector)
                if self.locator_count(loc) > 0:
                    try:
                        text = loc.first.inner_text()
                        if text and text.strip():
                            return text.strip()[:200]
                    except Exception:  # noqa: BLE001
                        return "检测到错误提示元素"
            for keyword in ("请输入", "请选择", "请填写", "错误", "失败", "无效", "不能为空", "不正确"):
                loc = page.locator(f"text={keyword}")
                if self.locator_count(loc) > 0:
                    try:
                        text = loc.first.inner_text().strip()
                        if text and len(text) < 80:
                            return text
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass
        return ""

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception:  # noqa: BLE001
            pass
        self._browser = None
        self._context = None
        self._page = None
        self._playwright = None
