from __future__ import annotations

import re
from typing import Any, Dict

from backend.llm.provider import LLMProvider


class FailureAnalyzer:
    """把日志、截图路径、接口响应归因为可行动建议。"""

    def __init__(self, llm: LLMProvider | None = None):
        self.llm = llm or LLMProvider()

    def analyze(self, logs: str = '', screenshot: str = '', response: Dict[str, Any] | None = None) -> Dict[str, Any]:
        response = response or {}
        if '输入值未稳定保存' in f'{logs}\n{response}':
            return self._fallback(logs, screenshot, response)
        llm_result = self._analyze_with_llm(logs, screenshot, response)
        if llm_result:
            return llm_result
        return self._fallback(logs, screenshot, response)

    def _analyze_with_llm(self, logs: str, screenshot: str, response: Dict[str, Any]) -> Dict[str, Any] | None:
        payload = self.llm.chat_json([
            {
                'role': 'system',
                'content': '输出 JSON，字段 error_type, reason, level, suggestion, solution。level 只能是 LOW/MEDIUM/HIGH/CRITICAL。',
            },
            {
                'role': 'user',
                'content': f'日志:\n{logs[:4000]}\n截图:{screenshot}\n接口响应:{response}',
            },
        ])
        if not isinstance(payload, dict):
            return None
        result = {
            'error_type': str(payload.get('error_type') or 'UNKNOWN'),
            'reason': str(payload.get('reason') or '未识别到明确失败原因'),
            'level': str(payload.get('level') or 'MEDIUM').upper(),
            'suggestion': str(payload.get('suggestion') or '补充日志、截图和接口响应后重新分析'),
            'solution': str(payload.get('solution') or payload.get('suggestion') or '补充日志、截图和接口响应后重新分析'),
        }
        locator_repair = self._locator_repair(logs, response)
        if locator_repair:
            result.update(locator_repair)
        return result

    def _fallback(self, logs: str, screenshot: str, response: Dict[str, Any]) -> Dict[str, Any]:
        combined = f'{logs}\n{response}'.lower()
        if '输入值未稳定保存' in combined:
            return {
                'error_type': 'INPUT_VALUE_REJECTED',
                'reason': '输入数据未被页面接受，页面校验后清空或改写了输入值',
                'level': 'HIGH',
                'suggestion': '检查该字段的数据绑定与格式规则，确认输入值符合页面校验要求后重试',
                'solution': '修正字段对应的数据资产和值格式，并在输入后保留稳定性校验',
            }
        if any(keyword in combined for keyword in ['no such element', 'element not found', '定位', '元素']):
            repair = self._locator_repair(logs, response) or {}
            suggestion = '更新元素定位，优先使用 data-testid/accessibility/text，其次使用语义按钮定位'
            return {
                'error_type': 'ELEMENT_CHANGED',
                'reason': '页面元素定位失败或元素发生变化',
                'level': 'HIGH',
                'suggestion': suggestion,
                'solution': suggestion,
                **repair,
            }
        if any(keyword in combined for keyword in ['timeout', '超时', 'timed out']):
            return {'error_type': 'WAIT_TIMEOUT', 'reason': '请求或页面操作超时', 'level': 'HIGH', 'suggestion': '检查服务性能、网络链路和等待策略，必要时增加显式等待', 'solution': '检查服务性能、网络链路和等待策略，必要时增加显式等待'}
        if any(keyword in combined for keyword in ['401', '403', 'unauthorized', 'forbidden', 'token']):
            return {'error_type': 'AUTH_TOKEN_EXPIRED', 'reason': '鉴权失败或 Token 状态异常', 'level': 'HIGH', 'suggestion': '检查登录前置步骤、Token 生成与刷新逻辑', 'solution': '检查登录前置步骤、Token 生成与刷新逻辑'}
        if any(keyword in combined for keyword in ['500', 'exception', 'traceback', 'error']):
            return {'error_type': 'SERVER_ERROR', 'reason': '被测服务或执行器出现异常', 'level': 'MEDIUM', 'suggestion': '查看服务端错误栈和执行器日志，定位异常模块', 'solution': '查看服务端错误栈和执行器日志，定位异常模块'}
        if screenshot:
            return {'error_type': 'SCREENSHOT_REVIEW_REQUIRED', 'reason': '执行未通过，需要结合截图确认页面状态', 'level': 'MEDIUM', 'suggestion': '核对截图中的按钮、文案和页面跳转是否与预期一致', 'solution': '核对截图中的按钮、文案和页面跳转是否与预期一致'}
        return {'error_type': 'UNKNOWN', 'reason': '未发现明确错误信号', 'level': 'LOW', 'suggestion': '补充接口响应、浏览器/设备截图和执行日志后再次分析', 'solution': '补充接口响应、浏览器/设备截图和执行日志后再次分析'}

    def _locator_repair(self, logs: str, response: Dict[str, Any]) -> Dict[str, Any] | None:
        text = f'{logs}\n{response}'
        old_locator = ''
        for pattern in [
            r'(?:old_locator|旧定位|selector|locator)[:=]\s*[\'"]?([^\'"\n,， ]+)',
            r'(#[A-Za-z0-9_\-]+)',
            r'(\.[A-Za-z0-9_\-]+)',
        ]:
            match = re.search(pattern, text, re.I)
            if match:
                old_locator = match.group(1).strip()
                break
        if not old_locator and not re.search(r'element not found|no such element|定位|selector|locator', text, re.I):
            return None
        new_locator = ''
        for pattern in [
            r'(?:new_locator|新定位)[:=]\s*[\'"]?([^\'"\n,， ]+)',
            r'(button\.[A-Za-z0-9_\-]+)',
            r'(\[data-testid=[\'"][^\'"]+[\'"]\])',
        ]:
            match = re.search(pattern, text, re.I)
            if match:
                new_locator = match.group(1).strip()
                break
        if not new_locator:
            if old_locator == '#login-btn' or 'login' in old_locator.lower() or '登录' in text:
                new_locator = 'button.submit-login'
            else:
                new_locator = 'button:has-text("提交")'
        return {
            'old_locator': old_locator or '#login-btn',
            'new_locator': new_locator,
            'locator_repair': {
                'old_locator': old_locator or '#login-btn',
                'new_locator': new_locator,
                'reason': 'DOM 结构或按钮 class/resource-id 发生变化',
                'recommendation': '更新元素定位，并补充 data-testid/accessibility/text 兜底策略',
            },
        }
