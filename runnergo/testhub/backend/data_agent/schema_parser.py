from __future__ import annotations

import json
import re
from typing import Any

import requests
import yaml


def load_swagger_doc(swagger_url: str = '', swagger_doc: dict | str | None = None) -> dict:
    if isinstance(swagger_doc, dict) and swagger_doc:
        return swagger_doc
    if isinstance(swagger_doc, str) and swagger_doc.strip():
        text = swagger_doc.strip()
        try:
            return json.loads(text)
        except ValueError:
            return yaml.safe_load(text) or {}
    if not swagger_url:
        raise ValueError('需要 swagger_url 或 swagger_doc')
    response = requests.get(swagger_url, timeout=15)
    response.raise_for_status()
    text = response.text
    try:
        return response.json()
    except ValueError:
        return yaml.safe_load(text) or {}


def split_api(api: str) -> tuple[str | None, str]:
    match = re.match(r'^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(.+)$', str(api or '').strip(), re.I)
    if match:
        return match.group(1).upper(), match.group(2).rstrip('/')
    return None, str(api or '').strip().rstrip('/')


def resolve_api(doc: dict, api: str) -> dict:
    wanted_method, wanted_path = split_api(api)
    paths = doc.get('paths') or {}
    for path, methods in paths.items():
        if path.rstrip('/') != wanted_path and not path.rstrip('/').endswith(wanted_path):
            continue
        method = (wanted_method or next(iter(methods.keys()), 'get')).lower()
        operation = methods.get(method) or methods.get(method.upper())
        if not operation:
            continue
        return _operation_to_schema(path, method.upper(), operation)
    raise ValueError(f'在 OpenAPI 文档中未找到接口：{api}')


def _operation_to_schema(path: str, method: str, operation: dict) -> dict:
    parameters: list[dict[str, Any]] = []
    for item in operation.get('parameters') or []:
        schema = item.get('schema') or {}
        parameters.append({
            'name': item.get('name') or '',
            'in': item.get('in') or 'query',
            'required': bool(item.get('required')),
            'type': schema.get('type') or item.get('type') or 'string',
            'format': schema.get('format') or item.get('format') or '',
            'schema': schema,
        })
    request_body = operation.get('requestBody') or {}
    content = request_body.get('content') or {}
    schema = (content.get('application/json') or content.get('*/*') or {}).get('schema') or {}
    for name, prop in (schema.get('properties') or {}).items():
        parameters.append({
            'name': name,
            'in': 'body',
            'required': name in (schema.get('required') or []),
            'type': prop.get('type') or 'string',
            'format': prop.get('format') or '',
            'schema': prop,
        })
    return {
        'path': path,
        'method': method,
        'summary': operation.get('summary') or operation.get('operationId') or '',
        'parameters': [item for item in parameters if item.get('name')],
    }


def infer_parameter_type(name: str, declared_type: str = '', declared_format: str = '') -> str:
    lowered = str(name or '').lower()
    dtype = str(declared_type or '').lower()
    dformat = str(declared_format or '').lower()
    if dtype in {'integer', 'number'} or dformat in {'int32', 'int64', 'float', 'double'}:
        return 'number'
    if dtype == 'boolean':
        return 'boolean'
    if dtype == 'array':
        return 'array'
    if dformat in {'date', 'date-time'} or 'date' in lowered or 'time' in lowered:
        return 'date'
    if 'email' in lowered:
        return 'email'
    if 'phone' in lowered or 'mobile' in lowered:
        return 'phone'
    if 'password' in lowered or 'pwd' in lowered:
        return 'password'
    if 'username' in lowered or 'account' in lowered or 'login' in lowered:
        return 'username'
    if lowered == 'id' or lowered.endswith('_id') or lowered.endswith('id'):
        return 'id'
    if 'url' in lowered or 'uri' in lowered or 'link' in lowered:
        return 'url'
    return 'string'
