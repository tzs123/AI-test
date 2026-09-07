from dataclasses import dataclass
from typing import Any, Optional

from django.db import transaction

from .models import PerformanceResult, PerformanceRule


@dataclass(frozen=True)
class MetricRule:
    key: str
    label: str
    rule_field: str
    operator: str
    unit: str


METRIC_RULES = [
    MetricRule('tps', 'TPS', 'min_tps', '>=', ''),
    MetricRule('p95', 'P95', 'max_p95', '<=', 'ms'),
    MetricRule('error_rate', 'Error', 'max_error_rate', '<=', '%'),
    MetricRule('cpu', 'CPU', 'max_cpu', '<=', '%'),
    MetricRule('db_connection_usage', 'DB连接使用率', 'max_db_connection_usage', '<=', '%'),
    MetricRule('db_slow_queries_per_sec', 'DB慢查询/s', 'max_db_slow_queries_per_sec', '<=', '/s'),
]

METRIC_ALIASES = {
    'tps': ('tps', 'TPS'),
    'p95': ('p95', 'P95', 'max_p95', 'ninety_five_request_time_line_value'),
    'error_rate': ('error_rate', 'errorRate', 'error', 'Error', 'error_percent'),
    'cpu': ('cpu', 'CPU', 'cpu_percent', 'cpu_usage', 'max_cpu'),
    'db_connection_usage': ('db_connection_usage', 'DBConnectionUsage', 'mysql_connection_usage'),
    'db_qps': ('db_qps', 'DBQPS', 'mysql_qps', 'mysql_global_status_queries'),
    'db_slow_queries_per_sec': (
        'db_slow_queries_per_sec', 'DBSlowQueriesQPS', 'mysql_slow_queries_per_sec',
        'mysql_global_status_slow_queries',
    ),
    'db_row_lock_waits_per_sec': (
        'db_row_lock_waits_per_sec', 'DBRowLockWaitsQPS',
        'mysql_row_lock_waits_per_sec', 'mysql_global_status_innodb_row_lock_waits',
    ),
}


def _parse_number(value: Any):
    if value is None or value == '':
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(',', '')
        if cleaned.endswith('%') or cleaned.endswith('％'):
            cleaned = cleaned[:-1].strip()
        if cleaned.lower().endswith('ms'):
            cleaned = cleaned[:-2].strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def normalize_performance_metrics(metrics: dict[str, Any]) -> dict[str, Optional[float]]:
    normalized = {}
    for key, aliases in METRIC_ALIASES.items():
        normalized[key] = None
        for alias in aliases:
            if alias not in metrics:
                continue
            normalized[key] = _parse_number(metrics.get(alias))
            break
    return normalized


def get_latest_rule(project_id: int):
    return PerformanceRule.objects.filter(project_id=project_id).order_by('-create_time').first()


def evaluate_performance_rule(rule: PerformanceRule, metrics: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    normalized_metrics = normalize_performance_metrics(metrics)
    details = []

    for metric_rule in METRIC_RULES:
        actual = normalized_metrics.get(metric_rule.key)
        threshold = _parse_number(getattr(rule, metric_rule.rule_field))
        if threshold is None:
            continue

        if actual is None:
            passed = False
        elif metric_rule.operator == '>=':
            passed = actual >= threshold
        else:
            passed = actual <= threshold

        details.append({
            'metric': metric_rule.label,
            'key': metric_rule.key,
            'value': actual,
            'threshold': threshold,
            'operator': metric_rule.operator,
            'unit': metric_rule.unit,
            'status': PerformanceResult.STATUS_PASS if passed else PerformanceResult.STATUS_FAIL,
        })

    overall_status = (
        PerformanceResult.STATUS_FAIL
        if any(item['status'] == PerformanceResult.STATUS_FAIL for item in details)
        else PerformanceResult.STATUS_PASS
    )
    return details, overall_status


@transaction.atomic
def generate_performance_result(project_id: int, metrics: dict[str, Any], report_id: str = '', rule=None):
    rule = rule or get_latest_rule(project_id)
    if rule is None:
        raise ValueError('当前项目未配置 SLA 性能规则')

    normalized_metrics = normalize_performance_metrics(metrics)
    details, overall_status = evaluate_performance_rule(rule, metrics)
    defaults = {
        'rule': rule,
        'tps': normalized_metrics.get('tps'),
        'p95': normalized_metrics.get('p95'),
        'error_rate': normalized_metrics.get('error_rate'),
        'cpu': normalized_metrics.get('cpu'),
        'db_connection_usage': normalized_metrics.get('db_connection_usage'),
        'db_qps': normalized_metrics.get('db_qps'),
        'db_slow_queries_per_sec': normalized_metrics.get('db_slow_queries_per_sec'),
        'db_row_lock_waits_per_sec': normalized_metrics.get('db_row_lock_waits_per_sec'),
        'overall_status': overall_status,
        'details': details,
    }

    # A report can be analyzed repeatedly while it is still running. Replacing
    # the same project's report snapshot keeps retries/idempotent callbacks from
    # creating duplicate SLA results.
    normalized_report_id = str(report_id or '').strip()
    if normalized_report_id:
        result = PerformanceResult.objects.select_for_update().filter(
            project_id=project_id,
            report_id=normalized_report_id,
        ).order_by('-id').first()
        if result:
            for field, value in defaults.items():
                setattr(result, field, value)
            result.save(update_fields=list(defaults))
            return result

    return PerformanceResult.objects.create(
        project_id=project_id,
        report_id=normalized_report_id,
        **defaults,
    )
