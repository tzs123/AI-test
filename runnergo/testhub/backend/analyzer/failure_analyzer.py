from __future__ import annotations

from typing import Any

from backend.agent.analyzer import FailureAnalyzer as LegacyFailureAnalyzer


class FailureAnalyzer:
    """Failure Analyzer with repair-oriented output."""

    def analyze(
        self,
        logs: str = '',
        screenshot: str = '',
        response: dict[str, Any] | None = None,
        exception: str = '',
        **_: Any,
    ) -> dict[str, Any]:
        legacy = LegacyFailureAnalyzer().analyze(logs=f'{logs}\n{exception}', screenshot=screenshot, response=response or {})
        reason = legacy.get('reason', '')
        error_type = self._error_type(reason, logs, response or {}, exception)
        return {
            'error_type': error_type,
            'reason': reason,
            'solution': legacy.get('suggestion') or self._solution(error_type),
            'level': legacy.get('level', 'MEDIUM'),
            'auto_fixable': error_type in {'ELEMENT_CHANGED', 'WAIT_TIMEOUT', 'AUTH_TOKEN_EXPIRED'},
        }

    def _error_type(self, reason: str, logs: str, response: dict[str, Any], exception: str) -> str:
        text = f'{reason}\n{logs}\n{response}\n{exception}'.lower()
        if any(word in text for word in ['element', '元素', '定位']):
            return 'ELEMENT_CHANGED'
        if any(word in text for word in ['timeout', '超时']):
            return 'WAIT_TIMEOUT'
        if any(word in text for word in ['401', '403', 'token', 'unauthorized', 'forbidden']):
            return 'AUTH_TOKEN_EXPIRED'
        if any(word in text for word in ['500', 'exception', 'traceback']):
            return 'SERVER_ERROR'
        if any(word in text for word in ['assert', '预期', 'expected']):
            return 'ASSERTION_FAILED'
        return 'UNKNOWN'

    def _solution(self, error_type: str) -> str:
        return {
            'ELEMENT_CHANGED': '重新扫描页面 DOM/视觉元素，更新语义定位。',
            'WAIT_TIMEOUT': '增加显式等待并检查接口响应时间。',
            'AUTH_TOKEN_EXPIRED': '刷新登录态或重放登录前置步骤。',
            'SERVER_ERROR': '查看服务端日志和接口响应，定位异常栈。',
            'ASSERTION_FAILED': '核对测试预期与业务规则是否一致。',
        }.get(error_type, '补充日志、截图和接口响应后重新分析。')
