"""浏览器 UI 录制、元素探测与 Web Flow 保存 API。"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import queue
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend import db
from backend import runnergo_test_objects
from backend.auth import service as auth_service
from backend.cases import service as case_service
from backend.projects import service as project_service
from backend.url_security import guard_playwright_route, validate_outbound_url
from core.web_flow_runner import click_visible_text_option, mobile_context_options, normalize_scroll_delta, perform_precise_scroll, select_popup_text
from core.mixed_steps import redact


router = APIRouter(prefix="/api/web", tags=["web-automation"])


SUPPORTED_FLOW_ACTIONS = {
    "goto", "open", "navigate",
    "go_back", "back", "history_back",
    "go_forward", "forward", "history_forward",
    "reload", "refresh_page",
    "click", "double_click", "dblclick", "drag",
    "fill", "input", "press", "switch_tab", "close_tab",
    "select", "popup_select_text", "popup_pick", "popup_select",
    "check", "uncheck", "hover", "upload", "scroll", "wait",
    "assert_visible", "assert_text", "assert_url", "assert_error", "screenshot",
    "api", "http", "request", "api_request",
    "db", "database", "query", "sql",
    "extract", "page_extract", "capture",
}


ROBUST_DOM_CLICK_SCRIPT = r"""
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
  return {clicked: true, tag: target.tagName.toLowerCase(), role: target.getAttribute('role') || ''};
}
"""


def _robust_locator_click(locator, page, timeout: int = 5000):
    """Click with fallbacks so small wrapped controls do not fail recording."""
    timeout = max(500, min(int(timeout or 5000), 8000))
    attempts = []
    first_error = None

    try:
        locator.click(timeout=timeout)
        return
    except Exception as exc:
        first_error = exc
        attempts.append(("playwright", str(exc)[:300]))

    try:
        locator.click(timeout=timeout, force=True)
        return
    except Exception as exc:
        attempts.append(("playwright-force", str(exc)[:300]))

    try:
        box = locator.bounding_box(timeout=timeout)
        if box:
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            return
        attempts.append(("mouse-center", "bounding-box-not-found"))
    except Exception as exc:
        attempts.append(("mouse-center", str(exc)[:300]))

    try:
        result = locator.evaluate(ROBUST_DOM_CLICK_SCRIPT)
        if result and result.get("clicked"):
            return
        attempts.append(("dom-click", str(result)[:300]))
    except Exception as exc:
        attempts.append(("dom-click", str(exc)[:300]))

    if first_error:
        raise first_error
    raise RuntimeError(f"元素点击失败: {attempts}")


PROBE_ELEMENT_SCRIPT = r"""
({x, y}) => {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim().slice(0, 200);
  const stable = value => Boolean(value) && value.length <= 100 && !/\d{5,}/.test(value) &&
    !/[a-f0-9]{12,}/i.test(value);
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
  const raw = deepElementFromPoint(Number(x), Number(y));
  if (!raw) return null;
  const visible = node => {
    if (!(node instanceof Element)) return false;
    const style = window.getComputedStyle(node);
    const box = node.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && box.width > 0 && box.height > 0;
  };
  const checkboxTargetAt = node => {
    const semanticSelector = 'input[type="checkbox"],[role="checkbox"]';
    const wrapperSelector = [
      '.van-checkbox', '.el-checkbox', '.ant-checkbox-wrapper', '.ant-checkbox',
      '.MuiCheckbox-root', '[class~="checkbox"]', '[class$="-checkbox"]',
      '[class$="__checkbox"]'
    ].join(',');
    const normalize = candidate => {
      if (!(candidate instanceof Element)) return null;
      if (candidate.matches('input[type="checkbox"]')) {
        if (visible(candidate)) return candidate;
        const roleWrapper = candidate.closest('[role="checkbox"]');
        if (roleWrapper && visible(roleWrapper)) return roleWrapper;
        const label = (candidate.labels && candidate.labels[0]) || candidate.closest('label');
        if (label && visible(label)) return label;
        return null;
      }
      if (candidate.matches('[role="checkbox"]') && visible(candidate)) return candidate;
      if (candidate.matches('label')) {
        const roleControl = candidate.querySelector('[role="checkbox"]');
        if (roleControl && visible(roleControl)) return roleControl;
        const nativeControl = candidate.querySelector('input[type="checkbox"]');
        if (nativeControl && visible(nativeControl)) return nativeControl;
        if (nativeControl && visible(candidate)) return candidate;
      }
      const roleControl = candidate.querySelector('[role="checkbox"]');
      if (roleControl && visible(roleControl)) return roleControl;
      const nativeControl = candidate.querySelector('input[type="checkbox"]');
      if (nativeControl && visible(nativeControl)) return nativeControl;
      if ((nativeControl || candidate.matches(wrapperSelector)) && visible(candidate)) {
        return candidate;
      }
      return null;
    };
    const directSemantic = node.closest(semanticSelector);
    if (directSemantic) {
      const normalized = normalize(directSemantic);
      if (normalized) return normalized;
    }
    const directContainer = node.closest(`label,${wrapperSelector}`);
    if (directContainer && (
      directContainer.matches('[role="checkbox"]') ||
      directContainer.querySelector(semanticSelector) ||
      directContainer.matches(wrapperSelector)
    )) {
      const normalized = normalize(directContainer);
      if (normalized) return normalized;
    }

    // 移动端复选框通常只有 16~24px；允许点击点在控件外沿 20px 内时吸附到复选框。
    const root = node.getRootNode && node.getRootNode();
    const searchRoot = root && root.querySelectorAll ? root : document;
    const candidates = [
      ...Array.from(searchRoot.querySelectorAll(`${semanticSelector},${wrapperSelector}`)),
      ...Array.from(searchRoot.querySelectorAll('label')).filter(label => label.querySelector(semanticSelector))
    ];
    const ranked = [];
    for (const candidate of candidates) {
      const target = normalize(candidate);
      if (!target) continue;
      const box = target.getBoundingClientRect();
      const dx = Number(x) < box.left ? box.left - Number(x) : Number(x) > box.right ? Number(x) - box.right : 0;
      const dy = Number(y) < box.top ? box.top - Number(y) : Number(y) > box.bottom ? Number(y) - box.bottom : 0;
      const distance = Math.hypot(dx, dy);
      if (distance <= 20) {
        const semanticPriority = target.matches('[role="checkbox"],input[type="checkbox"]') ? 0 : 1;
        ranked.push({target, distance, semanticPriority, area: box.width * box.height});
      }
    }
    ranked.sort((left, right) =>
      left.distance - right.distance ||
      left.semanticPriority - right.semanticPriority ||
      left.area - right.area
    );
    return ranked[0]?.target || null;
  };
  const virtualKeyboardKeyAt = node => {
    const keySelector = [
      '.car-keyboard-grids-btn', '[data-key]', '[data-key-value]', '[data-value]',
      'button', '[role="button"]', 'li',
      '[class*="key-item"]', '[class*="key-btn"]', '[class*="key__"]'
    ].join(',');
    const rootSelector = [
      '.car-keyboard', '[class*="keyboard"]', '[class*="key-board"]',
      '[data-keyboard]', '.van-popup', '[role="dialog"]'
    ].join(',');
    const candidate = node.closest(keySelector) || node;
    const root = candidate.parentElement ? candidate.parentElement.closest(rootSelector) : null;
    if (!root || !visible(root) || !visible(candidate)) return null;

    const keyText = clean(
      candidate.getAttribute('data-key') || candidate.getAttribute('data-key-value') ||
      candidate.getAttribute('data-value') || candidate.getAttribute('aria-label') ||
      candidate.innerText || candidate.textContent
    );
    const isCharacter = /^[\u3400-\u9fffA-Za-z0-9]$/.test(keyText);
    const isControl = /^(中\s*\/\s*英|中英|英文|字母|数字|确认|完成|确定|取消|删除|退格|⌫|×)$/.test(keyText);
    if (!isCharacter && !isControl) return null;

    const rootHint = clean(`${root.className || ''} ${root.getAttribute('data-keyboard') || ''}`);
    const keyCandidates = Array.from(root.querySelectorAll(keySelector)).filter(item => {
      if (!visible(item)) return false;
      const value = clean(
        item.getAttribute('data-key') || item.getAttribute('data-key-value') ||
        item.getAttribute('data-value') || item.getAttribute('aria-label') ||
        item.innerText || item.textContent
      );
      return /^[\u3400-\u9fffA-Za-z0-9]$/.test(value);
    });
    const keyboardLike = /keyboard|key-board|car-keyboard/i.test(rootHint) || keyCandidates.length >= 6;
    if (!keyboardLike) return null;

    return {
      element: candidate,
      text: keyText,
      kind: isCharacter ? 'character' : (
        /确认|完成|确定/.test(keyText) ? 'confirm' :
        /删除|退格|⌫|×/.test(keyText) ? 'delete' :
        /取消/.test(keyText) ? 'cancel' : 'switch'
      )
    };
  };
  const keyboardKey = virtualKeyboardKeyAt(raw);
  const checkboxTarget = keyboardKey ? null : checkboxTargetAt(raw);
  const el = keyboardKey?.element || checkboxTarget || raw.closest('button,a,input,textarea,select,label,summary,[role],[contenteditable="true"],[data-testid]') || raw;
  const rect = el.getBoundingClientRect();
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
    if (node.labels && node.labels.length) return clean(Array.from(node.labels).map(item => item.innerText).join(' '));
    const label = node.closest('label');
    return label ? clean(label.innerText) : '';
  };
  const compositeField = node => {
    const parent = node.parentElement;
    if (!parent) return null;
    const peers = Array.from(parent.children).filter(item => item.tagName === node.tagName);
    const itemIndex = peers.indexOf(node);
    if (itemIndex < 0 || peers.length < 2) return null;

    let container = parent;
    for (let depth = 0; container && depth < 4; depth += 1, container = container.parentElement) {
      const candidates = Array.from(container.children || [])
        .filter(item => item !== parent && !item.contains(node))
        .map(item => clean(item.innerText || item.textContent))
        .filter(text => text && text.length <= 40);
      const nested = Array.from(container.querySelectorAll(
        'label,[class*="title"],[class*="label"],[aria-label]'
      )).filter(item => !item.contains(node)).map(item => clean(
        item.getAttribute('aria-label') || item.innerText || item.textContent
      )).filter(text => text && text.length <= 40);
      const label = [...candidates, ...nested].find(text =>
        /车牌|牌照|license\s*plate|plate\s*(number|no)/i.test(text)
      );
      if (label) {
        const match = label.match(/车牌号?|牌照号码?|license\s*plate|plate\s*(?:number|no)/i);
        const groupLabel = clean(match ? match[0] : label);
        return {
          type: 'plate_input',
          label: groupLabel || '车牌号',
          item_index: itemIndex,
          item_count: peers.length,
          item_tag: node.tagName.toLowerCase()
        };
      }
    }
    return null;
  };
  const cssPath = node => {
    if (!(node instanceof Element)) return '';
    if (stable(node.id) && !(node.getRootNode() instanceof ShadowRoot)) return `#${CSS.escape(node.id)}`;
    const parts = [];
    let current = node;
    while (current && current.nodeType === 1 && parts.length < 12) {
      let part = current.tagName.toLowerCase();
      const testid = current.getAttribute('data-testid');
      const name = current.getAttribute('name');
      const root = current.getRootNode();
      const hasStableId = stable(current.id);
      if (hasStableId) part = `#${CSS.escape(current.id)}`;
      if (testid) {
        part += `[data-testid="${CSS.escape(testid)}"]`;
        parts.unshift(part);
        if (root instanceof ShadowRoot) {
          current = root.host;
          continue;
        }
        break;
      }
      if (hasStableId && !(root instanceof ShadowRoot)) {
        parts.unshift(part);
        break;
      }
      if (stable(name)) part += `[name="${CSS.escape(name)}"]`;
      const parent = current.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter(item => item.tagName === current.tagName);
        if (same.length > 1) part += `:nth-of-type(${same.indexOf(current) + 1})`;
      }
      parts.unshift(part);
      current = parent || (root instanceof ShadowRoot ? root.host : null);
    }
    // Playwright CSS 会穿透 open shadow root，使用后代组合器连接宿主和内部节点。
    return parts.join(' ');
  };
  const xpath = node => {
    if (stable(node.id)) return `//*[@id=${JSON.stringify(node.id)}]`;
    const parts = [];
    let current = node;
    while (current && current.nodeType === 1) {
      let index = 1;
      let sibling = current.previousElementSibling;
      while (sibling) {
        if (sibling.tagName === current.tagName) index += 1;
        sibling = sibling.previousElementSibling;
      }
      parts.unshift(`${current.tagName.toLowerCase()}[${index}]`);
      current = current.parentElement;
    }
    return '/' + parts.join('/');
  };
  const unique = selector => {
    try { return document.querySelectorAll(selector).length === 1; } catch (_) { return false; }
  };
  const tag = el.tagName.toLowerCase();
  const text = clean(el.innerText || el.textContent || el.getAttribute('value'));
  const label = labelText(el);
  const role = el.getAttribute('role') || implicitRole(el);
  const accessibleName = clean(el.getAttribute('aria-label') || label || el.getAttribute('alt') ||
    el.getAttribute('title') || el.getAttribute('placeholder') || text);
  const composite = keyboardKey ? null : compositeField(el);
  const checkboxControl = !keyboardKey && (
    Boolean(checkboxTarget) || role === 'checkbox' ||
    (tag === 'input' && String(el.getAttribute('type') || '').toLowerCase() === 'checkbox')
  );
  const displayName = keyboardKey
    ? `车牌键盘按键 ${keyboardKey.text}`
    : composite
      ? `${composite.label}第${composite.item_index + 1}位`
      : checkboxControl
        ? `复选框${accessibleName ? ` ${accessibleName}` : ''}`
        : accessibleName;
  const attrs = {
    testid: el.getAttribute('data-testid') || '',
    id: el.id || '',
    name: el.getAttribute('name') || '',
    placeholder: el.getAttribute('placeholder') || '',
    type: el.getAttribute('type') || ''
  };
  const shadowHosts = [];
  let shadowCursor = el;
  while (shadowCursor) {
    const root = shadowCursor.getRootNode();
    if (!(root instanceof ShadowRoot)) break;
    const host = root.host;
    shadowHosts.unshift({
      tag: host.tagName.toLowerCase(),
      id: host.id || '',
      testid: host.getAttribute('data-testid') || ''
    });
    shadowCursor = host;
  }
  const locators = [];
  const seen = new Set();
  const add = item => {
    const key = JSON.stringify(item);
    if (!seen.has(key)) { seen.add(key); locators.push(item); }
  };
  const rolePeerIndex = currentRole => {
    const selectors = {
      button: 'button,input[type="button"],input[type="submit"],input[type="reset"],[role="button"]',
      link: 'a[href],[role="link"]',
      listitem: 'li,[role="listitem"]',
      textbox: 'input,textarea,[role="textbox"]',
      checkbox: 'input[type="checkbox"],[role="checkbox"]',
      radio: 'input[type="radio"],[role="radio"]',
      combobox: 'select,[role="combobox"]'
    };
    const selector = selectors[currentRole] || `[role="${CSS.escape(currentRole)}"]`;
    const root = el.getRootNode();
    const peers = Array.from(root.querySelectorAll(selector)).filter(node =>
      (node.getAttribute('role') || implicitRole(node)) === currentRole
    );
    return peers.indexOf(el);
  };
  if (attrs.testid) add({strategy: 'testid', value: attrs.testid});
  if (keyboardKey) {
    add({
      strategy: 'keyboard_text',
      value: keyboardKey.text,
      key_kind: keyboardKey.kind
    });
  }
  if (composite) {
    add({
      strategy: 'group_index',
      group_text: composite.label,
      role: role || '',
      tag: composite.item_tag,
      index: composite.item_index,
      value: `${composite.label}第${composite.item_index + 1}位`
    });
  }
  if (role && accessibleName) {
    add({strategy: 'role', role, name: accessibleName, value: accessibleName});
  } else if (role) {
    const index = rolePeerIndex(role);
    if (index >= 0) add({strategy: 'role', role, index, value: `${role}[${index}]`});
  }
  if (label) add({strategy: 'label', value: label});
  if (attrs.placeholder) add({strategy: 'placeholder', value: attrs.placeholder});
  if (stable(attrs.id)) add({strategy: 'id', value: attrs.id});
  if (stable(attrs.name)) add({strategy: 'name', value: attrs.name});
  if (!checkboxControl && text && text.length <= 100) add({strategy: 'text', value: text});
  const css = cssPath(el);
  if (css) add({strategy: 'css', value: css, unique: unique(css)});
  const xp = shadowHosts.length ? '' : xpath(el);
  if (xp) add({strategy: 'xpath', value: xp});
  const viewport = {width: Math.max(1, window.innerWidth), height: Math.max(1, window.innerHeight)};
  const normalizedPosition = {
    x: Math.max(0, Math.min(1, (rect.left + rect.width / 2) / viewport.width)),
    y: Math.max(0, Math.min(1, (rect.top + rect.height / 2) / viewport.height))
  };
  return {
    name: displayName || attrs.name || attrs.id || `${tag}元素`,
    tag,
    role,
    text,
    label,
    accessible_name: accessibleName,
    input_type: attrs.type,
    shadow_hosts: shadowHosts,
    bounds: {x: rect.left, y: rect.top, width: rect.width, height: rect.height},
    fallback_position: normalizedPosition,
    locators,
    fingerprint: {
      version: 1,
      tag,
      role,
      accessible_name: accessibleName,
      text,
      attrs,
      control_type: keyboardKey ? 'virtual_keyboard_key' : (
        composite ? composite.type : (checkboxControl ? 'checkbox' : '')
      ),
      keyboard: keyboardKey ? {key: keyboardKey.text, kind: keyboardKey.kind} : {},
      group: composite || {},
      shadow_hosts: shadowHosts,
      parent: {
        tag: el.parentElement ? el.parentElement.tagName.toLowerCase() : '',
        text: el.parentElement ? clean(el.parentElement.innerText) : ''
      },
      normalized_position: normalizedPosition,
      minimum_score: 55,
      minimum_gap: 8
    },
    point: {x: Number(x), y: Number(y)},
    page_url: location.href
  };
}
"""


FRAME_ELEMENT_SELECTOR_SCRIPT = r"""
(el) => {
  const stable = value => Boolean(value) && value.length <= 100 && !/\d{5,}/.test(value);
  if (stable(el.id)) return `#${CSS.escape(el.id)}`;
  const name = el.getAttribute('name') || '';
  if (stable(name)) return `${el.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
  const parent = el.parentElement;
  if (!parent) return el.tagName.toLowerCase();
  const same = Array.from(parent.children).filter(item => item.tagName === el.tagName);
  return `${el.tagName.toLowerCase()}:nth-of-type(${same.indexOf(el) + 1})`;
}
"""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _validate_url(value: str) -> str:
    return validate_outbound_url(value)


def _element_signature(project_id: str, element: Dict[str, Any]) -> str:
    fingerprint = element.get("fingerprint") or {}
    stable_payload = {
        "project_id": project_id,
        "host": urlsplit(element.get("page_url") or "").netloc,
        "tag": fingerprint.get("tag"),
        "role": fingerprint.get("role"),
        "accessible_name": fingerprint.get("accessible_name"),
        "attrs": fingerprint.get("attrs") or {},
        "control_type": fingerprint.get("control_type"),
        "group": fingerprint.get("group") or {},
        "frame_path": element.get("frame_path") or [],
    }
    return hashlib.sha256(
        json.dumps(stable_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _persist_element(project_id: str, element: Dict[str, Any]) -> Dict[str, Any]:
    signature = _element_signature(project_id, element)
    rows = db.execute(
        "SELECT * FROM web_elements WHERE project_id=? AND signature=? LIMIT 1",
        (project_id, signature), fetch=True,
    )
    locators = json.dumps(element.get("locators") or [], ensure_ascii=False)
    fingerprint = json.dumps(element.get("fingerprint") or {}, ensure_ascii=False)
    element_context = json.dumps({
        "frame_path": element.get("frame_path") or [],
        "shadow_hosts": element.get("shadow_hosts") or [],
        "fallback_position": element.get("fallback_position") or {},
        "label": element.get("label") or "",
        "text": element.get("text") or "",
        "accessible_name": element.get("accessible_name") or "",
        "input_type": element.get("input_type") or "",
    }, ensure_ascii=False)

    def execute_with_context_migration(sql: str, params: tuple):
        try:
            db.execute(sql, params)
        except sqlite3.OperationalError as exc:
            if "element_context" not in str(exc):
                raise
            db.execute("ALTER TABLE web_elements ADD COLUMN element_context TEXT NOT NULL DEFAULT '{}'")
            db.execute(sql, params)

    if rows:
        existing = db.to_dict(rows[0])
        execute_with_context_migration(
            "UPDATE web_elements SET name=?,page_url=?,tag_name=?,element_role=?,"
            "locators=?,fingerprint=?,element_context=?,usage_count=usage_count+1,updated_at=? WHERE id=?",
            (
                existing.get("name"),
                element.get("page_url") or "",
                element.get("tag") or "",
                element.get("role") or "",
                locators, fingerprint, element_context, _now(), existing["id"],
            ),
        )
        return {"id": existing["id"], "name": existing.get("name")}

    element_id = f"wel_{uuid.uuid4().hex[:12]}"
    base_name = re.sub(r"\s+", " ", str(element.get("name") or "Web元素")).strip()[:80]
    name = f"{base_name}_{element_id[-4:]}"
    execute_with_context_migration(
        "INSERT INTO web_elements(id,project_id,name,page_url,tag_name,element_role,signature,"
        "locators,fingerprint,element_context,usage_count,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            element_id, project_id, name, element.get("page_url") or "",
            element.get("tag") or "", element.get("role") or "", signature,
            locators, fingerprint, element_context, 1, _now(), _now(),
        ),
    )
    return {"id": element_id, "name": name}


RECOVERABLE_DRAFT_STATUSES = ("starting", "recording", "stopped", "interrupted", "error")


def _decode_recording_draft(row) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    draft = db.to_dict(row)
    try:
        draft["viewport"] = json.loads(draft.get("viewport") or "{}")
    except (json.JSONDecodeError, TypeError):
        draft["viewport"] = {"width": 1440, "height": 900}
    try:
        draft["steps"] = json.loads(draft.get("steps") or "[]")
    except (json.JSONDecodeError, TypeError):
        draft["steps"] = []
    try:
        draft["storage_state"] = json.loads(draft.get("storage_state") or "{}")
    except (json.JSONDecodeError, TypeError):
        draft["storage_state"] = {}
    draft["step_count"] = len(draft["steps"])
    return draft


def _public_recording_draft(draft: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not draft:
        return None
    result = dict(draft)
    storage_state = result.pop("storage_state", {}) or {}
    result["auth_state_saved"] = bool(
        storage_state.get("cookies") or storage_state.get("origins")
    )
    return result


def _get_recording_draft(draft_id: str) -> Optional[Dict[str, Any]]:
    rows = db.execute(
        "SELECT * FROM web_recording_drafts WHERE id=? LIMIT 1",
        (draft_id,), fetch=True,
    )
    return _decode_recording_draft(rows[0]) if rows else None


def _latest_recording_draft(project_id: str) -> Optional[Dict[str, Any]]:
    placeholders = ",".join("?" for _ in RECOVERABLE_DRAFT_STATUSES)
    rows = db.execute(
        f"SELECT * FROM web_recording_drafts WHERE project_id=? "
        f"AND status IN ({placeholders}) ORDER BY updated_at DESC LIMIT 1",
        (project_id, *RECOVERABLE_DRAFT_STATUSES), fetch=True,
    )
    return _decode_recording_draft(rows[0]) if rows else None


def _upsert_recording_draft(
    draft_id: str,
    project_id: str,
    start_url: str,
    viewport: Dict[str, Any],
    steps: list,
    storage_state: Dict[str, Any],
    status: str,
    last_url: str,
    created_at: str = "",
) -> Dict[str, Any]:
    now = _now()
    db.execute(
        "INSERT INTO web_recording_drafts(id,project_id,start_url,viewport,steps,storage_state,status,last_url,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET project_id=excluded.project_id,start_url=excluded.start_url,"
        "viewport=excluded.viewport,steps=excluded.steps,storage_state=excluded.storage_state,"
        "status=excluded.status,last_url=excluded.last_url,"
        "updated_at=excluded.updated_at",
        (
            draft_id, project_id, start_url,
            json.dumps(viewport or {}, ensure_ascii=False),
            json.dumps(steps or [], ensure_ascii=False),
            json.dumps(storage_state or {}, ensure_ascii=False),
            status, last_url, created_at or now, now,
        ),
    )
    return _get_recording_draft(draft_id) or {}


def _update_recording_draft_steps(draft_id: str, steps: list) -> Dict[str, Any]:
    draft = _get_recording_draft(draft_id)
    if not draft:
        raise KeyError("录制草稿不存在")
    db.execute(
        "UPDATE web_recording_drafts SET steps=?,updated_at=? WHERE id=?",
        (json.dumps(steps or [], ensure_ascii=False), _now(), draft_id),
    )
    return _get_recording_draft(draft_id) or {}


def _delete_recording_draft(draft_id: str) -> bool:
    if not _get_recording_draft(draft_id):
        return False
    db.execute("DELETE FROM web_recording_drafts WHERE id=?", (draft_id,))
    return True


def mark_stale_recording_drafts_interrupted():
    """API 进程启动时，将上次异常退出遗留的活动草稿标记为可恢复。"""
    db.execute(
        "UPDATE web_recording_drafts SET status='interrupted',updated_at=? "
        "WHERE status IN ('starting','recording')",
        (_now(),),
    )


@dataclass
class BrowserRecordingSession:
    session_id: str
    project_id: str
    start_url: str
    viewport: Dict[str, int]
    resume_url: str = ""
    headless: bool = True
    commands: queue.Queue = field(default_factory=queue.Queue)
    ready: threading.Event = field(default_factory=threading.Event)
    error: Optional[BaseException] = None
    status: str = "starting"
    created_at: float = field(default_factory=time.time)
    steps: list = field(default_factory=list)
    record_insert_index: Optional[int] = None
    recording_enabled: bool = True
    storage_state: Dict[str, Any] = field(default_factory=dict)
    navigation_history: list = field(default_factory=list)
    navigation_index: int = -1

    def __post_init__(self):
        self.thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"web-recorder-{self.session_id}",
        )

    def start(self):
        self.thread.start()
        if not self.ready.wait(timeout=45):
            raise TimeoutError("浏览器录制会话启动超时")
        if self.error:
            raise RuntimeError(str(self.error))

    def call(self, command: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 35):
        response_queue: queue.Queue = queue.Queue(maxsize=1)
        self.commands.put((command, payload or {}, response_queue))
        try:
            result = response_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(f"浏览器命令超时: {command}") from exc
        if isinstance(result, BaseException):
            raise result
        return result

    def _reset_navigation_history(self, url: str):
        current_url = str(url or "").strip()
        self.navigation_history = [current_url] if current_url else []
        self.navigation_index = 0 if current_url else -1

    def _track_navigation_url(self, url: str):
        """记录录制期间实际到达的 URL，给 replace/SPA 路由提供可回退历史。"""
        current_url = str(url or "").strip()
        if not current_url:
            return
        if self.navigation_index < 0 or not self.navigation_history:
            self._reset_navigation_history(current_url)
            return
        if self.navigation_history[self.navigation_index] == current_url:
            return
        self.navigation_history = self.navigation_history[:self.navigation_index + 1]
        self.navigation_history.append(current_url)
        self.navigation_index = len(self.navigation_history) - 1

    def _navigation_target(self, direction: int) -> str:
        target_index = self.navigation_index + direction
        if 0 <= target_index < len(self.navigation_history):
            return str(self.navigation_history[target_index] or "")
        return ""

    def _commit_navigation_move(self, url: str, direction: int, expected_url: str = ""):
        current_url = str(url or "").strip()
        target_index = self.navigation_index + direction
        if (
            expected_url
            and current_url == expected_url
            and 0 <= target_index < len(self.navigation_history)
        ):
            self.navigation_index = target_index
            return

        matching_indexes = [
            index for index, item in enumerate(self.navigation_history)
            if item == current_url
        ]
        if direction < 0:
            matching_indexes = [index for index in matching_indexes if index < self.navigation_index]
            if matching_indexes:
                self.navigation_index = max(matching_indexes)
                return
        else:
            matching_indexes = [index for index in matching_indexes if index > self.navigation_index]
            if matching_indexes:
                self.navigation_index = min(matching_indexes)
                return
        self._track_navigation_url(current_url)

    def _persist_draft(self, page=None, status: str = "") -> Dict[str, Any]:
        last_url = self.resume_url or self.start_url
        if page is not None:
            try:
                last_url = page.url or last_url
                self.storage_state = page.context.storage_state()
            except Exception:
                pass
        return _upsert_recording_draft(
            self.session_id,
            self.project_id,
            self.start_url,
            self.viewport,
            list(self.steps),
            self.storage_state,
            status or self.status,
            last_url,
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created_at)),
        )

    def _snapshot(self, page) -> Dict[str, Any]:
        self._track_navigation_url(page.url)
        image = page.screenshot(type="jpeg", quality=72)
        pages = [item for item in page.context.pages if not item.is_closed()]
        captured_at = int(time.time() * 1000)
        return {
            "session_id": self.session_id,
            "status": self.status,
            "url": page.url,
            "title": page.title(),
            "viewport": self.viewport,
            "image": f"data:image/jpeg;base64,{base64.b64encode(image).decode('ascii')}",
            "captured_at": captured_at,
            "steps": list(self.steps),
            "step_count": len(self.steps),
            "auth_state_saved": bool(
                self.storage_state.get("cookies") or self.storage_state.get("origins")
            ),
            "tabs": [
                {"index": index, "url": item.url, "title": item.title(), "active": item == page}
                for index, item in enumerate(pages)
            ],
        }

    def _frame_context_at_point(self, page, x: float, y: float):
        frame = page.main_frame
        local_x, local_y = x, y
        frame_path = []
        for _ in range(8):
            handle = frame.evaluate_handle(
                "point => document.elementFromPoint(point.x, point.y)",
                {"x": local_x, "y": local_y},
            ).as_element()
            if not handle:
                break
            tag_name = str(handle.evaluate("el => el.tagName.toLowerCase()") or "")
            if tag_name not in {"iframe", "frame"}:
                break
            child = handle.content_frame()
            if child is None:
                break
            selector = str(handle.evaluate(FRAME_ELEMENT_SELECTOR_SCRIPT) or "")
            bounds = handle.bounding_box() or {}
            frame_path.append({
                "selector": selector,
                "name": child.name,
                "url": child.url,
            })
            if bounds:
                local_x = x - float(bounds.get("x") or 0)
                local_y = y - float(bounds.get("y") or 0)
            frame = child
        return frame, local_x, local_y, frame_path

    def _probe_context(self, page, payload: Dict[str, Any]):
        x, y = float(payload.get("x") or 0), float(payload.get("y") or 0)
        frame, local_x, local_y, frame_path = self._frame_context_at_point(page, x, y)
        element = frame.evaluate(PROBE_ELEMENT_SCRIPT, {"x": local_x, "y": local_y})
        if not element:
            raise ValueError("点击位置没有可探测元素")
        element["frame_path"] = frame_path
        element["point"] = {"x": x, "y": y}
        element["local_point"] = {"x": local_x, "y": local_y}
        return frame, element

    def _probe(self, page, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._probe_context(page, payload)[1]

    def _embedded_element(self, element: Dict[str, Any]) -> Dict[str, Any]:
        stored = _persist_element(self.project_id, element)
        return {
            "id": stored["id"],
            "name": stored["name"],
            "tag": element.get("tag"),
            "role": element.get("role"),
            "text": element.get("text"),
            "label": element.get("label"),
            "accessible_name": element.get("accessible_name"),
            "input_type": element.get("input_type"),
            "page_url": element.get("page_url"),
            "frame_path": element.get("frame_path") or [],
            "shadow_hosts": element.get("shadow_hosts") or [],
            "locators": element.get("locators") or [],
            "fingerprint": element.get("fingerprint") or {},
            "fallback_position": element.get("fallback_position") or {},
        }

    def _record_element_step(self, action: str, element: Dict[str, Any], **values) -> Dict[str, Any]:
        embedded_element = self._embedded_element(element)
        label = element.get("name") or element.get("accessible_name") or element.get("text") or element.get("tag") or "元素"
        name_prefix = {
            "click": "点击", "double_click": "双击", "fill": "输入", "select": "选择",
            "popup_select_text": "弹框选择",
            "check": "勾选", "uncheck": "取消勾选", "upload": "上传文件",
            "hover": "悬停", "press": "按键", "drag": "拖拽",
        }.get(action, action)
        step = {
            "id": f"step_{uuid.uuid4().hex[:12]}",
            "action": action,
            "name": f"{name_prefix} {str(label)[:60]}",
            "element": embedded_element,
            "timeout": 10000,
        }
        step.update(values)
        return self._store_recorded_step(step)

    def _store_recorded_step(self, step: Dict[str, Any]) -> Dict[str, Any]:
        """保存录制步骤；有插入锚点时，连续新步骤保持在同一位置。"""
        if not self.recording_enabled:
            return step
        if self.record_insert_index is None:
            self.steps.append(step)
            return step
        insert_index = max(0, min(int(self.record_insert_index), len(self.steps)))
        if insert_index == 0 and self.steps and self.steps[0].get("action") == "goto":
            raise ValueError("首个打开页面步骤必须保留在第一位")
        self.steps.insert(insert_index, step)
        self.record_insert_index = insert_index + 1
        return step

    def _insert_manual_step(self, raw_step: Dict[str, Any], index: Optional[int] = None) -> Dict[str, Any]:
        step = dict(raw_step or {})
        if not step.get("action"):
            raise ValueError("组件步骤缺少 action")
        step.setdefault("id", f"step_{uuid.uuid4().hex[:12]}")
        step.setdefault("name", str(step.get("action")))
        if index is None:
            insert_index = len(self.steps)
        else:
            insert_index = int(index)
            if insert_index < 0 or insert_index > len(self.steps):
                raise IndexError("步骤插入位置不存在")
        if (
            insert_index == 0
            and self.steps
            and self.steps[0].get("action") == "goto"
        ):
            raise ValueError("首个打开页面步骤必须保留在第一位")
        self.steps.insert(insert_index, step)
        return step

    def _perform_action(self, page, payload: Dict[str, Any]):
        action = str(payload.get("action") or payload.get("type") or "").lower()
        if action in {"goto", "navigate", "open"}:
            url = _validate_url(payload.get("url") or "")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            step = {
                "id": f"step_{uuid.uuid4().hex[:12]}",
                "action": "goto",
                "name": f"打开 {url}",
                "url": url,
                "timeout": 30000,
            }
            return self._store_recorded_step(step)

        if action in {"go_back", "back", "history_back"}:
            previous_url = page.url
            fallback_url = self._navigation_target(-1)
            response = page.go_back(wait_until="domcontentloaded", timeout=30000)
            current_url = page.url
            if response is None and current_url == previous_url:
                if fallback_url and fallback_url != previous_url:
                    page.goto(fallback_url, wait_until="domcontentloaded", timeout=30000)
                    current_url = page.url
                else:
                    raise ValueError("当前页面没有可返回的上一页")
            self._commit_navigation_move(current_url, -1, fallback_url)
            step = {
                "id": f"step_{uuid.uuid4().hex[:12]}",
                "action": "go_back",
                "name": "浏览器后退",
                "from_url": previous_url,
                "to_url": current_url,
                "timeout": 30000,
            }
            return self._store_recorded_step(step)

        if action in {"go_forward", "forward", "history_forward"}:
            previous_url = page.url
            fallback_url = self._navigation_target(1)
            response = page.go_forward(wait_until="domcontentloaded", timeout=30000)
            current_url = page.url
            if response is None and current_url == previous_url:
                if fallback_url and fallback_url != previous_url:
                    page.goto(fallback_url, wait_until="domcontentloaded", timeout=30000)
                    current_url = page.url
                else:
                    raise ValueError("当前页面没有可前进的下一页")
            self._commit_navigation_move(current_url, 1, fallback_url)
            step = {
                "id": f"step_{uuid.uuid4().hex[:12]}",
                "action": "go_forward",
                "name": "浏览器前进",
                "from_url": previous_url,
                "to_url": current_url,
                "timeout": 30000,
            }
            return self._store_recorded_step(step)

        if action in {"reload", "refresh_page"}:
            current_url = page.url
            page.reload(wait_until="domcontentloaded", timeout=30000)
            step = {
                "id": f"step_{uuid.uuid4().hex[:12]}",
                "action": "reload",
                "name": "刷新页面",
                "url": current_url,
                "timeout": 30000,
            }
            return self._store_recorded_step(step)

        if action in {"popup_select_text", "popup_pick", "popup_select"}:
            target_text = str(payload.get("option_label") or payload.get("value") or payload.get("text") or "").strip()
            if not target_text:
                raise ValueError("请输入要定位的弹框内容")
            result = select_popup_text(page, target_text, timeout_ms=10000)
            if hasattr(page, "wait_for_timeout"):
                page.wait_for_timeout(200)
            step = {
                "id": f"step_{uuid.uuid4().hex[:12]}",
                "action": "popup_select_text",
                "name": f"弹框选择 {target_text[:60]}",
                "value": target_text,
                "option_label": target_text,
                "timeout": 10000,
                "wait_after": 0.2,
            }
            if result.get("x") is not None and result.get("y") is not None:
                step["x"] = float(result.get("x") or 0)
                step["y"] = float(result.get("y") or 0)
                try:
                    _, selected_element = self._probe_context(page, {"x": step["x"], "y": step["y"]})
                    step["element"] = self._embedded_element(selected_element)
                except Exception:
                    pass
            return self._store_recorded_step(step)

        if action == "scroll":
            delta_x = int(payload.get("delta_x") or 0)
            raw_delta_y = payload.get("delta_y")
            delta_y = int(raw_delta_y if raw_delta_y is not None else (payload.get("value") or 500))
            if not delta_x and not delta_y:
                delta_y = 500
            has_point = payload.get("x") is not None and payload.get("y") is not None
            scroll_scope = str(payload.get("scroll_scope") or "").strip().lower()
            popup_only = scroll_scope == "popup" or bool(payload.get("popup_only"))
            delta_x, delta_y = normalize_scroll_delta(delta_x, delta_y, popup_only=popup_only)
            perform_precise_scroll(
                page,
                x=payload.get("x") if has_point else None,
                y=payload.get("y") if has_point else None,
                delta_x=delta_x,
                delta_y=delta_y,
                popup_only=popup_only,
            )
            direction = "向下" if delta_y >= 0 else "向上"
            step = {
                "id": f"step_{uuid.uuid4().hex[:12]}",
                "action": "scroll",
                "name": f"{'弹框' if popup_only else ''}{direction}滚动 {abs(delta_y)}px",
                "delta_x": delta_x,
                "delta_y": delta_y,
            }
            if popup_only:
                step["scroll_scope"] = "popup"
            if has_point:
                step["x"] = float(payload.get("x") or 0)
                step["y"] = float(payload.get("y") or 0)
            return self._store_recorded_step(step)

        if action == "press":
            key = str(payload.get("key") or payload.get("value") or "Enter")
            if payload.get("x") is not None and payload.get("y") is not None:
                frame, element = self._probe_context(page, payload)
                css_candidate = next(
                    (item for item in element.get("locators") or [] if item.get("strategy") == "css"),
                    None,
                )
                if css_candidate:
                    frame.locator(css_candidate["value"]).first.press(key)
                else:
                    page.mouse.click(float(payload.get("x") or 0), float(payload.get("y") or 0))
                    page.keyboard.press(key)
                return self._record_element_step("press", element, value=key, key=key)
            page.keyboard.press(key)
            step = {
                "id": f"step_{uuid.uuid4().hex[:12]}",
                "action": "press",
                "name": f"按键 {key}",
                "value": key,
            }
            return self._store_recorded_step(step)

        frame, element = self._probe_context(page, payload)
        x, y = float(payload.get("x") or 0), float(payload.get("y") or 0)
        css_candidate = next(
            (item for item in element.get("locators") or [] if item.get("strategy") == "css"),
            None,
        )
        if action == "click":
            control_type = str((element.get("fingerprint") or {}).get("control_type") or "")
            if control_type == "virtual_keyboard_key":
                from core.web_flow_runner import WebFlowRunner

                runner = WebFlowRunner(page, {
                    "flow_version": 2,
                    "base_url": self.start_url,
                    "timeout": 10000,
                    "steps": [],
                })
                runner._tap_virtual_keyboard_key(element, 10000)
                page.wait_for_timeout(160)
            elif control_type == "checkbox" and css_candidate:
                # 小复选框允许邻近吸附；实际执行时点击已识别的控件，而不是原始误差坐标。
                _robust_locator_click(frame.locator(css_candidate["value"]).first, page)
                page.wait_for_timeout(200)
            else:
                page.mouse.click(x, y)
                page.wait_for_timeout(350)
            return self._record_element_step("click", element, wait_after=0.2)

        if action in {"double_click", "dblclick"}:
            page.mouse.dblclick(x, y)
            page.wait_for_timeout(350)
            return self._record_element_step("double_click", element, wait_after=0.2)

        if action == "drag":
            source_x = float(payload.get("source_x") if payload.get("source_x") is not None else x)
            source_y = float(payload.get("source_y") if payload.get("source_y") is not None else y)
            _, source_element = self._probe_context(page, {"x": source_x, "y": source_y})
            target_element = element
            page.mouse.move(source_x, source_y)
            page.mouse.down()
            page.mouse.move(x, y, steps=12)
            page.mouse.up()
            page.wait_for_timeout(350)
            return self._record_element_step(
                "drag",
                source_element,
                target_element=self._embedded_element(target_element),
                source_position={"x": source_x, "y": source_y},
                target_position={"x": x, "y": y},
                wait_after=0.2,
            )

        if action in {"fill", "input"}:
            value = str(payload.get("value") or "")
            tag = str(element.get("tag") or "").lower()
            role = str(element.get("role") or "").lower()
            input_type = str(element.get("input_type") or "").lower()
            control_type = str((element.get("fingerprint") or {}).get("control_type") or "")
            fillable_input = tag == "input" and input_type not in {
                "button", "submit", "reset", "checkbox", "radio", "file",
            }
            fillable = (
                control_type == "plate_input"
                or fillable_input
                or tag in {"textarea", "select"}
                or role in {"textbox", "combobox"}
            )
            if control_type == "virtual_keyboard_key":
                from core.web_flow_runner import WebFlowRunner

                runner = WebFlowRunner(page, {
                    "flow_version": 2,
                    "base_url": self.start_url,
                    "timeout": 10000,
                    "steps": [],
                })
                runner._tap_virtual_keyboard_key(element, 10000)
                page.wait_for_timeout(160)
                return self._record_element_step("click", element, wait_after=0.2)
            if not fillable:
                page.mouse.click(x, y)
                page.wait_for_timeout(350)
                return self._record_element_step("click", element, wait_after=0.2)
            if control_type == "plate_input":
                from core.web_flow_runner import WebFlowRunner

                runner = WebFlowRunner(page, {
                    "flow_version": 2,
                    "base_url": self.start_url,
                    "timeout": 10000,
                    "steps": [],
                })
                runner._fill_plate_input(element, value, 10000)
            elif css_candidate and tag in {"input", "textarea"}:
                frame.locator(css_candidate["value"]).first.fill(value)
            elif css_candidate and tag == "select":
                locator = frame.locator(css_candidate["value"]).first
                try:
                    locator.select_option(label=value)
                except Exception:
                    locator.select_option(value=value)
            else:
                page.mouse.click(x, y)
                page.keyboard.press("Control+A")
                page.keyboard.insert_text(value)
            page.wait_for_timeout(200)
            recorded_value = "${PASSWORD}" if str(element.get("input_type") or "").lower() == "password" else value
            return self._record_element_step("fill", element, value=recorded_value)

        if action == "select":
            option_label = str(payload.get("option_label") or payload.get("value") or "")
            option_value = str(payload.get("option_value") or "")
            locator = frame.locator(css_candidate["value"]).first if css_candidate else None
            is_native_select = False
            if locator:
                is_native_select = bool(locator.evaluate(
                    "el => el instanceof HTMLSelectElement || String(el.tagName || '').toLowerCase() === 'select'"
                ))
            if is_native_select:
                if option_value:
                    locator.select_option(value=option_value)
                else:
                    locator.select_option(label=option_label)
                selected = locator.evaluate(
                    "el => ({value: el.value, label: el.selectedOptions && el.selectedOptions[0] ? el.selectedOptions[0].text : ''})"
                )
                return self._record_element_step(
                    "select", element,
                    option_value=selected.get("value") or option_value,
                    option_label=selected.get("label") or option_label,
                )

            if locator:
                _robust_locator_click(locator, page)
            else:
                page.mouse.click(x, y)
            page.wait_for_timeout(350)
            if option_label:
                try:
                    select_popup_text(page, option_label, timeout_ms=3000)
                except Exception:
                    click_visible_text_option(page, option_label)
            step = self._record_element_step("select", element, wait_after=0.2)
            if option_label:
                step["option_label"] = option_label
            if option_value:
                step["option_value"] = option_value
            return step

        if action in {"check", "uncheck"}:
            if not css_candidate:
                raise ValueError("复选框缺少可执行 CSS 定位器")
            locator = frame.locator(css_candidate["value"]).first
            expected_checked = action == "check"
            is_native_checkbox = locator.evaluate(
                "el => el.matches('input[type=checkbox]')"
            )
            if is_native_checkbox:
                try:
                    locator.check(timeout=5000) if expected_checked else locator.uncheck(timeout=5000)
                except Exception:
                    if bool(locator.evaluate("el => Boolean(el.checked)")) != expected_checked:
                        _robust_locator_click(locator, page)
            else:
                checked = locator.evaluate(
                    """el => {
                      const control = el.querySelector('input[type=checkbox]');
                      if (control) return Boolean(control.checked);
                      const roleTarget = el.matches('[role=checkbox]') ? el : el.closest('[role=checkbox]');
                      if (!roleTarget) return null;
                      const aria = roleTarget.getAttribute('aria-checked');
                      return aria === 'true' ? true : aria === 'false' ? false : null;
                    }"""
                )
                if checked is None or bool(checked) != expected_checked:
                    _robust_locator_click(locator, page)
            return self._record_element_step(action, element)

        if action == "upload":
            if not css_candidate:
                raise ValueError("文件控件缺少可执行 CSS 定位器")
            files = payload.get("files") or payload.get("value")
            if isinstance(files, str):
                files = [item.strip() for item in files.split(",") if item.strip()]
            if not files:
                raise ValueError("请选择要上传的文件")
            frame.locator(css_candidate["value"]).first.set_input_files(files)
            return self._record_element_step("upload", element, files=files)

        if action == "hover":
            page.mouse.move(x, y)
            page.wait_for_timeout(200)
            return self._record_element_step("hover", element)

        raise ValueError(f"不支持的录制动作: {action}")

    def _validate_element(self, page, payload: Dict[str, Any]) -> Dict[str, Any]:
        from core.web_flow_runner import WebFlowRunner

        element = dict(payload.get("element") or {})
        if not element:
            raise ValueError("缺少要验证的元素")
        timeout = max(500, min(15000, int(payload.get("timeout") or 5000)))
        runner = WebFlowRunner(page, {
            "flow_version": 2,
            "base_url": self.start_url,
            "timeout": timeout,
            "steps": [],
        })
        attempts = []
        try:
            scope, frame_path = runner._frame_for_element(element)
            selected_strategy = ""
            for candidate in element.get("locators") or []:
                attempt = {
                    "strategy": candidate.get("strategy"),
                    "value": candidate.get("value"),
                    "matched": False,
                    "count": 0,
                    "visible_count": 0,
                }
                try:
                    locator = runner._candidate_locator(scope, candidate)
                    if locator is not None:
                        count = locator.count()
                        visible_count = sum(
                            1 for index in range(min(count, 20))
                            if locator.nth(index).is_visible()
                        )
                        attempt.update({
                            "count": count,
                            "visible_count": visible_count,
                            "matched": visible_count > 0,
                        })
                        if visible_count > 0 and not selected_strategy:
                            selected_strategy = str(candidate.get("strategy") or "")
                except Exception as exc:
                    attempt["error"] = str(exc)[:300]
                attempts.append(attempt)

            if not selected_strategy and element.get("fingerprint"):
                healed, healing = runner._heal_locator(scope, element.get("fingerprint") or {})
                if healed is not None:
                    healing["matched"] = bool(healed.is_visible())
                attempts.append(healing)
                if healing.get("matched"):
                    selected_strategy = "semantic_healing"

            diagnostics = {
                "matched": bool(selected_strategy),
                "selected_strategy": selected_strategy,
                "frame_path": frame_path,
                "attempts": attempts,
            }
            return {
                "matched": bool(selected_strategy),
                "element_name": element.get("name") or "未命名元素",
                "diagnostics": diagnostics,
            }
        except Exception as exc:
            return {
                "matched": False,
                "element_name": element.get("name") or "未命名元素",
                "error": str(exc),
                "diagnostics": {"matched": False, "attempts": attempts},
            }

    def _debug_step(self, page, payload: Dict[str, Any]):
        from core.web_flow_runner import WebFlowRunner

        step = dict(payload.get("step") or {})
        if not step.get("action"):
            raise ValueError("调试步骤缺少 action")
        runner = WebFlowRunner(page, {
            "flow_version": 2,
            "base_url": self.start_url,
            "timeout": int(step.get("timeout") or 10000),
            "steps": [],
        })
        error = ""
        try:
            runner.execute_step(step)
        except Exception as exc:
            error = str(exc)
        current_page = runner.page
        if current_page.is_closed():
            pages = [item for item in page.context.pages if not item.is_closed()]
            if pages:
                current_page = pages[-1]
        return current_page, {
            "ok": not error,
            "action": step.get("action"),
            "step_name": step.get("name") or step.get("action"),
            "error": error,
            "diagnostics": runner.last_locator_diagnostics,
        }

    def _run(self):
        page = None
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=self.headless,
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                )
                context_options = {
                    **mobile_context_options(self.viewport),
                    "locale": "zh-CN",
                    "ignore_https_errors": True,
                }
                if self.storage_state and (
                    self.storage_state.get("cookies") or self.storage_state.get("origins")
                ):
                    context_options["storage_state"] = self.storage_state
                context = browser.new_context(**context_options)
                context.route("**/*", guard_playwright_route)
                page = context.new_page()
                page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                launch_url = self.resume_url or self.start_url
                page.goto(launch_url, wait_until="domcontentloaded", timeout=30000)
                self._reset_navigation_history(page.url)
                if not self.steps:
                    self.steps.append({
                        "id": f"step_{uuid.uuid4().hex[:12]}",
                        "action": "goto",
                        "name": f"打开 {self.start_url}",
                        "url": self.start_url,
                        "timeout": 30000,
                    })
                self.status = "recording"
                self._persist_draft(page)
                self.ready.set()

                running = True
                while running:
                    command, payload, response_queue = self.commands.get()
                    try:
                        if command == "snapshot":
                            result = self._snapshot(page)
                        elif command == "probe":
                            result = {"element": self._probe(page, payload), **self._snapshot(page)}
                        elif command == "action":
                            action_type = str(payload.get("action") or payload.get("type") or "").lower()
                            record_enabled = bool(payload.get("record", True))
                            self.recording_enabled = record_enabled
                            pages_before = [item for item in context.pages if not item.is_closed()]
                            if action_type == "switch_tab":
                                pages = [item for item in context.pages if not item.is_closed()]
                                index = int(payload.get("tab_index") if payload.get("tab_index") is not None else payload.get("value") or -1)
                                if index < 0 or index >= len(pages):
                                    raise ValueError("标签页序号无效")
                                page = pages[index]
                                page.bring_to_front()
                                step = {
                                    "id": f"step_{uuid.uuid4().hex[:12]}",
                                    "action": "switch_tab",
                                    "name": f"切换标签页 {index + 1}",
                                    "index": index,
                                    "match_url": page.url,
                                }
                                if record_enabled:
                                    self._store_recorded_step(step)
                            elif action_type == "close_tab":
                                pages = [item for item in context.pages if not item.is_closed()]
                                if len(pages) <= 1:
                                    raise ValueError("最后一个标签页不可关闭")
                                current_url = page.url
                                page.close()
                                page = [item for item in context.pages if not item.is_closed()][-1]
                                page.bring_to_front()
                                step = {
                                    "id": f"step_{uuid.uuid4().hex[:12]}",
                                    "action": "close_tab",
                                    "name": f"关闭标签页 {current_url}",
                                }
                                if record_enabled:
                                    self._store_recorded_step(step)
                            else:
                                step = self._perform_action(page, payload)
                                pages_after = [item for item in context.pages if not item.is_closed()]
                                new_pages = [item for item in pages_after if item not in pages_before]
                                if new_pages:
                                    page = new_pages[-1]
                                    try:
                                        page.wait_for_load_state("domcontentloaded", timeout=5000)
                                    except Exception:
                                        pass
                                    page.bring_to_front()
                                    switch_step = {
                                        "id": f"step_{uuid.uuid4().hex[:12]}",
                                        "action": "switch_tab",
                                        "name": f"切换新标签页 {page.url}",
                                        "index": pages_after.index(page),
                                        "match_url": page.url,
                                    }
                                    if record_enabled:
                                        self._store_recorded_step(switch_step)
                            self._persist_draft(page)
                            result = {"recorded_step": step, **self._snapshot(page)}
                            if isinstance(step, dict) and step.get("element"):
                                action_element = dict(step["element"])
                                if payload.get("x") is not None and payload.get("y") is not None:
                                    action_element["point"] = {
                                        "x": float(payload.get("x") or 0),
                                        "y": float(payload.get("y") or 0),
                                    }
                                result["action_element"] = action_element
                        elif command == "delete_step":
                            index = int(payload.get("index"))
                            if index < 0 or index >= len(self.steps):
                                raise IndexError("步骤不存在")
                            if index == 0 and self.steps[0].get("action") == "goto":
                                raise ValueError("首个打开页面步骤不可删除")
                            self.steps.pop(index)
                            self._persist_draft(page)
                            result = self._snapshot(page)
                        elif command == "append_step":
                            self._insert_manual_step(
                                payload.get("step") or {},
                                payload.get("index"),
                            )
                            self._persist_draft(page)
                            result = self._snapshot(page)
                        elif command == "update_step":
                            index = int(payload.get("index"))
                            if index < 0 or index >= len(self.steps):
                                raise IndexError("步骤不存在")
                            step = dict(payload.get("step") or {})
                            if not step.get("action"):
                                raise ValueError("组件步骤缺少 action")
                            if index == 0 and self.steps[0].get("action") == "goto" and step.get("action") != "goto":
                                raise ValueError("首个打开页面步骤必须保留为 goto")
                            step.setdefault("id", self.steps[index].get("id") or f"step_{uuid.uuid4().hex[:12]}")
                            self.steps[index] = step
                            self._persist_draft(page)
                            result = self._snapshot(page)
                        elif command == "reorder_step":
                            from_index = int(payload.get("from_index"))
                            to_index = int(payload.get("to_index"))
                            if from_index <= 0 or to_index <= 0:
                                raise ValueError("首个打开页面步骤必须保留在第一位")
                            if from_index >= len(self.steps) or to_index >= len(self.steps):
                                raise IndexError("步骤不存在")
                            step = self.steps.pop(from_index)
                            self.steps.insert(to_index, step)
                            self._persist_draft(page)
                            result = self._snapshot(page)
                        elif command == "validate_element":
                            validation = self._validate_element(page, payload)
                            result = {"validation": validation, **self._snapshot(page)}
                        elif command == "debug_step":
                            page, debug_result = self._debug_step(page, payload)
                            result = {"debug_result": debug_result, **self._snapshot(page)}
                        elif command == "stop":
                            self.status = "stopped"
                            self._persist_draft(page)
                            result = {
                                "session_id": self.session_id,
                                "status": self.status,
                                "url": page.url,
                                "steps": list(self.steps),
                                "step_count": len(self.steps),
                                "viewport": self.viewport,
                                "auth_state_saved": bool(
                                    self.storage_state.get("cookies") or self.storage_state.get("origins")
                                ),
                            }
                            running = False
                        else:
                            raise ValueError(f"未知浏览器命令: {command}")
                        response_queue.put(result)
                    except BaseException as exc:
                        response_queue.put(exc)
                context.close()
                browser.close()
        except BaseException as exc:
            self.error = exc
            self.status = "error"
            try:
                self._persist_draft(page)
            except Exception:
                pass
            self.ready.set()


class BrowserRecordingManager:
    def __init__(self):
        self.sessions: Dict[str, BrowserRecordingSession] = {}
        self.lock = threading.Lock()

    def _assert_project_available(self, project_id: str):
        for session in self.sessions.values():
            if session.project_id == project_id and session.status in {"starting", "recording"}:
                raise ValueError("当前项目已有正在进行的浏览器录制")

    def start(
        self,
        project_id: str,
        url: str,
        viewport: Dict[str, int],
        initial_steps: Optional[list[Dict[str, Any]]] = None,
        record_insert_index: Optional[int] = None,
    ) -> BrowserRecordingSession:
        with self.lock:
            self._assert_project_available(project_id)
            steps = json.loads(json.dumps(initial_steps or [], ensure_ascii=False))
            if record_insert_index is not None:
                record_insert_index = int(record_insert_index)
                if record_insert_index < 0 or record_insert_index > len(steps):
                    raise ValueError("录制步骤插入位置不存在")
                if record_insert_index == 0 and steps and steps[0].get("action") == "goto":
                    raise ValueError("首个打开页面步骤必须保留在第一位")
            session_id = f"wrec_{uuid.uuid4().hex[:12]}"
            session = BrowserRecordingSession(
                session_id=session_id,
                project_id=project_id,
                start_url=url,
                viewport=viewport,
                steps=steps,
                record_insert_index=record_insert_index,
            )
            self.sessions[session_id] = session
        try:
            session.start()
            return session
        except Exception:
            with self.lock:
                self.sessions.pop(session_id, None)
            raise

    def resume(
        self,
        draft_id: str,
        record_insert_index: Optional[int] = None,
    ) -> BrowserRecordingSession:
        draft = _get_recording_draft(draft_id)
        if not draft:
            raise KeyError("录制草稿不存在")
        if draft.get("status") not in RECOVERABLE_DRAFT_STATUSES:
            raise ValueError("当前录制草稿不可恢复")
        start_url = _validate_url(draft["start_url"])
        resume_url = _validate_url(draft.get("last_url") or start_url)
        steps = json.loads(json.dumps(draft.get("steps") or [], ensure_ascii=False))
        if record_insert_index is not None:
            record_insert_index = int(record_insert_index)
            if record_insert_index < 0 or record_insert_index > len(steps):
                raise ValueError("录制步骤插入位置不存在")
            if record_insert_index == 0 and steps and steps[0].get("action") == "goto":
                raise ValueError("首个打开页面步骤必须保留在第一位")
        with self.lock:
            self._assert_project_available(draft["project_id"])
            if draft_id in self.sessions:
                raise ValueError("录制草稿已经恢复")
            session = BrowserRecordingSession(
                session_id=draft_id,
                project_id=draft["project_id"],
                start_url=start_url,
                viewport=draft.get("viewport") or {"width": 1440, "height": 900},
                resume_url=resume_url,
                steps=steps,
                record_insert_index=record_insert_index,
                storage_state=dict(draft.get("storage_state") or {}),
            )
            self.sessions[draft_id] = session
        try:
            session.start()
            return session
        except Exception:
            with self.lock:
                self.sessions.pop(draft_id, None)
            raise

    def get(self, session_id: str) -> BrowserRecordingSession:
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError("录制会话不存在或已结束")
        return session

    def active_for_project(self, project_id: str) -> Optional[BrowserRecordingSession]:
        with self.lock:
            return next(
                (
                    session for session in self.sessions.values()
                    if session.project_id == project_id and session.status in {"starting", "recording"}
                ),
                None,
            )

    def is_active(self, session_id: str) -> bool:
        with self.lock:
            session = self.sessions.get(session_id)
            return bool(session and session.status in {"starting", "recording"})

    def stop(self, session_id: str):
        session = self.get(session_id)
        try:
            return session.call("stop")
        finally:
            with self.lock:
                self.sessions.pop(session_id, None)

    def shutdown(self):
        with self.lock:
            session_ids = list(self.sessions)
        for session_id in session_ids:
            try:
                self.stop(session_id)
            except Exception:
                pass


recording_manager = BrowserRecordingManager()


class RecordingStartIn(BaseModel):
    project_id: str
    url: str = ""
    env: str = "test"
    viewport: Dict[str, int] = Field(default_factory=lambda: {"width": 1440, "height": 900})
    initial_steps: list[Dict[str, Any]] = Field(default_factory=list)
    record_insert_index: Optional[int] = None


class RecordingResumeIn(BaseModel):
    record_insert_index: Optional[int] = None


class RecordingActionIn(BaseModel):
    action: str
    record: bool = True
    x: Optional[float] = None
    y: Optional[float] = None
    source_x: Optional[float] = None
    source_y: Optional[float] = None
    delta_x: int = 0
    delta_y: int = 0
    value: Any = None
    key: str = ""
    url: str = ""
    option_label: str = ""
    option_value: str = ""
    files: Any = None
    tab_index: Optional[int] = None
    scroll_scope: str = ""
    popup_only: bool = False


class RecordingProbeIn(BaseModel):
    x: float
    y: float


class AppendStepIn(BaseModel):
    step: Dict[str, Any]
    index: Optional[int] = None


class StepUpdateIn(BaseModel):
    step: Dict[str, Any]


class ReorderStepIn(BaseModel):
    from_index: int
    to_index: int


class RecordingDraftUpdateIn(BaseModel):
    steps: list[Dict[str, Any]]


class ElementValidationIn(BaseModel):
    element: Dict[str, Any]
    timeout: int = 5000


class DebugStepIn(BaseModel):
    step: Dict[str, Any]


class StandaloneDebugStepIn(BaseModel):
    """调试不依赖浏览器页面的接口或数据库步骤。"""

    step: Dict[str, Any]
    variables: Dict[str, Any] = Field(default_factory=dict)
    flow: Dict[str, Any] = Field(default_factory=dict)


class FlowSaveIn(BaseModel):
    project_id: str
    name: str
    filename: str = ""
    original_filename: str = ""
    description: str = ""
    base_url: str = ""
    viewport: Dict[str, int] = Field(default_factory=lambda: {"width": 1440, "height": 900})
    steps: list[Dict[str, Any]]
    variables: list[Dict[str, Any]] = Field(default_factory=list)
    database: Dict[str, Any] = Field(default_factory=dict)
    runtime_assertions: list[Dict[str, Any]] = Field(default_factory=list)
    draft_id: str = ""
    overwrite: bool = True


class FlowImportIn(BaseModel):
    project_id: str
    filename: str = ""
    content: str


def _session_or_404(session_id: str) -> BrowserRecordingSession:
    try:
        return recording_manager.get(session_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


def _materialize_test_object_step(step: Dict[str, Any], request: Request | None) -> Dict[str, Any]:
    """Resolve a RunnerGo test-object reference only for the current execution."""
    step = copy.deepcopy(step or {})
    references = runnergo_test_objects.collect_references(step)
    if not references:
        return step

    credential = auth_service.extract_credential(request) if request is not None else None
    bundle = runnergo_test_objects.resolve_references(
        [step], credential.token if credential else ""
    )
    ref = references[0]
    item = bundle.get(runnergo_test_objects.object_key(ref["team_id"], ref["target_id"])) or {}
    materialized = copy.deepcopy(item.get("step") or {})
    if not materialized:
        raise runnergo_test_objects.TestObjectError(
            f"测试对象不存在或无权访问: {ref['target_id']}"
        )
    for key, value in step.items():
        if (
            key in {"headers", "params", "extract", "extract_vars"}
            and isinstance(value, dict)
            and isinstance(materialized.get(key), dict)
        ):
            materialized[key].update(copy.deepcopy(value))
        else:
            materialized[key] = copy.deepcopy(value)
    materialized.pop("test_object_ref", None)
    return materialized


def _standalone_debug_step(
    step: Dict[str, Any],
    flow: Dict[str, Any] | None = None,
    variables: Dict[str, Any] | None = None,
    request: Request | None = None,
) -> Dict[str, Any]:
    """Execute an API/DB step without launching or requiring a browser session."""
    from core.web_flow_runner import WebFlowRunner

    materialized = _materialize_test_object_step(step, request)
    action = str(materialized.get("action") or materialized.get("type") or "").strip().lower()
    if action not in {"api", "http", "request", "api_request", "db", "database", "query", "sql"}:
        raise ValueError("独立单步调试只支持接口和数据库步骤；页面步骤请先启动浏览器录制")

    runtime_flow = copy.deepcopy(flow or {})
    runtime_flow.setdefault("flow_version", 2)
    runtime_flow.setdefault("steps", [])
    if variables:
        existing = runtime_flow.get("variables")
        if isinstance(existing, dict):
            runtime_flow["variables"] = {**existing, **copy.deepcopy(variables)}
        elif not existing:
            runtime_flow["variables"] = copy.deepcopy(variables)

    runner = WebFlowRunner(None, runtime_flow)
    error = ""
    result = None
    try:
        runner.execute_step(materialized)
        if runner.runtime_outputs:
            result = runner.runtime_outputs[-1].get("result")
    except Exception as exc:
        error = str(exc)
        result = getattr(exc, "runtime_result", None)

    if result is not None:
        result = redact(result)
    runtime_context = redact(runner.runtime_context.public())
    diagnostics = redact(runner.last_locator_diagnostics)
    if action in {"api", "http", "request", "api_request"}:
        business = (result or {}).get("business") if isinstance(result, dict) else None
        diagnostics = {
            **diagnostics,
            "matched": not bool(error),
            "selected_strategy": "api",
            "status": (result or {}).get("status") if isinstance(result, dict) else None,
            "url": (result or {}).get("url") if isinstance(result, dict) else None,
            "business_success": business.get("success") if isinstance(business, dict) else None,
            "business_code": business.get("code") if isinstance(business, dict) else None,
        }
    else:
        diagnostics = {
            **diagnostics,
            "matched": not bool(error),
            "selected_strategy": "database",
            "driver": (result or {}).get("driver") if isinstance(result, dict) else None,
            "row_count": (result or {}).get("row_count", 0) if isinstance(result, dict) else 0,
        }

    return {
        "ok": not bool(error),
        "standalone": True,
        "action": materialized.get("action") or materialized.get("type"),
        "step_name": materialized.get("name") or materialized.get("action") or materialized.get("type"),
        "error": error,
        "diagnostics": diagnostics,
        "result": result,
        "business": (result or {}).get("business") if isinstance(result, dict) else None,
        "business_success": (
            (result or {}).get("business", {}).get("success")
            if isinstance((result or {}).get("business"), dict)
            else None
        ),
        "warnings": (result or {}).get("warnings", []) if isinstance(result, dict) else [],
        "extracted": (result or {}).get("extracted", {}) if isinstance(result, dict) else {},
        "scripts": (result or {}).get("scripts", {}) if isinstance(result, dict) else {},
        "runtime_context": runtime_context,
    }


@router.post("/recordings/start")
def start_recording(body: RecordingStartIn):
    project = project_service.get_project(body.project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    envs = project.get("envs") or {}
    url = body.url or envs.get(body.env) or project.get("base_url") or ""
    try:
        url = _validate_url(url)
        viewport = {
            "width": max(320, min(3840, int(body.viewport.get("width") or 1440))),
            "height": max(480, min(2160, int(body.viewport.get("height") or 900))),
        }
        start_kwargs = {
            "initial_steps": body.initial_steps,
        }
        if body.record_insert_index is not None:
            start_kwargs["record_insert_index"] = body.record_insert_index
        session = recording_manager.start(body.project_id, url, viewport, **start_kwargs)
        return {"ok": True, **session.call("snapshot")}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"启动浏览器录制失败: {exc}") from exc


@router.get("/recordings/{session_id}/frame")
def recording_frame(session_id: str):
    session = _session_or_404(session_id)
    try:
        return session.call("snapshot")
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/recordings/{session_id}/probe")
def probe_recording_element(session_id: str, body: RecordingProbeIn):
    session = _session_or_404(session_id)
    try:
        return session.call("probe", body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/recordings/{session_id}/actions")
def recording_action(session_id: str, body: RecordingActionIn):
    session = _session_or_404(session_id)
    try:
        return session.call("action", body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@router.delete("/recordings/{session_id}/steps/{index}")
def delete_recording_step(session_id: str, index: int):
    session = _session_or_404(session_id)
    try:
        return session.call("delete_step", {"index": index})
    except (ValueError, IndexError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/recordings/{session_id}/steps")
def append_recording_step(session_id: str, body: AppendStepIn):
    session = _session_or_404(session_id)
    try:
        return session.call("append_step", body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.put("/recordings/{session_id}/steps/{index}")
def update_recording_step(session_id: str, index: int, body: StepUpdateIn):
    session = _session_or_404(session_id)
    try:
        return session.call("update_step", {"index": index, **body.model_dump()})
    except (ValueError, IndexError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/recordings/{session_id}/reorder-step")
def reorder_recording_step(session_id: str, body: ReorderStepIn):
    session = _session_or_404(session_id)
    try:
        return session.call("reorder_step", body.model_dump())
    except (ValueError, IndexError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/recordings/{session_id}/validate-element")
def validate_recording_element(session_id: str, body: ElementValidationIn):
    session = _session_or_404(session_id)
    try:
        return session.call("validate_element", body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/recordings/{session_id}/debug-step")
def debug_recording_step(session_id: str, body: DebugStepIn, request: Request):
    session = _session_or_404(session_id)
    try:
        payload = body.model_dump()
        payload["step"] = _materialize_test_object_step(payload.get("step") or {}, request)
        return session.call("debug_step", payload, timeout=45)
    except (ValueError, runnergo_test_objects.TestObjectError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/debug-step")
def debug_standalone_step(body: StandaloneDebugStepIn, request: Request):
    """调试接口/数据库步骤，不要求存在浏览器录制会话。"""
    try:
        return _standalone_debug_step(
            body.step,
            flow=body.flow,
            variables=body.variables,
            request=request,
        )
    except (ValueError, runnergo_test_objects.TestObjectError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"接口/数据库单步调试失败: {exc}") from exc


@router.get("/recordings/active")
def active_recording(project_id: str):
    session = recording_manager.active_for_project(project_id)
    if not session:
        return {"active": False, "draft": _public_recording_draft(_latest_recording_draft(project_id))}
    try:
        return {"active": True, **session.call("snapshot")}
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/recordings/{session_id}/stop")
def stop_recording(session_id: str):
    try:
        return {"ok": True, **recording_manager.stop(session_id)}
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@router.get("/recording-drafts/{draft_id}")
def get_recording_draft(draft_id: str):
    draft = _get_recording_draft(draft_id)
    if not draft:
        raise HTTPException(404, "录制草稿不存在")
    return _public_recording_draft(draft)


@router.put("/recording-drafts/{draft_id}")
def update_recording_draft(draft_id: str, body: RecordingDraftUpdateIn):
    if recording_manager.is_active(draft_id):
        raise HTTPException(409, "活动录制会话的步骤由录制器自动保存")
    try:
        return {"ok": True, **(_public_recording_draft(_update_recording_draft_steps(draft_id, body.steps)) or {})}
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/recording-drafts/{draft_id}")
def delete_recording_draft(draft_id: str):
    if recording_manager.is_active(draft_id):
        raise HTTPException(409, "请先停止活动录制会话")
    if not _delete_recording_draft(draft_id):
        raise HTTPException(404, "录制草稿不存在")
    return {"ok": True}


@router.post("/recording-drafts/{draft_id}/resume")
def resume_recording_draft(draft_id: str, body: Optional[RecordingResumeIn] = None):
    try:
        session = recording_manager.resume(
            draft_id,
            record_insert_index=body.record_insert_index if body else None,
        )
        return {"ok": True, "recovered": True, **session.call("snapshot")}
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"恢复浏览器录制失败: {exc}") from exc


@router.get("/elements")
def list_web_elements(project_id: str):
    rows = db.execute(
        "SELECT * FROM web_elements WHERE project_id=? ORDER BY updated_at DESC",
        (project_id,), fetch=True,
    )
    result = []
    for row in db.to_dicts(rows):
        row["locators"] = json.loads(row.get("locators") or "[]")
        row["fingerprint"] = json.loads(row.get("fingerprint") or "{}")
        row["element_context"] = json.loads(row.get("element_context") or "{}")
        result.append(row)
    return result


@router.delete("/elements/{element_id}")
def delete_web_element(element_id: str):
    rows = db.execute("SELECT id FROM web_elements WHERE id=?", (element_id,), fetch=True)
    if not rows:
        raise HTTPException(404, "元素不存在")
    db.execute("DELETE FROM web_elements WHERE id=?", (element_id,))
    return {"ok": True}


def _safe_flow_filename(value: str, name: str) -> str:
    raw = os.path.basename(str(value or "").strip())
    if not raw:
        stem = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_").lower() or f"recorded_{int(time.time())}"
        raw = f"test_{stem}.yaml"
    if not raw.endswith((".yaml", ".yml")):
        raw += ".yaml"
    stem, extension = os.path.splitext(raw)
    # Keep Unicode letters (including Chinese) in user-provided filenames;
    # generated pytest function names are sanitized separately.
    stem = re.sub(r"[^\w]+", "_", stem, flags=re.UNICODE).strip("_")
    if not stem:
        stem = f"test_recorded_{int(time.time())}"
    if not stem.startswith("test_") and not any(ord(char) > 127 for char in stem):
        stem = f"test_{stem}"
    return stem + extension


def _validate_web_flow_payload(value: Any, *, require_steps: bool = True) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise HTTPException(400, "YAML 顶层必须是对象")
    try:
        flow_version = int(value.get("flow_version") or 0)
    except (TypeError, ValueError):
        flow_version = 0
    if flow_version != 2:
        raise HTTPException(400, "仅支持 flow_version: 2 的场景编排 YAML")

    raw_steps = value.get("steps")
    if not isinstance(raw_steps, list):
        raise HTTPException(400, "场景编排 YAML 缺少 steps 数组")
    if require_steps and not raw_steps:
        raise HTTPException(400, "场景编排 YAML 至少需要一个步骤")

    steps = []
    for index, raw_step in enumerate(raw_steps, start=1):
        if not isinstance(raw_step, dict):
            raise HTTPException(400, f"第 {index} 个步骤必须是对象")
        step = dict(raw_step)
        action = str(step.get("action") or step.get("type") or "").strip().lower()
        if not action:
            raise HTTPException(400, f"第 {index} 个步骤缺少 action")
        if action not in SUPPORTED_FLOW_ACTIONS:
            raise HTTPException(400, f"第 {index} 个步骤使用了不支持的 action: {action}")
        step["action"] = action
        step.pop("type", None)
        step.setdefault("id", f"imported_step_{index}")
        steps.append(step)

    flow = dict(value)
    flow["flow_version"] = 2
    flow["steps"] = steps
    # Imported and historical cases use page-aware replay: execute a step when
    # its recorded element belongs to the current page, otherwise skip it.
    flow["step_execution_mode"] = "page_aware"
    browser = dict(flow.get("browser") or {})
    viewport = dict(browser.get("viewport") or {})
    try:
        width = max(320, min(3840, int(viewport.get("width") or 1440)))
        height = max(480, min(2160, int(viewport.get("height") or 900)))
    except (TypeError, ValueError):
        raise HTTPException(400, "browser.viewport 的 width/height 必须是数字")
    browser["engine"] = str(browser.get("engine") or "chromium")
    browser["viewport"] = {"width": width, "height": height}
    browser.setdefault("locale", "zh-CN")
    flow["browser"] = browser
    try:
        flow["timeout"] = max(1, int(flow.get("timeout") or 10000))
    except (TypeError, ValueError):
        raise HTTPException(400, "timeout 必须是毫秒数字")
    return flow


def _load_web_flow(project_id: str, filename: str) -> Dict[str, Any]:
    safe_filename = os.path.basename(str(filename or "").strip())
    if not safe_filename or safe_filename != filename:
        raise HTTPException(400, "用例文件名无效")
    try:
        content = case_service.get_case(project_id, "ui", safe_filename)
    except FileNotFoundError as exc:
        raise HTTPException(404, "用例不存在") from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    try:
        flow = yaml.safe_load(content) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(400, f"用例 YAML 解析失败: {exc}") from exc
    try:
        flow = _validate_web_flow_payload(flow, require_steps=False)
    except HTTPException as exc:
        raise HTTPException(400, f"该用例不是可视化场景编排格式：{exc.detail}，请使用 YAML 编辑") from exc
    return {"filename": safe_filename, "content": content, "flow": flow}


@router.get("/flows/{project_id}/{filename}")
def get_web_flow(project_id: str, filename: str):
    if not project_service.get_project(project_id):
        raise HTTPException(404, "项目不存在")
    return {"project_id": project_id, **_load_web_flow(project_id, filename)}


@router.post("/flows/import")
def import_web_flow(body: FlowImportIn):
    if not project_service.get_project(body.project_id):
        raise HTTPException(404, "项目不存在")
    if not body.content.strip():
        raise HTTPException(400, "YAML 文件内容为空")
    if len(body.content.encode("utf-8")) > 2 * 1024 * 1024:
        raise HTTPException(400, "YAML 文件不能超过 2MB")
    try:
        parsed = yaml.safe_load(body.content)
    except yaml.YAMLError as exc:
        raise HTTPException(400, f"YAML 解析失败: {exc}") from exc
    flow = _validate_web_flow_payload(parsed)
    filename = _safe_flow_filename(body.filename, str(flow.get("name") or "imported_flow"))
    flow["name"] = str(flow.get("name") or filename.rsplit(".", 1)[0])
    metadata = dict(flow.get("metadata") or {})
    now = _now()
    metadata.setdefault("created_at", now)
    metadata["updated_at"] = now
    metadata["source"] = "imported_yaml"
    metadata["imported_filename"] = os.path.basename(body.filename or filename)
    flow["metadata"] = metadata
    content = yaml.safe_dump(flow, allow_unicode=True, sort_keys=False, width=120)
    try:
        path = case_service.save_case(body.project_id, "ui", filename, content)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "ok": True,
        "project_id": body.project_id,
        "filename": filename,
        "path": path,
        "content": content,
        "flow": flow,
    }


@router.post("/flows")
def save_web_flow(body: FlowSaveIn):
    if not project_service.get_project(body.project_id):
        raise HTTPException(404, "项目不存在")
    if not body.steps:
        raise HTTPException(400, "录制流程没有步骤")
    filename = _safe_flow_filename(body.filename, body.name)
    original_filename = _safe_flow_filename(body.original_filename, body.name) if body.original_filename.strip() else ""
    now = _now()
    existing_metadata: Dict[str, Any] = {}
    existing_content = None
    try:
        existing_content = case_service.get_case(
            body.project_id, "ui", original_filename or filename
        )
    except (FileNotFoundError, ValueError):
        pass
    if existing_content is not None:
        if not body.overwrite:
            raise HTTPException(409, f"YAML 文件已存在：{filename}")
        try:
            existing = yaml.safe_load(existing_content) or {}
            if isinstance(existing, dict) and isinstance(existing.get("metadata"), dict):
                existing_metadata = dict(existing["metadata"])
        except yaml.YAMLError:
            pass
    metadata = existing_metadata
    metadata.setdefault("source", "browser_recorder")
    metadata.setdefault("created_at", now)
    metadata["updated_at"] = now
    required_variables = []
    for variable in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", json.dumps(body.steps, ensure_ascii=False)):
        if variable not in required_variables:
            required_variables.append(variable)
    if required_variables:
        metadata["required_variables"] = required_variables
    else:
        metadata.pop("required_variables", None)
    flow = {
        "flow_version": 2,
        "step_execution_mode": "page_aware",
        "name": body.name.strip() or filename.rsplit(".", 1)[0],
        "description": body.description,
        "base_url": body.base_url,
        "browser": {
            "engine": "chromium",
            "viewport": {
                "width": int(body.viewport.get("width") or 1440),
                "height": int(body.viewport.get("height") or 900),
            },
            "locale": "zh-CN",
        },
        "timeout": 10000,
        "variables": body.variables,
        "database": body.database,
        "runtime_assertions": body.runtime_assertions,
        "steps": body.steps,
        "metadata": metadata,
    }
    content = yaml.safe_dump(flow, allow_unicode=True, sort_keys=False, width=120)
    try:
        if original_filename and original_filename != filename:
            path = case_service.rename_case(
                body.project_id, "ui", original_filename, filename, content
            )
        else:
            path = case_service.save_case(body.project_id, "ui", filename, content)
    except FileNotFoundError as exc:
        raise HTTPException(404, "原用例不存在") from exc
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    draft_cleared = False
    if body.draft_id and not recording_manager.is_active(body.draft_id):
        draft_cleared = _delete_recording_draft(body.draft_id)
    return {
        "ok": True,
        "filename": filename,
        "path": path,
        "content": content,
        "flow": flow,
        "draft_cleared": draft_cleared,
    }
