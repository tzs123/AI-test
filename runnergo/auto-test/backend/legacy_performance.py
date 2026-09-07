"""Compatibility endpoints for the historical RunnerGo performance dashboard."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException

from backend import db


router = APIRouter()

DEFAULT_RULE = {
    "project_id": 0,
    "min_tps": 5000,
    "max_p95": 700,
    "max_p99": 1000,
    "max_error_rate": 1,
    "max_cpu": 85,
    "max_memory": 85,
    "max_db_connection_usage": 80,
    "max_db_slow_queries_per_sec": 1,
}


def init_storage() -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS legacy_performance_rules (
            project_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def _loads(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return default
    return parsed


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _run(report_id: str) -> dict[str, Any]:
    report_id = str(report_id).strip()
    if report_id.startswith("legacy_stress_report_"):
        report_id = report_id.removeprefix("legacy_stress_report_")
    rows = db.execute(
        "SELECT * FROM load_test_runs WHERE id=?",
        (report_id,),
        fetch=True,
    )
    if not rows:
        raise HTTPException(404, "性能报告不存在")
    item = db.to_dict(rows[0])
    item["config"] = _loads(item.get("config_json"), {})
    item["summary"] = _loads(item.get("summary_json"), {})
    return item


def _timestamp(value: Any) -> float:
    if not value:
        return 0.0
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _report_option(item: dict[str, Any]) -> dict[str, Any]:
    summary = item["summary"]
    return {
        "report_id": str(item.get("id") or ""),
        "report_name": str(item.get("scenario_name") or item.get("case_file") or item.get("id") or ""),
        "run_time_sec": _number(summary.get("elapsed_seconds")),
        "created_at": item.get("created_at") or "",
        "status": item.get("status") or "",
        "mode": item.get("mode") or "",
    }


def _sla_results(summary: dict[str, Any]) -> list[dict[str, Any]]:
    raw = summary.get("sla") or {}
    results = []
    for rule in raw.get("rules") or []:
        key = str(rule.get("key") or "")
        operator = ">=" if key.startswith("min_") else "<="
        unit = "ms" if key.endswith("_ms") else "%" if "error_rate" in key else ""
        results.append({
            "name": rule.get("label") or key,
            "key": key,
            "actual": _number(rule.get("actual")),
            "expected": _number(rule.get("expected")),
            "operator": operator,
            "unit": unit,
            "status": "PASS" if rule.get("passed") else "FAIL",
        })
    return results


def _interfaces(summary: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    transactions = summary.get("transactions") or summary.get("business_transactions") or []
    result = []
    for transaction in transactions:
        if not isinstance(transaction, dict):
            continue
        count = _int(transaction.get("count"))
        passed = _int(transaction.get("passed"))
        result.append({
            "api_name": str(transaction.get("name") or transaction.get("key") or "接口步骤"),
            "total_requests": count,
            "success_rate": round(passed / count * 100, 2) if count else 0,
            "avg_response_time": _number(transaction.get("avg_ms")),
            "p95": _number(transaction.get("p95_ms")),
            "p99": _number(transaction.get("p99_ms")),
            "peak_tps": _number(transaction.get("tps")),
            "max_concurrency": _int(config.get("virtual_users")),
        })
    return sorted(result, key=lambda value: value["p95"], reverse=True)


def _detail(item: dict[str, Any]) -> dict[str, Any]:
    summary = item["summary"]
    config = item["config"]
    total = _int(summary.get("completed"))
    passed = _int(summary.get("passed"), max(0, total - _int(summary.get("failed"))))
    failed = _int(summary.get("failed"), max(0, total - passed))
    error_rate = _number(summary.get("error_rate"), (failed / total * 100) if total else 0)
    sla = summary.get("sla") or {}
    sla_results = _sla_results(summary)
    passed_rules = sum(1 for rule in sla_results if rule["status"] == "PASS")
    score = round(passed_rules / len(sla_results) * 100, 2) if sla_results else round(passed / total * 100, 2) if total else 0
    overall_status = str(sla.get("status") or ("PASS" if failed == 0 else "FAIL")).upper()
    started_at = item.get("started_at") or item.get("created_at") or ""
    finished_at = item.get("finished_at") or started_at
    resource = summary.get("resource_monitoring") if isinstance(summary.get("resource_monitoring"), dict) else {}
    resource_metrics = resource.get("metrics") if isinstance(resource.get("metrics"), dict) else {}
    resource_availability = resource.get("availability") if isinstance(resource.get("availability"), dict) else {}

    def resource_avg(name: str) -> Any:
        metric = resource_metrics.get(name)
        return metric.get("avg") if isinstance(metric, dict) else None

    def resource_peak(name: str) -> Any:
        metric = resource_metrics.get(name)
        return metric.get("peak") if isinstance(metric, dict) else None

    metric_summary = {
        "TotalRequests": total,
        "SuccessRequests": passed,
        "ErrorRequests": failed,
        "SuccessRate": round(passed / total * 100, 2) if total else 0,
        "AvgResponseTime": _number(summary.get("avg_ms")),
        "MaxResponseTime": _number(summary.get("max_ms")),
        "P50": _number(summary.get("p50_ms")),
        "P90": _number(summary.get("p90_ms")),
        "P95": _number(summary.get("p95_ms")),
        "P99": _number(summary.get("p99_ms")),
        "AvgTPS": _number(summary.get("tps")),
        "AvgRPS": _number(summary.get("protocol_rps")),
        "MaxTPS": _number(summary.get("peak_tps")),
        "MaxConcurrency": _int(summary.get("peak_active_vus"), _int(config.get("virtual_users"))),
        "CPU": resource_avg("cpu_percent"),
        "MaxCPU": resource_peak("cpu_percent"),
        "Memory": resource_avg("memory_percent"),
        "MaxMemory": resource_peak("memory_percent"),
        "ProcessCPU": resource_avg("process_cpu_percent"),
        "ProcessRSSMB": resource_peak("process_rss_mb"),
        "NetworkRxKBPS": resource_avg("network_rx_kbps"),
        "NetworkTxKBPS": resource_avg("network_tx_kbps"),
        "JVM": None,
        "DBConnectionUsage": None,
        "DBQPS": None,
        "DBSlowQueriesQPS": None,
        "DBRowLockWaitsQPS": None,
    }
    errors = summary.get("recent_errors") or []
    issues = [
        str(error.get("message") or error.get("error") or error)
        if isinstance(error, dict) else str(error)
        for error in errors
    ]
    if error_rate > 0 and not issues:
        issues.append(f"错误率为 {error_rate:.2f}%。")
    suggestions = []
    if failed:
        suggestions.append("请优先检查失败请求对应的接口和被测服务资源。")
    else:
        suggestions.append("保持当前容量配置，并持续观察趋势。")

    return {
        "report_id": str(item.get("id") or ""),
        "report_name": str(item.get("scenario_name") or item.get("case_file") or ""),
        "run_time_sec": _number(summary.get("elapsed_seconds")),
        "score": score,
        "overall_status": overall_status,
        "details": {
            "summary": metric_summary,
            "interfaces": _interfaces(summary, config),
            "sla_results": sla_results,
            "issues": issues,
            "suggestions": suggestions,
            "metric_availability": {
                "cpu": bool(resource_availability.get("cpu")),
                "memory": bool(resource_availability.get("memory")),
                "network": bool(resource_availability.get("network")),
                "jvm": bool(resource_availability.get("jvm")),
                "db_connection_usage": False,
                "db_qps": False,
                "db_slow_queries_per_sec": False,
                "db_row_lock_waits_per_sec": False,
            },
            "resource_monitoring": resource,
            "metric_window": {
                "live": False,
                "start": started_at,
                "end": finished_at,
                "start_unix": _timestamp(started_at),
                "end_unix": _timestamp(finished_at),
            },
            "observed_at": finished_at,
        },
        "markdown": (
            f"性能报告：{item.get('scenario_name') or item.get('case_file') or item.get('id')}\n"
            f"总请求数：{total}，成功：{passed}，失败：{failed}\n"
            f"平均 TPS：{metric_summary['AvgTPS']}，P95：{metric_summary['P95']} ms，P99：{metric_summary['P99']} ms\n"
            f"SLA：{overall_status}"
        ),
    }


@router.get("/management/api/v1/report/list")
def report_list(keyword: str = "", limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(int(limit or 50), 200))
    rows = db.execute(
        "SELECT * FROM load_test_runs ORDER BY created_at DESC LIMIT ?",
        (limit,),
        fetch=True,
    )
    needle = str(keyword or "").strip().lower()
    reports = []
    for row in rows:
        item = db.to_dict(row)
        haystack = " ".join(str(item.get(key) or "") for key in ("id", "scenario_name", "case_file")).lower()
        if not needle or needle in haystack:
            reports.append(_report_option({**item, "summary": _loads(item.get("summary_json"), {})}))
    return {"reports": reports}


@router.get("/management/api/v1/performance/rule/latest")
def rule_latest(project_id: str = "0") -> dict[str, Any]:
    init_storage()
    rows = db.execute(
        "SELECT payload FROM legacy_performance_rules WHERE project_id=?",
        (str(project_id),),
        fetch=True,
    )
    payload = _loads(rows[0]["payload"], {}) if rows else {}
    return {**DEFAULT_RULE, **payload, "project_id": _int(payload.get("project_id", project_id))}


@router.post("/management/api/v1/performance/rule/save")
def rule_save(payload: dict[str, Any]) -> dict[str, Any]:
    init_storage()
    project_id = str(payload.get("project_id", 0))
    allowed = {key: payload[key] for key in DEFAULT_RULE if key in payload}
    allowed["project_id"] = _int(project_id)
    db.execute(
        "INSERT OR REPLACE INTO legacy_performance_rules(project_id,payload,updated_at) VALUES(?,?,datetime('now'))",
        (project_id, json.dumps(allowed, ensure_ascii=False)),
    )
    return {**DEFAULT_RULE, **allowed}


@router.post("/management/api/v1/performance/report/analyze")
def report_analyze(payload: dict[str, Any]) -> dict[str, Any]:
    return _detail(_run(str(payload.get("report_id") or "")))


@router.get("/management/api/v1/performance/report/detail")
def report_detail(report_id: str = "") -> dict[str, Any]:
    return _detail(_run(report_id))
