"""Restricted JavaScript runtime shared by mixed API steps.

The contract mirrors the RunnerGo engine script surface closely enough for an
API object to behave the same when it is embedded in a Web UI flow.  Scripts
run in a fresh QuickJS context without Python callbacks, filesystem access or
network access.
"""
from __future__ import annotations

import json
import secrets
from typing import Any, Dict, Optional


DEFAULT_TIMEOUT_MS = 500
MAX_SCRIPT_SIZE = 64 * 1024
MAX_VARIABLE_KEY = 128
MAX_VARIABLE_VALUE_SIZE = 256 * 1024
MAX_LOG_ENTRIES = 100
MAX_LOG_LENGTH = 2048
MEMORY_LIMIT_BYTES = 32 * 1024 * 1024
STACK_LIMIT_BYTES = 512 * 1024


class ScriptExecutionError(RuntimeError):
    """JavaScript failed before its assertions could be evaluated."""

    def __init__(self, message: str, result: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.result = result or _empty_result()


class ScriptAssertionError(AssertionError):
    """JavaScript completed but one or more script assertions failed."""

    def __init__(self, failures: list[str], result: Dict[str, Any]):
        super().__init__("脚本断言失败: " + "; ".join(failures))
        self.result = result
        self.failures = failures


def _empty_result() -> Dict[str, Any]:
    return {
        "value": None,
        "variables": {},
        "deleted": [],
        "logs": [],
        "assertions": [],
    }


def _json_value(value: Any) -> Any:
    """Convert runtime data to a value that can cross the QuickJS boundary."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _script_payload(
    variables: Dict[str, Any],
    request: Optional[Dict[str, Any]],
    response: Optional[Dict[str, Any]],
) -> str:
    return json.dumps(
        {
            "variables": _json_value(variables or {}),
            "request": _json_value(request or {}),
            "response": _json_value(response or {}),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _program(source: str, payload: str) -> str:
    # Randomized private bindings keep user code from mutating the harness'
    # accounting arrays through ordinary lexical-name guessing.
    prefix = "__runnergo_" + secrets.token_hex(12)
    state = prefix + "_state"
    current = prefix + "_current"
    changed = prefix + "_changed"
    deleted = prefix + "_deleted"
    logs = prefix + "_logs"
    assertions = prefix + "_assertions"
    runtime_error = prefix + "_runtime_error"
    utf8_length = prefix + "_utf8_length"
    clone = prefix + "_clone"
    stringify = prefix + "_stringify"
    validate_key = prefix + "_validate_key"
    validate_value = prefix + "_validate_value"
    log = prefix + "_log"
    record_assertion = prefix + "_record_assertion"

    return f"""
(function() {{
  "use strict";
  const {state} = {payload};
  const {current} = Object.assign({{}}, {state}.variables || {{}});
  const {changed} = {{}};
  const {deleted} = [];
  const {logs} = [];
  const {assertions} = [];
  let {runtime_error} = null;

  const {stringify} = JSON.stringify.bind(JSON);
  const {clone} = function(value) {{
    if (value === undefined) return null;
    const encoded = {stringify}(value);
    if (encoded === undefined) return null;
    return JSON.parse(encoded);
  }};
  const {utf8_length} = function(value) {{
    let size = 0;
    for (const character of value) {{
      const code = character.codePointAt(0);
      size += code <= 0x7f ? 1 : code <= 0x7ff ? 2 : code <= 0xffff ? 3 : 4;
    }}
    return size;
  }};
  const {validate_key} = function(value) {{
    const key = String(value === undefined || value === null ? "" : value).trim();
    if (!key) throw new Error("变量名不能为空");
    if ([...key].length > {MAX_VARIABLE_KEY}) throw new Error("变量名超过 {MAX_VARIABLE_KEY} 个字符");
    if (/[\\r\\n\\0]/.test(key)) throw new Error("变量名包含非法控制字符");
    return key;
  }};
  const {validate_value} = function(value) {{
    const normalized = {clone}(value);
    const encoded = {stringify}(normalized);
    if ({utf8_length}(encoded) > {MAX_VARIABLE_VALUE_SIZE}) {{
      throw new Error("变量值超过 {MAX_VARIABLE_VALUE_SIZE // 1024}KB 限制");
    }}
    return normalized;
  }};
  const {log} = function(level, args) {{
    if ({logs}.length >= {MAX_LOG_ENTRIES}) return;
    let message = Array.prototype.map.call(args, function(value) {{ return String(value); }}).join(" ");
    if (message.length > {MAX_LOG_LENGTH}) message = message.slice(0, {MAX_LOG_LENGTH});
    {logs}.push(level + ": " + message);
  }};
  const {record_assertion} = function(name, passed, message) {{
    const cleanName = String(name || "assertion").trim() || "assertion";
    const cleanMessage = String(message || cleanName).trim() || cleanName;
    {assertions}.push({{name: cleanName, passed: Boolean(passed), message: cleanMessage}});
  }};

  const vars = Object.freeze({{
    get: function(key) {{ return {current}[String(key).trim()]; }},
    has: function(key) {{ return Object.prototype.hasOwnProperty.call({current}, String(key).trim()); }},
    set: function(key, value) {{
      const cleanKey = {validate_key}(key);
      const cleanValue = {validate_value}(value);
      {current}[cleanKey] = cleanValue;
      {changed}[cleanKey] = cleanValue;
      const index = {deleted}.indexOf(cleanKey);
      if (index >= 0) {deleted}.splice(index, 1);
      return value;
    }},
    unset: function(key) {{
      const cleanKey = {validate_key}(key);
      delete {current}[cleanKey];
      delete {changed}[cleanKey];
      if ({deleted}.indexOf(cleanKey) < 0) {deleted}.push(cleanKey);
    }},
    all: function() {{ return {clone}({current}); }}
  }});
  const request = Object.freeze({clone}({state}.request || {{}}));
  const responseData = {clone}({state}.response || {{}});
  const response = Object.freeze(Object.assign(responseData, {{
    text: function() {{ return String(responseData.body || ""); }},
    json: function() {{ return JSON.parse(String(responseData.body || "")); }}
  }}));
  const console = Object.freeze({{
    log: function() {{ {log}("log", arguments); }},
    info: function() {{ {log}("info", arguments); }},
    warn: function() {{ {log}("warn", arguments); }},
    error: function() {{ {log}("error", arguments); }}
  }});
  const assert = function(passed, message) {{
    const name = String(message === undefined ? "assertion" : message).trim() || "assertion";
    {record_assertion}(name, Boolean(passed), name);
  }};
  const fail = function(message) {{
    const name = String(message === undefined ? "script failed" : message).trim() || "script failed";
    {record_assertion}(name, false, name);
  }};
  const pm = Object.freeze({{
    variables: vars,
    environment: vars,
    request: request,
    response: response,
    test: function(name, callback) {{
      const cleanName = String(name || "pm.test").trim() || "pm.test";
      try {{
        callback();
        {record_assertion}(cleanName, true, cleanName);
      }} catch (error) {{
        {record_assertion}(cleanName, false, error && error.message ? error.message : String(error));
      }}
    }}
  }});

  Object.defineProperty(globalThis, "eval", {{value: undefined, writable: false, configurable: false}});
  Object.defineProperty(globalThis, "Function", {{value: undefined, writable: false, configurable: false}});
  const blockedFunctionPrototypes = [
    Object.getPrototypeOf(function() {{}}),
    Object.getPrototypeOf(function*() {{}}),
    Object.getPrototypeOf(async function() {{}}),
    Object.getPrototypeOf(async function*() {{}})
  ];
  for (const prototype of blockedFunctionPrototypes) {{
    try {{
      Object.defineProperty(prototype, "constructor", {{
        value: undefined, writable: false, configurable: false
      }});
    }} catch (_) {{}}
  }}

  try {{
    (function() {{
{source}
    }}).call(undefined);
  }} catch (error) {{
    {runtime_error} = error && error.message ? String(error.message) : String(error);
  }}

  return {stringify}({{
    value: null,
    variables: {changed},
    deleted: {deleted},
    logs: {logs},
    assertions: {assertions},
    runtime_error: {runtime_error}
  }});
}})()
"""


def _validated_result(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ScriptExecutionError("JavaScript 运行时返回了无效结果")
    result = _empty_result()
    variables = value.get("variables")
    if isinstance(variables, dict):
        for key, item in variables.items():
            clean_key = str(key).strip()
            if not clean_key or len(clean_key) > MAX_VARIABLE_KEY or any(char in clean_key for char in "\r\n\x00"):
                raise ScriptExecutionError("JavaScript 返回了无效变量名")
            encoded = json.dumps(item, ensure_ascii=False, default=str).encode("utf-8")
            if len(encoded) > MAX_VARIABLE_VALUE_SIZE:
                raise ScriptExecutionError("JavaScript 返回的变量值超过 256KB 限制")
            result["variables"][clean_key] = _json_value(item)
    deleted = value.get("deleted")
    if isinstance(deleted, list):
        result["deleted"] = [str(item).strip() for item in deleted if str(item).strip()][:1000]
    logs = value.get("logs")
    if isinstance(logs, list):
        result["logs"] = [str(item)[:MAX_LOG_LENGTH] for item in logs[:MAX_LOG_ENTRIES]]
    assertions = value.get("assertions")
    if isinstance(assertions, list):
        for item in assertions[:1000]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "assertion")[:MAX_LOG_LENGTH]
            message = str(item.get("message") or name)[:MAX_LOG_LENGTH]
            result["assertions"].append({
                "name": name,
                "passed": bool(item.get("passed")),
                "message": message,
            })
    result["value"] = value.get("value")
    runtime_error = str(value.get("runtime_error") or "").strip()
    if runtime_error:
        raise ScriptExecutionError(runtime_error, result)
    failures = [
        f"{item['name']}: {item['message']}"
        for item in result["assertions"]
        if not item["passed"]
    ]
    if failures:
        raise ScriptAssertionError(failures, result)
    return result


def run_script(
    source: Any,
    *,
    variables: Optional[Dict[str, Any]] = None,
    request: Optional[Dict[str, Any]] = None,
    response: Optional[Dict[str, Any]] = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> Dict[str, Any]:
    """Execute one RunnerGo-compatible script in a fresh restricted context."""
    script = str(source or "")
    if not script.strip():
        return _empty_result()
    if len(script.encode("utf-8")) > MAX_SCRIPT_SIZE:
        raise ScriptExecutionError("脚本内容超过 64KB 限制")
    try:
        import quickjs
    except ImportError as exc:  # pragma: no cover - production image owns this dependency
        raise ScriptExecutionError("JavaScript 运行时不可用，请安装 quickjs") from exc

    context = quickjs.Context()
    if hasattr(context, "set_memory_limit"):
        context.set_memory_limit(MEMORY_LIMIT_BYTES)
    if hasattr(context, "set_max_stack_size"):
        context.set_max_stack_size(STACK_LIMIT_BYTES)
    if hasattr(context, "set_time_limit"):
        context.set_time_limit(max(0.001, min(int(timeout_ms), DEFAULT_TIMEOUT_MS) / 1000))
    try:
        encoded = context.eval(_program(script, _script_payload(variables or {}, request, response)))
    except Exception as exc:
        message = str(exc).strip()
        lowered = message.lower()
        if "interrupted" in lowered or "time limit" in lowered:
            message = "脚本执行超时"
        elif "out of memory" in lowered:
            message = "脚本内存超过限制"
        elif "syntaxerror" in lowered or "syntax error" in lowered:
            message = "脚本语法错误: " + message
        raise ScriptExecutionError(message or "JavaScript 执行失败") from exc
    try:
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ScriptExecutionError("JavaScript 运行时返回了无效 JSON") from exc
    return _validated_result(decoded)
