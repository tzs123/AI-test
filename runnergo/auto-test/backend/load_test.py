"""LoadRunner-style load orchestration for YAML Web/API/DB flows.

The YAML case remains immutable.  A load run owns an in-memory deep copy of the
flow, resolves execution-only test-object details, and schedules virtual users
against the existing :class:`WebFlowRunner` implementation.
"""
from __future__ import annotations

import copy
import csv
import html
import io
import json
import math
import os
import random
import re
import threading
import time
import uuid
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, Optional
from urllib.parse import urlsplit

import requests
import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from backend import db, redis_queue, runnergo_test_objects
from backend.auth import service as auth_service
from backend.cases import service as case_service
from backend.projects import service as project_service
from backend.url_security import validate_outbound_url
from core.mixed_steps import redact
from core.protocol_drivers import PROTOCOL_ACTIONS, PROTOCOL_CAPABILITIES, driver_for
from core.web_flow_runner import NON_BROWSER_ACTIONS, WebFlowRunner, run_web_flow


router = APIRouter(prefix="/api/load-tests", tags=["load-tests"])

PROTOCOL_ACTIONS = {"api", "http", "request", "api_request"}
DATABASE_ACTIONS = {"db", "database", "query", "sql"}
RUNNING_STATUSES = {"pending", "running", "stopping"}
TERMINAL_STATUSES = {"completed", "failed", "stopped", "interrupted"}
MAX_DURATION_SECONDS = max(60, int(os.environ.get("LOAD_TEST_MAX_DURATION_SECONDS", "7200")))
MAX_API_VUS = max(1, int(os.environ.get("LOAD_TEST_MAX_API_VUS", "200")))
MAX_UI_VUS = max(1, int(os.environ.get("LOAD_TEST_MAX_UI_VUS", "20")))
MAX_DISTRIBUTED_VUS = max(
    MAX_API_VUS,
    int(os.environ.get("LOAD_TEST_MAX_DISTRIBUTED_VUS", "5000")),
)
MAX_PERCENTILE_SAMPLES = max(1000, int(os.environ.get("LOAD_TEST_MAX_PERCENTILE_SAMPLES", "50000")))
MAX_RESOURCE_SAMPLES = max(60, int(os.environ.get("LOAD_TEST_MAX_RESOURCE_SAMPLES", "10000")))
EXTERNAL_MONITOR_FIELDS = {
    # JVM 概览
    "jvm_heap_used_percent": "jvm",
    "jvm_nonheap_used_mb": "jvm",
    "jvm_threads": "jvm",
    "jvm_gc_pause_ms": "jvm",
    # JVM 深度：堆分代 / 各收集器次数与耗时 / 线程状态 / 类加载
    "jvm_heap_eden_mb": "jvm",
    "jvm_heap_survivor_mb": "jvm",
    "jvm_heap_old_mb": "jvm",
    "jvm_gc_marksweep_count": "jvm",
    "jvm_gc_marksweep_time_ms": "jvm",
    "jvm_gc_scavenge_count": "jvm",
    "jvm_gc_scavenge_time_ms": "jvm",
    "jvm_threads_blocked": "jvm",
    "jvm_threads_waiting": "jvm",
    "jvm_classes_loaded": "jvm",
    # 数据库概览
    "db_connection_usage": "database",
    "db_qps": "database",
    "db_slow_queries_per_sec": "database",
    "db_row_lock_waits_per_sec": "database",
    # 数据库深度：连接池 / 慢查询样本 / 复制延迟 / 表锁 / InnoDB 行操作
    "db_active_connections": "database",
    "db_pool_max": "database",
    "db_pool_wait_ms": "database",
    "db_slow_query_samples": "database",
    "db_replication_lag_seconds": "database",
    "db_table_locks": "database",
    "db_innodb_row_ops_per_sec": "database",
}

# 推荐的 Prometheus 查询表达式样例（jmx_exporter / mysqld_exporter / postgres_exporter），
# 供配置 monitoring.queries 时参考；指标键必须命中 EXTERNAL_MONITOR_FIELDS。
MONITORING_QUERY_EXAMPLES = {
    "jvm_heap_used_percent": 'jvm_memory_bytes_used{area="heap"} / jvm_memory_bytes_max{area="heap"} * 100',
    "jvm_heap_old_mb": 'jvm_memory_bytes_used{area="heap",pool="Old Generation"} / 1048576',
    "jvm_gc_scavenge_count": "increase(jvm_gc_collection_seconds_count{gc=\"PS Scavenge\"}[1m])",
    "jvm_threads_blocked": 'jvm_threads_state{state="blocked"}',
    "db_connection_usage": "mysql_global_status_threads_connected / mysql_global_variables_max_connections * 100",
    "db_qps": "rate(mysql_global_status_questions[1m])",
    "db_slow_queries_per_sec": "rate(mysql_global_status_slow_queries[1m])",
    "db_replication_lag_seconds": "mysql_slave_status_seconds_behind_master",
    "db_innodb_row_ops_per_sec": "rate(mysql_global_status_innodb_row_ops_total[1m])",
    "db_active_connections": "mysql_global_status_threads_connected",
    "db_pool_max": "mysql_global_variables_max_connections",
    "db_row_lock_waits_per_sec": "rate(mysql_global_status_innodb_row_lock_waits[1m])",
    "db_table_locks": "rate(mysql_global_status_table_locks_immediate[1m])",
}
LOAD_AGENT_START_DELAY_SECONDS = max(0.5, float(os.environ.get("LOAD_AGENT_START_DELAY_SECONDS", "2")))
LOAD_AGENT_FINISH_GRACE_SECONDS = max(5, int(os.environ.get("LOAD_AGENT_FINISH_GRACE_SECONDS", "60")))
VUM_QUOTA_ENFORCED = str(os.environ.get("AUTO_TEST_ENFORCE_VUM_QUOTA", "false")).strip().lower() not in {
    "0", "false", "no", "off",
}


class DistributedLoadError(RuntimeError):
    """The distributed load-generator pool cannot accept this run."""


def _agent_capacity(agent: Dict[str, Any]) -> int:
    capabilities = agent.get("capabilities") if isinstance(agent.get("capabilities"), dict) else {}
    modes = capabilities.get("modes") if isinstance(capabilities.get("modes"), list) else []
    if "protocol" not in modes:
        return 0
    return max(0, int(capabilities.get("max_vus") or 0))


def _load_agent_inventory(*, available_only: bool = False) -> list[Dict[str, Any]]:
    try:
        agents = redis_queue.list_load_agents()
    except Exception:
        return []
    result = []
    for agent in agents:
        if not isinstance(agent, dict) or not str(agent.get("name") or "").strip():
            continue
        capacity = _agent_capacity(agent)
        if capacity <= 0:
            continue
        if available_only and not bool(agent.get("available", not agent.get("reserved_by"))):
            continue
        item = dict(agent)
        item["capacity_vus"] = capacity
        result.append(item)
    return sorted(result, key=lambda item: (-int(item["capacity_vus"]), str(item["name"])))


def _distributed_capacity(agents: Iterable[Dict[str, Any]]) -> int:
    return min(MAX_DISTRIBUTED_VUS, sum(int(agent.get("capacity_vus") or 0) for agent in agents))


def _allocate_vu_shards(total_vus: int, agents: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    remaining = max(0, int(total_vus))
    next_vu = 1
    shards = []
    capacities = [max(0, int(agent.get("capacity_vus") or 0)) for agent in agents]
    for index, agent in enumerate(agents):
        if remaining <= 0:
            break
        capacity = capacities[index]
        future_capacity = sum(capacities[index + 1:])
        minimum_here = max(0, remaining - future_capacity)
        balanced = int(math.ceil(remaining / max(1, len(agents) - index)))
        assigned = min(capacity, max(minimum_here, balanced))
        if assigned <= 0:
            continue
        shards.append({
            "name": str(agent["name"]),
            "max_vus": capacity,
            "assigned_vus": assigned,
            "vu_start": next_vu,
            "vu_end": next_vu + assigned - 1,
        })
        remaining -= assigned
        next_vu += assigned
    if remaining:
        raise DistributedLoadError(f"分布式节点容量不足，还缺少 {remaining} VU")
    return shards


def _prepare_distributed_run(config: Dict[str, Any], run_id: str) -> None:
    virtual_users = int(config.get("virtual_users") or 0)
    requested_count = max(0, int(config.get("requested_agent_count") or 0))
    candidates = _load_agent_inventory(available_only=True)
    if requested_count:
        candidates = candidates[:min(requested_count, virtual_users)]
        if len(candidates) < min(requested_count, virtual_users):
            raise DistributedLoadError("可用负载节点数量不足")
    reservation_ttl = int(config.get("total_duration_seconds") or 0) + 600
    reserved = []
    reserved_capacity = 0
    try:
        for agent in candidates:
            if not redis_queue.reserve_load_agent(str(agent["name"]), run_id, reservation_ttl):
                continue
            reserved.append(agent)
            reserved_capacity += int(agent["capacity_vus"])
            if not requested_count and reserved_capacity >= virtual_users:
                break
        if reserved_capacity < virtual_users:
            raise DistributedLoadError(
                f"可用分布式容量为 {reserved_capacity} VU，无法承载 {virtual_users} VU"
            )
        shards = _allocate_vu_shards(virtual_users, reserved)
        config["distributed_agents"] = shards
        config["agent_count"] = len(shards)
        config["distributed_capacity_vus"] = reserved_capacity
    except Exception:
        for agent in reserved:
            try:
                redis_queue.release_load_agent(str(agent["name"]), run_id)
            except Exception:
                pass
        raise


def _release_distributed_run(config: Dict[str, Any], run_id: str) -> None:
    for agent in config.get("distributed_agents") or []:
        try:
            redis_queue.release_load_agent(str(agent.get("name") or ""), run_id)
        except Exception:
            pass

METRIC_SEMANTICS = {
    "tps": "平均场景 TPS = 完成的整条业务链路数 / 实际运行秒数，等同 LoadRunner 的平均事务吞吐。",
    "peak_tps": "峰值 TPS = 单秒内完成的整条业务链路数，只代表瞬时最高值，不用于替代平均 TPS。",
    "stable_tps": "稳态 TPS = 稳态阶段完成的整条业务链路数 / 稳态阶段秒数，用于观察持续承载能力。",
    "active_burst_seconds": "有完成的秒数 = timeline 中存在样本的秒桶数量，可能少于实际运行秒数；不要把它当成观测时长，观测时长用 elapsed_seconds。",
    "protocol_rps": "接口 RPS = 接口步骤完成次数 / 实际运行秒数；混合链路中会高于场景 TPS，二者不能直接比较。",
    "response_time": "平均/P50/P90/P95/P99/最大响应时间统计的是整条场景迭代耗时，单位为毫秒。",
    "business_transactions": "业务事务来自 YAML load_profile.transactions 或步骤 transaction 字段，类似 LoadRunner Analysis 的事务汇总。",
    "chain_integrity": "链路成功率 = 所有必需步骤均真实执行且通过的迭代数 / 已校验迭代数；跳过必需步骤按失败计算。",
    "resource_monitoring": "资源监控采集压测机主机 CPU、内存、网络和压测进程资源；当 monitoring.enabled=true 且配置 prometheus_url 时，额外采集 JVM 堆/分代/GC/线程/类加载和数据库连接池/QPS/慢查询/复制延迟等 25 个字段。",
    "http_waterfall": "HTTP 瀑布图 = 浏览器在每步执行期间发出的所有 HTTP 请求（document/xhr/script/stylesheet/image/font/other）的 DNS/Connect/TLS/TTFB/Receive/Total 时长，按 step 与 resource_type 双维度聚合，P95/P99 用于定位慢请求与慢资源。",
    "page_components": "页面组件分解 = 按请求 URL（路径中的业务 ID 归一为 {id}、丢弃 query）聚合每个页面/接口组件的请求数、占比与 DNS/Connect/TLS/TTFB/Receive/Total 的 P50/P90/P95/P99，等同 LoadRunner Analysis 的 Page Component Breakdown，用于按页面 URL 定位慢请求与承压分布。",
}


def _planned_vum(config: Dict[str, Any]) -> int:
    """Bill the integral of scheduled VUs, rounded up to a whole VU-minute."""
    virtual_users = max(1, int(config.get("virtual_users") or 1))
    ramp_up = max(0.0, float(config.get("ramp_up_seconds") or 0))
    duration = max(1.0, float(config.get("duration_seconds") or 1))
    ramp_down = max(0.0, float(config.get("ramp_down_seconds") or 0))
    active_user_seconds = virtual_users * (duration + (ramp_up + ramp_down) / 2)
    return max(1, int(math.ceil(active_user_seconds / 60)))


def _team_vum_quota(token: str, team_id: str) -> Dict[str, Any]:
    if not VUM_QUOTA_ENFORCED:
        return {
            "team_id": str(team_id or ""),
            "available_vum_num": None,
            "enforced": False,
        }
    quota = runnergo_test_objects.get_team_vum(token, team_id)
    return {**quota, "enforced": True}


def _consume_team_vum(
    token: str,
    team_id: str,
    source_id: str,
    vum_num: int,
) -> Dict[str, Any]:
    if not VUM_QUOTA_ENFORCED:
        return {
            "team_id": str(team_id or ""),
            "source_id": str(source_id or ""),
            "consumed_vum_num": 0,
            "available_vum_num": None,
            "already_consumed": False,
            "enforced": False,
        }
    result = runnergo_test_objects.consume_team_vum(
        token,
        team_id,
        source_id,
        vum_num,
    )
    return {**result, "enforced": True}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return copy.deepcopy(default)
    return parsed


def _compact_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    compact = {
        key: summary.get(key)
        for key in (
            "elapsed_seconds", "target_vus", "active_vus", "peak_active_vus",
            "completed", "passed", "failed", "error_rate", "tps", "peak_tps",
            "stable_tps", "protocol_rps", "avg_ms", "p95_ms", "p99_ms",
            "scenarios", "business_transactions",
            "resource_monitoring",
            "data_quality", "target_integrity", "chain_integrity", "sla",
            "planned_duration_seconds", "load_window_seconds", "drain_duration_seconds",
            "http_waterfall",
        )
    }
    resource = summary.get("resource_monitoring") if isinstance(summary.get("resource_monitoring"), dict) else {}
    compact["resource_monitoring"] = {
        "sample_count": int(resource.get("sample_count") or 0),
        "sample_count_total": int(resource.get("sample_count_total") or resource.get("sample_count") or 0),
        "availability": resource.get("availability") or {},
        "metrics": resource.get("metrics") or {},
    }
    http = summary.get("http_waterfall") if isinstance(summary.get("http_waterfall"), dict) else {}
    compact["http_waterfall"] = {
        "total_requests": int(http.get("total_requests") or 0),
        "distinct_url_count": int(http.get("distinct_url_count") or 0),
        "by_type": http.get("by_type") or {},
        "page_components": (http.get("page_components") or [])[:10],
        "histogram": http.get("histogram") or [],
    }
    return compact


def _percentile(values: Iterable[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return round(ordered[0], 2)
    position = max(0.0, min(1.0, percentile)) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return round(ordered[lower], 2)
    weight = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * weight, 2)


def _bounded_sample(values: list[float], value: float, total_count: int) -> None:
    """Keep a deterministic bounded sample for long-running high-volume tests."""
    if len(values) < MAX_PERCENTILE_SAMPLES:
        values.append(float(value))
        return
    values[(max(1, total_count) - 1) % MAX_PERCENTILE_SAMPLES] = float(value)


def _proc_cpu_memory() -> tuple[Optional[tuple[int, int]], Dict[str, float]]:
    cpu = None
    memory: Dict[str, float] = {}
    try:
        fields = open("/proc/stat", encoding="utf-8").readline().split()
        values = [int(item) for item in fields[1:]]
        if len(values) >= 4:
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            cpu = (sum(values), idle)
    except (OSError, ValueError):
        pass
    try:
        info = {}
        for line in open("/proc/meminfo", encoding="utf-8"):
            key, _, raw = line.partition(":")
            info[key] = float(raw.strip().split()[0])
        total = info.get("MemTotal", 0.0)
        available = info.get("MemAvailable", info.get("MemFree", 0.0))
        if total > 0:
            memory = {
                "memory_percent": round(max(0.0, (total - available) * 100 / total), 2),
                "memory_used_mb": round((total - available) / 1024, 2),
                "memory_total_mb": round(total / 1024, 2),
            }
    except (OSError, ValueError):
        pass
    try:
        for line in open("/proc/self/status", encoding="utf-8"):
            if line.startswith("VmRSS:"):
                memory["process_rss_mb"] = round(float(line.split()[1]) / 1024, 2)
                break
    except (OSError, ValueError, IndexError):
        pass
    return cpu, memory


def _proc_network_bytes() -> tuple[int, int]:
    received = sent = 0
    try:
        for line in open("/proc/net/dev", encoding="utf-8"):
            if ":" not in line:
                continue
            _, payload = line.split(":", 1)
            fields = payload.split()
            if len(fields) >= 9:
                received += int(fields[0])
                sent += int(fields[8])
    except (OSError, ValueError, IndexError):
        return 0, 0
    return received, sent


def _loadavg_cpu_percent() -> Optional[float]:
    try:
        load = os.getloadavg()[0]
        cpus = max(1, int(os.cpu_count() or 1))
        return round(max(0.0, min(100.0, load * 100 / cpus)), 2)
    except (AttributeError, OSError, ValueError):
        return None


class _ResourceSampler:
    """Small /proc based sampler so self-hosted runs need no optional agent."""

    def __init__(self) -> None:
        self.previous_cpu, _ = _proc_cpu_memory()
        self.previous_network = _proc_network_bytes()
        self.previous_clock = time.monotonic()
        self.previous_process_cpu = sum(os.times()[:2])

    def sample(self, elapsed_seconds: float) -> Dict[str, Any]:
        now = time.monotonic()
        interval = max(0.001, now - self.previous_clock)
        cpu_ticks, memory = _proc_cpu_memory()
        cpu_percent: Optional[float] = None
        if cpu_ticks and self.previous_cpu:
            total_delta = cpu_ticks[0] - self.previous_cpu[0]
            idle_delta = cpu_ticks[1] - self.previous_cpu[1]
            if total_delta > 0:
                cpu_percent = round(max(0.0, min(100.0, (total_delta - idle_delta) * 100 / total_delta)), 2)
        if cpu_percent is None:
            cpu_percent = _loadavg_cpu_percent()
        network = _proc_network_bytes()
        rx_kbps = tx_kbps = None
        if self.previous_network and network != (0, 0):
            rx_kbps = round(max(0, network[0] - self.previous_network[0]) / 1024 / interval, 2)
            tx_kbps = round(max(0, network[1] - self.previous_network[1]) / 1024 / interval, 2)
        process_cpu = sum(os.times()[:2])
        process_cpu_percent = round(max(0.0, process_cpu - self.previous_process_cpu) * 100 / interval, 2)
        self.previous_cpu = cpu_ticks or self.previous_cpu
        self.previous_network = network if network != (0, 0) else self.previous_network
        self.previous_process_cpu = process_cpu
        self.previous_clock = now
        sample: Dict[str, Any] = {
            "elapsed_seconds": round(max(0.0, float(elapsed_seconds)), 3),
            "cpu_percent": cpu_percent,
            "process_cpu_percent": process_cpu_percent,
            "network_rx_kbps": rx_kbps,
            "network_tx_kbps": tx_kbps,
            "network_rx_bytes": network[0],
            "network_tx_bytes": network[1],
            **memory,
            "availability": {
                "cpu": cpu_percent is not None,
                "memory": "memory_percent" in memory,
                "network": network != (0, 0),
                "jvm": False,
                "database": False,
            },
        }
        return sample


def _normalize_monitoring_config(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict) or not raw:
        return {"enabled": False, "prometheus_url": "", "queries": {}}
    prometheus_url = str(raw.get("prometheus_url") or raw.get("url") or "").strip().rstrip("/")
    queries = raw.get("queries") if isinstance(raw.get("queries"), dict) else {}
    normalized_queries = {
        str(key): str(value).strip()
        for key, value in queries.items()
        if str(key) in EXTERNAL_MONITOR_FIELDS and str(value).strip()
    }
    if not normalized_queries and prometheus_url:
        normalized_queries = {
            key: value for key, value in MONITORING_QUERY_EXAMPLES.items()
            if key in EXTERNAL_MONITOR_FIELDS and value
        }
    if len(normalized_queries) > len(EXTERNAL_MONITOR_FIELDS):
        raise ValueError("被测服务监控指标数量超限")
    if prometheus_url:
        prometheus_url = validate_outbound_url(prometheus_url)
    headers = raw.get("headers") if isinstance(raw.get("headers"), dict) else {}
    return {
        "enabled": bool(prometheus_url and normalized_queries),
        "prometheus_url": prometheus_url,
        "queries": normalized_queries,
        "headers": {str(key): str(value) for key, value in headers.items()},
        "timeout_seconds": max(0.2, min(10.0, float(raw.get("timeout_seconds") or 2))),
    }


def _prometheus_value(payload: Any) -> Optional[float]:
    try:
        result = payload["data"]["result"]
        if not result:
            return None
        values = []
        for item in result:
            raw_value = item.get("value", [None, None])[1]
            value = float(raw_value)
            if math.isfinite(value):
                values.append(value)
        return sum(values) if values else None
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _collect_external_monitoring(
    config: Dict[str, Any],
    session: requests.Session,
) -> Dict[str, Any]:
    if not isinstance(config, dict) or not config.get("enabled"):
        return {}
    base_url = str(config.get("prometheus_url") or "").rstrip("/")
    query_url = base_url if base_url.endswith("/api/v1/query") else f"{base_url}/api/v1/query"
    timeout = max(0.2, min(10.0, float(config.get("timeout_seconds") or 2)))
    headers = config.get("headers") if isinstance(config.get("headers"), dict) else {}
    result: Dict[str, Any] = {}
    errors = []
    availability = {"jvm": False, "database": False}
    for name, expression in (config.get("queries") or {}).items():
        if name not in EXTERNAL_MONITOR_FIELDS:
            continue
        try:
            response = session.get(query_url, params={"query": expression}, headers=headers, timeout=timeout)
            response.raise_for_status()
            value = _prometheus_value(response.json())
            if value is None:
                errors.append(f"{name}: no data")
                continue
            result[name] = round(value, 2)
            availability[EXTERNAL_MONITOR_FIELDS[name]] = True
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}")
    result["external_availability"] = availability
    if errors:
        result["external_monitor_errors"] = errors[:10]
    return result


def _resource_summary(samples: list[Dict[str, Any]], *, include_agents: bool = True) -> Dict[str, Any]:
    fields = (
        "cpu_percent", "memory_percent", "process_cpu_percent", "process_rss_mb",
        "network_rx_kbps", "network_tx_kbps", *EXTERNAL_MONITOR_FIELDS.keys(),
    )
    metrics: Dict[str, Any] = {}
    availability = {"cpu": False, "memory": False, "network": False, "jvm": False, "database": False}
    for sample in samples:
        for key in availability:
            availability[key] = availability[key] or bool((sample.get("availability") or {}).get(key))
        for field_name in fields:
            value = sample.get(field_name)
            if isinstance(value, (int, float)):
                metrics.setdefault(field_name, []).append(float(value))
    aggregate = {}
    for field_name, values in metrics.items():
        aggregate[field_name] = {"avg": round(sum(values) / len(values), 2), "peak": round(max(values), 2)}
    by_agent: Dict[str, Dict[str, Any]] = {}
    if include_agents:
        grouped: Dict[str, list[Dict[str, Any]]] = defaultdict(list)
        for sample in samples:
            agent_id = str(sample.get("agent_id") or "controller")
            grouped[agent_id].append(sample)
        for agent_id, agent_samples in grouped.items():
            if len(grouped) == 1 and agent_id == "controller":
                continue
            compact = _resource_summary(agent_samples, include_agents=False)
            compact.pop("samples", None)
            by_agent[agent_id] = compact
    monitor_errors = [
        str(error)
        for sample in samples
        for error in (sample.get("external_monitor_errors") or [])
    ]
    return {
        "samples": samples,
        "sample_count": len(samples),
        "availability": availability,
        "metrics": aggregate,
        "source": "controller /proc sampler",
        "scope": "load-generator host",
        "interval_seconds": 1,
        "by_agent": by_agent,
        "external_monitor_errors": list(dict.fromkeys(monitor_errors))[:20],
    }


def _throughput_metrics(
    timeline: list[Dict[str, Any]],
    config: Dict[str, Any],
    elapsed: float,
    completed: int,
) -> Dict[str, Any]:
    """Return LoadRunner-style average/peak/steady throughput with explicit windows."""
    buckets = [
        item for item in timeline
        if isinstance(item, dict) and int(item.get("second") or 0) >= 0
    ]
    peak_tps = max((int(item.get("completed") or 0) for item in buckets), default=0)
    ramp_up = max(0.0, float(config.get("ramp_up_seconds") or 0))
    steady_duration = max(0.0, float(config.get("duration_seconds") or 0))
    steady_start = min(ramp_up, max(0.0, elapsed))
    steady_end = min(ramp_up + steady_duration, max(0.0, elapsed))
    steady_elapsed = max(0.0, steady_end - steady_start)
    steady_completed = sum(
        int(item.get("completed") or 0)
        for item in buckets
        if steady_start <= float(item.get("second") or 0) < steady_end
    )
    return {
        "tps": round(completed / max(0.001, elapsed), 2),
        "peak_tps": round(float(peak_tps), 2),
        "stable_tps": round(steady_completed / steady_elapsed, 2) if steady_elapsed else 0.0,
        "steady_window_seconds": round(steady_elapsed, 2),
        "steady_completed": steady_completed,
        "active_burst_seconds": len(buckets),
        "observed_seconds": len(buckets),
    }


def _data_quality(
    *,
    completed: int,
    failed: int,
    elapsed: float,
    timeline: list[Dict[str, Any]],
    durations: list[float],
) -> Dict[str, Any]:
    """Validate the stored aggregates without hiding approximate percentile sampling."""
    timeline_completed = sum(int(item.get("completed") or 0) for item in timeline)
    issues = []
    if elapsed <= 0:
        issues.append("运行时长为 0，无法计算吞吐量。")
    if failed < 0 or failed > completed:
        issues.append("失败数超出完成数，统计数据不一致。")
    if timeline_completed != completed:
        issues.append(
            f"时间线完成数为 {timeline_completed}，但汇总完成数为 {completed}。"
        )
    if len(durations) >= MAX_PERCENTILE_SAMPLES and completed > MAX_PERCENTILE_SAMPLES:
        issues.append(
            f"请求量超过 {MAX_PERCENTILE_SAMPLES} 条，P50/P95/P99 基于有界样本，属于近似值。"
        )
    status = "正常" if not issues else "需关注"
    if completed == 0:
        status = "无有效数据"
    return {
        "status": status,
        "issues": issues,
        "completed": completed,
        "timeline_completed": timeline_completed,
        "duration_samples": len(durations),
        "percentile_sample_limit": MAX_PERCENTILE_SAMPLES,
    }


def _request_target(value: Any) -> str:
    """Return a request origin without retaining paths, queries, or credentials."""
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    try:
        parsed_port = parsed.port
    except ValueError:
        return ""
    default_port = (parsed.scheme == "http" and parsed_port == 80) or (
        parsed.scheme == "https" and parsed_port == 443
    )
    port = f":{parsed_port}" if parsed_port and not default_port else ""
    return f"{parsed.scheme}://{parsed.hostname.lower()}{port}"


# Matches ID-like path segments: pure integers, long hex strings, or full UUIDs.
# Used to collapse /api/user/123 and /api/user/456 into /api/user/{id} so the
# per-URL page-component breakdown stays bounded (mirrors LoadRunner's Page
# Component Breakdown without exploding dimensions on business IDs).
_PATH_ID_RE = re.compile(
    r"^(?:\d{1,19}|[0-9a-fA-F]{8,}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)


def _normalize_request_url(value: Any, method: str = "") -> str:
    """Normalize a request URL into a stable page-component key.

    Keeps scheme+host+port+method+path; drops query/fragment and replaces
    ID-like path segments with {id}. This yields one row per logical page/API
    component (e.g. ``GET http://host/api/apply/{id}``) instead of one row per
    parameterized request, matching LoadRunner's Page Component Breakdown.
    """
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    try:
        parsed_port = parsed.port
    except ValueError:
        return ""
    default_port = (parsed.scheme == "http" and parsed_port == 80) or (
        parsed.scheme == "https" and parsed_port == 443
    )
    port = f":{parsed_port}" if parsed_port and not default_port else ""
    origin = f"{parsed.scheme}://{parsed.hostname.lower()}{port}"
    segments = [seg for seg in (parsed.path or "").split("/") if seg != ""]
    norm_segments = [("{id}" if _PATH_ID_RE.match(seg) else seg) for seg in segments]
    path = "/" + "/".join(norm_segments) if norm_segments else "/"
    method_part = f"{str(method or '').upper()} " if method else ""
    return f"{method_part}{origin}{path}"


HTTP_HISTOGRAM_BUCKETS_MS: tuple = (100, 300, 500, 1000, 2000, 3000, 5000, 10000)


def _http_percentiles(values: list) -> Dict[str, float]:
    if not values:
        return {"p50": 0.0, "p90": 0.0, "p95": 0.0, "p99": 0.0, "avg": 0.0, "max": 0.0}
    sorted_vals = sorted(float(v) for v in values if v is not None)
    if not sorted_vals:
        return {"p50": 0.0, "p90": 0.0, "p95": 0.0, "p99": 0.0, "avg": 0.0, "max": 0.0}
    n = len(sorted_vals)

    def _pct(p: float) -> float:
        idx = max(0, min(n - 1, int(round((p / 100.0) * (n - 1)))))
        return round(sorted_vals[idx], 2)
    return {
        "p50": _pct(50), "p90": _pct(90), "p95": _pct(95), "p99": _pct(99),
        "avg": round(sum(sorted_vals) / n, 2), "max": round(sorted_vals[-1], 2),
    }


def _aggregate_http_timings(metric: Dict[str, Any], http_timings: list, state: "LoadRunState") -> None:
    """Aggregate per-step HTTP timing samples into step metric and global histogram."""
    for item in http_timings:
        if not isinstance(item, dict):
            continue
        rtype = str(item.get("resource_type") or "other").lower() or "other"
        total = float(item.get("total_ms") or 0.0)
        metric["http_request_count"] = metric.get("http_request_count", 0) + 1
        type_bucket = metric["http_timings_by_type"].setdefault(rtype, {
            "count": 0, "dns_ms": [], "connect_ms": [], "tls_ms": [],
            "ttfb_ms": [], "receive_ms": [], "total_ms": [],
        })
        type_bucket["count"] += 1
        for field in ("dns_ms", "connect_ms", "tls_ms", "ttfb_ms", "receive_ms", "total_ms"):
            type_bucket[field].append(float(item.get(field) or 0.0))
        metric["http_total_ms_samples"].append(total)
        state.http_total_ms_samples.append(total)
        state.http_request_count += 1
        for upper in HTTP_HISTOGRAM_BUCKETS_MS:
            if total <= upper:
                state.http_histogram[upper] = state.http_histogram.get(upper, 0) + 1
                break
        else:
            state.http_histogram[float("inf")] = state.http_histogram.get(float("inf"), 0) + 1
        state.http_by_type.setdefault(rtype, {"count": 0, "total_ms": []})
        state.http_by_type[rtype]["count"] += 1
        state.http_by_type[rtype]["total_ms"].append(total)
        # Per-URL page-component aggregation (LoadRunner Page Component Breakdown).
        url_key = _normalize_request_url(item.get("url"), item.get("method"))
        if url_key:
            url_bucket = state.http_by_url.get(url_key)
            if url_bucket is None:
                url_bucket = {
                    "count": 0, "method": str(item.get("method") or "").upper(),
                    "resource_type": rtype, "dns_ms": [], "connect_ms": [], "tls_ms": [],
                    "ttfb_ms": [], "receive_ms": [], "total_ms": [],
                }
                state.http_by_url[url_key] = url_bucket
            url_bucket["count"] += 1
            for field in ("dns_ms", "connect_ms", "tls_ms", "ttfb_ms", "receive_ms", "total_ms"):
                url_bucket[field].append(float(item.get(field) or 0.0))


def _http_summary(state: "LoadRunState") -> Dict[str, Any]:
    """Produce http_waterfall + response_time_histogram for the run summary."""
    waterfall = []
    for key in sorted(state.step_metrics.keys(), key=lambda k: (state.step_metrics[k].get("index", 0), k)):
        m = state.step_metrics[key]
        if not m.get("http_request_count"):
            continue
        types_summary = {}
        for rtype, bucket in (m.get("http_timings_by_type") or {}).items():
            types_summary[rtype] = {
                "count": bucket["count"],
                "total": _http_percentiles(bucket.get("total_ms") or []),
                "ttfb": _http_percentiles(bucket.get("ttfb_ms") or []),
                "dns": _http_percentiles(bucket.get("dns_ms") or []),
                "connect": _http_percentiles(bucket.get("connect_ms") or []),
                "receive": _http_percentiles(bucket.get("receive_ms") or []),
            }
        waterfall.append({
            "step_index": m.get("index", 0),
            "step_name": m.get("name", ""),
            "request_count": m.get("http_request_count", 0),
            "by_type": types_summary,
        })
    total_requests = state.http_request_count
    histogram = []
    if total_requests:
        prev = 0
        for upper in HTTP_HISTOGRAM_BUCKETS_MS:
            count = state.http_histogram.get(upper, 0)
            histogram.append({"le_ms": upper, "count": count, "pct": round(count * 100.0 / total_requests, 2) if total_requests else 0.0})
            prev = upper
        overflow = state.http_histogram.get(float("inf"), 0)
        histogram.append({"le_ms": "+Inf", "count": overflow, "pct": round(overflow * 100.0 / total_requests, 2) if total_requests else 0.0})
    # Per-URL Page Component Breakdown (LoadRunner Analysis equivalent): one
    # row per normalized request URL with count + percentile timings so each
    # page/API component's pressure and latency can be inspected independently
    # instead of being merged into a single host origin. Capped to the heaviest
    # 100 components to keep the report payload bounded.
    page_components = []
    for url_key, bucket in sorted(state.http_by_url.items(), key=lambda kv: kv[1]["count"], reverse=True)[:100]:
        page_components.append({
            "url": url_key,
            "method": bucket.get("method", ""),
            "resource_type": bucket.get("resource_type", "other"),
            "count": bucket["count"],
            "pct": round(bucket["count"] * 100.0 / total_requests, 2) if total_requests else 0.0,
            "total": _http_percentiles(bucket.get("total_ms") or []),
            "ttfb": _http_percentiles(bucket.get("ttfb_ms") or []),
            "dns": _http_percentiles(bucket.get("dns_ms") or []),
            "connect": _http_percentiles(bucket.get("connect_ms") or []),
            "tls": _http_percentiles(bucket.get("tls_ms") or []),
            "receive": _http_percentiles(bucket.get("receive_ms") or []),
        })
    return {
        "total_requests": total_requests,
        "distinct_url_count": len(state.http_by_url),
        "waterfall": waterfall,
        "by_type": {
            rtype: {
                "count": v["count"],
                "pct": round(v["count"] * 100.0 / total_requests, 2) if total_requests else 0.0,
                "total": _http_percentiles(v.get("total_ms") or []),
            }
            for rtype, v in state.http_by_type.items()
        },
        "page_components": page_components,
        "histogram": histogram,
    }


def _target_integrity(
    config: Dict[str, Any],
    request_targets: Any,
    recent_errors: Any,
) -> Dict[str, Any]:
    """Audit whether protocol samples accidentally used a known template target."""
    expected_target = _request_target((config or {}).get("base_url"))
    observed: Counter = Counter()
    if isinstance(request_targets, dict):
        for raw_target, raw_count in request_targets.items():
            target = _request_target(raw_target)
            if target:
                observed[target] += max(0, int(raw_count or 0))
    elif isinstance(request_targets, list):
        for item in request_targets:
            if not isinstance(item, dict):
                continue
            target = _request_target(item.get("target"))
            if target:
                observed[target] += max(0, int(item.get("count") or 0))

    evidence_source = "请求样本"
    if not observed and isinstance(recent_errors, list):
        evidence_source = "错误样本"
        for item in recent_errors:
            message = str(item.get("message") or "") if isinstance(item, dict) else ""
            match = re.search(r"\bhost=['\"]([^'\"]+)['\"]", message)
            if not match:
                continue
            scheme = "https" if "HTTPSConnectionPool" in message else "http"
            port_match = re.search(r"\bport=(\d+)", message)
            port = f":{port_match.group(1)}" if port_match else ""
            target = _request_target(f"{scheme}://{match.group(1)}{port}")
            if target:
                observed[target] += 1

    observed_rows = [
        {"target": target, "count": count, "source": evidence_source}
        for target, count in observed.most_common()
    ]
    placeholder_hosts = {"example.com", "www.example.com", "example.test", "www.example.test"}
    expected_host = urlsplit(expected_target).hostname if expected_target else ""
    placeholder_targets = [
        target for target in observed
        if urlsplit(target).hostname in placeholder_hosts
    ]
    issues = []
    if expected_target and expected_host not in placeholder_hosts and placeholder_targets:
        issues.append(
            f"发现请求发往模板占位地址 {', '.join(placeholder_targets)}，"
            f"与配置目标 {expected_target} 不一致；本次结果不能作为被测系统容量结论。"
        )
        status = "MISMATCH"
    elif expected_target and expected_target in observed:
        status = "MATCHED"
    else:
        status = "UNKNOWN"
    return {
        "status": status,
        "expected_target": expected_target,
        "observed_targets": observed_rows,
        "issues": issues,
    }


def _load_profile(flow: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("load_profile", "loadProfile", "performance", "load_test"):
        value = flow.get(key) if isinstance(flow, dict) else None
        if isinstance(value, dict):
            return copy.deepcopy(value)
    return {}


def _normalize_transaction_defs(flow: Dict[str, Any]) -> list[Dict[str, Any]]:
    profile = _load_profile(flow)
    raw = profile.get("transactions") or flow.get("transactions") or []
    transactions = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("transaction") or "").strip()
            if not name:
                continue
            steps = item.get("steps") if isinstance(item.get("steps"), list) else []
            transactions.append({
                "name": name,
                "start_step": str(item.get("start_step") or item.get("start") or "").strip(),
                "end_step": str(item.get("end_step") or item.get("end") or "").strip(),
                "steps": [str(value).strip() for value in steps if str(value).strip()],
                "auto_generated": False,
            })
    if transactions:
        return transactions

    steps = flow.get("steps") if isinstance(flow.get("steps"), list) else []
    required_steps = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or _step_is_optional(step):
            continue
        required_steps.append(str(step.get("id") or index))
    if required_steps:
        transactions.append({
            "name": str(profile.get("default_transaction_name") or "主业务事务"),
            "start_step": "",
            "end_step": "",
            "steps": required_steps,
            "auto_generated": True,
        })
    return transactions


def _step_is_optional(step: Dict[str, Any]) -> bool:
    optional = step.get("optional")
    if isinstance(optional, str):
        optional = optional.strip().lower() in {"1", "true", "yes", "on"}
    return bool(optional) or step.get("required") is False or step.get("load_required") is False


def _chain_completeness(
    flow: Dict[str, Any],
    step_results: Optional[list[Dict[str, Any]]],
) -> Dict[str, Any]:
    steps = flow.get("steps") if isinstance(flow.get("steps"), list) else []
    required = [
        (index, step)
        for index, step in enumerate(steps, start=1)
        if isinstance(step, dict) and not _step_is_optional(step)
    ]
    results = step_results if isinstance(step_results, list) else []
    if not required or not results:
        return {
            "evaluated": False,
            "complete": True,
            "required_steps": len(required),
            "passed_required_steps": 0,
            "failed_required_steps": 0,
            "skipped_required_steps": 0,
            "incomplete_steps": [],
        }

    by_id = {
        str(item.get("id") or ""): item
        for item in results
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    by_index = {
        int(item.get("index") or 0): item
        for item in results
        if isinstance(item, dict) and int(item.get("index") or 0) > 0
    }
    _skipped_statuses = {"skipped", "pending"}
    exempted_indices: set[int] = set()
    for index, step in required:
        if not step.get("allow_branch_skip"):
            continue
        result = by_id.get(str(step.get("id") or "")) or by_index.get(index) or {}
        if str(result.get("status") or "pending").lower() not in _skipped_statuses:
            continue
        exempted_indices.add(index)
        for next_idx in range(index + 1, len(steps) + 1):
            next_step = steps[next_idx - 1] if isinstance(steps[next_idx - 1], dict) else {}
            next_result = by_id.get(str(next_step.get("id") or "")) or by_index.get(next_idx) or {}
            if str(next_result.get("status") or "pending").lower() in _skipped_statuses:
                exempted_indices.add(next_idx)
            else:
                break
    effective_required = [(i, s) for i, s in required if i not in exempted_indices]
    passed = failed = skipped = 0
    incomplete_steps = []
    for index, step in effective_required:
        result = by_id.get(str(step.get("id") or "")) or by_index.get(index) or {}
        status = str(result.get("status") or "pending").lower()
        if status == "passed":
            passed += 1
            continue
        if status == "failed":
            failed += 1
        else:
            skipped += 1
        incomplete_steps.append({
            "index": index,
            "id": str(step.get("id") or ""),
            "name": str(step.get("name") or step.get("action") or f"步骤{index}"),
            "status": status,
        })
    return {
        "evaluated": True,
        "complete": failed == 0 and skipped == 0,
        "required_steps": len(effective_required),
        "passed_required_steps": passed,
        "failed_required_steps": failed,
        "skipped_required_steps": skipped,
        "incomplete_steps": incomplete_steps,
    }


def _step_token(step: Dict[str, Any], index: int) -> set[str]:
    tokens = {str(index)}
    for key in ("id", "name", "action", "type"):
        value = str(step.get(key) or "").strip()
        if value:
            tokens.add(value)
    return tokens


def _transaction_map(flow: Dict[str, Any]) -> Dict[str, str]:
    steps = flow.get("steps") if isinstance(flow.get("steps"), list) else []
    mapping: Dict[str, str] = {}
    explicit_defs = _normalize_transaction_defs(flow)
    for item in explicit_defs:
        active = False
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            tokens = _step_token(step, index)
            if item["steps"] and not tokens.intersection(item["steps"]):
                continue
            if item["start_step"] and tokens.intersection({item["start_step"]}):
                active = True
            if active or item["steps"]:
                for token in tokens:
                    mapping[token] = item["name"]
            if item["end_step"] and tokens.intersection({item["end_step"]}):
                active = False
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        name = str(step.get("transaction") or step.get("transaction_name") or "").strip()
        if name:
            for token in _step_token(step, index):
                mapping[token] = name
    return mapping


def _transaction_name_for_result(result: Dict[str, Any], mapping: Dict[str, str]) -> str:
    for value in (
        result.get("id"),
        result.get("name"),
        result.get("action"),
        result.get("index"),
        str(result.get("index") or ""),
    ):
        key = str(value or "").strip()
        if key and key in mapping:
            return mapping[key]
    return ""


def _action(step: Dict[str, Any]) -> str:
    return str(step.get("action") or step.get("type") or "").strip().lower()


def classify_flow(flow: Dict[str, Any]) -> Dict[str, Any]:
    steps = flow.get("steps") if isinstance(flow, dict) else []
    steps = steps if isinstance(steps, list) else []
    actions = [_action(step) for step in steps if isinstance(step, dict)]
    api_steps = sum(action in PROTOCOL_ACTIONS for action in actions)
    database_steps = sum(action in DATABASE_ACTIONS for action in actions)
    browser_steps = sum(action not in NON_BROWSER_ACTIONS for action in actions)
    if browser_steps and (api_steps or database_steps):
        recommended_mode = "mixed"
    elif browser_steps:
        recommended_mode = "ui"
    else:
        recommended_mode = "protocol"
    return {
        "recommended_mode": recommended_mode,
        "step_count": len(actions),
        "browser_steps": browser_steps,
        "api_steps": api_steps,
        "database_steps": database_steps,
        "has_browser": browser_steps > 0,
    }


def _materialize_test_objects(value: Any, bundle: Dict[str, Dict[str, Any]]) -> Any:
    if isinstance(value, list):
        return [_materialize_test_objects(item, bundle) for item in value]
    if not isinstance(value, dict):
        return copy.deepcopy(value)

    ref = value.get("test_object_ref")
    if isinstance(ref, dict):
        team_id = str(ref.get("team_id") or "").strip()
        target_id = str(ref.get("target_id") or "").strip()
        target_type = str(ref.get("target_type") or "").strip().lower()
        item = bundle.get(f"{team_id}:{target_id}")
        if not isinstance(item, dict) or not isinstance(item.get("step"), dict):
            raise runnergo_test_objects.TestObjectError(
                f"测试对象未在本次压测中解析: {ref.get('name') or target_id}"
            )
        if str(item.get("target_type") or "").strip().lower() != target_type:
            raise runnergo_test_objects.TestObjectError(
                f"测试对象类型不匹配: {ref.get('name') or target_id}"
            )
        merged = copy.deepcopy(item["step"])
        for key, item_value in value.items():
            if key == "test_object_ref":
                continue
            if (
                key in {"headers", "params", "extract", "extract_vars"}
                and isinstance(item_value, dict)
                and isinstance(merged.get(key), dict)
            ):
                merged[key].update(copy.deepcopy(item_value))
            else:
                merged[key] = copy.deepcopy(item_value)
        return _materialize_test_objects(merged, bundle)

    return {key: _materialize_test_objects(item, bundle) for key, item in value.items()}


class LoadRunIn(BaseModel):
    team_id: str
    project_id: str
    case_file: str = ""
    case_files: list[str] = Field(default_factory=list)
    scenario_weights: Dict[str, int] = Field(default_factory=dict)
    mode: str = "auto"
    execution_mode: str = "local"  # local / distributed / auto
    agent_count: int = 0             # 0 = use all available load agents
    virtual_users: int = 1
    ramp_up_seconds: int = 0
    duration_seconds: int = 60
    ramp_down_seconds: int = 0
    think_time_ms: int = 0
    pacing_seconds: float = 0
    pacing_random_pct: float = 0
    rendezvous_enabled: bool = False
    rendezvous_timeout_seconds: int = 30
    iterations_per_user: int = 0
    headless: bool = True
    env: str = "test"
    base_url: str = ""
    runtime_variables: Dict[str, Any] = Field(default_factory=dict)
    data_rows: list[Dict[str, Any]] = Field(default_factory=list)
    sla: Dict[str, Any] = Field(default_factory=dict)
    monitoring: Dict[str, Any] = Field(default_factory=dict)
    auto_randomize: bool = False
    data_asset_alias: str = ""
    data_center_api_url: str = ""
    data_assignment_mode: str = "unique"
    goal: Dict[str, Any] = Field(default_factory=dict)
    ip_spoofing: Dict[str, Any] = Field(default_factory=dict)
    breakpoints: list[str] = Field(default_factory=list)
    protocols: Dict[str, Any] = Field(default_factory=dict)
    scenario_groups: list[Dict[str, Any]] = Field(default_factory=list)


class BatchDeleteLoadRunsIn(BaseModel):
    project_id: str
    run_ids: list[str] = Field(default_factory=list)


@dataclass
class LoadRunState:
    run_id: str
    project_id: str
    case_file: str
    scenario_name: str
    mode: str
    flow: Dict[str, Any]
    config: Dict[str, Any]
    flows: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    started_at: str = ""
    finished_at: str = ""
    status: str = "pending"
    error_message: str = ""
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    controller: Optional[threading.Thread] = field(default=None, repr=False)
    started_clock: float = field(default=0.0, repr=False)
    finished_clock: float = field(default=0.0, repr=False)
    completed: int = 0
    passed: int = 0
    failed: int = 0
    iteration_durations: list[float] = field(default_factory=list, repr=False)
    step_metrics: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False)
    transaction_metrics: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False)
    scenario_metrics: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False)
    error_types: Counter = field(default_factory=Counter, repr=False)
    request_targets: Counter = field(default_factory=Counter, repr=False)
    recent_errors: deque = field(default_factory=lambda: deque(maxlen=20), repr=False)
    timeline: Dict[int, Dict[str, Any]] = field(default_factory=dict, repr=False)
    active_vus: set[int] = field(default_factory=set, repr=False)
    peak_active_vus: int = 0
    evaluated_chains: int = 0
    complete_chains: int = 0
    incomplete_chains: int = 0
    required_step_checks: int = 0
    passed_required_steps: int = 0
    failed_required_steps: int = 0
    skipped_required_steps: int = 0
    iterations_by_vu: Dict[int, int] = field(default_factory=lambda: defaultdict(int), repr=False)
    logs: deque = field(default_factory=lambda: deque(maxlen=80), repr=False)
    http_total_ms_samples: list = field(default_factory=list, repr=False)
    http_request_count: int = 0
    http_histogram: Dict[Any, int] = field(default_factory=dict, repr=False)
    http_by_type: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False)
    http_by_url: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False)
    sample_callback: Optional[Callable[[dict], None]] = field(default=None, repr=False)
    agent_ids: list[str] = field(default_factory=list, repr=False)
    finished_agents: set[str] = field(default_factory=set, repr=False)
    agent_errors: Dict[str, str] = field(default_factory=dict, repr=False)
    agent_active_vus: Dict[str, int] = field(default_factory=dict, repr=False)
    rendezvous_barriers: Dict[str, threading.Barrier] = field(default_factory=dict, repr=False)
    resource_samples: list[Dict[str, Any]] = field(default_factory=list, repr=False)
    resource_sample_count: int = field(default=0, repr=False)
    monitor_callback: Optional[Callable[[dict], None]] = field(default=None, repr=False)
    vu_override: Optional[int] = field(default=None, repr=False)
    rtds_store: Dict[str, Any] = field(default_factory=dict, repr=False)
    rtds_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    debug_pause_event: threading.Event = field(default_factory=threading.Event, repr=False)
    debug_step_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def active_vu_count(self) -> int:
        if self.agent_ids:
            return sum(max(0, int(value)) for value in self.agent_active_vus.values())
        return len(self.active_vus)

    @property
    def total_duration_seconds(self) -> int:
        return (
            int(self.config.get("ramp_up_seconds") or 0)
            + int(self.config.get("duration_seconds") or 0)
            + int(self.config.get("ramp_down_seconds") or 0)
        )

    def log(self, message: str) -> None:
        with self.lock:
            self.logs.append({"time": _now(), "message": str(message)[:1000]})

    def rtds_put(self, key: str, value: Any) -> None:
        with self.rtds_lock:
            self.rtds_store[key] = value

    def rtds_get(self, key: str, default: Any = None) -> Any:
        with self.rtds_lock:
            return self.rtds_store.get(key, default)

    def rtds_pop(self, key: str, default: Any = None) -> Any:
        with self.rtds_lock:
            return self.rtds_store.pop(key, default)

    def target_vus(self, elapsed: Optional[float] = None) -> int:
        with self.lock:
            override = self.vu_override
        if override is not None:
            return max(0, int(override))
        elapsed = self.elapsed_seconds() if elapsed is None else max(0.0, float(elapsed))
        virtual_users = int(self.config.get("virtual_users") or 0)
        ramp_up = int(self.config.get("ramp_up_seconds") or 0)
        steady = int(self.config.get("duration_seconds") or 0)
        ramp_down = int(self.config.get("ramp_down_seconds") or 0)
        if ramp_up > 0 and elapsed < ramp_up:
            return min(virtual_users, max(1, int(math.ceil(virtual_users * elapsed / ramp_up))))
        elapsed -= ramp_up
        if elapsed < steady:
            return virtual_users
        elapsed -= steady
        if ramp_down > 0 and elapsed < ramp_down:
            remaining = max(0.0, 1.0 - elapsed / ramp_down)
            return max(0, min(virtual_users, int(math.ceil(virtual_users * remaining))))
        return 0

    def elapsed_seconds(self) -> float:
        if not self.started_clock:
            return 0.0
        end = self.finished_clock or time.monotonic()
        return max(0.0, end - self.started_clock)

    def set_vu_active(self, vu_id: int, active: bool) -> None:
        with self.lock:
            if active:
                self.active_vus.add(vu_id)
                self.peak_active_vus = max(self.peak_active_vus, len(self.active_vus))
            else:
                self.active_vus.discard(vu_id)

    def record_resource_sample(self, sample: Dict[str, Any]) -> None:
        if not isinstance(sample, dict):
            return
        with self.lock:
            self.resource_sample_count += 1
            if len(self.resource_samples) < MAX_RESOURCE_SAMPLES:
                self.resource_samples.append(copy.deepcopy(sample))
            elif self.resource_samples:
                index = (self.resource_sample_count - 1) % MAX_RESOURCE_SAMPLES
                self.resource_samples[index] = copy.deepcopy(sample)
        callback = self.monitor_callback
        if callback is not None:
            try:
                callback({"kind": "monitor", "sample": copy.deepcopy(sample)})
            except Exception as exc:
                self.log(f"资源监控回传失败: {type(exc).__name__}: {exc}")

    def record_iteration(
        self,
        *,
        vu_id: int,
        iteration: int,
        duration_ms: float,
        success: bool,
        error: str = "",
        error_type: str = "",
        step_results: Optional[list[Dict[str, Any]]] = None,
        elapsed_seconds: Optional[float] = None,
        scenario_file: str = "",
    ) -> None:
        iteration_flow = self.flows.get(scenario_file) or self.flow
        chain = _chain_completeness(iteration_flow, step_results)
        if success and chain["evaluated"] and not chain["complete"]:
            skipped_names = [
                item["name"] for item in chain["incomplete_steps"]
                if item["status"] != "failed"
            ]
            error = "业务链路不完整：必需步骤未执行"
            if skipped_names:
                error += f"（{'、'.join(skipped_names[:3])}）"
            error_type = "BusinessChainIncomplete"
            success = False
        elapsed = self.elapsed_seconds() if elapsed_seconds is None else max(0.0, float(elapsed_seconds))
        bucket_index = max(0, int(elapsed))
        with self.lock:
            self.completed += 1
            self.passed += int(success)
            self.failed += int(not success)
            self.iterations_by_vu[vu_id] = max(self.iterations_by_vu[vu_id], iteration)
            _bounded_sample(self.iteration_durations, duration_ms, self.completed)
            if chain["evaluated"]:
                self.evaluated_chains += 1
                self.complete_chains += int(chain["complete"])
                self.incomplete_chains += int(not chain["complete"])
                self.required_step_checks += int(chain["required_steps"])
                self.passed_required_steps += int(chain["passed_required_steps"])
                self.failed_required_steps += int(chain["failed_required_steps"])
                self.skipped_required_steps += int(chain["skipped_required_steps"])
            scenario = next(
                (
                    item for item in self.config.get("scenarios", [])
                    if str(item.get("filename") or "") == scenario_file
                ),
                {},
            ) if scenario_file else {}
            scenario_name = str(scenario.get("name") or scenario_file or self.scenario_name)
            scenario_key = str(scenario_file or scenario_name)
            scenario_metric = self.scenario_metrics.setdefault(scenario_key, {
                "name": scenario_name,
                "filename": scenario_file or self.case_file,
                "weight": int(scenario.get("weight") or 1),
                "count": 0,
                "passed": 0,
                "failed": 0,
                "duration_sum_ms": 0.0,
                "durations": [],
            })
            scenario_metric["count"] += 1
            scenario_metric["passed"] += int(success)
            scenario_metric["failed"] += int(not success)
            scenario_metric["duration_sum_ms"] += float(duration_ms)
            _bounded_sample(scenario_metric["durations"], duration_ms, scenario_metric["count"])
            if not success:
                normalized_type = error_type or "ExecutionError"
                self.error_types[normalized_type] += 1
                self.recent_errors.append({
                    "vu": vu_id,
                    "iteration": iteration,
                    "type": normalized_type,
                    "message": str(error)[:1000],
                    "elapsed_seconds": round(elapsed, 2),
                })

            bucket = self.timeline.setdefault(bucket_index, {
                "second": bucket_index,
                "completed": 0,
                "failed": 0,
                "duration_sum_ms": 0.0,
                "durations": [],
                "active_vus": 0,
            })
            bucket["completed"] += 1
            bucket["failed"] += int(not success)
            bucket["duration_sum_ms"] += float(duration_ms)
            _bounded_sample(bucket["durations"], duration_ms, bucket["completed"])
            bucket["active_vus"] = max(bucket["active_vus"], self.active_vu_count())

            for result in step_results or []:
                status = str(result.get("status") or "").lower()
                if status not in {"passed", "failed", "skipped"}:
                    continue
                index = int(result.get("index") or 0)
                name = str(result.get("name") or result.get("action") or f"步骤{index}")
                action = str(result.get("action") or "")
                key = str(result.get("id") or f"{index}:{name}")
                metric = self.step_metrics.setdefault(key, {
                    "key": key,
                    "index": index,
                    "name": name,
                    "action": action,
                    "count": 0,
                    "passed": 0,
                    "failed": 0,
                    "skipped": 0,
                    "duration_sum_ms": 0.0,
                    "durations": [],
                    "status_codes": Counter(),
                    "request_targets": Counter(),
                    "http_request_count": 0,
                    "http_timings_by_type": {},
                    "http_total_ms_samples": [],
                })
                metric["count"] += 1
                metric[status] += 1
                step_duration_ms = max(0.0, float(result.get("duration") or 0) * 1000)
                metric["duration_sum_ms"] += step_duration_ms
                _bounded_sample(metric["durations"], step_duration_ms, metric["count"])
                diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
                status_code = diagnostics.get("status")
                if status_code is not None:
                    metric["status_codes"][str(status_code)] += 1
                request_target = _request_target(diagnostics.get("url"))
                if request_target:
                    metric["request_targets"][request_target] += 1
                    self.request_targets[request_target] += 1
                http_timings = diagnostics.get("http_timings") if isinstance(diagnostics.get("http_timings"), list) else []
                if http_timings:
                    _aggregate_http_timings(metric, http_timings, self)
            transaction_maps = self.config.get("transaction_maps") if isinstance(self.config.get("transaction_maps"), dict) else {}
            transaction_map = transaction_maps.get(scenario_file) if isinstance(transaction_maps.get(scenario_file), dict) else self.config.get("transaction_map")
            transaction_map = transaction_map if isinstance(transaction_map, dict) else {}
            transaction_buckets: Dict[str, Dict[str, Any]] = {}
            for result in step_results or []:
                status = str(result.get("status") or "").lower()
                if status not in {"passed", "failed", "skipped"}:
                    continue
                transaction_name = _transaction_name_for_result(result, transaction_map)
                if not transaction_name:
                    continue
                bucket = transaction_buckets.setdefault(transaction_name, {
                    "name": transaction_name,
                    "duration_ms": 0.0,
                    "failed": 0,
                    "skipped": 0,
                    "steps": 0,
                })
                bucket["duration_ms"] += max(0.0, float(result.get("duration") or 0) * 1000)
                bucket["failed"] += int(status == "failed")
                bucket["skipped"] += int(status == "skipped")
                bucket["steps"] += 1
            for transaction_name, bucket in transaction_buckets.items():
                metric = self.transaction_metrics.setdefault(transaction_name, {
                    "name": transaction_name,
                    "count": 0,
                    "passed": 0,
                    "failed": 0,
                    "skipped": 0,
                    "duration_sum_ms": 0.0,
                    "durations": [],
                    "step_count": 0,
                })
                metric["count"] += 1
                metric["failed"] += int(bucket["failed"] > 0)
                metric["skipped"] += int(bucket["skipped"] > 0 and bucket["failed"] == 0)
                metric["passed"] += int(bucket["failed"] == 0)
                metric["duration_sum_ms"] += bucket["duration_ms"]
                metric["step_count"] += bucket["steps"]
                _bounded_sample(metric["durations"], bucket["duration_ms"], metric["count"])
        callback = self.sample_callback
        if callback is not None:
            compact_steps = []
            for result in step_results or []:
                diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
                compact_diagnostics = {}
                if diagnostics.get("status") is not None:
                    compact_diagnostics["status"] = diagnostics.get("status")
                request_target = _request_target(diagnostics.get("url"))
                if request_target:
                    compact_diagnostics["url"] = request_target
                compact_steps.append({
                    "index": result.get("index"),
                    "id": result.get("id"),
                    "name": result.get("name"),
                    "action": result.get("action"),
                    "status": result.get("status"),
                    "duration": result.get("duration", 0),
                    "diagnostics": compact_diagnostics,
                })
            try:
                callback({
                    "kind": "sample",
                    "active_vus": len(self.active_vus),
                    "vu_id": vu_id,
                    "iteration": iteration,
                    "duration_ms": float(duration_ms),
                    "success": bool(success),
                    "error": str(error)[:1000],
                    "error_type": str(error_type or ""),
                    "scenario_file": scenario_file,
                    "elapsed_seconds": round(elapsed, 3),
                    "step_results": compact_steps,
                    "chain_completeness": chain,
                })
            except Exception as exc:
                self.log(f"样本回传失败: {type(exc).__name__}: {exc}")

    def _sla_result(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        sla = self.config.get("sla") if isinstance(self.config.get("sla"), dict) else {}
        rules = []
        checks = (
            ("min_tps", "TPS", metrics.get("tps", 0), lambda actual, expected: actual >= expected),
            ("max_avg_ms", "平均", metrics.get("avg_ms", 0), lambda actual, expected: actual <= expected),
            ("max_p90_ms", "P90", metrics.get("p90_ms", 0), lambda actual, expected: actual <= expected),
            ("max_p95_ms", "P95", metrics.get("p95_ms", 0), lambda actual, expected: actual <= expected),
            ("max_p99_ms", "P99", metrics.get("p99_ms", 0), lambda actual, expected: actual <= expected),
            ("max_error_rate", "错误率", metrics.get("error_rate", 0), lambda actual, expected: actual <= expected),
            ("min_chain_success_rate", "链路成功率", (metrics.get("chain_integrity") or {}).get("success_rate", 0), lambda actual, expected: actual >= expected),
        )
        for key, label, actual, predicate in checks:
            raw_expected = sla.get(key)
            if raw_expected in {None, ""}:
                continue
            try:
                expected = float(raw_expected)
            except (TypeError, ValueError):
                continue
            passed = bool(predicate(float(actual), expected))
            rules.append({
                "key": key,
                "label": label,
                "actual": round(float(actual), 2),
                "expected": expected,
                "passed": passed,
            })
        if not rules:
            status = "NOT_CONFIGURED"
        elif self.status in RUNNING_STATUSES:
            status = "RUNNING"
        else:
            status = "PASS" if all(rule["passed"] for rule in rules) else "FAIL"
        return {"status": status, "rules": rules}

    def summary(self) -> Dict[str, Any]:
        with self.lock:
            elapsed = max(0.001, self.elapsed_seconds())
            durations = list(self.iteration_durations)
            completed = self.completed
            failed = self.failed
            transactions = []
            protocol_count = 0
            for metric in sorted(self.step_metrics.values(), key=lambda item: (item["index"], item["name"])):
                metric_durations = list(metric["durations"])
                action = str(metric["action"] or "").lower()
                if action in PROTOCOL_ACTIONS:
                    protocol_count += int(metric["count"])
                transactions.append({
                    "key": metric["key"],
                    "index": metric["index"],
                    "name": metric["name"],
                    "action": metric["action"],
                    "count": metric["count"],
                    "passed": metric["passed"],
                    "failed": metric["failed"],
                    "skipped": metric["skipped"],
                    "avg_ms": round(metric["duration_sum_ms"] / max(1, metric["count"]), 2),
                    "p95_ms": _percentile(metric_durations, 0.95),
                    "p99_ms": _percentile(metric_durations, 0.99),
                    "tps": round(metric["count"] / elapsed, 2),
                    "status_codes": dict(metric["status_codes"]),
                    "request_targets": [
                        {"target": target, "count": count}
                        for target, count in metric["request_targets"].most_common()
                    ],
                })
            business_transactions = []
            for metric in sorted(self.transaction_metrics.values(), key=lambda item: item["name"]):
                metric_durations = list(metric["durations"])
                business_transactions.append({
                    "name": metric["name"],
                    "count": metric["count"],
                    "passed": metric["passed"],
                    "failed": metric["failed"],
                    "skipped": metric["skipped"],
                    "avg_ms": round(metric["duration_sum_ms"] / max(1, metric["count"]), 2),
                    "p95_ms": _percentile(metric_durations, 0.95),
                    "p99_ms": _percentile(metric_durations, 0.99),
                    "tps": round(metric["count"] / elapsed, 2),
                    "pass_tps": round(metric["passed"] / elapsed, 2),
                    "pass_rate": round(metric["passed"] * 100 / max(1, metric["count"]), 2),
                    "avg_step_count": round(metric["step_count"] / max(1, metric["count"]), 2),
                })
            scenarios = []
            for metric in sorted(self.scenario_metrics.values(), key=lambda item: item["name"]):
                metric_durations = list(metric["durations"])
                scenarios.append({
                    "name": metric["name"],
                    "filename": metric["filename"],
                    "weight": metric["weight"],
                    "count": metric["count"],
                    "passed": metric["passed"],
                    "failed": metric["failed"],
                    "avg_ms": round(metric["duration_sum_ms"] / max(1, metric["count"]), 2),
                    "p95_ms": _percentile(metric_durations, 0.95),
                    "p99_ms": _percentile(metric_durations, 0.99),
                    "tps": round(metric["count"] / elapsed, 2),
                    "pass_rate": round(metric["passed"] * 100 / max(1, metric["count"]), 2),
                })
            timeline = []
            for second, bucket in sorted(self.timeline.items()):
                count = int(bucket["completed"])
                timeline.append({
                    "second": second,
                    "completed": count,
                    "failed": int(bucket["failed"]),
                    "tps": count,
                    "avg_ms": round(bucket["duration_sum_ms"] / max(1, count), 2),
                    "p95_ms": _percentile(bucket["durations"], 0.95),
                    "active_vus": int(bucket["active_vus"]),
                })
            throughput = _throughput_metrics(timeline, self.config, elapsed, completed)
            planned_duration = max(0.0, float(self.total_duration_seconds))
            chain_integrity = {
                "status": (
                    "UNKNOWN" if self.evaluated_chains == 0
                    else "COMPLETE" if self.incomplete_chains == 0
                    else "INCOMPLETE"
                ),
                "evaluated_chains": self.evaluated_chains,
                "successful_chains": self.complete_chains,
                "incomplete_chains": self.incomplete_chains,
                "success_rate": round(self.complete_chains * 100 / max(1, self.evaluated_chains), 2),
                "required_step_checks": self.required_step_checks,
                "passed_required_steps": self.passed_required_steps,
                "failed_required_steps": self.failed_required_steps,
                "skipped_required_steps": self.skipped_required_steps,
                "step_completion_rate": round(self.passed_required_steps * 100 / max(1, self.required_step_checks), 2),
                "estimated": False,
            }
            metrics = {
                "elapsed_seconds": round(elapsed, 2),
                "planned_duration_seconds": round(planned_duration, 2),
                "load_window_seconds": round(min(elapsed, planned_duration), 2),
                "drain_duration_seconds": round(max(0.0, elapsed - planned_duration), 2),
                "target_vus": self.target_vus(elapsed),
                "active_vus": self.active_vu_count(),
                "peak_active_vus": self.peak_active_vus,
                "completed": completed,
                "passed": self.passed,
                "failed": failed,
                "error_rate": round(failed * 100 / max(1, completed), 2),
                **throughput,
                "protocol_rps": round(protocol_count / elapsed, 2),
                "avg_ms": round(sum(durations) / max(1, len(durations)), 2),
                "min_ms": round(min(durations), 2) if durations else 0.0,
                "max_ms": round(max(durations), 2) if durations else 0.0,
                "p50_ms": _percentile(durations, 0.50),
                "p90_ms": _percentile(durations, 0.90),
                "p95_ms": _percentile(durations, 0.95),
                "p99_ms": _percentile(durations, 0.99),
                "chain_integrity": chain_integrity,
            }
            resource_monitoring = _resource_summary(list(self.resource_samples))
            resource_monitoring["sample_count_total"] = self.resource_sample_count
            return {
                **metrics,
                "transactions": transactions,
                "scenarios": scenarios,
                "business_transactions": business_transactions,
                "timeline": timeline,
                "error_types": dict(self.error_types),
                "recent_errors": list(self.recent_errors),
                "request_targets": [
                    {"target": target, "count": count}
                    for target, count in self.request_targets.most_common()
                ],
                "target_integrity": _target_integrity(
                    self.config,
                    self.request_targets,
                    list(self.recent_errors),
                ),
                "resource_monitoring": resource_monitoring,
                "http_waterfall": _http_summary(self),
                "iterations_by_vu": {str(key): value for key, value in self.iterations_by_vu.items()},
                "metric_semantics": dict(METRIC_SEMANTICS),
                "data_quality": _data_quality(
                    completed=completed,
                    failed=failed,
                    elapsed=elapsed,
                    timeline=timeline,
                    durations=durations,
                ),
                "sla": self._sla_result(metrics),
                "sample_limit": MAX_PERCENTILE_SAMPLES,
            }

    def public(self, compact: bool = False) -> Dict[str, Any]:
        public_config = redact(self.config)
        if compact:
            public_config = {
                key: public_config.get(key)
                for key in (
                    "mode", "virtual_users", "ramp_up_seconds", "duration_seconds",
                    "ramp_down_seconds", "think_time_ms", "iterations_per_user",
                    "pacing_seconds", "pacing_random_pct", "rendezvous_enabled",
                    "rendezvous_timeout_seconds", "headless", "env", "base_url",
                    "connection_reuse", "team_id", "planned_vum", "remaining_vum",
                    "execution_mode", "agent_count", "distributed_capacity_vus",
                )
            }
            public_config["runtime_variable_count"] = len(self.config.get("runtime_variables") or {})
            public_config["data_row_count"] = len(self.config.get("data_rows") or [])
            public_config["monitoring_enabled"] = bool((self.config.get("monitoring") or {}).get("enabled"))
        summary = self.summary()
        if compact:
            summary = _compact_summary(summary)
        return {
            "id": self.run_id,
            "project_id": self.project_id,
            "case_file": self.case_file,
            "scenario_name": self.scenario_name,
            "mode": self.mode,
            "status": self.status,
            "config": public_config,
            "summary": summary,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "logs": [] if compact else list(self.logs),
        }


_RUNS: Dict[str, LoadRunState] = {}
_RUNS_LOCK = threading.RLock()


def _persist_new(state: LoadRunState) -> None:
    db.execute(
        "INSERT INTO load_test_runs("
        "id,project_id,case_file,scenario_name,mode,status,config_json,summary_json,error_message,"
        "created_at,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            state.run_id, state.project_id, state.case_file, state.scenario_name, state.mode,
            state.status, _json(redact(state.config)), _json(state.summary()), state.error_message,
            state.created_at, state.started_at, state.finished_at,
        ),
    )


def _persist_state(state: LoadRunState) -> None:
    db.execute(
        "UPDATE load_test_runs SET status=?,config_json=?,summary_json=?,error_message=?,started_at=?,finished_at=? WHERE id=?",
        (
            state.status, _json(redact(state.config)), _json(state.summary()), state.error_message,
            state.started_at, state.finished_at, state.run_id,
        ),
    )


def _infer_random_expr(element_label: str, input_type: str = "") -> str:
    label = str(element_label or "").lower()
    it = str(input_type or "").lower()
    if any(k in label for k in ("手机", "phone", "电话")) or it == "tel":
        return "${random_phone}"
    if any(k in label for k in ("验证码", "code", "captcha")):
        return "${random_code(6)}"
    if any(k in label for k in ("身份证", "idcard", "id_card")):
        return "${random_idcard}"
    if any(k in label for k in ("邮箱", "email", "mail")):
        return "${random_email}"
    if any(k in label for k in ("姓名", "name", "用户名")):
        return "${random_name}"
    if any(k in label for k in ("车牌", "plate", "车牌号")):
        return "${random_plate}"
    if any(k in label for k in ("金额", "amount", "price", "价格")):
        return "${random_amount}"
    if any(k in label for k in ("密码", "password", "pwd")):
        return "${random_string(12)}"
    return "${random_string(10)}"


def _auto_randomize_flow(flow: Dict[str, Any]) -> None:
    for step in flow.get("steps") or []:
        if not isinstance(step, dict):
            continue
        action = str(step.get("action") or "").strip().lower()
        if action not in ("fill", "input", "type", "send_keys", "input_text", "smart_input"):
            continue
        value = step.get("value")
        if not isinstance(value, str) or not value.strip():
            continue
        if value.strip().startswith("${"):
            continue
        element = step.get("element") or {}
        label = ""
        if isinstance(element, dict):
            label = element.get("label") or element.get("name") or element.get("accessible_name") or ""
        input_type = element.get("input_type", "") if isinstance(element, dict) else ""
        label_lower = str(label or "").lower()
        if any(k in label_lower for k in ("验证码", "code", "captcha", "短信")):
            continue
        if any(k in label_lower for k in ("手机", "phone", "mobile", "电话")):
            continue
        step["value"] = _infer_random_expr(label, input_type)


def _fetch_data_asset_rows(alias: str, count: int, asset_type: str = "PHONE", api_url: str = "") -> list[Dict[str, Any]]:
    if count <= 0 or not alias:
        return []
    rows: list[Dict[str, Any]] = []
    wanted_type = str(asset_type or "PHONE").upper()

    def _extract(pl):
        if isinstance(pl, list):
            for item in pl:
                if isinstance(item, dict) and ("phone" in item or "code" in item):
                    rows.append(item)
        elif isinstance(pl, dict) and ("phone" in pl or "code" in pl):
            rows.append(pl)

    api_base = (api_url or os.environ.get("TEST_DATA_CENTER_API_URL", "")).strip()
    if api_base:
        try:
            import requests as _requests
            token = os.environ.get("TEST_DATA_CENTER_AGENT_TOKEN", "runnergo-local-agent-token")
            resp = _requests.get(
                api_base.rstrip("/") + "/api/core/test-data-assets/agent-context/",
                params={"aliases": alias},
                headers={"X-Agent-Token": token},
                timeout=10,
            )
            if resp.status_code < 400:
                for asset in (resp.json().get("assets") or []):
                    if not isinstance(asset, dict) or asset.get("status") != "AVAILABLE":
                        continue
                    if str(asset.get("asset_type") or "").upper() != wanted_type:
                        continue
                    pl = asset.get("payload")
                    if isinstance(pl, dict) and pl.get("_runnergo_source") == "bulk_test_data" and asset.get("bulk_rows"):
                        _extract(asset.get("bulk_rows"))
                    else:
                        _extract(pl)
                    if len(rows) >= count:
                        break
        except Exception:
            rows.clear()

    if len(rows) < count:
        db_path = os.environ.get(
            "TEST_DATA_ASSETS_DB_PATH",
            os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "testhub", "db.sqlite3")),
        )
        if os.path.isfile(db_path):
            try:
                import sqlite3 as _sqlite3
                conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                conn.row_factory = _sqlite3.Row
                for row in conn.execute(
                    "SELECT payload FROM test_data_assets WHERE status='AVAILABLE' AND asset_type=? ORDER BY id",
                    (wanted_type,),
                ).fetchall():
                    try:
                        pl = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
                    except Exception:
                        continue
                    _extract(pl)
                    if len(rows) >= count:
                        break
                conn.close()
            except Exception:
                pass
    return rows[:count]


def _fallback_data_rows(count: int) -> list[Dict[str, Any]]:
    import random as _r, string as _s
    prefixes = (
        "134", "135", "136", "137", "138", "139", "147", "148", "150", "151", "152",
        "157", "158", "159", "165", "172", "178", "182", "183", "184", "187", "188",
        "195", "197", "198",
        "130", "131", "132", "145", "146", "155", "156", "166", "171", "175", "176",
        "185", "186", "196",
        "133", "149", "153", "173", "177", "180", "181", "189", "190", "191", "192",
        "193", "199",
    )
    rows: list[Dict[str, Any]] = []
    for _ in range(max(0, count)):
        phone = _r.choice(prefixes) + "".join(_r.choice(_s.digits) for _ in range(8))
        code = "".join(_r.choice(_s.digits) for _ in range(6))
        rows.append({"phone": phone, "code": code})
    return rows


def _flow_for_iteration(state: LoadRunState, vu_id: int, iteration: int) -> Dict[str, Any]:
    scenario_groups = state.config.get("scenario_groups") or []
    if scenario_groups:
        vu_offset = 0
        for group in scenario_groups:
            group_vus = max(1, int(group.get("virtual_users") or 1))
            if vu_offset < vu_id <= vu_offset + group_vus:
                group_file = str(group.get("case_file") or "")
                if group_file in state.flows:
                    scenario_file = group_file
                    flow = copy.deepcopy(state.flows.get(scenario_file) or state.flow)
                    if state.config.get("auto_randomize"):
                        _auto_randomize_flow(flow)
                    variables = flow.get("variables") if isinstance(flow.get("variables"), dict) else {}
                    variables = copy.deepcopy(variables)
                    variables.update(copy.deepcopy(state.config.get("runtime_variables") or {}))
                    data_rows = state.config.get("data_rows") or []
                    if data_rows:
                        mode = str(state.config.get("data_assignment_mode") or "unique").lower()
                        if mode == "random":
                            row = random.choice(data_rows)
                        elif mode == "sequential":
                            row = data_rows[(vu_id - 1 + iteration - 1) % len(data_rows)]
                        else:
                            row = data_rows[(vu_id - 1) % len(data_rows)]
                        flow.setdefault("variables", {}).update(row)
                    flow["variables"] = variables
                    flow["_load_scenario_file"] = scenario_file
                    return flow
            vu_offset += group_vus
    scenarios = state.config.get("scenarios") if isinstance(state.config.get("scenarios"), list) else []
    weighted_files = [
        str(item.get("filename") or "")
        for item in scenarios
        for _ in range(max(1, min(100, int(item.get("weight") or 1))))
        if str(item.get("filename") or "") in state.flows
    ]
    scenario_file = weighted_files[(vu_id - 1) % len(weighted_files)] if weighted_files else state.case_file
    flow = copy.deepcopy(state.flows.get(scenario_file) or state.flow)
    if state.config.get("auto_randomize"):
        _auto_randomize_flow(flow)
    variables = flow.get("variables") if isinstance(flow.get("variables"), dict) else {}
    variables = copy.deepcopy(variables)
    variables.update(copy.deepcopy(state.config.get("runtime_variables") or {}))
    data_rows = state.config.get("data_rows") or []
    if data_rows:
        mode = str(state.config.get("data_assignment_mode") or "unique").lower()
        if mode == "random":
            row = data_rows[random.randrange(len(data_rows))]
        elif mode == "sequential":
            row = data_rows[(vu_id - 1 + iteration - 1) % len(data_rows)]
        else:
            row = data_rows[(vu_id - 1) % len(data_rows)]
        row = copy.deepcopy(row)
        if isinstance(row, dict):
            variables.update(row)
            variables.setdefault("data", row)
            variables.setdefault("dataAssets", row)
    variables.update({"vu_id": vu_id, "iteration": iteration, "load_run_id": state.run_id})
    flow["variables"] = variables
    flow["_runtime_base_url"] = state.config.get("base_url") or flow.get("base_url") or ""
    flow["_performance_mode"] = True
    flow["_load_scenario_file"] = scenario_file
    flow["strict_full_chain"] = bool(state.config.get("strict_full_chain", True))
    for step in flow.get("steps") or []:
        if isinstance(step, dict):
            step["capture_screenshot"] = False
            if state.config.get("strict_full_chain", True):
                step["disable_auto_page_skip"] = True
    return flow


def _rendezvous_wait(state: LoadRunState, vu_id: int, name: str = "iteration") -> None:
    config = state.config
    if not bool(config.get("rendezvous_enabled")):
        return
    parties = int(config.get("rendezvous_parties") or config.get("virtual_users") or 1)
    if parties <= 1 or state.agent_ids:
        return
    timeout = max(1.0, float(config.get("rendezvous_timeout_seconds") or 30))
    with state.lock:
        barrier = state.rendezvous_barriers.get(name)
        if barrier is None:
            barrier = threading.Barrier(parties=parties, timeout=timeout)
            state.rendezvous_barriers[name] = barrier
    try:
        state.log(f"VU {vu_id} 到达集合点 {name}")
        barrier.wait(timeout=timeout)
    except threading.BrokenBarrierError:
        state.log(f"集合点 {name} 等待超时，继续执行")


def _post_iteration_wait(state: LoadRunState, iteration_started: float) -> None:
    think_time = max(0, int(state.config.get("think_time_ms") or 0)) / 1000
    pacing = max(0.0, float(state.config.get("pacing_seconds") or 0))
    random_pct = max(0.0, min(100.0, float(state.config.get("pacing_random_pct") or 0)))
    if pacing and random_pct:
        span = pacing * random_pct / 100
        bucket = int((time.monotonic() * 1000) % 1000) / 1000
        pacing = max(0.0, pacing - span + bucket * span * 2)
    pacing_remaining = max(0.0, pacing - (time.monotonic() - iteration_started))
    wait_seconds = max(think_time, pacing_remaining)
    if wait_seconds and not state.stop_event.is_set():
        state.stop_event.wait(wait_seconds)


def _run_resource_monitor(state: LoadRunState, workers: list[threading.Thread], interval: float = 1.0) -> None:
    sampler = _ResourceSampler()
    session = requests.Session()

    def collect() -> Dict[str, Any]:
        sample = sampler.sample(state.elapsed_seconds())
        external = _collect_external_monitoring(state.config.get("monitoring") or {}, session)
        external_availability = external.pop("external_availability", {})
        sample.update(external)
        sample["availability"].update(external_availability)
        sample["active_vus"] = state.active_vu_count()
        sample["target_vus"] = state.target_vus()
        return sample

    try:
        while any(worker.is_alive() for worker in workers) and not state.stop_event.is_set():
            state.record_resource_sample(collect())
            state.stop_event.wait(max(0.2, interval))
        if not state.resource_samples:
            state.record_resource_sample(collect())
    finally:
        session.close()


def _step_results(runner: Optional[WebFlowRunner]) -> list[Dict[str, Any]]:
    results = getattr(runner, "step_results", None)
    return copy.deepcopy(results) if isinstance(results, list) else []


def _run_virtual_user(state: LoadRunState, vu_id: int) -> None:
    session = requests.Session()
    playwright = None
    browser = None
    iteration = 0
    active = False
    needs_browser = state.mode in {"ui", "mixed"}
    deadline = state.started_clock + state.total_duration_seconds
    try:
        while not state.stop_event.is_set() and time.monotonic() < deadline:
            elapsed = state.elapsed_seconds()
            enabled = vu_id <= state.target_vus(elapsed)
            if not enabled:
                if active:
                    state.set_vu_active(vu_id, False)
                    active = False
                state.stop_event.wait(0.05)
                continue
            if not active:
                state.set_vu_active(vu_id, True)
                active = True

            iteration_limit = int(state.config.get("iterations_per_user") or 0)
            if iteration_limit and iteration >= iteration_limit:
                break

            if needs_browser and browser is None:
                try:
                    from playwright.sync_api import sync_playwright

                    playwright = sync_playwright().start()
                    launch_kwargs = dict(
                        headless=bool(state.config.get("headless", True)),
                        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                    )
                    ip_proxies = state.config.get("ip_spoofing", {}).get("proxies") or []
                    if ip_proxies:
                        proxy_server = ip_proxies[(vu_id - 1) % len(ip_proxies)]
                        launch_kwargs["proxy"] = {"server": proxy_server}
                        state.log(f"VU {vu_id} 使用代理 {proxy_server}")
                    browser = playwright.chromium.launch(**launch_kwargs)
                    state.log(f"VU {vu_id} 浏览器已启动")
                except Exception as exc:
                    state.record_iteration(
                        vu_id=vu_id,
                        iteration=max(1, iteration + 1),
                        duration_ms=0,
                        success=False,
                        error=str(exc),
                        error_type="BrowserLaunchError",
                    )
                    state.log(f"VU {vu_id} 浏览器启动失败: {exc}")
                    return

            iteration += 1
            _rendezvous_wait(state, vu_id)
            flow = _flow_for_iteration(state, vu_id, iteration)
            scenario_file = str(flow.get("_load_scenario_file") or state.case_file)
            runner: Optional[WebFlowRunner] = None
            started = time.monotonic()
            success = False
            error = ""
            error_type = ""
            try:
                if needs_browser:
                    runner = run_web_flow(browser, flow, http_session=session,
                                          debug_pause_event=state.debug_pause_event,
                                          debug_step_event=state.debug_step_event,
                                          rtds_store=state.rtds_store,
                                          rtds_lock=state.rtds_lock)
                else:
                    runner = WebFlowRunner(None, flow)
                    runner.runtime_context.http_session = session
                    runner.debug_pause_event = state.debug_pause_event
                    runner.debug_step_event = state.debug_step_event
                    runner.rtds_store = state.rtds_store
                    runner.rtds_lock = state.rtds_lock
                    runner.run()
                success = True
            except Exception as exc:
                runner = getattr(exc, "flow_runner", None) or runner
                error = str(exc)
                error_type = type(exc).__name__
            duration_ms = round((time.monotonic() - started) * 1000, 2)
            state.record_iteration(
                vu_id=vu_id,
                iteration=iteration,
                duration_ms=duration_ms,
                success=success,
                error=error,
                error_type=error_type,
                step_results=_step_results(runner),
                scenario_file=scenario_file,
            )

            _post_iteration_wait(state, started)
    finally:
        if active:
            state.set_vu_active(vu_id, False)
        try:
            session.close()
        except Exception:
            pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass


def _adjust_goal_vus(state: LoadRunState, goal: Dict[str, Any]) -> None:
    target_tps = float(goal.get("target_tps") or 0)
    target_p95 = float(goal.get("target_p95_ms") or 0)
    max_vus = int(goal.get("max_vus") or state.config.get("virtual_users") or 1)
    if not target_tps and not target_p95:
        return
    with state.lock:
        current_tps = float(state.summary().get("tps") or 0)
        current_p95 = float(state.summary().get("p95_ms") or 0)
    current_vus = state.target_vus()
    new_vus = current_vus
    if target_tps and current_tps < target_tps and current_vus < max_vus:
        new_vus = min(max_vus, current_vus + max(1, int(current_vus * 0.1)))
    elif target_p95 and current_p95 > target_p95 and current_vus > 1:
        new_vus = max(1, current_vus - max(1, int(current_vus * 0.1)))
    if new_vus != current_vus:
        state.vu_override = new_vus
        state.log(f"目标导向调整 VU: {current_vus} → {new_vus} (TPS {current_tps:.1f}/{target_tps}, P95 {current_p95:.0f}/{target_p95})")


def _run_controller(
    state: LoadRunState,
    vu_ids: Optional[Iterable[int]] = None,
    *,
    persist: bool = True,
) -> None:
    state.status = "running"
    state.started_at = _now()
    state.started_clock = state.started_clock or time.monotonic()
    state.log(
        f"开始压测: mode={state.mode}, VUs={state.config['virtual_users']}, "
        f"总时长={state.total_duration_seconds}s"
    )
    if state.config.get("multi_protocol_detected"):
        protocols = state.config.get("protocols") or {}
        proto_list = [k for k, v in protocols.items() if v and k != "http"]
        if proto_list:
            state.log(f"多协议混合压测: 检测到 {', '.join(proto_list)} 步骤，将与浏览器步骤混合执行")
    if state.config.get("distributed_fallback_reason"):
        state.log(state.config["distributed_fallback_reason"])
    if state.config.get("execution_mode") == "distributed":
        agents = state.config.get("distributed_agents") or []
        state.log(f"分布式压测: {len(agents)} 个负载节点，分片执行 {state.config['virtual_users']} VU")
    if persist:
        _persist_state(state)
    scenario_groups = state.config.get("scenario_groups") or []
    if scenario_groups:
        total_group_vus = sum(max(1, int(g.get("virtual_users") or 1)) for g in scenario_groups)
        state.log(f"多场景分组: {len(scenario_groups)} 组并行，总 {total_group_vus} VU")
        group_vu_ids = []
        vu_offset = 0
        for gi, group in enumerate(scenario_groups):
            group_vus = max(1, int(group.get("virtual_users") or 1))
            group_name = str(group.get("name") or f"group-{gi}")
            group_ids = list(range(vu_offset + 1, vu_offset + group_vus + 1))
            group_vu_ids.extend(group_ids)
            state.log(f"  组 {group_name}: {group_vus} VU (VU {group_ids[0]}-{group_ids[-1]}) → {group.get('case_file','')}")
            vu_offset += group_vus
        selected_vu_ids = group_vu_ids
    else:
        selected_vu_ids = list(vu_ids) if vu_ids is not None else list(range(1, int(state.config["virtual_users"]) + 1))
    workers = [
        threading.Thread(
            target=_run_virtual_user,
            args=(state, vu_id),
            daemon=True,
            name=f"load-{state.run_id[:8]}-vu-{vu_id}",
        )
        for vu_id in selected_vu_ids
    ]
    monitor = threading.Thread(
        target=_run_resource_monitor,
        args=(state, workers),
        daemon=True,
        name=f"load-{state.run_id[:8]}-monitor",
    )
    try:
        for worker in workers:
            worker.start()
        monitor.start()
        last_persist = 0.0
        while any(worker.is_alive() for worker in workers):
            for worker in workers:
                worker.join(timeout=0.02)
            now = time.monotonic()
            if now - last_persist >= 1.0:
                if persist:
                    _persist_state(state)
                last_persist = now
                goal = state.config.get("goal") or {}
                if goal and state.status == "running":
                    _adjust_goal_vus(state, goal)
        for worker in workers:
            worker.join()
        monitor.join(timeout=2)
        state.finished_clock = time.monotonic()
        state.finished_at = _now()
        if state.status == "stopping" or state.stop_event.is_set():
            state.status = "stopped"
            state.log("压测已停止")
        elif state.completed == 0:
            state.status = "failed"
            state.error_message = "未产生任何压测样本，请检查持续时间、浏览器或场景配置"
            state.log(state.error_message)
        else:
            state.status = "completed"
            state.log(
                f"压测完成: 场景={state.completed}, 失败={state.failed}, "
                f"TPS={state.summary()['tps']}"
            )
    except Exception as exc:
        state.stop_event.set()
        state.finished_clock = time.monotonic()
        state.finished_at = _now()
        state.status = "failed"
        state.error_message = f"{type(exc).__name__}: {exc}"
        state.log(f"压测控制器失败: {state.error_message}")
    finally:
        if persist:
            _persist_state(state)


def _merge_distributed_event(state: LoadRunState, event: Dict[str, Any]) -> None:
    kind = str(event.get("kind") or "")
    agent_id = str(event.get("agent_id") or "")
    if kind == "monitor":
        sample = event.get("sample") if isinstance(event.get("sample"), dict) else None
        if sample is not None:
            item = copy.deepcopy(sample)
            item["agent_id"] = agent_id
            state.record_resource_sample(item)
        return
    if kind == "samples":
        samples = event.get("samples") if isinstance(event.get("samples"), list) else []
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            sample_agent = str(sample.get("agent_id") or agent_id)
            with state.lock:
                state.agent_active_vus[sample_agent] = max(0, int(sample.get("active_vus") or 0))
                state.peak_active_vus = max(state.peak_active_vus, state.active_vu_count())
            state.record_iteration(
                vu_id=max(1, int(sample.get("vu_id") or 1)),
                iteration=max(1, int(sample.get("iteration") or 1)),
                duration_ms=max(0.0, float(sample.get("duration_ms") or 0)),
                success=bool(sample.get("success")),
                error=str(sample.get("error") or ""),
                error_type=str(sample.get("error_type") or ""),
                step_results=sample.get("step_results") if isinstance(sample.get("step_results"), list) else [],
                elapsed_seconds=max(0.0, float(sample.get("elapsed_seconds") or 0)),
                scenario_file=str(sample.get("scenario_file") or ""),
            )
        return
    if kind == "shard_finished" and agent_id:
        with state.lock:
            state.finished_agents.add(agent_id)
            state.agent_active_vus[agent_id] = 0
            status = str(event.get("status") or "failed")
            if status == "failed":
                state.agent_errors[agent_id] = str(event.get("error_message") or "节点执行失败")
        state.log(
            f"负载节点 {agent_id} 已结束: status={event.get('status')}, "
            f"completed={int(event.get('completed') or 0)}"
        )


def _run_distributed_controller(state: LoadRunState) -> None:
    shards = state.config.get("distributed_agents") if isinstance(state.config.get("distributed_agents"), list) else []
    state.agent_ids = [str(item.get("name") or "") for item in shards if str(item.get("name") or "")]
    state.agent_active_vus = {agent_id: 0 for agent_id in state.agent_ids}
    state.status = "running"
    state.started_at = _now()
    start_delay = LOAD_AGENT_START_DELAY_SECONDS
    start_at = time.time() + start_delay
    state.started_clock = time.monotonic() + start_delay
    state.log(
        f"开始分布式协议压测: VUs={state.config['virtual_users']}, "
        f"节点={len(state.agent_ids)}, 总时长={state.total_duration_seconds}s"
    )
    stop_sent = False
    try:
        if not shards or not state.agent_ids:
            raise DistributedLoadError("没有已预留的分布式负载节点")
        redis_queue.clear_load_events(state.run_id)
        payload_ttl = state.total_duration_seconds + LOAD_AGENT_FINISH_GRACE_SECONDS + 600
        payload_key = redis_queue.store_load_payload(state.run_id, {
            "project_id": state.project_id,
            "case_file": state.case_file,
            "scenario_name": state.scenario_name,
            "config": state.config,
            "flow": state.flow,
            "flows": state.flows,
        }, ttl=payload_ttl)
        for shard in shards:
            redis_queue.push_load_command(str(shard["name"]), {
                "kind": "start",
                "run_id": state.run_id,
                "payload_key": payload_key,
                "start_at": start_at,
                "vu_ids": list(range(int(shard["vu_start"]), int(shard["vu_end"]) + 1)),
            })
        _persist_state(state)

        deadline = state.started_clock + state.total_duration_seconds + LOAD_AGENT_FINISH_GRACE_SECONDS
        last_persist = 0.0
        while len(state.finished_agents) < len(state.agent_ids):
            if state.stop_event.is_set() and not stop_sent:
                state.status = "stopping"
                redis_queue.request_load_stop(state.run_id, ttl=payload_ttl)
                stop_sent = True
            event = redis_queue.pop_load_event(state.run_id, timeout=1)
            if event:
                _merge_distributed_event(state, event)
            now = time.monotonic()
            if now >= deadline:
                missing = sorted(set(state.agent_ids) - state.finished_agents)
                raise DistributedLoadError(f"负载节点结束超时: {', '.join(missing)}")
            if now - last_persist >= 1.0:
                _persist_state(state)
                last_persist = now

        state.finished_clock = time.monotonic()
        state.finished_at = _now()
        if state.status == "stopping" or state.stop_event.is_set():
            state.status = "stopped"
            state.log("分布式压测已停止")
        elif state.agent_errors:
            state.status = "failed"
            state.error_message = "; ".join(
                f"{agent}: {message}" for agent, message in sorted(state.agent_errors.items())
            )[:2000]
            state.log(f"分布式压测失败: {state.error_message}")
        elif state.completed == 0:
            state.status = "failed"
            state.error_message = "分布式节点未回传任何压测样本"
            state.log(state.error_message)
        else:
            state.status = "completed"
            state.log(
                f"分布式压测完成: 场景={state.completed}, 失败={state.failed}, "
                f"TPS={state.summary()['tps']}"
            )
    except Exception as exc:
        state.stop_event.set()
        if state.agent_ids and not stop_sent:
            try:
                redis_queue.request_load_stop(state.run_id)
            except Exception:
                pass
        state.finished_clock = time.monotonic()
        state.finished_at = _now()
        state.status = "failed"
        state.error_message = f"{type(exc).__name__}: {exc}"
        state.log(f"分布式压测控制器失败: {state.error_message}")
    finally:
        _release_distributed_run(state.config, state.run_id)
        try:
            redis_queue.clear_load_events(state.run_id)
        except Exception:
            pass
        _persist_state(state)


def _normalize_config(
    body: LoadRunIn,
    classification: Dict[str, Any],
    project: Dict[str, Any],
    flow: Dict[str, Any],
) -> Dict[str, Any]:
    profile = _load_profile(flow)
    mode = str(body.mode or "auto").strip().lower()
    aliases = {"api": "protocol", "http": "protocol", "browser": "ui"}
    mode = aliases.get(mode, mode)
    if mode == "auto":
        mode = classification["recommended_mode"]
    if mode not in {"protocol", "ui", "mixed"}:
        raise ValueError("mode 仅支持 auto/protocol/ui/mixed")
    if mode == "protocol" and classification["has_browser"]:
        raise ValueError("该 YAML 包含页面步骤，不能使用纯接口模式；请选择 auto、ui 或 mixed")
    if mode in {"ui", "mixed"} and not classification["has_browser"]:
        mode = "protocol"

    team_id = str(body.team_id or "").strip()
    if not team_id:
        raise ValueError("缺少当前团队 ID")

    distributed_fallback_reason = ""

    virtual_users = int(body.virtual_users or 0)
    requested_execution_mode = str(getattr(body, "execution_mode", "auto") or "auto").strip().lower()
    if requested_execution_mode not in {"auto", "local", "distributed"}:
        raise ValueError("execution_mode 仅支持 auto/local/distributed")
    requested_agent_count = max(0, int(getattr(body, "agent_count", 0) or 0))
    distributed_capacity = 0
    if mode in {"ui", "mixed"}:
        if requested_execution_mode == "distributed":
            execution_mode = "local"
            distributed_fallback_reason = "浏览器场景不支持分布式，已自动降级为本机执行"
        else:
            execution_mode = "local"
        max_vus = MAX_UI_VUS
    else:
        execution_mode = requested_execution_mode
        if execution_mode == "auto":
            execution_mode = "distributed" if virtual_users > MAX_API_VUS else "local"
        if execution_mode == "distributed":
            agents = _load_agent_inventory(available_only=True)
            if requested_agent_count:
                agents = agents[:requested_agent_count]
            distributed_capacity = _distributed_capacity(agents)
            max_vus = distributed_capacity
            if max_vus <= 0:
                raise ValueError("没有可用的分布式协议负载节点")
        else:
            max_vus = MAX_API_VUS
    if virtual_users < 1 or virtual_users > max_vus:
        label = "分布式协议" if execution_mode == "distributed" else mode
        raise ValueError(f"{label} 模式虚拟用户数必须在 1-{max_vus} 之间")
    if not body.headless and virtual_users > 1:
        raise ValueError("有头浏览器模式仅允许 1 个虚拟用户")

    ramp_up = max(0, int(body.ramp_up_seconds or 0))
    duration = max(1, int(body.duration_seconds or 0))
    ramp_down = max(0, int(body.ramp_down_seconds or 0))
    if ramp_up + duration + ramp_down > MAX_DURATION_SECONDS:
        raise ValueError(f"压测总时长不能超过 {MAX_DURATION_SECONDS} 秒")
    think_time = max(0, min(600000, int(body.think_time_ms or profile.get("think_time_ms") or 0)))
    pacing_seconds = max(0.0, min(3600.0, float(body.pacing_seconds or profile.get("pacing_seconds") or 0)))
    pacing_random_pct = max(0.0, min(100.0, float(body.pacing_random_pct or profile.get("pacing_random_pct") or 0)))
    rendezvous_enabled = bool(body.rendezvous_enabled or profile.get("rendezvous_enabled") or profile.get("rendezvous"))
    rendezvous_timeout = max(1, min(3600, int(body.rendezvous_timeout_seconds or profile.get("rendezvous_timeout_seconds") or 30)))
    iterations = max(0, min(1000000, int(body.iterations_per_user or 0)))
    if len(body.data_rows) > 10000:
        raise ValueError("参数化数据最多支持 10000 行")

    data_rows = copy.deepcopy(body.data_rows)
    data_asset_alias = str(getattr(body, "data_asset_alias", "") or "").strip()
    if not data_rows and data_asset_alias:
        data_rows = _fetch_data_asset_rows(
            data_asset_alias, virtual_users, "PHONE",
            api_url=str(getattr(body, "data_center_api_url", "") or ""),
        )
    if not data_rows:
        data_rows = _fallback_data_rows(virtual_users)

    base_url = str(body.base_url or "").strip()
    if base_url:
        base_url = validate_outbound_url(base_url)
    monitoring = _normalize_monitoring_config(getattr(body, "monitoring", None) or profile.get("monitoring") or {})

    return {
        "mode": mode,
        "execution_mode": execution_mode,
        "requested_execution_mode": requested_execution_mode,
        "requested_agent_count": requested_agent_count,
        "agent_count": 0,
        "distributed_capacity_vus": distributed_capacity,
        "team_id": team_id,
        "virtual_users": virtual_users,
        "ramp_up_seconds": ramp_up,
        "duration_seconds": duration,
        "ramp_down_seconds": ramp_down,
        "think_time_ms": think_time,
        "pacing_seconds": pacing_seconds,
        "pacing_random_pct": pacing_random_pct,
        "rendezvous_enabled": rendezvous_enabled,
        "rendezvous_timeout_seconds": rendezvous_timeout,
        "iterations_per_user": iterations,
        "headless": bool(body.headless),
        "env": str(body.env or "test"),
        "base_url": base_url,
        "runtime_variables": copy.deepcopy(body.runtime_variables),
        "data_rows": data_rows,
        "data_assignment_mode": str(getattr(body, "data_assignment_mode", "unique") or "unique").lower(),
        "goal": copy.deepcopy(getattr(body, "goal", {}) or {}),
        "ip_spoofing": copy.deepcopy(getattr(body, "ip_spoofing", {}) or {}),
        "breakpoints": list(getattr(body, "breakpoints", []) or []),
        "protocols": copy.deepcopy(getattr(body, "protocols", {}) or {}),
        "scenario_groups": list(getattr(body, "scenario_groups", []) or []),
        "sla": copy.deepcopy(body.sla),
        "monitoring": monitoring,
        "auto_randomize": bool(getattr(body, "auto_randomize", False)),
        "transaction_map": _transaction_map(flow),
        "load_profile": {
            "transactions": _normalize_transaction_defs(flow),
            "rendezvous": copy.deepcopy(profile.get("rendezvous") or []),
        },
        "strict_full_chain": bool(profile.get("strict_full_chain", True)),
        "connection_reuse": True,
        "total_duration_seconds": ramp_up + duration + ramp_down,
        "distributed_fallback_reason": distributed_fallback_reason,
        "multi_protocol_detected": bool(classification.get("api_steps", 0) or classification.get("database_steps", 0)),
    }


def recover_incomplete_runs() -> None:
    db.execute(
        "UPDATE load_test_runs SET status='interrupted',finished_at=?,"
        "error_message=CASE WHEN error_message='' THEN '服务重启导致压测中断' ELSE error_message END "
        "WHERE status IN ('pending','running','stopping')",
        (_now(),),
    )


def shutdown() -> None:
    with _RUNS_LOCK:
        states = list(_RUNS.values())
    for state in states:
        if state.status in RUNNING_STATUSES:
            state.status = "stopping"
            state.stop_event.set()
            if state.config.get("execution_mode") == "distributed":
                try:
                    redis_queue.request_load_stop(state.run_id)
                except Exception:
                    pass


def _enrich_stored_summary(summary: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Backfill LoadRunner-style fields for reports persisted before they existed."""
    if not isinstance(summary, dict):
        return summary
    result = copy.deepcopy(summary)
    config = config if isinstance(config, dict) else {}
    elapsed = max(0.001, float(result.get("elapsed_seconds") or 0))
    completed = int(result.get("completed") or 0)
    planned_duration = max(0.0, float(config.get("total_duration_seconds") or (
        float(config.get("ramp_up_seconds") or 0)
        + float(config.get("duration_seconds") or 0)
        + float(config.get("ramp_down_seconds") or 0)
    )))
    result.setdefault("planned_duration_seconds", round(planned_duration, 2))
    result.setdefault("load_window_seconds", round(min(elapsed, planned_duration), 2))
    result.setdefault("drain_duration_seconds", round(max(0.0, elapsed - planned_duration), 2))
    if not isinstance(result.get("resource_monitoring"), dict):
        result["resource_monitoring"] = {
            "samples": [],
            "sample_count": 0,
            "sample_count_total": 0,
            "availability": {"cpu": False, "memory": False, "network": False, "jvm": False, "database": False},
            "metrics": {},
            "estimated": True,
        }

    if not isinstance(result.get("chain_integrity"), dict):
        step_metrics = result.get("transactions") if isinstance(result.get("transactions"), list) else []
        skipped_checks = sum(max(0, int(item.get("skipped") or 0)) for item in step_metrics if isinstance(item, dict))
        failed_checks = sum(max(0, int(item.get("failed") or 0)) for item in step_metrics if isinstance(item, dict))
        required_checks = sum(max(0, int(item.get("count") or 0)) for item in step_metrics if isinstance(item, dict))
        passed_checks = sum(max(0, int(item.get("passed") or 0)) for item in step_metrics if isinstance(item, dict))
        skipped_iterations = max(
            (max(0, int(item.get("skipped") or 0)) for item in step_metrics if isinstance(item, dict)),
            default=0,
        )
        original_failed = max(0, int(result.get("failed") or 0))
        incomplete = min(completed, max(original_failed, skipped_iterations))
        successful = max(0, completed - incomplete)
        evaluated = completed if step_metrics else 0
        result["chain_integrity"] = {
            "status": "UNKNOWN" if not evaluated else "COMPLETE" if not incomplete else "INCOMPLETE",
            "evaluated_chains": evaluated,
            "successful_chains": successful,
            "incomplete_chains": incomplete,
            "success_rate": round(successful * 100 / max(1, evaluated), 2),
            "required_step_checks": required_checks,
            "passed_required_steps": passed_checks,
            "failed_required_steps": failed_checks,
            "skipped_required_steps": skipped_checks,
            "step_completion_rate": round(passed_checks * 100 / max(1, required_checks), 2),
            "estimated": True,
        }
        if skipped_iterations:
            result["failed"] = incomplete
            result["passed"] = successful
            result["error_rate"] = round(incomplete * 100 / max(1, completed), 2)
            scenarios = result.get("scenarios") if isinstance(result.get("scenarios"), list) else []
            if len(scenarios) == 1:
                scenarios[0]["passed"] = successful
                scenarios[0]["failed"] = incomplete
                scenarios[0]["pass_rate"] = round(successful * 100 / max(1, completed), 2)
            if not result.get("business_transactions"):
                result["business_transactions"] = [{
                    "name": "主业务事务",
                    "count": completed,
                    "passed": successful,
                    "failed": incomplete,
                    "skipped": skipped_iterations,
                    "avg_ms": result.get("avg_ms", 0),
                    "p95_ms": result.get("p95_ms", 0),
                    "p99_ms": result.get("p99_ms", 0),
                    "tps": result.get("tps", 0),
                    "pass_tps": round(successful / elapsed, 2),
                    "pass_rate": round(successful * 100 / max(1, completed), 2),
                    "auto_generated": True,
                }]
    missing_throughput = {
        key for key in (
            "tps", "peak_tps", "stable_tps", "steady_window_seconds",
            "steady_completed", "active_burst_seconds",
        )
        if result.get(key) is None
    }
    if missing_throughput:
        timeline = result.get("timeline") if isinstance(result.get("timeline"), list) else []
        throughput = _throughput_metrics(timeline, config, elapsed, completed)
        for key in missing_throughput:
            result[key] = throughput.get(key)
    if result.get("active_burst_seconds") is None and result.get("observed_seconds") is not None:
        result["active_burst_seconds"] = result.get("observed_seconds")
    if not isinstance(result.get("data_quality"), dict):
        timeline = result.get("timeline") if isinstance(result.get("timeline"), list) else []
        result["data_quality"] = _data_quality(
            completed=completed,
            failed=int(result.get("failed") or 0),
            elapsed=elapsed,
            timeline=timeline,
            durations=[],
        )
    if "metric_semantics" not in result:
        result["metric_semantics"] = dict(METRIC_SEMANTICS)
    elif isinstance(result.get("metric_semantics"), dict):
        result["metric_semantics"].update(METRIC_SEMANTICS)
    http = result.get("http_waterfall") if isinstance(result.get("http_waterfall"), dict) else None
    if isinstance(http, dict):
        http.setdefault("distinct_url_count", len(http.get("page_components") or []))
        http.setdefault("page_components", [])
    if not result.get("request_targets"):
        inferred_targets = Counter()
        for item in result.get("transactions") if isinstance(result.get("transactions"), list) else []:
            if not isinstance(item, dict) or str(item.get("action") or "").lower() not in {"goto", "open", "navigate"}:
                continue
            match = re.search(r"https?://[^\s]+", str(item.get("name") or ""))
            target = _request_target(match.group(0) if match else "")
            if target:
                inferred_targets[target] += max(1, int(item.get("count") or 0))
        if inferred_targets:
            result["request_targets"] = [
                {"target": target, "count": count, "source": "录制导航步骤"}
                for target, count in inferred_targets.most_common()
            ]
    result["target_integrity"] = _target_integrity(
        config,
        result.get("request_targets"),
        result.get("recent_errors"),
    )
    return result


def _row_public(row: Any, compact: bool = False) -> Dict[str, Any]:
    item = db.to_dict(row)
    config = _loads(item.get("config_json"), {})
    summary = _loads(item.get("summary_json"), {})
    summary = _enrich_stored_summary(summary, config)
    if compact:
        config = {
            key: config.get(key)
            for key in (
                "mode", "virtual_users", "ramp_up_seconds", "duration_seconds",
                "ramp_down_seconds", "think_time_ms", "iterations_per_user",
                "pacing_seconds", "pacing_random_pct", "rendezvous_enabled",
                "rendezvous_timeout_seconds", "headless", "env", "base_url",
                "connection_reuse", "team_id", "planned_vum", "remaining_vum",
                "vum_quota_enforced",
                "execution_mode", "agent_count", "distributed_capacity_vus",
            )
        }
        raw_monitoring_config = _loads(item.get("config_json"), {})
        raw_monitoring_config = raw_monitoring_config if isinstance(raw_monitoring_config, dict) else {}
        config["monitoring_enabled"] = bool((raw_monitoring_config.get("monitoring") or {}).get("enabled"))
        summary = _compact_summary(summary)
    return {
        "id": item.get("id"),
        "project_id": item.get("project_id"),
        "case_file": item.get("case_file"),
        "scenario_name": item.get("scenario_name"),
        "mode": item.get("mode"),
        "status": item.get("status"),
        "config": config,
        "summary": summary,
        "error_message": item.get("error_message") or "",
        "created_at": item.get("created_at") or "",
        "started_at": item.get("started_at") or "",
        "finished_at": item.get("finished_at") or "",
        "logs": [],
    }


@router.get("/capabilities")
def capabilities(request: Request, team_id: str):
    credential = auth_service.extract_credential(request)
    try:
        quota = _team_vum_quota(
            credential.token if credential else "",
            team_id,
        )
    except runnergo_test_objects.VumQuotaError as exc:
        raise HTTPException(400, str(exc)) from exc
    agents = _load_agent_inventory()
    available_agents = [agent for agent in agents if bool(agent.get("available", True))]
    distributed_capacity = _distributed_capacity(available_agents)
    return {
        "modes": ["auto", "protocol", "ui", "mixed"],
        "max_api_vus": MAX_API_VUS,
        "max_ui_vus": MAX_UI_VUS,
        "max_distributed_vus": MAX_DISTRIBUTED_VUS,
        "distributed_capacity_vus": distributed_capacity,
        "load_agents": [{
            "name": str(agent.get("name") or ""),
            "max_vus": int(agent.get("capacity_vus") or 0),
            "available": bool(agent.get("available", True)),
            "version": str((agent.get("capabilities") or {}).get("version") or ""),
        } for agent in agents],
        "available_load_agents": len(available_agents),
        "max_duration_seconds": MAX_DURATION_SECONDS,
        "vum": quota,
        "load_pattern": "ramp-up -> steady -> ramp-down",
        "source": "flow_version: 2 YAML",
        "connection_reuse": True,
        "enterprise_controls": {
            "pacing": True,
            "rendezvous": True,
            "business_transactions": True,
            "distributed_agents": True,
            "resource_monitoring": True,
            "analysis_export": ["json", "csv", "markdown", "html"],
        },
        "monitoring_fields": sorted(EXTERNAL_MONITOR_FIELDS.keys()),
        "monitoring_categories": {
            "jvm": [k for k, v in EXTERNAL_MONITOR_FIELDS.items() if v == "jvm"],
            "database": [k for k, v in EXTERNAL_MONITOR_FIELDS.items() if v == "database"],
        },
        "monitoring_query_examples": MONITORING_QUERY_EXAMPLES,
        "protocols": PROTOCOL_CAPABILITIES,
        "official_model": "VuGen/YAML script -> Controller schedule -> Load Generator/VU -> Analysis report",
    }


@router.get("/scenarios")
def scenarios(project_id: str):
    if not project_service.get_project(project_id):
        raise HTTPException(404, "项目不存在")
    result = []
    for case in case_service.list_cases(project_id, "ui"):
        filename = case["name"]
        try:
            flow = yaml.safe_load(case_service.get_case(project_id, "ui", filename)) or {}
            if not isinstance(flow, dict) or int(flow.get("flow_version") or 0) != 2:
                continue
            if not isinstance(flow.get("steps"), list):
                continue
            classification = classify_flow(flow)
            result.append({
                "filename": filename,
                "name": str(flow.get("name") or filename),
                "description": str(flow.get("description") or ""),
                "base_url": str(flow.get("base_url") or ""),
                **classification,
            })
        except (OSError, yaml.YAMLError, ValueError):
            continue
    return {"project_id": project_id, "scenarios": result}


def _load_run_case_files(body: LoadRunIn) -> list[str]:
    requested = body.case_files or ([body.case_file] if body.case_file else [])
    filenames = []
    for value in requested:
        filename = os.path.basename(str(value or "").strip())
        if not filename or filename != str(value or "").strip() or not filename.endswith((".yaml", ".yml")):
            raise ValueError("YAML 场景文件名无效")
        if filename not in filenames:
            filenames.append(filename)
    if not filenames:
        raise ValueError("请至少选择一个业务场景")
    if len(filenames) > 20:
        raise ValueError("单次最多选择 20 个业务场景")
    return filenames


def _aggregate_classification(classifications: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    items = list(classifications)
    browser_steps = sum(int(item.get("browser_steps") or 0) for item in items)
    api_steps = sum(int(item.get("api_steps") or 0) for item in items)
    database_steps = sum(int(item.get("database_steps") or 0) for item in items)
    if browser_steps and (api_steps or database_steps):
        mode = "mixed"
    elif browser_steps:
        mode = "ui"
    else:
        mode = "protocol"
    return {
        "recommended_mode": mode,
        "step_count": sum(int(item.get("step_count") or 0) for item in items),
        "browser_steps": browser_steps,
        "api_steps": api_steps,
        "database_steps": database_steps,
        "has_browser": browser_steps > 0,
    }


def _scenario_transaction_map(flow: Dict[str, Any], scenario_name: str) -> Dict[str, str]:
    return {
        key: f"{scenario_name} / {value}"
        for key, value in _transaction_map(flow).items()
    }


@router.post("/runs")
def create_run(body: LoadRunIn, request: Request):
    project = project_service.get_project(body.project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    try:
        filenames = _load_run_case_files(body)
        loaded_flows: list[tuple[str, Dict[str, Any], Dict[str, Any]]] = []
        for filename in filenames:
            flow = yaml.safe_load(case_service.get_case(body.project_id, "ui", filename)) or {}
            if not isinstance(flow, dict) or int(flow.get("flow_version") or 0) != 2:
                raise ValueError("仅支持 flow_version: 2 的 UI 场景 YAML")
            if not isinstance(flow.get("steps"), list) or not flow["steps"]:
                raise ValueError(f"场景 {filename} 没有可执行步骤")
            loaded_flows.append((filename, flow, classify_flow(flow)))
        filename, flow, _ = loaded_flows[0]
        config = _normalize_config(
            body,
            _aggregate_classification(item[2] for item in loaded_flows),
            project,
            flow,
        )
        scenarios = []
        transaction_maps = {}
        for item_filename, item_flow, classification in loaded_flows:
            name = str(item_flow.get("name") or item_filename)
            try:
                weight = int((body.scenario_weights or {}).get(item_filename, 1) or 1)
            except (TypeError, ValueError):
                raise ValueError(f"场景 {name} 的权重必须是整数")
            if weight < 1 or weight > 100:
                raise ValueError(f"场景 {name} 的权重必须在 1-100 之间")
            scenarios.append({
                "filename": item_filename,
                "name": name,
                "weight": weight,
                **classification,
            })
            transaction_maps[item_filename] = _scenario_transaction_map(item_flow, name)
        config["scenarios"] = scenarios
        config["transaction_maps"] = transaction_maps
        config["load_profile"]["transactions"] = [
            {
                **transaction,
                "name": f"{scenario['name']} / {transaction['name']}",
            }
            for scenario, (_, item_flow, _) in zip(scenarios, loaded_flows)
            for transaction in _normalize_transaction_defs(item_flow)
        ]
        references = runnergo_test_objects.collect_references([
            item_flow for _, item_flow, _ in loaded_flows
        ])
        credential = auth_service.extract_credential(request)
        bundle = runnergo_test_objects.resolve_references(
            [item_flow for _, item_flow, _ in loaded_flows], credential.token if credential else "",
        ) if references else {}
        runtime_flows = {
            item_filename: _materialize_test_objects(item_flow, bundle)
            for item_filename, item_flow, _ in loaded_flows
        }
        runtime_flow = runtime_flows[filename]
    except FileNotFoundError as exc:
        raise HTTPException(404, "YAML 用例不存在") from exc
    except (ValueError, yaml.YAMLError, runnergo_test_objects.TestObjectError) as exc:
        raise HTTPException(400, str(exc)) from exc

    run_id = uuid.uuid4().hex
    if config.get("execution_mode") == "distributed":
        try:
            _prepare_distributed_run(config, run_id)
        except DistributedLoadError as exc:
            raise HTTPException(409, str(exc)) from exc
    planned_vum = _planned_vum(config)
    try:
        vum_result = _consume_team_vum(
            credential.token if credential else "",
            config["team_id"],
            run_id,
            planned_vum,
        )
    except runnergo_test_objects.VumQuotaError as exc:
        _release_distributed_run(config, run_id)
        raise HTTPException(400, str(exc)) from exc
    config["planned_vum"] = planned_vum
    config["remaining_vum"] = vum_result["available_vum_num"]
    config["vum_quota_enforced"] = bool(vum_result.get("enforced", True))
    state = LoadRunState(
        run_id=run_id,
        project_id=body.project_id,
        case_file=filename,
        scenario_name=(
            str(flow.get("name") or filename)
            if len(scenarios) == 1
            else f"业务链路多场景（{len(scenarios)} 个）"
        ),
        mode=config["mode"],
        flow=runtime_flow,
        config=config,
        flows=runtime_flows,
    )
    try:
        _persist_new(state)
    except Exception:
        _release_distributed_run(config, run_id)
        raise
    with _RUNS_LOCK:
        _RUNS[run_id] = state
    state.controller = threading.Thread(
        target=_run_distributed_controller if config.get("execution_mode") == "distributed" else _run_controller,
        args=(state,),
        daemon=True,
        name=f"load-controller-{run_id[:8]}",
    )
    state.controller.start()
    return state.public()


@router.get("/runs")
def list_runs(project_id: str = "", limit: int = 50):
    limit = max(1, min(int(limit or 50), 200))
    if project_id:
        rows = db.execute(
            "SELECT * FROM load_test_runs WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
            (project_id, limit), fetch=True,
        )
    else:
        rows = db.execute(
            "SELECT * FROM load_test_runs ORDER BY created_at DESC LIMIT ?",
            (limit,), fetch=True,
        )
    result = []
    with _RUNS_LOCK:
        states = dict(_RUNS)
    for row in rows:
        state = states.get(row["id"])
        result.append(state.public(compact=True) if state else _row_public(row, compact=True))
    return result


@router.delete("/runs/batch")
def batch_delete_runs(body: BatchDeleteLoadRunsIn):
    project_id = str(body.project_id or "").strip()
    run_ids = list(dict.fromkeys(
        str(run_id or "").strip() for run_id in (body.run_ids or [])
        if str(run_id or "").strip()
    ))
    if not project_id:
        raise HTTPException(400, "项目不能为空")
    if not run_ids:
        raise HTTPException(400, "请至少选择一条压测报告")
    if len(run_ids) > 200:
        raise HTTPException(400, "单次最多删除 200 条压测报告")

    placeholders = ",".join("?" for _ in run_ids)
    rows = db.execute(
        f"SELECT id, status FROM load_test_runs "
        f"WHERE project_id=? AND id IN ({placeholders})",
        (project_id, *run_ids),
        fetch=True,
    )
    found = {str(row["id"]): str(row["status"] or "") for row in rows}

    with _RUNS_LOCK:
        active_states = {
            run_id: _RUNS.get(run_id)
            for run_id in found
        }
        running_ids = [
            run_id for run_id, state in active_states.items()
            if state is not None and state.status in RUNNING_STATUSES
        ]
        running_ids.extend(
            run_id for run_id, status in found.items()
            if status in RUNNING_STATUSES and run_id not in running_ids
        )
        if running_ids:
            raise HTTPException(409, "运行中的压测报告不能删除，请先停止后再试")

        for run_id, state in active_states.items():
            if state is not None:
                _RUNS.pop(run_id, None)

    if found:
        db.execute(
            f"DELETE FROM load_test_runs "
            f"WHERE project_id=? AND id IN ({placeholders})",
            (project_id, *found.keys()),
        )

    missing_ids = [run_id for run_id in run_ids if run_id not in found]
    return {
        "deleted": len(found),
        "missing_ids": missing_ids,
        "message": f"已删除 {len(found)} 条压测报告",
    }


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    with _RUNS_LOCK:
        state = _RUNS.get(run_id)
    if state:
        return state.public()
    rows = db.execute("SELECT * FROM load_test_runs WHERE id=?", (run_id,), fetch=True)
    if not rows:
        raise HTTPException(404, "压测记录不存在")
    return _row_public(rows[0])


def _public_run_for_export(run_id: str) -> Dict[str, Any]:
    with _RUNS_LOCK:
        state = _RUNS.get(run_id)
    if state:
        return state.public()
    rows = db.execute("SELECT * FROM load_test_runs WHERE id=?", (run_id,), fetch=True)
    if not rows:
        raise HTTPException(404, "压测记录不存在")
    return _row_public(rows[0])


def _export_csv(run: Dict[str, Any]) -> str:
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    rows: list[list[Any]] = [["scope", "name", "count", "passed", "failed", "skipped", "pass_rate", "tps", "avg_ms", "p95_ms", "p99_ms", "second", "active_vus"]]
    rows.append(["summary", "全链路", summary.get("completed", 0), summary.get("passed", 0), summary.get("failed", 0), "", "", summary.get("tps", 0), summary.get("avg_ms", 0), summary.get("p95_ms", 0), summary.get("p99_ms", 0), "", summary.get("peak_active_vus", 0)])
    for scope in ("scenarios", "business_transactions", "transactions"):
        for item in summary.get(scope) or []:
            if not isinstance(item, dict):
                continue
            rows.append([
                scope, item.get("name") or item.get("key") or "", item.get("count", 0),
                item.get("passed", 0), item.get("failed", 0), item.get("skipped", 0),
                item.get("pass_rate", ""), item.get("tps", 0), item.get("avg_ms", 0),
                item.get("p95_ms", 0), item.get("p99_ms", 0), "", "",
            ])
    for item in summary.get("timeline") or []:
        if isinstance(item, dict):
            rows.append(["timeline", "", item.get("completed", 0), "", item.get("failed", 0), "", "", item.get("tps", 0), item.get("avg_ms", 0), item.get("p95_ms", 0), "", item.get("second", 0), item.get("active_vus", 0)])
    resource = summary.get("resource_monitoring") if isinstance(summary.get("resource_monitoring"), dict) else {}
    for item in resource.get("samples") or []:
        if isinstance(item, dict):
            rows.append(["resource", "", "", "", "", "", "", "", item.get("process_rss_mb", ""), item.get("cpu_percent", ""), item.get("memory_percent", ""), item.get("elapsed_seconds", ""), ""])
    http = summary.get("http_waterfall") if isinstance(summary.get("http_waterfall"), dict) else {}
    for item in http.get("page_components") or []:
        if not isinstance(item, dict):
            continue
        total = item.get("total") if isinstance(item.get("total"), dict) else {}
        rows.append([
            "page_component", item.get("url", ""), item.get("count", 0),
            "", "", "", "", "", total.get("avg", 0), total.get("p95", 0), total.get("p99", 0), "", "",
        ])
    output = io.StringIO(newline="")
    csv.writer(output).writerows(rows)
    return output.getvalue()


def _export_markdown(run: Dict[str, Any]) -> str:
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    lines = [
        f"# LoadRunner 压测报告：{run.get('scenario_name') or run.get('case_file') or run.get('id')}",
        "", f"- 状态：{run.get('status') or '-'}", f"- 模式：{run.get('mode') or '-'}",
        f"- VU：{(run.get('config') or {}).get('virtual_users', 0)}",
        f"- 完成/通过/失败：{summary.get('completed', 0)} / {summary.get('passed', 0)} / {summary.get('failed', 0)}",
        f"- TPS：{summary.get('tps', 0)}（稳态 {summary.get('stable_tps', 0)}，峰值 {summary.get('peak_tps', 0)}）",
        f"- P95/P99：{summary.get('p95_ms', 0)} ms / {summary.get('p99_ms', 0)} ms",
        f"- 链路成功率：{(summary.get('chain_integrity') or {}).get('success_rate', 0)}%",
        "", "## 业务事务", "", "| 事务 | 次数 | 通过率 | TPS | 平均(ms) | P95(ms) | P99(ms) |", "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summary.get("business_transactions") or []:
        if isinstance(item, dict):
            lines.append(f"| {item.get('name','')} | {item.get('count',0)} | {item.get('pass_rate',0)}% | {item.get('tps',0)} | {item.get('avg_ms',0)} | {item.get('p95_ms',0)} | {item.get('p99_ms',0)} |")
    http = summary.get("http_waterfall") if isinstance(summary.get("http_waterfall"), dict) else {}
    page_components = http.get("page_components") or []
    if page_components:
        lines.extend([
            "", "## 页面组件分解（按 URL，等同 LoadRunner Page Component Breakdown）", "",
            "| URL | 方法 | 类型 | 次数 | 占比 | 平均(ms) | P95(ms) | P99(ms) | TTFB P95(ms) |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|",
        ])
        for item in page_components:
            if not isinstance(item, dict):
                continue
            total = item.get("total") if isinstance(item.get("total"), dict) else {}
            ttfb = item.get("ttfb") if isinstance(item.get("ttfb"), dict) else {}
            lines.append(f"| `{item.get('url','')}` | {item.get('method','')} | {item.get('resource_type','')} | {item.get('count',0)} | {item.get('pct',0)}% | {total.get('avg',0)} | {total.get('p95',0)} | {total.get('p99',0)} | {ttfb.get('p95',0)} |")
    resource = summary.get("resource_monitoring") if isinstance(summary.get("resource_monitoring"), dict) else {}
    lines.extend(["", "## 资源监控", "", f"采样数：{resource.get('sample_count_total', resource.get('sample_count', 0))}"])
    for key, item in (resource.get("metrics") or {}).items():
        if isinstance(item, dict):
            lines.append(f"- {key}：平均 {item.get('avg', 0)}，峰值 {item.get('peak', 0)}")
    return "\n".join(lines) + "\n"


@router.get("/runs/{run_id}/export")
def export_run(run_id: str, format: str = "json"):
    run = _public_run_for_export(run_id)
    selected = str(format or "json").strip().lower()
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "", str(run.get("id") or run_id)) or "report"
    if selected == "json":
        payload = dict(run)
        payload["export_version"] = "loadrunner-analysis-v1"
        return JSONResponse(payload, headers={"Content-Disposition": f'attachment; filename="runnergo-load-report-{safe_id}.json"'})
    if selected == "csv":
        return PlainTextResponse(_export_csv(run), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="runnergo-load-report-{safe_id}.csv"'})
    if selected in {"md", "markdown"}:
        return PlainTextResponse(_export_markdown(run), media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="runnergo-load-report-{safe_id}.md"'})
    if selected == "html":
        summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
        table = "".join(f"<tr><td>{html.escape(str(item.get('name') or item.get('key') or ''))}</td><td>{item.get('count',0)}</td><td>{item.get('pass_rate',0)}</td><td>{item.get('tps',0)}</td><td>{item.get('p95_ms',0)}</td></tr>" for item in summary.get("business_transactions") or [] if isinstance(item, dict))
        resource = summary.get("resource_monitoring") if isinstance(summary.get("resource_monitoring"), dict) else {}
        resource_table = "".join(f"<tr><td>{html.escape(str(key))}</td><td>{item.get('avg',0)}</td><td>{item.get('peak',0)}</td></tr>" for key, item in (resource.get("metrics") or {}).items() if isinstance(item, dict))
        http = summary.get("http_waterfall") if isinstance(summary.get("http_waterfall"), dict) else {}
        page_rows = "".join(
            f"<tr><td>{html.escape(str(item.get('url','')))}</td><td>{html.escape(str(item.get('method','')))}</td><td>{html.escape(str(item.get('resource_type','')))}</td><td>{item.get('count',0)}</td><td>{item.get('pct',0)}%</td><td>{(item.get('total') or {}).get('avg',0)}</td><td>{(item.get('total') or {}).get('p95',0)}</td><td>{(item.get('total') or {}).get('p99',0)}</td><td>{(item.get('ttfb') or {}).get('p95',0)}</td></tr>"
            for item in http.get("page_components") or [] if isinstance(item, dict)
        )
        page_section = f"<h2>页面组件分解（按 URL，等同 LoadRunner Page Component Breakdown）</h2><table border='1' cellpadding='6'><thead><tr><th>URL</th><th>方法</th><th>类型</th><th>次数</th><th>占比</th><th>平均(ms)</th><th>P95(ms)</th><th>P99(ms)</th><th>TTFB P95(ms)</th></tr></thead><tbody>{page_rows}</tbody></table>" if page_rows else ""
        document = f"<!doctype html><meta charset='utf-8'><title>LoadRunner 报告</title><h1>{html.escape(str(run.get('scenario_name') or run.get('id') or '压测报告'))}</h1><p>状态：{html.escape(str(run.get('status') or '-'))}；TPS：{summary.get('tps',0)}；P95：{summary.get('p95_ms',0)} ms；错误率：{summary.get('error_rate',0)}%</p><h2>业务事务</h2><table border='1' cellpadding='6'><thead><tr><th>业务事务</th><th>次数</th><th>通过率</th><th>TPS</th><th>P95(ms)</th></tr></thead><tbody>{table}</tbody></table>{page_section}<h2>资源监控</h2><table border='1' cellpadding='6'><thead><tr><th>资源</th><th>平均</th><th>峰值</th></tr></thead><tbody>{resource_table}</tbody></table>"
        return HTMLResponse(document, headers={"Content-Disposition": f'attachment; filename="runnergo-load-report-{safe_id}.html"'})
    raise HTTPException(400, "format 仅支持 json/csv/markdown/html")


@router.post("/runs/{run_id}/stop")
def stop_run(run_id: str):
    with _RUNS_LOCK:
        state = _RUNS.get(run_id)
    if not state:
        rows = db.execute("SELECT status FROM load_test_runs WHERE id=?", (run_id,), fetch=True)
        if not rows:
            raise HTTPException(404, "压测记录不存在")
        if rows[0]["status"] in TERMINAL_STATUSES:
            return {"id": run_id, "status": rows[0]["status"], "stopped": False}
        raise HTTPException(409, "压测运行状态不在当前服务进程中，无法安全停止")
    if state.status in TERMINAL_STATUSES:
        return {"id": run_id, "status": state.status, "stopped": False}
    state.status = "stopping"
    state.stop_event.set()
    if state.config.get("execution_mode") == "distributed":
        try:
            redis_queue.request_load_stop(state.run_id)
        except Exception as exc:
            state.log(f"分布式停止信号发送失败: {exc}")
    state.log("收到停止请求，正在回收虚拟用户")
    _persist_state(state)
    return {"id": run_id, "status": state.status, "stopped": True}


class AdjustVUsIn(BaseModel):
    virtual_users: int


@router.post("/runs/{run_id}/adjust")
def adjust_vus(run_id: str, body: AdjustVUsIn):
    with _RUNS_LOCK:
        state = _RUNS.get(run_id)
    if not state:
        raise HTTPException(404, "压测记录不存在或不在当前进程中")
    if state.status not in RUNNING_STATUSES:
        raise HTTPException(409, f"压测状态为 {state.status}，无法调整")
    new_vus = max(0, int(body.virtual_users))
    old_vus = state.target_vus()
    state.vu_override = new_vus
    state.log(f"实时调整 VU: {old_vus} → {new_vus}")
    _persist_state(state)
    return {"id": run_id, "status": state.status, "vu_override": new_vus}


@router.post("/runs/{run_id}/breakpoint")
def toggle_breakpoint(run_id: str):
    with _RUNS_LOCK:
        state = _RUNS.get(run_id)
    if not state:
        raise HTTPException(404, "压测记录不存在或不在当前进程中")
    if state.status not in RUNNING_STATUSES:
        raise HTTPException(409, f"压测状态为 {state.status}，无法操作断点")
    if state.debug_pause_event.is_set():
        state.debug_pause_event.clear()
        state.log("断点已取消，继续执行")
        return {"id": run_id, "paused": False}
    state.debug_pause_event.set()
    state.log("断点已设置，暂停执行")
    return {"id": run_id, "paused": True}


@router.post("/runs/{run_id}/resume")
def resume_run(run_id: str):
    with _RUNS_LOCK:
        state = _RUNS.get(run_id)
    if not state:
        raise HTTPException(404, "压测记录不存在或不在当前进程中")
    state.debug_pause_event.clear()
    state.debug_step_event.set()
    state.log("从断点恢复执行")
    return {"id": run_id, "resumed": True}


@router.get("/runs/compare")
def compare_runs(ids: str):
    run_ids = [rid.strip() for rid in ids.split(",") if rid.strip()]
    if not run_ids:
        raise HTTPException(400, "ids 参数不能为空")
    if len(run_ids) > 10:
        raise HTTPException(400, "最多对比 10 条压测记录")
    results = []
    for rid in run_ids:
        with _RUNS_LOCK:
            state = _RUNS.get(rid)
        if state:
            run = state.public(compact=True)
        else:
            rows = db.execute("SELECT * FROM load_test_runs WHERE id=?", (rid,), fetch=True)
            if not rows:
                results.append({"id": rid, "error": "记录不存在"})
                continue
            run = _row_public(rows[0], compact=True)
        summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
        config = run.get("config") if isinstance(run.get("config"), dict) else {}
        results.append({
            "id": rid,
            "scenario_name": run.get("scenario_name") or "",
            "status": run.get("status") or "",
            "virtual_users": config.get("virtual_users", 0),
            "completed": summary.get("completed", 0),
            "passed": summary.get("passed", 0),
            "failed": summary.get("failed", 0),
            "tps": summary.get("tps", 0),
            "avg_ms": summary.get("avg_ms", 0),
            "p95_ms": summary.get("p95_ms", 0),
            "p99_ms": summary.get("p99_ms", 0),
            "error_rate": summary.get("error_rate", 0),
            "chain_success_rate": (summary.get("chain_integrity") or {}).get("success_rate", 0),
            "started_at": run.get("started_at") or "",
            "finished_at": run.get("finished_at") or "",
        })
    return {"runs": results}
