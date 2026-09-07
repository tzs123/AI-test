"""DBeaver 风格 SQL 控制台后端。

在同一个 MySQL 连接上顺序执行多条 SQL（与 DBeaver 一致）：
- 每条语句独立返回 结果集列/行、影响行数、耗时、错误；
- 语句 N 提取的变量（关联提取）可被语句 N+1 以 ${var} 引用；
- 每条语句可挂多条断言（行数/单元格/状态），随执行结果一起返回判定。
"""
import datetime
import decimal
import logging
import re
import time

import pymysql
from django.conf import settings
from rest_framework.decorators import api_view, permission_classes, authentication_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.authentication import SessionAuthentication
from rest_framework_simplejwt.authentication import JWTAuthentication

from .authentication import ManageCookieAuthentication

logger = logging.getLogger(__name__)

MAX_STATEMENTS = 200
MAX_SQL_LENGTH = 200_000
DEFAULT_ROW_LIMIT = 200
MAX_ROW_LIMIT = 1000
DEFAULT_TIMEOUT_MS = 30_000
MAX_TIMEOUT_MS = 120_000

_VAR_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')
_VAR_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

AUTH = [ManageCookieAuthentication, JWTAuthentication, SessionAuthentication]


def _jsonable(value):
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        if abs(value) > 9007199254740991:
            return str(value)
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat(sep=' ') if isinstance(value, datetime.datetime) else value.isoformat()
    if isinstance(value, bytes):
        try:
            return value.decode('utf-8')
        except UnicodeDecodeError:
            return value.hex()[:256]
    return str(value)


def _var_name_ok(name):
    return bool(_VAR_NAME_RE.match(name))


def _substitute(sql, variables):
    """Replace ${var} placeholders. Returns (sql, missing_var_names)."""
    missing = []

    def _repl(match):
        name = match.group(1)
        if name in variables:
            return str(variables[name])
        missing.append(name)
        return match.group(0)

    return _VAR_RE.sub(_repl, sql), missing


def _compare(actual, op, expected):
    a = None if actual is None else str(actual)
    e = None if expected is None else str(expected)
    if op in ('is_null',):
        return a is None
    if op in ('not_null',):
        return a is not None
    if op in ('empty',):
        return a is not None and a.strip() == ''
    if op in ('not_empty',):
        return a is not None and a.strip() != ''
    if a is None or (e is None and op not in ('eq', 'ne')):
        return False
    try:
        if op in ('eq', 'ne', 'gt', 'gte', 'lt', 'lte'):
            # 数值优先比较，退化为字符串比较
            try:
                fa, fe = float(a), float(e)
                result = {
                    'eq': fa == fe, 'ne': fa != fe,
                    'gt': fa > fe, 'gte': fa >= fe,
                    'lt': fa < fe, 'lte': fa <= fe,
                }[op]
            except (TypeError, ValueError):
                result = {'eq': a == e, 'ne': a != e}.get(op, False)
            return result
        if op == 'contains':
            return e is not None and e in a
        if op == 'not_contains':
            return e is None or e not in a
        if op == 'regex':
            return e is not None and re.search(e, a) is not None
    except (re.error, TypeError, ValueError):
        return False
    return False


def _cell_value(result, row, col):
    rows = result.get('rows') or []
    columns = result.get('columns') or []
    if not rows:
        return None, '结果集为空'
    r = row if row is not None else 0
    if not isinstance(r, int) or r < 0 or r >= len(rows):
        return None, '行号 %s 超出范围(0-%d)' % (row, max(len(rows) - 1, 0))
    if isinstance(col, int) or (isinstance(col, str) and col.isdigit() and col not in columns):
        idx = int(col)
        if idx < 0 or idx >= len(columns):
            return None, '列号 %s 超出范围(0-%d)' % (col, max(len(columns) - 1, 0))
        return rows[r][idx], None
    name = str(col) if col is not None else (columns[0] if columns else '')
    if name in columns:
        return rows[r][columns.index(name)], None
    return None, '列 %s 不存在' % name


def _eval_assertions(rules, result):
    out = []
    for rule in rules or []:
        rtype = rule.get('type')
        op = rule.get('op', 'eq')
        expected = rule.get('value')
        actual = None
        message = None
        try:
            if rtype == 'row_count':
                actual = result.get('row_count') if result.get('columns') else result.get('affected_rows')
                passed = _compare(actual, op, expected)
            elif rtype == 'cell':
                actual, err = _cell_value(result, rule.get('row', 0), rule.get('col'))
                if err:
                    passed, message = False, err
                else:
                    passed = _compare(actual, op, expected)
            elif rtype == 'status':
                actual = result.get('status')
                passed = _compare(actual, op if op in ('eq', 'ne') else 'eq', expected or 'ok')
            else:
                passed, message = False, '未知断言类型 %s' % rtype
        except Exception as exc:  # 防御：断言本身不能炸掉执行流程
            passed, message = False, '断言执行异常: %s' % exc
        out.append({
            'name': rule.get('name') or ('%s %s %s' % (rtype, op, '' if expected is None else expected)),
            'type': rtype, 'op': op,
            'expected': _jsonable(expected),
            'actual': _jsonable(actual),
            'passed': bool(passed),
            'message': message,
        })
    return out


def _eval_extractions(rules, result):
    out = []
    variables = {}
    for rule in rules or []:
        name = (rule.get('name') or '').strip()
        rtype = rule.get('type', 'cell')
        if not _var_name_ok(name):
            out.append({'name': name, 'type': rtype, 'value': None,
                        'source': '-', 'error': '变量名必须以字母/下划线开头'})
            continue
        if rtype == 'cell':
            value, err = _cell_value(result, rule.get('row', 0), rule.get('col'))
            source = 'row%s.%s' % (rule.get('row', 0), rule.get('col'))
            out.append({'name': name, 'type': rtype, 'value': _jsonable(value),
                        'source': source, 'error': err})
            if err is None:
                variables[name] = value
        elif rtype == 'row_count':
            value = result.get('row_count') if result.get('columns') else result.get('affected_rows')
            out.append({'name': name, 'type': rtype, 'value': _jsonable(value),
                        'source': 'row_count', 'error': None})
            variables[name] = value
        elif rtype == 'affected_rows':
            value = result.get('affected_rows')
            out.append({'name': name, 'type': rtype, 'value': _jsonable(value),
                        'source': 'affected_rows', 'error': None})
            variables[name] = value
        elif rtype == 'insert_id':
            value = result.get('insert_id')
            out.append({'name': name, 'type': rtype, 'value': _jsonable(value),
                        'source': 'insert_id', 'error': None})
            variables[name] = value
        else:
            out.append({'name': name, 'type': rtype, 'value': None,
                        'source': '-', 'error': '未知提取类型 %s' % rtype})
    return out, variables


def _connect(conn_conf, timeout_ms):
    ctype = (conn_conf.get('type') or 'mysql').lower()
    if ctype != 'mysql':
        raise ValueError('SQL 控制台当前仅支持 MySQL 连接（收到类型: %s）' % ctype)
    host = (conn_conf.get('host') or '').strip()
    if not host:
        raise ValueError('缺少数据库主机 (host)')
    port = int(conn_conf.get('port') or 3306)
    if not (1 <= port <= 65535):
        raise ValueError('端口不合法: %s' % port)
    timeout_s = max(1, min(int(timeout_ms), MAX_TIMEOUT_MS) / 1000.0)
    return pymysql.connect(
        host=host,
        port=port,
        user=conn_conf.get('user') or '',
        password=conn_conf.get('password') or '',
        database=(conn_conf.get('db_name') or '').strip() or None,
        charset=(conn_conf.get('charset') or 'utf8mb4'),
        connect_timeout=min(timeout_s, 10),
        read_timeout=timeout_s,
        write_timeout=timeout_s,
        autocommit=True,
        client_flag=pymysql.constants.CLIENT.MULTI_STATEMENTS,
    )


def _run_statements(conn, statements, variables, assertions, extractions,
                    row_limit, stop_on_error):
    results = []
    vars_live = dict(variables or {})
    stop = False
    with conn.cursor() as cur:
        for idx, raw_sql in enumerate(statements):
            entry = {
                'index': idx,
                'sql': raw_sql,
                'executed_sql': raw_sql,
                'status': 'ok',
                'duration_ms': 0,
                'columns': [],
                'rows': [],
                'row_count': 0,
                'truncated': False,
                'affected_rows': 0,
                'insert_id': 0,
                'error': None,
                'skipped': False,
                'assertions': [],
                'extractions': [],
            }
            if stop:
                entry['status'] = 'skipped'
                entry['error'] = '前序语句失败，已跳过'
                results.append(entry)
                continue

            executed, missing = _substitute(raw_sql, vars_live)
            entry['executed_sql'] = executed
            if missing:
                entry['status'] = 'error'
                entry['error'] = '未定义变量: %s' % ', '.join('${%s}' % m for m in missing)
                entry['assertions'] = _eval_assertions(assertions.get(idx), entry)
                if stop_on_error:
                    stop = True
                results.append(entry)
                continue

            started = time.perf_counter()
            try:
                cur.execute(executed)
                if cur.description:
                    columns = [d[0] for d in cur.description]
                    fetch_n = row_limit + 1
                    fetched = cur.fetchmany(fetch_n)
                    truncated = len(fetched) > row_limit
                    fetched = fetched[:row_limit]
                    entry['columns'] = columns
                    entry['rows'] = [[_jsonable(c) for c in row] for row in fetched]
                    entry['row_count'] = len(fetched)
                    entry['truncated'] = truncated
                else:
                    affected = cur.rowcount
                    entry['affected_rows'] = int(affected) if affected and affected > 0 else 0
                    entry['insert_id'] = int(cur.lastrowid or 0)
            except pymysql.MySQLError as exc:
                entry['status'] = 'error'
                entry['error'] = str(exc)
                if stop_on_error:
                    stop = True
            entry['duration_ms'] = int((time.perf_counter() - started) * 1000)

            if entry['status'] == 'ok':
                new_vars_out, new_vars = _eval_extractions(extractions.get(idx), entry)
                entry['extractions'] = new_vars_out
                vars_live.update(new_vars)
            entry['assertions'] = _eval_assertions(assertions.get(idx), entry)
            results.append(entry)
    return results, vars_live


def _parse_rules(payload, key):
    raw = payload.get(key) or {}
    rules = {}
    for k, v in raw.items():
        try:
            idx = int(k)
        except (TypeError, ValueError):
            continue
        if isinstance(v, list) and v:
            rules[idx] = v
    return rules


@api_view(['POST'])
@authentication_classes(AUTH)
@permission_classes([IsAuthenticated])
def execute(request):
    payload = request.data if isinstance(request.data, dict) else {}
    conn_conf = payload.get('connection') or {}
    statements = payload.get('statements')
    if not isinstance(statements, list) or not statements:
        return Response({'code': 1, 'detail': 'statements 不能为空'})
    statements = [str(s) for s in statements][:MAX_STATEMENTS]
    if any(len(s) > MAX_SQL_LENGTH for s in statements):
        return Response({'code': 1, 'detail': '单条语句长度超过限制(%d 字符)' % MAX_SQL_LENGTH})

    try:
        row_limit = int(payload.get('row_limit') or DEFAULT_ROW_LIMIT)
    except (TypeError, ValueError):
        row_limit = DEFAULT_ROW_LIMIT
    row_limit = max(1, min(row_limit, MAX_ROW_LIMIT))
    try:
        timeout_ms = int(payload.get('timeout_ms') or DEFAULT_TIMEOUT_MS)
    except (TypeError, ValueError):
        timeout_ms = DEFAULT_TIMEOUT_MS
    timeout_ms = max(1000, min(timeout_ms, MAX_TIMEOUT_MS))
    stop_on_error = payload.get('stop_on_error', True)
    stop_on_error = True if stop_on_error is None else bool(stop_on_error)

    variables = payload.get('variables') or {}
    if not isinstance(variables, dict):
        variables = {}
    assertions = _parse_rules(payload, 'assertions')
    extractions = _parse_rules(payload, 'extractions')

    started = time.perf_counter()
    try:
        conn = _connect(conn_conf, timeout_ms)
    except ValueError as exc:
        return Response({'code': 1, 'detail': str(exc)})
    except pymysql.MySQLError as exc:
        return Response({'code': 1, 'detail': '数据库连接失败: %s' % exc})

    try:
        results, vars_final = _run_statements(
            conn, statements, variables, assertions, extractions,
            row_limit, stop_on_error,
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return Response({
        'code': 0,
        'data': {
            'results': results,
            'variables': {k: _jsonable(v) for k, v in vars_final.items()},
            'total_ms': int((time.perf_counter() - started) * 1000),
            'row_limit': row_limit,
        },
    })


@api_view(['POST'])
@authentication_classes(AUTH)
@permission_classes([IsAuthenticated])
def test_connection(request):
    payload = request.data if isinstance(request.data, dict) else {}
    conn_conf = payload.get('connection') or payload
    try:
        timeout_ms = int(payload.get('timeout_ms') or 8000)
    except (TypeError, ValueError):
        timeout_ms = 8000
    try:
        conn = _connect(conn_conf, timeout_ms)
    except ValueError as exc:
        return Response({'code': 1, 'detail': str(exc)})
    except pymysql.MySQLError as exc:
        return Response({'code': 1, 'detail': '连接失败: %s' % exc})
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT VERSION()')
            version = cur.fetchone()[0]
    except pymysql.MySQLError as exc:
        return Response({'code': 1, 'detail': '连接成功但探测失败: %s' % exc})
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return Response({'code': 0, 'data': {'version': str(version)}})
