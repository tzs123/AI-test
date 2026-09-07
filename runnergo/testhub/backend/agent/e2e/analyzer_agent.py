from __future__ import annotations

import json
import re
from typing import Any

from backend.analyzer.failure_analyzer import FailureAnalyzer
from backend.llm.provider import LLMProvider


class AnalyzerAgent:
    """Convert E2E evidence into a stable P0-P3 developer bug report."""

    def __init__(self, *, llm: LLMProvider | None = None):
        self.llm = llm or LLMProvider()

    def analyze(
        self,
        *,
        task_name: str,
        scenario: dict[str, Any] | None = None,
        steps: list[dict[str, Any]] | None = None,
        screenshot: str = '',
        console_logs: list[dict[str, Any]] | None = None,
        network_logs: list[dict[str, Any]] | None = None,
        device_log: str = '',
        stack_trace: str = '',
        error: str = '',
    ) -> dict[str, Any]:
        evidence = {
            'console_logs': (console_logs or [])[-100:],
            'network_logs': (network_logs or [])[-100:],
            'device_log': str(device_log or '')[-20000:],
            'stack_trace': str(stack_trace or '')[-20000:],
            'error': str(error or '')[:10000],
        }
        combined = json.dumps(evidence, ensure_ascii=False, default=str)
        rule_result = FailureAnalyzer().analyze(
            logs=combined,
            screenshot=screenshot,
            response={'steps': steps or []},
            exception=error or stack_trace,
        )
        reproduction_steps = self._reproduction_steps(steps or [])
        fallback = {
            'title': self._title(task_name, rule_result),
            'severity': self._priority(combined, rule_result),
            'reason': str(rule_result.get('reason') or '未识别到明确根因'),
            'root_cause': str(rule_result.get('reason') or '未识别到明确根因'),
            'reproduction_steps': reproduction_steps,
            'fix_suggestion': str(rule_result.get('solution') or '结合完整日志和服务端链路继续排查'),
            'error_type': str(rule_result.get('error_type') or 'UNKNOWN'),
            'screenshot': screenshot,
            'evidence_summary': self._evidence_summary(evidence),
            'analyzer': 'rules',
        }
        enhanced = self._enhance_with_llm(
            task_name=task_name,
            scenario=scenario or {},
            evidence=evidence,
            fallback=fallback,
        )
        return enhanced or fallback

    def _enhance_with_llm(self, *, task_name: str, scenario: dict[str, Any], evidence: dict[str, Any], fallback: dict[str, Any]):
        if not self.llm.enabled:
            return None
        payload = self.llm.chat_json([
            {
                'role': 'system',
                'content': (
                    '你是资深测试故障分析 Agent。只输出 JSON：title,severity,reason,root_cause,'
                    'reproduction_steps,fix_suggestion,error_type。severity 只能是 P0/P1/P2/P3。'
                    '不得编造证据，不得泄漏凭据。'
                ),
            },
            {
                'role': 'user',
                'content': json.dumps({
                    'task_name': task_name,
                    'scenario': scenario,
                    'evidence': evidence,
                    'rule_analysis': fallback,
                }, ensure_ascii=False, default=str)[:30000],
            },
        ], timeout=30, temperature=0.1)
        if not isinstance(payload, dict):
            return None
        severity = str(payload.get('severity') or fallback['severity']).upper()
        if severity not in {'P0', 'P1', 'P2', 'P3'}:
            severity = fallback['severity']
        reproduction = payload.get('reproduction_steps')
        if not isinstance(reproduction, list) or not reproduction:
            reproduction = fallback['reproduction_steps']
        return {
            **fallback,
            'title': str(payload.get('title') or fallback['title'])[:300],
            'severity': severity,
            'reason': str(payload.get('reason') or fallback['reason'])[:5000],
            'root_cause': str(payload.get('root_cause') or payload.get('reason') or fallback['root_cause'])[:5000],
            'reproduction_steps': [str(item)[:1000] for item in reproduction[:50]],
            'fix_suggestion': str(payload.get('fix_suggestion') or fallback['fix_suggestion'])[:5000],
            'error_type': str(payload.get('error_type') or fallback['error_type'])[:160],
            'analyzer': 'llm+rules',
        }

    def _priority(self, combined: str, analysis: dict[str, Any]) -> str:
        text = f'{combined}\n{analysis}'.casefold()
        if any(token in text for token in ('数据丢失', 'data loss', '重复扣款', '资金损失', '全站不可用')):
            return 'P0'
        if any(token in text for token in ('crash', 'anr', 'fatal', '500', '502', '503', '504', '支付失败', 'server_error')):
            return 'P1'
        if any(token in text for token in ('timeout', 'element', '定位', 'assert', 'expected', '401', '403')):
            return 'P2'
        return 'P3'

    def _title(self, task_name: str, analysis: dict[str, Any]) -> str:
        error_type = str(analysis.get('error_type') or 'E2E')
        return f'{task_name or "E2E 测试"}：{error_type} 失败'[:300]

    def _reproduction_steps(self, steps: list[dict[str, Any]]) -> list[str]:
        rows = []
        for index, step in enumerate(steps, 1):
            if not isinstance(step, dict):
                continue
            name = step.get('name') or step.get('step_name') or step.get('action') or f'步骤 {index}'
            rows.append(f'{index}. {name}')
            if str(step.get('status') or '').lower() in {'failed', 'error'}:
                break
        return rows or ['1. 使用报告记录的环境与测试数据启动任务', '2. 执行失败步骤并观察错误证据']

    def _evidence_summary(self, evidence: dict[str, Any]) -> dict[str, Any]:
        network = evidence['network_logs']
        return {
            'console_error_count': len([
                item for item in evidence['console_logs']
                if str(item.get('type') or '').lower() in {'error', 'assert'}
            ]),
            'http_error_count': len([
                item for item in network
                if int(item.get('status') or 0) >= 400
            ]),
            'has_device_log': bool(evidence['device_log']),
            'has_stack_trace': bool(evidence['stack_trace']),
        }
