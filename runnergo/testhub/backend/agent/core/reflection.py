from __future__ import annotations

from typing import Any, Dict


class ReflectionEngine:
    """Decides whether a failed observation should adjust, retry, or escalate."""

    def reflect(self, observation: Dict[str, Any], attempt: int = 1) -> Dict[str, Any]:
        if observation.get('status') == 'not_applicable':
            return {
                'decision': 'continue',
                'reason': '当前条件不触发该分支，本步骤标记为不适用并继续后续编排。',
                'retryable': False,
            }
        if observation.get('status') == 'skipped':
            return {
                'decision': 'continue',
                'reason': '工具未发现可绑定的真实测试资产，本步骤标记为 SKIPPED 并继续后续编排。',
                'retryable': False,
            }
        if observation.get('success'):
            return {
                'decision': 'continue',
                'reason': 'Observation 显示工具执行成功，可以进入下一步。',
                'retryable': False,
            }
        raw = observation.get('raw') or {}
        message = f"{observation.get('message', '')} {raw.get('error', '')}".lower()
        if attempt <= 1 and any(word in message for word in ['timeout', '超时', 'temporary', '暂时']):
            return {
                'decision': 'retry',
                'reason': '失败可能由临时超时或环境抖动导致，允许自动重试一次。',
                'retryable': True,
            }
        return {
            'decision': 'analyze_failure',
            'reason': '失败需要进入 Failure Analyzer 做归因分析。',
            'retryable': False,
        }
