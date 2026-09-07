"""ReAct 规划：Agent Planner -> Action -> Observation -> Reflection -> Retry。"""
from __future__ import annotations

import re

from backend.agent.planner import (  # noqa: F401
    build_test_dimension_plan,
    build_workflow_plan,
    decompose_requirement,
    _rule_dimension_plan,
    _rule_step_plan,
)

_SECURITY_TERMS = re.compile(r"安全|漏洞|注入|sql|xss|越权|jwt|ssrf|扫描", re.IGNORECASE)
_BROWSER_TERMS = re.compile(r"页面|浏览器|视觉|点击|输入框|登录页|购物车页面|表单", re.IGNORECASE)
_CASE_TERMS = re.compile(r"贷款|申请|额度|车融|全流程|完整流程|录制用例|业务流程|跑通|贷款申请", re.IGNORECASE)


def discover_cases(project_id: str = "default", requirement: str = "") -> list[dict]:
    """从项目记忆读取真实用例，按需求关键词匹配。"""
    try:
        from backend.agent.core.memory import recall_project
        cases = recall_project(project_id or "default", "fact:case", [])
    except Exception:  # noqa: BLE001
        cases = []
    if not isinstance(cases, list):
        return []
    matched = []
    requirement = str(requirement or "").lower()
    for item in cases:
        case = item.get("content") if isinstance(item, dict) else {}
        if not isinstance(case, dict) or not case.get("name"):
            continue
        if requirement:
            haystack = " ".join([
                str(case.get("name", "")),
                str(case.get("title", "")),
                str(case.get("flow", "")),
                str(case.get("business", "")),
            ]).lower()
            if requirement not in haystack and not _CASE_TERMS.search(requirement):
                continue
        matched.append(case)
    return matched[:3]


def build_react_plan(requirement: str, project_id: str = "default") -> dict:
    """生成 ReAct 执行计划，显式纳入数据、安全、浏览器视觉与失败分析能力。"""
    requirement = str(requirement or "").strip()
    rule_steps, source = _rule_step_plan(requirement, project_id=project_id)
    plan = {
        "summary": f"针对「{requirement[:40]}」的 AI Agent 测试计划",
        "source": source,
        "steps": rule_steps,
        "dimensions": _rule_dimension_plan(requirement),
    }
    steps = list(plan.get("steps") or [])
    step_types = {str(s.get("type")) for s in steps}
    tools = {str(s.get("tool")) for s in steps}

    # 项目记忆中发现真实用例时，优先交给现有 UI 执行器。
    # run_web_test 负责 YAML 用例、测试数据中心变量、逐步骤截图和报告；
    # run_browser_agent 只用于没有用例资产时的轻量视觉探索，避免重复跑完整业务流。
    discovered = discover_cases(project_id, requirement)
    if discovered:
        case = discovered[0]
        case_payload = {
            "case_files": [case.get("name", "")],
            "base_url": case.get("base_url", ""),
            "env": "test",
        }
        ui_step = next((step for step in steps if step.get("tool") == "run_web_test"), None)
        if ui_step is not None:
            payload = ui_step.setdefault("payload", {})
            if isinstance(payload, dict):
                payload.setdefault("case_files", case_payload["case_files"])
                payload.setdefault("base_url", case_payload["base_url"])
                payload.setdefault("env", case_payload["env"])
                ui_step["action"] = (
                    f"按真实用例 {case.get('name')} 执行全流程"
                    f"（{case.get('title') or case.get('flow') or ''}）"
                )
        else:
            steps.append({
                "step": len(steps) + 1,
                "type": "ui",
                "tool": "run_web_test",
                "action": f"按真实用例 {case.get('name')} 执行全流程（{case.get('title') or case.get('flow') or ''}）",
                "payload": case_payload,
            })
        tools.add("run_web_test")

    if _SECURITY_TERMS.search(requirement) and "security" not in step_types:
        steps.append({
            "step": len(steps) + 1,
            "type": "security",
            "tool": "run_security_test",
            "action": "对需求相关接口执行安全扫描（SQL注入/XSS/越权/JWT/SSRF）",
            "payload": {"target": "", "parameters": []},
        })
    if _BROWSER_TERMS.search(requirement) and "run_browser_agent" not in tools:
        steps.append({
            "step": len(steps) + 1,
            "type": "browser",
            "tool": "run_browser_agent",
            "action": "打开页面并视觉识别元素，执行页面操作测试",
            "payload": {"url": "", "goal": requirement},
        })
        tools.add("run_browser_agent")
    if "generate_from_api" not in tools and (_SECURITY_TERMS.search(requirement) or "api" in step_types):
        steps.append({
            "step": len(steps) + 1,
            "type": "data",
            "tool": "generate_from_api",
            "action": "AI Test Data Generator 基于接口 Schema 生成 normal/boundary/security 数据",
            "payload": {"swagger_url": "", "swagger_doc": {}, "api": "", "mode": ["normal", "boundary", "security"]},
        })
        tools.add("generate_from_api")
    if "analyze_failure" not in tools:
        steps.append({
            "step": len(steps) + 1,
            "type": "analysis",
            "tool": "analyze_failure",
            "action": "AI 结果分析失败证据，输出根因、旧定位、新定位候选和修复建议",
            "payload": {"logs": "", "screenshots": [], "api_response": "", "context": requirement},
        })
        tools.add("analyze_failure")
    if "report" not in tools:
        steps.append({
            "step": len(steps) + 1,
            "type": "report",
            "tool": "create_report",
            "action": "汇总全部执行结果并生成测试报告",
            "payload": {},
        })
    plan["steps"] = steps
    plan["source"] = "react_agent_planner"
    plan["react_architecture"] = [
        "Agent Planner",
        "Action Executor",
        "Observation",
        "Reflection",
        "Retry",
    ]
    return plan
