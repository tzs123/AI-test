from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .schema_parser import infer_parameter_type


RULE_PATH = Path(__file__).resolve().parent / 'rules' / 'security.json'


def load_security_rules() -> dict:
    return json.loads(RULE_PATH.read_text(encoding='utf-8'))


def categories_for(param: dict[str, Any]) -> list[str]:
    name = str(param.get('name') or '').lower()
    if re.search(r'token|jwt|authorization|auth', name):
        return ['jwt_tamper', 'authorization_bypass']
    if re.search(r'url|uri|link|callback|redirect|webhook|host', name):
        return ['ssrf', 'path_traversal']
    if re.search(r'file|upload|avatar|image|attachment', name):
        return ['file_upload', 'path_traversal']
    if re.search(r'role|permission|user_id|userid|is_admin', name):
        return ['authorization_bypass']
    ptype = infer_parameter_type(name, param.get('type', ''), param.get('format', ''))
    if ptype in {'id', 'username', 'email', 'phone', 'string'}:
        return ['sql_injection', 'xss']
    return ['sql_injection', 'xss']


def generate_security_cases(parameters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rules = load_security_rules()
    cases = []
    for param in parameters:
        name = param.get('name')
        for category in categories_for(param):
            for payload in rules.get(category, [])[:4]:
                cases.append({
                    'mode': 'security',
                    'category': category,
                    'parameter': name,
                    'value': payload,
                    'expected': '服务端拒绝攻击载荷，不泄露堆栈，不产生未授权副作用。',
                })
    return cases
