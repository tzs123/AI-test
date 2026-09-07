# -*- coding: utf-8 -*-
"""AI-style test data generation for business API scenarios.

The generator is deterministic and auditable by design. It can later be
replaced or enriched by an LLM, while keeping the same API contract.
"""
from __future__ import annotations

import copy
import random
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from apps.data_factory.tools.test_data_tools import TestDataTools


SQL_INJECTION_PROBES = [
    "' OR '1'='1",
    "admin'--",
    "'; SELECT 1; --",
]

SCRIPT_PROBES = [
    '<script>alert(1)</script>',
    '"><img src=x onerror=alert(1)>',
]

LONG_TEXT = 'A' * 260


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    data_type: str
    required: bool = True
    unique: bool = False
    max_length: Optional[int] = None


DEFAULT_REGISTRATION_FIELDS = [
    FieldSpec('phone', '手机号', 'phone', required=True, unique=True, max_length=11),
    FieldSpec('name', '姓名', 'name', required=True, max_length=20),
    FieldSpec('id_card', '身份证', 'id_card', required=False, unique=True, max_length=18),
    FieldSpec('account', '账号', 'account', required=True, unique=True, max_length=32),
    FieldSpec('password', '密码', 'password', required=True, max_length=64),
]


def _normalize_text(value: Any) -> str:
    return str(value or '').strip().lower()


def _infer_data_type(name: str, label: str, raw_type: str = '') -> str:
    text = f'{name} {label} {raw_type}'.lower()
    if any(token in text for token in ['phone', 'mobile', '手机号', '手机']):
        return 'phone'
    if any(token in text for token in ['id_card', 'idcard', 'identity', '身份证']):
        return 'id_card'
    if any(token in text for token in ['name', '姓名', '用户名', 'user_name']):
        return 'name'
    if any(token in text for token in ['account', '账号', 'user', 'login']):
        return 'account'
    if any(token in text for token in ['password', 'pwd', '密码']):
        return 'password'
    if any(token in text for token in ['email', '邮箱']):
        return 'email'
    if any(token in text for token in ['age', '年龄']):
        return 'number'
    return raw_type or 'string'


def _field_from_dict(item: Dict[str, Any]) -> FieldSpec:
    name = str(item.get('name') or item.get('key') or item.get('field') or '').strip()
    label = str(item.get('label') or item.get('title') or name).strip()
    data_type = _infer_data_type(name, label, str(item.get('type') or item.get('data_type') or ''))
    max_length = item.get('max_length') or item.get('maxLength')
    try:
        max_length = int(max_length) if max_length is not None else None
    except (TypeError, ValueError):
        max_length = None
    return FieldSpec(
        name=name,
        label=label or name,
        data_type=data_type,
        required=bool(item.get('required', True)),
        unique=bool(item.get('unique', data_type in {'phone', 'id_card', 'account'})),
        max_length=max_length,
    )


def infer_fields(interface_name: str, fields: Optional[Iterable[Dict[str, Any]]] = None) -> List[FieldSpec]:
    """Infer business fields from the user supplied interface metadata."""
    normalized_fields = [
        field for field in (_field_from_dict(item) for item in (fields or []))
        if field.name
    ]
    if normalized_fields:
        return normalized_fields

    text = _normalize_text(interface_name)
    if any(token in text for token in ['注册', 'register', 'signup', 'sign up']):
        return DEFAULT_REGISTRATION_FIELDS
    return [
        FieldSpec('phone', '手机号', 'phone', required=True, unique=True, max_length=11),
        FieldSpec('name', '姓名', 'name', required=True, max_length=20),
    ]


def _tool_result(result: Dict[str, Any], fallback: Any) -> Any:
    if isinstance(result, dict) and result.get('success'):
        return result.get('result', fallback)
    return fallback


def _normal_value(field: FieldSpec, sequence: int) -> Any:
    if field.data_type == 'phone':
        generated = _tool_result(TestDataTools.generate_chinese_phone(), '')
        phone = re.sub(r'\D', '', str(generated))
        if len(phone) == 11 and phone.startswith('1'):
            return phone
        return f'138{sequence:08d}'[-11:]
    if field.data_type == 'name':
        return _tool_result(TestDataTools.generate_chinese_name(), random.choice(['张三', '李四', '王五']))
    if field.data_type == 'id_card':
        return _tool_result(TestDataTools.generate_id_card(), '110101199001010011')
    if field.data_type == 'account':
        return f'user_{uuid.uuid4().hex[:10]}'
    if field.data_type == 'password':
        return f'Rg@{uuid.uuid4().hex[:8]}9'
    if field.data_type == 'email':
        return _tool_result(TestDataTools.generate_chinese_email(), f'user{sequence}@example.com')
    if field.data_type == 'number':
        return sequence
    return f'test_{field.name}_{sequence}'


def _normal_payload(fields: List[FieldSpec], sequence: int) -> Dict[str, Any]:
    return {field.name: _normal_value(field, sequence) for field in fields}


def _invalid_value(field: FieldSpec, kind: str, normal: Dict[str, Any]) -> Any:
    if kind == 'empty':
        return ''
    if kind == 'duplicate':
        return normal.get(field.name)
    if kind == 'too_long':
        length = max((field.max_length or 128) + 1, 260)
        return '超长字符' + ('A' * length)
    if kind == 'sql':
        return random.choice(SQL_INJECTION_PROBES)
    if kind == 'script':
        return random.choice(SCRIPT_PROBES)
    if field.data_type == 'phone':
        return '12345'
    if field.data_type == 'id_card':
        return '11010119900101999X'
    if field.data_type == 'email':
        return 'invalid-email'
    if field.data_type == 'number':
        return -1
    return '非法值'


def _case(
    case_type: str,
    title: str,
    payload: Dict[str, Any],
    expected: str,
    tags: List[str],
    field: Optional[FieldSpec] = None,
) -> Dict[str, Any]:
    item = {
        'id': uuid.uuid4().hex,
        'type': case_type,
        'title': title,
        'payload': payload,
        'expected': expected,
        'tags': tags,
    }
    if field:
        item['field'] = field.name
        item['field_label'] = field.label
    return item


def generate_ai_test_data(
    interface_name: str,
    fields: Optional[Iterable[Dict[str, Any]]] = None,
    count: int = 1,
    include_security: bool = True,
) -> Dict[str, Any]:
    """Generate normal, abnormal and injection-oriented API test data."""
    count = max(1, min(int(count or 1), 20))
    field_specs = infer_fields(interface_name, fields)
    normal_payloads = [_normal_payload(field_specs, index + 1) for index in range(count)]
    base_payload = copy.deepcopy(normal_payloads[0])

    cases: List[Dict[str, Any]] = [
        _case(
            'normal',
            f'{interface_name or "接口"}-正常数据-{index + 1}',
            payload,
            '接口返回成功，业务数据创建或更新成功',
            ['normal', 'ai-generated'],
        )
        for index, payload in enumerate(normal_payloads)
    ]

    for field in field_specs:
        if field.required:
            payload = copy.deepcopy(base_payload)
            payload[field.name] = _invalid_value(field, 'empty', base_payload)
            cases.append(_case(
                'abnormal',
                f'空{field.label}',
                payload,
                f'接口拒绝请求并提示{field.label}必填',
                ['abnormal', 'required'],
                field,
            ))

        if field.data_type in {'phone', 'id_card', 'email', 'number'}:
            payload = copy.deepcopy(base_payload)
            payload[field.name] = _invalid_value(field, 'invalid', base_payload)
            cases.append(_case(
                'abnormal',
                f'非法{field.label}',
                payload,
                f'接口拒绝请求并提示{field.label}格式错误',
                ['abnormal', 'format'],
                field,
            ))

        if field.unique:
            payload = copy.deepcopy(base_payload)
            payload[field.name] = _invalid_value(field, 'duplicate', base_payload)
            cases.append(_case(
                'abnormal',
                f'重复{field.label}',
                payload,
                f'接口拒绝请求并提示{field.label}已存在',
                ['abnormal', 'duplicate'],
                field,
            ))

        if field.max_length:
            payload = copy.deepcopy(base_payload)
            payload[field.name] = _invalid_value(field, 'too_long', base_payload)
            cases.append(_case(
                'abnormal',
                f'{field.label}超长字符',
                payload,
                f'接口拒绝请求并提示{field.label}长度超限',
                ['abnormal', 'boundary'],
                field,
            ))

    if include_security:
        injectable_fields = [
            field for field in field_specs
            if field.data_type in {'string', 'name', 'account', 'email', 'password'}
        ] or field_specs[:1]
        for field in injectable_fields:
            sql_payload = copy.deepcopy(base_payload)
            sql_payload[field.name] = _invalid_value(field, 'sql', base_payload)
            cases.append(_case(
                'injection',
                f'{field.label}SQL字符注入',
                sql_payload,
                '接口不执行注入内容，返回参数校验失败或安全拦截结果',
                ['security', 'sql-injection'],
                field,
            ))

            script_payload = copy.deepcopy(base_payload)
            script_payload[field.name] = _invalid_value(field, 'script', base_payload)
            cases.append(_case(
                'injection',
                f'{field.label}脚本字符注入',
                script_payload,
                '接口对脚本字符进行拒绝、转义或安全拦截',
                ['security', 'script-injection'],
                field,
            ))

    return {
        'interface_name': interface_name or '未命名接口',
        'fields': [
            {
                'name': field.name,
                'label': field.label,
                'type': field.data_type,
                'required': field.required,
                'unique': field.unique,
                'max_length': field.max_length,
            }
            for field in field_specs
        ],
        'cases': cases,
        'summary': {
            'normal': len([item for item in cases if item['type'] == 'normal']),
            'abnormal': len([item for item in cases if item['type'] == 'abnormal']),
            'injection': len([item for item in cases if item['type'] == 'injection']),
            'total': len(cases),
        },
    }
