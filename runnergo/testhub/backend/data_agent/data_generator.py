from __future__ import annotations

import random
from typing import Any

from .boundary_generator import generate_boundary_cases
from .schema_parser import infer_parameter_type, load_swagger_doc, resolve_api
from .security_generator import generate_security_cases


class DataAgent:
    """AI Test Data Agent for OpenAPI-driven data generation."""

    def generate_from_api(
        self,
        *,
        swagger_url: str = '',
        swagger_doc: dict | str | None = None,
        api: str = '',
        mode: list[str] | None = None,
        count: int = 1000,
        **_: Any,
    ) -> dict[str, Any]:
        modes = [str(item).lower() for item in (mode or ['normal', 'boundary', 'security']) if str(item).strip()]
        endpoint, parameters = self._resolve_or_fallback(swagger_url=swagger_url, swagger_doc=swagger_doc, api=api)
        normal_count = max(1, min(int(count or 1000), 10000))
        cases: list[dict[str, Any]] = []
        if 'normal' in modes:
            cases.extend(self._normal_cases(parameters, min(normal_count, 1000)))
        if 'boundary' in modes:
            cases.extend(generate_boundary_cases(parameters))
        if 'security' in modes:
            cases.extend(generate_security_cases(parameters))
        asset_id = f"data_agent_{endpoint['method'].lower()}_{endpoint['path'].strip('/').replace('/', '_') or 'root'}"
        return {
            'status': 'success',
            'agent': 'AI Test Data Generator',
            'modes': modes,
            'asset_id': asset_id[:80],
            'data_count': len(cases),
            'api': {'path': endpoint['path'], 'method': endpoint['method'], 'summary': endpoint.get('summary', '')},
            'schema': {'parameters': parameters},
            'cases': cases,
            'examples': {
                'normal': [case for case in cases if case.get('mode') == 'normal'][:3],
                'abnormal': self._abnormal_examples(parameters)[:5],
                'boundary': [case for case in cases if case.get('mode') == 'boundary'][:3],
                'security': [case for case in cases if case.get('mode') == 'security'][:3],
            },
        }

    def _resolve_or_fallback(
        self,
        *,
        swagger_url: str,
        swagger_doc: dict | str | None,
        api: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        try:
            doc = load_swagger_doc(swagger_url=swagger_url, swagger_doc=swagger_doc)
            if not api:
                api = self._first_api(doc)
            endpoint = resolve_api(doc, api)
            parameters = endpoint.get('parameters') or []
            if parameters:
                return endpoint, parameters
        except Exception:
            pass
        return self._fallback_endpoint(api)

    def _first_api(self, doc: dict[str, Any]) -> str:
        for path, methods in (doc.get('paths') or {}).items():
            for method in methods.keys():
                return f'{str(method).upper()} {path}'
        raise ValueError('OpenAPI 文档中没有可用接口')

    def _fallback_endpoint(self, api: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        api_text = str(api or '').strip()
        if not api_text:
            api_text = 'POST /orders'
        method, _, path = api_text.partition(' ')
        method = method.upper() if path else 'POST'
        path = path or '/orders'
        parameters = [
            {'name': 'order_id', 'in': 'body', 'required': True, 'type': 'string', 'format': '', 'schema': {}},
            {'name': 'amount', 'in': 'body', 'required': True, 'type': 'number', 'format': '', 'schema': {}},
            {'name': 'user', 'in': 'body', 'required': True, 'type': 'string', 'format': '', 'schema': {}},
            {'name': 'product', 'in': 'body', 'required': True, 'type': 'string', 'format': '', 'schema': {}},
            {'name': 'remark', 'in': 'body', 'required': False, 'type': 'string', 'format': '', 'schema': {}},
        ]
        return {
            'path': path,
            'method': method,
            'summary': 'Fallback order data generation endpoint',
            'parameters': parameters,
        }, parameters

    def _normal_cases(self, parameters: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
        rows = []
        for index in range(1, count + 1):
            row = {}
            for param in parameters:
                row[param['name']] = self._value_for(param, index)
            rows.append({'mode': 'normal', 'index': index, 'data': row})
        return rows

    def _abnormal_examples(self, parameters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        field = parameters[0]['name'] if parameters else 'value'
        return [
            {'mode': 'abnormal', 'parameter': field, 'value': None, 'description': '空值'},
            {'mode': 'abnormal', 'parameter': field, 'value': -1, 'description': '非法负数'},
            {'mode': 'abnormal', 'parameter': field, 'value': "1' OR '1'='1", 'description': 'SQL 字符'},
            {'mode': 'abnormal', 'parameter': field, 'value': '超长字符' * 64, 'description': '超长字符'},
            {'mode': 'abnormal', 'parameter': field, 'value': '测试🚀Unicode', 'description': 'Unicode 字符'},
        ]

    def _value_for(self, param: dict[str, Any], index: int) -> Any:
        name = str(param.get('name') or '')
        ptype = infer_parameter_type(name, param.get('type', ''), param.get('format', ''))
        if ptype == 'username':
            return f'test{index:03d}'
        if ptype == 'phone':
            return f'138{random.randint(10000000, 99999999)}'
        if ptype == 'email':
            return f'test{index:03d}@test.com'
        if ptype == 'password':
            return f'Passw0rd@{index:04d}'
        if ptype == 'number':
            return index
        if ptype == 'boolean':
            return True
        if ptype == 'array':
            return [f'item_{index}']
        if ptype == 'date':
            return '2026-08-11'
        if ptype == 'id':
            return index
        if ptype == 'url':
            return f'https://example.com/{index}'
        return f'value_{index:03d}'
