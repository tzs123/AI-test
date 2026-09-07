"""Swagger/OpenAPI Schema 解析器：从接口文档提取参数定义。

支持：
- swagger_url：远程拉取 OpenAPI 2.0 / 3.0 JSON（或 YAML）
- swagger_doc：直接传入文档 dict
- api：路径（支持 /user/register 或带方法 "POST /user/register"）
"""
from __future__ import annotations

import re
from typing import Any, Optional

import requests
import yaml

from backend.url_security import validate_outbound_url


def load_swagger_doc(swagger_url: str = "", swagger_doc: Optional[dict] = None) -> dict:
    """加载 OpenAPI 文档。优先使用传入 dict，否则拉取 swagger_url。"""
    if isinstance(swagger_doc, dict) and swagger_doc:
        return swagger_doc
    swagger_url = str(swagger_url or "").strip()
    if not swagger_url:
        raise ValueError("需要 swagger_url 或 swagger_doc 提供接口文档")
    try:
        validate_outbound_url(swagger_url)
    except ValueError as exc:
        raise ValueError(f"Swagger 地址被安全策略拦截: {exc}") from exc
    response = requests.get(swagger_url, timeout=15)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    text = response.text
    if "yaml" in content_type or "yml" in content_type or text.lstrip().startswith(("openapi:", "swagger:")):
        return yaml.safe_load(text) or {}
    try:
        return response.json()
    except ValueError:
        return yaml.safe_load(text) or {}


def _method_of(api: str) -> tuple[Optional[str], str]:
    """从 api 描述中提取 HTTP 方法与路径。"""
    api = str(api or "").strip()
    match = re.match(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(.+)$", api, re.IGNORECASE)
    if match:
        return match.group(1).upper(), match.group(2).strip().rstrip("/")
    return None, api.rstrip("/")


def resolve_api(doc: dict, api: str) -> dict:
    """在 OpenAPI 文档中找到目标接口，返回 {path, method, parameters, request_body, summary}。"""
    method, path = _method_of(api)
    paths = doc.get("paths") or {}
    candidates = []
    for doc_path, methods in paths.items():
        normalized = doc_path.rstrip("/")
        if normalized == path or normalized.endswith(path) or path.endswith(normalized):
            candidates.append((doc_path, methods))
    if not candidates:
        # 尝试按路径段模糊匹配
        path_segments = [seg for seg in path.split("/") if seg]
        for doc_path, methods in paths.items():
            doc_segments = [seg for seg in doc_path.split("/") if seg and not seg.startswith("{")]
            if doc_segments and path_segments and doc_segments[-1] == path_segments[-1]:
                candidates.append((doc_path, methods))
    if not candidates:
        raise ValueError(f"在接口文档中未找到 API: {api}")
    doc_path, methods = candidates[0]
    if method is None:
        method = next(iter(methods), "get")
    method = method.lower()
    operation = methods.get(method) or methods.get(method.upper())
    if operation is None:
        raise ValueError(f"接口 {doc_path} 不支持方法 {method.upper()}")
    parameters = []
    for param in operation.get("parameters") or []:
        schema = param.get("schema") or {}
        if param.get("in") == "body":
            schema = param.get("schema") or {}
        parameters.append({
            "name": str(param.get("name") or ""),
            "in": str(param.get("in") or "query"),
            "required": bool(param.get("required", False)),
            "type": str(schema.get("type") or ""),
            "format": str(schema.get("format") or ""),
            "schema": schema,
            "description": str(param.get("description") or ""),
        })
    request_body = operation.get("requestBody") or {}
    content = (request_body.get("content") or {}).get("application/json") or {}
    body_schema = content.get("schema") or {}
    if body_schema:
        for name, prop in (body_schema.get("properties") or {}).items():
            parameters.append({
                "name": str(name),
                "in": "body",
                "required": bool(name in (body_schema.get("required") or [])),
                "type": str(prop.get("type") or ""),
                "format": str(prop.get("format") or ""),
                "schema": prop,
                "description": str(prop.get("description") or ""),
            })
    return {
        "path": doc_path,
        "method": method.upper(),
        "summary": str(operation.get("summary") or ""),
        "parameters": parameters,
        "operation": operation,
    }


def infer_parameter_type(name: str, declared_type: str = "", declared_format: str = "") -> str:
    """根据参数名推断业务类型（用于选择生成器与异常规则）。"""
    name = str(name or "").lower()
    declared_type = str(declared_type or "").lower()
    declared_format = str(declared_format or "").lower()
    if declared_format in {"int32", "int64", "float", "double", "number", "integer"} or declared_type in {"integer", "number"}:
        return "number"
    if declared_type == "boolean":
        return "boolean"
    if declared_type == "array":
        return "array"
    if declared_type == "date" or declared_format == "date" or declared_format == "date-time":
        return "date"
    if "email" in name or declared_format == "email":
        return "email"
    if "phone" in name or "mobile" in name:
        return "phone"
    if "password" in name or "pwd" in name:
        return "password"
    if "username" in name or "user_name" in name or "account" in name or "login" in name:
        return "username"
    if "name" in name:
        return "name"
    if "age" in name or "count" in name or "num" in name or "amount" in name or "price" in name:
        return "number"
    if "id" == name or name.endswith("_id") or name.endswith("id"):
        return "id"
    if "url" in name or "link" in name or "uri" in name:
        return "url"
    if "code" in name or "token" in name:
        return "code"
    if "date" in name or "time" in name:
        return "date"
    if "search" in name or "keyword" in name or "query" in name:
        return "search"
    if declared_type == "string" or not declared_type:
        return "string"
    return "string"
