"""失败分析器：输入日志/截图/接口响应，输出失败原因与修复建议。

输出结构：
    {
        "reason": "登录按钮元素发生变化",
        "level": "HIGH",
        "suggestion": "更新元素定位",
        "category": "..."
    }
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from backend import db
from backend.llm.provider import chat_json, is_configured


LEVELS = {"HIGH", "MEDIUM", "LOW"}

_RULES = [
    {
        "category": "测试数据变量未解析",
        "level": "HIGH",
        "patterns": [
            r"\$\{[A-Z0-9_]+\}",
            r"输入值未稳定保存: 期望 ['\"]?\$\{",
            r"expected ['\"]?\$\{[A-Z0-9_]+\}",
        ],
        "suggestion": "为用例绑定测试数据变量，或在运行环境中配置 VERIFICATION_CODE 等变量；验证码类字段建议使用测试环境固定验证码或接口 Mock，不要直接录制动态占位符",
    },
    {
        "category": "输入稳定性校验失败",
        "level": "HIGH",
        "patterns": [
            r"输入值未稳定保存",
            r"fill_stability",
            r"实际 ['\"]?['\"]?$",
        ],
        "suggestion": "检查目标输入框是否只读、被验证码按钮/弹层覆盖、输入后被前端清空，必要时改用专用验证码注入或等待输入框可编辑后再填充",
    },
    {
        "category": "服务端异常",
        "level": "HIGH",
        "patterns": [r"500 Internal Server Error", r"502 Bad Gateway", r"503 Service Unavailable",
                     r"504 Gateway Timeout", r"Traceback \(most recent call last\)",
                     r"Internal Server Error", r"HttpStatus\.500", r"status.?code.?[5-9]\d\d"],
        "suggestion": "查看服务端日志定位异常堆栈，确认被测系统版本与测试环境是否一致",
    },
    {
        "category": "页面稳定等待超时",
        "level": "HIGH",
        "patterns": [
            r"页面智能等待超时",
            r"仍有 \d+ 个页面请求未完成",
            r"smart_wait.*pending_requests",
            r"已等待 \d+ms",
        ],
        "suggestion": "检查页面是否存在长轮询/未结束接口；必要时把该接口加入忽略列表、调整智能等待策略，或在断言前增加明确的业务完成信号",
    },
    {
        "category": "环境/网络问题",
        "level": "HIGH",
        "patterns": [r"Connection (refused|reset|timed out)", r"ETIMEDOUT", r"ECONNREFUSED",
                     r"ERR_CONNECTION", r"ENOTFOUND", r"timeout|超时", r"网络不可达", r"Unable to connect"],
        "suggestion": "检查被测系统是否启动、网络与代理是否正常、防火墙/白名单是否放行",
    },
    {
        "category": "元素定位问题",
        "level": "MEDIUM",
        "patterns": [r"NoSuchElement", r"element not found|元素.*(未找到|不存在|变化)",
                     r"Timeout waiting for element|等待元素超时", r"locator|定位失败", r"not visible",
                     r"ElementClickInterceptedException", r"selector.*not found"],
        "suggestion": "更新元素定位（改用语义定位/数据属性），或先运行元素采集同步最新页面结构",
    },
    {
        "category": "权限/认证问题",
        "level": "MEDIUM",
        "patterns": [r"401", r"403", r"Forbidden", r"Unauthorized", r"未授权", r"无权限", r"登录态失效", r"Token.*(expired|invalid)"],
        "suggestion": "检查账号权限、登录态有效期与 Token 刷新逻辑",
    },
    {
        "category": "业务断言失败",
        "level": "MEDIUM",
        "patterns": [r"AssertionError", r"assert .*==|断言失败", r"期望.*但|实际.*不符", r"expected .* but got",
                     r"验证码错误|密码错误|账号不存在|校验失败"],
        "suggestion": "核对业务预期与实际响应差异，确认是数据问题还是被测系统缺陷",
    },
    {
        "category": "测试代码问题",
        "level": "LOW",
        "patterns": [r"SyntaxError", r"NameError", r"AttributeError", r"TypeError", r"ImportError",
                     r"KeyError", r"IndexError"],
        "suggestion": "检查测试脚本中的变量、导入与类型使用，修正测试代码本身",
    },
]


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _rule_analysis(logs: str, api_response: str) -> dict:
    text = f"{logs or ''}\n{api_response or ''}"
    for rule in _RULES:
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in rule["patterns"]):
            return {
                "reason": f"{rule['category']}（命中特征：{text.strip().splitlines()[0][:120] if text.strip() else '无日志'}）",
                "level": rule["level"],
                "suggestion": rule["suggestion"],
                "category": rule["category"],
            }
    return {
        "reason": "未匹配到已知失败模式，需要结合截图与人工判断",
        "level": "MEDIUM",
        "suggestion": "查看 Allure 报告与截图，确认是环境、数据还是产品问题",
        "category": "未知",
    }


def analyze_failure(
    *,
    logs: str = "",
    screenshots: Optional[list[str]] = None,
    api_response: str = "",
    context: str = "",
) -> dict:
    """分析单条失败。模型可用时增强，失败回退规则。"""
    rule_result = _rule_analysis(logs, api_response)
    screenshots = screenshots or []
    if screenshots:
        rule_result["screenshots"] = screenshots
    if rule_result.get("category") != "未知":
        return rule_result
    if is_configured():
        system = (
            "你是资深测试失败分析专家。根据日志、接口响应和上下文判断失败原因。"
            "只输出 JSON：{\"reason\":\"具体失败原因\",\"level\":\"HIGH|MEDIUM|LOW\","
            "\"suggestion\":\"可执行的修复建议\",\"category\":\"问题分类\"}。"
        )
        user = (
            f"上下文：{context or '无'}\n"
            f"日志：{logs[:4000]}\n"
            f"接口响应：{api_response[:2000]}\n"
            f"截图：{json.dumps(screenshots, ensure_ascii=False)}"
        )
        try:
            data = chat_json(system, user, max_tokens=400, temperature=0.1)
            level = str(data.get("level", "")).upper()
            result = {
                "reason": str(data.get("reason") or rule_result["reason"])[:500],
                "level": level if level in LEVELS else rule_result["level"],
                "suggestion": str(data.get("suggestion") or rule_result["suggestion"])[:500],
                "category": str(data.get("category") or rule_result["category"])[:100],
            }
            if screenshots:
                result["screenshots"] = screenshots
            return result
        except Exception:  # noqa: BLE001 - 模型失败回退规则
            pass
    return rule_result


def analyze_task_failures(agent_task_id: str) -> list[dict]:
    """分析任务下全部失败执行结果，并回写 failure_analysis。"""
    rows = db.execute(
        "SELECT id, tool, target, summary, logs, api_response, screenshots, failure_analysis "
        "FROM test_execution_result WHERE agent_task_id=? AND status='failed' ORDER BY id",
        (agent_task_id,),
        fetch=True,
    ) or []
    analyses = []
    for row in rows:
        item = db.to_dict(row)
        analysis = analyze_failure(
            logs=item.get("logs") or item.get("summary") or "",
            screenshots=json.loads(item.get("screenshots") or "[]"),
            api_response=item.get("api_response") or "",
            context=f"工具={item.get('tool')} 目标={item.get('target')}",
        )
        try:
            existing = json.loads(item.get("failure_analysis") or "{}")
        except (TypeError, ValueError):
            existing = {}
        if existing.get("failure_details"):
            analysis["failure_details"] = existing["failure_details"]
        if existing.get("execution_steps"):
            analysis["execution_steps"] = existing["execution_steps"]
        if existing.get("step_counts"):
            analysis["step_counts"] = existing["step_counts"]
        analysis["execution_result_id"] = item["id"]
        db.execute(
            "UPDATE test_execution_result SET failure_analysis=? WHERE id=?",
            (json.dumps(analysis, ensure_ascii=False), item["id"]),
        )
        analyses.append(analysis)
    return analyses
