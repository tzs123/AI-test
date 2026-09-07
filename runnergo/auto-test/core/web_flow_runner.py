"""通用浏览器 UI Flow 执行器。

`flow_version: 2` 用例由浏览器录制器生成，步骤中同时保存主定位器、备用定位器
和元素语义指纹。执行时优先使用 Playwright 语义定位，全部失效后才进入带阈值
和歧义保护的智能匹配。
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from base64 import b64decode
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import yaml

from backend.url_security import guard_playwright_route, validate_outbound_url
from core.mixed_steps import (
    RuntimeDataContext,
    assert_runtime_value,
    execute_api_step,
    execute_database_step,
    redact,
)
from core.protocol_drivers import PROTOCOL_ACTIONS, driver_for
from core.runtime_case import inject_runtime_assertions, resolve_dynamic_value

try:
    import allure
except Exception:  # pragma: no cover - allure 在正式执行镜像中可用
    allure = None


VARIABLE_PATTERN = re.compile(r"\$\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\.\d+)*)\s*\}")
POPUP_SCROLL_DELTA_LIMIT = 88
_MISSING = object()
ERROR_ASSERTION_KEYWORDS = (
    "失败",
    "错误",
    "异常",
    "报错",
    "无法",
    "无效",
    "不支持",
    "超时",
    "格式不正确",
    "不能为空",
    "请上传",
    "请重新上传",
)
LOADING_STATE_TEXT_PATTERN = re.compile(
    r"^(?:(?:页面|数据)?(?:正在)?加载中?|请稍候|请稍等|loading)[.…。!！\s]*$",
    re.IGNORECASE,
)
ASSERTION_ACTIONS = {"assert_visible", "assert_text", "assert_url", "assert_error"}
NON_BROWSER_ACTIONS = {
    "api", "http", "request", "api_request",
    "db", "database", "query", "sql",
    "jmeter", "jmeter_script", "jmx",
} | PROTOCOL_ACTIONS
PAGE_MUTATING_ACTIONS = {
    "goto", "open", "navigate",
    "go_back", "back", "history_back",
    "go_forward", "forward", "history_forward",
    "reload", "refresh_page",
    "click", "double_click", "dblclick", "drag",
    "fill", "input", "select", "check", "uncheck", "upload",
    "scroll", "hover", "press", "popup_select_text",
}


def normalize_scroll_delta(delta_x: int, delta_y: int, popup_only: bool = False) -> Tuple[int, int]:
    """Keep popup picker scrolling granular while leaving normal page scrolling untouched."""
    delta_x = int(delta_x or 0)
    delta_y = int(delta_y if delta_y is not None else 0)
    if not popup_only:
        return delta_x, delta_y
    return delta_x, delta_y


def _run_jmeter_step(step: Dict[str, Any], runtime_context: RuntimeDataContext) -> Dict[str, Any]:
    """Run a JMeter .jmx plan as a non-browser protocol step."""
    from backend import settings

    script = (
        step.get("script")
        or step.get("jmx")
        or step.get("test_plan")
        or step.get("value")
        or ""
    )
    script = str(runtime_context.resolve(script) or "").strip()
    if not script:
        raise ValueError("JMeter 步骤缺少 script/jmx/test_plan")
    if not script.endswith(".jmx"):
        raise ValueError("JMeter 步骤只允许执行 .jmx 脚本")
    candidate = script if os.path.isabs(script) else os.path.join(settings.ROOT, script)
    script_path = os.path.realpath(candidate)
    root = os.path.realpath(settings.ROOT)
    if os.path.commonpath([root, script_path]) != root:
        raise ValueError("JMeter 脚本必须位于 auto-test 工作目录内")
    if not os.path.isfile(script_path):
        raise FileNotFoundError(f"JMeter 脚本不存在: {script}")

    jmeter_bin = (
        os.environ.get("JMETER_BIN")
        or os.environ.get("JMETER_HOME") and os.path.join(os.environ["JMETER_HOME"], "bin", "jmeter")
        or shutil.which("jmeter")
    )
    if not jmeter_bin:
        raise RuntimeError("未找到 JMeter，请配置 JMETER_BIN 或安装 jmeter 命令")

    timeout = max(1, int(step.get("timeout_seconds") or step.get("timeout") or 600))
    props = step.get("properties") if isinstance(step.get("properties"), dict) else {}
    variables = step.get("variables") if isinstance(step.get("variables"), dict) else {}
    with tempfile.NamedTemporaryFile(prefix="runnergo-jmeter-", suffix=".jtl", delete=False) as output:
        jtl_path = output.name
    command = [jmeter_bin, "-n", "-t", script_path, "-l", jtl_path]
    for key, value in {**props, **variables}.items():
        resolved = runtime_context.resolve(value)
        command.append(f"-J{key}={resolved}")

    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=settings.ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    elapsed_ms = round((time.monotonic() - started) * 1000, 2)
    result = {
        "script": os.path.relpath(script_path, settings.ROOT),
        "jtl": jtl_path,
        "exit_code": completed.returncode,
        "duration_ms": elapsed_ms,
        "stdout": (completed.stdout or "")[-2000:],
        "stderr": (completed.stderr or "")[-2000:],
    }
    if completed.returncode != 0:
        exc = RuntimeError(f"JMeter 执行失败，退出码 {completed.returncode}")
        setattr(exc, "runtime_result", result)
        raise exc
    return result

    def clamp(value: int) -> int:
        if not value:
            return 0
        sign = 1 if value > 0 else -1
        return sign * min(abs(value), POPUP_SCROLL_DELTA_LIMIT)

    return clamp(delta_x), clamp(delta_y)


HEAL_CANDIDATES_SCRIPT = r"""
({targetTag}) => {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim().slice(0, 160);
  const visible = el => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' &&
      rect.width > 0 && rect.height > 0;
  };
  const implicitRole = el => {
    const tag = el.tagName.toLowerCase();
    const type = String(el.getAttribute('type') || '').toLowerCase();
    if (tag === 'button') return 'button';
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'select') return 'combobox';
    if (tag === 'li') return 'listitem';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input' && ['button', 'submit', 'reset'].includes(type)) return 'button';
    if (tag === 'input' && type === 'checkbox') return 'checkbox';
    if (tag === 'input' && type === 'radio') return 'radio';
    if (tag === 'input') return 'textbox';
    return '';
  };
  const labelText = el => {
    if (el.labels && el.labels.length) return clean(Array.from(el.labels).map(x => x.innerText).join(' '));
    const parent = el.closest('label');
    return parent ? clean(parent.innerText) : '';
  };
  const cssPath = el => {
    if (!(el instanceof Element)) return '';
    if (el.id && !/\d{5,}/.test(el.id) && !(el.getRootNode() instanceof ShadowRoot)) return `#${CSS.escape(el.id)}`;
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 12) {
      let part = node.tagName.toLowerCase();
      const testid = node.getAttribute('data-testid');
      const root = node.getRootNode();
      const hasStableId = node.id && !/\d{5,}/.test(node.id);
      if (hasStableId) part = `#${CSS.escape(node.id)}`;
      const stableClasses = Array.from(node.classList || []).filter(name =>
        name && !/\d{4,}/.test(name) && !/^(active|selected|disabled|focus|hover|checked|current)$/i.test(name)
      ).slice(0, 2);
      if (!hasStableId && stableClasses.length) {
        part += stableClasses.map(name => `.${CSS.escape(name)}`).join('');
      }
      if (testid) {
        part += `[data-testid="${CSS.escape(testid)}"]`;
        parts.unshift(part);
        if (root instanceof ShadowRoot) {
          node = root.host;
          continue;
        }
        break;
      }
      if (hasStableId && !(root instanceof ShadowRoot)) {
        parts.unshift(part);
        break;
      }
      const name = node.getAttribute('name');
      if (name && !/\d{5,}/.test(name)) part += `[name="${CSS.escape(name)}"]`;
      const parent = node.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter(x => x.tagName === node.tagName);
        if (same.length > 1) part += `:nth-of-type(${same.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      node = parent || (root instanceof ShadowRoot ? root.host : null);
    }
    return parts.join(' ');
  };
  const viewport = { width: Math.max(1, window.innerWidth), height: Math.max(1, window.innerHeight) };
  const result = [];
  const selectors = [
    'button', 'a', 'input', 'textarea', 'select', '[role]', '[onclick]', '[tabindex]',
    '[contenteditable="true"]', '[data-testid]', 'label', 'summary'
  ];
  const normalizedTargetTag = String(targetTag || '').trim().toLowerCase();
  if (/^[a-z][a-z0-9-]*$/.test(normalizedTargetTag)) selectors.push(normalizedTargetTag);
  const selector = Array.from(new Set(selectors)).join(',');
  const roots = [document];
  for (let rootIndex = 0; rootIndex < roots.length; rootIndex += 1) {
    const root = roots[rootIndex];
    for (const host of Array.from(root.querySelectorAll('*'))) {
      if (host.shadowRoot) roots.push(host.shadowRoot);
    }
  }
  const elements = roots.flatMap(root => Array.from(root.querySelectorAll(selector)));
  for (const el of elements) {
    if (!visible(el)) continue;
    const rect = el.getBoundingClientRect();
    const text = clean(el.innerText || el.textContent || el.getAttribute('value'));
    const label = labelText(el);
    const accessibleName = clean(el.getAttribute('aria-label') || label || el.getAttribute('alt') ||
      el.getAttribute('title') || el.getAttribute('placeholder') || text);
    result.push({
      css: cssPath(el),
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || implicitRole(el),
      accessible_name: accessibleName,
      text,
      attrs: {
        testid: el.getAttribute('data-testid') || '',
        id: el.id || '',
        class: String(el.className || ''),
        name: el.getAttribute('name') || '',
        placeholder: el.getAttribute('placeholder') || '',
        type: el.getAttribute('type') || ''
      },
      parent: {
        tag: el.parentElement ? el.parentElement.tagName.toLowerCase() : '',
        text: el.parentElement ? clean(el.parentElement.innerText) : ''
      },
      normalized_position: {
        x: Math.max(0, Math.min(1, (rect.left + rect.width / 2) / viewport.width)),
        y: Math.max(0, Math.min(1, (rect.top + rect.height / 2) / viewport.height))
      }
    });
    if (result.length >= 800) break;
  }
  return result;
}
"""


POINT_TARGET_SCRIPT = r"""
({x, y, targetTag}) => {
  const deepElementFromPoint = (pointX, pointY) => {
    let current = document.elementFromPoint(pointX, pointY);
    let guard = 0;
    while (current && current.shadowRoot && guard < 8) {
      const nested = current.shadowRoot.elementFromPoint(pointX, pointY);
      if (!nested || nested === current) break;
      current = nested;
      guard += 1;
    }
    return current;
  };
  const pointX = Math.max(0, Math.min(window.innerWidth - 1, Number(x) * window.innerWidth));
  const pointY = Math.max(0, Math.min(window.innerHeight - 1, Number(y) * window.innerHeight));
  let element = deepElementFromPoint(pointX, pointY);
  const normalizedTargetTag = String(targetTag || '').trim().toLowerCase();
  if (element && /^[a-z][a-z0-9-]*$/.test(normalizedTargetTag) &&
      element.tagName.toLowerCase() !== normalizedTargetTag) {
    element = element.closest(normalizedTargetTag) || element;
  }
  return element;
}
"""


POINT_CANDIDATE_SCRIPT = r"""
(el) => {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim().slice(0, 160);
  const implicitRole = node => {
    const tag = node.tagName.toLowerCase();
    const type = String(node.getAttribute('type') || '').toLowerCase();
    if (tag === 'button') return 'button';
    if (tag === 'a' && node.hasAttribute('href')) return 'link';
    if (tag === 'select') return 'combobox';
    if (tag === 'li') return 'listitem';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input' && ['button', 'submit', 'reset'].includes(type)) return 'button';
    if (tag === 'input' && type === 'checkbox') return 'checkbox';
    if (tag === 'input' && type === 'radio') return 'radio';
    if (tag === 'input') return 'textbox';
    return '';
  };
  const labelText = node => {
    if (node.labels && node.labels.length) return clean(Array.from(node.labels).map(x => x.innerText).join(' '));
    const parent = node.closest('label');
    return parent ? clean(parent.innerText) : '';
  };
  const rect = el.getBoundingClientRect();
  const text = clean(el.innerText || el.textContent || el.getAttribute('value'));
  const label = labelText(el);
  return {
    tag: el.tagName.toLowerCase(),
    role: el.getAttribute('role') || implicitRole(el),
    accessible_name: clean(el.getAttribute('aria-label') || label || el.getAttribute('alt') ||
      el.getAttribute('title') || el.getAttribute('placeholder') || text),
    text,
    attrs: {
      testid: el.getAttribute('data-testid') || '',
      id: el.id || '',
      class: String(el.className || ''),
      name: el.getAttribute('name') || '',
      placeholder: el.getAttribute('placeholder') || '',
      type: el.getAttribute('type') || ''
    },
    parent: {
      tag: el.parentElement ? el.parentElement.tagName.toLowerCase() : '',
      text: el.parentElement ? clean(el.parentElement.innerText) : ''
    },
    normalized_position: {
      x: Math.max(0, Math.min(1, (rect.left + rect.width / 2) / Math.max(1, window.innerWidth))),
      y: Math.max(0, Math.min(1, (rect.top + rect.height / 2) / Math.max(1, window.innerHeight)))
    }
  };
}
"""


BLOCKING_UI_STATE_SCRIPT = r"""
() => {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    if (!el || !(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number(style.opacity || 1) > 0.01 && rect.width > 1 && rect.height > 1 &&
      rect.bottom > 0 && rect.right > 0 && rect.top < window.innerHeight && rect.left < window.innerWidth;
  };
  const selectors = [
    '.loading', '.van-toast--loading', '.van-overlay .van-loading',
    '[class*="loading-mask"]', '[class*="loading-overlay"]'
  ].join(',');
  for (const el of Array.from(document.querySelectorAll(selectors))) {
    if (!visible(el)) continue;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    const text = clean(el.innerText || el.textContent);
    const viewportArea = Math.max(1, window.innerWidth * window.innerHeight);
    const coverage = (rect.width * rect.height) / viewportArea;
    const fixedLayer = style.position === 'fixed' || style.position === 'absolute';
    const loadingClass = /(?:^|[\s_-])loading(?:[\s_-]|$)|busy/i.test(String(el.className || ''));
    const loadingText = /加载中|正在加载|请稍候|loading/i.test(text);
    if (!loadingText && !(loadingClass && (coverage >= 0.2 || fixedLayer))) continue;
    return {
      found: true,
      tag: el.tagName.toLowerCase(),
      class_name: String(el.className || '').slice(0, 160),
      text: text.slice(0, 160),
      coverage: Math.round(coverage * 1000) / 1000
    };
  }
  return {found: false};
}
"""


PAGE_READY_STATE_SCRIPT = r"""
() => {
  const now = performance.now();
  const body = document.body;
  let probe = window.__runnerGoSmartWaitProbe;
  if (!probe || probe.body !== body) {
    if (probe && probe.observer) probe.observer.disconnect();
    probe = {
      body,
      lastMutationAt: now,
      mutationCount: 0,
      observer: null
    };
    if (body && typeof MutationObserver !== 'undefined') {
      probe.observer = new MutationObserver(records => {
        probe.lastMutationAt = performance.now();
        probe.mutationCount += records.length;
      });
      probe.observer.observe(body, {
        subtree: true,
        childList: true,
        characterData: true,
        attributes: true,
        attributeFilter: ['class', 'style', 'hidden', 'aria-hidden', 'aria-busy', 'disabled']
      });
    }
    window.__runnerGoSmartWaitProbe = probe;
  }
  return {
    ready_state: document.readyState,
    has_body: Boolean(body),
    mutation_age_ms: Math.max(0, Math.round(now - probe.lastMutationAt)),
    mutation_count: probe.mutationCount,
    url: location.href
  };
}
"""


PLATE_KEYBOARD_VISIBLE_SCRIPT = r"""
() => {
  const visible = el => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  return Array.from(document.querySelectorAll(
    '.car-keyboard,[class*="keyboard"],[class*="key-board"],[role="dialog"],.van-popup'
  )).some(visible);
}
"""


PLATE_TAP_KEY_SCRIPT = r"""
(char) => {
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
    } catch (_) {
      el.click();
    }
  };
  const selectors = [
    '.car-keyboard-grids-btn',
    '[class*="keyboard"] button', '[class*="keyboard"] [role="button"]',
    '[class*="keyboard"] li', '[class*="keyboard"] span', '[class*="keyboard"] div',
    '[class*="key-board"] button', '[class*="key-board"] [role="button"]',
    '[role="dialog"] button', '[role="dialog"] [role="button"]', '[role="dialog"] span', '[role="dialog"] div',
    '.van-popup button', '.van-popup [role="button"]', '.van-popup span', '.van-popup div'
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


PLATE_SWITCH_KEYBOARD_SCRIPT = r"""
() => {
  const visible = el => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const fire = el => {
    try {
      el.dispatchEvent(new TouchEvent('touchstart', {bubbles: true, cancelable: true}));
      el.dispatchEvent(new TouchEvent('touchend', {bubbles: true, cancelable: true}));
    } catch (_) {
      el.click();
    }
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


PLATE_CONFIRM_SCRIPT = r"""
() => {
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
    } catch (_) {
      el.click();
    }
  };
  const specific = document.querySelector('.car-tooltips-submit');
  if (specific && visible(specific)) {
    fire(specific);
    return true;
  }
  const candidate = Array.from(document.querySelectorAll(
    '[class*="keyboard"] button,[class*="keyboard"] [role="button"],[role="dialog"] button,[role="dialog"] [role="button"],.van-popup button'
  )).find(el => visible(el) && /^(确认|完成|确定)$/.test(clean(el.textContent)));
  if (!candidate) return false;
  fire(candidate);
  return true;
}
"""


VIRTUAL_KEYBOARD_TAP_SCRIPT = r"""
(el) => {
  if (!(el instanceof Element)) return {tapped: false, reason: 'element-not-found'};
  const fireTouch = type => {
    try {
      el.dispatchEvent(new TouchEvent(type, {
        bubbles: true,
        cancelable: true,
        composed: true,
        touches: [],
        targetTouches: [],
        changedTouches: []
      }));
      return true;
    } catch (_) {
      return false;
    }
  };
  const touchStarted = fireTouch('touchstart');
  const touchEnded = fireTouch('touchend');
  if (!touchStarted || !touchEnded) {
    el.click();
    return {tapped: true, method: 'click-fallback'};
  }
  return {tapped: true, method: 'touch'};
}
"""


ROBUST_CLICK_SCRIPT = r"""
(el) => {
  if (!(el instanceof Element)) return {clicked: false, reason: 'element-not-found'};
  const target = el.closest('label,[role="checkbox"],button,a,[role="button"],[onclick]') || el;
  if (!(target instanceof Element)) return {clicked: false, reason: 'target-not-found'};
  try {
    target.scrollIntoView({block: 'center', inline: 'center'});
  } catch (_) {}
  const rect = target.getBoundingClientRect();
  const clientX = rect.left + rect.width / 2;
  const clientY = rect.top + rect.height / 2;
  const eventOptions = {
    bubbles: true,
    cancelable: true,
    composed: true,
    clientX,
    clientY,
    view: window,
    button: 0,
    buttons: 1
  };
  const fire = (type, EventClass) => {
    try {
      target.dispatchEvent(new EventClass(type, eventOptions));
    } catch (_) {}
  };
  fire('pointerover', window.PointerEvent || MouseEvent);
  fire('mouseover', MouseEvent);
  fire('pointerdown', window.PointerEvent || MouseEvent);
  fire('mousedown', MouseEvent);
  fire('pointerup', window.PointerEvent || MouseEvent);
  fire('mouseup', MouseEvent);
  if (typeof target.click === 'function') {
    target.click();
  } else {
    fire('click', MouseEvent);
  }
  return {
    clicked: true,
    tag: target.tagName.toLowerCase(),
    role: target.getAttribute('role') || '',
    text: String(target.innerText || target.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120)
  };
}
"""


SCROLL_TARGET_STATE_SCRIPT = r"""
({x, y}) => {
  const clamp = (value, min, max) => Math.max(min, Math.min(max, Number(value) || 0));
  const pointX = clamp(x, 0, Math.max(0, window.innerWidth - 1));
  const pointY = clamp(y, 0, Math.max(0, window.innerHeight - 1));
  const deepElementFromPoint = (root, px, py) => {
    let current = root.elementFromPoint(px, py);
    let guard = 0;
    while (current && current.shadowRoot && guard < 8) {
      const nested = current.shadowRoot.elementFromPoint(px, py);
      if (!nested || nested === current) break;
      current = nested;
      guard += 1;
    }
    return current;
  };
  const parentOf = el => {
    if (!el) return null;
    if (el.parentElement) return el.parentElement;
    const root = el.getRootNode && el.getRootNode();
    return root instanceof ShadowRoot ? root.host : null;
  };
  const canScroll = el => {
    if (!el || !(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    const overflowY = `${style.overflowY} ${style.overflow}`;
    const overflowX = `${style.overflowX} ${style.overflow}`;
    return (
      (/auto|scroll|overlay/.test(overflowY) && el.scrollHeight > el.clientHeight + 1) ||
      (/auto|scroll|overlay/.test(overflowX) && el.scrollWidth > el.clientWidth + 1)
    );
  };
  const target = deepElementFromPoint(document, pointX, pointY);
  const visible = el => {
    if (!el || !(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' &&
      rect.width > 20 && rect.height > 20 && rect.bottom > 0 && rect.right > 0 &&
      rect.top < window.innerHeight && rect.left < window.innerWidth;
  };
  const zIndex = el => {
    const raw = window.getComputedStyle(el).zIndex;
    const value = Number(raw);
    return Number.isFinite(value) ? value : 0;
  };
  const popups = Array.from(document.querySelectorAll(
    '.van-popup,.van-picker,.van-cascader,[role="dialog"],[aria-modal="true"],.ant-modal,.el-dialog'
  )).filter(visible).sort((left, right) => {
    const zDiff = zIndex(right) - zIndex(left);
    if (zDiff) return zDiff;
    const leftRect = left.getBoundingClientRect();
    const rightRect = right.getBoundingClientRect();
    return (rightRect.width * rightRect.height) - (leftRect.width * leftRect.height);
  });
  const popup = popups[0] || null;
  const inPopup = Boolean(popup && target && popup.contains(target));
  const popupTarget = popup ? (popup.className ? String(popup.className).slice(0, 160) : popup.tagName.toLowerCase()) : '';
  let current = target;
  while (current) {
    if (canScroll(current)) {
      return {
        found: true,
        source: 'element',
        tag: current.tagName.toLowerCase(),
        class_name: String(current.className || '').slice(0, 160),
        scroll_top: current.scrollTop,
        scroll_left: current.scrollLeft,
        popup_active: Boolean(popup),
        in_popup: inPopup,
        popup_target: popupTarget
      };
    }
    current = parentOf(current);
  }
  if (inPopup) {
    return {
      found: false,
      source: 'popup',
      scroll_top: 0,
      scroll_left: 0,
      popup_active: true,
      in_popup: true,
      popup_target: popupTarget
    };
  }
  const scrolling = document.scrollingElement || document.documentElement;
  if (scrolling && (scrolling.scrollHeight > scrolling.clientHeight + 1 || scrolling.scrollWidth > scrolling.clientWidth + 1)) {
    return {
      found: true,
      source: 'document',
      tag: scrolling.tagName.toLowerCase(),
      class_name: String(scrolling.className || '').slice(0, 160),
      scroll_top: scrolling.scrollTop,
      scroll_left: scrolling.scrollLeft,
      popup_active: Boolean(popup),
      in_popup: inPopup,
      popup_target: popupTarget
    };
  }
  return {
    found: false,
    scroll_top: 0,
    scroll_left: 0,
    popup_active: Boolean(popup),
    in_popup: inPopup,
    popup_target: popupTarget
  };
}
"""


SCROLL_EFFECTIVE_POINT_SCRIPT = r"""
({x, y, popupOnly}) => {
  const clamp = (value, min, max) => Math.max(min, Math.min(max, Number(value) || 0));
  let pointX = clamp(x, 0, Math.max(0, window.innerWidth - 1));
  let pointY = clamp(y, 0, Math.max(0, window.innerHeight - 1));
  const mustUsePopup = Boolean(popupOnly);
  const deepElementFromPoint = (root, px, py) => {
    let current = root.elementFromPoint(px, py);
    let guard = 0;
    while (current && current.shadowRoot && guard < 8) {
      const nested = current.shadowRoot.elementFromPoint(px, py);
      if (!nested || nested === current) break;
      current = nested;
      guard += 1;
    }
    return current;
  };
  const visible = el => {
    if (!el || !(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' &&
      rect.width > 20 && rect.height > 20 && rect.bottom > 0 && rect.right > 0 &&
      rect.top < window.innerHeight && rect.left < window.innerWidth;
  };
  const zIndex = el => {
    const raw = window.getComputedStyle(el).zIndex;
    const value = Number(raw);
    return Number.isFinite(value) ? value : 0;
  };
  const current = deepElementFromPoint(document, pointX, pointY);
  const popups = Array.from(document.querySelectorAll(
    '.van-popup,.van-picker,.van-cascader,[role="dialog"],[aria-modal="true"],.ant-modal,.el-dialog'
  )).filter(visible).sort((left, right) => {
    const zDiff = zIndex(right) - zIndex(left);
    if (zDiff) return zDiff;
    const leftRect = left.getBoundingClientRect();
    const rightRect = right.getBoundingClientRect();
    return (rightRect.width * rightRect.height) - (leftRect.width * leftRect.height);
  });
  const popup = popups[0];
  const insidePopup = Boolean(popup && current && popup.contains(current));
  const popupTarget = popup ? (popup.className ? String(popup.className).slice(0, 160) : popup.tagName.toLowerCase()) : '';
  if (mustUsePopup && popup && !insidePopup) {
    const rect = popup.getBoundingClientRect();
    pointX = clamp(pointX, Math.max(1, rect.left + 1), Math.min(window.innerWidth - 2, rect.right - 2));
    pointY = clamp(rect.top + rect.height * 0.62, Math.max(1, rect.top + 1), Math.min(window.innerHeight - 2, rect.bottom - 2));
    return {
      x: pointX,
      y: pointY,
      redirected: true,
      popup_active: true,
      inside_popup: true,
      popup_only: true,
      target: popupTarget
    };
  }
  return {
    x: pointX,
    y: pointY,
    redirected: false,
    popup_active: Boolean(popup),
    inside_popup: insidePopup,
    popup_only: mustUsePopup,
    target: popupTarget
  };
}
"""


SCROLL_FALLBACK_SCRIPT = r"""
({x, y, deltaX, deltaY, popupOnly}) => {
  const clamp = (value, min, max) => Math.max(min, Math.min(max, Number(value) || 0));
  const pointX = clamp(x, 0, Math.max(0, window.innerWidth - 1));
  const pointY = clamp(y, 0, Math.max(0, window.innerHeight - 1));
  const scrollDeltaX = Number(deltaX) || 0;
  const scrollDeltaY = Number(deltaY) || 0;
  const preferSingleGesture = Boolean(popupOnly);
  const deepElementFromPoint = (root, px, py) => {
    let current = root.elementFromPoint(px, py);
    let guard = 0;
    while (current && current.shadowRoot && guard < 8) {
      const nested = current.shadowRoot.elementFromPoint(px, py);
      if (!nested || nested === current) break;
      current = nested;
      guard += 1;
    }
    return current;
  };
  const parentOf = el => {
    if (!el) return null;
    if (el.parentElement) return el.parentElement;
    const root = el.getRootNode && el.getRootNode();
    return root instanceof ShadowRoot ? root.host : null;
  };
  const canScroll = el => {
    if (!el || !(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    const overflowY = `${style.overflowY} ${style.overflow}`;
    const overflowX = `${style.overflowX} ${style.overflow}`;
    return (
      (/auto|scroll|overlay/.test(overflowY) && el.scrollHeight > el.clientHeight + 1) ||
      (/auto|scroll|overlay/.test(overflowX) && el.scrollWidth > el.clientWidth + 1)
    );
  };
  const eventTarget = deepElementFromPoint(document, pointX, pointY) || document.scrollingElement || document.body;
  const visible = el => {
    if (!el || !(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' &&
      rect.width > 20 && rect.height > 20 && rect.bottom > 0 && rect.right > 0 &&
      rect.top < window.innerHeight && rect.left < window.innerWidth;
  };
  const zIndex = el => {
    const raw = window.getComputedStyle(el).zIndex;
    const value = Number(raw);
    return Number.isFinite(value) ? value : 0;
  };
  const popups = Array.from(document.querySelectorAll(
    '.van-popup,.van-picker,.van-cascader,[role="dialog"],[aria-modal="true"],.ant-modal,.el-dialog'
  )).filter(visible).sort((left, right) => {
    const zDiff = zIndex(right) - zIndex(left);
    if (zDiff) return zDiff;
    const leftRect = left.getBoundingClientRect();
    const rightRect = right.getBoundingClientRect();
    return (rightRect.width * rightRect.height) - (leftRect.width * leftRect.height);
  });
  const popup = popups[0] || null;
  const inPopup = Boolean(popup && eventTarget && popup.contains(eventTarget));
  const candidates = [];
  let current = eventTarget;
  while (current) {
    if (canScroll(current)) candidates.push(current);
    if (inPopup && current === popup) break;
    current = parentOf(current);
  }
  const scrolling = document.scrollingElement || document.documentElement;
  if (!inPopup && scrolling && !candidates.includes(scrolling)) candidates.push(scrolling);

  let method = '';
  let scrollChanged = false;
  for (const candidate of candidates) {
    const beforeTop = candidate.scrollTop;
    const beforeLeft = candidate.scrollLeft;
    try {
      candidate.scrollBy({left: scrollDeltaX, top: scrollDeltaY, behavior: 'auto'});
    } catch (_) {
      candidate.scrollLeft += scrollDeltaX;
      candidate.scrollTop += scrollDeltaY;
    }
    if (candidate.scrollTop !== beforeTop || candidate.scrollLeft !== beforeLeft) {
      candidate.dispatchEvent(new Event('scroll', {bubbles: true}));
      method = 'dom-scroll';
      scrollChanged = true;
      break;
    }
  }

  if (!preferSingleGesture) {
    try {
      eventTarget.dispatchEvent(new WheelEvent('wheel', {
        bubbles: true,
        cancelable: true,
        composed: true,
        clientX: pointX,
        clientY: pointY,
        deltaX: scrollDeltaX,
        deltaY: scrollDeltaY
      }));
    } catch (_) {}
  }

  if (!scrollChanged) {
    const travelX = -clamp(scrollDeltaX, -360, 360);
    const travelY = -clamp(scrollDeltaY, -360, 360);
    if (Math.abs(travelX) + Math.abs(travelY) > 0) {
      const startX = pointX;
      const startY = pointY;
      const endX = clamp(pointX + travelX, 1, Math.max(1, window.innerWidth - 2));
      const endY = clamp(pointY + travelY, 1, Math.max(1, window.innerHeight - 2));
      const makeTouch = (target, cx, cy) => {
        const init = {
          identifier: 1,
          target,
          clientX: cx,
          clientY: cy,
          screenX: cx,
          screenY: cy,
          pageX: cx + window.scrollX,
          pageY: cy + window.scrollY,
          radiusX: 4,
          radiusY: 4,
          rotationAngle: 0,
          force: 0.5
        };
        try {
          return typeof Touch === 'function' ? new Touch(init) : init;
        } catch (_) {
          return init;
        }
      };
      const firePointer = (type, cx, cy, buttons) => {
        try {
          eventTarget.dispatchEvent(new PointerEvent(type, {
            bubbles: true,
            cancelable: true,
            composed: true,
            clientX: cx,
            clientY: cy,
            pointerId: 1,
            pointerType: 'touch',
            isPrimary: true,
            button: 0,
            buttons
          }));
        } catch (_) {}
      };
      const fireTouch = (type, cx, cy, active) => {
        try {
          const touch = makeTouch(eventTarget, cx, cy);
          eventTarget.dispatchEvent(new TouchEvent(type, {
            bubbles: true,
            cancelable: true,
            composed: true,
            touches: active ? [touch] : [],
            targetTouches: active ? [touch] : [],
            changedTouches: [touch]
          }));
        } catch (_) {}
      };
      const fireMouse = (type, cx, cy, buttons) => {
        try {
          eventTarget.dispatchEvent(new MouseEvent(type, {
            bubbles: true,
            cancelable: true,
            composed: true,
            clientX: cx,
            clientY: cy,
            button: 0,
            buttons
          }));
        } catch (_) {}
      };

      const fireLegacyGesture = () => {
        firePointer('pointerover', startX, startY, 1);
        firePointer('pointerdown', startX, startY, 1);
        fireMouse('mousedown', startX, startY, 1);
        fireTouch('touchstart', startX, startY, true);
        for (let index = 1; index <= 6; index += 1) {
          const ratio = index / 6;
          const cx = startX + (endX - startX) * ratio;
          const cy = startY + (endY - startY) * ratio;
          firePointer('pointermove', cx, cy, 1);
          fireMouse('mousemove', cx, cy, 1);
          fireTouch('touchmove', cx, cy, true);
        }
        fireTouch('touchend', endX, endY, false);
        firePointer('pointerup', endX, endY, 0);
        fireMouse('mouseup', endX, endY, 0);
        method = method ? `${method}+touch` : 'touch';
      };
      const fireSingleGesture = () => {
        if (typeof TouchEvent === 'function') {
          fireTouch('touchstart', startX, startY, true);
          for (let index = 1; index <= 6; index += 1) {
            const ratio = index / 6;
            fireTouch('touchmove', startX + (endX - startX) * ratio, startY + (endY - startY) * ratio, true);
          }
          fireTouch('touchend', endX, endY, false);
          method = method ? `${method}+touch` : 'touch';
          return;
        }
        if (typeof PointerEvent === 'function') {
          firePointer('pointerover', startX, startY, 1);
          firePointer('pointerdown', startX, startY, 1);
          for (let index = 1; index <= 6; index += 1) {
            const ratio = index / 6;
            firePointer('pointermove', startX + (endX - startX) * ratio, startY + (endY - startY) * ratio, 1);
          }
          firePointer('pointerup', endX, endY, 0);
          method = method ? `${method}+pointer` : 'pointer';
          return;
        }
        fireMouse('mousedown', startX, startY, 1);
        for (let index = 1; index <= 6; index += 1) {
          const ratio = index / 6;
          fireMouse('mousemove', startX + (endX - startX) * ratio, startY + (endY - startY) * ratio, 1);
        }
        fireMouse('mouseup', endX, endY, 0);
        method = method ? `${method}+mouse` : 'mouse';
      };
      if (preferSingleGesture) fireSingleGesture();
      else fireLegacyGesture();
    }
  }

  return {
    fallback: true,
    method: method || 'wheel-event',
    scroll_changed: scrollChanged,
    target_tag: eventTarget && eventTarget.tagName ? eventTarget.tagName.toLowerCase() : '',
    popup_active: Boolean(popup),
    in_popup: inPopup
  };
}
"""


POPUP_SELECT_TEXT_SCRIPT = r"""
async ({text, timeoutMs}) => {
  const targetText = String(text || '').replace(/\s+/g, ' ').trim();
  const timeout = Math.max(800, Number(timeoutMs) || 8000);
  if (!targetText) throw new Error('缺少要选择的弹框文本');
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const clamp = (value, min, max) => Math.max(min, Math.min(max, Number(value) || 0));
  const popupSelector = [
    '.van-popup',
    '.van-picker',
    '.van-cascader',
    '[role="dialog"]',
    '[aria-modal="true"]',
    '.ant-modal',
    '.el-dialog'
  ].join(',');
  const itemSelector = [
    '[role="option"]',
    '[role="button"]',
    'button',
    'li',
    '[class*="option"]',
    '[class*="item"]',
    'div',
    'span'
  ].join(',');
  const visible = el => {
    if (!el || !(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' &&
      rect.width > 1 && rect.height > 1 && rect.bottom > 0 && rect.right > 0 &&
      rect.top < window.innerHeight && rect.left < window.innerWidth;
  };
  const rendered = el => {
    if (!el || !(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' &&
      rect.width > 1 && rect.height > 1;
  };
  const zIndex = el => {
    const raw = window.getComputedStyle(el).zIndex;
    const value = Number(raw);
    return Number.isFinite(value) ? value : 0;
  };
  const popupList = () => Array.from(document.querySelectorAll(popupSelector))
    .filter(visible)
    .sort((left, right) => {
      const zDiff = zIndex(right) - zIndex(left);
      if (zDiff) return zDiff;
      const leftRect = left.getBoundingClientRect();
      const rightRect = right.getBoundingClientRect();
      return (rightRect.width * rightRect.height) - (leftRect.width * leftRect.height);
    });
  const matchesValue = (value, wanted) => {
    const current = clean(value);
    const expected = clean(wanted);
    return current === expected || current.includes(expected);
  };
  const matches = value => matchesValue(value, targetText);
  const nodeText = el => clean(
    el.getAttribute('aria-label') ||
    el.getAttribute('title') ||
    el.getAttribute('data-value') ||
    el.getAttribute('value') ||
    el.innerText ||
    el.textContent
  );
  const hasChildMatch = (el, wanted = targetText) => Array.from(el.children || []).some(child => matchesValue(nodeText(child), wanted));
  const pickerColumns = popup => Array.from(popup.querySelectorAll(
    '.van-picker-column,.van-picker__column,.picker-column,[class*="picker-column"]'
  )).filter(el => {
    if (!rendered(el)) return false;
    return Array.from(el.classList || []).some(className =>
      className === 'van-picker-column' ||
      className === 'van-picker__column' ||
      className === 'picker-column' ||
      /(?:^|-)picker-column$/.test(className)
    );
  });
  const findCandidate = (popup, wanted = targetText, preferredColumnIndex = null) => {
    const columns = pickerColumns(popup);
    const scopes = preferredColumnIndex !== null && columns[preferredColumnIndex]
      ? [columns[preferredColumnIndex]]
      : [popup];
    return scopes.flatMap(scope => Array.from(scope.querySelectorAll(itemSelector)))
    .filter(el => {
      if (!rendered(el)) return false;
      const value = nodeText(el);
      if (!matchesValue(value, wanted)) return false;
      if (hasChildMatch(el, wanted)) return false;
      return true;
    })
    .sort((left, right) => {
      const leftText = nodeText(left);
      const rightText = nodeText(right);
      const exactDiff = Number(rightText === wanted) - Number(leftText === wanted);
      if (exactDiff) return exactDiff;
      return leftText.length - rightText.length;
    })[0] || null;
  };
  const viewportFor = (el, popup) => {
    const popupRect = popup.getBoundingClientRect();
    let current = el.parentElement;
    while (current && current !== popup) {
      const rect = current.getBoundingClientRect();
      const style = window.getComputedStyle(current);
      const className = String(current.className || '');
      const overflowY = `${style.overflowY} ${style.overflow}`;
      const pickerLike = /picker|wheel|roller|scroll|column|option/i.test(className);
      const clipped = /hidden|auto|scroll|overlay/.test(overflowY);
      if (
        rect.width >= 30 &&
        rect.height >= 50 &&
        rect.height <= popupRect.height + 2 &&
        rect.bottom > popupRect.top &&
        rect.top < popupRect.bottom &&
        (pickerLike || clipped)
      ) {
        return current;
      }
      current = current.parentElement;
    }
    return popup;
  };
  const eventPointTarget = (x, y, fallback) => document.elementFromPoint(x, y) || fallback;
  const makeTouch = (target, cx, cy) => {
    const init = {
      identifier: 1,
      target,
      clientX: cx,
      clientY: cy,
      screenX: cx,
      screenY: cy,
      pageX: cx + window.scrollX,
      pageY: cy + window.scrollY,
      radiusX: 4,
      radiusY: 4,
      rotationAngle: 0,
      force: 0.5
    };
    try {
      return typeof Touch === 'function' ? new Touch(init) : init;
    } catch (_) {
      return init;
    }
  };
  const fireTouch = (target, type, cx, cy, active) => {
    try {
      const touch = makeTouch(target, cx, cy);
      target.dispatchEvent(new TouchEvent(type, {
        bubbles: true,
        cancelable: true,
        composed: true,
        touches: active ? [touch] : [],
        targetTouches: active ? [touch] : [],
        changedTouches: [touch]
      }));
      return true;
    } catch (_) {
      return false;
    }
  };
  const firePointer = (target, type, cx, cy, buttons) => {
    try {
      target.dispatchEvent(new PointerEvent(type, {
        bubbles: true,
        cancelable: true,
        composed: true,
        clientX: cx,
        clientY: cy,
        pointerId: 1,
        pointerType: 'touch',
        isPrimary: true,
        button: 0,
        buttons
      }));
      return true;
    } catch (_) {
      return false;
    }
  };
  const fireMouse = (target, type, cx, cy, buttons) => {
    try {
      target.dispatchEvent(new MouseEvent(type, {
        bubbles: true,
        cancelable: true,
        composed: true,
        clientX: cx,
        clientY: cy,
        button: 0,
        buttons
      }));
      return true;
    } catch (_) {
      return false;
    }
  };
  const dragWithin = (viewport, x, startY, travelY) => {
    const rect = viewport.getBoundingClientRect();
    const startX = clamp(x, rect.left + 2, rect.right - 2);
    const y1 = clamp(startY, rect.top + 6, rect.bottom - 6);
    const y2 = clamp(y1 + travelY, rect.top + 6, rect.bottom - 6);
    const target = eventPointTarget(startX, y1, viewport);
    if (fireTouch(target, 'touchstart', startX, y1, true)) {
      for (let index = 1; index <= 6; index += 1) {
        const ratio = index / 6;
        fireTouch(target, 'touchmove', startX, y1 + (y2 - y1) * ratio, true);
      }
      fireTouch(target, 'touchend', startX, y2, false);
      return 'touch';
    }
    if (firePointer(target, 'pointerdown', startX, y1, 1)) {
      for (let index = 1; index <= 6; index += 1) {
        const ratio = index / 6;
        firePointer(target, 'pointermove', startX, y1 + (y2 - y1) * ratio, 1);
      }
      firePointer(target, 'pointerup', startX, y2, 0);
      return 'pointer';
    }
    fireMouse(target, 'mousedown', startX, y1, 1);
    for (let index = 1; index <= 6; index += 1) {
      const ratio = index / 6;
      fireMouse(target, 'mousemove', startX, y1 + (y2 - y1) * ratio, 1);
    }
    fireMouse(target, 'mouseup', startX, y2, 0);
    return 'mouse';
  };
  const clickAt = (el, x, y) => {
    const target = eventPointTarget(x, y, el);
    const clickable = target.closest?.('button,[role="option"],[role="button"],li,[onclick]') || target;
    firePointer(clickable, 'pointerdown', x, y, 1);
    fireMouse(clickable, 'mousedown', x, y, 1);
    firePointer(clickable, 'pointerup', x, y, 0);
    fireMouse(clickable, 'mouseup', x, y, 0);
    try {
      if (typeof clickable.click === 'function') clickable.click();
      else fireMouse(clickable, 'click', x, y, 0);
    } catch (_) {
      fireMouse(clickable, 'click', x, y, 0);
    }
    return clickable;
  };
  const selectVantItem = async (candidate, wanted) => {
    const item = candidate?.closest?.('.van-picker-column__item');
    const column = item?.closest?.('.van-picker-column');
    const wrapper = item?.closest?.('.van-picker-column__wrapper');
    if (!item || !column || !wrapper) return null;
    const viewportRect = column.getBoundingClientRect();
    const itemRect = item.getBoundingClientRect();
    const desiredY = viewportRect.top + viewportRect.height / 2;
    const currentTransform = window.getComputedStyle(wrapper).transform;
    let currentY = 0;
    if (currentTransform && currentTransform !== 'none') {
      try {
        currentY = new DOMMatrixReadOnly(currentTransform).m42;
      } catch (_) {
        const matrix = currentTransform.match(/^matrix(?:3d)?\((.+)\)$/);
        const values = matrix ? matrix[1].split(',').map(Number) : [];
        currentY = values.length === 6 ? values[5] : (values.length === 16 ? values[13] : 0);
      }
    }
    const travelY = desiredY - (itemRect.top + itemRect.height / 2);
    if (Math.abs(travelY) > 1) {
      wrapper.style.transitionDuration = '0ms';
      wrapper.style.transitionProperty = 'none';
      wrapper.style.transform = `translate3d(0px, ${currentY + travelY}px, 0px)`;
      void wrapper.offsetHeight;
    }
    try {
      item.click();
    } catch (_) {
      return null;
    }
    await sleep(180);
    const selected = column.querySelector(
      '.van-picker-column__item--selected,[aria-selected="true"],.is-selected,.selected'
    );
    if (!selected || !matchesValue(nodeText(selected), wanted)) return null;
    const selectedRect = selected.getBoundingClientRect();
    return {
      matched: true,
      text: nodeText(selected),
      target_text: clean(wanted),
      x: Math.round(selectedRect.left + selectedRect.width / 2),
      y: Math.round(selectedRect.top + selectedRect.height / 2),
      method: 'vant-item-click'
    };
  };
  const componentCandidates = el => {
    const candidates = [];
    const push = value => {
      if (value && typeof value === 'object' && !candidates.includes(value)) candidates.push(value);
    };
    push(el.__vue__);
    const instance = el.__vueParentComponent;
    push(instance?.exposed);
    push(instance?.proxy);
    push(instance?.ctx);
    push(instance);
    return candidates;
  };
  const callComponent = (vm, name, args) => {
    const fn = vm && vm[name];
    if (typeof fn !== 'function') return false;
    try {
      fn.apply(vm, args);
      return true;
    } catch (_) {
      return false;
    }
  };
  const collectPickerComponents = popup => {
    const nodes = [popup, ...Array.from(popup.querySelectorAll('.van-area,.van-picker,.van-picker-column,[class*="picker"]'))];
    const components = [];
    for (const node of nodes) {
      for (const vm of componentCandidates(node)) {
        if (!components.includes(vm)) components.push(vm);
      }
    }
    return components;
  };
  const columnItems = column => Array.from(column.querySelectorAll(itemSelector))
    .filter(el => rendered(el) && !hasChildMatch(el, nodeText(el)));
  const tryVantColumnApi = async (popup, wanted, preferredColumnIndex = null) => {
    const columns = pickerColumns(popup);
    const columnIndexes = preferredColumnIndex !== null
      ? [preferredColumnIndex]
      : columns.map((_, index) => index);
    const components = collectPickerComponents(popup);
    for (const columnIndex of columnIndexes) {
      const column = columns[columnIndex] || null;
      const items = column ? columnItems(column) : [];
      const itemIndex = items.findIndex(item => matchesValue(nodeText(item), wanted));
      const optionText = itemIndex >= 0 ? nodeText(items[itemIndex]) : clean(wanted);
      const attempts = [];
      for (const vm of components) {
        attempts.push(() => callComponent(vm, 'setColumnValue', [columnIndex, optionText]));
        if (itemIndex >= 0) attempts.push(() => callComponent(vm, 'setColumnIndex', [columnIndex, itemIndex]));
      }
      for (const vm of column ? componentCandidates(column) : []) {
        attempts.push(() => callComponent(vm, 'setValue', [optionText]));
        if (itemIndex >= 0) attempts.push(() => callComponent(vm, 'setIndex', [itemIndex]));
        if (itemIndex >= 0) attempts.push(() => callComponent(vm, 'setCurrentIndex', [itemIndex]));
      }
      for (const attempt of attempts) {
        if (!attempt()) continue;
        await sleep(120);
        const verified = findCandidate(popup, wanted, columnIndex);
        if (!verified) continue;
        const rect = (verified || column || popup).getBoundingClientRect();
        return {
          matched: true,
          text: verified ? nodeText(verified) : optionText,
          target_text: clean(wanted),
          x: Math.round(rect.left + rect.width / 2),
          y: Math.round(rect.top + rect.height / 2),
          method: 'vant-api',
          column_index: columnIndex
        };
      }
    }
    return null;
  };
  const optionText = value => {
    if (value === null || value === undefined) return '';
    if (typeof value === 'string' || typeof value === 'number') return clean(value);
    if (typeof value !== 'object') return '';
    return clean(value.text || value.name || value.label || value.title || value.value || value.code || '');
  };
  const optionCode = (key, value) => {
    if (value && typeof value === 'object') {
      return clean(value.code || value.value || value.id || value.key || key || '');
    }
    return clean(key || '');
  };
  const normalizeOptionList = value => {
    if (!value) return [];
    if (Array.isArray(value)) {
      return value.map((item, index) => ({
        code: optionCode(index, item),
        text: optionText(item),
        raw: item
      })).filter(item => item.text);
    }
    if (typeof value === 'object') {
      return Object.entries(value).map(([key, item]) => ({
        code: optionCode(key, item),
        text: optionText(item),
        raw: item
      })).filter(item => item.text);
    }
    return [];
  };
  const childrenOf = item => {
    if (!item || typeof item !== 'object') return [];
    for (const key of ['children', 'options', 'values', 'items', 'list']) {
      if (Array.isArray(item[key])) return item[key];
    }
    return [];
  };
  const findTreePath = (nodes, wanted, path = [], depth = 0) => {
    if (!Array.isArray(nodes) || depth > 8) return null;
    for (const node of nodes) {
      const textValue = optionText(node);
      const nextPath = textValue ? [...path, textValue] : path;
      if (matchesValue(textValue, wanted)) return nextPath;
      const found = findTreePath(childrenOf(node), wanted, nextPath, depth + 1);
      if (found) return found;
    }
    return null;
  };
  const pathFromAreaLists = (source, wanted) => {
    if (!source || typeof source !== 'object') return null;
    const provinceList = source.province_list || source.provinceList || source.provinces || source.province;
    const cityList = source.city_list || source.cityList || source.cities || source.city;
    const countyList = source.county_list || source.countyList || source.counties || source.county || source.district_list || source.districtList;
    const provinces = normalizeOptionList(provinceList);
    const cities = normalizeOptionList(cityList);
    const counties = normalizeOptionList(countyList);
    if (!provinces.length && !cities.length && !counties.length) return null;
    const byCode = items => new Map(items.map(item => [item.code, item.text]));
    const provinceByCode = byCode(provinces);
    const cityByCode = byCode(cities);
    const provinceMatch = provinces.find(item => matchesValue(item.text, wanted));
    if (provinceMatch) return [provinceMatch.text];
    const cityMatch = cities.find(item => matchesValue(item.text, wanted));
    if (cityMatch) {
      const code = cityMatch.code;
      const parentCode = clean(cityMatch.raw?.parentCode || cityMatch.raw?.parent_code || cityMatch.raw?.pid || '');
      const provinceText = provinceByCode.get(parentCode) || provinceByCode.get(`${code.slice(0, 2)}0000`);
      return [provinceText, cityMatch.text].filter(Boolean);
    }
    const countyMatch = counties.find(item => matchesValue(item.text, wanted));
    if (countyMatch) {
      const code = countyMatch.code;
      const parentCode = clean(countyMatch.raw?.parentCode || countyMatch.raw?.parent_code || countyMatch.raw?.pid || '');
      const cityCode = parentCode || `${code.slice(0, 4)}00`;
      const provinceCode = `${(parentCode || code).slice(0, 2)}0000`;
      return [provinceByCode.get(provinceCode), cityByCode.get(cityCode), countyMatch.text].filter(Boolean);
    }
    return null;
  };
  const discoverComponentPath = (popup, wanted) => {
    const roots = collectPickerComponents(popup);
    const queue = [...roots];
    const seen = new Set();
    let inspected = 0;
    while (queue.length && inspected < 1200) {
      const current = queue.shift();
      if (!current || typeof current !== 'object' || seen.has(current)) continue;
      seen.add(current);
      inspected += 1;
      const areaPath = pathFromAreaLists(current, wanted);
      if (areaPath && areaPath.length) return areaPath;
      for (const key of ['columns', 'currentColumns', 'options', 'areaList', 'state', 'props']) {
        const value = current[key];
        const treePath = findTreePath(Array.isArray(value) ? value : [], wanted);
        if (treePath && treePath.length) return treePath;
        if (value && typeof value === 'object' && !seen.has(value)) queue.push(value);
      }
      if (Array.isArray(current.children)) {
        for (const child of current.children) if (child && typeof child === 'object') queue.push(child);
      }
    }
    return null;
  };
  const selectDiscoveredPath = async (popup, path) => {
    let selected = null;
    for (let index = 0; index < path.length; index += 1) {
      const part = path[index];
      selected = await tryVantColumnApi(popup, part, index);
      if (!selected) {
        const candidate = findCandidate(popup, part, index);
        if (!candidate) return null;
        const viewport = viewportFor(candidate, popup);
        const viewportRect = viewport.getBoundingClientRect();
        const rect = candidate.getBoundingClientRect();
        const centerX = clamp(rect.left + rect.width / 2, viewportRect.left + 4, viewportRect.right - 4);
        const clickY = clamp(rect.top + rect.height / 2, viewportRect.top + 4, viewportRect.bottom - 4);
        clickAt(candidate, centerX, clickY);
        selected = {matched: true, text: nodeText(candidate), target_text: part, x: Math.round(centerX), y: Math.round(clickY), method: 'path-click', column_index: index};
        await sleep(120);
      }
    }
    return selected ? {...selected, method: `${selected.method}+discovered-path`, selected_path: path} : null;
  };
  const selectColumnOption = async (popup, wanted, columnIndex) => {
    const apiResult = await tryVantColumnApi(popup, wanted, columnIndex);
    if (apiResult) return apiResult;
    const candidate = findCandidate(popup, wanted, columnIndex);
    if (!candidate) return null;
    const vantItemResult = await selectVantItem(candidate, wanted);
    if (vantItemResult) return {...vantItemResult, column_index: columnIndex};
    const viewport = viewportFor(candidate, popup);
    const viewportRect = viewport.getBoundingClientRect();
    const rect = candidate.getBoundingClientRect();
    const centerX = clamp(rect.left + rect.width / 2, viewportRect.left + 4, viewportRect.right - 4);
    const clickY = clamp(rect.top + rect.height / 2, viewportRect.top + 4, viewportRect.bottom - 4);
    clickAt(candidate, centerX, clickY);
    await sleep(140);
    const verified = findCandidate(popup, wanted, columnIndex);
    if (!verified) return null;
    return {
      matched: true,
      text: nodeText(verified),
      target_text: clean(wanted),
      x: Math.round(centerX),
      y: Math.round(clickY),
      method: 'column-click',
      column_index: columnIndex
    };
  };
  const probeDependentColumns = async (popup, wanted, deadline) => {
    const initialColumns = pickerColumns(popup);
    if (initialColumns.length < 2) return null;
    let attempts = 0;
    const maxAttempts = 160;
    const selectVisibleTarget = async (startIndex, path) => {
      const columns = pickerColumns(popup);
      for (let index = startIndex; index < columns.length; index += 1) {
        if (!findCandidate(popup, wanted, index)) continue;
        const selected = await selectColumnOption(popup, wanted, index);
        if (selected) {
          return {
            ...selected,
            method: `${selected.method}+dependent-probe`,
            selected_path: [...path, clean(wanted)]
          };
        }
      }
      return null;
    };
    const probeLevel = async (columnIndex, path) => {
      if (performance.now() >= deadline || attempts >= maxAttempts) return null;
      const columns = pickerColumns(popup);
      if (columnIndex >= columns.length - 1) return null;
      const optionTexts = Array.from(new Set(
        columnItems(columns[columnIndex]).map(nodeText).filter(Boolean)
      ));
      for (const option of optionTexts) {
        if (performance.now() >= deadline || attempts >= maxAttempts) return null;
        attempts += 1;
        const parentResult = await selectColumnOption(popup, option, columnIndex);
        if (!parentResult) continue;
        const nextPath = [...path, option];
        const directResult = await selectVisibleTarget(columnIndex + 1, nextPath);
        if (directResult) return directResult;
        const nestedResult = await probeLevel(columnIndex + 1, nextPath);
        if (nestedResult) return nestedResult;
      }
      return null;
    };
    return probeLevel(0, []);
  };
  const deadline = performance.now() + timeout;
  let lastMethod = '';
  let dependentProbeAttempted = false;
  let sawVisiblePopup = false;
  while (performance.now() < deadline) {
    const popup = popupList()[0];
    if (!popup) {
      await sleep(100);
      continue;
    }
    sawVisiblePopup = true;
    const vantResult = await tryVantColumnApi(popup, targetText);
    if (vantResult) return vantResult;
    const candidate = findCandidate(popup);
    if (!candidate) {
      const path = discoverComponentPath(popup, targetText);
      if (path && path.length > 1) {
        const pathResult = await selectDiscoveredPath(popup, path);
        if (pathResult) {
          return {
            ...pathResult,
            target_text: targetText
          };
        }
      }
      if (!dependentProbeAttempted) {
        dependentProbeAttempted = true;
        const probedResult = await probeDependentColumns(popup, targetText, deadline);
        if (probedResult) {
          return {
            ...probedResult,
            target_text: targetText
          };
        }
      }
      const scrollable = Array.from(popup.querySelectorAll('*')).find(el => {
        const style = window.getComputedStyle(el);
        return /auto|scroll|overlay/.test(`${style.overflowY} ${style.overflow}`) && el.scrollHeight > el.clientHeight + 1;
      });
      if (scrollable) scrollable.scrollTop += 180;
      await sleep(120);
      continue;
    }
    const viewport = viewportFor(candidate, popup);
    const vantItemResult = await selectVantItem(candidate, targetText);
    if (vantItemResult) return vantItemResult;
    const viewportRect = viewport.getBoundingClientRect();
    const rect = candidate.getBoundingClientRect();
    const centerX = clamp(rect.left + rect.width / 2, viewportRect.left + 4, viewportRect.right - 4);
    const centerY = rect.top + rect.height / 2;
    const desiredY = viewportRect.top + viewportRect.height / 2;
    const withinViewport = rect.bottom > viewportRect.top + 2 && rect.top < viewportRect.bottom - 2;
    const closeEnough = Math.abs(centerY - desiredY) <= Math.max(20, Math.min(48, rect.height * 0.75));
    if (withinViewport && closeEnough) {
      const clickY = clamp(centerY, viewportRect.top + 4, viewportRect.bottom - 4);
      const clicked = clickAt(candidate, centerX, clickY);
      return {
        matched: true,
        text: nodeText(candidate),
        target_text: targetText,
        x: Math.round(centerX),
        y: Math.round(clickY),
        method: lastMethod || 'click',
        clicked_tag: clicked && clicked.tagName ? clicked.tagName.toLowerCase() : ''
      };
    }
    const travelY = clamp(desiredY - centerY, -120, 120);
    if (Math.abs(travelY) < 8) break;
    lastMethod = dragWithin(viewport, centerX, desiredY, travelY);
    await sleep(140);
  }
  if (!sawVisiblePopup) {
    throw new Error(`等待 ${timeout}ms 后仍未检测到可见弹框`);
  }
  throw new Error(`弹框内未定位到文本：${targetText}`);
}
"""


CLICK_VISIBLE_TEXT_OPTION_SCRIPT = r"""
({text}) => {
  const targetText = String(text || '').replace(/\s+/g, ' ').trim();
  if (!targetText) return {matched: false, reason: 'empty-text'};
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    if (!el || !(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' &&
      rect.width > 1 && rect.height > 1 && rect.bottom > 0 && rect.right > 0 &&
      rect.top < window.innerHeight && rect.left < window.innerWidth;
  };
  const selector = [
    '[role="option"]',
    '[role="menuitem"]',
    '[role="button"]',
    'button',
    'li',
    'option',
    '[class*="option"]',
    '[class*="item"]',
    'div',
    'span'
  ].join(',');
  const candidates = Array.from(document.querySelectorAll(selector))
    .filter(visible)
    .filter(el => clean(el.innerText || el.textContent) === targetText);
  const target = candidates[0] || null;
  if (!target) return {matched: false, reason: 'not-found', text: targetText};
  try { target.scrollIntoView({block: 'center', inline: 'center'}); } catch (_) {}
  const rect = target.getBoundingClientRect();
  const clickTarget = target.closest('button,[role="option"],[role="menuitem"],[role="button"],li,[onclick]') || target;
  clickTarget.dispatchEvent(new MouseEvent('mouseover', {bubbles: true, composed: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2}));
  clickTarget.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, composed: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2}));
  clickTarget.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, composed: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2}));
  if (typeof clickTarget.click === 'function') clickTarget.click();
  else clickTarget.dispatchEvent(new MouseEvent('click', {bubbles: true, composed: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2}));
  return {
    matched: true,
    text: targetText,
    tag: target.tagName.toLowerCase(),
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2
  };
}
"""


def _scroll_state_changed(before: Any, after: Any) -> bool:
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    if not before.get("found") or not after.get("found"):
        return False
    return (
        before.get("scroll_top") != after.get("scroll_top")
        or before.get("scroll_left") != after.get("scroll_left")
    )


def mobile_context_options(viewport: Dict[str, Any]) -> Dict[str, Any]:
    width = int((viewport or {}).get("width") or 1440)
    height = int((viewport or {}).get("height") or 900)
    options: Dict[str, Any] = {"viewport": {"width": width, "height": height}}
    if width <= 480:
        options.update({
            "is_mobile": True,
            "has_touch": True,
            "device_scale_factor": 1,
        })
    return options


def perform_precise_scroll(
    page,
    x: Optional[float] = None,
    y: Optional[float] = None,
    delta_x: int = 0,
    delta_y: int = 500,
    wait_ms: int = 250,
    popup_only: bool = False,
) -> Dict[str, Any]:
    """Scroll at a viewport point, with fallbacks for touch-driven mobile pickers."""
    delta_x = int(delta_x or 0)
    delta_y = int(delta_y if delta_y is not None else 0)
    if not delta_x and not delta_y:
        delta_y = 500
    delta_x, delta_y = normalize_scroll_delta(delta_x, delta_y, popup_only=popup_only)
    has_point = x is not None and y is not None
    point_x = float(x or 0)
    point_y = float(y or 0)
    if popup_only and not has_point:
        viewport = getattr(page, "viewport_size", None)
        if isinstance(viewport, dict):
            point_x = float(viewport.get("width") or 0) / 2
            point_y = float(viewport.get("height") or 0) / 2
        has_point = True
    before = None
    result: Dict[str, Any] = {"wheel": True, "fallback": None}
    popup_context = False
    popup_missing = False

    if has_point and hasattr(page, "evaluate"):
        try:
            effective_point = page.evaluate(
                SCROLL_EFFECTIVE_POINT_SCRIPT,
                {"x": point_x, "y": point_y, "popupOnly": popup_only},
            )
            if isinstance(effective_point, dict):
                point_x = float(effective_point.get("x", point_x))
                point_y = float(effective_point.get("y", point_y))
                if popup_only and not effective_point.get("popup_active"):
                    popup_missing = True
                popup_context = bool(
                    popup_only
                    and effective_point.get("popup_active")
                    and effective_point.get("inside_popup")
                )
                if effective_point.get("redirected") or effective_point.get("popup_active"):
                    result["effective_point"] = effective_point
        except Exception as exc:
            result["effective_point_error"] = str(exc)[:300]
            if popup_only:
                raise ValueError(f"弹框滚动命中失败: {exc}") from exc
        if popup_missing:
            raise ValueError("当前页面未检测到可滚动弹框")
        try:
            before = page.evaluate(SCROLL_TARGET_STATE_SCRIPT, {"x": point_x, "y": point_y})
        except Exception:
            before = None
    elif popup_only:
        raise ValueError("弹框滚动需要可探测的浏览器页面")

    if popup_context and hasattr(page, "evaluate"):
        try:
            result["wheel"] = False
            result["fallback"] = page.evaluate(
                SCROLL_FALLBACK_SCRIPT,
                {"x": point_x, "y": point_y, "deltaX": delta_x, "deltaY": delta_y, "popupOnly": popup_only},
            )
        except Exception as exc:
            result["fallback_error"] = str(exc)[:300]
        if wait_ms and hasattr(page, "wait_for_timeout"):
            page.wait_for_timeout(wait_ms)
        return result

    if has_point:
        page.mouse.move(point_x, point_y)
    page.mouse.wheel(delta_x, delta_y)

    if has_point and hasattr(page, "evaluate"):
        try:
            if hasattr(page, "wait_for_timeout"):
                page.wait_for_timeout(80)
            after = page.evaluate(SCROLL_TARGET_STATE_SCRIPT, {"x": point_x, "y": point_y})
            if not _scroll_state_changed(before, after):
                result["fallback"] = page.evaluate(
                    SCROLL_FALLBACK_SCRIPT,
                    {"x": point_x, "y": point_y, "deltaX": delta_x, "deltaY": delta_y, "popupOnly": popup_only},
                )
        except Exception as exc:
            result["fallback_error"] = str(exc)[:300]

    if wait_ms and hasattr(page, "wait_for_timeout"):
        page.wait_for_timeout(wait_ms)
    return result


def select_popup_text(page, text: Any, timeout_ms: int = 8000) -> Dict[str, Any]:
    target_text = str(resolve_variables(text) or "").strip()
    if not target_text:
        raise ValueError("弹框选择文本不能为空")
    if not hasattr(page, "evaluate"):
        raise ValueError("弹框文本选择需要可探测的浏览器页面")
    parts = [
        item.strip()
        for item in re.split(r"\s*(?:/|／|>|›|→|,|，|\n)\s*", target_text)
        if item.strip()
    ] or [target_text]
    results = []
    per_part_timeout = max(1200, int(timeout_ms or 8000))
    for part in parts:
        result = page.evaluate(
            POPUP_SELECT_TEXT_SCRIPT,
            {"text": part, "timeoutMs": per_part_timeout},
        )
        if not isinstance(result, dict) or not result.get("matched"):
            raise AssertionError(f"弹框内未定位到文本: {part}")
        results.append(result)
        if hasattr(page, "wait_for_timeout"):
            page.wait_for_timeout(180)
    final_result = dict(results[-1])
    final_result["target_text"] = target_text
    final_result["selected_parts"] = results
    return final_result


def click_visible_text_option(page, text: Any) -> Dict[str, Any]:
    target_text = str(resolve_variables(text) or "").strip()
    if not target_text:
        raise ValueError("选项文本不能为空")
    if not hasattr(page, "evaluate"):
        raise ValueError("文本选项选择需要可探测的浏览器页面")
    result = page.evaluate(CLICK_VISIBLE_TEXT_OPTION_SCRIPT, {"text": target_text})
    if not isinstance(result, dict) or not result.get("matched"):
        raise AssertionError(f"页面内未定位到可见选项文本: {target_text}")
    if hasattr(page, "wait_for_timeout"):
        page.wait_for_timeout(180)
    return result


def load_web_flow(path: str) -> Dict[str, Any]:
    """加载并验证 Web Flow YAML。"""
    runtime_path = str(os.getenv("RUNTIME_CASE_FILE") or "").strip()
    effective_path = runtime_path if runtime_path and os.path.isfile(runtime_path) else path
    with open(effective_path, "r", encoding="utf-8") as handle:
        flow = yaml.safe_load(handle) or {}
    if not isinstance(flow, dict) or int(flow.get("flow_version") or 0) != 2:
        raise ValueError("不是有效的 flow_version: 2 Web UI 用例")
    if not isinstance(flow.get("steps"), list):
        raise ValueError("Web UI 用例缺少 steps 数组")
    # 编排执行采用页面感知回放：当前页面匹配步骤时真实执行，页面不匹配时
    # 跳过该步骤并继续探测后续步骤。历史 YAML 中的旧模式不能覆盖这个规则。
    flow["step_execution_mode"] = "page_aware"
    return flow


def _dict_get(source: Dict[str, Any], key: str, default: Any = _MISSING) -> Any:
    if key in source:
        return source[key]
    lower = key.lower()
    for item_key, item_value in source.items():
        if str(item_key).lower() == lower:
            return item_value
    return default


def _get_path(source: Any, parts: list[str], default: Any = _MISSING) -> Any:
    current = source
    for part in parts:
        if isinstance(current, dict):
            current = _dict_get(current, part, default)
            if current is default:
                return default
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (TypeError, ValueError, IndexError):
                return default
        else:
            return default
    return current


def _runtime_context() -> Dict[str, Dict[str, Any]]:
    context = {
        "global": {},
        "local": {},
        "outputs": {},
        "data": {},
        "dataAssets": {},
        "smartData": {},
    }
    raw = os.getenv("RUNTIME_CONTEXT_JSON", "")
    if raw:
        try:
            loaded = json.loads(raw)
        except (TypeError, ValueError):
            loaded = {}
        if isinstance(loaded, dict):
            for scope in context:
                if isinstance(loaded.get(scope), dict):
                    context[scope].update(loaded[scope])
            if isinstance(loaded.get("variables"), dict):
                context["local"].update(loaded["variables"])
    raw_variables = os.getenv("RUNTIME_VARIABLES_JSON", "")
    if raw_variables:
        try:
            variables = json.loads(raw_variables)
        except (TypeError, ValueError):
            variables = {}
        if isinstance(variables, dict):
            context["local"].update(variables)

    # Parameterized executions expose the selected row separately as well as
    # in RUNTIME_CONTEXT_JSON.  Keep this fallback so a pytest fixture or an
    # xdist worker that only preserves the row-level environment can still
    # resolve data-asset references before a value reaches Playwright.fill().
    raw_row = os.getenv("RUNTIME_DATA_ROW_JSON", "")
    if raw_row:
        try:
            row = json.loads(raw_row)
        except (TypeError, ValueError):
            row = {}
        if isinstance(row, dict):
            row_assets = row.get("dataAssets")
            row_data = row.get("data")
            if not isinstance(row_assets, dict) and not isinstance(row_data, dict):
                row_assets = row
            if isinstance(row_assets, dict):
                _merge_runtime_scope(context["dataAssets"], row_assets)
            if isinstance(row_data, dict):
                _merge_runtime_scope(context["data"], row_data)
            elif isinstance(row_assets, dict):
                _merge_runtime_scope(context["data"], row_assets)
    return context


def _merge_runtime_scope(target: Dict[str, Any], source: Dict[str, Any]) -> None:
    """Merge a row scope without discarding aliases already loaded upstream."""
    for key, value in source.items():
        existing = next((item for item in target if str(item).lower() == str(key).lower()), None)
        if existing is None:
            target[key] = value
        elif isinstance(target.get(existing), dict) and isinstance(value, dict):
            _merge_runtime_scope(target[existing], value)
        else:
            target[existing] = value


def _resolve_runtime_reference(name: str) -> Any:
    name = str(name or "").strip()
    if not name:
        return _MISSING

    context = _runtime_context()
    parts = name.split(".")
    if parts[0] in context and len(parts) > 1:
        value = _get_path(context.get(parts[0], {}), parts[1:])
        if value is not _MISSING:
            return value

    for scope in ("local", "data", "dataAssets", "smartData", "global", "outputs"):
        scope_values = context.get(scope) or {}
        direct = _dict_get(scope_values, name)
        if direct is not _MISSING:
            return direct
        nested = _get_path(scope_values, parts)
        if nested is not _MISSING:
            return nested

    env_value = os.getenv(name)
    return env_value if env_value is not None else _MISSING


def _stringify_runtime_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def _resolve_variable_string(source: str) -> Any:
    full_match = VARIABLE_PATTERN.fullmatch(source)
    if full_match:
        value = _resolve_runtime_reference(full_match.group(1))
        return source if value is _MISSING else value

    def replace(match):
        value = _resolve_runtime_reference(match.group(1))
        return match.group(0) if value is _MISSING else _stringify_runtime_value(value)

    return VARIABLE_PATTERN.sub(replace, source)


def resolve_variables(value: Any) -> Any:
    """递归解析动态表达式、运行上下文变量及 `${ENV_NAME}` 环境变量。"""
    if isinstance(value, str):
        current: Any = value
        for _ in range(10):
            if not isinstance(current, str):
                return current
            resolved = _resolve_variable_string(current)
            if isinstance(resolved, str):
                resolved = resolve_dynamic_value(resolved)
            if resolved == current:
                return resolved
            current = resolved
        return current
    if isinstance(value, list):
        return [resolve_variables(item) for item in value]
    if isinstance(value, dict):
        return {key: resolve_variables(item) for key, item in value.items()}
    return value


# 容器共享上传目录（与 docker-compose.testhub.yaml auto-test.volumes 同步）
_CONTAINER_UPLOAD_DIRS: Tuple[str, ...] = (
    "/app/uploads",
    "/app/imports",
    "/app/cases",
    "/data/testhub/media",
)


def _normalize_upload_files(raw: Any) -> List[str]:
    """把 files/value 字段规范化为文件路径列表。

    兼容：字符串（逗号分隔/单路径）、list（任意嵌套字符串）、None/空。
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        result: List[str] = []
        for item in raw:
            if item is None:
                continue
            if isinstance(item, str):
                text = item.strip()
                if not text:
                    continue
                # 兼容单字符串里用逗号写多个路径的老格式
                if "," in text:
                    for part in text.split(","):
                        part = part.strip()
                        if part:
                            result.append(part)
                else:
                    result.append(text)
            else:
                result.extend(_normalize_upload_files(item))
        return result
    text = str(raw).strip()
    if not text:
        return []
    if "," in text:
        return [p.strip() for p in text.split(",") if p.strip()]
    return [text]


def _resolve_upload_file_paths(raw_files: Any) -> Tuple[List[str], Dict[str, Any]]:
    """容器感知的上传文件路径解析。

    典型问题：用户在宿主机（macOS/Windows）录制的 YAML 里写了
    ``/Users/tanzsongsen/Downloads/身份证正面.png`` 这种宿主绝对路径，
    但 pytest/worker 跑在 Linux 容器（``runnergo-auto-test-1``）内，
    该路径不存在。本函数按优先级回退查找：

    1. 原始路径直接存在（本地非容器执行）。
    2. 取 basename 在 ``/app/uploads`` 等共享目录查找。
    3. 把常见宿主前缀（``/Users/*/Downloads``、``/home/*/Downloads``、
       ``C:\\Users\\*\\Downloads``、``/Downloads``、``~/Downloads``）
       重映射到共享上传目录。

    返回：``(resolved_paths, diagnostics)``。diagnostics 中包含每个原始路径的
    命中/未命中详情以及最终容器映射目录列表，便于前端提示用户。
    """
    original_paths = _normalize_upload_files(raw_files)
    diagnostics: Dict[str, Any] = {
        "originals": [],
        "container_dirs": list(_CONTAINER_UPLOAD_DIRS),
        "missing": [],
    }
    resolved: List[str] = []

    upload_dirs = list(_CONTAINER_UPLOAD_DIRS)
    # 运行时也接受环境变量覆盖（方便本地调试切换）
    extra = os.getenv("RUNNERGO_UPLOAD_DIRS", "").strip()
    if extra:
        upload_dirs = [d for d in extra.split("|") if d.strip()] + upload_dirs

    import glob as _glob

    for idx, raw_path in enumerate(original_paths):
        candidates: List[str] = []
        candidates.append(raw_path)
        basename = os.path.basename(raw_path) or f"upload_file_{idx}"

        # 去 basename 在共享目录中匹配
        for d in upload_dirs:
            candidates.append(os.path.join(d, basename))

        # 常见宿主 Downloads 前缀的重映射
        stripped = raw_path
        for prefix_pattern in (
            re.compile(r"^/Users/[^/]+/Downloads/?", re.IGNORECASE),
            re.compile(r"^/home/[^/]+/Downloads/?", re.IGNORECASE),
            re.compile(r"^/[A-Za-z]/Users/[^/]+/Downloads/?", re.IGNORECASE),
            re.compile(r"^[A-Z]:\\Users\\[^\\]+\\Downloads\\?", re.IGNORECASE),
            re.compile(r"^~/Downloads/?", re.IGNORECASE),
            re.compile(r"^/Downloads/?", re.IGNORECASE),
            re.compile(r"^/Users/[^/]+/?", re.IGNORECASE),
            re.compile(r"^/home/[^/]+/?", re.IGNORECASE),
            re.compile(r"^[A-Z]:\\Users\\[^\\]+\\?", re.IGNORECASE),
        ):
            replaced = prefix_pattern.sub("", stripped, count=1)
            if replaced != stripped:
                stripped = replaced
                for d in upload_dirs:
                    candidates.append(os.path.join(d, stripped.lstrip("/\\")))
                break

        # 展开通配符（支持少量 glob，仅限容器共享路径避免 SSRF 式遍历）
        matched: List[str] = []
        for cand in candidates:
            if not cand:
                continue
            if any(sep in cand for sep in ("*", "?")):
                try:
                    globs = _glob.glob(cand)
                except Exception:
                    globs = []
                for gp in globs:
                    if os.path.isfile(gp) and gp not in matched:
                        matched.append(gp)
            elif os.path.isfile(cand):
                if cand not in matched:
                    matched.append(cand)
            if matched:
                break

        info: Dict[str, Any] = {
            "index": idx,
            "original": raw_path,
            "resolved": matched[0] if matched else "",
            "mapped": bool(matched and matched[0] != raw_path),
            "candidates_tried": candidates,
        }
        diagnostics["originals"].append(info)
        if matched:
            resolved.append(matched[0])
        else:
            diagnostics["missing"].append(
                {
                    "original": raw_path,
                    "basename": basename,
                    "hint": (
                        "宿主机路径在容器内不存在。请把文件复制到仓库目录 "
                        "auto-test/uploads/ 下（对应容器 /app/uploads/），"
                        "并在步骤文件路径中填写 /app/uploads/" + basename
                    ),
                }
            )

    diagnostics["resolved_count"] = len(resolved)
    diagnostics["missing_count"] = len(diagnostics["missing"])
    return resolved, diagnostics


def _recorded_element_identity(element: Dict[str, Any]) -> Dict[str, str]:
    """Return stable identity fields used to compare adjacent recorded steps."""
    fingerprint = element.get("fingerprint") or {}
    attrs = fingerprint.get("attrs") or {}
    locators = element.get("locators") or []

    identity = {
        "stored_id": _clean(element.get("id")),
        "tag": _clean(element.get("tag") or fingerprint.get("tag")).lower(),
        "accessible_name": _clean(
            element.get("accessible_name") or fingerprint.get("accessible_name")
        ),
        "testid": _clean(attrs.get("testid")),
        "dom_id": _clean(attrs.get("id")),
        "name": _clean(attrs.get("name")),
    }
    for locator in locators:
        strategy = str(locator.get("strategy") or "").lower()
        if strategy == "testid" and not identity["testid"]:
            identity["testid"] = _clean(locator.get("value"))
        elif strategy == "id" and not identity["dom_id"]:
            identity["dom_id"] = _clean(locator.get("value"))
        elif strategy == "name" and not identity["name"]:
            identity["name"] = _clean(locator.get("value"))
    return identity


def is_redundant_focus_click(
    click_step: Dict[str, Any],
    next_step: Optional[Dict[str, Any]],
) -> bool:
    """Detect a recorder-generated focus click immediately followed by filling the same field."""
    if not isinstance(next_step, dict):
        return False
    if str(click_step.get("action") or click_step.get("type") or "").lower() != "click":
        return False
    if str(next_step.get("action") or next_step.get("type") or "").lower() not in {"fill", "input"}:
        return False
    if click_step.get("preserve_click") or click_step.get("disable_focus_click_coalescing"):
        return False

    click_element = click_step.get("element") or {}
    fill_element = next_step.get("element") or {}
    if not click_element or not fill_element:
        return False

    for element in (click_element, fill_element):
        fingerprint = element.get("fingerprint") or {}
        control_type = str(fingerprint.get("control_type") or "").lower()
        if control_type in {"plate_input", "virtual_keyboard_key"}:
            return False

    tag = str(
        click_element.get("tag")
        or (click_element.get("fingerprint") or {}).get("tag")
        or ""
    ).lower()
    role = str(
        click_element.get("role")
        or (click_element.get("fingerprint") or {}).get("role")
        or ""
    ).lower()
    input_type = str(click_element.get("input_type") or "").lower()
    editable_input = tag == "input" and input_type not in {
        "button", "submit", "reset", "checkbox", "radio", "file", "hidden",
    }
    if not (editable_input or tag == "textarea" or role == "textbox"):
        return False

    left = _recorded_element_identity(click_element)
    right = _recorded_element_identity(fill_element)
    if left["stored_id"] and left["stored_id"] == right["stored_id"]:
        return True
    if left["testid"] and left["testid"] == right["testid"]:
        return True
    if left["dom_id"] and left["dom_id"] == right["dom_id"]:
        return True
    return bool(
        left["name"]
        and left["name"] == right["name"]
        and left["tag"] == right["tag"]
        and left["accessible_name"]
        and left["accessible_name"] == right["accessible_name"]
    )


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _similarity(left: Any, right: Any) -> float:
    left_value, right_value = _clean(left), _clean(right)
    if not left_value or not right_value:
        return 0.0
    if left_value == right_value:
        return 1.0
    return SequenceMatcher(None, left_value, right_value).ratio()


def _semantic_similarity(expected: Any, actual: Any) -> float:
    """Label similarity tolerant of prefix truncation.

    Recorded accessible names may include document-link text that later
    becomes separate elements, leaving the runtime label as a prefix of the
    recorded one (e.g. ``本人已阅读并同意签署`` vs the full string with policy
    titles). A character-level ratio under-penalises this, so when one label
    is an exact prefix of the other and the shorter side is meaningful (>=6
    chars) the match is treated as a strong partial (0.9).
    """
    expected_value, actual_value = _clean(expected), _clean(actual)
    if not expected_value or not actual_value:
        return 0.0
    if expected_value == actual_value:
        return 1.0
    similarity = SequenceMatcher(None, expected_value, actual_value).ratio()
    if (
        expected_value.startswith(actual_value)
        or actual_value.startswith(expected_value)
    ) and min(len(expected_value), len(actual_value)) >= 6:
        similarity = max(similarity, 0.9)
    return similarity


def _semantic_label_match(expected: Any, actual: Any) -> tuple[bool, float]:
    expected_value, actual_value = _clean(expected), _clean(actual)
    if not expected_value or not actual_value:
        return False, 0.0
    if expected_value == actual_value:
        return True, 1.0
    similarity = _semantic_similarity(expected_value, actual_value)
    return similarity >= 0.85, similarity


def _primary_semantic_conflict(fingerprint: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Detect visible form fields/buttons whose displayed meaning conflicts with the recorded step."""
    for key in ("accessible_name", "text"):
        expected = _clean(fingerprint.get(key))
        actual = _clean(candidate.get(key))
        if not expected or not actual:
            continue
        matched, similarity = _semantic_label_match(expected, actual)
        if not matched:
            return {
                "conflict": True,
                "field": key,
                "expected": expected,
                "actual": actual,
                "similarity": round(similarity, 3),
            }
    return {"conflict": False}


def score_web_candidate(fingerprint: Dict[str, Any], candidate: Dict[str, Any]) -> Tuple[float, list[str]]:
    """根据稳定属性、可访问语义、文本、父级和相对位置对 DOM 候选评分。"""
    score = 0.0
    reasons: list[str] = []
    expected_attrs = fingerprint.get("attrs") or {}
    actual_attrs = candidate.get("attrs") or {}

    for key, weight in (("testid", 70), ("id", 55), ("name", 40), ("placeholder", 26), ("type", 8)):
        expected = _clean(expected_attrs.get(key))
        actual = _clean(actual_attrs.get(key))
        if expected and expected == actual:
            score += weight
            reasons.append(f"attrs.{key}+{weight}")
        elif expected and key in {"testid", "id", "name"}:
            score -= weight * 0.4

    for key, weight in (("role", 24), ("tag", 10)):
        expected = _clean(fingerprint.get(key))
        actual = _clean(candidate.get(key))
        if expected and expected == actual:
            score += weight
            reasons.append(f"{key}+{weight}")

    for key, weight in (("accessible_name", 42), ("text", 30)):
        similarity = _semantic_similarity(fingerprint.get(key), candidate.get(key))
        if similarity >= 0.98:
            score += weight
            reasons.append(f"{key}+{weight}")
        elif similarity >= 0.65:
            partial = weight * similarity * 0.7
            score += partial
            reasons.append(f"{key}+{partial:.1f}")

    expected_parent = fingerprint.get("parent") or {}
    actual_parent = candidate.get("parent") or {}
    if _clean(expected_parent.get("tag")) and _clean(expected_parent.get("tag")) == _clean(actual_parent.get("tag")):
        score += 5
        reasons.append("parent.tag+5")
    parent_similarity = _similarity(expected_parent.get("text"), actual_parent.get("text"))
    if parent_similarity >= 0.7:
        partial = 12 * parent_similarity
        score += partial
        reasons.append(f"parent.text+{partial:.1f}")

    expected_position = fingerprint.get("normalized_position") or {}
    actual_position = candidate.get("normalized_position") or {}
    try:
        dx = float(expected_position["x"]) - float(actual_position["x"])
        dy = float(expected_position["y"]) - float(actual_position["y"])
        distance = (dx * dx + dy * dy) ** 0.5
        position_score = max(0.0, 12.0 * (1.0 - distance / 0.45))
        score += position_score
        if position_score:
            reasons.append(f"position+{position_score:.1f}")
    except (KeyError, TypeError, ValueError):
        pass

    return round(score, 2), reasons


def _attr_selector(attribute: str, value: Any) -> str:
    escaped = str(value or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'[{attribute}="{escaped}"]'


def _truthy_env(value: str, default: bool = False) -> bool:
    if value == "":
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _int_setting(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _json_loads_dict(value: Any) -> Dict[str, Any]:
    try:
        parsed = json.loads(value or "{}") if isinstance(value, str) else value
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_loads_list(value: Any) -> list:
    try:
        parsed = json.loads(value or "[]") if isinstance(value, str) else value
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _format_locator(candidate: Dict[str, Any]) -> str:
    strategy = str(candidate.get("strategy") or "").lower()
    value = candidate.get("value")
    if strategy == "role":
        role = candidate.get("role") or value or ""
        name = candidate.get("name")
        return f"role={role}" + (f"[name={name}]" if name else "")
    if strategy == "id":
        return f"id={value}"
    if strategy == "testid":
        return f"data-testid={value}"
    if strategy:
        return f"{strategy}={value}"
    return str(value or "")


class WebFlowRunner:
    """执行一个浏览器步骤流，并为每一步输出定位诊断。"""

    def __init__(self, page, flow: Dict[str, Any]):
        self.page = page
        self.flow = resolve_variables(flow)
        self.runtime_context = RuntimeDataContext(_runtime_context())
        raw_flow_variables = self.flow.get("variables") or {}
        if isinstance(raw_flow_variables, dict):
            for variable_name, variable_value in raw_flow_variables.items():
                self.runtime_context.assign(str(variable_name), variable_value)
        elif isinstance(raw_flow_variables, list):
            for variable in raw_flow_variables:
                if isinstance(variable, dict) and variable.get("name"):
                    self.runtime_context.assign(
                        str(variable["name"]),
                        variable.get("value", ""),
                        scope=str(variable.get("scope") or "local"),
                    )
        self.runtime_outputs: list[Dict[str, Any]] = []
        self.step_results: list[Dict[str, Any]] = []
        self.debug_pause_event = None
        self.debug_step_event = None
        self.debug_step_name = ""
        self.rtds_store = None
        self.rtds_lock = None
        self.auto_correlate = bool(self.flow.get("auto_correlate") or False)
        self._correlate_history: Dict[str, Any] = {}
        self.recorded_base_url = str(self.flow.get("base_url") or "").rstrip("/")
        self.runtime_base_url = str(
            self.flow.get("_runtime_base_url")
            or os.getenv("BASE_URL")
            or self.recorded_base_url
        ).rstrip("/")
        runtime_public = self.runtime_context.public()
        repair_strategy = (runtime_public.get("local") or {}).get("_runnergo_repair_strategy")
        self.repair_strategy = repair_strategy if isinstance(repair_strategy, dict) else {}
        self.default_timeout = int(
            self.repair_strategy.get("timeout_ms")
            or self.flow.get("timeout")
            or 10000
        )
        self.last_locator_diagnostics: Dict[str, Any] = {}
        self.task_id = str(os.getenv("TASK_ID") or "")
        self.case_file = str(os.getenv("FLOW_CASE_FILE") or "")
        try:
            self.iteration_index = max(1, int(os.getenv("RUNTIME_DATA_ROW_INDEX") or 1))
        except (TypeError, ValueError):
            self.iteration_index = 1
        try:
            self.iteration_count = max(1, int(os.getenv("RUNTIME_DATA_ROW_COUNT") or 1))
        except (TypeError, ValueError):
            self.iteration_count = 1
        self.iteration_label = str(
            os.getenv("RUNTIME_DATA_ROW_LABEL") or f"data-{self.iteration_index}"
        )
        try:
            iteration_data = json.loads(os.getenv("RUNTIME_DATA_ROW_JSON") or "{}")
        except (TypeError, ValueError):
            iteration_data = {}
        self.iteration_data = iteration_data if isinstance(iteration_data, dict) else {}
        try:
            raw_iteration_rows = json.loads(os.getenv("RUNTIME_CONTEXT_ROWS_JSON") or "[]")
        except (TypeError, ValueError):
            raw_iteration_rows = []
        self.iteration_data_rows = []
        if isinstance(raw_iteration_rows, list):
            for row in raw_iteration_rows:
                if not isinstance(row, dict):
                    continue
                data = row.get("dataAssets") or row.get("data") or {}
                self.iteration_data_rows.append(data if isinstance(data, dict) else {})
        self.project_id = str(self.flow.get("project_id") or os.getenv("PROJECT_ID") or "default")
        self.self_heal_enabled = True
        # Repair policies are per-run assistance. They may use semantic
        # healing and report a candidate, but must not mutate stored locators.
        flow_self_heal = self.flow.get("self_heal") or {}
        self.self_heal_apply = _truthy_env(
            os.getenv("AI_SELF_HEAL_APPLY", ""),
            default=bool(flow_self_heal.get("auto_apply", True)),
        ) and not bool(self.repair_strategy.get("self_heal"))
        try:
            self.self_heal_apply_threshold = float(
                self.repair_strategy.get("self_heal_apply_threshold")
                or (self.flow.get("self_heal") or {}).get("apply_threshold")
                or os.getenv("AI_SELF_HEAL_APPLY_THRESHOLD")
                or 0.9
            )
        except (TypeError, ValueError):
            self.self_heal_apply_threshold = 0.9
        self.current_step_index = 0
        self.last_step_screenshot = ""
        self.flow_logs: list[str] = []
        # Imported and historical cases use page-aware replay: a step is really
        # executed only when its recorded element belongs to the current page;
        # otherwise the runner skips it and resumes at a visible future step.
        self.step_execution_mode = str(
            self.flow.get("step_execution_mode") or "page_aware"
        ).strip().lower()
        self.page_aware_execution = self.step_execution_mode != "real"
        smart_wait_setting = (
            self.repair_strategy.get("smart_wait")
            if "smart_wait" in self.repair_strategy
            else (
                self.flow.get("smart_wait")
                if self.flow.get("smart_wait") is not None
                else os.getenv("WEB_SMART_WAIT", "")
            )
        )
        self.smart_wait_enabled = _truthy_env(
            str(smart_wait_setting),
            default=True,
        )
        self.smart_wait_timeout_ms = _int_setting(
            self.flow.get("smart_wait_timeout_ms")
            or os.getenv("WEB_SMART_WAIT_TIMEOUT_MS"),
            default=max(self.default_timeout, 15000),
            minimum=200,
            maximum=60000,
        )
        self.smart_wait_stable_ms = _int_setting(
            self.flow.get("smart_wait_stable_ms")
            or os.getenv("WEB_SMART_WAIT_STABLE_MS"),
            default=120,
            minimum=80,
            maximum=2000,
        )
        self.smart_wait_poll_ms = _int_setting(
            self.flow.get("smart_wait_poll_ms")
            or os.getenv("WEB_SMART_WAIT_POLL_MS"),
            default=80,
            minimum=25,
            maximum=500,
        )
        self.smart_wait_after_grace_ms = _int_setting(
            self.flow.get("smart_wait_after_grace_ms")
            or os.getenv("WEB_SMART_WAIT_AFTER_GRACE_MS"),
            default=120,
            minimum=0,
            maximum=3000,
        )
        self.auto_page_skip_probe_timeout_ms = _int_setting(
            self.flow.get("auto_page_skip_probe_timeout_ms")
            or os.getenv("AUTO_PAGE_SKIP_PROBE_TIMEOUT_MS"),
            default=200,
            minimum=150,
            maximum=1200,
        )
        self.auto_page_skip_scan_limit = _int_setting(
            self.flow.get("auto_page_skip_scan_limit")
            or os.getenv("AUTO_PAGE_SKIP_SCAN_LIMIT"),
            default=40,
            minimum=1,
            maximum=200,
        )
        self._auto_skip_probe_cache: Dict[Tuple[str, int], Dict[str, Any]] = {}
        self._prepared_step_wait: Dict[str, Any] = {}
        self._pending_page_requests: set[int] = set()
        self._last_page_network_activity = time.monotonic()
        self._network_tracking_enabled = False
        self._http_timing_buffer: List[Dict[str, Any]] = []
        self._http_timing_step_start: int = 0
        if self.page is not None and hasattr(self.page, "on"):
            try:
                def request_started(request):
                    self._pending_page_requests.add(id(request))
                    self._last_page_network_activity = time.monotonic()

                def request_finished(request):
                    self._pending_page_requests.discard(id(request))
                    self._last_page_network_activity = time.monotonic()
                    self._record_http_timing(request)

                self.page.on("request", request_started)
                self.page.on("requestfinished", request_finished)
                self.page.on("requestfailed", request_finished)
                self._network_tracking_enabled = True
                self._request_started_listener = request_started
                self._request_finished_listener = request_finished
            except Exception:
                self._network_tracking_enabled = False

    def _resource_type_of(self, request) -> str:
        try:
            rt = getattr(request, "resource_type", None)
            return str(rt) if rt else "other"
        except Exception:
            return "other"

    def _record_http_timing(self, request) -> None:
        try:
            timing = getattr(request, "timing", None)
            if not isinstance(timing, dict) and not (timing and hasattr(timing, "__dict__")):
                return
            t = dict(timing) if isinstance(timing, dict) else dict(vars(timing))
            dns = max(0.0, float(t.get("domainLookupEnd") or 0.0) - float(t.get("domainLookupStart") or 0.0))
            connect = max(0.0, float(t.get("connectEnd") or 0.0) - float(t.get("connectStart") or 0.0))
            tls = 0.0
            if t.get("secureConnectionStart") is not None and float(t.get("secureConnectionStart") or 0.0) > 0:
                tls = max(0.0, float(t.get("connectEnd") or 0.0) - float(t.get("secureConnectionStart") or 0.0))
            ttfb = max(0.0, float(t.get("responseStart") or 0.0) - float(t.get("requestStart") or 0.0))
            receive = max(0.0, float(t.get("responseEnd") or 0.0) - float(t.get("responseStart") or 0.0))
            total = max(0.0, float(t.get("responseEnd") or 0.0))
            url = str(getattr(request, "url", "") or "")
            method = str(getattr(request, "method", "") or "GET")
            self._http_timing_buffer.append({
                "url": url[:300],
                "method": method,
                "status": 0,
                "resource_type": self._resource_type_of(request),
                "start_ms": round(float(t.get("startTime") or 0.0), 2),
                "dns_ms": round(dns, 2),
                "connect_ms": round(connect, 2),
                "tls_ms": round(tls, 2),
                "ttfb_ms": round(ttfb, 2),
                "receive_ms": round(receive, 2),
                "total_ms": round(total, 2),
            })
        except Exception:
            return

    def _begin_http_timing_step(self) -> None:
        self._http_timing_step_start = len(self._http_timing_buffer)

    def _collect_http_timing_step(self) -> List[Dict[str, Any]]:
        items = self._http_timing_buffer[self._http_timing_step_start:]
        self._http_timing_step_start = len(self._http_timing_buffer)
        return items

    def _resolve_runtime_step(self, step: Dict[str, Any]) -> Dict[str, Any]:
        raw_scripts = copy.deepcopy(step.get("_scripts"))
        resolved = self.runtime_context.resolve(step)
        if isinstance(resolved, dict) and isinstance(raw_scripts, dict):
            # JavaScript template literals also use ${...}; script source must
            # reach the restricted runtime unchanged.
            resolved["_scripts"] = raw_scripts
        return resolved if isinstance(resolved, dict) else dict(step)

    def _auto_correlate_response(self, result: Dict[str, Any], step: Dict[str, Any]) -> None:
        _DYNAMIC_KEYS = (
            "id", "token", "sessionid", "session_id", "csrftoken", "csrf_token",
            "ticket", "nonce", "uuid", "key", "code", "auth", "authorization",
            "refreshtoken", "access_token", "userid", "user_id", "orderid",
            "order_id", "traceno", "trace_id", "requestid", "request_id",
        )
        body = result.get("json") or result.get("body") or result.get("data")
        if not isinstance(body, dict):
            return
        step_name = str(step.get("name") or step.get("id") or "")
        extracted = {}
        def _scan(obj, prefix=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    kl = str(k).lower()
                    if kl in _DYNAMIC_KEYS and v is not None:
                        var_name = f"auto_{kl}"
                        self.runtime_context.assign(var_name, v)
                        extracted[var_name] = v
                    if isinstance(v, (dict, list)):
                        _scan(v, f"{prefix}.{k}" if prefix else k)
            elif isinstance(obj, list):
                for i, item in enumerate(obj[:3]):
                    if isinstance(item, (dict, list)):
                        _scan(item, f"{prefix}[{i}]")
        _scan(body)
        if extracted:
            self._runtime_log(f"[自动关联] {step_name}: 提取 {len(extracted)} 个动态参数 → {list(extracted.keys())}")

    def _attach_runtime_data(self, title: str, result: Any) -> None:
        record = {"step": title, "result": result}
        self.runtime_outputs.append(record)
        if not allure:
            return
        try:
            allure.attach(
                json.dumps(redact(record), ensure_ascii=False, indent=2, default=str),
                name=f"数据交换-{title}",
                attachment_type=allure.attachment_type.JSON,
            )
        except Exception:
            pass

    def _attach_runtime_context(self) -> None:
        if not allure:
            return
        try:
            allure.attach(
                json.dumps(redact(self.runtime_context.public()), ensure_ascii=False, indent=2, default=str),
                name="混合步骤运行时数据上下文",
                attachment_type=allure.attachment_type.JSON,
            )
        except Exception:
            pass

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def _runtime_log(self, message: str):
        value = str(message)
        self.flow_logs.append(value)
        if not self.task_id:
            return
        try:
            from backend import log_store
            log_store.write(self.task_id, value)
        except Exception:
            pass

    def _initialize_runtime_steps(self):
        self.step_results = []
        for index, step in enumerate(self.flow.get("steps") or [], start=1):
            step = step if isinstance(step, dict) else {}
            self.step_results.append({
                "index": index,
                "id": str(step.get("id") or ""),
                "name": str(step.get("name") or step.get("action") or step.get("type") or f"步骤{index}"),
                "action": str(step.get("action") or step.get("type") or ""),
                "status": "pending",
                "duration": 0.0,
                "error": "",
                "diagnostics": {},
            })
        if not self.task_id:
            return
        try:
            from backend import step_store
            step_store.initialize(
                self.task_id,
                self.flow,
                case_file=self.case_file,
                iteration_index=self.iteration_index,
                iteration_count=self.iteration_count,
                iteration_label=self.iteration_label,
                iteration_data=self.iteration_data,
                iteration_data_rows=self.iteration_data_rows,
            )
        except Exception as exc:
            self._runtime_log(f"⚠️ 初始化步骤状态失败: {exc}")

    def _update_runtime_step(self, index: int, **fields):
        if 1 <= index <= len(self.step_results):
            result = self.step_results[index - 1]
            for key, value in fields.items():
                if key in {
                    "status", "duration", "error", "diagnostics", "started_at",
                    "finished_at", "log", "screenshot", "assertion_matched",
                }:
                    result[key] = copy.deepcopy(value)
        if not self.task_id:
            return
        try:
            from backend import step_store
            step_store.update_step(
                self.task_id,
                index,
                iteration_index=self.iteration_index if self.iteration_count > 1 else None,
                **fields,
            )
        except Exception as exc:
            self._runtime_log(f"⚠️ 更新第 {index} 步状态失败: {exc}")

    def _finalize_runtime_steps(self, status: str, error: str = ""):
        terminal = "failed" if status == "failed" else "skipped"
        for result in self.step_results:
            if result.get("status") in {"pending", "running"}:
                result["status"] = terminal
                if error and terminal == "failed":
                    result["error"] = str(error)[:5000]
        if not self.task_id:
            return
        try:
            from backend import step_store
            step_store.finalize(
                self.task_id,
                status,
                error=error,
                iteration_index=self.iteration_index if self.iteration_count > 1 else None,
            )
        except Exception as exc:
            self._runtime_log(f"⚠️ 完成步骤状态失败: {exc}")

    def _screenshot_filename(self, step: Dict[str, Any], status: str) -> str:
        raw = str(step.get("id") or step.get("name") or step.get("action") or "step")
        slug = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_")[:56] or "step"
        prefix = f"data_{self.iteration_index:02d}_" if self.iteration_count > 1 else ""
        return f"{prefix}{max(1, self.current_step_index):03d}_{slug}_{status}.png"

    def _capture_step_screenshot(
        self,
        step: Dict[str, Any],
        *,
        status: str,
        attachment_name: str,
        full_page: bool = False,
    ) -> str:
        if step.get("capture_screenshot") is False:
            return ""
        try:
            if hasattr(self.page, "is_closed") and self.page.is_closed():
                return ""
            image = self.page.screenshot(full_page=full_page)
        except Exception:
            return ""
        if allure:
            try:
                allure.attach(
                    image,
                    name=attachment_name,
                    attachment_type=allure.attachment_type.PNG,
                )
            except Exception:
                pass
        screenshot_url = ""
        if self.task_id:
            try:
                from backend import settings
                directory = os.path.join(settings.SCREENSHOTS_DIR, self.task_id)
                os.makedirs(directory, exist_ok=True)
                filename = self._screenshot_filename(step, status)
                with open(os.path.join(directory, filename), "wb") as handle:
                    handle.write(image)
                screenshot_url = f"/screenshots/{self.task_id}/{filename}"
            except Exception:
                screenshot_url = ""
        self.last_step_screenshot = screenshot_url
        return screenshot_url

    def _attach_step_log(self, title: str, text: str):
        if not allure:
            return
        try:
            allure.attach(
                text,
                name=f"执行日志-{title}",
                attachment_type=allure.attachment_type.TEXT,
            )
        except Exception:
            pass

    def _attach_step_summary(
        self,
        *,
        total_steps: int,
        executed_steps: int,
        passed_steps: int,
        failed_steps: int,
        skipped_steps: int,
        status: str,
    ):
        """Attach machine-readable step counts so Allure cannot hide skips."""
        if not allure:
            return
        summary = {
            "total": int(total_steps),
            "executed": int(executed_steps),
            "passed": int(passed_steps),
            "failed": int(failed_steps),
            "skipped": int(skipped_steps),
            "status": str(status),
        }
        try:
            allure.attach(
                json.dumps(summary, ensure_ascii=False, indent=2),
                name="步骤统计",
                attachment_type=allure.attachment_type.JSON,
            )
        except Exception:
            pass

    def _runtime_url(self, value: Any) -> str:
        url = str(resolve_variables(value) or "")
        if not url:
            return self.runtime_base_url
        if self.recorded_base_url and self.runtime_base_url and url.startswith(self.recorded_base_url):
            return self.runtime_base_url + url[len(self.recorded_base_url):]
        if url.startswith(("http://", "https://")) and self.recorded_base_url and self.runtime_base_url:
            recorded = urlsplit(self.recorded_base_url)
            runtime = urlsplit(self.runtime_base_url)
            current = urlsplit(url)
            if current.netloc == recorded.netloc and runtime.netloc:
                return current._replace(scheme=runtime.scheme, netloc=runtime.netloc).geturl()
        if url.startswith(("http://", "https://")):
            return url
        return urljoin(self.runtime_base_url.rstrip("/") + "/", url.lstrip("/"))

    def _frame_for_element(self, element: Dict[str, Any]):
        frame = self.page.main_frame
        traversed = []
        for target in element.get("frame_path") or []:
            child = None
            selector = str(target.get("selector") or "")
            if selector:
                try:
                    handle = frame.locator(selector).first.element_handle(timeout=2000)
                    child = handle.content_frame() if handle else None
                except Exception:
                    child = None
            if child is None:
                expected_name = str(target.get("name") or "")
                expected_url = str(target.get("url") or "")
                for candidate in frame.child_frames:
                    if expected_name and candidate.name == expected_name:
                        child = candidate
                        break
                    if expected_url and expected_url in candidate.url:
                        child = candidate
                        break
            if child is None:
                raise AssertionError(f"iframe 上下文定位失败: {target}")
            traversed.append({"selector": selector, "name": child.name, "url": child.url})
            frame = child
        return frame, traversed

    def _candidate_locator(self, scope, candidate: Dict[str, Any]):
        strategy = str(candidate.get("strategy") or "").lower()
        value = candidate.get("value")
        if strategy == "testid":
            return scope.get_by_test_id(str(value))
        if strategy == "role":
            role = str(candidate.get("role") or "")
            index = candidate.get("index")
            name = candidate.get("name")
            if name is None and index is None:
                name = value
            locator = scope.get_by_role(role, name=str(name), exact=True) if name else scope.get_by_role(role)
            if index is not None:
                locator = locator.nth(int(index))
            return locator
        if strategy == "group_index":
            group_text = str(candidate.get("group_text") or "")
            if not group_text:
                return None
            group = scope.get_by_text(group_text, exact=True).first.locator("xpath=..")
            role = str(candidate.get("role") or "")
            tag = str(candidate.get("tag") or "")
            if role:
                locator = group.get_by_role(role)
            elif tag:
                locator = group.locator(tag)
            else:
                return None
            return locator.nth(int(candidate.get("index") or 0))
        if strategy == "keyboard_text":
            key_text = str(value or "")
            selector = ",".join((
                ".car-keyboard-grids-btn",
                "[data-key]",
                "[data-key-value]",
                "[data-value]",
                "[class*='keyboard'] button",
                "[class*='keyboard'] [role='button']",
                "[class*='keyboard'] li",
                "[class*='keyboard'] span",
                "[class*='keyboard'] div",
                "[class*='key-board'] button",
                "[class*='key-board'] [role='button']",
                "[class*='key-board'] span",
                "[class*='key-board'] div",
                "[role='dialog'] button",
                "[role='dialog'] [role='button']",
                "[role='dialog'] span",
                "[role='dialog'] div",
                ".van-popup button",
                ".van-popup [role='button']",
                ".car-tooltips-submit",
            ))
            return scope.locator(selector).filter(
                has_text=re.compile(rf"^\s*{re.escape(key_text)}\s*$")
            )
        if strategy == "label":
            return scope.get_by_label(str(value), exact=True)
        if strategy == "placeholder":
            return scope.get_by_placeholder(str(value), exact=True)
        if strategy == "id":
            return scope.locator(_attr_selector("id", value))
        if strategy == "name":
            return scope.locator(_attr_selector("name", value))
        if strategy == "text":
            return scope.get_by_text(str(value), exact=True)
        if strategy == "css":
            return scope.locator(str(value))
        if strategy == "xpath":
            return scope.locator(f"xpath={value}")
        return None

    def _heal_locator(self, scope, fingerprint: Dict[str, Any]):
        raw_candidates = scope.evaluate(
            HEAL_CANDIDATES_SCRIPT,
            {"targetTag": fingerprint.get("tag") or ""},
        )
        ranked = []
        for candidate in raw_candidates or []:
            if not candidate.get("css"):
                continue
            score, reasons = score_web_candidate(fingerprint, candidate)
            semantic_conflict = _primary_semantic_conflict(fingerprint, candidate)
            ranked.append({
                "score": score,
                "reasons": reasons,
                "candidate": candidate,
                "semantic_conflict": semantic_conflict,
            })
        ranked.sort(key=lambda item: item["score"], reverse=True)
        preview = [
            {
                "score": item["score"],
                "reasons": item["reasons"],
                "css": item["candidate"].get("css"),
                "role": item["candidate"].get("role"),
                "name": item["candidate"].get("accessible_name"),
                "text": item["candidate"].get("text"),
                "semantic_conflict": item["semantic_conflict"],
            }
            for item in ranked[:5]
        ]
        minimum_score = float(fingerprint.get("minimum_score") or 55)
        minimum_gap = float(fingerprint.get("minimum_gap") or 8)
        diagnostics = {
            "strategy": "semantic_healing",
            "minimum_score": minimum_score,
            "minimum_gap": minimum_gap,
            "candidates": preview,
        }
        ranked = [item for item in ranked if not item["semantic_conflict"].get("conflict")]
        if not ranked or ranked[0]["score"] < minimum_score:
            diagnostics["matched"] = False
            diagnostics["reason"] = "score-below-threshold"
            return None, diagnostics
        gap = ranked[0]["score"] - ranked[1]["score"] if len(ranked) > 1 else ranked[0]["score"]
        if gap < minimum_gap:
            diagnostics["matched"] = False
            diagnostics["reason"] = "ambiguous-candidates"
            diagnostics["score_gap"] = round(gap, 2)
            return None, diagnostics
        locator = scope.locator(ranked[0]["candidate"]["css"]).first
        diagnostics.update({
            "matched": True,
            "score": ranked[0]["score"],
            "score_gap": round(gap, 2),
            "selected_css": ranked[0]["candidate"]["css"],
            "selected_candidate": ranked[0]["candidate"],
            "confidence": round(min(0.99, ranked[0]["score"] / 100), 3),
        })
        return locator, diagnostics

    def _build_self_heal_repair(
        self,
        element: Dict[str, Any],
        attempts: list[Dict[str, Any]],
        healing_attempt: Dict[str, Any],
    ) -> Dict[str, Any]:
        selected_css = str(healing_attempt.get("selected_css") or "")
        if not selected_css:
            return {}
        failed = [
            item for item in attempts
            if item.get("strategy") and not item.get("matched")
        ]
        old_locator = _format_locator(failed[0]) if failed else ""
        selected_candidate = healing_attempt.get("selected_candidate") or {}
        selected_attrs = selected_candidate.get("attrs") or {}
        fingerprint = element.get("fingerprint") or {}
        expected_attrs = fingerprint.get("attrs") or {}
        change_reasons = []
        for key in ("testid", "id", "name"):
            old_value = _clean(expected_attrs.get(key))
            new_value = _clean(selected_attrs.get(key))
            if old_value and new_value and old_value != new_value:
                change_reasons.append(f"{key}: {old_value} -> {new_value}")
            elif old_value and key == "id" and _clean(selected_attrs.get("class")):
                change_reasons.append(f"id: {old_value} -> class: {_clean(selected_attrs.get('class'))}")
        if not change_reasons:
            change_reasons.append("原定位器失效，语义、文本与页面位置匹配新元素")

        try:
            confidence = float(healing_attempt.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence <= 0:
            confidence = min(0.99, float(healing_attempt.get("score") or 0) / 100)
        confidence = round(max(0.0, min(confidence, 0.99)), 3)
        should_apply = bool(
            self.self_heal_apply
            and confidence >= self.self_heal_apply_threshold
            and healing_attempt.get("matched")
        )
        return {
            "reason": "元素属性变化" if change_reasons else "语义定位自愈命中",
            "details": change_reasons,
            "old_locator": old_locator,
            "new_locator": f"css={selected_css}",
            "new_element": {
                "tag": selected_candidate.get("tag"),
                "role": selected_candidate.get("role"),
                "text": selected_candidate.get("text"),
                "accessible_name": selected_candidate.get("accessible_name"),
                "attrs": selected_attrs,
            },
            "confidence": confidence,
            "confidence_percent": round(confidence * 100),
            "should_apply": should_apply,
            "applied": False,
            "apply_threshold": self.self_heal_apply_threshold,
        }

    def _persist_self_heal_repair(
        self,
        element: Dict[str, Any],
        repair: Dict[str, Any],
    ) -> Dict[str, Any]:
        element_id = str(element.get("id") or "")
        new_locator = str(repair.get("new_locator") or "")
        selected_css = new_locator.split("=", 1)[1] if new_locator.startswith("css=") else ""
        if not element_id or not selected_css or not repair.get("should_apply"):
            return repair
        try:
            from backend import db

            rows = db.execute(
                "SELECT locators,fingerprint,element_context FROM web_elements "
                "WHERE id=? AND project_id=? LIMIT 1",
                (element_id, self.project_id),
                fetch=True,
            )
            if not rows:
                repair["apply_error"] = "元素库未找到该元素"
                return repair
            row = rows[0]
            locators = _json_loads_list(row["locators"])
            fingerprint = _json_loads_dict(row["fingerprint"])
            element_context = _json_loads_dict(row["element_context"])
            existing_css = [
                item for item in locators
                if str(item.get("strategy") or "").lower() == "css"
                and item.get("value") == selected_css
            ]
            old_locators = locators[:]
            if not existing_css:
                locators.insert(0, {
                    "strategy": "css",
                    "value": selected_css,
                    "source": "ai_self_heal",
                    "confidence": repair.get("confidence"),
                })
            selected_element = repair.get("new_element") or {}
            selected_attrs = selected_element.get("attrs") or {}
            if selected_attrs:
                attrs = dict(fingerprint.get("attrs") or {})
                for key in ("testid", "id", "name", "placeholder", "type"):
                    if selected_attrs.get(key):
                        attrs[key] = selected_attrs.get(key)
                fingerprint["attrs"] = attrs
            for key, source_key in (("tag", "tag"), ("role", "role"), ("accessible_name", "accessible_name"), ("text", "text")):
                value = selected_element.get(source_key)
                if value:
                    fingerprint[key] = value
            history = element_context.setdefault("self_heal_history", [])
            history.insert(0, {
                "reason": repair.get("reason"),
                "details": repair.get("details") or [],
                "old_locator": repair.get("old_locator"),
                "new_locator": repair.get("new_locator"),
                "confidence": repair.get("confidence"),
                "case_file": self.case_file,
                "step_index": self.current_step_index,
                "created_at": self._timestamp(),
            })
            element_context["self_heal_history"] = history[:20]
            db.execute(
                "UPDATE web_elements SET locators=?, fingerprint=?, element_context=?, updated_at=? "
                "WHERE id=? AND project_id=?",
                (
                    json.dumps(locators, ensure_ascii=False),
                    json.dumps(fingerprint, ensure_ascii=False),
                    json.dumps(element_context, ensure_ascii=False),
                    self._timestamp(),
                    element_id,
                    self.project_id,
                ),
            )
            repair.update({
                "applied": True,
                "element_id": element_id,
                "old_locators": old_locators[:5],
            })
        except Exception as exc:
            repair["apply_error"] = str(exc)[:300]
        return repair

    def _validate_direct_locator_candidate(
        self,
        target,
        locator_candidate: Dict[str, Any],
        fingerprint: Dict[str, Any],
        force_semantic: bool = False,
    ) -> Tuple[bool, Dict[str, Any]]:
        """Reject locator matches that resolve to a semantically different element."""
        strategy = str(locator_candidate.get("strategy") or "").lower()
        expected_attrs = fingerprint.get("attrs") or {}
        expected_type = _clean(expected_attrs.get("type")).lower()
        requires_semantic_check = strategy in {"id", "name", "css", "xpath"}
        if strategy == "placeholder" and (
            _clean(fingerprint.get("accessible_name")) or _clean(fingerprint.get("text"))
        ):
            requires_semantic_check = True
        if expected_type:
            requires_semantic_check = True
        if not fingerprint or (not force_semantic and not requires_semantic_check):
            return True, {"checked": False}
        try:
            actual = target.evaluate(POINT_CANDIDATE_SCRIPT)
        except Exception as exc:
            return False, {"checked": True, "reason": "candidate-probe-error", "error": str(exc)[:300]}
        if not isinstance(actual, dict):
            return True, {"checked": False, "reason": "candidate-probe-unavailable"}

        actual_attrs = actual.get("attrs") or {}
        expected_tag = _clean(fingerprint.get("tag"))
        actual_tag = _clean(actual.get("tag"))
        if expected_tag and actual_tag and expected_tag != actual_tag:
            return False, {
                "checked": True,
                "reason": "tag-mismatch",
                "expected_tag": expected_tag,
                "actual_tag": actual_tag,
            }

        signals = []
        strong_attribute_match = False
        placeholder_match = False
        for key in ("testid", "name", "placeholder"):
            expected = _clean(expected_attrs.get(key))
            if expected:
                field_matched = expected == _clean(actual_attrs.get(key))
                signals.append({
                    "field": f"attrs.{key}",
                    "matched": field_matched,
                })
                if key == "testid" and field_matched:
                    strong_attribute_match = True
                elif key == "name" and field_matched:
                    # Values such as picker/select are shared by many fields in
                    # component libraries. They are useful hints, but not strong
                    # enough to override an accessible-name mismatch.
                    generic_names = {"field", "input", "picker", "select", "textarea"}
                    if expected.lower() not in generic_names:
                        strong_attribute_match = True
                if key == "placeholder" and field_matched:
                    placeholder_match = True
        expected_name = _clean(fingerprint.get("accessible_name"))
        name_match = False
        name_conflict = False
        if expected_name:
            name_match, similarity = _semantic_label_match(expected_name, actual.get("accessible_name"))
            name_conflict = bool(_clean(actual.get("accessible_name")) and not name_match)
            signals.append({"field": "accessible_name", "matched": name_match, "similarity": round(similarity, 3)})
        expected_text = _clean(fingerprint.get("text"))
        text_match = False
        text_conflict = False
        if expected_text:
            text_match, similarity = _semantic_label_match(expected_text, actual.get("text"))
            text_conflict = bool(_clean(actual.get("text")) and not text_match)
            signals.append({"field": "text", "matched": text_match, "similarity": round(similarity, 3)})

        actual_type = _clean(actual_attrs.get("type")).lower()
        if expected_type:
            type_matched = expected_type == actual_type or (expected_type == "text" and not actual_type)
            signals.append({"field": "attrs.type", "matched": type_matched})
            if not type_matched:
                return False, {
                    "checked": True,
                    "matched": False,
                    "reason": "type-mismatch",
                    "signals": signals,
                    "actual": {
                        "tag": actual.get("tag"),
                        "role": actual.get("role"),
                        "accessible_name": actual.get("accessible_name"),
                        "text": actual.get("text"),
                        "attrs": actual_attrs,
                    },
                }

        score, reasons = score_web_candidate(fingerprint, actual)
        if expected_name:
            matched = name_match or (strong_attribute_match and not name_conflict)
        elif expected_text:
            matched = text_match or (strong_attribute_match and not text_conflict)
        else:
            has_secondary_identity = any(
                _clean(expected_attrs.get(key)) for key in ("testid", "name", "placeholder")
            )
            matched = not has_secondary_identity or strong_attribute_match or placeholder_match
        if name_conflict or text_conflict:
            matched = False
        return matched, {
            "checked": True,
            "matched": matched,
            "reason": "semantic-conflict" if (name_conflict or text_conflict) else (
                "semantic-mismatch" if not matched else "semantic-match"
            ),
            "score": score,
            "reasons": reasons,
            "signals": signals,
            "actual": {
                "tag": actual.get("tag"),
                "role": actual.get("role"),
                "accessible_name": actual.get("accessible_name"),
                "text": actual.get("text"),
                "attrs": actual_attrs,
            },
        }

    def _smart_wait_for_page(
        self,
        step: Dict[str, Any],
        *,
        phase: str = "before",
    ) -> Dict[str, Any]:
        """Wait until loading UI, network requests and DOM mutations have settled."""
        action = str(step.get("action") or step.get("type") or "").strip().lower()
        step_setting = step.get("smart_wait")
        enabled = self.smart_wait_enabled if step_setting is None else _truthy_env(str(step_setting), default=True)
        if not enabled:
            return {"enabled": False, "ready": True, "phase": phase, "reason": "disabled"}
        if action in NON_BROWSER_ACTIONS:
            return {"enabled": True, "ready": True, "phase": phase, "reason": "non-browser-step"}
        if phase == "after" and action not in PAGE_MUTATING_ACTIONS:
            return {"enabled": True, "ready": True, "phase": phase, "reason": "read-only-step"}
        if self.page is None or not hasattr(self.page, "evaluate"):
            return {"enabled": True, "ready": True, "phase": phase, "reason": "page-probe-unavailable"}

        raw_timeout = (
            step.get("smart_wait_timeout_ms")
            or step.get("smart_wait_timeout")
            or step.get("timeout")
            or self.smart_wait_timeout_ms
        )
        timeout_ms = _int_setting(raw_timeout, self.smart_wait_timeout_ms, 200, 60000)
        stable_ms = _int_setting(
            step.get("smart_wait_stable_ms") or self.smart_wait_stable_ms,
            self.smart_wait_stable_ms,
            80,
            2000,
        )
        poll_ms = _int_setting(
            step.get("smart_wait_poll_ms") or self.smart_wait_poll_ms,
            self.smart_wait_poll_ms,
            25,
            500,
        )
        grace_ms = (
            _int_setting(
                step.get("smart_wait_after_grace_ms") or self.smart_wait_after_grace_ms,
                self.smart_wait_after_grace_ms,
                0,
                3000,
            )
            if phase == "after" else 0
        )
        started_at = time.monotonic()
        deadline = started_at + timeout_ms / 1000
        polls = 0
        saw_blocking_ui = False
        last_blocking: Dict[str, Any] = {"found": False}
        last_page_state: Dict[str, Any] = {}

        while True:
            polls += 1
            now = time.monotonic()
            try:
                blocking = self.page.evaluate(BLOCKING_UI_STATE_SCRIPT)
                last_blocking = blocking if isinstance(blocking, dict) else {"found": False}
                saw_blocking_ui = saw_blocking_ui or bool(last_blocking.get("found"))
                page_state = self.page.evaluate(PAGE_READY_STATE_SCRIPT)
                last_page_state = page_state if isinstance(page_state, dict) else {}
            except Exception as exc:
                return {
                    "enabled": True,
                    "ready": True,
                    "phase": phase,
                    "reason": "page-probe-error",
                    "error": str(exc)[:300],
                    "waited_ms": round((time.monotonic() - started_at) * 1000),
                    "polls": polls,
                }

            mutation_age_ms = int(last_page_state.get("mutation_age_ms") or 0)
            network_idle_ms = round(max(0.0, now - self._last_page_network_activity) * 1000)
            pending_requests = len(self._pending_page_requests)
            document_ready = (
                bool(last_page_state.get("has_body"))
                and str(last_page_state.get("ready_state") or "") in {"interactive", "complete"}
            )
            elapsed_ms = round((now - started_at) * 1000)
            ready = (
                not last_blocking.get("found")
                and document_ready
                and mutation_age_ms >= stable_ms
                and pending_requests == 0
                and (not self._network_tracking_enabled or network_idle_ms >= stable_ms)
                and elapsed_ms >= grace_ms
            )
            if ready:
                return {
                    "enabled": True,
                    "ready": True,
                    "phase": phase,
                    "waited_ms": elapsed_ms,
                    "polls": polls,
                    "stable_ms": stable_ms,
                    "mutation_age_ms": mutation_age_ms,
                    "network_idle_ms": network_idle_ms,
                    "pending_requests": pending_requests,
                    "saw_blocking_ui": saw_blocking_ui,
                    "page_state": last_page_state,
                }
            if now >= deadline:
                return {
                    "enabled": True,
                    "ready": False,
                    "timed_out": True,
                    "phase": phase,
                    "waited_ms": elapsed_ms,
                    "polls": polls,
                    "stable_ms": stable_ms,
                    "mutation_age_ms": mutation_age_ms,
                    "network_idle_ms": network_idle_ms,
                    "pending_requests": pending_requests,
                    "saw_blocking_ui": saw_blocking_ui,
                    "last_blocking": last_blocking,
                    "page_state": last_page_state,
                }
            remaining_ms = max(1, round((deadline - now) * 1000))
            if hasattr(self.page, "wait_for_timeout"):
                self.page.wait_for_timeout(min(poll_ms, remaining_ms))
            else:
                time.sleep(min(poll_ms, remaining_ms) / 1000)

    def _wait_for_blocking_ui(self, timeout: int = 5000) -> Dict[str, Any]:
        started_at = time.monotonic()
        deadline = started_at + max(0.1, min(int(timeout or 5000), 60000) / 1000)
        last_state: Dict[str, Any] = {"found": False}
        polls = 0
        while True:
            polls += 1
            try:
                state = self.page.evaluate(BLOCKING_UI_STATE_SCRIPT)
                last_state = state if isinstance(state, dict) else {"found": False}
            except Exception as exc:
                return {"waited_ms": round((time.monotonic() - started_at) * 1000), "polls": polls, "error": str(exc)[:300]}
            if not last_state.get("found"):
                return {
                    "waited_ms": round((time.monotonic() - started_at) * 1000),
                    "polls": polls,
                    "cleared": True,
                }
            now = time.monotonic()
            if now >= deadline:
                return {
                    "waited_ms": round((now - started_at) * 1000),
                    "polls": polls,
                    "cleared": False,
                    "last_state": last_state,
                }
            self.page.wait_for_timeout(max(1, min(100, round((deadline - now) * 1000))))

    def _position_fallback_target(self, scope, element: Dict[str, Any]):
        """在定位器和语义修复都失败后，校验录制坐标处的元素并返回句柄。"""
        fingerprint = element.get("fingerprint") or {}
        position = element.get("fallback_position") or fingerprint.get("normalized_position") or {}
        try:
            x, y = float(position["x"]), float(position["y"])
        except (KeyError, TypeError, ValueError):
            return None, {"matched": False, "reason": "missing-position"}
        if not (0 <= x <= 1 and 0 <= y <= 1):
            return None, {"matched": False, "reason": "invalid-position", "position": position}

        try:
            handle = scope.evaluate_handle(
                POINT_TARGET_SCRIPT,
                {"x": x, "y": y, "targetTag": fingerprint.get("tag") or element.get("tag") or ""},
            ).as_element()
            if handle is None:
                return None, {"matched": False, "reason": "no-element-at-position", "position": position}
            candidate = handle.evaluate(POINT_CANDIDATE_SCRIPT)
        except Exception as exc:
            return None, {
                "matched": False,
                "reason": "position-probe-error",
                "position": position,
                "error": str(exc)[:300],
            }

        expected_tag = _clean(fingerprint.get("tag") or element.get("tag"))
        actual_tag = _clean(candidate.get("tag"))
        score, reasons = score_web_candidate(fingerprint, candidate)
        expected_position = fingerprint.get("normalized_position") or position
        actual_position = candidate.get("normalized_position") or {}
        try:
            dx = float(expected_position["x"]) - float(actual_position["x"])
            dy = float(expected_position["y"]) - float(actual_position["y"])
            distance = (dx * dx + dy * dy) ** 0.5
        except (KeyError, TypeError, ValueError):
            distance = 1.0

        minimum_score = float(fingerprint.get("position_minimum_score") or 35)
        maximum_distance = float(fingerprint.get("position_maximum_distance") or 0.08)
        diagnostics = {
            "strategy": "position_fallback",
            "matched": False,
            "position": {"x": x, "y": y},
            "score": score,
            "minimum_score": minimum_score,
            "distance": round(distance, 4),
            "maximum_distance": maximum_distance,
            "reasons": reasons,
            "candidate": {
                "tag": actual_tag,
                "role": candidate.get("role"),
                "name": candidate.get("accessible_name"),
                "text": candidate.get("text"),
            },
        }
        if expected_tag and actual_tag != expected_tag:
            diagnostics["reason"] = "tag-mismatch"
            return None, diagnostics
        if distance > maximum_distance:
            diagnostics["reason"] = "position-drift-too-large"
            return None, diagnostics
        if score < minimum_score:
            diagnostics["reason"] = "score-below-threshold"
            return None, diagnostics
        diagnostics["matched"] = True
        return handle, diagnostics

    def locate(
        self,
        element: Dict[str, Any],
        timeout: Optional[int] = None,
        *,
        allow_healing: bool = True,
    ):
        timeout = int(timeout or self.default_timeout)
        started_at = time.monotonic()
        deadline = started_at + max(0.1, timeout / 1000)
        scope, frame_path = self._frame_for_element(element)
        prepared = []
        attempts = []
        for candidate in element.get("locators") or []:
            attempt = {
                "strategy": candidate.get("strategy"),
                "value": candidate.get("value"),
                "matched": False,
            }
            attempts.append(attempt)
            try:
                locator = self._candidate_locator(scope, candidate)
                if locator is not None:
                    prepared.append((candidate, locator, attempt))
            except Exception as exc:
                attempt["error"] = str(exc)[:300]

        fingerprint = element.get("fingerprint") or {}
        healing_attempt = None
        poll_count = 0
        next_heal_at = started_at + min(0.25, max(0.05, timeout / 4000))
        while True:
            poll_count += 1
            for candidate, locator, attempt in prepared:
                try:
                    count = locator.count()
                    attempt["count"] = count
                    if count <= 0:
                        attempt["reason"] = "not-found-yet"
                        continue
                    visible_candidates = []
                    for index in range(min(count, 25)):
                        target = locator.nth(index)
                        if not target.is_visible():
                            continue
                        valid_candidate, validation = self._validate_direct_locator_candidate(
                            target,
                            candidate,
                            fingerprint,
                            force_semantic=count > 1,
                        )
                        visible_candidates.append({
                            "index": index,
                            "target": target,
                            "valid": valid_candidate,
                            "validation": validation,
                            "score": float(validation.get("score") or 0),
                        })
                    if not visible_candidates:
                        attempt["reason"] = "matched-but-not-visible"
                        continue
                    valid_candidates = [item for item in visible_candidates if item["valid"]]
                    if not valid_candidates:
                        validation = visible_candidates[0]["validation"]
                        if count > 1:
                            validation = {
                                **validation,
                                "candidate_count": count,
                                "visible_candidate_count": len(visible_candidates),
                                "alternatives": [
                                    {
                                        "index": item["index"],
                                        "matched": item["valid"],
                                        "score": item["score"],
                                        "actual": item["validation"].get("actual"),
                                    }
                                    for item in visible_candidates[:8]
                                ],
                            }
                        attempt["candidate_validation"] = validation
                        attempt["reason"] = validation.get("reason") or "semantic-mismatch"
                        attempt["matched"] = False
                        continue
                    selected = max(valid_candidates, key=lambda item: (item["score"], -item["index"]))
                    visible_target = selected["target"]
                    validation = selected["validation"]
                    if count > 1:
                        validation = {
                            **validation,
                            "candidate_count": count,
                            "visible_candidate_count": len(visible_candidates),
                            "selected_index": selected["index"],
                            "alternatives": [
                                {
                                    "index": item["index"],
                                    "matched": item["valid"],
                                    "score": item["score"],
                                    "actual": item["validation"].get("actual"),
                                }
                                for item in sorted(
                                    visible_candidates,
                                    key=lambda item: (item["score"], -item["index"]),
                                    reverse=True,
                                )[:8]
                            ],
                        }
                    attempt["candidate_validation"] = validation
                    attempt.pop("reason", None)
                    attempt["matched"] = True
                    elapsed_ms = round((time.monotonic() - started_at) * 1000)
                    self.last_locator_diagnostics = {
                        "matched": True,
                        "selected_strategy": candidate.get("strategy"),
                        "frame_path": frame_path,
                        "poll_count": poll_count,
                        "elapsed_ms": elapsed_ms,
                        "attempts": attempts + ([healing_attempt] if healing_attempt else []),
                    }
                    return visible_target
                except Exception as exc:
                    attempt["error"] = str(exc)[:300]

            now = time.monotonic()
            if (
                allow_healing
                and self.self_heal_enabled
                and fingerprint
                and (now >= next_heal_at or now >= deadline)
            ):
                try:
                    healed, healing_attempt = self._heal_locator(scope, fingerprint)
                    if healed is not None and healed.is_visible():
                        elapsed_ms = round((time.monotonic() - started_at) * 1000)
                        repair = self._build_self_heal_repair(element, attempts, healing_attempt)
                        if repair.get("should_apply"):
                            repair = self._persist_self_heal_repair(element, repair)
                        self.last_locator_diagnostics = {
                            "matched": True,
                            "selected_strategy": "semantic_healing",
                            "frame_path": frame_path,
                            "poll_count": poll_count,
                            "elapsed_ms": elapsed_ms,
                            "attempts": attempts + [healing_attempt],
                            "self_heal_repair": repair,
                        }
                        if repair:
                            self._runtime_log(
                                "AI 自愈修复: "
                                f"{repair.get('old_locator') or '-'} -> {repair.get('new_locator') or '-'} | "
                                f"原因={repair.get('reason')} | 可信度={repair.get('confidence_percent')}% | "
                                f"应用={'是' if repair.get('applied') else '否'}"
                            )
                        return healed
                except Exception as exc:
                    healing_attempt = {
                        "strategy": "semantic_healing",
                        "matched": False,
                        "reason": "healing-error",
                        "error": str(exc)[:300],
                    }
                next_heal_at = now + 0.5

            now = time.monotonic()
            if now >= deadline:
                break
            self.page.wait_for_timeout(max(1, min(100, round((deadline - now) * 1000))))

        elapsed_ms = round((time.monotonic() - started_at) * 1000)
        self.last_locator_diagnostics = {
            "matched": False,
            "frame_path": frame_path,
            "poll_count": poll_count,
            "elapsed_ms": elapsed_ms,
            "attempts": attempts + ([healing_attempt] if healing_attempt else []),
        }
        _locate_url = ""
        try:
            _locate_url = str(getattr(self.page, "url", "") or "")
        except Exception:
            pass
        raise AssertionError(
            f"元素定位失败: {element.get('name') or fingerprint.get('accessible_name') or '未命名元素'}"
            + (f" | 当前页面: {_locate_url}" if _locate_url else "")
        )

    def _read_bound_element_text(self, element: Dict[str, Any], timeout: int = 500) -> str:
        """读取原始定位器命中的文本，仅用于断言失败诊断。

        ``locate`` 会拒绝语义已变化的元素，避免把旧定位器误当成新元素继续操作。
        断言回退到页面文本/OCR 时，仍需要把这个旧定位器实际命中的内容记录下来，
        方便报告解释为什么绑定元素没有命中。这里不绕过 ``locate`` 执行动作，
        只读取首个可见原始候选的文本。
        """
        try:
            scope, _frame_path = self._frame_for_element(element)
        except Exception:
            return ""
        deadline = time.monotonic() + max(0.05, int(timeout or 500) / 1000)
        for candidate in element.get("locators") or []:
            try:
                locator = self._candidate_locator(scope, candidate)
                if locator is None:
                    continue
                count = locator.count()
                for index in range(min(count, 25)):
                    target = locator.nth(index)
                    if not target.is_visible():
                        continue
                    try:
                        return str(target.inner_text(timeout=max(1, min(200, round((deadline - time.monotonic()) * 1000)))) or "")
                    except Exception:
                        try:
                            return str(target.text_content(timeout=max(1, min(200, round((deadline - time.monotonic()) * 1000)))) or "")
                        except Exception:
                            continue
            except Exception:
                continue
            if time.monotonic() >= deadline:
                break
        return ""

    def _attach_diagnostics(self, step: Dict[str, Any]):
        if not allure or not self.last_locator_diagnostics:
            return
        allure.attach(
            json.dumps(self.last_locator_diagnostics, ensure_ascii=False, indent=2),
            name=f"定位诊断-{step.get('name') or step.get('action')}",
            attachment_type=allure.attachment_type.JSON,
        )

    def _attach_step_screenshot(self, step: Dict[str, Any]):
        """把成功步骤截图同时写入任务目录和 Allure。"""
        return self._capture_step_screenshot(
            step,
            status="passed",
            attachment_name=f"执行成功-{step.get('name') or step.get('action') or '步骤'}",
            full_page=bool(step.get("screenshot_full_page", False)),
        )

    def _plate_group_items(self, scope, element: Dict[str, Any]):
        candidate = next(
            (
                item for item in element.get("locators") or []
                if str(item.get("strategy") or "").lower() == "group_index"
            ),
            {},
        )
        fingerprint = element.get("fingerprint") or {}
        group_meta = fingerprint.get("group") or {}
        group_text = str(candidate.get("group_text") or group_meta.get("label") or "")
        if not group_text:
            return None
        group = scope.get_by_text(group_text, exact=True).first.locator("xpath=..")
        tag = str(candidate.get("tag") or group_meta.get("item_tag") or element.get("tag") or "li")
        return group.locator(tag)

    def _read_plate_input(self, scope, element: Dict[str, Any]) -> str:
        items = self._plate_group_items(scope, element)
        if items is None:
            return ""
        values = []
        for text in items.all_inner_texts():
            value = _clean(text)
            if value and value not in {"新能源", "_", "·", "•"}:
                values.append(value)
        return "".join(values)

    def _fill_plate_input(self, element: Dict[str, Any], value: Any, timeout: int):
        expected = re.sub(r"[\s·•.-]+", "", str(resolve_variables(value) or "")).upper()
        if not expected:
            raise ValueError("车牌号不能为空")

        scope, _frame_path = self._frame_for_element(element)
        target = self.locate(element, timeout)
        target.click(timeout=timeout, force=True)

        deadline = time.monotonic() + max(0.5, timeout / 1000)
        while time.monotonic() < deadline:
            if scope.evaluate(PLATE_KEYBOARD_VISIBLE_SCRIPT):
                break
            self.page.wait_for_timeout(80)
        else:
            raise AssertionError("车牌号虚拟键盘未弹出")

        for index, char in enumerate(expected):
            if index == 1:
                scope.evaluate(PLATE_SWITCH_KEYBOARD_SCRIPT)
                self.page.wait_for_timeout(150)
            if not scope.evaluate(PLATE_TAP_KEY_SCRIPT, char):
                raise AssertionError(f"车牌号虚拟键盘未找到按键: {char}")
            self.page.wait_for_timeout(120)

        scope.evaluate(PLATE_CONFIRM_SCRIPT)
        self.page.wait_for_timeout(200)
        actual = self._read_plate_input(scope, element)
        plate_diagnostics = {
            "expected": expected,
            "actual": actual,
            "verified": actual == expected,
        }
        self.last_locator_diagnostics["plate_input"] = plate_diagnostics
        if not actual:
            raise AssertionError("车牌号输入后无法读取页面显示值")
        if actual != expected:
            raise AssertionError(f"车牌号输入校验失败: 期望 {expected}，实际 {actual}")

    def _tap_virtual_keyboard_key(self, element: Dict[str, Any], timeout: int):
        target = self.locate(element, timeout)
        result = target.evaluate(VIRTUAL_KEYBOARD_TAP_SCRIPT)
        if not result or not result.get("tapped"):
            raise AssertionError(
                f"虚拟键盘按键触发失败: {element.get('name') or '未命名按键'}"
            )
        self.last_locator_diagnostics["virtual_keyboard_key"] = {
            "key": ((element.get("fingerprint") or {}).get("keyboard") or {}).get("key"),
            **result,
        }

    def _robust_click(self, target, timeout: int):
        """Click with fallbacks for wrapped/mobile-style controls that Playwright cannot action."""
        short_timeout = max(500, min(int(timeout or self.default_timeout), 5000))
        attempts = []
        first_error = None

        try:
            target.click(timeout=short_timeout)
            return
        except Exception as exc:
            first_error = exc
            attempts.append({"method": "playwright", "error": str(exc)[:300]})

        try:
            target.click(timeout=short_timeout, force=True)
            self.last_locator_diagnostics["click_fallback"] = {
                "method": "playwright-force",
                "attempts": attempts,
            }
            return
        except Exception as exc:
            attempts.append({"method": "playwright-force", "error": str(exc)[:300]})

        try:
            box = target.bounding_box(timeout=short_timeout)
            if box:
                self.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                self.last_locator_diagnostics["click_fallback"] = {
                    "method": "mouse-center",
                    "box": box,
                    "attempts": attempts,
                }
                return
            attempts.append({"method": "mouse-center", "error": "bounding-box-not-found"})
        except Exception as exc:
            attempts.append({"method": "mouse-center", "error": str(exc)[:300]})

        try:
            result = target.evaluate(ROBUST_CLICK_SCRIPT, timeout=short_timeout)
            if result and result.get("clicked"):
                self.last_locator_diagnostics["click_fallback"] = {
                    "method": "dom-click",
                    "result": result,
                    "attempts": attempts,
                }
                return
            attempts.append({"method": "dom-click", "error": str(result)[:300]})
        except Exception as exc:
            attempts.append({"method": "dom-click", "error": str(exc)[:300]})

        self.last_locator_diagnostics["click_fallback"] = {
            "method": "failed",
            "attempts": attempts,
        }
        if first_error:
            raise first_error
        raise AssertionError("元素点击失败")

    def _toggle_role_checkbox(self, target, timeout: int) -> bool:
        """Toggle a composite/ARIA checkbox without hitting sibling links.

        Agreement checkboxes are usually ``div[role=checkbox]`` wrapping the
        consent text together with the document links; clicking the element's
        geometric centre lands on a nested ``<a>`` and navigates away instead
        of toggling. Prefer the inner checkbox indicator, otherwise dispatch a
        click on the ``role=checkbox`` node itself — a synthetic click on the
        control toggles it without following nested anchors.
        """
        indicator = target.locator(
            "input[type=checkbox], .van-checkbox__icon, .el-checkbox__inner, "
            ".ant-checkbox-inner, .nut-checkbox__icon, .nut-checkbox__icon-hook, "
            "[class*='checkbox__icon'], [class*='checkbox__inner'], "
            "[class*='checkbox-icon']"
        ).first
        try:
            if indicator.count() > 0:
                self._robust_click(indicator, timeout)
                return True
        except Exception:
            pass
        try:
            target.evaluate(
                "el => { const r = el.matches('[role=checkbox]') ? el : "
                "el.closest('[role=checkbox]'); if (r) r.click(); }"
            )
            return True
        except Exception:
            return False

    def _robust_fill(self, element: Dict[str, Any], value: str, timeout: int):
        """Fill a controlled field and verify the value survives loading-driven DOM replacement."""
        deadline = time.monotonic() + max(0.5, int(timeout or self.default_timeout) / 1000)
        attempts = []
        last_error: Optional[Exception] = None

        while time.monotonic() < deadline:
            remaining_ms = max(200, round((deadline - time.monotonic()) * 1000))
            attempt: Dict[str, Any] = {"attempt": len(attempts) + 1}
            try:
                locator = self.locate(element, timeout=min(remaining_ms, 2500))
                locator.fill(value, timeout=min(remaining_ms, 2500))
                attempt["filled"] = True

                blocking_wait = self._wait_for_blocking_ui(min(remaining_ms, 5000))
                if blocking_wait.get("waited_ms", 0) >= 50 or not blocking_wait.get("cleared", True):
                    attempt["blocking_ui_wait"] = blocking_wait
                self.page.wait_for_timeout(min(150, remaining_ms))

                remaining_ms = max(200, round((deadline - time.monotonic()) * 1000))
                verified = self.locate(element, timeout=min(remaining_ms, 1500))
                actual = str(verified.evaluate(
                    "el => 'value' in el ? String(el.value ?? '') : String(el.textContent ?? '')"
                ))
                attempt["actual"] = actual
                if actual == value:
                    attempt["matched"] = True
                    attempts.append(attempt)
                    self.last_locator_diagnostics["fill_stability"] = {
                        "verified": True,
                        "expected": value,
                        "attempts": attempts,
                    }
                    return
                attempt["matched"] = False
                attempt["reason"] = "value-reset-after-render"
            except Exception as exc:
                last_error = exc
                attempt["error"] = str(exc)[:300]
            attempts.append(attempt)
            if time.monotonic() < deadline:
                self.page.wait_for_timeout(100)

        self.last_locator_diagnostics["fill_stability"] = {
            "verified": False,
            "expected": value,
            "attempts": attempts,
        }
        if last_error and not any(item.get("filled") for item in attempts):
            raise last_error
        actual = next(
            (item.get("actual") for item in reversed(attempts) if "actual" in item),
            "",
        )
        raise AssertionError(f"输入值未稳定保存: 期望 {value!r}，实际 {actual!r}")

    def _visible_error_message(self, expected: str, keywords: Any, timeout: int) -> Dict[str, Any]:
        """Fail on any visible prompt, with keyword matching as a page-text fallback."""
        expected = str(resolve_variables(expected or "") or "").strip()
        if LOADING_STATE_TEXT_PATTERN.fullmatch(expected):
            expected = ""
        keyword_values = keywords if isinstance(keywords, list) else ERROR_ASSERTION_KEYWORDS
        keyword_values = [str(resolve_variables(item) or "").strip() for item in keyword_values]
        keyword_values = [
            item for item in keyword_values
            if item and not LOADING_STATE_TEXT_PATTERN.fullmatch(item)
        ]
        deadline = time.monotonic() + max(0.2, int(timeout or self.default_timeout) / 1000)
        attempts = []

        script = r"""
        ({expected, keywords}) => {
          const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
          const loadingText = text => /^(?:页面|数据)?(?:正在)?加载中?(?:[.…。!！\s]*)$|^(?:请稍候|请稍等|loading)(?:[.…。!！\s]*)$/i.test(clean(text));
          const visible = el => {
            if (!(el instanceof Element)) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
            if (el.getAttribute('aria-hidden') === 'true') return false;
            return rect.width > 0 && rect.height > 0;
          };
          const selectors = [
            '[role="alert"]',
            '[aria-live]',
            '.van-toast',
            '.van-notify',
            '.van-dialog',
            '.van-field__error-message',
            '.el-message',
            '.el-notification',
            '.el-form-item__error',
            '.ant-message',
            '.ant-notification',
            '.ant-form-item-explain-error',
            '.toast',
            '.notification',
            '.message',
            '.error',
            '.error-message',
            '.warning'
          ];
          const seen = new Set();
          const candidates = [];
          const ignoredLoading = [];
          const isLoadingPrompt = (el, text) => {
            const className = String(el.className || '');
            const loadingControl = el.matches(
              '[aria-busy="true"],[role="progressbar"],.van-toast--loading,.van-loading,.el-loading-mask,.ant-spin'
            ) || Boolean(el.querySelector(
              '[aria-busy="true"],[role="progressbar"],.van-loading,.el-loading-spinner,.ant-spin'
            ));
            const loadingClass = /(?:^|[\s_-])loading(?:[\s_-]|$)|(?:^|[\s_-])spinner(?:[\s_-]|$)/i.test(className);
            return loadingText(text) || ((loadingControl || loadingClass) && !clean(text));
          };
          const add = (el, source, prompt = false) => {
            if (!(el instanceof Element) || seen.has(el) || !visible(el)) return;
            seen.add(el);
            const text = clean(el.innerText || el.textContent);
            if (isLoadingPrompt(el, text)) {
              ignoredLoading.push({text, source, class_name: String(el.className || '').slice(0, 160)});
              return;
            }
            if (text) candidates.push({text, source, prompt});
          };
          selectors.forEach(selector => {
            document.querySelectorAll(selector).forEach(el => add(el, selector, true));
          });
          if (expected) {
            const bodyText = clean(document.body ? document.body.innerText : '');
            if (bodyText.includes(expected)) candidates.push({text: expected, source: 'body_text', prompt: false});
          }
          const genericTags = 'div,span,p,li,label,section,main';
          document.querySelectorAll(genericTags).forEach(el => {
            if (!visible(el)) return;
            const text = clean(el.innerText || el.textContent);
            if (!text || text.length > 160) return;
            if (keywords.some(keyword => text.includes(keyword))) add(el, 'keyword_text', false);
          });
          const matched = candidates.find(item => {
            // assert_error 的核心语义是拦截页面提示：toast、dialog、alert、
            // notification 及表单错误提示只要可见就命中，不再依赖具体文案。
            if (item.prompt) return true;
            if (expected) return item.text.includes(expected);
            return keywords.some(keyword => item.text.includes(keyword));
          });
          return {
            matched: Boolean(matched),
            expected,
            keywords,
            message: matched ? matched.text : '',
            source: matched ? matched.source : '',
            candidates: candidates.slice(0, 10),
            ignored_loading: ignoredLoading.slice(0, 10)
          };
        }
        """

        while True:
            result = self.page.evaluate(script, {"expected": expected, "keywords": keyword_values})
            attempts.append(result)
            if result.get("matched"):
                result["attempts"] = attempts[:-1]
                self.last_locator_diagnostics["assert_error"] = result
                detected = str(result.get("message") or expected or "可见错误提示").strip()
                raise AssertionError(f"检测到错误提示: {detected}")
            if time.monotonic() >= deadline:
                break
            self.page.wait_for_timeout(100)

        failure = {
            "matched": False,
            "expected": expected,
            "keywords": keyword_values,
            "attempts": attempts[-5:],
        }
        self.last_locator_diagnostics["assert_error"] = failure
        return failure

    def _ocr_page_images_for_text(self, expected: str, timeout: int) -> Dict[str, Any]:
        """Read text from large rendered document images when no DOM text exists."""
        executable = shutil.which("tesseract")
        diagnostics: Dict[str, Any] = {
            "matched": False,
            "strategy": "page_image_ocr",
            "expected": expected,
            "attempts": [],
        }
        if not executable:
            diagnostics["reason"] = "tesseract-unavailable"
            return diagnostics

        expected_compact = re.sub(r"\s+", "", expected)
        if not expected_compact:
            diagnostics["reason"] = "empty-expected-text"
            return diagnostics

        try:
            images = self.page.locator("img:visible")
            candidates = images.evaluate_all(
                """els => els.map((el, index) => ({
                  index,
                  src: String(el.currentSrc || el.src || ''),
                  className: String(el.className || ''),
                  naturalWidth: Number(el.naturalWidth || 0),
                  naturalHeight: Number(el.naturalHeight || 0),
                })).filter(item =>
                  item.src && (
                    item.className.includes('contractPage') ||
                    item.naturalWidth * item.naturalHeight >= 250000
                  )
                ).sort((a, b) =>
                  (b.className.includes('contractPage') - a.className.includes('contractPage')) ||
                  (b.naturalWidth * b.naturalHeight - a.naturalWidth * a.naturalHeight)
                ).slice(0, 6)"""
            )
        except Exception as exc:
            diagnostics["reason"] = "image-discovery-failed"
            diagnostics["error"] = str(exc)[:300]
            return diagnostics

        deadline = time.monotonic() + max(1.0, timeout / 1000)
        for candidate in candidates:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                diagnostics["reason"] = "ocr-timeout"
                break

            source = str(candidate.get("src") or "")
            attempt = {
                "index": candidate.get("index"),
                "class_name": candidate.get("className") or "",
                "natural_width": candidate.get("naturalWidth") or 0,
                "natural_height": candidate.get("naturalHeight") or 0,
            }
            try:
                if source.startswith("data:image/") and "," in source:
                    header, encoded = source.split(",", 1)
                    if ";base64" not in header:
                        attempt["reason"] = "unsupported-data-url"
                        diagnostics["attempts"].append(attempt)
                        continue
                    image_bytes = b64decode(encoded)
                    attempt["source"] = "data-url"
                else:
                    image_bytes = images.nth(int(candidate["index"])).screenshot(
                        type="png",
                        timeout=max(1000, min(int(remaining * 1000), timeout)),
                    )
                    attempt["source"] = "element-screenshot"

                result = subprocess.run(
                    [executable, "stdin", "stdout", "-l", "chi_sim+eng", "--psm", "6"],
                    input=image_bytes,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=max(1.0, min(remaining, 20.0)),
                    check=False,
                )
                recognized = result.stdout.decode("utf-8", errors="ignore")
                recognized_compact = re.sub(r"\s+", "", recognized)
                attempt.update({
                    "exit_code": result.returncode,
                    "matched": expected_compact in recognized_compact,
                    "recognized_preview": recognized.strip()[:300],
                })
                diagnostics["attempts"].append(attempt)
                if attempt["matched"]:
                    diagnostics["matched"] = True
                    diagnostics["image_index"] = candidate.get("index")
                    return diagnostics
            except subprocess.TimeoutExpired:
                attempt["reason"] = "tesseract-timeout"
                diagnostics["attempts"].append(attempt)
            except Exception as exc:
                attempt["reason"] = "ocr-failed"
                attempt["error"] = str(exc)[:300]
                diagnostics["attempts"].append(attempt)

        if not candidates:
            diagnostics["reason"] = "no-large-visible-images"
        elif "reason" not in diagnostics:
            diagnostics["reason"] = "text-not-recognized"
        return diagnostics

    def execute_step(self, step: Dict[str, Any]):
        raw_step = step
        prepared_wait = None
        if self._prepared_step_wait.get("step_token") == id(raw_step):
            prepared_wait = self._prepared_step_wait.get("result")
            self._prepared_step_wait = {}
        self._begin_http_timing_step()
        step = self._resolve_runtime_step(step)
        action = str(step.get("action") or step.get("type") or "").lower()
        timeout = int(step.get("timeout") or self.default_timeout)
        element = step.get("element") or {}
        smart_wait_before = prepared_wait or self._smart_wait_for_page(step, phase="before")
        self.last_locator_diagnostics = {"smart_wait_before": smart_wait_before}
        if not smart_wait_before.get("ready", True):
            last_blocking = smart_wait_before.get("last_blocking") or {}
            page_state = smart_wait_before.get("page_state") or {}
            blocking_reason = ""
            if last_blocking.get("found"):
                blocking_reason = str(last_blocking.get("text") or "加载提示仍未消失").strip()
            elif int(smart_wait_before.get("pending_requests") or 0) > 0:
                blocking_reason = f"仍有 {smart_wait_before.get('pending_requests')} 个页面请求未完成"
            elif (
                not page_state.get("has_body")
                or str(page_state.get("ready_state") or "") not in {"interactive", "complete"}
            ):
                blocking_reason = f"document.readyState={page_state.get('ready_state') or 'unknown'}"
            if blocking_reason:
                raise AssertionError(
                    f"页面智能等待超时: {blocking_reason}；"
                    f"已等待 {smart_wait_before.get('waited_ms', 0)}ms"
                )

        if action in {"api", "http", "request", "api_request"}:
            api_step = dict(step)
            raw_url = str(api_step.get("url") or api_step.get("value") or "")
            if raw_url:
                api_step["url"] = self._runtime_url(raw_url)
            try:
                result = execute_api_step(api_step, self.runtime_context)
            except Exception as exc:
                failed_result = getattr(exc, "runtime_result", None)
                if isinstance(failed_result, dict):
                    business = failed_result.get("business") if isinstance(failed_result.get("business"), dict) else {}
                    self.last_locator_diagnostics = {
                        "matched": False,
                        "selected_strategy": "api",
                        "status": failed_result.get("status"),
                        "url": failed_result.get("url"),
                        "business_success": business.get("success"),
                        "business_code": business.get("code"),
                        "extracted": failed_result.get("extracted") or {},
                        "scripts": failed_result.get("scripts") or {},
                        "assertions": failed_result.get("assertions") or [],
                    }
                    self._attach_runtime_data(str(step.get("name") or action), failed_result)
                raise
            self.last_locator_diagnostics = {
                "matched": True,
                "selected_strategy": "api",
                "status": result.get("status"),
                "url": result.get("url"),
                "extracted": result.get("extracted") or {},
                "scripts": result.get("scripts") or {},
                "assertions": result.get("assertions") or [],
            }
            self._attach_runtime_data(str(step.get("name") or action), result)
            if self.auto_correlate:
                self._auto_correlate_response(result, step)
        elif action in {"db", "database", "query", "sql"}:
            try:
                result = execute_database_step(step, self.runtime_context, self.flow)
            except Exception as exc:
                failed_result = getattr(exc, "runtime_result", None)
                if isinstance(failed_result, dict):
                    self.last_locator_diagnostics = {
                        "matched": False,
                        "selected_strategy": "database",
                        "driver": failed_result.get("driver"),
                        "row_count": failed_result.get("row_count", 0),
                        "assertions": failed_result.get("assertions") or [],
                    }
                    self._attach_runtime_data(str(step.get("name") or action), failed_result)
                raise
            self.last_locator_diagnostics = {
                "matched": True,
                "selected_strategy": "database",
                "driver": result.get("driver"),
                "row_count": result.get("row_count", 0),
                "assertions": result.get("assertions") or [],
            }
            self._attach_runtime_data(str(step.get("name") or action), result)
        elif action in {"jmeter", "jmeter_script", "jmx"}:
            try:
                result = _run_jmeter_step(step, self.runtime_context)
            except Exception as exc:
                failed_result = getattr(exc, "runtime_result", None)
                if isinstance(failed_result, dict):
                    self.last_locator_diagnostics = {
                        "matched": False,
                        "selected_strategy": "jmeter",
                        "script": failed_result.get("script"),
                        "exit_code": failed_result.get("exit_code"),
                        "duration_ms": failed_result.get("duration_ms"),
                        "stderr": failed_result.get("stderr"),
                    }
                    self._attach_runtime_data(str(step.get("name") or action), failed_result)
                raise
            self.last_locator_diagnostics = {
                "matched": True,
                "selected_strategy": "jmeter",
                "script": result.get("script"),
                "jtl": result.get("jtl"),
                "exit_code": result.get("exit_code"),
                "duration_ms": result.get("duration_ms"),
            }
            self._attach_runtime_data(str(step.get("name") or action), result)
        elif action in PROTOCOL_ACTIONS:
            protocol_driver = driver_for(action)
            try:
                result = protocol_driver.execute(step, self.runtime_context)
            except Exception as exc:
                failed_result = getattr(exc, "runtime_result", None)
                if isinstance(failed_result, dict):
                    self.last_locator_diagnostics = {
                        "matched": False,
                        "selected_strategy": protocol_driver.name,
                        "status": failed_result.get("status"),
                        "url": failed_result.get("url"),
                        "duration_ms": failed_result.get("duration_ms"),
                    }
                    self._attach_runtime_data(str(step.get("name") or action), failed_result)
                raise
            self.last_locator_diagnostics = {
                "matched": True,
                "selected_strategy": protocol_driver.name,
                "status": result.get("status"),
                "url": result.get("url"),
                "duration_ms": result.get("duration_ms"),
            }
            self._attach_runtime_data(str(step.get("name") or action), result)
        elif action in {"extract", "page_extract", "capture"}:
            source = str(step.get("source") or "text").lower()
            if source == "url":
                value = self.page.url
            else:
                locator = self.locate(element, timeout)
                if source in {"value", "input_value"}:
                    value = locator.input_value(timeout=timeout)
                elif source in {"attribute", "attr"}:
                    attribute = str(step.get("attribute") or "value")
                    value = locator.get_attribute(attribute)
                else:
                    value = locator.inner_text(timeout=timeout)
            save_as = str(step.get("save_as") or step.get("saveAs") or step.get("variable") or "").strip()
            if not save_as:
                raise ValueError("页面提取步骤缺少 save_as/variable")
            self.runtime_context.assign(save_as, value, category="page")
            result = {"source": source, "value": value, "variable": save_as}
            self.last_locator_diagnostics = {
                "matched": True,
                "selected_strategy": "page_extract",
                "variable": save_as,
                "source": source,
            }
            self._attach_runtime_data(str(step.get("name") or action), result)
        elif action in {"goto", "open", "navigate"}:
            self.page.goto(
                validate_outbound_url(self._runtime_url(step.get("url") or step.get("value"))),
                wait_until="domcontentloaded",
                timeout=timeout,
            )
        elif action in {"go_back", "back", "history_back"}:
            previous_url = self.page.url
            response = self.page.go_back(wait_until="domcontentloaded", timeout=timeout)
            if response is None and self.page.url == previous_url:
                fallback_url = step.get("to_url") or step.get("fallback_url")
                if fallback_url:
                    self.page.goto(
                        validate_outbound_url(self._runtime_url(fallback_url)),
                        wait_until="domcontentloaded",
                        timeout=timeout,
                    )
                else:
                    raise AssertionError("当前页面没有可返回的上一页")
        elif action in {"go_forward", "forward", "history_forward"}:
            previous_url = self.page.url
            response = self.page.go_forward(wait_until="domcontentloaded", timeout=timeout)
            if response is None and self.page.url == previous_url:
                fallback_url = step.get("to_url") or step.get("fallback_url")
                if fallback_url:
                    self.page.goto(
                        validate_outbound_url(self._runtime_url(fallback_url)),
                        wait_until="domcontentloaded",
                        timeout=timeout,
                    )
                else:
                    raise AssertionError("当前页面没有可前进的下一页")
        elif action in {"reload", "refresh_page"}:
            self.page.reload(wait_until="domcontentloaded", timeout=timeout)
        elif action in {"click", "double_click", "dblclick"}:
            try:
                target = self.locate(element, timeout)
            except AssertionError:
                scope, frame_path = self._frame_for_element(element)
                target, position_diagnostics = self._position_fallback_target(scope, element)
                self.last_locator_diagnostics["position_fallback"] = position_diagnostics
                if target is None:
                    raise
                self.last_locator_diagnostics.update({
                    "matched": True,
                    "selected_strategy": "position_fallback",
                    "frame_path": frame_path,
                })
            control_type = str((element.get("fingerprint") or {}).get("control_type") or "")
            blocking_wait = self._wait_for_blocking_ui(timeout)
            if blocking_wait.get("waited_ms", 0) >= 50 or not blocking_wait.get("cleared", True):
                self.last_locator_diagnostics["blocking_ui_wait"] = blocking_wait
            if action == "click" and control_type == "virtual_keyboard_key":
                result = target.evaluate(VIRTUAL_KEYBOARD_TAP_SCRIPT)
                if not result or not result.get("tapped"):
                    raise AssertionError(
                        f"虚拟键盘按键触发失败: {element.get('name') or '未命名按键'}"
                    )
                self.last_locator_diagnostics["virtual_keyboard_key"] = {
                    "key": ((element.get("fingerprint") or {}).get("keyboard") or {}).get("key"),
                    **result,
                }
            elif action == "click":
                self._robust_click(target, timeout)
            else:
                target.dblclick(timeout=timeout)
        elif action == "drag":
            source_position = step.get("source_position") or {}
            target_position = step.get("target_position") or {}
            if source_position and target_position:
                source_x = float(source_position.get("x") or 0)
                source_y = float(source_position.get("y") or 0)
                target_x = float(target_position.get("x") or 0)
                target_y = float(target_position.get("y") or 0)
                self.page.mouse.move(source_x, source_y)
                self.page.mouse.down()
                self.page.mouse.move(target_x, target_y, steps=12)
                self.page.mouse.up()
                self.last_locator_diagnostics = {
                    "matched": True,
                    "selected_strategy": "coordinate_drag",
                    "source_position": source_position,
                    "target_position": target_position,
                    "attempts": [],
                }
                return
            source = self.locate(element, timeout)
            source_diagnostics = dict(self.last_locator_diagnostics)
            target_element = step.get("target_element") or {}
            if not target_element:
                raise ValueError("拖拽步骤缺少 target_element")
            target = self.locate(target_element, timeout)
            target_diagnostics = dict(self.last_locator_diagnostics)
            source.drag_to(target, timeout=timeout)
            self.last_locator_diagnostics = {
                "matched": True,
                "selected_strategy": "drag",
                "source": source_diagnostics,
                "target": target_diagnostics,
                "attempts": [
                    *[{**item, "target": "source"} for item in source_diagnostics.get("attempts") or []],
                    *[{**item, "target": "destination"} for item in target_diagnostics.get("attempts") or []],
                ],
            }
        elif action in {"fill", "input", "type", "send_keys", "input_text"}:
            control_type = str((element.get("fingerprint") or {}).get("control_type") or "")
            if control_type == "plate_input":
                self._fill_plate_input(element, step.get("value") or "", timeout)
            else:
                tag = str(element.get("tag") or "").lower()
                role = str(element.get("role") or "").lower()
                input_type = str(element.get("input_type") or "").lower()
                fillable_input = tag == "input" and input_type not in {
                    "button", "submit", "reset", "checkbox", "radio", "file",
                }
                fillable = (
                    fillable_input
                    or tag in {"textarea", "select"}
                    or role in {"textbox", "combobox"}
                )
                value = str(resolve_variables(step.get("value") or ""))
                if control_type == "virtual_keyboard_key":
                    locator = self.locate(element, timeout)
                    result = locator.evaluate(VIRTUAL_KEYBOARD_TAP_SCRIPT)
                    if not result or not result.get("tapped"):
                        raise AssertionError(
                            f"虚拟键盘按键触发失败: {element.get('name') or '未命名按键'}"
                        )
                    self.last_locator_diagnostics["legacy_fill_as_click"] = True
                    self.last_locator_diagnostics["virtual_keyboard_key"] = {
                        "key": ((element.get("fingerprint") or {}).get("keyboard") or {}).get("key"),
                        **result,
                    }
                elif tag == "select" or role == "combobox":
                    locator = self.locate(element, timeout)
                    try:
                        locator.select_option(label=value, timeout=timeout)
                    except Exception:
                        locator.select_option(value=value, timeout=timeout)
                elif fillable:
                    self._robust_fill(element, value, timeout)
                else:
                    locator = self.locate(element, timeout)
                    self._robust_click(locator, timeout)
                    self.last_locator_diagnostics["legacy_fill_as_click"] = True
        elif action == "press":
            if element:
                self.locate(element, timeout).press(str(step.get("value") or step.get("key") or "Enter"), timeout=timeout)
            else:
                self.page.keyboard.press(str(step.get("value") or step.get("key") or "Enter"))
        elif action == "switch_tab":
            match_url = self._runtime_url(step.get("match_url")) if step.get("match_url") else ""
            target = None
            deadline = time.monotonic() + min(10.0, timeout / 1000)
            pages = []
            while time.monotonic() < deadline and target is None:
                pages = [item for item in self.page.context.pages if not item.is_closed()]
                if match_url:
                    target = next((item for item in reversed(pages) if match_url in item.url), None)
                if target is None and step.get("index") is not None:
                    index = int(step.get("index"))
                    if 0 <= index < len(pages):
                        target = pages[index]
                if target is None:
                    self.page.wait_for_timeout(100)
            if target is None:
                raise AssertionError("没有可切换的浏览器标签页")
            self.page = target
            self.page.bring_to_front()
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=timeout)
            except Exception:
                pass
        elif action == "close_tab":
            current = self.page
            pages = [item for item in current.context.pages if not item.is_closed() and item != current]
            current.close()
            if not pages:
                raise AssertionError("关闭标签页后没有剩余页面")
            self.page = pages[-1]
            self.page.bring_to_front()
        elif action in {"popup_select_text", "popup_pick", "popup_select"}:
            target_text = step.get("option_label") or step.get("value") or step.get("text") or ""
            result = select_popup_text(self.page, target_text, timeout_ms=timeout)
            self.last_locator_diagnostics = {
                "matched": True,
                "selected_strategy": "popup_text",
                "target_text": str(resolve_variables(target_text) or ""),
                "result": result,
                "attempts": [{
                    "strategy": "popup_text",
                    "value": str(resolve_variables(target_text) or ""),
                    "matched": True,
                }],
            }
        elif action == "select":
            option_args = {}
            if step.get("option_value") is not None:
                option_args["value"] = step.get("option_value")
            elif step.get("option_label") is not None:
                option_args["label"] = step.get("option_label")
            elif step.get("option_index") is not None:
                option_args["index"] = step.get("option_index")
            else:
                option_args["label"] = str(resolve_variables(step.get("value") or ""))
            target = self.locate(element, timeout)
            is_native_select = bool(target.evaluate(
                "el => el instanceof HTMLSelectElement || String(el.tagName || '').toLowerCase() === 'select'"
            ))
            if is_native_select:
                target.select_option(timeout=timeout, **option_args)
            else:
                self._robust_click(target, timeout)
                option_text = (
                    step.get("option_label")
                    or step.get("value")
                    or step.get("text")
                    or ""
                )
                if option_text and hasattr(self.page, "evaluate"):
                    try:
                        result = select_popup_text(self.page, option_text, timeout_ms=min(timeout, 3000))
                        selected_strategy = "popup_text"
                    except Exception:
                        result = click_visible_text_option(self.page, option_text)
                        selected_strategy = "visible_text"
                    self.last_locator_diagnostics = {
                        "matched": True,
                        "selected_strategy": selected_strategy,
                        "target_text": str(resolve_variables(option_text) or ""),
                        "result": result,
                    }
        elif action == "check":
            target = self.locate(element, timeout)
            if target.evaluate("el => el.matches('input[type=checkbox]')"):
                try:
                    target.check(timeout=timeout)
                except Exception:
                    if not target.evaluate("el => Boolean(el.checked)"):
                        self._robust_click(target, timeout)
            else:
                checked = target.evaluate(
                    """el => {
                      const control = el.querySelector('input[type=checkbox]');
                      if (control) return Boolean(control.checked);
                      const roleTarget = el.matches('[role=checkbox]') ? el : el.closest('[role=checkbox]');
                      if (!roleTarget) return null;
                      const aria = roleTarget.getAttribute('aria-checked');
                      return aria === 'true' ? true : aria === 'false' ? false : null;
                    }"""
                )
                if checked is not True:
                    if not self._toggle_role_checkbox(target, timeout):
                        self._robust_click(target, timeout)
        elif action == "uncheck":
            target = self.locate(element, timeout)
            if target.evaluate("el => el.matches('input[type=checkbox]')"):
                try:
                    target.uncheck(timeout=timeout)
                except Exception:
                    if target.evaluate("el => Boolean(el.checked)"):
                        self._robust_click(target, timeout)
            else:
                checked = target.evaluate(
                    """el => {
                      const control = el.querySelector('input[type=checkbox]');
                      if (control) return Boolean(control.checked);
                      const roleTarget = el.matches('[role=checkbox]') ? el : el.closest('[role=checkbox]');
                      if (!roleTarget) return null;
                      const aria = roleTarget.getAttribute('aria-checked');
                      return aria === 'true' ? true : aria === 'false' ? false : null;
                    }"""
                )
                if checked is not False:
                    if not self._toggle_role_checkbox(target, timeout):
                        self._robust_click(target, timeout)
        elif action == "hover":
            self.locate(element, timeout).hover(timeout=timeout)
        elif action == "upload":
            target = self.locate(element, timeout)
            is_file_input = bool(target.evaluate(
                "el => el instanceof HTMLInputElement && String(el.type || '').toLowerCase() === 'file'"
            ))
            if not is_file_input:
                try:
                    actual = target.evaluate(POINT_CANDIDATE_SCRIPT)
                except Exception:
                    actual = {}
                self.last_locator_diagnostics["upload_target_validation"] = {
                    "matched": False,
                    "reason": "not-file-input",
                    "actual": actual,
                }
                raise AssertionError("上传步骤定位到的不是 input[type=file]，已阻止误传到当前页面的普通输入框")
            # 1) 先展开变量（${...}），再做容器感知的路径映射与存在性校验
            raw_files_value = resolve_variables(step.get("files") or step.get("value"))
            resolved_paths, upload_diag = _resolve_upload_file_paths(raw_files_value)
            self.last_locator_diagnostics["upload_target_validation"] = {
                "matched": True,
                "type": "file",
            }
            self.last_locator_diagnostics["upload_files"] = upload_diag
            if upload_diag["missing_count"]:
                missing_items = upload_diag["missing"]
                # 每个缺失文件列出具体的推荐容器路径，便于用户"复制-粘贴"直接修改
                per_file_lines = [
                    f"  • [{m['basename']}] 原路径 {m['original']} 在容器内不存在；"
                    f"请把同名文件放入仓库 auto-test/uploads/ 目录，步骤路径填写："
                    f"/app/uploads/{m['basename']}"
                    for m in missing_items[:5]
                ]
                if len(missing_items) > 5:
                    per_file_lines.append(f"  …还有 {len(missing_items) - 5} 个文件未列出，处理方式相同")
                remediation = (
                    "上传步骤共找不到 %d 个文件（运行在容器 runnergo-auto-test-1 内，"
                    "宿主 Downloads /Users/* /home/* 等路径不可直接访问）。处理方法：%s%s"
                ) % (
                    upload_diag["missing_count"],
                    "\n" + "\n".join(per_file_lines),
                    "\n也可在步骤中直接写容器绝对路径 /app/uploads/<文件名>，或本地调试填写宿主机可访问路径。",
                )
                raise AssertionError(remediation)
            if not resolved_paths:
                raise AssertionError(
                    "上传步骤未配置任何文件路径：请在步骤参数中填写 files，"
                    "例如 /app/uploads/身份证正面.png，多个路径用逗号分隔。"
                )
            # Playwright set_input_files 要求传入 list[str] | str；单/多文件都兼容 list
            target.set_input_files(resolved_paths, timeout=timeout)
        elif action == "scroll":
            delta_x = int(step.get("delta_x") or 0)
            raw_delta_y = step.get("delta_y")
            delta_y = int(raw_delta_y if raw_delta_y is not None else (step.get("value") or 500))
            if not delta_x and not delta_y:
                delta_y = 500
            perform_precise_scroll(
                self.page,
                x=step.get("x"),
                y=step.get("y"),
                delta_x=delta_x,
                delta_y=delta_y,
                popup_only=str(step.get("scroll_scope") or "").lower() == "popup" or bool(step.get("popup_only")),
            )
        elif action == "wait":
            time.sleep(max(0.0, float(step.get("seconds") or step.get("value") or 1)))
        elif action == "assert_visible":
            assert self.locate(element, timeout).is_visible(), step.get("message") or "元素应可见"
        elif action == "assert_text":
            expected_raw = step.get("expected", step.get("value") or "")
            expected = self.runtime_context.resolve(expected_raw)
            operator = str(step.get("operator") or "contains").strip().lower()
            data_assertion = "operator" in step or "expected" in step
            if element:
                blocking_wait = self._wait_for_blocking_ui(timeout)
                actual = ""
                element_error = ""
                try:
                    element_timeout = min(timeout, 2000)
                    actual = self.locate(element, element_timeout).inner_text(
                        timeout=element_timeout
                    )
                except Exception as exc:
                    element_error = str(exc)[:300]
                    if not actual:
                        actual = self._read_bound_element_text(element, timeout=min(timeout, 500))
                if data_assertion:
                    normalized_actual = re.sub(r"\s+", " ", str(actual)).strip()
                    normalized_expected = (
                        re.sub(r"\s+", " ", str(expected)).strip()
                        if isinstance(expected, str) else expected
                    )
                    assert_runtime_value(
                        normalized_actual,
                        normalized_expected,
                        operator=operator,
                        label="页面文本",
                    )
                    self.last_locator_diagnostics["page_data_assertion"] = {
                        "matched": True,
                        "operator": operator,
                        "expected": normalized_expected,
                        "actual": normalized_actual,
                        "source": expected_raw,
                    }
                elif expected not in actual:
                    try:
                        self.page.get_by_text(expected, exact=False).first.wait_for(
                            state="visible",
                            timeout=min(timeout, 3000),
                        )
                        self.last_locator_diagnostics["assert_text_fallback"] = {
                            "matched": True,
                            "strategy": "page_text",
                            "expected": expected,
                            "bound_element_text": actual,
                            "bound_element_error": element_error,
                            "blocking_ui_wait": blocking_wait,
                        }
                    except Exception:
                        image_ocr = self._ocr_page_images_for_text(expected, timeout)
                        self.last_locator_diagnostics["assert_text_image_ocr"] = image_ocr
                        if image_ocr.get("matched"):
                            self.last_locator_diagnostics["assert_text_fallback"] = {
                                "matched": True,
                                "strategy": "page_image_ocr",
                                "expected": expected,
                                "bound_element_text": actual,
                                "bound_element_error": element_error,
                                "blocking_ui_wait": blocking_wait,
                            }
                        else:
                            detail = element_error or f"实际 {actual!r}"
                            raise AssertionError(
                                step.get("message") or f"期望文本 {expected!r}，{detail}"
                            )
            else:
                if data_assertion:
                    actual = self.page.locator("body").inner_text(timeout=timeout)
                    normalized_actual = re.sub(r"\s+", " ", str(actual)).strip()
                    normalized_expected = (
                        re.sub(r"\s+", " ", str(expected)).strip()
                        if isinstance(expected, str) else expected
                    )
                    assert_runtime_value(
                        normalized_actual,
                        normalized_expected,
                        operator=operator,
                        label="页面内容",
                    )
                    self.last_locator_diagnostics["page_data_assertion"] = {
                        "matched": True,
                        "operator": operator,
                        "expected": normalized_expected,
                        "actual": normalized_actual,
                        "source": expected_raw,
                    }
                else:
                    self.page.get_by_text(expected).first.wait_for(state="visible", timeout=timeout)
        elif action == "assert_url":
            expected = str(resolve_variables(step.get("value") or step.get("url") or ""))
            assert expected in self.page.url, step.get("message") or f"URL 应包含 {expected!r}，实际 {self.page.url!r}"
        elif action == "assert_error":
            self._visible_error_message(
                str(step.get("value") or step.get("expected") or ""),
                step.get("keywords"),
                timeout,
            )
        elif action == "screenshot":
            self._capture_step_screenshot(
                step,
                status="passed",
                attachment_name=step.get("name") or "步骤截图",
                full_page=bool(step.get("full_page", False)),
            )
        else:
            raise ValueError(f"不支持的 Web Flow 动作: {action}")

        smart_wait_after = self._smart_wait_for_page(step, phase="after")
        if step.get("wait_after"):
            time.sleep(max(0.0, float(step["wait_after"])))
        self.last_locator_diagnostics.setdefault("smart_wait_before", smart_wait_before)
        if smart_wait_before.get("saw_blocking_ui"):
            self.last_locator_diagnostics.setdefault("blocking_ui_wait", {
                "cleared": bool(smart_wait_before.get("ready")),
                "waited_ms": smart_wait_before.get("waited_ms", 0),
                "polls": smart_wait_before.get("polls", 0),
                "last_state": smart_wait_before.get("last_blocking") or {},
                "source": "smart_wait_before",
            })
        if smart_wait_after.get("reason") != "read-only-step":
            self.last_locator_diagnostics["smart_wait_after"] = smart_wait_after
        http_timings = self._collect_http_timing_step()
        if http_timings:
            self.last_locator_diagnostics["http_timings"] = http_timings
        self._attach_diagnostics(step)
        if action != "screenshot":
            self._attach_step_screenshot(step)

    def _conditional_skip_target(
        self,
        step: Dict[str, Any],
        steps: list[Dict[str, Any]],
        current_index: int,
    ) -> Optional[Dict[str, Any]]:
        """Resolve an explicit branch that resumes when a future step is already visible."""
        if self.flow.get("strict_full_chain") and not step.get("allow_branch_skip"):
            return None
        config = step.get("skip_to_if_step_visible")
        if not config:
            return None
        previous_diagnostics = self.last_locator_diagnostics
        try:
            candidates = config if isinstance(config, list) else [config]
            for candidate_config in candidates:
                if isinstance(candidate_config, str):
                    target_step_id = candidate_config
                    probe_timeout = 1500
                elif isinstance(candidate_config, dict):
                    target_step_id = str(
                        candidate_config.get("step_id")
                        or candidate_config.get("target_step_id")
                        or ""
                    )
                    probe_timeout = int(candidate_config.get("timeout") or 1500)
                else:
                    continue
                if not target_step_id:
                    continue

                target_index = next(
                    (
                        index
                        for index, candidate in enumerate(steps, start=1)
                        if index > current_index and candidate.get("id") == target_step_id
                    ),
                    0,
                )
                if not target_index:
                    continue
                target_step = steps[target_index - 1]
                target_element = target_step.get("element") or {}
                if not target_element:
                    continue

                try:
                    self.last_locator_diagnostics = {}
                    self.locate(
                        target_element,
                        timeout=max(200, min(5000, probe_timeout)),
                        allow_healing=False,
                    )
                    return {
                        "target_index": target_index,
                        "target_step_id": target_step_id,
                        "target_step_name": (
                            target_step.get("name")
                            or target_step.get("action")
                            or target_step_id
                        ),
                        "probe_diagnostics": dict(self.last_locator_diagnostics),
                    }
                except Exception:
                    continue
            return None
        finally:
            self.last_locator_diagnostics = previous_diagnostics

    def _step_probe_element(self, step: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        action = str(step.get("action") or step.get("type") or "").lower()
        if action in {"goto", "wait", "sleep", "scroll", "screenshot", "reload", "refresh_page"}:
            return None
        element = step.get("element")
        return element if isinstance(element, dict) and element else None

    @staticmethod
    def _repair_retry_allowed(step: Dict[str, Any], action: str) -> bool:
        if step.get("repair_retry_safe") is True:
            return True
        return action in {
            "goto", "open", "navigate", "wait", "sleep",
            "extract", "page_extract", "capture",
        }

    def _probe_step_visible(self, step: Dict[str, Any], timeout_ms: int = 400) -> Dict[str, Any]:
        """Probe whether a recorded step belongs to the currently rendered page."""
        element = self._step_probe_element(step)
        if not element:
            return {"matched": False, "reason": "no-probe-element"}
        previous_diagnostics = self.last_locator_diagnostics
        try:
            self.last_locator_diagnostics = {}
            self.locate(
                element,
                timeout=max(150, min(1200, int(timeout_ms or 400))),
                allow_healing=False,
            )
            return {
                "matched": True,
                "probe": dict(self.last_locator_diagnostics),
            }
        except Exception as exc:
            return {
                "matched": False,
                "reason": "step-not-visible",
                "error": str(exc)[:300],
                "probe": dict(self.last_locator_diagnostics),
            }
        finally:
            self.last_locator_diagnostics = previous_diagnostics

    def _auto_skip_page_signature(self) -> str:
        try:
            return str(self.page.evaluate(
                """() => {
                  const body = document.body;
                  const text = body ? String(body.innerText || '') : '';
                  const stride = Math.max(1, Math.floor(text.length / 500));
                  let hash = 0;
                  for (let i = 0; i < text.length; i += stride) {
                    hash = ((hash * 31) + text.charCodeAt(i)) | 0;
                  }
                  const elementCount = body ? body.querySelectorAll('*').length : 0;
                  return [
                    location.href,
                    document.readyState,
                    text.length,
                    hash,
                    elementCount
                  ].join('|');
                }"""
            ))
        except Exception:
            try:
                return str(self.page.url)
            except Exception:
                return ""

    def _cached_auto_skip_probe(
        self,
        step: Dict[str, Any],
        step_index: int,
        page_signature: str,
    ) -> Dict[str, Any]:
        cache_key = (page_signature, step_index)
        cached = self._auto_skip_probe_cache.get(cache_key)
        if cached is not None:
            result = copy.deepcopy(cached)
            result["cached"] = True
            return result

        result = self._probe_step_visible(
            step,
            timeout_ms=self.auto_page_skip_probe_timeout_ms,
        )
        if len(self._auto_skip_probe_cache) > 1000:
            self._auto_skip_probe_cache.clear()
        self._auto_skip_probe_cache[cache_key] = copy.deepcopy(result)
        return result

    def _auto_skip_target(
        self,
        step: Dict[str, Any],
        steps: list[Dict[str, Any]],
        current_index: int,
    ) -> Optional[Dict[str, Any]]:
        if step.get("disable_auto_page_skip") or self.flow.get("disable_auto_page_skip"):
            return None
        if not self._step_probe_element(step):
            return None

        page_signature = self._auto_skip_page_signature()
        current_probe = self._cached_auto_skip_probe(step, current_index, page_signature)
        if current_probe.get("matched"):
            return None

        end_index = min(len(steps), current_index + self.auto_page_skip_scan_limit)
        future_probes = []
        for target_index in range(current_index + 1, end_index + 1):
            target_step = steps[target_index - 1]
            if not self._step_probe_element(target_step):
                continue
            probe = self._cached_auto_skip_probe(target_step, target_index, page_signature)
            future_probes.append({
                "step_id": target_step.get("id") or "",
                "step_name": target_step.get("name") or target_step.get("action") or "",
                "matched": bool(probe.get("matched")),
                "reason": probe.get("reason") or "",
                "cached": bool(probe.get("cached")),
            })
            if probe.get("matched"):
                return {
                    "target_index": target_index,
                    "target_step_id": target_step.get("id") or "",
                    "target_step_name": target_step.get("name") or target_step.get("action") or "",
                    "current_probe": current_probe,
                    "target_probe": probe,
                    "future_probes": future_probes[-10:],
                }
        next_step = steps[current_index] if current_index < len(steps) else {}
        return {
            "target_index": current_index + 1,
            "target_step_id": next_step.get("id") or "",
            "target_step_name": next_step.get("name") or next_step.get("action") or "下一步",
            "current_probe": current_probe,
            "target_probe": {"matched": False, "reason": "no-visible-future-step"},
            "future_probes": future_probes[-10:],
            "single_step": True,
        }

    def run(self):
        steps = self.flow.get("steps") or []
        self._initialize_runtime_steps()
        iteration_text = (
            f" [{self.iteration_label}/{self.iteration_count}]"
            if self.iteration_count > 1 else ""
        )
        self._runtime_log(
            f"🎬 场景开始{iteration_text}: "
            f"{self.flow.get('name') or self.case_file or 'UI 场景'}，共 {len(steps)} 步"
        )
        if self.page_aware_execution:
            self._runtime_log("🛡️ 执行策略：当前页面匹配时真实执行，页面不匹配时跳过步骤")
        conditional_skip_until = 0
        conditional_skip_diagnostics: Dict[str, Any] = {}
        soft_assertion_failures: list[Dict[str, Any]] = []
        executed_steps = 0
        passed_steps = 0
        failed_steps = 0
        skipped_steps = 0
        auto_skipped_required: list[Dict[str, Any]] = []
        for index, step in enumerate(steps, start=1):
            next_step = steps[index] if index < len(steps) else None
            # 页面匹配的步骤必须真实执行；不能因为后续 fill 会自动聚焦就
            # 把当前 click 变成缓存/合并步骤。
            coalesced_focus_click = False
            self.current_step_index = index
            self.last_step_screenshot = ""
            title = step.get("name") or f"步骤{index}-{step.get('action') or step.get('type')}"
            action = str(step.get("action") or step.get("type") or "")
            action_key = action.lower()
            step_started_at = self._timestamp()
            step_started_clock = time.monotonic()

            if self.debug_pause_event is not None and self.debug_pause_event.is_set():
                self._runtime_log(f"[步骤 {index}/{len(steps)}] ⏸ 断点暂停: {title}")
                if self.debug_step_event is not None:
                    self.debug_step_event.wait()
                    self.debug_step_event.clear()
                self._runtime_log(f"[步骤 {index}/{len(steps)}] ▶ 断点恢复: {title}")

            if self.page_aware_execution and index <= conditional_skip_until:
                skipped_steps += 1
                timestamp = self._timestamp()
                skip_strategy = str(conditional_skip_diagnostics.get("selected_strategy") or "")
                skip_reason = "自动页面跳过" if skip_strategy == "auto_page_skip" else "条件分支跳过"
                self._update_runtime_step(
                    index,
                    status="skipped",
                    started_at=timestamp,
                    finished_at=timestamp,
                    duration=0,
                    error="",
                    screenshot="",
                    log=f"{skip_reason}，继续到步骤 {conditional_skip_until + 1}",
                    diagnostics=conditional_skip_diagnostics,
                )
                self._runtime_log(
                    f"[步骤 {index}/{len(steps)}] ⏭️ {title} | {skip_reason}"
                )
                continue

            if action_key in ("rtds_put", "rtds_get", "rtds_pop") and self.rtds_store is not None:
                import threading as _th
                rtds_key = str(step.get("key") or step.get("name") or "")
                rtds_var = str(step.get("variable") or step.get("var") or rtds_key)
                step_started = self._timestamp()
                try:
                    if action_key == "rtds_put":
                        rtds_value = self.runtime_context.public().get("local", {}).get(rtds_var, "")
                        if not rtds_value:
                            rtds_value = step.get("value", "")
                        with self.rtds_lock:
                            self.rtds_store[rtds_key] = rtds_value
                        self._runtime_log(f"[步骤 {index}] RTDS put: {rtds_key}={str(rtds_value)[:50]}")
                    elif action_key == "rtds_get":
                        with self.rtds_lock:
                            rtds_value = self.rtds_store.get(rtds_key, step.get("default", ""))
                        self.runtime_context.assign(rtds_var, rtds_value)
                        self._runtime_log(f"[步骤 {index}] RTDS get: {rtds_key}→{rtds_var}={str(rtds_value)[:50]}")
                    elif action_key == "rtds_pop":
                        with self.rtds_lock:
                            rtds_value = self.rtds_store.pop(rtds_key, step.get("default", ""))
                        self.runtime_context.assign(rtds_var, rtds_value)
                        self._runtime_log(f"[步骤 {index}] RTDS pop: {rtds_key}→{rtds_var}={str(rtds_value)[:50]}")
                    self._update_runtime_step(index, status="passed", started_at=step_started, finished_at=self._timestamp(), duration=0, error="", screenshot="", log="RTDS OK")
                    passed_steps += 1
                    executed_steps += 1
                    continue
                except Exception as rtds_exc:
                    self._update_runtime_step(index, status="failed", started_at=step_started, finished_at=self._timestamp(), duration=0, error=str(rtds_exc), screenshot="", log=f"RTDS error: {rtds_exc}")
                    failed_steps += 1
                    executed_steps += 1
                    continue

            self._runtime_log(f"[步骤 {index}/{len(steps)}] ⏳ {title} | 智能等待页面稳定")
            pre_step_wait = self._smart_wait_for_page(step, phase="before")
            self._prepared_step_wait = {
                "step_token": id(step),
                "result": pre_step_wait,
            }
            page_ready_for_match = bool(pre_step_wait.get("ready", True))
            if not page_ready_for_match:
                self._runtime_log(
                    f"[步骤 {index}/{len(steps)}] ⚠️ {title} | "
                    "页面在等待时限内未完全稳定，本步不做自动页面跳过，继续按原步骤执行"
                )

            conditional_target = None
            if self.page_aware_execution and page_ready_for_match:
                conditional_target = self._conditional_skip_target(step, steps, index)
            if conditional_target:
                conditional_skip_until = conditional_target["target_index"] - 1
                conditional_skip_diagnostics = {
                    "matched": True,
                    "selected_strategy": "conditional_branch",
                    "conditional_branch": {
                        "trigger_step_id": step.get("id") or "",
                        "resume_step_id": conditional_target["target_step_id"],
                        "resume_step_name": conditional_target["target_step_name"],
                        "skipped_through_index": conditional_skip_until,
                        "probe": conditional_target["probe_diagnostics"],
                        "smart_wait_before": pre_step_wait,
                    },
                }
                timestamp = self._timestamp()
                skipped_steps += 1
                self._update_runtime_step(
                    index,
                    status="skipped",
                    started_at=timestamp,
                    finished_at=timestamp,
                    duration=0,
                    error="",
                    screenshot="",
                    log=(
                        "检测到后续页面状态，条件跳转到 "
                        f"{conditional_target['target_step_name']}"
                    ),
                    diagnostics=conditional_skip_diagnostics,
                )
                self._runtime_log(
                    f"[步骤 {index}/{len(steps)}] ⏭️ {title} | "
                    f"已进入 {conditional_target['target_step_name']}，跳过当前分支"
                )
                continue

            auto_target = None
            if self.page_aware_execution and page_ready_for_match:
                auto_target = self._auto_skip_target(step, steps, index)
            if auto_target:
                conditional_skip_until = auto_target["target_index"] - 1
                conditional_skip_diagnostics = {
                    "matched": True,
                    "selected_strategy": "auto_page_skip",
                    "auto_page_skip": {
                        "trigger_step_id": step.get("id") or "",
                        "resume_step_id": auto_target["target_step_id"],
                        "resume_step_name": auto_target["target_step_name"],
                        "skipped_through_index": conditional_skip_until,
                        "current_probe": auto_target["current_probe"],
                        "target_probe": auto_target["target_probe"],
                        "future_probes": auto_target["future_probes"],
                        "smart_wait_before": pre_step_wait,
                    },
                }
                timestamp = self._timestamp()
                skipped_steps += 1
                _step_optional = (
                    bool(step.get("optional"))
                    or step.get("required") is False
                    or step.get("load_required") is False
                )
                if not _step_optional and not self.flow.get("disable_required_skip_check"):
                    auto_skipped_required.append({
                        "index": index,
                        "name": title,
                        "resume": auto_target.get("target_step_name", ""),
                    })
                self._update_runtime_step(
                    index,
                    status="skipped",
                    started_at=timestamp,
                    finished_at=timestamp,
                    duration=0,
                    error="",
                    screenshot="",
                    log=(
                        "当前页面未命中该步骤，自动跳转到 "
                        f"{auto_target['target_step_name']}"
                    ),
                    diagnostics=conditional_skip_diagnostics,
                )
                self._runtime_log(
                    f"[步骤 {index}/{len(steps)}] ⏭️ {title} | "
                    f"当前页面不匹配，跳到 {auto_target['target_step_name']}"
                )
                continue

            started_at = step_started_at
            started_clock = step_started_clock
            self._update_runtime_step(
                index,
                status="running",
                started_at=started_at,
                finished_at="",
                duration=0,
                error="",
                screenshot="",
                log=f"开始执行: {title}",
                diagnostics={},
            )
            self._runtime_log(f"[步骤 {index}/{len(steps)}] ▶ {title} ({action})")

            def perform_step():
                repair_retry_count = 0
                if self._repair_retry_allowed(step, action_key):
                    repair_retry_count = max(
                        0,
                        min(int(self.repair_strategy.get("retry_count") or 0), 1),
                    )
                repair_attempts = []
                last_error = None
                for attempt in range(repair_retry_count + 1):
                    try:
                        if coalesced_focus_click:
                            self.last_locator_diagnostics = {
                                "matched": True,
                                "selected_strategy": "coalesced_with_next_fill",
                                "attempts": [],
                                "coalesced_focus_click": {
                                    "next_step_id": next_step.get("id") or "",
                                    "next_step_name": (
                                        next_step.get("name")
                                        or next_step.get("action")
                                        or "fill"
                                    ),
                                    "reason": "fill 会自行聚焦同一输入控件",
                                },
                            }
                            self._attach_diagnostics(step)
                            self._attach_step_screenshot(step)
                        else:
                            self.execute_step(step)
                        if repair_attempts:
                            self.last_locator_diagnostics["repair_retry"] = {
                                "attempts": [*repair_attempts, {"attempt": attempt + 1, "status": "passed"}],
                                "policy_id": self.repair_strategy.get("_policy_id"),
                            }
                        return
                    except Exception as exc:
                        last_error = exc
                        repair_attempts.append({
                            "attempt": attempt + 1,
                            "status": "failed",
                            "error": f"{type(exc).__name__}: {exc}"[:1000],
                        })
                        if attempt < repair_retry_count:
                            self._runtime_log(
                                f"[步骤 {index}/{len(steps)}] 🔁 {title} | "
                                "AI 修复辅助执行一次安全重试"
                            )
                            if hasattr(self.page, "wait_for_timeout"):
                                self.page.wait_for_timeout(500)
                            continue
                        self.last_locator_diagnostics["repair_retry"] = {
                            "attempts": repair_attempts,
                            "policy_id": self.repair_strategy.get("_policy_id"),
                        }
                    self.last_locator_diagnostics.setdefault(
                        "smart_wait_before",
                        pre_step_wait,
                    )
                    self._capture_step_screenshot(
                        step,
                        status="failed",
                        attachment_name=f"失败截图-{index}-{title}",
                        full_page=True,
                    )
                    if allure:
                        try:
                            allure.attach(
                                self.page.content(),
                                name=f"失败DOM-{index}",
                                attachment_type=allure.attachment_type.HTML,
                            )
                            self._attach_diagnostics(step)
                        except Exception:
                            pass
                    failure_log = (
                        f"步骤: {index}/{len(steps)} {title}\n"
                        f"动作: {action}\n状态: failed\n"
                        f"开始时间: {started_at}\n错误: {type(last_error).__name__}: {last_error}\n"
                        f"定位诊断: {json.dumps(self.last_locator_diagnostics, ensure_ascii=False, indent=2)}"
                    )
                    self._attach_step_log(title, failure_log)
                    raise last_error

            try:
                if allure:
                    with allure.step(title):
                        perform_step()
                        success_log = (
                            f"步骤: {index}/{len(steps)} {title}\n"
                            f"动作: {action}\n状态: passed\n"
                            f"开始时间: {started_at}\n当前 URL: {getattr(self.page, 'url', '')}\n"
                            f"定位诊断: {json.dumps(self.last_locator_diagnostics, ensure_ascii=False, indent=2)}"
                        )
                        self._attach_step_log(title, success_log)
                else:
                    perform_step()
                duration = round(time.monotonic() - started_clock, 3)
                finished_at = self._timestamp()
                executed_steps += 1
                passed_steps += 1
                diagnostics = copy.deepcopy(self.last_locator_diagnostics or {})
                current_url = str(getattr(self.page, "url", "") or "")
                if current_url.startswith(("http://", "https://")):
                    diagnostics["url"] = current_url
                self.last_locator_diagnostics = diagnostics
                step_log = (
                    f"执行通过 | 动作={action} | 耗时={duration}s | "
                    f"URL={current_url}"
                )
                assertion_matched = None
                if action_key in ASSERTION_ACTIONS:
                    assertion_matched = True
                    if action_key == "assert_error":
                        error_diagnostics = self.last_locator_diagnostics.get("assert_error") or {}
                        matched = error_diagnostics.get("matched")
                        assertion_matched = matched if isinstance(matched, bool) else None
                self._update_runtime_step(
                    index,
                    status="passed",
                    finished_at=finished_at,
                    duration=duration,
                    screenshot=self.last_step_screenshot,
                    error="",
                    log=step_log,
                    diagnostics=self.last_locator_diagnostics,
                    assertion_matched=assertion_matched,
                )
                self._runtime_log(f"[步骤 {index}/{len(steps)}] ✅ {title} | {duration}s")
            except Exception as exc:
                duration = round(time.monotonic() - started_clock, 3)
                finished_at = self._timestamp()
                error = f"{type(exc).__name__}: {exc}"
                assertion_can_continue = (
                    action_key in ASSERTION_ACTIONS
                )
                if assertion_can_continue:
                    executed_steps += 1
                    failed_steps += 1
                    diagnostics = dict(self.last_locator_diagnostics or {})
                    assertion_matched = False
                    assertion_failure_label = "断言未命中"
                    if action_key == "assert_error":
                        error_diagnostics = diagnostics.get("assert_error") or {}
                        matched = error_diagnostics.get("matched")
                        assertion_matched = matched if isinstance(matched, bool) else None
                        assertion_failure_label = "错误断言命中" if assertion_matched is True else "错误断言执行失败"
                    diagnostics["soft_assertion"] = {
                        "matched": assertion_matched,
                        "continued": True,
                        "error": error[:1000],
                    }
                    self.last_locator_diagnostics = diagnostics
                    soft_assertion_failures.append({
                        "index": index,
                        "name": title,
                        "action": action_key,
                        "error": error,
                        "label": assertion_failure_label,
                    })
                    self._update_runtime_step(
                        index,
                        status="failed",
                        finished_at=finished_at,
                        duration=duration,
                        screenshot=self.last_step_screenshot,
                        error=error[:5000],
                        log=f"{assertion_failure_label} | 动作={action} | 耗时={duration}s | 后续步骤继续执行 | {error}",
                        diagnostics=self.last_locator_diagnostics,
                        assertion_matched=assertion_matched,
                    )
                    self._runtime_log(
                        f"[步骤 {index}/{len(steps)}] ❌ {title} | {assertion_failure_label}，继续后续步骤 | {duration}s | {error}"
                    )
                    continue
                self._update_runtime_step(
                    index,
                    status="failed",
                    finished_at=finished_at,
                    duration=duration,
                    screenshot=self.last_step_screenshot,
                    error=error[:5000],
                    log=f"执行失败 | 动作={action} | 耗时={duration}s | {error}",
                    diagnostics=self.last_locator_diagnostics,
                )
                executed_steps += 1
                failed_steps += 1
                self._runtime_log(f"[步骤 {index}/{len(steps)}] ❌ {title} | {duration}s | {error}")
                self._attach_runtime_context()
                self._finalize_runtime_steps("failed", error=error[:5000])
                self._attach_step_summary(
                    total_steps=len(steps),
                    executed_steps=executed_steps,
                    passed_steps=passed_steps,
                    failed_steps=failed_steps,
                    skipped_steps=skipped_steps,
                    status="failed",
                )
                self._attach_step_log("完整场景日志", "\n".join(self.flow_logs))
                raise
        if soft_assertion_failures:
            preview = "；".join(
                f"{item['index']}. {item['name']}（{item['label']}）: {item['error'][:180]}"
                for item in soft_assertion_failures[:5]
            )
            if len(soft_assertion_failures) > 5:
                preview += f"；另有 {len(soft_assertion_failures) - 5} 个断言失败"
            error = f"{len(soft_assertion_failures)} 个断言失败，后续步骤已继续执行。{preview}"
            self._attach_runtime_context()
            self._finalize_runtime_steps("failed", error=error[:5000])
            self._attach_step_summary(
                total_steps=len(steps),
                executed_steps=executed_steps,
                passed_steps=passed_steps,
                failed_steps=failed_steps,
                skipped_steps=skipped_steps,
                status="failed",
            )
            self._runtime_log(
                f"🏁 场景执行完成: 执行 {executed_steps}/{len(steps)} 步，"
                f"通过 {passed_steps}，失败 {failed_steps}，跳过 {skipped_steps}；{error}"
            )
            self._attach_step_log("完整场景日志", "\n".join(self.flow_logs))
            raise AssertionError(error)
        if auto_skipped_required and not self.flow.get("disable_required_skip_check"):
            _skip_preview = "；".join(
                f"{item['index']}. {item['name']}→跳到{item['resume']}"
                for item in auto_skipped_required[:5]
            )
            if len(auto_skipped_required) > 5:
                _skip_preview += f"；另有 {len(auto_skipped_required) - 5} 个必需步骤被跳过"
            _chain_error = (
                f"业务链路不完整：{len(auto_skipped_required)} 个必需步骤因页面未命中被自动跳过。"
                f"{_skip_preview}"
            )
            self._attach_runtime_context()
            self._finalize_runtime_steps("failed", error=_chain_error[:5000])
            self._attach_step_summary(
                total_steps=len(steps),
                executed_steps=executed_steps,
                passed_steps=passed_steps,
                failed_steps=failed_steps,
                skipped_steps=skipped_steps,
                status="failed",
            )
            self._runtime_log(f"🏁 场景执行完成: 链路不完整 | {_chain_error}")
            self._attach_step_log("完整场景日志", "\n".join(self.flow_logs))
            raise AssertionError(_chain_error)
        self._attach_runtime_context()
        self._finalize_runtime_steps("success")
        self._attach_step_summary(
            total_steps=len(steps),
            executed_steps=executed_steps,
            passed_steps=passed_steps,
            failed_steps=failed_steps,
            skipped_steps=skipped_steps,
            status="success",
        )
        self._runtime_log(
            f"🏁 场景执行完成: 执行 {executed_steps}/{len(steps)} 步，"
            f"通过 {passed_steps}，失败 {failed_steps}，跳过 {skipped_steps}"
        )
        self._attach_step_log("完整场景日志", "\n".join(self.flow_logs))


def run_web_flow(browser, flow: Dict[str, Any], http_session=None, debug_pause_event=None, debug_step_event=None, rtds_store=None, rtds_lock=None):
    """按 Flow 中的浏览器配置创建隔离 Context 并执行。"""
    flow = inject_runtime_assertions(flow)
    runtime_data = flow.get("_runtime_data") or []
    if allure:
        allure.attach(
            json.dumps(runtime_data, ensure_ascii=False, indent=2),
            name="实际执行数据",
            attachment_type=allure.attachment_type.JSON,
        )
    profile = flow.get("browser") or {}
    viewport = profile.get("viewport") or {"width": 1440, "height": 900}
    context_options: Dict[str, Any] = {
        **mobile_context_options(viewport),
        "locale": profile.get("locale") or "zh-CN",
    }
    if profile.get("is_mobile") is not None:
        context_options["is_mobile"] = bool(profile.get("is_mobile"))
    if profile.get("has_touch") is not None:
        context_options["has_touch"] = bool(profile.get("has_touch"))
    if profile.get("device_scale_factor"):
        context_options["device_scale_factor"] = float(profile.get("device_scale_factor"))
    if profile.get("user_agent"):
        context_options["user_agent"] = profile["user_agent"]
    if profile.get("timezone_id"):
        context_options["timezone_id"] = profile["timezone_id"]
    context = browser.new_context(**context_options)
    context.route("**/*", guard_playwright_route)
    page = context.new_page()
    runner = WebFlowRunner(page, flow)
    if http_session is not None:
        runner.runtime_context.http_session = http_session
    if debug_pause_event is not None:
        runner.debug_pause_event = debug_pause_event
    if debug_step_event is not None:
        runner.debug_step_event = debug_step_event
    if rtds_store is not None:
        runner.rtds_store = rtds_store
    if rtds_lock is not None:
        runner.rtds_lock = rtds_lock
    try:
        runner.run()
    except Exception as exc:
        try:
            setattr(exc, "flow_runner", runner)
        except Exception:
            pass
        raise
    finally:
        context.close()
    return runner
