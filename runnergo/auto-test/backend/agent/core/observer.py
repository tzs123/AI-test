"""观察器：把工具执行结果转换为结构化 Observation，并提取可复用事实。

Observation 结构：
    {
        "status": "success|failed|skipped",
        "summary": "一句话结论",
        "facts": {"asset_id": "...", "count": 100, "task_id": "...", ...},
        "raw": {原始结果}
    }
"""
from __future__ import annotations

from typing import Any, Optional


def _extract_facts(result: dict) -> dict:
    facts: dict[str, Any] = {}
    for key in ("asset_id", "count", "data_count", "task_id", "status_code",
                "elapsed_ms", "report_file", "report_json", "risk", "vulnerability_type"):
        if result.get(key) not in (None, ""):
            facts[key] = result[key]
    summary = result.get("summary") or result.get("message") or result.get("error")
    if isinstance(summary, dict):
        for key in ("total", "passed", "failed", "skipped", "pass_rate"):
            if summary.get(key) is not None:
                facts[key] = summary[key]
    total = result.get("total")
    if total is not None:
        facts["total"] = total
    if isinstance(result.get("analyses"), list) and result["analyses"]:
        facts["failed_count"] = len(result["analyses"])
    return facts


def observe(result: dict) -> dict:
    """把工具结果转换为 Observation。"""
    result = result or {}
    status = str(result.get("status") or "failed")
    summary = (
        result.get("summary")
        or result.get("message")
        or result.get("error")
        or ("执行成功" if status == "success" else "执行失败")
    )
    if isinstance(summary, dict):
        summary = "执行完成"
    return {
        "status": status,
        "summary": str(summary)[:500],
        "facts": _extract_facts(result),
        "raw": result,
    }


def observation_text(observation: dict) -> str:
    """生成 Observation 文本（写入执行日志 / 记忆）。"""
    status_text = {"success": "成功", "failed": "失败", "skipped": "跳过"}.get(
        observation.get("status"), observation.get("status")
    )
    facts = observation.get("facts") or {}
    parts = [f"结果:{status_text}", observation.get("summary", "")]
    for key in ("asset_id", "count", "data_count", "task_id", "status_code",
                "failed_count", "total", "passed", "failed"):
        if key in facts:
            parts.append(f"{key}={facts[key]}")
    return "；".join(parts)
