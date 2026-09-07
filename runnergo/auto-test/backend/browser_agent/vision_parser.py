"""视觉解析器：分析页面元素快照，把测试目标映射为可执行动作。

不依赖固定 XPath：基于语义（role/name/label/placeholder/文本）识别元素；
模型可用时用 LLM 从候选元素中挑选，失败回退规则匹配。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from backend.llm.provider import chat_json, is_configured

_LOGIN_USERNAME_HINTS = ["用户名", "账号", "邮箱", "username", "account", "user", "email"]
_LOGIN_PASSWORD_HINTS = ["密码", "password", "pwd"]
_PHONE_HINTS = ["手机", "手机号", "mobile", "phone", "电话"]
_CODE_HINTS = ["验证码", "短信", "code", "captcha", "otp", "动态码"]
_SEND_CODE_HINTS = ["发送", "获取验证码", "send", "获取短信"]
_LOGIN_BUTTON_HINTS = ["登录", "登陆", "sign in", "login", "立即登录"]
_REGISTER_BUTTON_HINTS = ["注册", "sign up", "register", "创建账号"]
_SEARCH_HINTS = ["搜索", "search", "查询", "keyword"]
_CART_HINTS = ["购物车", "cart", "加入购物车", "add to cart"]
_SUBMIT_HINTS = ["提交", "确定", "确认", "submit", "confirm", "ok", "立即购买", "去结算", "结算", "下单", "支付"]


def _match_hint(name: str, hints: list[str]) -> bool:
    name = str(name or "").lower()
    return any(hint.lower() in name for hint in hints)


def parse_goal(goal: str) -> dict:
    """解析测试目标，提取动作意图。"""
    goal = str(goal or "")
    intents: list[dict] = []
    low = goal.lower()
    if any(k in low for k in ("登录", "登陆", "sign in", "login")):
        intents.append({"action": "login", "label": "完成登录"})
    if any(k in low for k in ("注册", "sign up", "register")):
        intents.append({"action": "register", "label": "完成注册"})
    if any(k in low for k in ("搜索", "search", "查询")):
        intents.append({"action": "search", "label": "执行搜索"})
    if any(k in low for k in ("购物车", "cart", "加购", "add to cart")):
        intents.append({"action": "cart", "label": "加入购物车"})
    if any(k in low for k in ("结算", "下单", "购买", "支付", "提交订单", "checkout", "submit")):
        intents.append({"action": "checkout", "label": "结算下单"})
    if not intents:
        intents.append({"action": "inspect", "label": "页面检查"})
    return {"intents": intents, "raw": goal}


def analyze_page(controller: Any, goal: str) -> dict:
    """分析当前页面，识别可交互元素并生成动作候选。"""
    snapshot = controller.accessibility_snapshot()
    parsed = parse_goal(goal)
    candidates = []
    for entry in snapshot:
        role = str(entry.get("role") or "")
        name = str(entry.get("name") or "")
        if role in {"textbox", "searchbox"} or any(k in role.lower() for k in ("textbox", "searchbox")):
            if _match_hint(name, _LOGIN_PASSWORD_HINTS):
                kind = "password"
            elif _match_hint(name, _PHONE_HINTS):
                kind = "phone"
            elif _match_hint(name, _CODE_HINTS):
                kind = "code"
            elif _match_hint(name, _LOGIN_USERNAME_HINTS):
                kind = "username"
            else:
                kind = "text"
            candidates.append({"kind": kind, "role": "textbox", "name": name, "node_id": entry.get("node_id")})
        elif role in {"button", "link", "menuitem"}:
            kind = "submit"
            if _match_hint(name, _LOGIN_BUTTON_HINTS):
                kind = "login"
            elif _match_hint(name, _REGISTER_BUTTON_HINTS):
                kind = "register"
            elif _match_hint(name, _SEND_CODE_HINTS):
                kind = "send_code"
            elif _match_hint(name, _SEARCH_HINTS):
                kind = "search"
            elif _match_hint(name, _CART_HINTS):
                kind = "cart"
            candidates.append({"kind": kind, "role": "button" if role == "button" else "link",
                               "name": name, "node_id": entry.get("node_id")})
    if is_configured():
        try:
            prompt_candidates = [
                {"kind": c["kind"], "role": c["role"], "name": c["name"]}
                for c in candidates[:80]
            ]
            data = chat_json(
                "你是浏览器视觉 Agent。根据测试目标从候选元素中挑选每个意图应操作的元素。"
                '输出：{"actions":[{"intent":"login|register|search|cart|checkout|inspect","role":"textbox|button","name":"元素名","value_hint":"应填写的值提示(可选)"}]}。'
                "找不到合适元素时该 intent 省略。",
                f"测试目标：{goal}\n候选元素：{json_dumps(prompt_candidates)}",
                max_tokens=500,
                temperature=0.1,
            )
            actions = data.get("actions") or []
            if isinstance(actions, list) and actions:
                mapped = []
                for action in actions:
                    name = str(action.get("name") or "")
                    kind_map = {
                        "login": "login", "register": "register", "search": "search",
                        "cart": "cart", "checkout": "checkout", "inspect": "inspect",
                    }
                    mapped.append({
                        "kind": kind_map.get(str(action.get("intent") or ""), "submit"),
                        "role": str(action.get("role") or "textbox"),
                        "name": name,
                        "value_hint": str(action.get("value_hint") or ""),
                    })
                return {"intents": parsed["intents"], "candidates": candidates, "actions": mapped}
        except Exception:  # noqa: BLE001 - 模型失败回退规则
            pass
    return {"intents": parsed["intents"], "candidates": candidates, "actions": []}


def pick_for_kind(candidates: list[dict], kind: str) -> list[dict]:
    """按 kind 挑选候选元素（保持顺序）。"""
    return [c for c in candidates if c.get("kind") == kind]


def json_dumps(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, default=str)
