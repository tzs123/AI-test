# -*- coding: utf-8 -*-
"""AI test data orchestration across API, UI and APP automation cases."""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Tuple

from .ai_test_data import generate_ai_test_data
from .runtime_orchestration import get_case_payload, normalize_target_type, scan_case_input_fields
from .smart_data_orchestration import analyze_data_requirements


COUNT_RE = re.compile(r'(\d+)\s*(?:个|条)?\s*(用户|user|users|订单|order|orders|支付|payment|payments)', re.I)


def _safe_slug(value: Any, fallback: str) -> str:
    text = str(value or '').strip()
    if not text:
        return fallback
    tokens = re.findall(r'[A-Za-z0-9_\u4e00-\u9fff]+', text)
    return ('_'.join(tokens) or fallback)[:64]


def _runtime_fields_for_generation(requirements: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    fields = []
    seen = set()
    for index, item in enumerate(requirements or [], 1):
        field_type = item.get('fieldType') or 'string'
        label = item.get('fieldLabel') or item.get('alias') or f'字段{index}'
        name = _safe_slug(item.get('alias') or item.get('fieldKey') or label, f'field_{index}')
        if name in seen:
            name = f'{name}_{index}'
        seen.add(name)
        max_length = 11 if field_type == 'phone' else 100 if field_type in {'username', 'name'} else None
        fields.append({
            'name': name,
            'label': label,
            'type': field_type,
            'required': True,
            'unique': field_type in {'phone', 'username', 'email', 'id_card'},
            'max_length': max_length,
        })
    return fields


def _fallback_fields(scanned_fields: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    fields = []
    seen = set()
    for index, item in enumerate(scanned_fields or [], 1):
        label = item.get('element') or item.get('step') or item.get('stepId') or f'字段{index}'
        name = _safe_slug(item.get('stepId') or item.get('stepPath') or label, f'field_{index}')
        if name in seen:
            name = f'{name}_{index}'
        seen.add(name)
        fields.append({
            'name': name,
            'label': label,
            'type': 'string',
            'required': True,
            'unique': False,
            'max_length': 100,
        })
    return fields


def _parse_requested_counts(text: str) -> Dict[str, int]:
    result: Dict[str, int] = {}
    label_map = {
        '用户': 'users',
        'user': 'users',
        'users': 'users',
        '订单': 'orders',
        'order': 'orders',
        'orders': 'orders',
        '支付': 'payments',
        'payment': 'payments',
        'payments': 'payments',
    }
    for amount, label in COUNT_RE.findall(text or ''):
        result[label_map[label.lower()]] = int(amount)
    return result


def _keyword_text(*values: Any) -> str:
    return ' '.join(str(value or '') for value in values).lower()


def _relationship_chain(goal: str, case_name: str, fields: List[Dict[str, Any]]) -> Dict[str, Any]:
    text = _keyword_text(goal, case_name, ' '.join(item.get('label', '') + ' ' + item.get('name', '') for item in fields))
    nodes = []
    edges = []

    has_user = any(token in text for token in ['用户', '注册', '登录', 'user', 'buyer', 'username', 'phone'])
    has_order = any(token in text for token in ['订单', '下单', '购物', 'order', 'cart'])
    has_payment = any(token in text for token in ['支付', '付款', 'payment', 'pay'])

    if has_user or not fields:
        nodes.append({
            'key': 'user',
            'name': '用户表',
            'assetType': 'USER',
            'primaryKey': 'user_id',
            'fields': ['user_id', 'username', 'phone', 'email'],
        })
    if has_order:
        nodes.append({
            'key': 'order',
            'name': '订单表',
            'assetType': 'ORDER',
            'primaryKey': 'order_id',
            'fields': ['order_id', 'user_id', 'amount'],
        })
        edges.append({
            'from': 'user.user_id',
            'to': 'order.user_id',
            'rule': '订单.user_id 必须来自用户.user_id',
        })
    if has_payment:
        if not has_order:
            nodes.append({
                'key': 'order',
                'name': '订单表',
                'assetType': 'ORDER',
                'primaryKey': 'order_id',
                'fields': ['order_id', 'user_id', 'amount'],
            })
            edges.append({
                'from': 'user.user_id',
                'to': 'order.user_id',
                'rule': '订单.user_id 必须来自用户.user_id',
            })
        nodes.append({
            'key': 'payment',
            'name': '支付表',
            'assetType': 'PAYMENT',
            'primaryKey': 'payment_id',
            'fields': ['payment_id', 'order_id', 'amount', 'status'],
        })
        edges.append({
            'from': 'order.order_id',
            'to': 'payment.order_id',
            'rule': '支付.order_id 必须来自订单.order_id',
        })

    if not nodes:
        nodes.append({
            'key': 'case_data',
            'name': '用例输入数据',
            'assetType': 'CUSTOM',
            'primaryKey': '',
            'fields': [item['name'] for item in fields],
        })

    return {
        'nodes': nodes,
        'edges': edges,
        'topology': ' -> '.join(item['name'] for item in nodes),
    }


def _agent_plan(goal: str, case_name: str, requested_counts: Dict[str, int], chain: Dict[str, Any]) -> Dict[str, Any]:
    demands = requested_counts or {'normal_cases': 1}
    return {
        'target': goal or case_name,
        'detectedDemands': demands,
        'flow': [
            'AI Agent 分析测试目标',
            '识别接口/UI/APP 用例输入字段',
            '调用测试数据中心生成正常、边界、异常与安全数据',
            '按数据链维护用户、订单、支付等外键关系',
            '注入执行并回收资产租约',
            '分析执行结果并反馈下一轮数据修复',
        ],
        'dataChain': chain.get('topology', ''),
    }


def analyze_case_test_data(
    target_type: Any,
    case_id: Any,
    *,
    goal: str = '',
    fields: Iterable[Dict[str, Any]] | None = None,
    count: int = 1,
    include_security: bool = True,
    requested_counts: Dict[str, int] | None = None,
) -> Dict[str, Any]:
    """Analyze a case and generate audit-friendly AI test data suggestions."""
    normalized_type = normalize_target_type(target_type)
    case_id = int(case_id)
    scanned = scan_case_input_fields(normalized_type, case_id)
    case_payload, case_name = get_case_payload(normalized_type, case_id)
    smart_requirements = analyze_data_requirements(case_payload)
    generation_fields = list(fields or []) or _runtime_fields_for_generation(smart_requirements) or _fallback_fields(scanned['fields'])

    generated = generate_ai_test_data(
        interface_name=case_name,
        fields=generation_fields,
        count=count,
        include_security=include_security,
    )
    chain = _relationship_chain(goal, case_name, generated['fields'])
    counts = {
        **_parse_requested_counts(goal),
        **(requested_counts or {}),
    }

    return {
        'target': {
            'targetType': normalized_type,
            'caseId': case_id,
            'caseName': case_name,
        },
        'source': {
            **scanned,
            'smartRequirements': smart_requirements,
        },
        'generation': generated,
        'dataChain': chain,
        'agentPlan': _agent_plan(goal, case_name, counts, chain),
    }
