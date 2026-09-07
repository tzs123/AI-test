"""Read RunnerGo API/SQL test objects without persisting their secrets in flows."""

from __future__ import annotations

import copy
import json
import re
from collections import defaultdict
from typing import Any, Iterable, Optional
from urllib.parse import urljoin

import requests

from backend import settings


class TestObjectError(ValueError):
    """The selected RunnerGo test object is invalid or unavailable."""


class VumQuotaError(ValueError):
    """The current RunnerGo team VUM quota is invalid or unavailable."""


_RG_VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\}\}")
_DYNAMIC_AUTH_HEADER = re.compile(r"(?:timestamp|nonce|signature|(^|[-_])sign($|[-_]))", re.I)
_LITERAL_EPOCH = re.compile(r"^\d{10,13}$")
_COMPARE_OPERATORS = {
    "includes": "contains",
    "contain": "contains",
    "unincludes": "not_contains",
    "notcontain": "not_contains",
    "equal": "equals",
    "equals": "equals",
    "unequal": "not_equals",
    "notequal": "not_equals",
    "greaterthan": "gt",
    "greaterthanorequal": "gte",
    "lessthan": "lt",
    "lessthanorequal": "lte",
    "originatingfrom": "starts_with",
    "endin": "ends_with",
    "null": "equals",
    "notnull": "not_equals",
}


def _replace_runnergo_variables(value: Any) -> Any:
    if isinstance(value, str):
        return _RG_VARIABLE.sub(lambda match: "${" + match.group(1) + "}", value)
    if isinstance(value, list):
        return [_replace_runnergo_variables(item) for item in value]
    if isinstance(value, dict):
        return {key: _replace_runnergo_variables(item) for key, item in value.items()}
    return value


def _enabled(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    marker = item.get("is_checked", 1)
    return str(marker).strip() == "1"


def _parameters(container: Any) -> list[dict[str, Any]]:
    if isinstance(container, dict):
        container = container.get("parameter") or []
    if not isinstance(container, list):
        return []
    return [item for item in container if _enabled(item)]


def _parameter_map(container: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in _parameters(container):
        key = str(item.get("key") or "").strip()
        if key:
            result[key] = _replace_runnergo_variables(item.get("value", ""))
    return result


def _object_variables(detail: dict[str, Any], *additional_groups: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}

    def add(container: Any) -> None:
        for item in _parameters(container):
            key = str(item.get("key") or "").strip()
            if key:
                result[key] = _replace_runnergo_variables(item.get("value", ""))

    global_variable = detail.get("global_variable") if isinstance(detail.get("global_variable"), dict) else {}
    add(global_variable.get("variable"))
    for group in additional_groups:
        if isinstance(group, dict):
            add(group.get("variable"))
        else:
            add(group)
    add(detail.get("variable"))
    return result


def _clean_variable_name(value: Any) -> str:
    name = str(value or "").strip()
    match = re.fullmatch(r"\{\{\s*(.*?)\s*\}\}", name)
    return (match.group(1) if match else name).strip()


def _operator(value: Any) -> str:
    normalized = re.sub(r"[^a-z]", "", str(value or "equal").lower())
    return _COMPARE_OPERATORS.get(normalized, str(value or "equals").strip().lower())


def _expected_value(rule: dict[str, Any]) -> Any:
    compare = re.sub(r"[^a-z]", "", str(rule.get("compare") or "").lower())
    if compare in {"null", "notnull"}:
        return ""
    return _replace_runnergo_variables(rule.get("val", ""))


def _absolute_url(detail: dict[str, Any], request: dict[str, Any]) -> str:
    url = str(detail.get("url") or request.get("url") or "").strip()
    if url.startswith(("http://", "https://")):
        return _replace_runnergo_variables(url)
    prefix = str(request.get("pre_url") or detail.get("pre_url") or "").strip()
    if prefix:
        return _replace_runnergo_variables(urljoin(prefix.rstrip("/") + "/", url.lstrip("/")))
    return _replace_runnergo_variables(url)


def _auth_headers(request: dict[str, Any], headers: dict[str, Any]) -> Optional[dict[str, Any]]:
    auth = request.get("auth") if isinstance(request.get("auth"), dict) else {}
    auth_type = str(auth.get("type") or "").strip().lower()
    if auth_type == "bearer":
        bearer = auth.get("bearer") if isinstance(auth.get("bearer"), dict) else {}
        token = bearer.get("key")
        if token and not any(str(key).lower() == "authorization" for key in headers):
            headers["Authorization"] = "Bearer " + str(_replace_runnergo_variables(token))
    elif auth_type == "basic":
        basic = auth.get("basic") if isinstance(auth.get("basic"), dict) else {}
        if not any(str(key).lower() == "authorization" for key in headers):
            return {
                "username": _replace_runnergo_variables(basic.get("username") or ""),
                "password": _replace_runnergo_variables(basic.get("password") or ""),
            }
    elif auth_type in {"kv", "apikey", "api_key"}:
        kv = auth.get("kv") if isinstance(auth.get("kv"), dict) else {}
        key = str(kv.get("key") or "").strip()
        if key:
            headers.setdefault(key, _replace_runnergo_variables(kv.get("value", "")))
    return None


def _api_assertions(request: dict[str, Any]) -> list[dict[str, Any]]:
    assertions = request.get("assert") or []
    if not isinstance(assertions, list):
        return []
    result = []
    for rule in assertions:
        if not _enabled(rule):
            continue
        response_type = int(rule.get("response_type") or 2)
        variable = str(rule.get("var") or "").strip()
        if response_type == 3:
            path = "$.status"
        elif response_type == 1:
            path = f"$.headers.{variable}" if variable else "$.headers"
        else:
            path = variable or "$"
        result.append({
            "path": path,
            "operator": _operator(rule.get("compare")),
            "expected": _expected_value(rule),
        })
    return result


def _api_extracts(request: dict[str, Any]) -> dict[str, str]:
    regexes = request.get("regex") or []
    if not isinstance(regexes, list):
        return {}
    result = {}
    for rule in regexes:
        if not _enabled(rule) or int(rule.get("type") or 0) != 1:
            continue
        name = _clean_variable_name(rule.get("var"))
        path = str(rule.get("express") or "").strip()
        if name and path:
            result[name] = path
    return result


def _api_object_warnings(request: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    static_auth_headers = []
    for item in _parameters(request.get("header")):
        key = str(item.get("key") or "").strip()
        value = str(item.get("value") or "").strip()
        if not key or not value or not _DYNAMIC_AUTH_HEADER.search(key):
            continue
        if "{{" in value or "${" in value:
            continue
        if "timestamp" in key.lower() and not _LITERAL_EPOCH.fullmatch(value):
            continue
        static_auth_headers.append(key)
    if static_auth_headers:
        warnings.append(
            "动态认证头使用固定值，回放时可能过期："
            + ", ".join(static_auth_headers)
            + "；建议改为变量或前置脚本生成"
        )
    return warnings


def _api_step(detail: dict[str, Any]) -> dict[str, Any]:
    request = detail.get("request") if isinstance(detail.get("request"), dict) else {}
    global_variable = detail.get("global_variable") if isinstance(detail.get("global_variable"), dict) else {}
    headers = _parameter_map(global_variable.get("header"))
    headers.update(_parameter_map(request.get("header")))
    basic_auth = _auth_headers(request, headers)
    cookies = _parameter_map(global_variable.get("cookie"))
    cookies.update(_parameter_map(request.get("cookie")))
    if cookies and not any(str(key).lower() == "cookie" for key in headers):
        headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in cookies.items())
    step: dict[str, Any] = {
        "action": "api",
        "method": str(detail.get("method") or request.get("method") or "GET").upper(),
        "url": _absolute_url(detail, request),
        "headers": headers,
        "params": _parameter_map(request.get("query")),
    }
    variables = _object_variables(detail)
    if variables:
        step["_object_variables"] = variables
    if basic_auth is not None:
        step["_basic_auth"] = basic_auth
    event = request.get("event") if isinstance(request.get("event"), dict) else {}
    scripts = {}
    pre_script = str(event.get("pre_script") or "")
    post_script = str(event.get("test") or "")
    if pre_script.strip():
        scripts["pre"] = pre_script
    if post_script.strip():
        scripts["post"] = post_script
    if scripts:
        step["_scripts"] = scripts
    body = request.get("body") if isinstance(request.get("body"), dict) else {}
    mode = str(body.get("mode") or "none").strip().lower()
    raw = _replace_runnergo_variables(body.get("raw", ""))
    if mode == "json" and str(raw).strip():
        try:
            step["json"] = json.loads(raw)
        except (TypeError, ValueError):
            step["data"] = raw
    elif mode in {"urlencoded", "x-www-form-urlencoded", "form-data", "form"}:
        step["form"] = _parameter_map(body)
    elif mode not in {"", "none"} and str(raw).strip():
        step["data"] = raw
    assertion_request = dict(request)
    assertion_request["assert"] = [
        *(global_variable.get("assert") or []),
        *(request.get("assert") or []),
    ]
    assertions = _api_assertions(assertion_request)
    if assertions:
        step["assert"] = assertions
    extracts = _api_extracts(request)
    if extracts:
        step["extract"] = extracts
    warnings = _api_object_warnings(request)
    if warnings:
        step["_object_warnings"] = warnings
    timeout = (request.get("http_api_setup") or {}).get("read_time_out") if isinstance(request.get("http_api_setup"), dict) else None
    if timeout:
        step["timeout"] = int(timeout) * 1000 if int(timeout) < 1000 else int(timeout)
    return step


def _sql_assertions(sql_detail: dict[str, Any]) -> list[dict[str, Any]]:
    assertions = sql_detail.get("assert") or []
    if not isinstance(assertions, list):
        return []
    result = []
    for rule in assertions:
        if not _enabled(rule):
            continue
        field = str(rule.get("field") or "").strip()
        if not field:
            continue
        index = int(rule.get("index") if rule.get("index") is not None else 0)
        path = f"$.rows[{index}].{field}" if index >= 0 else f"$.first.{field}"
        result.append({
            "path": path,
            "operator": _operator(rule.get("compare")),
            "expected": _expected_value(rule),
        })
    return result


def _sql_extracts(sql_detail: dict[str, Any]) -> dict[str, str]:
    regexes = sql_detail.get("regex") or []
    if not isinstance(regexes, list):
        return {}
    result = {}
    for rule in regexes:
        if not _enabled(rule):
            continue
        name = _clean_variable_name(rule.get("var"))
        field = str(rule.get("field") or "").strip()
        if not name or not field:
            continue
        index = int(rule.get("index") if rule.get("index") is not None else 0)
        result[name] = f"$.rows[{index}].{field}" if index >= 0 else f"$.first.{field}"
    return result


def _sql_step(detail: dict[str, Any]) -> dict[str, Any]:
    sql_detail = detail.get("sql_detail") if isinstance(detail.get("sql_detail"), dict) else {}
    info = sql_detail.get("sql_database_info") if isinstance(sql_detail.get("sql_database_info"), dict) else {}
    driver = str(info.get("type") or "mysql").strip().lower()
    connection = {
        "driver": driver,
        "host": info.get("host") or "127.0.0.1",
        "port": info.get("port") or (5432 if driver in {"postgres", "postgresql"} else 3306),
        "user": info.get("user") or "",
        "password": info.get("password") or "",
        "database": info.get("db_name") or "",
        "charset": info.get("charset") or "utf8mb4",
    }
    step: dict[str, Any] = {
        "action": "db",
        "sql": _replace_runnergo_variables(sql_detail.get("sql_string") or ""),
        "connection": connection,
    }
    variables = _object_variables(
        detail,
        sql_detail.get("global_variable"),
        sql_detail.get("sql_variable"),
    )
    if variables:
        step["_object_variables"] = variables
    assertions = _sql_assertions(sql_detail)
    if assertions:
        step["assert"] = assertions
    extracts = _sql_extracts(sql_detail)
    if extracts:
        step["extract"] = extracts
    return step


def object_key(team_id: Any, target_id: Any) -> str:
    return f"{str(team_id or '').strip()}:{str(target_id or '').strip()}"


def convert_test_object(detail: dict[str, Any]) -> dict[str, Any]:
    target_type = str(detail.get("target_type") or "api").strip().lower()
    if target_type == "api":
        step = _api_step(detail)
    elif target_type == "sql":
        step = _sql_step(detail)
    else:
        raise TestObjectError(f"暂不支持引用 {target_type or '未知'} 类型测试对象")
    return {
        "team_id": str(detail.get("team_id") or "").strip(),
        "target_id": str(detail.get("target_id") or "").strip(),
        "target_type": target_type,
        "name": str(detail.get("name") or "").strip(),
        "step": step,
    }


def _headers(token: str, team_id: str) -> dict[str, str]:
    return {
        "Authorization": str(token or "").strip(),
        "token": str(token or "").strip(),
        "CurrentTeamID": str(team_id or "").strip(),
    }


def _get(path: str, *, token: str, team_id: str, params: Any) -> dict[str, Any]:
    if not token:
        raise TestObjectError("引用 RunnerGo 测试对象需要有效登录态")
    try:
        response = requests.get(
            settings.RUNNERGO_MANAGEMENT_API_URL + path,
            headers=_headers(token, team_id),
            params=params,
            timeout=8,
        )
    except requests.RequestException as exc:
        raise TestObjectError("RunnerGo 测试对象服务暂时不可用") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise TestObjectError("RunnerGo 测试对象服务返回了无效数据") from exc
    if not response.ok or str(payload.get("code")) != "0":
        message = payload.get("et") or payload.get("em") or payload.get("message") or "读取测试对象失败"
        raise TestObjectError(str(message))
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _vum_request(
    method: str,
    path: str,
    *,
    token: str,
    team_id: str,
    params: Any = None,
    body: Any = None,
) -> dict[str, Any]:
    if not token:
        raise VumQuotaError("读取 VUM 额度需要有效登录态")
    if not team_id:
        raise VumQuotaError("缺少当前团队 ID")
    try:
        response = requests.request(
            method,
            settings.RUNNERGO_MANAGEMENT_API_URL + path,
            headers=_headers(token, team_id),
            params=params,
            json=body,
            timeout=8,
        )
    except requests.RequestException as exc:
        raise VumQuotaError("RunnerGo VUM 额度服务暂时不可用") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise VumQuotaError("RunnerGo VUM 额度服务返回了无效数据") from exc
    if not response.ok or str(payload.get("code")) != "0":
        message = payload.get("et") or payload.get("em") or payload.get("message") or "VUM 额度校验失败"
        raise VumQuotaError(str(message))
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def get_team_vum(token: str, team_id: str) -> dict[str, Any]:
    data = _vum_request(
        "GET",
        "/team/vum",
        token=token,
        team_id=team_id,
        params={"team_id": team_id},
    )
    return {
        "team_id": str(data.get("team_id") or team_id),
        "available_vum_num": max(0, int(data.get("available_vum_num") or 0)),
    }


def consume_team_vum(
    token: str,
    team_id: str,
    source_id: str,
    vum_num: int,
) -> dict[str, Any]:
    data = _vum_request(
        "POST",
        "/team/vum/consume",
        token=token,
        team_id=team_id,
        body={
            "team_id": team_id,
            "source_id": source_id,
            "vum_num": int(vum_num),
        },
    )
    return {
        "team_id": str(data.get("team_id") or team_id),
        "source_id": str(data.get("source_id") or source_id),
        "consumed_vum_num": max(0, int(data.get("consumed_vum_num") or 0)),
        "available_vum_num": max(0, int(data.get("available_vum_num") or 0)),
        "already_consumed": bool(data.get("already_consumed")),
    }


def list_test_objects(token: str, team_id: str) -> list[dict[str, Any]]:
    team_id = str(team_id or "").strip()
    if not team_id:
        raise TestObjectError("缺少当前团队 ID")
    data = _get("/target/list", token=token, team_id=team_id, params={"team_id": team_id})
    targets = data.get("targets") or []
    result = []

    def visit(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            append_item(item)
            visit(item.get("children"))

    def append_item(item: dict[str, Any]) -> None:
        target_type = str(item.get("target_type") or "").strip().lower()
        if target_type not in {"api", "sql"}:
            return
        if str(item.get("team_id") or team_id) != team_id:
            return
        result.append({
            "team_id": team_id,
            "target_id": str(item.get("target_id") or ""),
            "target_type": target_type,
            "name": str(item.get("name") or ""),
            "method": str(item.get("method") or ""),
            "url": str(item.get("url") or ""),
            "parent_id": str(item.get("parent_id") or ""),
        })

    visit(targets)
    return result


def list_teams(token: str) -> list[dict[str, Any]]:
    data = _get("/team/list", token=token, team_id="", params={})
    settings_data = _get("/setting/get", token=token, team_id="", params={})
    settings = settings_data.get("settings") if isinstance(settings_data.get("settings"), dict) else {}
    current_team_id = str(settings.get("current_team_id") or "").strip()
    teams = data.get("teams") or []
    result = []
    for item in teams if isinstance(teams, list) else []:
        if not isinstance(item, dict):
            continue
        team_id = str(item.get("team_id") or "").strip()
        if not team_id:
            continue
        result.append({
            "team_id": team_id,
            "name": str(item.get("name") or team_id).strip(),
            "type": item.get("type"),
            "sort": item.get("sort"),
            "is_current": team_id == current_team_id,
        })
    return result


def get_test_objects(token: str, team_id: str, target_ids: Iterable[str]) -> list[dict[str, Any]]:
    team_id = str(team_id or "").strip()
    ids = [str(item or "").strip() for item in target_ids if str(item or "").strip()]
    if not team_id or not ids:
        return []
    params = [("team_id", team_id), *(("target_ids", target_id) for target_id in ids)]
    data = _get("/target/detail", token=token, team_id=team_id, params=params)
    targets = data.get("targets") or []
    converted = []
    for detail in targets if isinstance(targets, list) else []:
        if str(detail.get("team_id") or "") != team_id:
            raise TestObjectError("测试对象团队信息不匹配")
        converted.append(convert_test_object(detail))
    returned_ids = {item["target_id"] for item in converted}
    missing = [target_id for target_id in ids if target_id not in returned_ids]
    if missing:
        raise TestObjectError(f"测试对象不存在或无权访问: {', '.join(missing)}")
    return converted


def collect_references(payload: Any) -> list[dict[str, str]]:
    references: dict[str, dict[str, str]] = {}

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        ref = value.get("test_object_ref")
        if isinstance(ref, dict):
            team_id = str(ref.get("team_id") or "").strip()
            target_id = str(ref.get("target_id") or "").strip()
            target_type = str(ref.get("target_type") or "").strip().lower()
            if not team_id or not target_id or target_type not in {"api", "sql"}:
                raise TestObjectError("测试对象引用必须包含 team_id、target_id 和 api/sql 类型")
            references[object_key(team_id, target_id)] = {
                "team_id": team_id,
                "target_id": target_id,
                "target_type": target_type,
                "name": str(ref.get("name") or "").strip(),
            }
        for item in value.values():
            visit(item)

    visit(payload)
    return list(references.values())


def resolve_references(payloads: Iterable[Any], token: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    for payload in payloads:
        for ref in collect_references(payload):
            grouped[ref["team_id"]][ref["target_id"]] = ref
    resolved = {}
    for team_id, references in grouped.items():
        for item in get_test_objects(token, team_id, references):
            ref = references[item["target_id"]]
            if item["target_type"] != ref["target_type"]:
                raise TestObjectError(f"测试对象类型已变化: {item['name'] or item['target_id']}")
            resolved[object_key(team_id, item["target_id"])] = copy.deepcopy(item)
    return resolved
