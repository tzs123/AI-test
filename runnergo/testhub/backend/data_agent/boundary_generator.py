from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .schema_parser import infer_parameter_type


RULE_PATH = Path(__file__).resolve().parent / 'rules' / 'boundary.json'


def load_boundary_rules() -> dict:
    return json.loads(RULE_PATH.read_text(encoding='utf-8'))


def generate_boundary_cases(parameters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rules = load_boundary_rules()
    cases = []
    for param in parameters:
        name = param.get('name')
        ptype = infer_parameter_type(name, param.get('type', ''), param.get('format', ''))
        rule_key = ptype if ptype in rules else 'string'
        for code, rule in rules.get(rule_key, {}).items():
            cases.append({
                'mode': 'boundary',
                'parameter': name,
                'rule': code,
                'label': rule.get('label') or code,
                'value': rule.get('value'),
                'expected': '接口应明确拒绝、校验或安全处理，不允许服务异常。',
            })
    return cases
