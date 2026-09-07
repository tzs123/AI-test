"""Failure Analyzer：失败分析系统。

复用 backend.agent.analyzer 的规则 + 模型分析能力，新增：
    analyze_batch  批量分析失败
    auto_fix       生成可执行的修复建议（自动修复测试）
    solve          输出标准 {error_type, reason, solution}

输入：日志 / 截图 / 接口响应 / 异常信息
输出：{error_type, reason, solution, level, category}
"""
from __future__ import annotations

import re
from typing import Any, Optional

from backend.agent.analyzer import (  # noqa: F401
    LEVELS,
    analyze_failure,
    analyze_task_failures,
)


_FIX_TEMPLATES = {
    "ELEMENT_CHANGED": {
        "error_type": "ELEMENT_CHANGED",
        "solution": "更新元素定位：改用语义定位（role/name/label）或数据属性，运行元素采集同步最新页面结构",
        "patch": "重新采集页面元素并替换用例中的 selector；启用自愈评分与歧义保护",
    },
    "ELEMENT_NOT_FOUND": {
        "error_type": "ELEMENT_NOT_FOUND",
        "solution": "检查页面加载与 iframe/弹窗遮挡，补等待或切换到正确 frame 后重试",
        "patch": "在操作前增加显式等待（wait_for_selector），确认元素在当前 DOM",
    },
    "TIMEOUT": {
        "error_type": "TIMEOUT",
        "solution": "检查被测系统性能与网络，增大超时阈值或拆分长流程用例",
        "patch": "将超时阈值从默认值提升并增加重试机制",
    },
    "SERVER_ERROR": {
        "error_type": "SERVER_ERROR",
        "solution": "查看服务端日志定位 5xx 堆栈，确认被测系统版本与测试环境一致",
        "patch": "记录复现请求（method/url/body），提交缺陷单给服务端",
    },
    "AUTH_FAILED": {
        "error_type": "AUTH_FAILED",
        "solution": "检查账号权限、登录态有效期与 Token 刷新逻辑",
        "patch": "在用例前置中刷新登录态或配置有效账号变量",
    },
    "ASSERTION_FAILED": {
        "error_type": "ASSERTION_FAILED",
        "solution": "核对业务预期与实际响应差异，确认是数据问题还是被测系统缺陷",
        "patch": "校正断言期望值或测试数据",
    },
    "NETWORK_ERROR": {
        "error_type": "NETWORK_ERROR",
        "solution": "检查被测系统是否启动、网络与代理是否正常、防火墙/白名单是否放行",
        "patch": "确认 base_url 可达并检查安全策略是否拦截目标地址",
    },
    "UNKNOWN": {
        "error_type": "UNKNOWN",
        "solution": "结合截图与 Allure 报告人工判断，或补充更多日志后重新分析",
        "patch": "补充截图/接口响应/完整日志后重新执行分析",
    },
}

_CATEGORY_TO_ERROR_TYPE = {
    "服务端异常": "SERVER_ERROR",
    "环境/网络问题": "NETWORK_ERROR",
    "元素定位问题": "ELEMENT_CHANGED",
    "权限/认证问题": "AUTH_FAILED",
    "业务断言失败": "ASSERTION_FAILED",
    "页面稳定等待超时": "TIMEOUT",
    "输入稳定性校验失败": "ASSERTION_FAILED",
    "测试数据变量未解析": "ASSERTION_FAILED",
}


def _error_type_of(analysis: dict) -> str:
    category = str(analysis.get("category") or "")
    exact = _CATEGORY_TO_ERROR_TYPE.get(category)
    if exact:
        return exact
    evidence = f"{category}\n{analysis.get('reason') or ''}".lower()
    semantic_types = (
        ("TIMEOUT", ("timeout", "超时", "等待过久")),
        ("ELEMENT_CHANGED", ("element", "locator", "selector", "元素", "定位")),
        ("NETWORK_ERROR", ("network", "connection", "网络", "连接")),
        ("AUTH_FAILED", ("auth", "token", "权限", "认证", "登录态")),
        ("SERVER_ERROR", ("server", "5xx", "服务端")),
        ("ASSERTION_FAILED", ("assert", "断言", "输入值", "测试数据")),
    )
    for error_type, keywords in semantic_types:
        if any(keyword in evidence for keyword in keywords):
            return error_type
    return "UNKNOWN"


def solve(
    *,
    logs: str = "",
    screenshots: Optional[list[str]] = None,
    api_response: str = "",
    context: str = "",
) -> dict:
    """标准化失败分析输出：{error_type, reason, solution, level, category}。"""
    analysis = analyze_failure(
        logs=logs,
        screenshots=screenshots,
        api_response=api_response,
        context=context,
    )
    error_type = _error_type_of(analysis)
    template = _FIX_TEMPLATES.get(error_type, _FIX_TEMPLATES["UNKNOWN"])
    return {
        "error_type": error_type,
        "reason": str(analysis.get("reason") or "")[:500],
        "solution": str(analysis.get("suggestion") or template["solution"])[:500],
        "level": str(analysis.get("level") or "MEDIUM"),
        "category": str(analysis.get("category") or "未知"),
        "screenshots": analysis.get("screenshots") or [],
    }


def auto_fix(analysis: dict) -> dict:
    """根据失败分析生成自动修复建议（测试脚本侧可执行的 patch 提示）。"""
    analysis = analysis or {}
    error_type = str(analysis.get("error_type") or _error_type_of(analysis))
    template = _FIX_TEMPLATES.get(error_type, _FIX_TEMPLATES["UNKNOWN"])
    return {
        "error_type": error_type,
        "reason": str(analysis.get("reason") or "")[:500],
        "solution": str(analysis.get("solution") or analysis.get("suggestion") or template["solution"])[:500],
        "patch": template["patch"],
        "auto_fixable": error_type in {"ELEMENT_CHANGED", "ELEMENT_NOT_FOUND", "TIMEOUT", "AUTH_FAILED"},
    }


def analyze_batch(failures: list[dict]) -> list[dict]:
    """批量分析失败列表（每条可含 logs/screenshots/api_response/context）。"""
    return [
        solve(
            logs=str(item.get("logs") or item.get("summary") or ""),
            screenshots=item.get("screenshots") or [],
            api_response=str(item.get("api_response") or ""),
            context=str(item.get("context") or ""),
        )
        for item in (failures or [])
    ]
