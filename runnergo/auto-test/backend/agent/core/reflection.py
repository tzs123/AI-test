"""反思器：根据 Observation 与历史轨迹判断是否需要调整。

Reflection 结构：
    {
        "decision": "continue|retry|adjust|finish|stop",
        "reason": "判断依据",
        "suggestion": "可执行的调整建议",
        "round": 当前第几轮
    }
"""
from __future__ import annotations

from typing import Any, Optional

from backend.agent.memory import recall
from backend.llm.provider import chat_json, is_configured


def _retry_count(history: list[dict], action_key: str) -> int:
    return sum(
        1
        for item in history
        if item.get("decision") == "retry" and item.get("action_key", item.get("action")) == action_key
    )


def reflect(
    task_id: str,
    observation: dict,
    history: Optional[list[dict]] = None,
    *,
    action_key: str = "",
    max_retries: int = 2,
    requirement: str = "",
) -> dict:
    """根据观察结果决定下一步策略。模型可用时增强，失败回退规则。"""
    history = history or []
    status = observation.get("status")
    round_no = len(history) + 1

    if status == "success":
        return {
            "decision": "continue",
            "reason": "工具执行成功，继续下一步",
            "suggestion": "",
            "round": round_no,
        }
    if status == "skipped":
        return {
            "decision": "continue",
            "reason": "工具被安全策略跳过（如未配置 base_url），不影响后续步骤",
            "suggestion": str(observation.get("summary") or ""),
            "round": round_no,
        }

    retries = _retry_count(history, action_key)
    if retries < max_retries:
        return {
            "decision": "retry",
            "reason": f"执行失败（第 {retries + 1} 次），尝试调整参数重试",
            "suggestion": "补充 base_url/参数或更换测试数据后重试",
            "round": round_no,
            "action_key": action_key,
        }

    if is_configured() and requirement:
        try:
            data = chat_json(
                "你是资深测试 Agent 反思器。根据失败观察判断下一步："
                '{"decision":"continue|retry|adjust|finish","reason":"...","suggestion":"..."}。'
                "continue=继续后续步骤；retry=换数据重试；adjust=更换测试策略/工具；finish=结束并进入失败分析。",
                f"需求：{requirement}\n观察：{observation.get('summary')}",
                max_tokens=300,
                temperature=0.1,
            )
            decision = str(data.get("decision") or "finish")
            if decision not in {"continue", "retry", "adjust", "finish"}:
                decision = "finish"
            return {
                "decision": decision,
                "reason": str(data.get("reason") or "重试次数耗尽，结束执行")[:500],
                "suggestion": str(data.get("suggestion") or "")[:500],
                "round": round_no,
            }
        except Exception:  # noqa: BLE001 - 模型失败回退规则
            pass
    return {
        "decision": "adjust",
        "reason": "重试次数耗尽，切换到失败分析并结束本轮执行",
        "suggestion": "调用 analyze_failure 分析失败原因，生成修复建议",
        "round": round_no,
    }
