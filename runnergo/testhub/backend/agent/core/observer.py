from __future__ import annotations

from typing import Any, Dict


class Observer:
    """Turns raw tool output into Agent observations."""

    SUCCESS_VALUES = {'success', 'ok', 'passed', 'pass'}

    def observe(self, result: Dict[str, Any] | None) -> Dict[str, Any]:
        result = result or {}
        status = str(result.get('status') or '').lower()
        skipped = status == 'skipped'
        not_applicable = status == 'not_applicable'
        success = status in self.SUCCESS_VALUES or not status
        facts = {}
        for key in ['asset_id', 'data_count', 'count', 'case_ids', 'execution_result_id', 'risk', 'type', 'parameter']:
            if key in result:
                facts[key] = result.get(key)
        message = result.get('message') or result.get('reason') or result.get('error') or '工具执行完成'
        return {
            'success': success or not_applicable,
            'status': 'not_applicable' if not_applicable else 'skipped' if skipped else 'success' if success else 'failed',
            'message': str(message),
            'facts': facts,
            'raw': result,
        }
