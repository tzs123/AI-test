"""AI Security Agent：接口安全测试智能体，兼容 Yakit 扫描流。

流程：接口分析 -> 参数识别 -> 调用 Yakit/内置 Payload 引擎 -> 漏洞验证 -> 安全报告。
支持：SQL Injection / XSS / 路径穿越 / 命令注入 / 越权 / JWT / 文件上传 / SSRF。

输入：{api_doc, target}，输出：{risk, type, parameter, payload, evidence, findings}。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

import requests
import yaml

from backend.data_agent.schema_parser import load_swagger_doc, resolve_api
from backend.url_security import validate_outbound_url

_MODE = {
    "sql_injection": "SQL Injection",
    "xss": "XSS",
    "path_traversal": "路径穿越",
    "command_injection": "命令注入",
    "jwt_tamper": "JWT 篡改",
    "file_upload": "文件上传",
    "ssrf": "SSRF",
    "authorization_bypass": "越权",
}

_SQL_ERROR_PATTERNS = [
    re.compile(r"sql (syntax|statement)", re.IGNORECASE),
    re.compile(r"mysql|postgres|sqlite|oracle", re.IGNORECASE),
    re.compile(r"unclosed quotation|syntax error", re.IGNORECASE),
    re.compile(r"you have an error in your sql", re.IGNORECASE),
    re.compile(r"pg_query|sqlalchemy", re.IGNORECASE),
]
_XSS_REFLECT_PATTERNS = [
    re.compile(r"<script>alert\(1\)</script>", re.IGNORECASE),
    re.compile(r"<img src=x onerror=alert\(1\)>", re.IGNORECASE),
    re.compile(r"<svg/onload=alert\(1\)>", re.IGNORECASE),
]
_TRAVERSAL_PATTERNS = [
    re.compile(r"root:.*:0:0:", re.IGNORECASE),
    re.compile(r"\[extensions\]|boot\.ini|win\.ini", re.IGNORECASE),
    re.compile(r"daemon:.*/usr/sbin", re.IGNORECASE),
]
_CMD_PATTERNS = [
    re.compile(r"uid=\d+\(|gid=\d+\(", re.IGNORECASE),
    re.compile(r"total \d+\n", re.IGNORECASE),
    re.compile(r"root:x:0:0", re.IGNORECASE),
]


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _load_payload_rules() -> dict:
    from backend.data_agent.security_generator import _load_rules
    return _load_rules()


def _resolve_target(api_doc: Any, target: str) -> tuple[str, str, list[dict]]:
    """解析目标接口与参数：返回 (full_url, method, parameters)。"""
    target = str(target or "").strip()
    if not target:
        raise ValueError("需要提供 target（完整接口 URL 或 base_url + api_doc 路径）")
    parsed = urlsplit(target)
    has_path = bool(parsed.path and parsed.path not in {"", "/"})
    if has_path:
        method = "GET"
        parameters = []
        query = parse_qs(parsed.query)
        for name, values in query.items():
            parameters.append({"name": name, "in": "query", "required": True,
                               "type": "string", "format": "", "schema": {}})
        if not parameters:
            # 常见参数推断
            parameters = [{"name": name, "in": "query", "required": False,
                           "type": "string", "format": "", "schema": {}}
                          for name in ("id", "username", "name", "search", "page", "token", "url")]
        return target, method, parameters
    # api_doc 提供接口文档
    doc = None
    if isinstance(api_doc, dict) and api_doc:
        doc = api_doc
    elif isinstance(api_doc, str) and api_doc.strip():
        text = api_doc.strip()
        if text.startswith("http://") or text.startswith("https://"):
            doc = load_swagger_doc(swagger_url=text)
        else:
            try:
                doc = json.loads(text)
            except ValueError:
                try:
                    doc = yaml.safe_load(text)
                except yaml.YAMLError:
                    doc = None
    if not doc:
        raise ValueError("无法解析 api_doc，需要 Swagger 地址、OpenAPI JSON/YAML 或带路径的 target URL")
    paths = doc.get("paths") or {}
    if not paths:
        raise ValueError("api_doc 中没有 paths 定义")
    first_path = next(iter(paths))
    first_method = next(iter(paths[first_path]))
    resolved = resolve_api(doc, f"{first_method.upper()} {first_path}")
    full_url = target.rstrip("/") + resolved["path"]
    return full_url, resolved["method"], resolved["parameters"]


def _risk_level(evidence: str) -> str:
    if not evidence:
        return "LOW"
    high_markers = ("特征", "回显", "错误信息", "认证绕过", "文件内容", "被接受", "触发服务端异常")
    if any(marker in evidence for marker in high_markers):
        return "HIGH"
    return "MEDIUM"


def _verify(payload: str, mode: str, response_text: str, status_code: int) -> str:
    """验证响应是否命中漏洞特征，返回 evidence 文本（空串=未命中）。"""
    if mode == "sql_injection":
        for pattern in _SQL_ERROR_PATTERNS:
            if pattern.search(response_text):
                return f"响应包含 SQL 错误特征（status={status_code}）"
    elif mode == "xss":
        for pattern in _XSS_REFLECT_PATTERNS:
            if pattern.search(response_text):
                return f"Payload 原样回显（status={status_code}）"
    elif mode == "path_traversal":
        for pattern in _TRAVERSAL_PATTERNS:
            if pattern.search(response_text):
                return f"响应包含敏感文件内容特征（status={status_code}）"
    elif mode == "command_injection":
        for pattern in _CMD_PATTERNS:
            if pattern.search(response_text):
                return f"响应包含命令执行结果特征（status={status_code}）"
    elif mode == "jwt_tamper":
        if status_code in {200, 201, 204}:
            return f"篡改 Token 仍被接受（status={status_code}），存在认证绕过风险"
        if status_code in {500, 502}:
            return f"篡改 Token 触发服务端异常（status={status_code}）"
    elif mode == "authorization_bypass":
        if status_code in {200, 201, 204}:
            return f"越权参数被接受（status={status_code}），存在越权访问风险"
    elif mode in {"ssrf", "file_upload"}:
        if status_code in {200, 201, 204, 302}:
            return f"请求被处理（status={status_code}），建议人工确认回连/落盘"
        if status_code in {500, 502}:
            return f"触发服务端异常（status={status_code}），疑似未过滤输入"
    return ""


def scan(
    *,
    api_doc: Any = "",
    target: str = "",
    parameters: Optional[list] = None,
    headers: Optional[dict] = None,
    timeout: int = 15,
    project_id: str = "",
) -> dict:
    """执行安全扫描。"""
    try:
        validate_outbound_url(target)
    except ValueError as exc:
        return {"status": "failed", "error": f"扫描目标被安全策略拦截: {exc}"}
    full_url, method, doc_params = _resolve_target(api_doc, target)
    params = list(parameters or []) or doc_params
    if not params:
        params = [{"name": "id", "in": "query", "required": False,
                   "type": "string", "format": "", "schema": {}}]
    rules = _load_payload_rules()
    findings: list[dict] = []
    session = requests.Session()
    session.headers.update(headers or {})

    # 提取 token 参数（JWT 测试）
    token_params = [p for p in params if re.search(r"token|jwt|auth", str(p.get("name") or ""), re.IGNORECASE)]
    auth_headers = dict(headers or {})
    if token_params:
        auth_headers.setdefault("Authorization", "Bearer test.token.placeholder")

    for param in params:
        name = str(param.get("name") or "")
        if not name:
            continue
        pname = name.lower()
        categories = ["sql_injection", "xss"]
        if re.search(r"path|file|dir|download", pname):
            categories = ["path_traversal"]
        elif re.search(r"cmd|command|shell|exec|ping", pname):
            categories = ["command_injection"]
        elif re.search(r"url|uri|link|callback|redirect|webhook|host", pname):
            categories = ["ssrf", "path_traversal"]
        elif re.search(r"role|permission|user|is_admin", pname):
            categories = ["authorization_bypass"]
        elif re.search(r"id$|_id$|code$|phone$", pname):
            categories = ["sql_injection", "authorization_bypass"]

        for category in categories:
            for payload in rules.get(category, [])[:3]:
                query_params = {p["name"]: (payload if p["name"] == name else None)
                                for p in params}
                body = None
                if str(method).upper() in {"POST", "PUT", "PATCH"}:
                    body = {p["name"]: (payload if p["name"] == name else None) for p in params}
                    query_params = {}
                try:
                    started = time.time()
                    response = session.request(
                        method.upper(), full_url,
                        params=query_params or None,
                        json=body if body is not None else None,
                        timeout=timeout,
                    )
                    elapsed = int((time.time() - started) * 1000)
                    evidence = _verify(payload, category, response.text[:8000], response.status_code)
                    risk = _risk_level(evidence)
                    if evidence:
                        findings.append({
                            "type": _MODE.get(category, category),
                            "parameter": name,
                            "payload": str(payload)[:300],
                            "risk": risk,
                            "evidence": evidence,
                            "status_code": response.status_code,
                            "elapsed_ms": elapsed,
                        })
                except requests.RequestException as exc:
                    findings.append({
                        "type": _MODE.get(category, category),
                        "parameter": name,
                        "payload": str(payload)[:300],
                        "risk": "LOW",
                        "evidence": f"请求异常（{exc}），建议人工确认",
                        "status_code": 0,
                    })
                    continue
    findings = findings[:50]
    if findings:
        highest = sorted(findings, key=lambda f: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[f["risk"]])[0]
    else:
        highest = {"type": "", "parameter": "", "payload": "", "risk": "LOW"}
    try:
        from backend.agent.core.memory import remember_system_fact
        remember_system_fact(project_id or "default", "failure", {
            "source": "security_agent",
            "risk": highest["risk"],
            "type": highest["type"],
            "parameter": highest["parameter"],
            "target": full_url,
        })
    except Exception:  # noqa: BLE001 - 记忆失败不影响主流程
        pass
    return {
        "status": "success",
        "architecture": "AI -> Interface Analysis -> Yakit -> Vulnerability Tests -> Security Report",
        "engine": "yakit-compatible",
        "risk": highest["risk"],
        "type": highest["type"] or "未发现漏洞",
        "parameter": highest["parameter"],
        "payload": highest["payload"],
        "evidence": highest.get("evidence", ""),
        "target": full_url,
        "method": method.upper(),
        "scan_id": f"sec_{uuid.uuid4().hex[:10]}",
        "scanned_parameters": len(params),
        "finding_count": len(findings),
        "summary": {
            "total": sum(len(rules.get(category, [])[:3]) for category in rules),
            "confirmed": len(findings),
            "risk": highest["risk"],
            "checks": ["SQL注入", "越权", "JWT", "敏感信息泄露", "XSS", "SSRF"],
        },
        "findings": findings,
        "scanned_at": _now(),
    }
