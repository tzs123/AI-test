"""Runtime data exchange for mixed Web UI, HTTP and read-only DB steps.

The flow YAML remains the source of truth.  This module only keeps resolved
values in memory for one execution and exposes a deliberately small JSON-path
subset so API/DB/page steps can pass data to one another.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
from typing import Any, Dict, Iterable, Optional

import requests

from backend.url_security import validate_outbound_url
from core.runtime_case import resolve_dynamic_value
from core.script_runtime import ScriptAssertionError, ScriptExecutionError, run_script


_MISSING = object()
_VARIABLE_PATTERN = re.compile(
    r"\$\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\.\d+)*)\s*\}"
)
_JSON_PATH_TOKEN = re.compile(
    r"(?:^\$)|(?:^\.([A-Za-z_][A-Za-z0-9_-]*))|(?:^\[\s*(?:'([^']+)'|\"([^\"]+)\"|(\d+))\s*\])"
)
_READ_ONLY_SQL = re.compile(r"^(select|with|show|describe|desc|explain|pragma)\b", re.I)
_SECRET_KEY = re.compile(r"(?:password|passwd|secret|token|authorization|api[_-]?key)", re.I)
_TEST_OBJECTS_CACHE: Optional[Dict[str, Any]] = None


def _case_insensitive_get(source: Any, key: str) -> Any:
    if not isinstance(source, dict):
        return _MISSING
    if key in source:
        return source[key]
    lowered = str(key).lower()
    for item_key, value in source.items():
        if str(item_key).lower() == lowered:
            return value
    return _MISSING


def get_path(source: Any, path: Any, default: Any = _MISSING) -> Any:
    """Read a JSON-path-like expression such as ``$.data.items[0].id``."""
    raw = str(path or "").strip()
    if raw in {"", "$"}:
        return source
    if not raw.startswith("$"):
        raw = "$" + (raw if raw.startswith(".") else "." + raw)

    current = source
    remaining = raw
    while remaining:
        match = _JSON_PATH_TOKEN.match(remaining)
        if not match:
            return default
        remaining = remaining[match.end():]
        if match.group(0) == "$":
            continue
        key = match.group(1) or match.group(2) or match.group(3)
        if key is not None:
            current = _case_insensitive_get(current, key)
        else:
            try:
                current = current[int(match.group(4))]
            except (TypeError, ValueError, IndexError, KeyError):
                return default
        if current is _MISSING:
            return default
    return current


def _set_path(target: Dict[str, Any], name: str, value: Any) -> None:
    parts = [part for part in str(name or "").split(".") if part]
    if not parts:
        return
    current = target
    for part in parts[:-1]:
        existing = _case_insensitive_get(current, part)
        if not isinstance(existing, dict):
            current[part] = {}
            existing = current[part]
        current = existing
    current[parts[-1]] = value


def _remove_path(target: Dict[str, Any], name: str) -> None:
    parts = [part for part in str(name or "").split(".") if part]
    if not parts:
        return
    current: Any = target
    for part in parts[:-1]:
        current = _case_insensitive_get(current, part)
        if not isinstance(current, dict):
            return
    lowered = parts[-1].casefold()
    for key in list(current):
        if str(key).casefold() == lowered:
            current.pop(key, None)
            return


def _stringify(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def assert_runtime_value(
    actual: Any,
    expected: Any = None,
    *,
    operator: str = "equals",
    label: str = "数据",
) -> None:
    """Compare a page/API/DB value with a resolved runtime value."""
    operation = str(operator or "equals").strip().lower()
    if operation in {"eq", "equal", "is"}:
        operation = "equals"
    if operation in {"ne", "not_equal"}:
        operation = "not_equals"
    if operation in {"match", "regexp"}:
        operation = "regex"

    if operation in {"exists", "not_exists"}:
        matched = actual is not _MISSING and actual is not None
        if operation == "not_exists":
            matched = not matched
    elif operation == "equals":
        matched = actual == expected or _stringify(actual) == _stringify(expected)
    elif operation == "not_equals":
        matched = not (actual == expected or _stringify(actual) == _stringify(expected))
    elif operation == "contains":
        if isinstance(actual, (dict, list, tuple, set)):
            matched = expected in actual
        else:
            matched = _stringify(expected) in _stringify(actual)
    elif operation == "not_contains":
        if isinstance(actual, (dict, list, tuple, set)):
            matched = expected not in actual
        else:
            matched = _stringify(expected) not in _stringify(actual)
    elif operation == "starts_with":
        matched = _stringify(actual).startswith(_stringify(expected))
    elif operation == "ends_with":
        matched = _stringify(actual).endswith(_stringify(expected))
    elif operation == "regex":
        matched = re.search(_stringify(expected), _stringify(actual)) is not None
    elif operation in {"gt", "greater_than", "gte", "greater_or_equal", "lt", "less_than", "lte", "less_or_equal"}:
        try:
            left, right = float(actual), float(expected)
        except (TypeError, ValueError):
            left, right = _stringify(actual), _stringify(expected)
        if operation in {"gt", "greater_than"}:
            matched = left > right
        elif operation in {"gte", "greater_or_equal"}:
            matched = left >= right
        elif operation in {"lt", "less_than"}:
            matched = left < right
        else:
            matched = left <= right
    else:
        raise ValueError(f"不支持的数据断言操作符: {operator}")

    if not matched:
        raise AssertionError(
            f"{label}断言失败：实际值={_stringify(actual)!r}，"
            f"操作符={operation}，期望值={_stringify(expected)!r}"
        )


class RuntimeDataContext:
    """Execution-only scopes shared by page, API and DB steps."""

    def __init__(self, seed: Optional[Dict[str, Any]] = None):
        # A load-test virtual user may attach one requests.Session here so
        # sequential API steps reuse TCP connections like JMeter/LoadRunner.
        self.http_session = None
        self.scopes: Dict[str, Dict[str, Any]] = {
            "global": {},
            "local": {},
            "outputs": {},
            "data": {},
            "dataAssets": {},
            "smartData": {},
            "api": {},
            "db": {},
            "database": {},
            "page": {},
        }
        if isinstance(seed, dict):
            for scope, values in seed.items():
                if scope in self.scopes and isinstance(values, dict):
                    self.scopes[scope].update(copy.deepcopy(values))

    def lookup(self, name: str) -> Any:
        raw = str(name or "").strip()
        if not raw:
            return _MISSING
        parts = raw.split(".")
        if parts[0] in self.scopes and len(parts) > 1:
            value = get_path(self.scopes[parts[0]], ".".join(parts[1:]))
            if value is not _MISSING:
                return value
        for scope in ("local", "data", "dataAssets", "smartData", "global", "outputs", "api", "db", "database", "page"):
            value = _case_insensitive_get(self.scopes[scope], raw)
            if value is not _MISSING:
                return value
            value = get_path(self.scopes[scope], raw)
            if value is not _MISSING:
                return value
        return _MISSING

    def resolve(self, value: Any) -> Any:
        if isinstance(value, str):
            full_match = _VARIABLE_PATTERN.fullmatch(value)
            if full_match:
                found = self.lookup(full_match.group(1))
                if found is not _MISSING:
                    return copy.deepcopy(found)

            def replace(match: re.Match) -> str:
                found = self.lookup(match.group(1))
                return match.group(0) if found is _MISSING else _stringify(found)

            resolved = _VARIABLE_PATTERN.sub(replace, value)
            return resolve_dynamic_value(resolved)
        if isinstance(value, list):
            return [self.resolve(item) for item in value]
        if isinstance(value, dict):
            return {key: self.resolve(item) for key, item in value.items()}
        return value

    def assign(self, name: str, value: Any, *, scope: str = "local", category: str = "") -> None:
        clean_name = str(name or "").strip()
        if not clean_name:
            return
        target = self.scopes.setdefault(scope, {})
        _set_path(target, clean_name, copy.deepcopy(value))
        if scope != "outputs":
            _set_path(self.scopes["outputs"], clean_name, copy.deepcopy(value))
        if category:
            category_target = self.scopes.setdefault(category, {})
            _set_path(category_target, clean_name, copy.deepcopy(value))

    def unset(self, name: str) -> None:
        for values in self.scopes.values():
            _remove_path(values, name)

    def script_variables(self) -> Dict[str, Any]:
        """Flatten visible top-level variables using normal lookup precedence."""
        result: Dict[str, Any] = {}
        precedence = ("local", "data", "dataAssets", "smartData", "global", "outputs", "api", "db", "database", "page")
        for scope in reversed(precedence):
            values = self.scopes.get(scope)
            if isinstance(values, dict):
                result.update(copy.deepcopy(values))
        return result

    def public(self) -> Dict[str, Any]:
        return copy.deepcopy(self.scopes)


class _RuntimeDataOverlay(RuntimeDataContext):
    """Resolve a step's object defaults without polluting shared run data."""

    def __init__(self, parent: RuntimeDataContext, defaults: Dict[str, Any]):
        self.parent = parent
        self.defaults = copy.deepcopy(defaults)
        self._resolving_defaults: set[str] = set()
        self._deleted_defaults: set[str] = set()

    def lookup(self, name: str) -> Any:
        found = self.parent.lookup(name)
        if found is not _MISSING:
            return found

        raw = str(name or "").strip()
        if raw.casefold() in self._deleted_defaults:
            return _MISSING
        found = _case_insensitive_get(self.defaults, raw)
        if found is _MISSING:
            found = get_path(self.defaults, raw)
        if found is _MISSING:
            return _MISSING

        lookup_key = raw.casefold()
        if lookup_key in self._resolving_defaults:
            return _MISSING
        self._resolving_defaults.add(lookup_key)
        try:
            return self.resolve(copy.deepcopy(found))
        finally:
            self._resolving_defaults.discard(lookup_key)

    def assign(self, name: str, value: Any, *, scope: str = "local", category: str = "") -> None:
        self._deleted_defaults.discard(str(name or "").strip().casefold())
        self.parent.assign(name, value, scope=scope, category=category)

    def unset(self, name: str) -> None:
        clean_name = str(name or "").strip()
        self.parent.unset(clean_name)
        if clean_name:
            self._deleted_defaults.add(clean_name.casefold())

    def script_variables(self) -> Dict[str, Any]:
        result = copy.deepcopy(self.defaults)
        for key in list(result):
            if str(key).casefold() in self._deleted_defaults:
                result.pop(key, None)
        result.update(self.parent.script_variables())
        return result

    def public(self) -> Dict[str, Any]:
        return self.parent.public()


def _step_runtime_context(context: RuntimeDataContext, step: Dict[str, Any]) -> RuntimeDataContext:
    defaults = step.get("_object_variables")
    if not isinstance(defaults, dict) or not defaults:
        return context
    return _RuntimeDataOverlay(context, defaults)


def _extract_mapping(context: RuntimeDataContext, source: Any, mapping: Any) -> Dict[str, Any]:
    if not isinstance(mapping, dict):
        return {}
    extracted = {}
    for name, expression in mapping.items():
        value = get_path(source, context.resolve(expression))
        if value is _MISSING:
            raise AssertionError(f"数据提取失败：{name} 未命中路径 {expression}")
        context.assign(str(name), value)
        extracted[str(name)] = copy.deepcopy(value)
    return extracted


def _runtime_test_objects() -> Dict[str, Any]:
    global _TEST_OBJECTS_CACHE
    if _TEST_OBJECTS_CACHE is not None:
        return _TEST_OBJECTS_CACHE
    raw = ""
    secret_dir = os.environ.get("RUNNERGO_TEST_OBJECTS_DIR", "").strip()
    secret_path = ""
    if secret_dir:
        worker_id = os.environ.get("PYTEST_XDIST_WORKER", "").strip() or "main"
        if not re.fullmatch(r"[A-Za-z0-9_-]+", worker_id):
            raise ValueError("pytest worker 标识无效")
        secret_path = os.path.join(secret_dir, f"{worker_id}.json")
    if not secret_path:
        secret_path = os.environ.get("RUNNERGO_TEST_OBJECTS_FILE", "").strip()
    if secret_path:
        try:
            with open(secret_path, "r", encoding="utf-8") as handle:
                raw = handle.read()
        finally:
            try:
                os.remove(secret_path)
            except OSError:
                pass
            if secret_dir:
                try:
                    os.rmdir(secret_dir)
                except OSError:
                    pass
    if not raw:
        raw = os.environ.get("RUNNERGO_TEST_OBJECTS_JSON", "").strip()
    if not raw:
        _TEST_OBJECTS_CACHE = {}
        return _TEST_OBJECTS_CACHE
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("RunnerGo 测试对象运行时配置不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("RunnerGo 测试对象运行时配置必须是对象")
    _TEST_OBJECTS_CACHE = payload
    return _TEST_OBJECTS_CACHE


def _merge_test_object_step(step: Dict[str, Any], expected_type: str) -> Dict[str, Any]:
    ref = step.get("test_object_ref")
    if not isinstance(ref, dict):
        return step
    team_id = str(ref.get("team_id") or "").strip()
    target_id = str(ref.get("target_id") or "").strip()
    target_type = str(ref.get("target_type") or "").strip().lower()
    if not team_id or not target_id:
        raise ValueError("RunnerGo 测试对象引用缺少 team_id 或 target_id")
    if target_type != expected_type:
        raise ValueError(f"当前步骤需要 {expected_type} 测试对象，实际引用为 {target_type or '未知类型'}")
    item = _runtime_test_objects().get(f"{team_id}:{target_id}")
    if not isinstance(item, dict) or not isinstance(item.get("step"), dict):
        raise ValueError(f"RunnerGo 测试对象未在本次执行中解析: {ref.get('name') or target_id}")
    if str(item.get("target_type") or "").lower() != expected_type:
        raise ValueError(f"RunnerGo 测试对象类型不匹配: {ref.get('name') or target_id}")
    merged = copy.deepcopy(item["step"])
    for key, value in step.items():
        if key in {"headers", "params", "extract", "extract_vars"} and isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(copy.deepcopy(value))
        else:
            merged[key] = copy.deepcopy(value)
    merged["test_object_ref"] = copy.deepcopy(ref)
    return merged


def _record_assertion(
    result: Dict[str, Any],
    *,
    label: str,
    operator: str,
    expected: Any,
    actual: Any,
    passed: bool,
    message: str = "",
) -> None:
    """把每条断言的评估明细写入 result["assertions"]，供报告统计与展示。"""
    entries = result.get("assertions")
    if not isinstance(entries, list):
        entries = []
        result["assertions"] = entries
    entries.append({
        "label": str(label or ""),
        "operator": str(operator or "equals"),
        "expected": "(路径不存在)" if expected is _MISSING else _stringify(expected),
        "actual": "(路径不存在)" if actual is _MISSING else _stringify(actual),
        "passed": bool(passed),
        "message": str(message or ""),
    })


def _evaluate_rule_assertion(
    result: Dict[str, Any],
    failures: list,
    *,
    actual: Any,
    expected: Any,
    operator: str,
    label: str,
) -> None:
    """评估单条断言规则：无论通过与否都记录明细，失败信息追加到 failures。"""
    try:
        assert_runtime_value(actual, expected, operator=operator, label=label)
    except AssertionError as exc:
        _record_assertion(
            result,
            label=label,
            operator=operator,
            expected=expected,
            actual=actual,
            passed=False,
            message=str(exc),
        )
        failures.append(str(exc))
        return
    _record_assertion(
        result,
        label=label,
        operator=operator,
        expected=expected,
        actual=actual,
        passed=True,
    )


def _api_assertions(context: RuntimeDataContext, step: Dict[str, Any], result: Dict[str, Any]) -> None:
    rules = step.get("assert") or step.get("assertions") or {}
    if not isinstance(rules, (dict, list)):
        return
    if isinstance(rules, dict):
        if not rules:
            return
        rules = [rules]
    failures: list = []
    for rule in rules:
        if isinstance(rule, str):
            rule = {"path": "$.text", "contains": rule}
        if not isinstance(rule, dict):
            continue
        path = rule.get("path") or rule.get("json_path")
        resolved_path = context.resolve(path) if path else ""
        actual = result.get("body")
        if resolved_path:
            actual = get_path(result.get("body"), resolved_path)
            if actual is _MISSING:
                actual = get_path(result, resolved_path)
        if "status" in rule or "status_code" in rule:
            expected = context.resolve(rule.get("status", rule.get("status_code")))
            _evaluate_rule_assertion(
                result, failures,
                actual=result.get("status"), expected=expected,
                operator="equals", label="接口状态码",
            )
        if "equals" in rule or "expected" in rule or "value" in rule:
            expected = context.resolve(rule.get("equals", rule.get("expected", rule.get("value"))))
            _evaluate_rule_assertion(
                result, failures,
                actual=actual, expected=expected,
                operator=rule.get("operator") or "equals",
                label=f"接口数据 {path or '$'}",
            )
        if "contains" in rule:
            expected = str(context.resolve(rule["contains"]))
            _evaluate_rule_assertion(
                result, failures,
                actual=actual, expected=expected,
                operator="contains", label=f"接口数据 {path or '$'}",
            )
    if failures:
        raise AssertionError("; ".join(failures))


def _database_assertions(context: RuntimeDataContext, step: Dict[str, Any], result: Dict[str, Any]) -> None:
    rules = step.get("assert") or step.get("assertions") or {}
    if not isinstance(rules, (dict, list)):
        return
    if isinstance(rules, dict):
        if not rules:
            return
        rules = [rules]
    failures: list = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        path = rule.get("path") or rule.get("json_path") or "$.rows"
        actual = get_path(result, context.resolve(path))
        expected = context.resolve(
            rule.get("contains", rule.get("expected", rule.get("equals", rule.get("value"))))
        )
        _evaluate_rule_assertion(
            result, failures,
            actual=actual, expected=expected,
            operator=rule.get("operator") or ("contains" if "contains" in rule else "equals"),
            label=f"数据库数据 {path}",
        )
    if failures:
        raise AssertionError("; ".join(failures))


def _api_business_outcome(body: Any) -> Optional[Dict[str, Any]]:
    """Infer common business status fields without changing assertion semantics."""
    if not isinstance(body, dict):
        return None
    message = next(
        (
            body.get(key)
            for key in ("msg", "message", "error", "errorMessage", "error_message")
            if body.get(key) not in (None, "")
        ),
        "",
    )
    success = body.get("success")
    if isinstance(success, bool):
        return {
            "success": success,
            "code": body.get("code"),
            "message": message,
            "signal": "success",
        }

    code_key = next(
        (key for key in ("code", "errorCode", "error_code", "statusCode") if key in body),
        "",
    )
    if not code_key:
        return None
    code = body.get(code_key)
    normalized = str(code).strip().lower()
    if normalized in {"0", "200", "ok", "success", "true"} or (
        normalized and set(normalized) == {"0"}
    ):
        inferred = True
    else:
        try:
            numeric_code = int(float(normalized))
        except (TypeError, ValueError):
            inferred = None
        else:
            inferred = False if numeric_code >= 400 or numeric_code < 0 else None
    return {
        "success": inferred,
        "code": code,
        "message": message,
        "signal": code_key,
    }


def _script_body(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _script_request(method: str, url: Any, headers: Any, body: Any) -> Dict[str, Any]:
    return {
        "method": str(method or "GET").upper(),
        "url": str(url or ""),
        "headers": {
            str(key): _stringify(value)
            for key, value in (headers.items() if isinstance(headers, dict) else [])
        },
        "body": _script_body(body),
    }


def _apply_script_result(context: RuntimeDataContext, result: Dict[str, Any]) -> None:
    for name in result.get("deleted") or []:
        context.unset(str(name))
    for name, value in (result.get("variables") or {}).items():
        context.assign(str(name), value)


def _script_stage(
    source: Any,
    context: RuntimeDataContext,
    *,
    request: Dict[str, Any],
    response: Optional[Dict[str, Any]] = None,
) -> tuple[Dict[str, Any], Optional[BaseException]]:
    error: Optional[BaseException] = None
    try:
        script_result = run_script(
            source,
            variables=context.script_variables(),
            request=request,
            response=response,
        )
        _apply_script_result(context, script_result)
    except ScriptAssertionError as exc:
        script_result = exc.result
        _apply_script_result(context, script_result)
        error = exc
    except ScriptExecutionError as exc:
        script_result = exc.result
        error = exc
    summary = {
        "executed": True,
        "ok": error is None,
        "logs": copy.deepcopy(script_result.get("logs") or []),
        "variables": copy.deepcopy(script_result.get("variables") or {}),
        "deleted": copy.deepcopy(script_result.get("deleted") or []),
        "assertions": copy.deepcopy(script_result.get("assertions") or []),
    }
    if error is not None:
        summary["error"] = str(error)
    return summary, error


def _raise_with_runtime_result(exc: BaseException, result: Dict[str, Any]) -> None:
    try:
        setattr(exc, "runtime_result", copy.deepcopy(result))
    except Exception:
        pass
    raise exc


def execute_api_step(step: Dict[str, Any], context: RuntimeDataContext) -> Dict[str, Any]:
    step = _merge_test_object_step(step, "api")
    step_context = _step_runtime_context(context, step)
    method = str(step.get("method") or "GET").upper()
    request_config = step.get("request") if isinstance(step.get("request"), dict) else {}
    raw_url = step.get("url") or step.get("value") or ""
    raw_headers = step.get("headers") or request_config.get("headers") or {}
    raw_json_body = step.get("json", step.get("json_body", request_config.get("json")))
    raw_form_body = step.get("form") or step.get("data") or request_config.get("data")
    scripts = step.get("_scripts") if isinstance(step.get("_scripts"), dict) else {}
    script_results: Dict[str, Any] = {}
    result: Dict[str, Any] = {
        "test_object_ref": copy.deepcopy(step.get("test_object_ref") or {}),
    }
    warnings = step.get("_object_warnings") or []
    if isinstance(warnings, list) and warnings:
        result["warnings"] = [str(item) for item in warnings if str(item).strip()]

    pre_source = scripts.get("pre")
    if str(pre_source or "").strip():
        pre_request = _script_request(
            method,
            raw_url,
            raw_headers,
            raw_json_body if raw_json_body is not None else raw_form_body,
        )
        pre_summary, pre_error = _script_stage(pre_source, step_context, request=pre_request)
        script_results["pre"] = pre_summary
        result["scripts"] = script_results
        if pre_error is not None:
            _raise_with_runtime_result(
                ValueError(f"前置脚本执行失败: {pre_error}"),
                result,
            )

    resolved_url = step_context.resolve(raw_url)
    url = validate_outbound_url(str(resolved_url))
    result["url"] = url
    headers = step_context.resolve(raw_headers)
    params = step_context.resolve(step.get("params") or step.get("query") or request_config.get("params") or {})
    json_body = step_context.resolve(raw_json_body)
    form_body = step_context.resolve(raw_form_body)
    timeout_ms = int(step.get("timeout") or step.get("timeout_ms") or 10000)
    kwargs: Dict[str, Any] = {"headers": headers or {}, "params": params or {}, "timeout": max(0.1, timeout_ms / 1000)}
    basic_auth = step_context.resolve(step.get("_basic_auth"))
    if (
        isinstance(basic_auth, dict)
        and not any(str(key).lower() == "authorization" for key in kwargs["headers"])
    ):
        kwargs["auth"] = (
            str(basic_auth.get("username") or ""),
            str(basic_auth.get("password") or ""),
        )
    if json_body is not None:
        kwargs["json"] = json_body
    elif form_body is not None:
        kwargs["data"] = form_body
    try:
        transport = getattr(context, "http_session", None) or requests
        response = transport.request(method, url, **kwargs)
    except Exception as exc:
        if script_results:
            result["scripts"] = script_results
        _raise_with_runtime_result(exc, result)
    text = response.text
    try:
        body = response.json()
    except ValueError:
        body = text
    result.update({
        "status": response.status_code,
        "headers": dict(response.headers),
        "body": body,
        "text": text,
        "url": response.url,
    })
    business = _api_business_outcome(body)
    if business is not None:
        result["business"] = business
    pending_errors: list[BaseException] = []
    try:
        result["extracted"] = _extract_mapping(
            step_context,
            result.get("body"),
            step.get("extract") or step.get("extract_vars") or {},
        )
    except Exception as exc:
        result["extracted"] = {}
        pending_errors.append(exc)
    try:
        _api_assertions(step_context, step, result)
    except Exception as exc:
        pending_errors.append(exc)

    post_source = scripts.get("post")
    if str(post_source or "").strip():
        post_request = _script_request(
            method,
            response.url,
            kwargs.get("headers") or {},
            json_body if json_body is not None else form_body,
        )
        post_response = {
            "status": response.status_code,
            "code": response.status_code,
            "headers": dict(response.headers),
            "body": text,
        }
        post_summary, post_error = _script_stage(
            post_source,
            step_context,
            request=post_request,
            response=post_response,
        )
        script_results["post"] = post_summary
        if post_error is not None:
            error_type = AssertionError if isinstance(post_error, ScriptAssertionError) else RuntimeError
            pending_errors.append(error_type(f"后置脚本执行失败: {post_error}"))
    if script_results:
        result["scripts"] = script_results

    if pending_errors:
        if len(pending_errors) == 1:
            failure = pending_errors[0]
        else:
            failure = AssertionError("; ".join(str(item) for item in pending_errors))
        _raise_with_runtime_result(failure, result)

    save_as = str(step.get("save_as") or step.get("saveAs") or step.get("name") or "").strip()
    if save_as:
        step_context.assign(save_as, result, category="api")
    return result


def _readonly_sql(sql: Any) -> str:
    value = str(sql or "").strip()
    value = re.sub(r"^(?:--[^\n]*\n|/\*.*?\*/\s*)+", "", value, flags=re.S).strip()
    if not value or not _READ_ONLY_SQL.match(value):
        raise ValueError("数据库步骤仅支持只读 SQL（SELECT/WITH/SHOW/DESCRIBE/EXPLAIN/PRAGMA）")
    if ";" in value.rstrip(";"):
        raise ValueError("数据库步骤不允许执行多条 SQL")
    return value


def _db_connection(config: Dict[str, Any]):
    driver = str(config.get("driver") or config.get("type") or "sqlite").lower()
    dsn = str(config.get("dsn") or "")
    if dsn.startswith("sqlite:///"):
        config = {**config, "path": dsn.removeprefix("sqlite:///")}
        driver = "sqlite"
    if driver in {"sqlite", "sqlite3"}:
        path = str(config.get("path") or config.get("database") or ":memory:")
        if path == ":memory:":
            return sqlite3.connect(path), driver
        absolute_path = os.path.abspath(path)
        if not os.path.isfile(absolute_path):
            raise ValueError(f"SQLite 数据库文件不存在: {absolute_path}")
        return sqlite3.connect(f"file:{absolute_path}?mode=ro", uri=True), driver
    if driver in {"mysql", "mariadb"}:
        try:
            import pymysql
        except ImportError as exc:
            raise RuntimeError("执行 MySQL 数据库步骤需要安装 pymysql") from exc
        return pymysql.connect(
            host=str(config.get("host") or "127.0.0.1"),
            port=int(config.get("port") or 3306),
            user=str(config.get("user") or config.get("username") or ""),
            password=str(config.get("password") or ""),
            database=str(config.get("database") or config.get("db") or "") or None,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=max(1, int(config.get("connect_timeout") or 10)),
            read_timeout=max(1, int(config.get("read_timeout") or 30)),
            write_timeout=max(1, int(config.get("write_timeout") or 30)),
        ), driver
    if driver in {"postgres", "postgresql"}:
        try:
            import psycopg2
        except ImportError as exc:
            raise RuntimeError("执行 PostgreSQL 数据库步骤需要安装 psycopg2-binary") from exc
        return psycopg2.connect(
            host=config.get("host") or "127.0.0.1",
            port=int(config.get("port") or 5432),
            user=config.get("user") or config.get("username") or "",
            password=config.get("password") or "",
            dbname=config.get("database") or config.get("db") or "",
            connect_timeout=max(1, int(config.get("connect_timeout") or 10)),
        ), driver
    raise ValueError(f"不支持的数据库驱动: {driver}")


def execute_database_step(step: Dict[str, Any], context: RuntimeDataContext, flow: Dict[str, Any]) -> Dict[str, Any]:
    step = _merge_test_object_step(step, "sql")
    step_context = _step_runtime_context(context, step)
    config = step_context.resolve(step.get("connection") or step.get("database") or flow.get("database") or {})
    if not isinstance(config, dict):
        raise ValueError("数据库步骤 connection/database 必须是对象")
    sql = _readonly_sql(step_context.resolve(step.get("sql") or step.get("query")))
    params = step_context.resolve(step.get("params") or step.get("parameters") or [])
    connection, driver = _db_connection(config)
    try:
        if driver in {"mysql", "mariadb"}:
            cursor = connection.cursor()
        else:
            cursor = connection.cursor()
        cursor.execute(sql, params)
        columns = [item[0] for item in (cursor.description or [])]
        raw_rows = cursor.fetchmany(max(1, min(int(step.get("max_rows") or 1000), 10000)))
        rows = []
        for row in raw_rows:
            if isinstance(row, dict):
                rows.append(dict(row))
            else:
                rows.append(dict(zip(columns, row)))
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        connection.close()
    result = {
        "driver": driver,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "first": rows[0] if rows else None,
    }
    try:
        _database_assertions(step_context, step, result)
    except Exception as exc:
        # Keep the query result available to the standalone debugger even when
        # a row assertion fails.  The normal flow still raises the original error.
        try:
            setattr(exc, "runtime_result", copy.deepcopy(result))
        except Exception:
            pass
        raise
    save_as = str(step.get("save_as") or step.get("saveAs") or step.get("name") or "").strip()
    if save_as:
        step_context.assign(save_as, result, category="db")
        step_context.assign(save_as, result, category="database")
    result["extracted"] = _extract_mapping(
        step_context,
        result,
        step.get("extract") or step.get("extract_vars") or {},
    )
    return result


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("***" if _SECRET_KEY.search(str(key)) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
