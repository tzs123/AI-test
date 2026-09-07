"""边界数据生成器：基于规则库 boundary.json 按参数类型生成边界值。"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from backend.data_agent.schema_parser import infer_parameter_type


_RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules", "boundary.json")


def _load_rules() -> dict:
    with open(_RULES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _schema_type(param: dict) -> str:
    declared = str(param.get("type") or "").lower()
    if declared in {"integer", "number"}:
        return declared
    if declared in {"boolean", "array"}:
        return declared
    return "string"


def generate_boundary_data(parameters: list[dict]) -> list[dict]:
    """为每个参数生成边界用例：{parameter, value, label, expected}。"""
    rules = _load_rules()
    cases: list[dict] = []
    for param in parameters:
        name = str(param.get("name") or "")
        if not name:
            continue
        schema = param.get("schema") or {}
        ptype = infer_parameter_type(name, param.get("type", ""), param.get("format", ""))
        schema_type = _schema_type(param)
        if ptype == "date" or schema_type in {"date"} or str(param.get("format") or "").lower() in {"date", "date-time"}:
            rule_set = rules.get("date", {})
        elif schema_type == "number":
            rule_set = rules.get("number", {})
        elif schema_type == "boolean":
            rule_set = rules.get("boolean", {})
        elif schema_type == "array":
            rule_set = rules.get("array", {})
        else:
            rule_set = rules.get("string", {})
        for rule_name, rule in rule_set.items():
            value = rule.get("value")
            cases.append({
                "case_type": "boundary",
                "parameter": name,
                "value": value,
                "label": f"{rule.get('label', rule_name)}",
                "expected": "按接口校验规则处理：拒绝、截断或明确提示，不允许崩溃",
            })
    return cases
