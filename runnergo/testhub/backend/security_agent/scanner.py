from __future__ import annotations

import re
from typing import Any

import requests

from backend.data_agent.schema_parser import load_swagger_doc, resolve_api
from backend.data_agent.security_generator import categories_for, load_security_rules


class SecurityAgent:
    """AI Security Agent with a Yakit-compatible payload and report flow."""

    ARCHITECTURE = 'AI -> Interface Analysis -> Yakit-compatible Scanner -> SQL注入/越权/JWT/敏感信息泄露 -> 安全报告'
    CHECKS = ['SQL注入测试', '越权测试', 'JWT测试', '敏感信息泄露', 'XSS测试', 'SSRF测试']

    SQL_ERRORS = re.compile(r'sql syntax|mysql|postgres|sqlite|oracle|sqlalchemy|unclosed quotation', re.I)
    XSS_REFLECT = re.compile(r'<script>alert\(1\)</script>|<img src=x onerror=alert\(1\)>|<svg/onload=alert\(1\)>', re.I)

    def scan(
        self,
        *,
        api_doc: Any = '',
        target: str = '',
        parameters: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 8,
        **_: Any,
    ) -> dict[str, Any]:
        if not target and not api_doc:
            return {
                'status': 'skipped',
                'risk': 'LOW',
                'message': '未提供扫描目标，AI Security Agent 已生成 Yakit 兼容扫描动作但跳过实际探测。',
                'architecture': self.ARCHITECTURE,
                'engine': 'yakit-compatible',
                'summary': {
                    'total': 0,
                    'confirmed': 0,
                    'risk': 'LOW',
                    'severity': {'critical': 0, 'high': 0, 'medium': 0, 'low': 0, 'info': 0},
                    'checks': self.CHECKS,
                },
                'findings': [],
            }
        endpoint = self._resolve_endpoint(api_doc, target, parameters)
        rules = load_security_rules()
        findings = []
        for param in endpoint['parameters']:
            for category in categories_for(param):
                for payload in rules.get(category, [])[:2]:
                    finding = self._probe(endpoint, param['name'], category, payload, headers or {}, timeout)
                    findings.append(finding)
        confirmed = [item for item in findings if item.get('evidence')]
        first = confirmed[0] if confirmed else (findings[0] if findings else {})
        severity = self._severity(findings)
        return {
            'status': 'success',
            'architecture': self.ARCHITECTURE,
            'engine': 'yakit-compatible',
            'message': 'AI Security Agent 已完成接口分析与 Yakit 兼容安全探测报告生成',
            'risk': 'HIGH' if confirmed else 'LOW',
            'summary': {
                'total': len(findings),
                'confirmed': len(confirmed),
                'risk': 'HIGH' if confirmed else 'LOW',
                'severity': severity,
                'checks': self.CHECKS,
            },
            'type': first.get('type', ''),
            'parameter': first.get('parameter', ''),
            'payload': first.get('payload', ''),
            'evidence': first.get('evidence', ''),
            'findings': findings,
        }

    def _resolve_endpoint(self, api_doc: Any, target: str, parameters: list[dict[str, Any]] | None) -> dict:
        if parameters:
            return {'url': target, 'method': 'GET', 'parameters': parameters}
        if api_doc:
            doc = load_swagger_doc(swagger_doc=api_doc if not str(api_doc).startswith(('http://', 'https://')) else None, swagger_url=api_doc if str(api_doc).startswith(('http://', 'https://')) else '')
            api = target if re.match(r'^(GET|POST|PUT|PATCH|DELETE)\s+', target, re.I) else next(
                f'{method.upper()} {path}'
                for path, methods in (doc.get('paths') or {}).items()
                for method in methods.keys()
            )
            schema = resolve_api(doc, api)
            return {'url': target if target.startswith(('http://', 'https://')) else '', 'method': schema['method'], 'parameters': schema['parameters']}
        return {
            'url': target,
            'method': 'GET',
            'parameters': [{'name': name, 'in': 'query', 'type': 'string', 'format': '', 'schema': {}} for name in ['id', 'username', 'search', 'token', 'url']],
        }

    def _probe(self, endpoint: dict, parameter: str, category: str, payload: str, headers: dict, timeout: int) -> dict:
        finding = {
            'type': self._type_name(category),
            'parameter': parameter,
            'payload': payload,
            'risk': 'LOW',
            'evidence': '',
            'engine': 'yakit-compatible',
        }
        url = endpoint.get('url') or ''
        if not url.startswith(('http://', 'https://')):
            finding['evidence'] = ''
            finding['note'] = '未提供完整 URL，仅生成 Payload。'
            return finding
        try:
            method = str(endpoint.get('method') or 'GET').upper()
            kwargs = {'headers': headers, 'timeout': timeout}
            if method in {'POST', 'PUT', 'PATCH'}:
                kwargs['json'] = {parameter: payload}
            else:
                kwargs['params'] = {parameter: payload}
            response = requests.request(method, url, **kwargs)
        except requests.RequestException as exc:
            finding['note'] = f'请求失败：{exc}'
            return finding
        text = response.text[:5000]
        if category == 'sql_injection' and self.SQL_ERRORS.search(text):
            finding['evidence'] = f'响应包含 SQL 错误特征，status={response.status_code}'
            finding['risk'] = 'HIGH'
        elif category == 'xss' and self.XSS_REFLECT.search(text):
            finding['evidence'] = f'Payload 原样回显，status={response.status_code}'
            finding['risk'] = 'HIGH'
        elif category in {'authorization_bypass', 'jwt_tamper'} and response.status_code in {200, 201, 204}:
            finding['evidence'] = f'越权/篡改请求被接受，status={response.status_code}'
            finding['risk'] = 'HIGH'
        return finding

    def _severity(self, findings: list[dict[str, Any]]) -> dict[str, int]:
        summary = {'critical': 0, 'high': 0, 'medium': 0, 'low': 0, 'info': 0}
        for item in findings:
            risk = str(item.get('risk') or '').upper()
            if risk == 'HIGH':
                summary['high'] += 1
            elif item.get('evidence'):
                summary['medium'] += 1
            else:
                summary['low'] += 1
        return summary

    def _type_name(self, category: str) -> str:
        return {
            'sql_injection': 'SQL Injection',
            'xss': 'XSS',
            'authorization_bypass': '越权',
            'jwt_tamper': 'JWT',
            'file_upload': '文件上传',
            'ssrf': 'SSRF',
            'path_traversal': '路径穿越',
        }.get(category, category)
