"""安全数据生成器：基于规则库 security.json 按参数语义生成攻击载荷。"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from backend.data_agent.schema_parser import infer_parameter_type


_RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules", "security.json")


def _load_rules() -> dict:
    with open(_RULES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


_CATEGORY_HINTS = [
    ("sql_injection", re.compile(r"id$|_id$|name$|username$|account$|search$|keyword$|code$|phone$|email$|pass", re.IGNORECASE)),
    ("xss", re.compile(r"name$|username$|nickname$|content$|comment$|title$|remark$|message$|search$|keyword$|description$", re.IGNORECASE)),
    ("path_traversal", re.compile(r"path$|file$|filename$|dir$|download$", re.IGNORECASE)),
    ("command_injection", re.compile(r"cmd$|command$|shell$|exec$|ping$", re.IGNORECASE)),
    ("jwt_tamper", re.compile(r"token$|jwt$|authorization$|auth$", re.IGNORECASE)),
    ("ssrf", re.compile(r"url$|uri$|link$|callback$|redirect$|webhook$|endpoint$|host$", re.IGNORECASE)),
    ("file_upload", re.compile(r"file$|upload$|attachment$|image$|avatar$", re.IGNORECASE)),
    ("authorization_bypass", re.compile(r"role$|permission$|user(id)?$|is_admin$", re.IGNORECASE)),
]


def _categories_for(param: dict) -> list[str]:
    name = str(param.get("name") or "").lower()
    matched = [category for category, pattern in _CATEGORY_HINTS if pattern.search(name)]
    if not matched:
        ptype = infer_parameter_type(name, param.get("type", ""), param.get("format", ""))
        if ptype == "id":
            matched = ["sql_injection", "authorization_bypass"]
        elif ptype in {"username", "name", "email", "phone", "search"}:
            matched = ["sql_injection", "xss"]
        elif ptype == "url":
            matched = ["ssrf", "path_traversal"]
        else:
            matched = ["sql_injection", "xss"]
    return matched[:3]


def generate_security_data(parameters: list[dict]) -> list[dict]:
    """为每个参数生成安全用例：{parameter, category, value, expected}。"""
    rules = _load_rules()
    cases: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for param in parameters:
        name = str(param.get("name") or "")
        if not name:
            continue
        for category in _categories_for(param):
            payloads = rules.get(category, [])
            for payload in payloads[:4]:
                key = (name, str(payload))
                if key in seen:
                    continue
                seen.add(key)
                cases.append({
                    "case_type": "security",
                    "category": category,
                    "parameter": name,
                    "value": payload,
                    "expected": "服务端拒绝并返回 4xx/5xx 且不泄露堆栈；无 SQL 注入/XSS 回显",
                })
    return cases
