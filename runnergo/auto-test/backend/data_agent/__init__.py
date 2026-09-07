"""AI Test Data Agent：根据 Swagger/OpenAPI 自动生成测试数据。

输入：swagger_url（或 swagger_doc）+ api + mode（normal/boundary/security）
输出：{asset_id, data_count, cases}
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

from backend import db
from backend.data_agent.boundary_generator import generate_boundary_data
from backend.data_agent.data_generator import generate_normal_data
from backend.data_agent.schema_parser import load_swagger_doc, resolve_api
from backend.data_agent.security_generator import generate_security_data


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _asset_id() -> str:
    return f"api_data_{uuid.uuid4().hex[:10]}"


def generate_from_api(
    *,
    swagger_url: str = "",
    swagger_doc: Optional[dict] = None,
    api: str = "",
    mode: Optional[list[str]] = None,
    project_id: str = "",
    count: int = 1000,
    created_by: str = "",
) -> dict:
    """从接口文档生成测试数据资产。"""
    api = str(api or "").strip()
    modes = [str(m).strip().lower() for m in (mode or ["normal", "boundary", "security"]) if str(m).strip()]
    if not modes:
        modes = ["normal"]

    doc = load_swagger_doc(swagger_url=swagger_url, swagger_doc=swagger_doc)
    if not api:
        paths = doc.get("paths") or {}
        if not paths:
            raise ValueError("OpenAPI 文档中没有可用接口")
        first_path = next(iter(paths))
        first_methods = paths.get(first_path) or {}
        first_method = next(iter(first_methods), "GET")
        api = f"{str(first_method).upper()} {first_path}"
    resolved = resolve_api(doc, api)
    parameters = resolved["parameters"]
    if not parameters:
        raise ValueError(f"接口 {resolved['method']} {resolved['path']} 没有可解析的参数")

    normal_rows: list[dict] = []
    cases: list[dict] = []
    if "normal" in modes:
        normal_rows = generate_normal_data(parameters, count)
        cases.extend([
            {"case_type": "normal", "mode": "normal", "parameter": p["name"], "value": row.get(p["name"]), "label": "正常数据"}
            for row in normal_rows[:20] for p in parameters if p["name"] in row
        ])
    if "boundary" in modes:
        cases.extend(generate_boundary_data(parameters))
    if "security" in modes:
        cases.extend(generate_security_data(parameters))

    data_count = len(normal_rows) + len(cases)
    asset_id = _asset_id()
    now = _now()
    payload = {
        "api": resolved["path"],
        "method": resolved["method"],
        "parameters": parameters,
        "normal_rows": normal_rows,
        "cases": cases,
    }
    db.execute(
        "INSERT INTO test_assets(id, asset_type, name, project_id, source, count, fields, payload, status, created_by, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            asset_id,
            "API_DATA",
            f"{resolved['method']} {resolved['path']} 数据资产",
            project_id or "",
            "data-agent",
            data_count,
            json.dumps([p["name"] for p in parameters], ensure_ascii=False),
            json.dumps(payload, ensure_ascii=False, default=str),
            "success",
            created_by or "",
            now,
            now,
        ),
    )
    data_center_rows = normal_rows or [
        {
            "case_type": case.get("case_type"),
            "category": case.get("category"),
            str(case.get("parameter") or "value"): case.get("value"),
            "expected": case.get("expected"),
        }
        for case in cases[:100]
        if isinstance(case, dict)
    ]
    data_center_asset = {"synced": False, "reason": "not_attempted"}
    try:
        from backend.agent.tools import _sync_rows_to_test_data_center
        data_center_asset = _sync_rows_to_test_data_center(
            asset_type="API_DATA",
            name=f"{resolved['method']} {resolved['path']} 数据资产",
            rows=data_center_rows,
            tags=["data-agent", "api-data", *modes],
            source_asset_id=asset_id,
        )
    except Exception as exc:  # noqa: BLE001 - 同步失败不影响数据生成
        data_center_asset = {"synced": False, "reason": str(exc)}
    try:
        from backend.agent.core.memory import remember_system_fact
        remember_system_fact(project_id or "default", "api", {
            "method": resolved["method"],
            "path": resolved["path"],
            "summary": resolved.get("summary", ""),
            "parameters": [p["name"] for p in parameters],
            "asset_id": asset_id,
        })
    except Exception:  # noqa: BLE001 - 记忆失败不影响主流程
        pass
    return {
        "status": "success",
        "agent": "AI Test Data Generator",
        "asset_id": asset_id,
        "data_center_asset": data_center_asset,
        "data_count": data_count,
        "api": resolved["path"],
        "method": resolved["method"],
        "parameters": [p["name"] for p in parameters],
        "modes": modes,
        "cases": cases,
        "sample": normal_rows[:3],
    }
