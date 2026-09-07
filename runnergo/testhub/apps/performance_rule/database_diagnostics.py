from __future__ import annotations

from contextlib import closing
from typing import Any

import pymysql

from .tested_database_config import load_tested_database_config, redacted_tested_database_config


STATUS_NAMES = (
    'Threads_connected',
    'Threads_running',
    'Max_used_connections',
    'Connections',
    'Aborted_connects',
    'Slow_queries',
    'Innodb_row_lock_waits',
    'Innodb_row_lock_time',
)

VARIABLE_NAMES = (
    'max_connections',
    'slow_query_log',
    'long_query_time',
    'log_output',
)


def tested_db_config() -> dict[str, Any]:
    return load_tested_database_config()


def redacted_config(config: dict[str, Any]) -> dict[str, Any]:
    return redacted_tested_database_config(config)


def load_database_diagnostics() -> dict[str, Any]:
    config = tested_db_config()
    if not config['host'] or not config['user']:
        return {
            'configured': False,
            'config': redacted_config(config),
            'metrics': {},
            'slow_queries': [],
            'statement_digests': [],
            'warnings': ['APPLY_DB_HOST 和 APPLY_DB_USER 未配置'],
            'access_mode': 'readonly_select_only',
        }

    warnings: list[str] = []
    metrics: dict[str, Any] = {}
    slow_queries: list[dict[str, Any]] = []
    statement_digests: list[dict[str, Any]] = []

    with closing(_connect(config)) as connection:
        with connection.cursor() as cursor:
            metrics.update(_read_status(cursor, warnings))
            metrics.update(_read_variables(cursor, warnings))
            metrics.update(_derived_metrics(metrics))
            statement_digests = _read_statement_digests(cursor, warnings, config['database'])
            slow_queries = _read_slow_log(cursor, warnings, config['database'])
            _explain_slow_query_visibility(metrics, slow_queries, warnings)

    return {
        'configured': True,
        'config': redacted_config(config),
        'metrics': metrics,
        'slow_queries': slow_queries,
        'statement_digests': statement_digests,
        'warnings': warnings,
        'access_mode': 'readonly_select_only',
    }


def _connect(config: dict[str, Any]):
    return pymysql.connect(
        host=config['host'],
        port=config['port'],
        user=config['user'],
        password=config['password'],
        database=config['database'] or None,
        charset='utf8mb4',
        connect_timeout=5,
        read_timeout=8,
        write_timeout=8,
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _read_status(cursor, warnings: list[str]) -> dict[str, Any]:
    try:
        cursor.execute(
            '''
            SELECT VARIABLE_NAME AS Variable_name, VARIABLE_VALUE AS Value
            FROM performance_schema.global_status
            WHERE VARIABLE_NAME IN (%s)
            '''
            % ','.join(['%s'] * len(STATUS_NAMES)),
            STATUS_NAMES,
        )
        return {row['Variable_name'].lower(): _number(row['Value']) for row in cursor.fetchall()}
    except Exception as exc:
        warnings.append(f'读取 GLOBAL STATUS 失败: {exc}')
        return {}


def _read_variables(cursor, warnings: list[str]) -> dict[str, Any]:
    try:
        cursor.execute(
            '''
            SELECT VARIABLE_NAME AS Variable_name, VARIABLE_VALUE AS Value
            FROM performance_schema.global_variables
            WHERE VARIABLE_NAME IN (%s)
            '''
            % ','.join(['%s'] * len(VARIABLE_NAMES)),
            VARIABLE_NAMES,
        )
        return {row['Variable_name'].lower(): row['Value'] for row in cursor.fetchall()}
    except Exception as exc:
        warnings.append(f'读取 GLOBAL VARIABLES 失败: {exc}')
        return {}


def _derived_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    max_connections = _number(metrics.get('max_connections'))
    threads_connected = _number(metrics.get('threads_connected'))
    max_used_connections = _number(metrics.get('max_used_connections'))
    return {
        'connection_usage_percent': round(threads_connected / max_connections * 100, 2)
        if max_connections else 0,
        'max_connection_usage_percent': round(max_used_connections / max_connections * 100, 2)
        if max_connections else 0,
    }


def _read_statement_digests(
    cursor,
    warnings: list[str],
    database: str = '',
) -> list[dict[str, Any]]:
    try:
        schema_filter = 'AND SCHEMA_NAME = %s' if database else ''
        params = (database,) if database else None
        cursor.execute(
            f'''
            SELECT
                SCHEMA_NAME AS schema_name,
                DIGEST_TEXT AS digest_text,
                COUNT_STAR AS count_star,
                SUM_ERRORS AS sum_errors,
                ROUND(AVG_TIMER_WAIT / 1000000000000, 4) AS avg_seconds,
                ROUND(MAX_TIMER_WAIT / 1000000000000, 4) AS max_seconds,
                FIRST_SEEN AS first_seen,
                LAST_SEEN AS last_seen
            FROM performance_schema.events_statements_summary_by_digest
            WHERE DIGEST_TEXT IS NOT NULL
            {schema_filter}
            ORDER BY SUM_TIMER_WAIT DESC
            LIMIT 10
            ''',
            params,
        )
        return [_normalize_row(row) for row in cursor.fetchall()]
    except Exception as exc:
        warnings.append(f'读取 performance_schema SQL摘要失败: {exc}')
        return []


def _read_slow_log(
    cursor,
    warnings: list[str],
    database: str = '',
) -> list[dict[str, Any]]:
    try:
        where_clause = 'WHERE db = %s' if database else ''
        params = (database,) if database else None
        cursor.execute(
            f'''
            SELECT
                start_time,
                user_host,
                query_time,
                lock_time,
                rows_sent,
                rows_examined,
                db,
                sql_text
            FROM mysql.slow_log
            {where_clause}
            ORDER BY start_time DESC
            LIMIT 20
            ''',
            params,
        )
        return [_normalize_row(row) for row in cursor.fetchall()]
    except Exception as exc:
        warnings.append(f'读取 mysql.slow_log 失败: {exc}')
        return []


def _explain_slow_query_visibility(
    metrics: dict[str, Any],
    slow_queries: list[dict[str, Any]],
    warnings: list[str],
) -> None:
    if slow_queries:
        return

    slow_query_count = _number(metrics.get('slow_queries'))
    slow_query_log = str(metrics.get('slow_query_log') or '').upper()
    log_output = str(metrics.get('log_output') or '').upper()
    long_query_time = metrics.get('long_query_time')

    if slow_query_count <= 0:
        warnings.append('慢查询明细为空：GLOBAL STATUS Slow_queries 为 0，本次采集窗口未观察到慢查询。')
        return

    if slow_query_log and slow_query_log not in ('ON', '1'):
        warnings.append('慢查询明细为空：被测数据库 slow_query_log 未开启，只能看到累计计数，无法读取明细。')
        return

    if log_output and 'TABLE' not in log_output:
        warnings.append('慢查询明细为空：被测数据库 log_output 未包含 TABLE，慢日志可能写入文件，mysql.slow_log 表没有明细。')
        return

    suffix = f'，当前 long_query_time={long_query_time}' if long_query_time else ''
    warnings.append(f'慢查询明细为空：Slow_queries 有累计值但 mysql.slow_log 无记录，请确认慢日志表权限、清理策略或采集窗口{suffix}。')


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = {}
    for key, value in row.items():
        if hasattr(value, 'total_seconds'):
            normalized[key] = round(value.total_seconds(), 4)
        elif hasattr(value, 'isoformat'):
            normalized[key] = value.isoformat(sep=' ')
        else:
            normalized[key] = value
    return normalized


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0
